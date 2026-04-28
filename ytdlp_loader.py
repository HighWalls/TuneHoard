"""Fetch tracks from a YouTube or SoundCloud URL via yt-dlp.

Handles both playlists and single videos/tracks. Returns the same `Track`
shape as the Spotify loader so the rest of the pipeline doesn't care where
the input came from. Entries carry a `source_url` so the downloader can fetch
them directly instead of doing a title-based search.
"""

import re

import yt_dlp

from spotify_client import Track

_YT_RE = re.compile(r"(?:youtube\.com|youtu\.be)", re.I)
_SC_RE = re.compile(r"soundcloud\.com", re.I)

# Strip common YouTube title noise like "(Official Video)" / "[HD]" / "[Lyric]".
_NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:official|video|audio|lyrics?|mv|hd|4k|visualizer|m/?v)\b[^\)\]]*[\)\]]",
    re.I,
)


def is_youtube_url(url: str) -> bool:
    return bool(_YT_RE.search(url))


def is_soundcloud_url(url: str) -> bool:
    return bool(_SC_RE.search(url))


def _parse_artist_title(raw_title: str, uploader: str) -> tuple[str, str]:
    """Best-effort parse of 'Artist - Title' from a YouTube/SC video title.
    Falls back to uploader as artist when no ' - ' separator is present."""
    cleaned = _NOISE_RE.sub("", raw_title).strip()
    parts = cleaned.split(" - ", 1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return (uploader or "").strip(), cleaned or raw_title.strip()


def _entry_to_track(entry: dict, id_prefix: str) -> Track | None:
    eid = entry.get("id") or ""
    if not eid:
        return None
    raw_title = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("channel") or entry.get("artist") or ""
    artist, title = _parse_artist_title(raw_title, uploader)
    # webpage_url is the canonical watch URL; entry["url"] is sometimes the
    # signed media URL (single-video extract) which expires. Prefer webpage_url.
    source_url = entry.get("webpage_url") or entry.get("url") or ""
    duration_s = entry.get("duration") or 0
    return Track(
        spotify_id=f"{id_prefix}:{eid}",
        title=title,
        artists=[artist] if artist else [],
        album=entry.get("album") or "",
        duration_ms=int(float(duration_s) * 1000),
        isrc=None,
        source_url=source_url,
    )


def get_ytdlp_tracks(url: str, id_prefix: str) -> tuple[str, list[Track]]:
    """Fetch tracks from a YouTube or SoundCloud URL — either a playlist or a
    single video/track. `id_prefix` is 'yt' or 'sc' and namespaces the
    spotify_id column so cross-source IDs can't collide. Single-track URLs
    return a folder name of 'singles' so they accumulate in one place."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # Single video/track has no `entries` key; playlists do.
    if "entries" not in info:
        track = _entry_to_track(info, id_prefix)
        return "singles", ([track] if track else [])

    playlist_name = info.get("title") or f"{id_prefix}_playlist"
    entries = info.get("entries") or []
    tracks = [t for t in (_entry_to_track(e, id_prefix) for e in entries if e) if t]
    return playlist_name, tracks
