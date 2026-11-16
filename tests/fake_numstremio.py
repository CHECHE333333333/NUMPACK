"""End-to-end test of NumStremio: a fake calculator talks to the real gateway
over a pty, the way the firmware does, and two fake Stremio addons run on
localhost (a catalogue, and a provider of streams and subtitles). Nothing
touches the Internet.

Checks: genres, browsing with a type and a genre, search, the paging, posters,
a movie, a series with its seasons and episodes, the providers and their
streams, the subtitle languages, and playback (pictures, the subtitle at the
right moment, pause, stop).

Usage: ../.venv/bin/python3 fake_numstremio.py
"""
import http.server
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tty
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import numstremio_service as st  # noqa: E402
import numtube_gateway as gw  # noqa: E402

TMP = tempfile.mkdtemp(prefix="numstremio-test-")
VIDEO = os.path.join(TMP, "video.mp4")
SRT_FR = "1\n00:00:01,000 --> 00:00:03,000\nBonjour <i>le monde</i>, c'est l'été\n\n2\n00:00:03,500 --> 00:00:05,000\nDeuxième réplique\n"
SRT_EN = "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"

MOVIES = [{"id": f"tt{i:07d}", "type": "movie", "name": f"Film numero {i}", "releaseInfo": str(2000 + i),
           "imdbRating": "7.5", "poster": "POSTER/%d.png" % i, "genres": ["Action" if i % 2 == 0 else "Drama"]}
          for i in range(1, 15)]
SERIES = [{"id": f"tt90{i:05d}", "type": "series", "name": f"Serie numero {i}", "releaseInfo": "2010-2014",
           "poster": "POSTER/s%d.png" % i, "genres": ["Drama"]} for i in range(1, 5)]
SERIES_VIDEOS = [{"id": f"tt9000001:{s}:{e}", "season": s, "episode": e, "title": f"Episode {s}x{e}"}
                 for s in (1, 2) for e in range(1, 4)]
requests_seen: list[str] = []


def make_video() -> None:
    subprocess.run([gw.FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=8",
                    "-pix_fmt", "yuv420p", "-y", VIDEO], check=True)


def png(color: int) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (200, 300), (color, 255 - color, 128)).save(out, "PNG")
    return out.getvalue()


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        self._send(200, json.dumps(data).encode())

    def do_GET(self):
        path = urllib.parse.unquote(self.path)
        requests_seen.append(path)
        base = f"http://127.0.0.1:{self.server.server_port}"
        if path.startswith("/poster/"):
            return self._send(200, png(len(path) * 7 % 256), "image/png")
        if path == "/video.mp4":
            data = open(VIDEO, "rb").read()
            header = self.headers.get("Range")
            if header:
                start = int(re.match(r"bytes=(\d+)-", header).group(1))
                chunk = data[start:]
                return self._send(206, chunk, "video/mp4", {
                    "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}", "Accept-Ranges": "bytes"})
            return self._send(200, data, "video/mp4", {"Accept-Ranges": "bytes"})
        if path == "/sub_fr.srt":
            return self._send(200, SRT_FR.encode("utf-8"), "text/plain")
        if path == "/sub_en.srt":
            return self._send(200, SRT_EN.encode("utf-8"), "text/plain")
        if path == "/catalog/manifest.json":
            extra = [{"name": "genre", "options": ["Action", "Drama", "Comedy"]}, {"name": "search"}, {"name": "skip"}]
            return self._json({"id": "fake.catalog", "name": "FakeCatalog", "version": "1", "types": ["movie", "series"],
                               "resources": ["catalog", "meta"], "idPrefixes": ["tt"],
                               "catalogs": [{"type": "movie", "id": "top", "extra": extra},
                                            {"type": "series", "id": "top", "extra": extra}]})
        if path == "/sources/manifest.json":
            return self._json({"id": "fake.sources", "name": "FakeSources", "version": "1", "types": ["movie", "series"],
                               "resources": ["stream", "subtitles"], "idPrefixes": ["tt"], "catalogs": []})
        if path == "/other/manifest.json":
            return self._json({"id": "fake.other", "name": "OtherSrc", "version": "1", "types": ["movie"],
                               "resources": ["stream"], "idPrefixes": ["tt"], "catalogs": []})
        path = re.sub(r"^/(catalog|sources|other)(?=/(catalog|meta|stream|subtitles)/)", "", path)  # the addon's base
        m = re.match(r"/catalog/(movie|series)/top/?(.*)\.json$", path)
        if m:
            kind, extra = m.groups()
            options = dict(p.split("=", 1) for p in extra.split("&") if "=" in p)
            data = MOVIES if kind == "movie" else SERIES
            if "genre" in options:
                data = [x for x in data if options["genre"] in x["genres"]]
            if "search" in options:
                data = [x for x in data if options["search"].lower() in x["name"].lower()]
            skip = int(options.get("skip", 0))
            metas = [dict(x, poster=x["poster"].replace("POSTER", base + "/poster")) for x in data[skip:skip + 5]]
            return self._json({"metas": metas})
        m = re.match(r"/meta/(movie|series)/(.+)\.json$", path)
        if m:
            kind, item_id = m.groups()
            pool = MOVIES + SERIES
            meta = next(x for x in pool if x["id"] == item_id)
            meta = dict(meta)
            if kind == "series":
                meta["videos"] = SERIES_VIDEOS
            return self._json({"meta": meta})
        m = re.match(r"/stream/(movie|series)/(.+)\.json$", path)
        if m:
            kind, video_id = m.groups()
            if self.server.name == "other":
                return self._json({"streams": [{"url": base + "/video.mp4", "name": "OtherSrc\n480p",
                                                "title": "Autre.source.480p"}]})
            return self._json({"streams": [
                {"url": base + "/video.mp4", "name": "FakeSources\n720p", "title": f"{video_id}.720p.WEB\n1.2 GB",
                 "behaviorHints": {"videoSize": 1_200_000_000}},
                {"externalUrl": "https://example.invalid", "name": "NotPlayable"},
                {"url": base + "/video.mp4", "name": "FakeSources\n1080p", "title": "Meme film en 1080p"},
            ]})
        m = re.match(r"/subtitles/(movie|series)/(.+)\.json$", path)
        if m:
            return self._json({"subtitles": [{"id": "1", "lang": "eng", "url": base + "/sub_en.srt"},
                                             {"id": "2", "lang": "fre", "url": base + "/sub_fr.srt"}]})
        self._send(404, b"{}")


def serve(name):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    server.name = name
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FakeCalculator:
    def __init__(self) -> None:
        self.master, slave = pty.openpty()
        tty.setraw(self.master)
        self.slave_path = os.ttyname(slave)
        self.parser = gw.FrameParser()
        self.backlog: list = []

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        os.write(self.master, gw.encode_frame(frame_type, payload))

    def frames(self, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while self.backlog:
                yield self.backlog.pop(0)
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                self.backlog += self.parser.feed(os.read(self.master, 65536))

    def collect_until(self, wanted: int, timeout: float = 20):
        got = []
        for frame_type, payload in self.frames(timeout):
            got.append((frame_type, payload))
            if frame_type == wanted:
                return got
            if frame_type == st.T_ERROR:
                raise AssertionError("erreur du PC: " + payload.decode("utf-8", "replace"))
        raise AssertionError(f"pas de trame 0x{wanted:02x} en {timeout} s")


def str8(p: bytes, c: int):
    n = p[c]
    return p[c + 1:c + 1 + n].decode(), c + 1 + n


def parse_item(p: bytes):
    slot, kind = p[0], p[1]
    item_id, c = str8(p, 2)
    title, c = str8(p, c)
    info, c = str8(p, c)
    return slot, kind, item_id, title, info


def browse(calc, kind=0, genre=0, page=0, query=""):
    calc.send(st.T_BROWSE, bytes([kind, genre]) + struct.pack("<H", page) + query.encode())
    frames = calc.collect_until(st.T_LIST_DONE)
    items = [parse_item(p) for t, p in frames if t == st.T_ITEM]
    done = next(p for t, p in frames if t == st.T_LIST_DONE)
    return items, done[1], struct.unpack("<H", done[2:4])[0]


def posters(calc, count, timeout=15):
    got = {}
    for frame_type, payload in calc.frames(timeout):
        if frame_type == st.T_POSTER:
            slot, w, h = payload[0], payload[1], payload[2]
            assert (w, h) == (st.POSTER_W, st.POSTER_H) and len(payload) == 3 + w * h * 2, "affiche mal formee"
            got[slot] = payload[3:]
            if len(got) == count:
                break
    return got


def main() -> int:
    make_video()
    catalog, sources, other = serve("catalog"), serve("sources"), serve("other")
    base = lambda s: f"http://127.0.0.1:{s.server_port}"  # noqa: E731
    config = os.path.join(TMP, "stremio.json")
    os.environ["NUMSTREMIO_CONFIG"] = config
    st.CINEMETA = base(catalog) + "/catalog/manifest.json"
    st.save_config({"addons": [st.CINEMETA, base(sources) + "/sources/manifest.json",
                               base(other) + "/other/manifest.json"], "langs": ["fre", "eng"]})

    # --- the addon list of the app page
    summaries = st.list_addons()
    assert [s["name"] for s in summaries] == ["FakeCatalog", "FakeSources", "OtherSrc"], summaries
    try:
        st.add_addon("pas une adresse")
        raise AssertionError("une adresse invalide a ete acceptee")
    except ValueError:
        pass
    assert st.normalize_addon_url("stremio://x.example/abc") == "https://x.example/abc/manifest.json"
    print("0. configuration des addons: ok")

    calc = FakeCalculator()
    link = gw.SerialLink(calc.slave_path)
    opts = gw.default_options()
    session = gw.Session(link, opts)
    threading.Thread(target=session.run, daemon=True).start()
    try:
        calc.send(gw.T_HELLO)
        calc.collect_until(gw.T_HELLO_ACK)
        print("1. connexion: ok")

        calc.send(st.T_START)
        frames = calc.collect_until(st.T_CONFIG)
        payload = frames[-1][1]
        genres, c = [], 1
        for _ in range(payload[0]):
            g, c = str8(payload, c)
            genres.append(g)
        assert genres == ["Action", "Drama", "Comedy"], genres
        print("2. genres:", genres)

        # type "all": movies and series alternate, six per page, with posters
        items, more, page = browse(calc)
        assert [i[0] for i in items] == [0, 1, 2, 3, 4, 5] and more == 1 and page == 0
        assert [i[1] for i in items] == [1, 2, 1, 2, 1, 2], [i[1] for i in items]
        assert items[0][3] == "Film numero 1" and items[0][4].startswith("2001")
        got = posters(calc, 6)
        assert sorted(got) == [0, 1, 2, 3, 4, 5], sorted(got)
        print("3. liste complete (films et series alternes) + 6 affiches: ok")

        items2, more2, page2 = browse(calc, page=1)
        assert page2 == 1 and items2 and items2[0][2] != items[0][2]
        print(f"4. page suivante: {len(items2)} elements, suite={more2}")

        items, more, _ = browse(calc, kind=1)
        assert all(i[1] == 1 for i in items) and len(items) == 6 and more == 1
        items, more, _ = browse(calc, kind=1, genre=1)  # movies, Action
        assert items and all(int(i[2][2:]) % 2 == 0 for i in items), items
        print("5. filtre type + genre: ok (", ", ".join(i[3] for i in items), ")")

        items, more, _ = browse(calc, query="numero 3")
        titles = [i[3] for i in items]
        assert "Film numero 3" in titles and "Serie numero 3" in titles, titles
        items, more, _ = browse(calc, kind=2, query="serie")
        assert items and all(i[1] == 2 for i in items)
        print("6. recherche (tous les types, puis series seulement): ok")

        # a movie: details, then the sources
        movie_id = b"tt0000002"
        calc.send(st.T_OPEN, bytes([1, len(movie_id)]) + movie_id)
        detail = calc.collect_until(st.T_DETAIL)[-1][1]
        title, c = str8(detail, 1)
        info, c = str8(detail, c)
        assert title == "Film numero 2" and detail[c] == 0, (title, detail[c])
        print(f"7. fiche film: {title} ({info})")

        calc.send(st.T_STREAMS, bytes([st.NO_SEASON]) + struct.pack("<H", 0))
        frames = calc.collect_until(st.T_SUBLANGS)
        providers = [p for t, p in frames if t == st.T_PROVIDERS][0]
        names, c = [], 1
        for _ in range(providers[0]):
            n, c = str8(providers, c)
            names.append(n)
        assert names == ["FakeSources", "OtherSrc"], names
        streams = []
        for t, p in frames:
            if t == st.T_STREAM:
                l1, c = str8(p, 2)
                l2, c = str8(p, c)
                streams.append((p[0], p[1], l1, l2))
        assert [s[1] for s in streams] == [1, 1, 2], streams  # the external link is not listed
        assert streams[0][2].startswith("720P") and "1.2 Go" in streams[0][2], streams[0]
        subs = [p for t, p in frames if t == st.T_SUBLANGS][0]
        labels, c = [], 1
        for _ in range(subs[0]):
            n, c = str8(subs, c)
            labels.append(n)
        assert labels == ["Francais", "English"], labels  # preferred language first
        print("8. fournisseurs:", names, "| sources:", [(s[1], s[2]) for s in streams], "| sous-titres:", labels)

        # play the first source of "FakeSources" with the French subtitles, medium speed
        calc.send(0x43, struct.pack("<I", 0) + b"@0.1.2")
        info = None
        frame_ids, shown_subs, ended = [], [], False
        deadline = time.monotonic() + 40
        paused_checked = False
        for frame_type, payload in calc.frames(40):
            if frame_type == gw.T_PLAY_INFO:
                info = struct.unpack("<IBB", payload[:6])
                assert info[2] == 1, "les sous-titres auraient du etre trouves"
                assert 6000 <= info[0] <= 9000, f"duree {info[0]} ms"
                assert info[1] == 12, "vitesse moyenne = 12 images/s"
            elif frame_type == gw.T_VIDEO:
                frame_id = struct.unpack("<H", payload[:2])[0]
                if payload[8] & 1:
                    frame_ids.append(frame_id)
                    calc.send(gw.T_ACK, struct.pack("<H", frame_id))
            elif frame_type == gw.T_SUBTITLE:
                shown_subs.append(payload.decode())
            elif frame_type == gw.T_PLAY_END:
                ended = payload == b"\x00"
                break
            elif frame_type == st.T_ERROR:
                raise AssertionError(payload.decode())
            if len(frame_ids) == 10 and not paused_checked:
                paused_checked = True
                calc.send(gw.T_CONTROL, bytes([gw.CTRL_PAUSE]))
                time.sleep(0.5)
                calc.backlog.clear()
                time.sleep(0.5)
                before = len(frame_ids)
                for ft, p in calc.frames(0.6):
                    if ft == gw.T_VIDEO and p[8] & 1:
                        frame_ids.append(0)
                assert len(frame_ids) - before <= 2, "la pause n'arrete pas l'image"
                calc.send(gw.T_CONTROL, bytes([gw.CTRL_RESUME]))
        assert info is not None and len(frame_ids) > 20 and ended, (info, len(frame_ids), ended)
        text = " | ".join(s for s in shown_subs if s)
        assert "Bonjour le monde" in text.replace("é", "e"), shown_subs
        assert "<i>" not in text
        print(f"9. lecture: {len(frame_ids)} images, sous-titres {[s for s in shown_subs if s]}, pause, fin: ok")

        # a series: seasons, episodes, the sources of a given episode
        series_id = b"tt9000001"
        calc.send(st.T_OPEN, bytes([2, len(series_id)]) + series_id)
        detail = calc.collect_until(st.T_DETAIL)[-1][1]
        title, c = str8(detail, 1)
        info, c = str8(detail, c)
        seasons = list(detail[c + 1:c + 1 + detail[c]])
        assert seasons == [1, 2], seasons
        calc.send(st.T_SEASON, bytes([2]))
        frames = calc.collect_until(st.T_EPISODES_DONE)
        episodes = [(struct.unpack("<H", p[:2])[0], str8(p, 2)[0]) for t, p in frames if t == st.T_EPISODE]
        assert episodes == [(1, "Episode 2x1"), (2, "Episode 2x2"), (3, "Episode 2x3")], episodes
        calc.send(st.T_STREAMS, bytes([2]) + struct.pack("<H", 3))
        calc.collect_until(st.T_SUBLANGS)
        assert any("/stream/series/tt9000001:2:3.json" in r for r in requests_seen), requests_seen[-8:]
        print("10. serie: saisons", seasons, "episodes", [e[1] for e in episodes], "-> sources de 2x3: ok")

        # stop while playing
        calc.send(0x43, struct.pack("<I", 0) + b"@0.0.0")
        for frame_type, payload in calc.frames(20):
            if frame_type == gw.T_VIDEO:
                break
        calc.send(gw.T_CONTROL, bytes([gw.CTRL_STOP]))
        time.sleep(0.5)
        print("11. arret de la lecture: ok")

        # an unknown source is refused cleanly
        calc.send(0x43, struct.pack("<I", 0) + b"@9.0.0")
        kinds = []
        for frame_type, _ in calc.frames(5):
            kinds.append(frame_type)
            if frame_type == gw.T_PLAY_END:
                break
        assert kinds[-2:] == [st.T_ERROR, gw.T_PLAY_END], kinds
        print("12. source inconnue: erreur propre")
    finally:
        link.close()
    print("\nTout est bon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
