"""NumStremio: Stremio's addon catalogues and streams, on the calculator.

Stremio is open: an *addon* is a web server that answers a few JSON requests
(https://github.com/Stremio/stremio-addon-sdk). This service speaks that
protocol, so any addon works, and the calculator (apps/numstremio in the
patch) is a thin client:

  browse   /catalog/{type}/{id}/[genre=..&][search=..&]skip=N.json
  details  /meta/{type}/{id}.json
  sources  /stream/{type}/{id}.json   (one list per addon: the "providers")
  subtitles /subtitles/{type}/{id}.json

The video itself is played by NumTube's player (decoded by ffmpeg, drawn as
tiles, subtitles in the bottom strip): the calculator sends the usual Play
frame with the id "@<stream>.<subtitle language>.<speed>".

No addon but Stremio's own catalogue (Cinemeta) is built in. The addons that
provide streams are the ones YOU add (app page, or the config file): what they
serve, and whether you may watch it, is between you and them.

Frames (keep in sync with apps/numstremio/protocol.h):
  0xB0 Start        calc -> PC
  0xB1 Config       PC -> calc   u8 n, n * str8 genre
  0xB2 Browse       calc -> PC   u8 kind (0 all, 1 movie, 2 series), u8 genre, u16 page, query
  0xB3 Item         PC -> calc   u8 slot, u8 kind, str8 id, str8 title, str8 info
  0xB4 ListDone     PC -> calc   u8 count, u8 more, u16 page
  0xB5 Open         calc -> PC   u8 kind, str8 id
  0xB6 Detail       PC -> calc   u8 kind, str8 title, str8 info, u8 n, n * u8 season
  0xB7 Episode      PC -> calc   u16 number, str8 title
  0xB8 Season       calc -> PC   u8 season
  0xB9 EpisodesDone PC -> calc   u8 season, u8 count
  0xBA Streams      calc -> PC   u8 season (255: none), u16 episode
  0xBB Providers    PC -> calc   u8 n, n * str8 name
  0xBC Stream       PC -> calc   u8 index, u8 provider (1-based), str8 line 1, str8 line 2
  0xBD StreamsDone  PC -> calc   u8 count
  0xBE SubLangs     PC -> calc   u8 n, n * str8 label
  0xBF Poster       PC -> calc   u8 slot, u8 w, u8 h, RGB565 little endian
Errors use 0x07 (text).
"""
from __future__ import annotations

import collections
import concurrent.futures
import copy
import io
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import numpy as np

T_START, T_CONFIG, T_BROWSE, T_ITEM, T_LIST_DONE = 0xB0, 0xB1, 0xB2, 0xB3, 0xB4
T_OPEN, T_DETAIL, T_EPISODE, T_SEASON, T_EPISODES_DONE = 0xB5, 0xB6, 0xB7, 0xB8, 0xB9
T_STREAMS, T_PROVIDERS, T_STREAM, T_STREAMS_DONE, T_SUBLANGS, T_POSTER = 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF
T_FIRST, T_LAST = 0xB0, 0xCF
T_ERROR = 0x07

PAGE_SIZE = 6            # items per screen of the calculator
POSTER_W, POSTER_H = 28, 42
MAX_GENRES = 24
MAX_STREAMS = 30
MAX_PER_PROVIDER = 15
MAX_SEASONS = 40
MAX_EPISODES = 60
NO_SEASON = 255
SPEEDS = {0: 15, 1: 8, 2: 12, 3: 15}  # Auto, Low, Medium, High: pictures per second

CINEMETA = "https://v3-cinemeta.strem.io/manifest.json"
FALLBACK_GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama",
                   "Family", "Fantasy", "History", "Horror", "Mystery", "Romance", "Sci-Fi",
                   "Thriller", "War", "Western"]
USER_AGENT = "NumStremio/1.0 (+https://github.com/derim00/numscreen)"

LANG_NAMES = {
    "fre": "Francais", "fra": "Francais", "fr": "Francais", "eng": "English", "en": "English",
    "spa": "Espanol", "es": "Espanol", "ger": "Deutsch", "deu": "Deutsch", "de": "Deutsch",
    "ita": "Italiano", "it": "Italiano", "por": "Portugues", "pt": "Portugues", "pob": "Portugues BR",
    "dut": "Nederlands", "nld": "Nederlands", "nl": "Nederlands", "ara": "Arabe", "ar": "Arabe",
    "rus": "Russe", "ru": "Russe", "tur": "Turc", "tr": "Turc", "pol": "Polski", "pl": "Polski",
    "jpn": "Japonais", "ja": "Japonais", "kor": "Coreen", "ko": "Coreen", "chi": "Chinois", "zho": "Chinois",
    "zh": "Chinois", "hin": "Hindi", "hi": "Hindi", "swe": "Svenska", "sv": "Svenska",
}
LANG_ALIASES = {"fr": "fre", "fra": "fre", "en": "eng", "es": "spa", "de": "ger", "deu": "ger", "it": "ita",
                "pt": "por", "nl": "dut", "nld": "dut", "ar": "ara", "ru": "rus", "tr": "tur", "pl": "pol",
                "ja": "jpn", "ko": "kor", "zh": "chi", "zho": "chi", "hi": "hin", "sv": "swe"}


# ---------------------------------------------------------------------------
# Configuration: the addons the user chose
# ---------------------------------------------------------------------------
def config_path() -> str:
    override = os.environ.get("NUMSTREMIO_CONFIG")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "NumSocial", "stremio.json")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/NumSocial/stremio.json")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "numsocial", "stremio.json")


def normalize_addon_url(url: str) -> str:
    url = url.strip()
    if url.startswith("stremio://"):
        url = "https://" + url[len("stremio://"):]
    if not url.startswith(("http://", "https://")):
        raise ValueError("L'adresse doit commencer par https:// (ou stremio://)")
    if not url.endswith("/manifest.json"):
        url = url.rstrip("/") + "/manifest.json"
    return url


def load_config() -> dict:
    config = {"addons": [CINEMETA], "langs": ["fre", "eng"], "server": "http://127.0.0.1:11470"}
    try:
        with open(config_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
        for key in config:
            if key in stored:
                config[key] = stored[key]
    except (OSError, ValueError):
        pass
    if CINEMETA not in config["addons"]:
        config["addons"].insert(0, CINEMETA)  # the catalogue is always there
    return config


def save_config(config: dict) -> None:
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def http_json(url: str, timeout: float = 15, data: dict | None = None):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def addon_summary(url: str) -> dict:
    """Name and what an addon offers, for the app page."""
    manifest = http_json(url, timeout=10)
    resources = [r if isinstance(r, str) else r.get("name", "") for r in manifest.get("resources", [])]
    return {"url": url, "name": manifest.get("name") or url, "resources": sorted(set(resources)),
            "description": manifest.get("description", "")}


def list_addons() -> list[dict]:
    result = []
    for url in load_config()["addons"]:
        try:
            result.append(addon_summary(url))
        except Exception as error:
            result.append({"url": url, "name": url, "resources": [], "error": str(error)})
    return result


def add_addon(url: str) -> dict:
    url = normalize_addon_url(url)
    summary = addon_summary(url)  # refuses what is not an addon
    config = load_config()
    if url not in config["addons"]:
        config["addons"].append(url)
        save_config(config)
    return summary


def remove_addon(url: str) -> None:
    config = load_config()
    if url != CINEMETA and url in config["addons"]:
        config["addons"].remove(url)
        save_config(config)


# ---------------------------------------------------------------------------
# Addons
# ---------------------------------------------------------------------------
class Addon:
    def __init__(self, manifest_url: str) -> None:
        self.manifest_url = manifest_url
        self.base = manifest_url[: -len("/manifest.json")]
        self.manifest: dict | None = None
        self.failed_at = 0.0

    def load(self) -> bool:
        if self.manifest is not None:
            return True
        if time.monotonic() - self.failed_at < 30:
            return False
        try:
            self.manifest = http_json(self.manifest_url, timeout=12)
            return True
        except Exception:
            self.failed_at = time.monotonic()
            return False

    @property
    def name(self) -> str:
        return (self.manifest or {}).get("name") or self.base

    def provides(self, resource: str, type_: str, item_id: str) -> bool:
        manifest = self.manifest or {}
        for entry in manifest.get("resources", []):
            name = entry if isinstance(entry, str) else entry.get("name")
            if name != resource:
                continue
            types = manifest.get("types", []) if isinstance(entry, str) else entry.get("types", manifest.get("types", []))
            prefixes = manifest.get("idPrefixes") if isinstance(entry, str) else entry.get("idPrefixes", manifest.get("idPrefixes"))
            if types and type_ not in types:
                continue
            if prefixes and not any(item_id.startswith(p) for p in prefixes):
                continue
            return True
        return False

    def get(self, resource: str, type_: str, item_id: str, extra: str = "", timeout: float = 15):
        path = f"{self.base}/{resource}/{urllib.parse.quote(type_)}/{urllib.parse.quote(item_id, safe=':')}"
        if extra:
            path += "/" + extra
        return http_json(path + ".json", timeout=timeout)

    def catalog_for(self, type_: str, search: bool, genre: bool) -> dict | None:
        """The first catalogue of this type usable without extra input."""
        for catalog in (self.manifest or {}).get("catalogs", []):
            if catalog.get("type") != type_:
                continue
            extras = {e["name"]: e for e in catalog.get("extra", []) if isinstance(e, dict) and "name" in e}
            for old in catalog.get("extraSupported", []):
                extras.setdefault(old, {"name": old})
            if search and "search" not in extras:
                continue
            if genre and "genre" not in extras:
                continue
            required = {n for n, e in extras.items() if e.get("isRequired")}
            if required - {"search" if search else "", "genre" if genre else ""}:
                continue
            if not search and "search" in required:
                continue
            return catalog
        return None


def extra_path(search: str, genre: str, skip: int) -> str:
    parts = []
    if search:
        parts.append("search=" + urllib.parse.quote(search, safe=""))
    if genre:
        parts.append("genre=" + urllib.parse.quote(genre, safe=""))
    if skip:
        parts.append(f"skip={skip}")
    return "&".join(parts)


class Source:
    """One catalogue of one addon, read page by page."""

    def __init__(self, addon: Addon, catalog: dict, search: str, genre: str) -> None:
        self.addon, self.catalog, self.search, self.genre = addon, catalog, search, genre
        self.skip = 0
        self.done = False
        self.queue: collections.deque = collections.deque()

    def fill(self) -> None:
        if self.done or self.queue:
            return
        try:
            data = self.addon.get("catalog", self.catalog["type"], self.catalog["id"],
                                  extra_path(self.search, self.genre, self.skip))
            metas = [m for m in data.get("metas", []) if m.get("id") and m.get("name")]
        except Exception:
            metas = []
        if not metas:
            self.done = True
            return
        self.skip += len(metas)
        self.queue.extend(metas)


class Listing:
    """What the calculator is browsing: items from every source, merged."""

    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources
        self.items: list[dict] = []
        self.seen: set[str] = set()
        self.lock = threading.Lock()

    @property
    def exhausted(self) -> bool:
        return all(s.done and not s.queue for s in self.sources)

    def ensure(self, count: int, pool: concurrent.futures.Executor) -> None:
        with self.lock:
            while len(self.items) < count:
                pending = [s for s in self.sources if not s.done and not s.queue]
                if pending:
                    list(pool.map(Source.fill, pending))
                added = False
                for source in self.sources:  # round robin: movies and series alternate
                    while source.queue:
                        meta = source.queue.popleft()
                        if meta["id"] in self.seen:
                            continue
                        self.seen.add(meta["id"])
                        self.items.append(meta)
                        added = True
                        break
                if not added and self.exhausted:
                    break


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def lang_code(raw: str) -> str:
    raw = (raw or "").lower().strip()
    return LANG_ALIASES.get(raw, raw)


def lang_label(code: str) -> str:
    return LANG_NAMES.get(code) or code.upper()


def poster_rgb565(data: bytes) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    # "cover": fill the box, cut what overflows
    scale = max(POSTER_W / image.width, POSTER_H / image.height)
    resized = image.resize((max(POSTER_W, round(image.width * scale)), max(POSTER_H, round(image.height * scale))))
    left, top = (resized.width - POSTER_W) // 2, (resized.height - POSTER_H) // 2
    rgb = np.asarray(resized.crop((left, top, left + POSTER_W, top + POSTER_H)), dtype=np.uint16)
    value = ((rgb[..., 0] >> 3) << 11) | ((rgb[..., 1] >> 2) << 5) | (rgb[..., 2] >> 3)
    return value.astype("<u2").tobytes()


TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def parse_subtitles(text: str, clean) -> list:
    """SRT or WebVTT to (start_ms, end_ms, text) tuples."""
    def ms(h, m, s, f) -> int:
        return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(f.ljust(3, "0")[:3])

    cues = []
    for block in re.split(r"\r?\n\r?\n+", text.replace("﻿", "")):
        lines = block.strip().splitlines()
        for number, line in enumerate(lines):
            found = TIME_RE.search(line)
            if found:
                start = ms(*found.group(1, 2, 3, 4))
                end = ms(*found.group(5, 6, 7, 8))
                body = " ".join(lines[number + 1:])
                body = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", body)
                body = clean(body)
                if body:
                    cues.append((start, end, body))
                break
    cues.sort()
    return cues


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


QUALITY_RE = re.compile(r"(2160p|4k|1440p|1080p|720p|576p|480p|360p)", re.I)
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(GB|MB|GiB|MiB)", re.I)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------
class StremioService:
    def __init__(self, gateway) -> None:
        """`gateway` is the numtube_gateway module (text cleaning, player, logs)."""
        self.gw = gateway
        self.log = gateway.log
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=12)
        self.link = None
        self.lock = threading.Lock()
        self.generation = 0
        self.config = load_config()
        self.addons: list[Addon] = []
        self.genres: list[str] = []
        self.listing: Listing | None = None
        self.listing_key = None
        self.posters: collections.OrderedDict = collections.OrderedDict()
        self.current: dict = {}
        self.streams: list[dict] = []
        self.sub_langs: list[tuple[str, str, list[str]]] = []
        self.player_class = _make_player_class(gateway)

    # -- plumbing
    def attach(self, link) -> None:
        self.link = link

    def detach(self, link) -> None:
        if self.link is link:
            self.link = None

    def _send(self, link, frame_type: int, payload: bytes = b"") -> None:
        try:
            link.send(frame_type, payload)
        except OSError:
            pass

    def _text(self, text: str, limit: int) -> bytes:
        data = self.gw.truncate_utf8(self.gw.clean_text(text or ""), limit)
        return bytes([len(data)]) + data

    def _error(self, link, message: str) -> None:
        self.log(f"stremio: {message}")
        self._send(link, T_ERROR, self.gw.truncate_utf8(self.gw.clean_text(message), 120))

    def _refresh_addons(self) -> list[Addon]:
        self.config = load_config()
        known = {a.manifest_url: a for a in self.addons}
        self.addons = [known.get(u) or Addon(u) for u in self.config["addons"]]
        list(self.pool.map(Addon.load, self.addons))
        return [a for a in self.addons if a.manifest is not None]

    def _spawn(self, function, *args) -> None:
        def run() -> None:
            try:
                function(*args)
            except OSError:
                pass
            except Exception as error:
                self.log(f"stremio: {function.__name__}: {error}")
                if self.link is not None:
                    self._error(self.link, f"Erreur : {error}")

        threading.Thread(target=run, daemon=True).start()

    # -- frames from the calculator
    def handle(self, link, frame_type: int, payload: bytes) -> None:
        self.link = link
        if frame_type == T_START:
            self._spawn(self.do_start, link)
        elif frame_type == T_BROWSE and len(payload) >= 4:
            kind, genre, page = payload[0], payload[1], struct.unpack("<H", payload[2:4])[0]
            query = payload[4:].decode("utf-8", "ignore").strip()
            self.generation += 1
            self._spawn(self.do_browse, link, self.generation, kind, genre, page, query)
        elif frame_type == T_OPEN and len(payload) >= 2:
            size = payload[1]
            self._spawn(self.do_open, link, payload[0], payload[2:2 + size].decode("utf-8", "ignore"))
        elif frame_type == T_SEASON and payload:
            self._spawn(self.do_season, link, payload[0])
        elif frame_type == T_STREAMS and len(payload) >= 3:
            self._spawn(self.do_streams, link, payload[0], struct.unpack("<H", payload[1:3])[0])

    # -- start: the genres
    def do_start(self, link) -> None:
        addons = self._refresh_addons()
        genres: list[str] = []
        for addon in addons:
            for catalog in addon.manifest.get("catalogs", []):
                for extra in catalog.get("extra", []):
                    if isinstance(extra, dict) and extra.get("name") == "genre":
                        genres += [g for g in extra.get("options", []) if g not in genres]
        genres = (genres or FALLBACK_GENRES)[:MAX_GENRES]
        self.genres = genres
        payload = bytes([len(genres)]) + b"".join(self._text(g, 14) for g in genres)
        self._send(link, T_CONFIG, payload)
        self.log(f"stremio: {len(addons)} addon(s), {len(genres)} genres")

    # -- browse and search
    def _listing(self, kind: int, genre: int, query: str) -> Listing:
        genre_name = self.genres[genre - 1] if 0 < genre <= len(self.genres) else ""
        key = (kind, genre_name, query)
        if self.listing is not None and self.listing_key == key:
            return self.listing
        types = {0: ["movie", "series"], 1: ["movie"], 2: ["series"]}.get(kind, ["movie", "series"])
        sources = []
        for type_ in types:
            for addon in self._refresh_addons():
                catalog = addon.catalog_for(type_, bool(query), bool(genre_name))
                if catalog is not None:
                    sources.append(Source(addon, catalog, query, genre_name))
        self.listing, self.listing_key = Listing(sources), key
        return self.listing

    def do_browse(self, link, generation: int, kind: int, genre: int, page: int, query: str) -> None:
        self.log(f"stremio: liste type={kind} genre={genre} page={page} recherche={query!r}")
        listing = self._listing(kind, genre, query)
        listing.ensure((page + 1) * PAGE_SIZE + 1, self.pool)
        if generation != self.generation:
            return  # the user asked something else meanwhile
        items = listing.items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        more = len(listing.items) > (page + 1) * PAGE_SIZE
        for slot, meta in enumerate(items):
            type_ = 2 if meta.get("type") == "series" else 1
            year = str(meta.get("releaseInfo") or meta.get("year") or "")[:9]
            rating = meta.get("imdbRating")
            info = f"{year} *{rating}" if rating else year
            payload = bytes([slot, type_]) + self._text(meta["id"], 40) + self._text(meta["name"], 60) + self._text(info, 24)
            self._send(link, T_ITEM, payload)
        self._send(link, T_LIST_DONE, bytes([len(items), 1 if more else 0]) + struct.pack("<H", page))
        for slot, meta in enumerate(items):
            if meta.get("poster"):
                self.pool.submit(self.send_poster, link, generation, slot, meta["poster"])

    def send_poster(self, link, generation: int, slot: int, url: str) -> None:
        try:
            pixels = self.posters.get(url)
            if pixels is None:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=12) as response:
                    pixels = poster_rgb565(response.read(2_000_000))
                self.posters[url] = pixels
                while len(self.posters) > 120:
                    self.posters.popitem(last=False)
            if generation == self.generation:
                self._send(link, T_POSTER, bytes([slot, POSTER_W, POSTER_H]) + pixels)
        except Exception as error:
            self.log(f"stremio: affiche impossible ({error})")

    # -- details
    def _meta(self, type_: str, item_id: str) -> dict:
        for addon in self._refresh_addons():
            if addon.provides("meta", type_, item_id):
                try:
                    meta = addon.get("meta", type_, item_id).get("meta")
                    if meta:
                        return meta
                except Exception:
                    continue
        return {"id": item_id, "name": item_id, "type": type_}

    def do_open(self, link, kind: int, item_id: str) -> None:
        type_ = "series" if kind == 2 else "movie"
        meta = self._meta(type_, item_id)
        seasons: dict[int, dict[int, dict]] = {}
        if type_ == "series":
            for video in meta.get("videos") or []:
                season = int(video.get("season") or 0)
                number = int(video.get("episode") or video.get("number") or 0)
                if number <= 0 and season == 0:
                    continue
                seasons.setdefault(season, {})[number] = video
        self.current = {"id": item_id, "type": type_, "meta": meta, "seasons": seasons}
        year = str(meta.get("releaseInfo") or meta.get("year") or "")
        genres = ", ".join((meta.get("genres") or [])[:2])
        info = " - ".join(x for x in (year, genres) if x)
        numbers = sorted(seasons)[:MAX_SEASONS]
        payload = bytes([kind]) + self._text(meta.get("name") or item_id, 60) + self._text(info, 40)
        payload += bytes([len(numbers)]) + bytes(numbers)
        self._send(link, T_DETAIL, payload)

    def do_season(self, link, season: int) -> None:
        episodes = self.current.get("seasons", {}).get(season, {})
        count = 0
        for number in sorted(episodes)[:MAX_EPISODES]:
            video = episodes[number]
            title = video.get("title") or video.get("name") or ""
            self._send(link, T_EPISODE, struct.pack("<H", number) + self._text(title, 28))
            count += 1
        self._send(link, T_EPISODES_DONE, bytes([season, count]))

    # -- sources ("providers") and subtitles
    def _video_id(self, season: int, episode: int) -> str:
        current = self.current
        if current.get("type") != "series":
            return current.get("id", "")
        video = current.get("seasons", {}).get(season, {}).get(episode)
        return (video or {}).get("id") or f"{current['id']}:{season}:{episode}"

    def do_streams(self, link, season: int, episode: int) -> None:
        current = self.current
        type_, video_id = current.get("type", "movie"), self._video_id(season if season != NO_SEASON else 0, episode)
        current["video_id"] = video_id
        current["label"] = (current.get("meta", {}).get("name") or "") + (
            f" S{season:02d}E{episode:02d}" if type_ == "series" else "")
        addons = self._refresh_addons()
        self.log(f"stremio: sources pour {video_id}")

        def ask(addon: Addon):
            try:
                data = addon.get("stream", type_, video_id, timeout=18)
                return addon, data.get("streams", [])
            except Exception as error:
                self.log(f"  {addon.name}: {error}")
                return addon, []

        stream_addons = [a for a in addons if a.provides("stream", type_, video_id)]
        sub_addons = [a for a in addons if a.provides("subtitles", type_, video_id)]
        stream_jobs = [self.pool.submit(ask, a) for a in stream_addons]
        sub_jobs = [self.pool.submit(self._ask_subtitles, a, type_, video_id) for a in sub_addons]

        streams: list[dict] = []
        providers: list[str] = []
        embedded_subs: list[dict] = []
        for job in stream_jobs:
            addon, found = job.result()
            usable = []
            for stream in found:
                playable = stream.get("url") or stream.get("infoHash") or stream.get("ytId")
                if not playable:
                    continue
                embedded_subs += stream.get("subtitles") or []
                usable.append(stream)
            if not usable:
                continue
            providers.append(addon.name)
            for stream in usable[:MAX_PER_PROVIDER]:
                stream["_provider"] = len(providers)
                stream["_addon"] = addon.name
                streams.append(stream)
        streams = streams[:MAX_STREAMS]
        self.streams = streams

        subs: dict[str, list[str]] = {}
        for job in sub_jobs:
            for entry in job.result():
                self._add_sub(subs, entry)
        for entry in embedded_subs:
            self._add_sub(subs, entry)
        preferred = [lang_code(l) for l in self.config.get("langs", [])]
        order = [c for c in preferred if c in subs] + sorted(c for c in subs if c not in preferred)
        self.sub_langs = [(c, lang_label(c), subs[c]) for c in order[:12]]

        if not streams:
            self._error(link, "Aucune source. Ajoute un addon de sources dans la page NumSocial")
            return
        self._send(link, T_PROVIDERS, bytes([len(providers)]) + b"".join(self._text(p, 14) for p in providers))
        for index, stream in enumerate(streams):
            line1, line2 = self._describe(stream)
            self._send(link, T_STREAM, bytes([index, stream["_provider"]]) + self._text(line1, 26) + self._text(line2, 52))
        self._send(link, T_STREAMS_DONE, bytes([len(streams)]))
        self._send(link, T_SUBLANGS, bytes([len(self.sub_langs)]) + b"".join(self._text(l, 14) for _, l, _ in self.sub_langs))

    def _ask_subtitles(self, addon: Addon, type_: str, video_id: str) -> list[dict]:
        try:
            return addon.get("subtitles", type_, video_id, timeout=12).get("subtitles", [])
        except Exception:
            return []

    @staticmethod
    def _add_sub(subs: dict[str, list[str]], entry: dict) -> None:
        url, code = entry.get("url"), lang_code(entry.get("lang", ""))
        if url and code and url not in subs.setdefault(code, []):
            subs[code].append(url)

    @staticmethod
    def _describe(stream: dict) -> tuple[str, str]:
        name = (stream.get("name") or "").replace("\n", " ")
        title = (stream.get("title") or stream.get("description") or "").strip()
        first = title.splitlines()[0] if title else (stream.get("behaviorHints") or {}).get("filename", "")
        quality = QUALITY_RE.search(name + " " + title)
        size = (stream.get("behaviorHints") or {}).get("videoSize")
        size_text = ""
        if size:
            size_text = f"{size / 1e9:.1f} Go" if size >= 1e9 else f"{size / 1e6:.0f} Mo"
        else:
            found = SIZE_RE.search(title)
            if found:
                size_text = f"{found.group(1)} {'Go' if found.group(2).lower().startswith('g') else 'Mo'}"
        kind = "YouTube" if stream.get("ytId") else "Torrent" if stream.get("infoHash") and not stream.get("url") else ""
        line1 = " ".join(x for x in (quality.group(1).upper() if quality else "", size_text, kind) if x) or name[:26] or "Source"
        return line1, first or name

    # -- playing
    def make_player(self, link, spec: str, opts):
        """Player for the id "@<stream>.<subtitles>.<speed>", started by the session."""
        try:
            stream_index, sub_index, speed = (int(x) for x in spec.lstrip("@").split("."))
            stream = self.streams[stream_index]
        except (ValueError, IndexError):
            raise RuntimeError("Source introuvable, recommence")
        if stream.get("ytId") and not stream.get("url"):
            return self.gw.Player(link, stream["ytId"], 0, opts)  # a trailer: NumTube plays it
        player_opts = copy.copy(opts)
        player_opts.fps = SPEEDS.get(speed, 15)
        subs = self.sub_langs[sub_index - 1] if 0 < sub_index <= len(self.sub_langs) else None
        return self.player_class(link, "", 0, player_opts, self, stream, subs)

    def stream_url(self, stream: dict) -> tuple[str, dict]:
        hints = stream.get("behaviorHints") or {}
        headers = ((hints.get("proxyHeaders") or {}).get("request")) or {}
        if stream.get("url"):
            return stream["url"], headers
        info_hash = stream["infoHash"]
        server = self.config.get("server", "http://127.0.0.1:11470").rstrip("/")
        try:
            http_json(server + "/settings", timeout=3)
        except Exception:
            raise RuntimeError("Lance Stremio Service sur le PC pour ce type de source")
        trackers = [s[len("tracker:"):] for s in stream.get("sources", []) if s.startswith("tracker:")]
        try:
            http_json(f"{server}/{info_hash}/create", timeout=20, data={
                "torrent": {"infoHash": info_hash},
                "peerSearch": {"sources": [f"dht:{info_hash}"] + [f"tracker:{t}" for t in trackers], "min": 40, "max": 150},
            })
        except Exception as error:
            self.log(f"stremio: creation du torrent: {error}")
        index = stream.get("fileIdx")
        return f"{server}/{info_hash}/{index if index is not None else -1}", {}

    def probe_duration_ms(self, url: str, headers: dict) -> int:
        command = [self.gw.FFMPEG, "-hide_banner", "-nostdin"]
        if headers:
            command += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        command += ["-i", url]
        try:
            result = subprocess.run(command, capture_output=True, timeout=40, **self.gw.POPEN_FLAGS)
        except subprocess.TimeoutExpired:
            return 0
        found = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr.decode("utf-8", "replace"))
        if not found:
            return 0
        return int((int(found.group(1)) * 3600 + int(found.group(2)) * 60 + float(found.group(3))) * 1000)

    def load_cues(self, subs) -> list:
        if subs is None:
            return []
        for url in subs[2][:4]:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=20) as response:
                    cues = parse_subtitles(decode_bytes(response.read(3_000_000)), self.gw.clean_text)
                if cues:
                    return [self.gw.Cue(a, b, t) for a, b, t in cues]
            except Exception as error:
                self.log(f"stremio: sous-titres {url[:60]}: {error}")
        return []


def _make_player_class(gw):
    class StremioPlayer(gw.Player):
        connecting_text = "Recherche de la source..."
        slow_text = "La source est lente... ({s} s)"
        can_pipe = False  # no yt-dlp fallback: the stream is a plain URL

        def __init__(self, link, video_id, start_ms, opts, service, stream, subs) -> None:
            super().__init__(link, video_id, start_ms, opts)
            self.service, self.stream, self.subs = service, stream, subs

        def resolve(self):
            service = self.service
            url, headers = service.stream_url(self.stream)
            duration = service.probe_duration_ms(url, headers)
            cues = service.load_cues(self.subs)
            label = service.current.get("label") or "Stremio"
            return gw.VideoInfo(title=gw.clean_text(label), duration_ms=duration, url=url, page_url=url,
                                format_id="", info_path="", headers=headers, cues=cues,
                                sub_lang=self.subs[1] if self.subs else "")

    return StremioPlayer
