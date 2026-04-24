"""Spotify playlist → YouTube/SoundCloud MP3 downloads with BPM + Camelot key tags.

Usage:
    python main.py <spotify_playlist_url>
        [--sources youtube,soundcloud] [--out DIR]
        [--limit N] [--skip-existing]
"""

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

# Windows console defaults to cp1252 which can't print most unicode track titles
# or UI arrows. Reconfigure before any print.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from mutagen.id3 import ID3, ID3NoHeaderError
from tqdm import tqdm

from analyzer import analyze
from downloader import download_track
from spotify_client import Track, get_playlist_tracks
from tagger import tag_file


_SANITIZE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CSV_FIELDS = ["camelot", "bpm", "artist", "title", "album", "key", "source", "file", "spotify_id"]


def safe_filename(s: str, max_len: int = 120) -> str:
    s = _SANITIZE.sub("_", s).strip().rstrip(".")
    return s[:max_len] if len(s) > max_len else s


def safe_replace(src: Path, dst: Path, retries: int = 6, delay: float = 0.5) -> None:
    """os.replace() with retries — Windows antivirus/indexer briefly locks new MP3s."""
    for attempt in range(retries):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def process_track(track: Track, out_dir: Path, sources: list[str]) -> dict | None:
    """Try each source in order; first match wins. Returns None if all sources fail."""
    tmp_dir = out_dir / "_tmp"
    downloaded: Path | None = None
    used_source: str | None = None
    for src in sources:
        downloaded = download_track(track.search_query, tmp_dir, source=src)
        if downloaded:
            used_source = src
            break

    if not downloaded:
        return None

    try:
        result = analyze(downloaded)
    except Exception as e:
        print(f"  ! analysis failed ({e}); keeping file untagged")
        result = None

    if result:
        tag_file(
            downloaded,
            title=track.title,
            artist=track.primary_artist,
            album=track.album,
            bpm=result.bpm,
            camelot=result.camelot,
            key_name=result.key_name,
        )
        final_name = safe_filename(
            f"{result.camelot} - {result.bpm:03d} - {track.primary_artist} - {track.title}"
        ) + ".mp3"
    else:
        final_name = safe_filename(f"{track.primary_artist} - {track.title}") + ".mp3"

    final_path = out_dir / final_name
    safe_replace(downloaded, final_path)

    return {
        "title": track.title,
        "artist": track.primary_artist,
        "album": track.album,
        "bpm": result.bpm if result else "",
        "camelot": result.camelot if result else "",
        "key": result.key_name if result else "",
        "source": used_source,
        "file": final_path.name,
        "spotify_id": track.spotify_id,
    }


def load_existing_index(csv_path: Path) -> dict[str, dict]:
    """Load prior index.csv keyed by spotify_id so reruns can skip done tracks."""
    if not csv_path.exists():
        return {}
    out: dict[str, dict] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sid = row.get("spotify_id")
            if sid:
                out[sid] = row
    return out


def reconstruct_row_from_disk(track: Track, out_dir: Path) -> dict | None:
    """Find an mp3 in out_dir matching this track's artist-title and rebuild its row
    from ID3 tags. Lets --skip-existing survive CSV loss."""
    suffix = safe_filename(f"{track.primary_artist} - {track.title}") + ".mp3"
    for p in out_dir.glob("*.mp3"):
        if not p.name.endswith(suffix):
            continue
        bpm: int | str = ""
        camelot = ""
        try:
            tags = ID3(p)
            if "TBPM" in tags:
                bpm = int(str(tags["TBPM"].text[0]))
            if "TKEY" in tags:
                camelot = str(tags["TKEY"].text[0])
        except (ID3NoHeaderError, ValueError, KeyError):
            pass
        return {
            "title": track.title,
            "artist": track.primary_artist,
            "album": track.album,
            "bpm": bpm,
            "camelot": camelot,
            "key": "",
            "source": "",
            "file": p.name,
            "spotify_id": track.spotify_id,
        }
    return None


def _bpm_sort_key(row: dict) -> int:
    """Coerce bpm to int — rows from CSV have str bpm, fresh rows have int."""
    v = row.get("bpm")
    try:
        return int(v) if v else 0
    except (ValueError, TypeError):
        return 0


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("playlist", help="Spotify playlist URL or ID")
    ap.add_argument(
        "--sources",
        default="youtube,soundcloud",
        help="Comma-separated sources tried in order (default: youtube,soundcloud)",
    )
    ap.add_argument("--out", default="downloads", help="Output directory (default: downloads)")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N tracks (0 = all)")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip tracks already in the output index.csv (by spotify_id)",
    )
    args = ap.parse_args()

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for s in sources:
        if s not in ("youtube", "soundcloud"):
            sys.exit(f"Invalid source '{s}' — must be 'youtube' or 'soundcloud'")

    cid = os.getenv("SPOTIFY_CLIENT_ID")
    cs = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not cid or not cs:
        sys.exit("Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (copy .env.example to .env)")

    print("Fetching playlist from Spotify...")
    playlist_name, tracks = get_playlist_tracks(args.playlist, cid, cs)
    print(f"  → '{playlist_name}' ({len(tracks)} tracks)")

    if args.limit > 0:
        tracks = tracks[: args.limit]
        print(f"  → limited to first {len(tracks)}")

    out_dir = Path(args.out) / safe_filename(playlist_name)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "index.csv"
    existing: dict[str, dict] = {}
    if args.skip_existing:
        existing = load_existing_index(csv_path)
        # Fallback: also recover tracks whose MP3 is on disk but absent from CSV
        # (survives a prior crash that nuked the index).
        recovered = 0
        for t in tracks:
            if t.spotify_id in existing:
                continue
            row = reconstruct_row_from_disk(t, out_dir)
            if row:
                existing[t.spotify_id] = row
                recovered += 1
        if existing:
            print(
                f"  → resuming: {len(existing)} existing "
                f"({recovered} reconstructed from disk)"
            )

    rows: list[dict] = list(existing.values())
    failures: list[Track] = []
    to_process = [t for t in tracks if t.spotify_id not in existing]

    for track in tqdm(to_process, desc="Processing"):
        tqdm.write(f"→ {track.search_query}")
        row = process_track(track, out_dir, sources)
        if row:
            rows.append(row)
        else:
            tqdm.write("  ! skipped (no match on any source)")
            failures.append(track)

    tmp_dir = out_dir / "_tmp"
    if tmp_dir.exists():
        for leftover in tmp_dir.iterdir():
            leftover.unlink()
        tmp_dir.rmdir()

    # Atomic write: tmp file + replace, so a crash here can't wipe the index.
    sorted_rows = sorted(rows, key=lambda r: (str(r.get("camelot", "")), _bpm_sort_key(r)))
    tmp_csv = csv_path.with_suffix(".csv.tmp")
    with tmp_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    tmp_csv.replace(csv_path)

    if failures:
        fail_path = out_dir / "failures.txt"
        with fail_path.open("w", encoding="utf-8") as f:
            f.write(f"# {len(failures)} tracks with no match on any source ({', '.join(sources)}).\n")
            f.write("# Format: Artist - Title\tSpotify URL\n\n")
            for t in failures:
                url = f"https://open.spotify.com/track/{t.spotify_id}"
                f.write(f"{t.primary_artist} - {t.title}\t{url}\n")
        print(f"  ! {len(failures)} failed tracks written to {fail_path}")

    succeeded = len(rows) - len(existing)
    print(
        f"\nDone. {succeeded} new, {len(existing)} kept, "
        f"{len(failures)} failed. Index → {csv_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
