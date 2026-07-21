"""Fetch tracks from a Spotify playlist URL via the Web API.

Uses OAuth user flow because Spotify tightened Client Credentials access to
`playlist_items` in 2025 — even public playlists now return 401 without a user
token. First run opens a browser; subsequent runs read a cached token.

Third-party playlists (owned by another account) get 403 on `playlist_items`
for Development Mode apps since Spotify's 2024/2025 API restrictions. For
those, `get_playlist_tracks` transparently falls back to the public embed page
(open.spotify.com/embed/playlist/<id>), which serves the track list
anonymously — same spirit as applemusic_client's anonymous web-token approach.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import requests
import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

# Anchor the OAuth token cache to this module's folder (the project root), NOT
# the current working directory. A bare ".spotify_cache" resolves relative to
# CWD, so the dashboard's authorize step, the /api/spotify/* status checks, and
# the download subprocess could each read/write a *different* file depending on
# where the server was launched from — making a real authorization look like
# "not authorized" and Spotify appear completely broken. An absolute path keeps
# all callers in agreement. (Matches docs/GOTCHAS.md: cached "in the project root".)
_DEFAULT_CACHE = str(Path(__file__).resolve().parent / ".spotify_cache")


@dataclass
class Track:
    spotify_id: str            # generic "primary key"; YT/SC entries get "yt:..." / "sc:..."
    title: str
    artists: list[str]
    album: str
    duration_ms: int
    isrc: str | None
    source_url: str | None = None   # direct download URL (set for YT/SC entries)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else "Unknown"

    @property
    def search_query(self) -> str:
        if self.artists:
            return f"{self.primary_artist} - {self.title}"
        return self.title


def _extract_playlist_id(url_or_id: str) -> str:
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


def _extract_track_id(url_or_id: str) -> str:
    m = re.search(r"track[/:]([A-Za-z0-9]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


def _spotify_client(
    client_id: str,
    client_secret: str,
    redirect_uri: str = "http://127.0.0.1:8888/callback",
    cache_path: str | Path = _DEFAULT_CACHE,
) -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="playlist-read-private playlist-read-collaborative user-library-read",
        cache_path=str(cache_path),
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth)


def _spotify_track_to_track(t: dict) -> Track:
    return Track(
        spotify_id=t["id"],
        title=t["name"],
        artists=[a["name"] for a in t["artists"]],
        album=t["album"]["name"],
        duration_ms=t["duration_ms"],
        isrc=(t.get("external_ids") or {}).get("isrc"),
    )


def get_track(
    track_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = "http://127.0.0.1:8888/callback",
    cache_path: str | Path = _DEFAULT_CACHE,
) -> tuple[str, list[Track]]:
    sp = _spotify_client(client_id, client_secret, redirect_uri, cache_path)
    t = sp.track(_extract_track_id(track_url))
    return "singles", [_spotify_track_to_track(t)]


def get_liked_songs(
    client_id: str,
    client_secret: str,
    redirect_uri: str = "http://127.0.0.1:8888/callback",
    cache_path: str | Path = _DEFAULT_CACHE,
) -> tuple[str, list[Track]]:
    """Fetch the authenticated user's Liked Songs (saved tracks).

    Returns ("Liked Songs", tracks) — the literal folder name is hard-coded
    here (it's not user-namable on Spotify's side) and the user-library-read
    scope is already included in `_spotify_client`.

    Note: saved-tracks entries still carry the track payload under `entry["track"]`
    (the 2025 `playlist_items` rename to `item` did not propagate here). We read
    `entry.get("track") or entry.get("item")` for forward-compat.
    """
    sp = _spotify_client(client_id, client_secret, redirect_uri, cache_path)

    tracks: list[Track] = []
    offset = 0
    page_size = 50
    while True:
        results = sp.current_user_saved_tracks(limit=page_size, offset=offset)
        items = (results or {}).get("items") or []
        if not items:
            break
        for entry in items:
            if not entry:
                continue
            t = entry.get("track") or entry.get("item")
            # Spotify returns null tracks for unavailable / removed entries.
            if not t or t.get("type") == "episode" or not t.get("id"):
                continue
            tracks.append(
                Track(
                    spotify_id=t["id"],
                    title=t["name"],
                    artists=[a["name"] for a in t["artists"]],
                    album=t["album"]["name"],
                    duration_ms=t["duration_ms"],
                    isrc=(t.get("external_ids") or {}).get("isrc"),
                )
            )
        if len(items) < page_size or not results.get("next"):
            break
        offset += page_size

    return "Liked Songs", tracks


_EMBED_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# The embed page's trackList is believed to cap around 100 entries; when we see
# exactly this many we warn that the playlist may actually be longer.
_EMBED_SUSPECT_CAP = 100


def _embed_playlist_tracks(playlist_id: str) -> tuple[str, list[Track]]:
    """Anonymous fallback: read the track list from the public embed page.

    Development Mode apps get 403 on `playlist_items` for playlists the
    authorized user doesn't own. The public embed page still ships the track
    list in its __NEXT_DATA__ JSON — title, artists (one combined string),
    duration, and the track uri. No album / ISRC, which is fine: the download
    search query only needs artist + title.
    """
    try:
        resp = requests.get(
            f"https://open.spotify.com/embed/playlist/{playlist_id}",
            headers={"User-Agent": _EMBED_UA},
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"network error reaching Spotify embed page: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(
            f"Spotify won't share this playlist (embed page HTTP {resp.status_code}) — "
            "it may be private or removed."
        )

    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text,
        re.S,
    )
    if not m:
        raise RuntimeError(
            "could not read Spotify's embed page — Spotify may have changed "
            "their site; try updating TuneHoard."
        )

    def _find_key(obj, key, depth=0):
        # The entity sits a few levels deep under props/pageProps; walk instead
        # of hardcoding the path so minor page reshuffles don't break us.
        if depth > 8:
            return None
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                found = _find_key(v, key, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = _find_key(v, key, depth + 1)
                if found is not None:
                    return found
        return None

    entity = _find_key(json.loads(m.group(1)), "entity") or {}
    name = entity.get("name") or entity.get("title") or "playlist"
    rows = entity.get("trackList") or []
    if not rows:
        raise RuntimeError(
            "Spotify won't share this playlist's tracks (private or restricted) — "
            "the embed page lists none."
        )

    tracks: list[Track] = []
    for row in rows:
        uri = row.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue  # skip episodes / local files
        artists = [a.strip() for a in (row.get("subtitle") or "").split(",") if a.strip()]
        tracks.append(
            Track(
                spotify_id=uri.rsplit(":", 1)[1],
                title=row.get("title", ""),
                artists=artists,
                album="",
                duration_ms=int(row.get("duration") or 0),
                isrc=None,
            )
        )

    print(
        f"  → Spotify blocks API access to this third-party playlist; "
        f"read {len(tracks)} tracks from the public embed page instead."
    )
    if len(rows) == _EMBED_SUSPECT_CAP:
        print(
            f"  ! embed page returned exactly {_EMBED_SUSPECT_CAP} tracks — the "
            "playlist may be longer (the embed caps its list). Longer tail "
            "tracks can't be fetched without owning the playlist."
        )
    return name, tracks


def get_playlist_tracks(
    playlist_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = "http://127.0.0.1:8888/callback",
    cache_path: str | Path = _DEFAULT_CACHE,
) -> tuple[str, list[Track]]:
    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="playlist-read-private playlist-read-collaborative user-library-read",
        cache_path=str(cache_path),
        open_browser=True,
    )
    sp = spotipy.Spotify(auth_manager=auth)

    pid = _extract_playlist_id(playlist_url)
    try:
        meta = sp.playlist(pid, fields="name")
        playlist_name = meta["name"]
    except SpotifyException as e:
        # Editorial / algorithmic playlists 403 or 404 even on metadata for
        # Development Mode apps. Try the anonymous embed page before giving up.
        if e.http_status in (403, 404):
            return _embed_playlist_tracks(pid)
        raise

    tracks: list[Track] = []
    # Spotify renamed the per-entry key from `track` to `item` in 2025 (unifying
    # tracks + episodes). Check both keys for resilience.
    try:
        results = sp.playlist_items(pid, additional_types=["track"])
    except SpotifyException as e:
        # Third-party playlists (owned by another account): Development Mode
        # apps get 403 on the items endpoint even though metadata worked.
        # Fall back to the public embed page — anonymous, no ownership check.
        if e.http_status == 403:
            return _embed_playlist_tracks(pid)
        raise
    while results:
        for entry in results["items"]:
            t = entry.get("item") or entry.get("track")
            if not t or t.get("type") == "episode" or not t.get("id"):
                continue
            tracks.append(
                Track(
                    spotify_id=t["id"],
                    title=t["name"],
                    artists=[a["name"] for a in t["artists"]],
                    album=t["album"]["name"],
                    duration_ms=t["duration_ms"],
                    isrc=(t.get("external_ids") or {}).get("isrc"),
                )
            )
        results = sp.next(results) if results.get("next") else None

    return playlist_name, tracks
