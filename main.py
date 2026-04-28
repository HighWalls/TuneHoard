"""Spotify / YouTube / SoundCloud URL → MP3 downloads with BPM + Camelot key tags.

Accepts playlist URLs (all three platforms) or single track / video URLs
(Spotify track, YouTube video, SoundCloud track). Singles land in a `singles/`
subfolder under --out.

Usage:
    python main.py <url>
        [--sources youtube,soundcloud] [--out DIR]
        [--limit N] [--skip-existing]
        [--bucket-by-bpm] [--reanalyze]
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
from downloader import download_track, download_url
from spotify_client import Track, get_playlist_tracks, get_track
from tagger import tag_file
from ytdlp_loader import get_ytdlp_tracks, is_soundcloud_url, is_youtube_url


_SANITIZE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CSV_FIELDS = ["camelot", "bpm", "artist", "title", "album", "key", "source", "file", "spotify_id"]


def safe_filename(s: str, max_len: int = 120) -> str:
    s = _SANITIZE.sub("_", s).strip().rstrip(".")
    return s[:max_len] if len(s) > max_len else s


def bpm_bucket(bpm) -> str:
    """DJ-friendly BPM-range folder name.

    Anchor band is 115-125 (11 BPM wide). Everything above/below uses 10-wide
    bands that align to that anchor: 126-135, 136-145, ...  and 105-114, 95-104, ...
    Tracks with no BPM → 'unknown-bpm'.
    """
    try:
        b = int(bpm) if bpm not in (None, "") else None
    except (ValueError, TypeError):
        b = None
    if b is None:
        return "unknown-bpm"
    if 115 <= b <= 125:
        return "115-125"
    if b > 125:
        lower = 126 + ((b - 126) // 10) * 10
        return f"{lower}-{lower + 9}"
    # b < 115 — bands extend downward in 10-wide chunks
    upper = 114 - ((114 - b) // 10) * 10
    return f"{upper - 9}-{upper}"


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


def process_track(
    track: Track, out_dir: Path, sources: list[str], bucket_by_bpm: bool = False
) -> dict | None:
    """Download track, analyze, tag, rename. Returns None if no source matched.

    Tracks with a `source_url` (YouTube / SoundCloud playlist entries) download
    directly from that URL. Tracks without one (Spotify entries) fall back to
    title-based search across the configured sources in order.
    """
    tmp_dir = out_dir / "_tmp"
    downloaded: Path | None = None
    used_source: str | None = None

    if track.source_url:
        downloaded = download_url(track.source_url, tmp_dir)
        if downloaded:
            prefix = track.spotify_id.split(":", 1)[0] if ":" in track.spotify_id else ""
            used_source = {"yt": "youtube", "sc": "soundcloud"}.get(prefix, prefix or "url")
    else:
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

    if bucket_by_bpm:
        bucket_dir = out_dir / bpm_bucket(result.bpm if result else None)
        bucket_dir.mkdir(parents=True, exist_ok=True)
        final_path = bucket_dir / final_name
    else:
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
    """Find an mp3 anywhere under out_dir matching this track's artist-title and
    rebuild its row from ID3 tags. Lets --skip-existing survive CSV loss and
    finds files already moved into BPM bucket subfolders."""
    suffix = safe_filename(f"{track.primary_artist} - {track.title}") + ".mp3"
    for p in out_dir.rglob("*.mp3"):
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


def _row_bpm_int(row: dict) -> int | None:
    v = row.get("bpm")
    try:
        return int(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _expected_filename(row: dict) -> str | None:
    """Canonical filename for a row based on its current camelot/bpm/artist/title."""
    camelot = row.get("camelot") or ""
    bpm_int = _row_bpm_int(row)
    artist = row.get("artist", "")
    title = row.get("title", "")
    if not (camelot and bpm_int is not None and artist and title):
        return None
    return safe_filename(f"{camelot} - {bpm_int:03d} - {artist} - {title}") + ".mp3"


def _find_disk_file(row: dict, by_name: dict[str, Path], all_paths: list[Path]) -> Path | None:
    """Locate the MP3 for this row on disk, tolerating out-of-sync filenames.
    Exact filename match first (fast dict lookup), then artist/title suffix fallback."""
    expected = row.get("file")
    if expected and expected in by_name:
        return by_name[expected]
    artist = safe_filename(row.get("artist", ""))
    title = safe_filename(row.get("title", ""))
    if not (artist and title):
        return None
    suffix = f" - {artist} - {title}.mp3"
    for p in all_paths:
        if p.name.endswith(suffix):
            return p
    return None


def reanalyze_rows(rows: list[dict], out_dir: Path) -> int:
    """Re-run BPM/key analysis on each row's MP3. Updates the row dict in place,
    rewrites ID3 tags, and renames the file if the camelot/bpm prefix changed.
    Returns the count of rows whose values actually changed."""
    all_paths = list(out_dir.rglob("*.mp3"))
    by_name: dict[str, Path] = {p.name: p for p in all_paths}
    changed = 0
    for row in tqdm(rows, desc="Reanalyzing"):
        current = _find_disk_file(row, by_name, all_paths)
        if current is None:
            continue
        try:
            result = analyze(current)
        except Exception as e:
            tqdm.write(f"  ! reanalyze failed ({e}): {current.name}")
            continue
        if str(row.get("bpm") or "") == str(result.bpm) and (row.get("camelot") or "") == result.camelot:
            continue
        try:
            tag_file(
                current,
                title=row.get("title", ""),
                artist=row.get("artist", ""),
                album=row.get("album", ""),
                bpm=result.bpm,
                camelot=result.camelot,
                key_name=result.key_name,
            )
        except Exception as e:
            tqdm.write(f"  ! retag failed ({e}): {current.name}")
            continue
        new_name = (
            safe_filename(
                f"{result.camelot} - {result.bpm:03d} - "
                f"{row.get('artist', '')} - {row.get('title', '')}"
            )
            + ".mp3"
        )
        if current.name != new_name:
            new_path = current.parent / new_name
            try:
                safe_replace(current, new_path)
                by_name.pop(current.name, None)
                by_name[new_name] = new_path
                row["file"] = new_name
            except Exception as e:
                tqdm.write(f"  ! rename failed ({e}): {current.name}")
        row["bpm"] = result.bpm
        row["camelot"] = result.camelot
        row["key"] = result.key_name
        changed += 1
    return changed


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "url",
        help="Spotify / YouTube / SoundCloud URL — playlist or single track",
    )
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
    ap.add_argument(
        "--bucket-by-bpm",
        action="store_true",
        help="Organize downloads into BPM-range subfolders (115-125, 126-135, ...)",
    )
    ap.add_argument(
        "--reanalyze",
        action="store_true",
        help="Re-run BPM/key analysis on existing MP3s (for fixing prior half-time errors). "
             "Implies --skip-existing.",
    )
    args = ap.parse_args()
    if args.reanalyze:
        args.skip_existing = True

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for s in sources:
        if s not in ("youtube", "soundcloud"):
            sys.exit(f"Invalid source '{s}' — must be 'youtube' or 'soundcloud'")

    url = args.url
    if is_youtube_url(url):
        print("Fetching from YouTube...")
        playlist_name, tracks = get_ytdlp_tracks(url, "yt")
    elif is_soundcloud_url(url):
        print("Fetching from SoundCloud...")
        playlist_name, tracks = get_ytdlp_tracks(url, "sc")
    else:
        cid = os.getenv("SPOTIFY_CLIENT_ID")
        cs = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not cid or not cs:
            sys.exit(
                "Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET "
                "(copy .env.example to .env). Not required for YouTube/SoundCloud URLs."
            )
        if "/track/" in url or url.startswith("spotify:track:"):
            print("Fetching Spotify track...")
            playlist_name, tracks = get_track(url, cid, cs)
        else:
            print("Fetching Spotify playlist...")
            playlist_name, tracks = get_playlist_tracks(url, cid, cs)
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

    if args.reanalyze and existing:
        print(f"Reanalyzing {len(existing)} existing tracks...")
        n_changed = reanalyze_rows(list(existing.values()), out_dir)
        print(f"  → {n_changed}/{len(existing)} rows updated")

    rows: list[dict] = list(existing.values())
    failures: list[Track] = []
    to_process = [t for t in tracks if t.spotify_id not in existing]

    for track in tqdm(to_process, desc="Processing"):
        tqdm.write(f"→ {track.search_query}")
        row = process_track(track, out_dir, sources, bucket_by_bpm=args.bucket_by_bpm)
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

    if args.bucket_by_bpm:
        # Re-tag + rename + move each file so its name, tags, and folder location
        # all agree with the row values. Tolerates filenames that got out of sync
        # with the CSV (e.g., from a prior crashed reanalyze run).
        all_paths = list(out_dir.rglob("*.mp3"))
        by_name: dict[str, Path] = {p.name: p for p in all_paths}
        moved = 0
        retagged = 0
        renamed = 0
        for row in rows:
            current = _find_disk_file(row, by_name, all_paths)
            if current is None:
                continue
            bpm_int = _row_bpm_int(row)
            # Re-tag from row values (propagates CSV edits + reanalyze updates)
            if bpm_int is not None and row.get("camelot"):
                try:
                    tag_file(
                        current,
                        title=row.get("title", ""),
                        artist=row.get("artist", ""),
                        album=row.get("album", ""),
                        bpm=bpm_int,
                        camelot=row["camelot"],
                        key_name=row.get("key", "") or "",
                    )
                    retagged += 1
                except Exception as e:
                    tqdm.write(f"  ! retag failed ({e}): {current.name}")
                    continue
            # Rename the file to the canonical name for its row values.
            expected_name = _expected_filename(row) or current.name
            if current.name != expected_name:
                new_path = current.parent / expected_name
                try:
                    safe_replace(current, new_path)
                    by_name.pop(current.name, None)
                    by_name[expected_name] = new_path
                    current = new_path
                    renamed += 1
                except Exception as e:
                    tqdm.write(f"  ! rename failed ({e}): {current.name}")
            row["file"] = current.name
            # Move to correct bucket folder.
            target_dir = out_dir / bpm_bucket(row.get("bpm"))
            target_path = target_dir / current.name
            if current.resolve() == target_path.resolve():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                safe_replace(current, target_path)
                by_name.pop(current.name, None)
                by_name[target_path.name] = target_path
                moved += 1
            except Exception as e:
                tqdm.write(f"  ! move failed ({e}): {current.name}")
        # Remove any now-empty subdirs.
        for sub in sorted(
            (p for p in out_dir.iterdir() if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                sub.rmdir()
            except OSError:
                pass
        if moved or retagged or renamed:
            print(f"  → bucket sync: renamed {renamed}, retagged {retagged}, moved {moved}")

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
