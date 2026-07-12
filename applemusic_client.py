"""Fetch tracks from Apple Music public playlist / album / song URLs.

No developer account and no user login: this uses the anonymous web-player JWT
that music.apple.com's own JS bundle ships to every visitor. The token is
extracted from the bundle, cached to `.applemusic_token` (~35-day validity),
and auto-refreshed when the API rejects it. Returns the same `Track` shape as
`spotify_client`; tracks carry no `source_url`, so the pipeline searches
YouTube / SoundCloud for the audio, exactly like Spotify entries.
"""

import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from spotify_client import Track

# Anchor the token cache to this module's folder (the project root), NOT the
# current working directory — same reasoning as spotify_client._DEFAULT_CACHE:
# the server, the /api/preview worker, and the download subprocess must all read
# and write the same file no matter where they were launched from.
_TOKEN_CACHE = Path(__file__).resolve().parent / ".applemusic_token"

_API_BASE = "https://amp-api.music.apple.com"
_WEB_BASE = "https://music.apple.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_TIMEOUT = 30          # seconds, every requests call
_PAGE_LIMIT = 50       # verified working page size for the /tracks subresource
_TOKEN_MARGIN = 3600   # refresh a cached token this many seconds before it expires

# Substring match on the whole URL, mirroring the _YT_RE / _SC_RE style in
# ytdlp_loader.py. Deliberately also matches geo.music.apple.com,
# beta.music.apple.com, classical.music.apple.com so those never fall through to
# the Spotify branch in the dispatchers.
_AM_RE = re.compile(r"music\.apple\.com", re.I)

# JWT: three base64url segments separated by dots.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class AppleMusicError(RuntimeError):
    """User-facing Apple Music failure. `str(e)` is safe to show in UI / logs."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def is_applemusic_url(url: str) -> bool:
    return bool(_AM_RE.search(url or ""))


# ── URL parsing ────────────────────────────────────────────────────────
def _parse_url(url: str) -> tuple[str, str, str]:
    """Return (kind, storefront, id) for an Apple Music URL.

    kind is "playlist" | "album" | "song". Raises AppleMusicError with a
    user-facing message for library links and unsupported page types.
    """
    parsed = urlparse(url)
    path = parsed.path or ""

    # Personal-library links (…/library/playlist/p.xxxx) can't be read without a
    # signed-in user token. Catch them first and tell the user how to get a
    # public link. Library ids are p.xxxx; catalog ids are pl.xxxx.
    if "/library/" in path:
        raise AppleMusicError(
            "This is a personal-library link, which can't be read without "
            "signing in. In Apple Music, use Share → Copy Link on the "
            "playlist to get a public music.apple.com link."
        )

    segments = [s for s in path.split("/") if s]
    storefront = segments[0].lower() if segments and re.fullmatch(r"[a-z]{2}", segments[0]) else "us"

    m = re.search(r"/playlist/(?:[^/]+/)?(pl\.[\w-]+)", path)
    if m:
        return "playlist", storefront, m.group(1)

    m = re.search(r"/song/(?:[^/]+/)?(\d+)", path)
    if m:
        return "song", storefront, m.group(1)

    if "/album/" in path:
        # An album URL with ?i=<trackId> points at a single song.
        i = (parse_qs(parsed.query).get("i") or [None])[0]
        if i and i.isdigit():
            return "song", storefront, i
        m = re.search(r"/album/(?:[^/]+/)?(\d+)", path)
        if m:
            return "album", storefront, m.group(1)

    raise AppleMusicError(
        "Only Apple Music playlist, album, or song links are supported."
    )


def classify_url(url: str) -> str:
    """Return "playlist" | "album" | "song"; raises AppleMusicError otherwise."""
    return _parse_url(url)[0]


# ── Token acquisition + caching ────────────────────────────────────────
def _extract_web_token() -> str:
    try:
        html = requests.get(
            f"{_WEB_BASE}/us/browse", headers={"User-Agent": _UA}, timeout=_TIMEOUT
        ).text
    except requests.RequestException as e:
        raise AppleMusicError(f"network error reaching Apple Music: {e}") from e

    # There are ~3 bundles; the non-legacy index~*.js carries the token.
    asset_paths = re.findall(r"/assets/index[^\"']*?\.js", html)
    for asset in sorted(set(asset_paths), key=lambda p: "legacy" in p):
        try:
            js = requests.get(
                _WEB_BASE + asset, headers={"User-Agent": _UA}, timeout=_TIMEOUT
            ).text
        except requests.RequestException:
            continue
        m = _JWT_RE.search(js)
        if m:
            return m.group(0)

    raise AppleMusicError(
        "could not extract the Apple Music web token — Apple may have "
        "changed their site; try updating TuneHoard."
    )


def _jwt_exp(token: str) -> int:
    """Return the JWT `exp` claim, or a conservative now+7d fallback."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data["exp"])
    except Exception:
        return int(time.time()) + 7 * 86400


def _get_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        try:
            cached = json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
            if int(cached["exp"]) - time.time() > _TOKEN_MARGIN:
                return cached["token"]
        except Exception:
            pass  # missing / corrupt cache → fall through and re-extract

    token = _extract_web_token()
    try:
        tmp = _TOKEN_CACHE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"token": token, "exp": _jwt_exp(token)}), encoding="utf-8"
        )
        tmp.replace(_TOKEN_CACHE)
    except Exception:
        pass  # caching is best-effort; a write failure shouldn't break the fetch
    return token


# ── API helper ─────────────────────────────────────────────────────────
def _api_get(path: str, params: dict | None = None, *, what: str = "resource") -> dict:
    # The `next` cursor comes back as a bare "/v1/catalog/..." path; absolute
    # URLs (none today) would also pass through unchanged.
    url = path if path.startswith("http") else _API_BASE + path

    def _do(token: str):
        return requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": _WEB_BASE,
                "User-Agent": _UA,
            },
            params=params,
            timeout=_TIMEOUT,
        )

    try:
        resp = _do(_get_token())
        if resp.status_code == 401:
            resp = _do(_get_token(force_refresh=True))
        if resp.status_code == 429:
            try:
                delay = min(int(resp.headers.get("Retry-After", 5)), 15)
            except ValueError:
                delay = 5
            time.sleep(delay)
            resp = _do(_get_token())
    except requests.RequestException as e:
        raise AppleMusicError(f"network error talking to Apple Music: {e}") from e

    if resp.status_code == 401:
        raise AppleMusicError(
            "Apple Music rejected the anonymous token twice — try again later.",
            401,
        )
    if resp.status_code == 404:
        raise AppleMusicError(
            f"Apple Music {what} not found — it may have been deleted, may "
            "not exist in this storefront, or is a private playlist (share it as "
            "a public link).",
            404,
        )
    if resp.status_code != 200:
        raise AppleMusicError(
            f"Apple Music API error HTTP {resp.status_code}.", resp.status_code
        )
    try:
        return resp.json()
    except ValueError as e:
        raise AppleMusicError("Apple Music returned an unreadable response.") from e


# ── Track mapping ──────────────────────────────────────────────────────
def _song_to_track(item: dict) -> Track | None:
    # Unavailable / greyed catalog entries come back without attributes or as
    # unexpected types — omit them, mirroring the null-track skip in
    # spotify_client. No playParams check: a track unplayable on Apple Music is
    # still findable on YouTube, which is where the audio actually comes from.
    if item.get("type") not in ("songs", "music-videos"):
        return None
    attrs = item.get("attributes")
    if not attrs:
        return None
    artist = attrs.get("artistName")
    return Track(
        spotify_id=f"am:{item['id']}",
        title=attrs.get("name", ""),
        artists=[artist] if artist else [],
        album=attrs.get("albumName", ""),
        duration_ms=int(attrs.get("durationInMillis") or 0),
        isrc=attrs.get("isrc"),
    )  # source_url stays None → search-download path


# ── Loaders ────────────────────────────────────────────────────────────
def get_playlist_tracks(url: str) -> tuple[str, list[Track]]:
    _, sf, pid = _parse_url(url)
    meta = _api_get(f"/v1/catalog/{sf}/playlists/{pid}", {"fields": "name"}, what="playlist")
    name = meta["data"][0]["attributes"]["name"]

    tracks: list[Track] = []
    page = _api_get(
        f"/v1/catalog/{sf}/playlists/{pid}/tracks", {"limit": _PAGE_LIMIT}, what="playlist"
    )
    while True:
        tracks += [t for t in map(_song_to_track, page.get("data") or []) if t]
        nxt = page.get("next")
        if not nxt:
            break
        page = _api_get(nxt, what="playlist")
    return name, tracks


def get_album_tracks(url: str) -> tuple[str, list[Track]]:
    _, sf, aid = _parse_url(url)
    data = _api_get(f"/v1/catalog/{sf}/albums/{aid}", {"include": "tracks"}, what="album")
    album = data["data"][0]
    name = album["attributes"]["name"]

    rel = (album.get("relationships") or {}).get("tracks") or {}
    tracks = [t for t in map(_song_to_track, rel.get("data") or []) if t]
    nxt = rel.get("next")
    while nxt:
        page = _api_get(nxt, what="album")
        tracks += [t for t in map(_song_to_track, page.get("data") or []) if t]
        nxt = page.get("next")
    return name, tracks


def get_track(url: str) -> tuple[str, list[Track]]:
    _, sf, sid = _parse_url(url)
    data = _api_get(f"/v1/catalog/{sf}/songs/{sid}", what="song")
    t = _song_to_track(data["data"][0])
    return "singles", ([t] if t else [])
