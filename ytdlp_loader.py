"""Fetch tracks from a YouTube or SoundCloud playlist via yt-dlp.

Returns the same `Track` shape as the Spotify loader so the rest of the pipeline
doesn't care where the playlist came from. Entries carry a `source_url` so the
downloader can fetch them directly instead of doing a title-based search.
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


def get_ytdlp_playlist(url: str, id_prefix: str) -> tuple[str, list[Track]]:
    """Fetch a YouTube or SoundCloud playlist. `id_prefix` is 'yt' or 'sc'
    and is used to namespace the spotify_id column so YT/SC/Spotify IDs
    can't collide."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    playlist_name = info.get("title") or f"{id_prefix}_playlist"
    entries = info.get("entries") or []

    tracks: list[Track] = []
    for entry in entries:
        if entry is None:
            continue
        eid = entry.get("id") or ""
        if not eid:
            continue
        raw_title = entry.get("title") or ""
        uploader = entry.get("uploader") or entry.get("channel") or entry.get("artist") or ""
        artist, title = _parse_artist_title(raw_title, uploader)
        source_url = entry.get("url") or entry.get("webpage_url") or ""
        duration_s = entry.get("duration") or 0
        tracks.append(
            Track(
                spotify_id=f"{id_prefix}:{eid}",
                title=title,
                artists=[artist] if artist else [],
                album=entry.get("album") or "",
                duration_ms=int(float(duration_s) * 1000),
                isrc=None,
                source_url=source_url,
            )
        )
    return playlist_name, tracks
