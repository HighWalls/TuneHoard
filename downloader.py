"""yt-dlp wrapper: search YouTube or SoundCloud and download best audio as MP3."""

from pathlib import Path

import yt_dlp


def download_track(query: str, out_dir: Path, source: str = "youtube") -> Path | None:
    """Search + download best audio match. Returns final mp3 path, or None on failure.

    source: "youtube" | "soundcloud"
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    search_prefix = {"youtube": "ytsearch1:", "soundcloud": "scsearch1:"}[source]
    tmpl = str(out_dir / "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "default_search": search_prefix,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }

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
