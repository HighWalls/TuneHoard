"""yt-dlp wrapper: search YouTube or SoundCloud, or download a known URL, as MP3."""

import sys
from pathlib import Path

import yt_dlp


def bundled_ffmpeg() -> str:
    """Return the path to a bundled ffmpeg binary when running under PyInstaller.

    The build pipeline places `ffmpeg.exe` (Windows) or `ffmpeg` (macOS) at the
    bundle root, which PyInstaller extracts to `sys._MEIPASS` at runtime.
    Source / dev runs return "" (caller falls back to PATH lookup).
    """
    if not getattr(sys, "frozen", False):
        return ""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return ""
    name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    candidate = Path(meipass) / name
    return str(candidate) if candidate.is_file() else ""


def _resolve_ffmpeg(ffmpeg_location: str) -> str:
    """Explicit caller-provided path wins; else fall back to the bundled binary
    when frozen; else return empty (PATH lookup)."""
    return ffmpeg_location or bundled_ffmpeg()


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
    resolved = _resolve_ffmpeg(ffmpeg_location)
    if resolved:
        # yt-dlp accepts either a directory containing ffmpeg(.exe) or the
        # full path to the executable itself.
        opts["ffmpeg_location"] = resolved
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
