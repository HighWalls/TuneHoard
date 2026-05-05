"""Spotify / YouTube / SoundCloud URL → MP3 downloads with BPM + key tags.

Accepts playlist URLs (all three platforms) or single track / video URLs
(Spotify track, YouTube video, SoundCloud track). Singles land in a `singles/`
subfolder under --out.

Both Camelot ('8A') and musical ('Am') notation are always written to ID3
in dedicated frames. --key-format controls which format goes into the
primary TKEY frame and the filename's leading prefix; the other format is
still in TXXX:MUSICAL_KEY / TXXX:CAMELOT_KEY. Existing files keep their
current format on syncs unless --migrate-keys is passed explicitly.

Usage:
    python main.py <url>
        [--sources youtube,soundcloud] [--out DIR]
        [--limit N] [--skip-existing]
        [--bucket-by-bpm] [--reanalyze]
        [--key-format camelot|musical]
        [--migrate-keys]
        [--bpm-min INT] [--bpm-max INT] [--analysis-seconds INT]
        [--ffmpeg-location PATH]
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
from camelot import musical_key_short
from downloader import download_track, download_url
from spotify_client import Track, get_liked_songs, get_playlist_tracks, get_track
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
    track: Track,
    out_dir: Path,
    sources: list[str],
    bucket_by_bpm: bool = False,
    skip_analyze: bool = False,
    key_format: str = "camelot",
    bpm_min: int = 85,
    bpm_max: int = 200,
    analysis_seconds: int = 120,
    ffmpeg_location: str = "",
) -> dict | None:
    """Download track, analyze, tag, rename. Returns None if no source matched.

    Tracks with a `source_url` (YouTube / SoundCloud playlist entries) download
    directly from that URL. Tracks without one (Spotify entries) fall back to
    title-based search across the configured sources in order.

    `key_format` ('camelot' or 'musical') controls the value written to the
    ID3 TKEY frame and the filename's key prefix.
    """
    tmp_dir = out_dir / "_tmp"
    downloaded: Path | None = None
    used_source: str | None = None

    if track.source_url:
        downloaded = download_url(track.source_url, tmp_dir, ffmpeg_location=ffmpeg_location)
        if downloaded:
            prefix = track.spotify_id.split(":", 1)[0] if ":" in track.spotify_id else ""
            used_source = {"yt": "youtube", "sc": "soundcloud"}.get(prefix, prefix or "url")
    else:
        for src in sources:
            downloaded = download_track(
                track.search_query, tmp_dir, source=src, ffmpeg_location=ffmpeg_location
            )
            if downloaded:
                used_source = src
                break

    if not downloaded:
        return None

    if skip_analyze:
        result = None
    else:
        try:
            result = analyze(
                downloaded, bpm_min=bpm_min, bpm_max=bpm_max, duration=analysis_seconds
            )
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
            key_format=key_format,
        )
        prefix = _key_prefix(result.camelot, result.key_name, key_format)
        final_name = safe_filename(
            f"{prefix} - {result.bpm:03d} - {track.primary_artist} - {track.title}"
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


_CAMELOT_RE = re.compile(r"^\d{1,2}[AB]$")
_MUSICAL_RE = re.compile(r"^[A-G][#b]?m?$")


def _classify_key_format(s: str) -> str | None:
    """'8A' -> 'camelot', 'Am' -> 'musical', anything else -> None."""
    if not s:
        return None
    if _CAMELOT_RE.match(s):
        return "camelot"
    if _MUSICAL_RE.match(s):
        return "musical"
    return None


def _existing_tkey_format(mp3_path: Path) -> str | None:
    """Read the file's TKEY value and classify which format it's in.
    Returns 'camelot', 'musical', or None (no TKEY / unrecognized value)."""
    try:
        tags = ID3(mp3_path)
    except (ID3NoHeaderError, Exception):
        return None
    if "TKEY" not in tags:
        return None
    return _classify_key_format(str(tags["TKEY"].text[0]))


def _key_prefix(camelot: str, key_name: str, key_format: str) -> str:
    """The string used as the filename's leading key chunk: '8A' or 'Am'."""
    if key_format == "musical" and key_name:
        return musical_key_short(key_name)
    return camelot


def _expected_filename(row: dict, key_format: str = "camelot") -> str | None:
    """Canonical filename for a row based on its current key/bpm/artist/title."""
    camelot = row.get("camelot") or ""
    bpm_int = _row_bpm_int(row)
    artist = row.get("artist", "")
    title = row.get("title", "")
    if not (camelot and bpm_int is not None and artist and title):
        return None
    prefix = _key_prefix(camelot, row.get("key", "") or "", key_format)
    return safe_filename(f"{prefix} - {bpm_int:03d} - {artist} - {title}") + ".mp3"


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


def reanalyze_rows(
    rows: list[dict],
    out_dir: Path,
    key_format: str = "camelot",
    migrate_keys: bool = False,
    bpm_min: int = 85,
    bpm_max: int = 200,
    analysis_seconds: int = 120,
) -> int:
    """Re-run BPM/key analysis on each row's MP3. Updates the row dict in place,
    rewrites ID3 tags, and renames the file if the key/bpm prefix changed.

    Each file's TKEY notation is preserved (the file gets new BPM/key values
    in the same format it was originally tagged with) unless `migrate_keys`
    is set, in which case every file is forced to `key_format`.

    Returns the count of rows whose values actually changed."""
    all_paths = list(out_dir.rglob("*.mp3"))
    by_name: dict[str, Path] = {p.name: p for p in all_paths}
    changed = 0
    for row in tqdm(rows, desc="Reanalyzing"):
        current = _find_disk_file(row, by_name, all_paths)
        if current is None:
            continue
        try:
            result = analyze(
                current, bpm_min=bpm_min, bpm_max=bpm_max, duration=analysis_seconds
            )
        except Exception as e:
            tqdm.write(f"  ! reanalyze failed ({e}): {current.name}")
            continue
        if str(row.get("bpm") or "") == str(result.bpm) and (row.get("camelot") or "") == result.camelot:
            continue
        # Preserve the file's existing TKEY format unless caller asked to migrate.
        effective_format = key_format
        if not migrate_keys:
            detected = _existing_tkey_format(current)
            if detected:
                effective_format = detected
        try:
            tag_file(
                current,
                title=row.get("title", ""),
                artist=row.get("artist", ""),
                album=row.get("album", ""),
                bpm=result.bpm,
                camelot=result.camelot,
                key_name=result.key_name,
                key_format=effective_format,
            )
        except Exception as e:
            tqdm.write(f"  ! retag failed ({e}): {current.name}")
            continue
        prefix = _key_prefix(result.camelot, result.key_name, effective_format)
        new_name = (
            safe_filename(
                f"{prefix} - {result.bpm:03d} - "
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
    ap.add_argument(
        "--into",
        default=None,
        help="Override the destination folder name (defaults to the playlist title or "
             "'singles'). Use this to merge single-track downloads into an existing "
             "playlist library — e.g. --out 'E:/tracks' --into 'get right!'.",
    )
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
        "--skip-analyze",
        action="store_true",
        help="Download only — skip BPM/key analysis. Tracks land untagged with "
             "an empty BPM/key in the CSV; re-run analysis later via --reanalyze "
             "or per-track in the dashboard.",
    )
    ap.add_argument(
        "--reanalyze",
        action="store_true",
        help="Re-run BPM/key analysis on existing MP3s (for fixing prior half-time errors). "
             "Implies --skip-existing.",
    )
    ap.add_argument(
        "--key-format",
        choices=["camelot", "musical"],
        default="camelot",
        help="Key format for new downloads' TKEY tag and filename prefix. "
             "'camelot' (default, Rekordbox) writes '8A'. "
             "'musical' (Traktor / Serato) writes 'Am'. Existing files keep "
             "their current format unless --migrate-keys is also passed.",
    )
    ap.add_argument(
        "--migrate-keys",
        action="store_true",
        help="On bucket-sync / reanalyze, force every existing file's TKEY tag "
             "and filename prefix to the chosen --key-format. Without this, "
             "files keep the format they were originally tagged with.",
    )
    ap.add_argument(
        "--bpm-min",
        type=int,
        default=85,
        help="Lower BPM clamp for the half/double-time normalizer (default: 85). "
             "Detected BPMs below this are doubled.",
    )
    ap.add_argument(
        "--bpm-max",
        type=int,
        default=200,
        help="Upper BPM clamp for the half/double-time normalizer (default: 200). "
             "Detected BPMs above this are halved.",
    )
    ap.add_argument(
        "--analysis-seconds",
        type=int,
        default=120,
        help="Seconds of audio loaded for BPM/key analysis (default: 120). "
             "Lower = faster scans, higher = more accurate on long intros.",
    )
    ap.add_argument(
        "--ffmpeg-location",
        default="",
        help="Path to ffmpeg directory or executable. If unset, yt-dlp uses PATH.",
    )
    args = ap.parse_args()
    if args.reanalyze:
        args.skip_existing = True

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for s in sources:
        if s not in ("youtube", "soundcloud"):
            sys.exit(f"Invalid source '{s}' — must be 'youtube' or 'soundcloud'")

    url = args.url
    # Sentinel from the dashboard's Spotify picker. Must be checked BEFORE the
    # generic spotify regex below — it's not a real URL, just a magic string.
    if url.strip().lower() == "spotify:liked":
        cid = os.getenv("SPOTIFY_CLIENT_ID")
        cs = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not cid or not cs:
            sys.exit(
                "Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET "
                "(copy .env.example to .env). Not required for YouTube/SoundCloud URLs."
            )
        print("Fetching Spotify Liked Songs...")
        playlist_name, tracks = get_liked_songs(cid, cs)
    elif is_youtube_url(url):
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

    folder_name = args.into if args.into else playlist_name
    out_dir = Path(args.out) / safe_filename(folder_name)
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
        n_changed = reanalyze_rows(
            list(existing.values()),
            out_dir,
            key_format=args.key_format,
            migrate_keys=args.migrate_keys,
            bpm_min=args.bpm_min,
            bpm_max=args.bpm_max,
            analysis_seconds=args.analysis_seconds,
        )
        print(f"  → {n_changed}/{len(existing)} rows updated")

    rows: list[dict] = list(existing.values())
    failures: list[Track] = []
    to_process = [t for t in tracks if t.spotify_id not in existing]

    for track in tqdm(to_process, desc="Processing"):
        tqdm.write(f"→ {track.search_query}")
        row = process_track(
            track,
            out_dir,
            sources,
            bucket_by_bpm=args.bucket_by_bpm,
            skip_analyze=args.skip_analyze,
            key_format=args.key_format,
            bpm_min=args.bpm_min,
            bpm_max=args.bpm_max,
            analysis_seconds=args.analysis_seconds,
            ffmpeg_location=args.ffmpeg_location,
        )
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
            # Per-file: preserve the file's current TKEY format unless caller
            # asked to migrate. New downloads (no TKEY yet) and unrecognized
            # values fall through to args.key_format.
            effective_format = args.key_format
            if not args.migrate_keys:
                detected = _existing_tkey_format(current)
                if detected:
                    effective_format = detected
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
                        key_format=effective_format,
                    )
                    retagged += 1
                except Exception as e:
                    tqdm.write(f"  ! retag failed ({e}): {current.name}")
                    continue
            # Filename: accept either Camelot- or musical-prefixed name as
            # valid (so changing --key-format mid-library doesn't trigger
            # mass rename). Only rename if the current name doesn't match
            # any valid form, or if caller explicitly asked to migrate.
            valid_names = {
                _expected_filename(row, key_format="camelot"),
                _expected_filename(row, key_format="musical"),
            }
            valid_names.discard(None)
            if args.migrate_keys or current.name not in valid_names:
                expected_name = _expected_filename(row, key_format=args.key_format) or current.name
            else:
                expected_name = current.name
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
