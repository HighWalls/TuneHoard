"""Fetch tracks from a Spotify playlist URL via the Web API.

Uses OAuth user flow because Spotify tightened Client Credentials access to
`playlist_items` in 2025 — even public playlists now return 401 without a user
token. First run opens a browser; subsequent runs read a cached token.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth


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
    cache_path: str | Path = ".spotify_cache",
) -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="playlist-read-private playlist-read-collaborative",
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
    cache_path: str | Path = ".spotify_cache",
) -> tuple[str, list[Track]]:
    sp = _spotify_client(client_id, client_secret, redirect_uri, cache_path)
    t = sp.track(_extract_track_id(track_url))
    return "singles", [_spotify_track_to_track(t)]


def get_playlist_tracks(
    playlist_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = "http://127.0.0.1:8888/callback",
    cache_path: str | Path = ".spotify_cache",
) -> tuple[str, list[Track]]:
    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="playlist-read-private playlist-read-collaborative",
        cache_path=str(cache_path),
        open_browser=True,
    )
    sp = spotipy.Spotify(auth_manager=auth)

    pid = _extract_playlist_id(playlist_url)
    meta = sp.playlist(pid, fields="name")
    playlist_name = meta["name"]

    tracks: list[Track] = []
    # Spotify renamed the per-entry key from `track` to `item` in 2025 (unifying
    # tracks + episodes). Check both keys for resilience.
    results = sp.playlist_items(pid, additional_types=["track"])
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
