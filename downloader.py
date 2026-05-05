"""yt-dlp wrapper: search YouTube or SoundCloud, or download a known URL, as MP3."""

from pathlib import Path

import yt_dlp


def _base_opts(out_dir: Path, ffmpeg_location: str = "") -> dict:
    opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }
    if ffmpeg_location:
        # yt-dlp accepts either a directory containing ffmpeg(.exe) or the
        # full path to the executable itself.
        opts["ffmpeg_location"] = ffmpeg_location
    return opts


def download_url(url: str, out_dir: Path, ffmpeg_location: str = "") -> Path | None:
    """Download a known video/track URL (YouTube, SoundCloud, etc.) as 320k mp3."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with yt_dlp.YoutubeDL(_base_opts(out_dir, ffmpeg_location)) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"  ! download failed: {e}")
        return None
    final = out_dir / f"{info['id']}.mp3"
    return final if final.exists() else None


def download_track(
    query: str,
    out_dir: Path,
    source: str = "youtube",
    ffmpeg_location: str = "",
) -> Path | None:
    """Search + download best audio match. Returns final mp3 path, or None on failure.

    source: "youtube" | "soundcloud"
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    search_prefix = {"youtube": "ytsearch1:", "soundcloud": "scsearch1:"}[source]
    ydl_opts = {**_base_opts(out_dir, ffmpeg_location), "default_search": search_prefix}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_prefix + query, download=True)
    except Exception as e:
        print(f"  ! download failed: {e}")
        return None

    if "entries" in info:
        if not info["entries"]:
            return None
        info = info["entries"][0]

    final = out_dir / f"{info['id']}.mp3"
    return final if final.exists() else None
