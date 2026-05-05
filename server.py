"""TuneHoard FastAPI server: wires the existing CLI to the dashboard.

Run:
    .venv/Scripts/python.exe server.py

Then open http://127.0.0.1:8765/ — auto-opened by default.

Endpoints (all under /api/):
    GET    /api/settings          — current settings dict
    PATCH  /api/settings          — partial update of settings
    GET    /api/library           — list of tracks in the configured library_dir
    PATCH  /api/tracks/{id}       — update bpm / camelot / key for one track
    DELETE /api/tracks/{id}       — remove track from CSV + delete MP3 file
    POST   /api/tracks/{id}/move  — drag-drop move to a different bucket
    POST   /api/tracks/{id}/reanalyze — re-run librosa on the file, update row
    POST   /api/migrate-keys      — bulk: rewrite TKEY + filename to current key_format
    POST   /api/jobs              — start a download job (URL → MP3s)
    GET    /api/jobs              — list jobs (running + recent)
    GET    /api/jobs/{job_id}/log — get streamed log of a job
    GET    /                      — serve the dashboard HTML
    GET    /static/*              — anything under dashboard/tunehoard/
"""

# librosa/numba/openblas can race at import on Windows with default thread
# pools; pin to single-thread BEFORE the heavy imports run.
import os as _os
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
_os.environ.setdefault("OMP_NUM_THREADS", "1")
_os.environ.setdefault("PYTHONIOENCODING", "utf-8")
del _os

# Source-of-truth running version. Used by /api/version for upstream-update
# checks and as the User-Agent for the GitHub releases probe.
__version__ = "0.1.0"

import asyncio
import csv
import datetime as _dt
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from mutagen.id3 import ID3, ID3NoHeaderError

# Progress-parsing patterns for main.py's stdout.
_TOTAL_RE = re.compile(r"\((\d+)\s+tracks?\)")
_DONE_RE = re.compile(r"^Done\.\s+(\d+)\s+new")

import spotipy

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────────────
# Reuse existing CLI logic. main.py defines several helpers we share.
# ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# Default port; overridden at startup if 8765 is in use. The host-header
# validation middleware reads this to whitelist `localhost:<port>` and
# `127.0.0.1:<port>` for the ACTUAL bound port. Default-init to 8765 so
# tests / standalone imports get a sane value before main() runs.
_CHOSEN_PORT: int = 8765

from analyzer import analyze
from camelot import musical_key_short
from main import (  # type: ignore[no-redef]
    CSV_FIELDS,
    _bpm_sort_key,
    _classify_key_format,
    _expected_filename,
    _existing_tkey_format,
    _find_disk_file,
    _key_prefix,
    _row_bpm_int,
    bpm_bucket,
    safe_filename,
    safe_replace,
)
from spotify_client import _spotify_client, get_liked_songs, get_playlist_tracks, get_track
from tagger import tag_file
from ytdlp_loader import get_ytdlp_tracks, is_soundcloud_url, is_youtube_url


# ──────────────────────────────────────────────────────────────────────
# Settings — persisted JSON next to the project.
# ──────────────────────────────────────────────────────────────────────
SETTINGS_FILE = PROJECT_ROOT / ".tunehoard_settings.json"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard" / "tunehoard"
DASHBOARD_HTML = DASHBOARD_DIR / "tunehoard.html"

DEFAULT_SETTINGS: dict[str, Any] = {
    # The single folder where MP3s are downloaded AND the library is read from.
    # Also used as `--out / --into` when spawning main.py jobs.
    "library_dir": str(PROJECT_ROOT / "downloads"),
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "sources": ["youtube", "soundcloud"],
    "key_format": "camelot",
    "bucket_by_bpm": True,
    "skip_existing": True,
    "skip_analyze": False,
    "bpm_min": 85,
    "bpm_max": 200,
    "analysis_seconds": 120,
    "ffmpeg_path": "",
}


def load_settings() -> dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            # One-time migration: older configs had a separate output_dir.
            # If library_dir is empty/missing and output_dir is set, fold it in.
            if not stored.get("library_dir") and stored.get("output_dir"):
                stored["library_dir"] = stored["output_dir"]
            stored.pop("output_dir", None)
            return {**DEFAULT_SETTINGS, **stored}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(s: dict[str, Any]) -> None:
    # Never persist the dead output_dir key, even if the caller passes it.
    s = {k: v for k, v in s.items() if k != "output_dir"}
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


# ──────────────────────────────────────────────────────────────────────
# Library — read/write the configured playlist's index.csv.
# ──────────────────────────────────────────────────────────────────────
def _csv_path() -> Path | None:
    s = load_settings()
    if not s["library_dir"]:
        return None
    p = Path(s["library_dir"]) / "index.csv"
    return p if p.exists() else None


def _read_library() -> list[dict[str, Any]]:
    csv_path = _csv_path()
    if csv_path is None:
        return []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_library(rows: list[dict[str, Any]]) -> None:
    """Atomic write — same shape as main.py's CSV write block. Creates the
    destination path even if no index.csv exists yet (used by /api/library/scan
    for first-time initialization of a folder)."""
    s = load_settings()
    if not s["library_dir"]:
        return
    out_dir = Path(s["library_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "index.csv"
    sorted_rows = sorted(rows, key=lambda r: (str(r.get("camelot", "")), _bpm_sort_key(r)))
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    tmp.replace(csv_path)


def _to_dashboard_track(row: dict[str, Any]) -> dict[str, Any]:
    """CSV row → JSON shape the dashboard expects."""
    cam = row.get("camelot", "") or ""
    key_full = row.get("key", "") or ""
    musical = musical_key_short(key_full) if key_full else ""
    # When BPM is missing, bucket should be 'unknown-bpm' — passing 0 to
    # bpm_bucket() would compute a nonsense band like '-5-4'.
    bpm_str = (row.get("bpm") or "").strip() if isinstance(row.get("bpm"), str) else row.get("bpm")
    try:
        bpm_val = int(bpm_str) if bpm_str not in (None, "") else 0
    except (ValueError, TypeError):
        bpm_val = 0
    bucket = bpm_bucket(bpm_val) if bpm_val > 0 else "unknown-bpm"
    src = (row.get("source", "") or "").lower()
    src_short = {"youtube": "YT", "soundcloud": "SC", "spotify": "SP"}.get(src, "")
    return {
        "id": row.get("spotify_id", "") or "",
        "cam": cam,
        "key": musical,
        "bpm": bpm_val,
        "artist": row.get("artist", "") or "",
        "title": row.get("title", "") or "",
        "source": src_short,
        "bucket": bucket,
        "file": row.get("file", "") or "",
    }


def _find_row(rows: list[dict[str, Any]], track_id: str) -> dict[str, Any] | None:
    for r in rows:
        if r.get("spotify_id") == track_id:
            return r
    return None


def _disk_index(out_dir: Path) -> tuple[dict[str, Path], list[Path]]:
    paths = list(out_dir.rglob("*.mp3"))
    return {p.name: p for p in paths}, paths


def _apply_row_to_disk(
    row: dict[str, Any],
    out_dir: Path,
    *,
    key_format: str,
    rename: bool = True,
    move_bucket: bool = True,
) -> None:
    """Write tags + (optionally) rename + (optionally) move-to-bucket for a single row.

    Mirrors what main.py's bucket-sync pass does for one track. Doesn't touch the CSV
    here — the caller is expected to persist it via _write_library afterwards.
    """
    by_name, all_paths = _disk_index(out_dir)
    current = _find_disk_file(row, by_name, all_paths)
    if current is None:
        return  # file not on disk; nothing to do
    bpm_int = _row_bpm_int(row)
    if bpm_int is None or not row.get("camelot"):
        return
    tag_file(
        current,
        title=row.get("title", "") or "",
        artist=row.get("artist", "") or "",
        album=row.get("album", "") or "",
        bpm=bpm_int,
        camelot=row["camelot"],
        key_name=row.get("key", "") or "",
        key_format=key_format,
    )
    if rename:
        expected = _expected_filename(row, key_format=key_format) or current.name
        if current.name != expected:
            new_path = current.parent / expected
            safe_replace(current, new_path)
            current = new_path
            row["file"] = expected
    if move_bucket:
        target_dir = out_dir / bpm_bucket(row.get("bpm"))
        target_path = target_dir / current.name
        if current.resolve() != target_path.resolve():
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_replace(current, target_path)


# ──────────────────────────────────────────────────────────────────────
# Jobs — subprocess-based. We launch main.py per request.
# ──────────────────────────────────────────────────────────────────────
JOBS: dict[str, dict[str, Any]] = {}
# Live subprocess.Popen handles for running jobs. Kept separate from JOBS so
# we don't leak non-JSON-serializable values into the API responses.
PROCS: dict[str, subprocess.Popen] = {}
JOBS_LOCK = threading.Lock()

# Background scans (analyze_missing=true). Same record shape as JOBS so the
# dashboard can poll/render with the same code path. Cancellation is best-
# effort: we set a `cancelled` flag, and the worker checks between tracks.
SCANS: dict[str, dict[str, Any]] = {}
SCANS_LOCK = threading.Lock()

# ── WebSocket push for /ws/jobs ───────────────────────────────────────
# Connected clients receive a snapshot on connect, then incremental updates
# whenever JOBS state changes. Polling on /api/jobs is preserved as a fallback.
_WS_JOBS_CONNECTIONS: set[WebSocket] = set()
_WS_LOCK = threading.Lock()  # threading (not asyncio) — broadcast is called from
# the runner thread, which has no event loop of its own.
_WS_LOOP: asyncio.AbstractEventLoop | None = None  # captured on first WS accept

# /api/version cache. 60-second TTL keeps a refresh-happy user from pounding
# the GitHub API. Populated lazily on first hit.
_VERSION_CACHE: dict[str, Any] = {"value": None, "fetched_at": 0.0}
_VERSION_CACHE_TTL = 60.0


def _jobs_response() -> list[dict[str, Any]]:
    """Snapshot of JOBS shaped for the dashboard. Same payload as GET /api/jobs.
    Caller is responsible for thread-safety; this acquires JOBS_LOCK itself."""
    with JOBS_LOCK:
        out = []
        for j in JOBS.values():
            # Drop internal-only keys (the broadcast throttle stamp) and keep
            # only the last 3 log lines — full log lives behind /api/jobs/{id}/log.
            snap = {k: v for k, v in j.items() if not k.startswith("_")}
            snap["log"] = j["log"][-3:]
            out.append(snap)
        return out


def _broadcast_jobs() -> None:
    """Push the current JOBS snapshot to every connected /ws/jobs client.

    Safe to call from any thread. Schedules `ws.send_json(...)` on the captured
    event loop via `run_coroutine_threadsafe`. If no loop has been captured yet
    (e.g. nobody has ever connected) this silently no-ops.
    """
    try:
        loop = _WS_LOOP
        if loop is None:
            return
        with _WS_LOCK:
            conns = list(_WS_JOBS_CONNECTIONS)
        if not conns:
            return
        payload = {"type": "update", "jobs": _jobs_response()}
        for ws in conns:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
            except Exception:
                # Best-effort: a dead socket will surface on the next receive_text
                # in the handler and be removed there. Don't try to fix it here.
                pass
    except Exception:
        pass


def _scan_snapshot(scan: dict[str, Any]) -> dict[str, Any]:
    """Trim a SCANS record for over-the-wire shipping. Drops internal-only
    keys, truncates the log to the last 5 entries (full log isn't currently
    queryable but capping keeps WS frames small)."""
    out = {k: v for k, v in scan.items() if not k.startswith("_")}
    log = out.get("log") or []
    if isinstance(log, list):
        out["log"] = log[-5:]
    return out


def _broadcast_scans() -> None:
    """Push the current SCANS state to every /ws/jobs client. Wrapped in
    {type: 'scan_update', scan: {...}} so the frontend distinguishes from
    job updates. Sends one frame per scan (almost always 1, rarely 2)."""
    try:
        loop = _WS_LOOP
        if loop is None:
            return
        with _WS_LOCK:
            conns = list(_WS_JOBS_CONNECTIONS)
        if not conns:
            return
        with SCANS_LOCK:
            snaps = [_scan_snapshot(s) for s in SCANS.values()]
        for snap in snaps:
            payload = {"type": "scan_update", "scan": snap}
            for ws in conns:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
                except Exception:
                    pass
    except Exception:
        pass


def _spawn_job(
    url: str,
    *,
    sources: list[str],
    bucket_by_bpm: bool,
    skip_existing: bool,
    skip_analyze: bool,
    key_format: str,
    limit: int,
) -> str:
    job_id = f"j{int(time.time() * 1000)}"
    s = load_settings()
    # Pin the destination to the user's currently-viewed library directory so
    # that single-track downloads, new-playlist downloads, and incremental
    # adds all merge into the same folder. --into overrides the loader's
    # default ("singles" or the playlist title).
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured — open Settings")
    lib_dir = Path(s["library_dir"])
    out_parent = str(lib_dir.parent)
    into_name = lib_dir.name
    args = [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        url,
        "--out", out_parent,
        "--into", into_name,
        "--sources", ",".join(sources or s["sources"]),
        "--key-format", key_format,
        "--bpm-min", str(s["bpm_min"]),
        "--bpm-max", str(s["bpm_max"]),
        "--analysis-seconds", str(s["analysis_seconds"]),
    ]
    if s.get("ffmpeg_path"):
        args.extend(["--ffmpeg-location", s["ffmpeg_path"]])
    if bucket_by_bpm:
        args.append("--bucket-by-bpm")
    if skip_existing:
        args.append("--skip-existing")
    if skip_analyze:
        args.append("--skip-analyze")
    if limit:
        args.extend(["--limit", str(limit)])

    env = os.environ.copy()
    if s["spotify_client_id"]:
        env["SPOTIFY_CLIENT_ID"] = s["spotify_client_id"]
    if s["spotify_client_secret"]:
        env["SPOTIFY_CLIENT_SECRET"] = s["spotify_client_secret"]
    env["PYTHONIOENCODING"] = "utf-8"

    job: dict[str, Any] = {
        "id": job_id,
        "url": url,
        "status": "queued",
        "log": [],
        "total": 0,
        "done": 0,
        "failed": 0,
        "current": "",
        "eta_sec": 0,
        "started_at": time.time(),
        "finished_at": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    def runner() -> None:
        # Throttle WS broadcasts during chatty downloads (yt-dlp writes lots of
        # progress lines per track). Always broadcast on terminal states; that
        # is handled by callers below using `force=True` instead of this helper.
        def maybe_broadcast() -> None:
            now = time.time()
            last = job.get("_last_broadcast", 0.0)
            if now - last >= 0.5:
                job["_last_broadcast"] = now
                _broadcast_jobs()

        try:
            proc = subprocess.Popen(
                args,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            with JOBS_LOCK:
                job["status"] = "running"
                job["pid"] = proc.pid
                PROCS[job_id] = proc
            _broadcast_jobs()  # status flipped queued→running; push immediately
            assert proc.stdout is not None
            track_in_progress = False  # True between a "→ Track" and the next track-end signal
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                terminal_done = False
                with JOBS_LOCK:
                    job["log"].append(line)
                    if len(job["log"]) > 2000:
                        job["log"] = job["log"][-1500:]

                    # Final summary line — set status + saturate the progress bar.
                    if _DONE_RE.match(line):
                        if job.get("total"):
                            job["done"] = job["total"]
                        job["current"] = ""
                        job["eta_sec"] = 0
                        job["status"] = "done"
                        terminal_done = True

                    # Total tracks announcement: "  → 'name' (N tracks)"
                    elif "tracks" in line and "(" in line:
                        m = _TOTAL_RE.search(line)
                        if m:
                            job["total"] = int(m.group(1))

                    # Per-track start: a line that BEGINS with "→ " (no leading
                    # whitespace). Excludes the playlist intro (2-space indent),
                    # bucket-sync info, and the "Index → path" substring in the
                    # Done line (handled above).
                    elif line.startswith("→ "):
                        # Bumping done on the *next* track start means the previous
                        # one finished. Cap at total to be safe.
                        if track_in_progress:
                            tot = job.get("total") or 0
                            job["done"] = min(job.get("done", 0) + 1, tot or job.get("done", 0) + 1)
                        job["current"] = line[2:].strip()
                        track_in_progress = True
                        # ETA from elapsed + rate
                        d, t = job.get("done", 0), job.get("total", 0)
                        if d > 0 and t > 0:
                            elapsed = time.time() - job["started_at"]
                            rate = d / elapsed
                            if rate > 0:
                                job["eta_sec"] = int((t - d) / rate)

                    # Skipped track ("  ! skipped (no match on any source)")
                    elif "skipped" in line:
                        job["failed"] += 1
                        if track_in_progress:
                            tot = job.get("total") or 0
                            job["done"] = min(job.get("done", 0) + 1, tot or job.get("done", 0) + 1)
                            track_in_progress = False

                if terminal_done:
                    _broadcast_jobs()  # always push terminal states
                else:
                    maybe_broadcast()  # throttled to ~2 Hz during normal progress
            proc.wait()
            with JOBS_LOCK:
                job["finished_at"] = time.time()
                # If a DELETE marked the job cancelled, leave it alone.
                if job["status"] in ("cancelled", "done"):
                    pass
                elif proc.returncode != 0:
                    job["status"] = "failed"
                elif job["status"] == "running":
                    job["status"] = "done"
                PROCS.pop(job_id, None)
            _broadcast_jobs()  # final status (done/failed/cancelled)
        except Exception as e:
            with JOBS_LOCK:
                if job["status"] != "cancelled":
                    job["status"] = "failed"
                job["log"].append(f"!! server error: {e}")
                PROCS.pop(job_id, None)
            _broadcast_jobs()

    threading.Thread(target=runner, daemon=True).start()
    return job_id


# ──────────────────────────────────────────────────────────────────────
# FastAPI app + endpoints.
# ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="TuneHoard", version=__version__)


# ── Host header validation (DNS-rebinding guard) ──────────────────────
# Localhost-only server. We don't issue CSRF tokens (overkill for a single-
# user local dashboard) but we DO validate `Host:` on every mutating request
# to defend against a malicious page resolving its own hostname to 127.0.0.1
# and POSTing to us cross-origin. The middleware closes over `_CHOSEN_PORT`,
# which is assigned by `find_free_port()` BEFORE uvicorn.run() — so by the
# time any request arrives, the global is the actual bound port.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _host_header_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.method in _MUTATING_METHODS:
        host_hdr = (request.headers.get("host") or "").strip().lower()
        allowed = {
            f"127.0.0.1:{_CHOSEN_PORT}",
            f"localhost:{_CHOSEN_PORT}",
            # Also allow the canonical 8765 forms regardless of fallback —
            # some bookmarks / dashboards may still hit those even when we're
            # bound elsewhere. (Belt-and-braces; harmless if _CHOSEN_PORT
            # equals 8765.)
            "127.0.0.1:8765",
            "localhost:8765",
        }
        if host_hdr not in allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": f"host header rejected: {host_hdr}"},
            )
    return await call_next(request)


# ── Settings ──────────────────────────────────────────────────────────
class SettingsPatch(BaseModel):
    library_dir: str | None = None
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    sources: list[str] | None = None
    key_format: str | None = None
    bucket_by_bpm: bool | None = None
    skip_existing: bool | None = None
    skip_analyze: bool | None = None
    bpm_min: int | None = None
    bpm_max: int | None = None
    analysis_seconds: int | None = None
    ffmpeg_path: str | None = None


@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    s = load_settings()
    # Don't leak the secret to the client unless explicitly requested.
    s = dict(s)
    if s.get("spotify_client_secret"):
        s["spotify_client_secret"] = "*" * 8
    return s


@app.patch("/api/settings")
def api_patch_settings(patch: SettingsPatch) -> dict[str, Any]:
    s = load_settings()
    update = patch.model_dump(exclude_unset=True)
    if "key_format" in update and update["key_format"] not in ("camelot", "musical"):
        raise HTTPException(400, "key_format must be 'camelot' or 'musical'")
    s.update(update)
    save_settings(s)
    return api_get_settings()


# ── URL preview (lightweight, no download) ────────────────────────────
@app.get("/api/preview")
def api_preview(url: str) -> dict[str, Any]:
    """Hit the right loader to get a real title/track-count for the input URL.

    YouTube / SoundCloud go through yt-dlp's extract_info (extract_flat). Spotify
    goes through spotipy if creds are configured. No downloading happens.
    """
    url = (url or "").strip()
    if not url:
        return {"kind": "empty", "label": ""}
    s = load_settings()
    # Sentinel from the Spotify picker. The frontend already short-circuits this
    # in setStatus() — but if anything calls /api/preview programmatically we
    # still want a structured response instead of a 400.
    if url.lower() == "spotify:liked":
        cid, cs = s["spotify_client_id"], s["spotify_client_secret"]
        if not cid or not cs:
            return {"kind": "sp-unconfigured", "label": "Spotify not configured — open Settings"}
        try:
            sp = _spotify_client(cid, cs)
            result = sp.current_user_saved_tracks(limit=1)
            total = int((result or {}).get("total", 0))
        except Exception as e:
            return {"kind": "error", "label": f"preview failed: {type(e).__name__}"}
        return {
            "kind": "spotify_liked",
            "label": f"Spotify Liked Songs — {total} tracks",
            "name": "Liked Songs",
            "track_count": total,
        }
    try:
        if is_youtube_url(url):
            name, tracks = get_ytdlp_tracks(url, "yt")
            src_label, src_short = "YouTube", "yt"
            single_word = "video"
        elif is_soundcloud_url(url):
            name, tracks = get_ytdlp_tracks(url, "sc")
            src_label, src_short = "SoundCloud", "sc"
            single_word = "track"
        elif "open.spotify.com" in url or url.startswith("spotify:"):
            cid, cs = s["spotify_client_id"], s["spotify_client_secret"]
            if not cid or not cs:
                return {"kind": "sp-unconfigured", "label": "Spotify not configured — open Settings"}
            if "/track/" in url or url.startswith("spotify:track:"):
                name, tracks = get_track(url, cid, cs)
            else:
                name, tracks = get_playlist_tracks(url, cid, cs)
            src_label, src_short = "Spotify", "sp"
            single_word = "track"
        else:
            return {"kind": "unsupported", "label": "unsupported URL"}
    except Exception as e:
        return {"kind": "error", "label": f"preview failed: {type(e).__name__}"}

    if name == "singles" and len(tracks) == 1:
        t = tracks[0]
        return {
            "kind": f"{src_short}-tr",
            "label": f"{src_label} {single_word}: {t.primary_artist} — {t.title}",
            "name": name,
            "track_count": 1,
        }
    return {
        "kind": f"{src_short}-pl",
        "label": f'{src_label} playlist: "{name}" — {len(tracks)} tracks',
        "name": name,
        "track_count": len(tracks),
    }


# ── Library ───────────────────────────────────────────────────────────
@app.get("/api/library")
def api_get_library() -> list[dict[str, Any]]:
    return [_to_dashboard_track(r) for r in _read_library()]


@app.get("/api/failures")
def api_get_failures() -> list[dict[str, Any]]:
    """Read <library_dir>/failures.txt and return parsed failed tracks.

    File format (written by main.py's job runner):
        # comment lines start with '#'
        Artist - Title<TAB>https://open.spotify.com/track/<id>

    Lenient parser: malformed lines fall back to artist="" and put the whole
    left-of-tab into title rather than crashing. Returns [] (with HTTP 200) if
    library_dir isn't set or failures.txt doesn't exist.
    """
    s = load_settings()
    if not s["library_dir"]:
        return []
    fail_path = Path(s["library_dir"]) / "failures.txt"
    if not fail_path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = fail_path.read_text(encoding="utf-8")
    except Exception:
        return []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Split off the URL on the first TAB.
        if "\t" in line:
            left, url = line.split("\t", 1)
        else:
            left, url = line, ""
        left = left.strip()
        url = url.strip()
        # Split "Artist - Title" on first ' - '.
        if " - " in left:
            artist, title = left.split(" - ", 1)
            artist = artist.strip()
            title = title.strip()
        else:
            artist = ""
            title = left
        out.append({"artist": artist, "title": title, "spotify_url": url})
    return out


class FailureDismiss(BaseModel):
    spotify_url: str


@app.post("/api/failures/dismiss")
def api_dismiss_failure(req: FailureDismiss) -> list[dict[str, Any]]:
    """Remove one failure row (by exact URL match) from `<library_dir>/failures.txt`
    and rewrite atomically. If removing the row leaves only header / comment
    lines, deletes the file entirely so the dashboard's hidden-when-empty UI
    stays accurate. Returns the updated failures list (same shape as
    GET /api/failures)."""
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(404, "library_dir not configured")
    fail_path = Path(s["library_dir"]) / "failures.txt"
    if not fail_path.exists():
        raise HTTPException(404, "failures.txt does not exist")

    target = (req.spotify_url or "").strip()
    if not target:
        raise HTTPException(400, "spotify_url required")

    try:
        text = fail_path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"failed to read failures.txt: {e}")

    kept_lines: list[str] = []
    removed = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            kept_lines.append(raw)
            continue
        # The data-row format is "Artist - Title<TAB>URL". Match URL exactly.
        url = line.split("\t", 1)[1].strip() if "\t" in line else ""
        if not removed and url == target:
            removed = True
            continue
        kept_lines.append(raw)

    if not removed:
        raise HTTPException(404, "not in failures.txt")

    # If everything left is comments / blanks, just unlink the file. Otherwise
    # atomic rewrite (.tmp → replace), same pattern as index.csv.
    has_data = any(
        ln.strip() and not ln.strip().startswith("#") for ln in kept_lines
    )
    if not has_data:
        try:
            fail_path.unlink()
        except Exception as e:
            raise HTTPException(500, f"failed to delete failures.txt: {e}")
    else:
        tmp = fail_path.with_suffix(".txt.tmp")
        try:
            tmp.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
            tmp.replace(fail_path)
        except Exception as e:
            raise HTTPException(500, f"failed to rewrite failures.txt: {e}")

    return api_get_failures()


def _short_musical_to_full(short: str) -> str:
    """'Am' -> 'A minor', 'C' -> 'C major', 'C#m' -> 'C# minor'.
    Inverse of camelot.musical_key_short(). Empty string for unrecognized."""
    if not short:
        return ""
    m = re.match(r"^([A-G][#b]?)(m?)$", short.strip())
    if not m:
        return ""
    root, mode = m.group(1), m.group(2)
    return f"{root} {'minor' if mode else 'major'}"


def _read_mp3_tags(mp3: Path) -> dict[str, str]:
    """Pull TIT2/TPE1/TALB/TBPM/TKEY/TXXX:CAMELOT_KEY/TXXX:MUSICAL_KEY out of
    one MP3. Returns a dict of strings (empty for absent fields). Lenient —
    a corrupt header just means an empty dict, not a crash."""
    try:
        tags: ID3 | None = ID3(mp3)
    except (ID3NoHeaderError, Exception):
        tags = None

    def get_text(frame_id: str) -> str:
        if not tags or frame_id not in tags:
            return ""
        try:
            return str(tags[frame_id].text[0])
        except Exception:
            return ""

    return {
        "title": get_text("TIT2"),
        "artist": get_text("TPE1"),
        "album": get_text("TALB"),
        "bpm": get_text("TBPM"),
        "tkey": get_text("TKEY"),
        "camelot_txxx": get_text("TXXX:CAMELOT_KEY"),
        "musical_txxx": get_text("TXXX:MUSICAL_KEY"),
    }


def _scan_one_mp3(
    mp3: Path,
    *,
    read_bpm_key: bool,
    analyze_missing: bool,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Build a fresh-row payload from one MP3, applying the chosen scan flags.
    Caller merges this with any pre-existing CSV row (smart-fill semantics)."""
    raw = _read_mp3_tags(mp3)
    title = raw["title"] or mp3.stem
    artist = raw["artist"]
    album = raw["album"]

    bpm: int | str = ""
    camelot = ""
    key_full = ""
    if read_bpm_key:
        bpm_str = raw["bpm"]
        try:
            bpm = int(bpm_str) if bpm_str else ""
        except ValueError:
            bpm = ""
        # Camelot: prefer the dedicated TXXX frame TuneHoard writes,
        # fall back to TKEY if its value parses as Camelot.
        camelot = raw["camelot_txxx"]
        if not camelot:
            tkey = raw["tkey"]
            if _classify_key_format(tkey) == "camelot":
                camelot = tkey
        # Musical key: prefer dedicated TXXX, fall back to TKEY if musical.
        musical_short = raw["musical_txxx"]
        if not musical_short:
            tkey = raw["tkey"]
            if _classify_key_format(tkey) == "musical":
                musical_short = tkey
        key_full = _short_musical_to_full(musical_short)

    # If still missing BPM/Camelot AND user asked for analysis, run librosa.
    if analyze_missing and (bpm == "" or not camelot):
        try:
            result = analyze(
                mp3,
                bpm_min=settings["bpm_min"],
                bpm_max=settings["bpm_max"],
                duration=settings["analysis_seconds"],
            )
            if bpm == "":
                bpm = result.bpm
            if not camelot:
                camelot = result.camelot
            if not key_full:
                key_full = result.key_name
        except Exception:
            pass  # leave whatever we have; row still gets added

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "bpm": bpm,
        "camelot": camelot,
        "key_full": key_full,
    }


def _merge_scan_row(
    mp3: Path,
    fresh: dict[str, Any],
    existing: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    """Return (row, label) where label is 'added'|'kept'|'filled'. Smart-fill:
    only blank existing fields get populated from `fresh`."""
    if existing:
        merged = dict(existing)
        updated = False
        if not (existing.get("bpm") or "").strip() and fresh["bpm"] != "":
            merged["bpm"] = str(fresh["bpm"])
            updated = True
        if not (existing.get("camelot") or "").strip() and fresh["camelot"]:
            merged["camelot"] = fresh["camelot"]
            updated = True
        if not (existing.get("key") or "").strip() and fresh["key_full"]:
            merged["key"] = fresh["key_full"]
            updated = True
        return merged, ("filled" if updated else "kept")

    new_row = {
        "camelot": fresh["camelot"],
        "bpm": str(fresh["bpm"]) if fresh["bpm"] != "" else "",
        "artist": fresh["artist"],
        "title": fresh["title"],
        "album": fresh["album"],
        "key": fresh["key_full"],
        "source": "scanned",
        "file": mp3.name,
        "spotify_id": f"scan:{mp3.name}",
    }
    return new_row, "added"


def _run_scan_thread(scan_id: str, read_bpm_key: bool, analyze_missing: bool) -> None:
    """Worker thread for analyze_missing scans. Updates SCANS[scan_id] as it
    iterates; checks the cancelled flag between tracks. Broadcasts state on
    each track and on terminal status. The in-progress librosa call is NOT
    interruptible, so cancellation is best-effort (current track finishes)."""
    s = load_settings()
    out_dir = Path(s["library_dir"]) if s.get("library_dir") else None

    def _set(**kwargs: Any) -> None:
        with SCANS_LOCK:
            SCANS[scan_id].update(kwargs)

    def _cancelled() -> bool:
        with SCANS_LOCK:
            return bool(SCANS[scan_id].get("cancelled"))

    if out_dir is None or not out_dir.exists():
        _set(
            status="failed",
            finished_at=time.time(),
            error="library_dir not configured or missing",
        )
        _broadcast_scans()
        return

    try:
        existing_rows = _read_library()
        existing_by_file = {r.get("file"): r for r in existing_rows if r.get("file")}
        mp3s = sorted(out_dir.rglob("*.mp3"))
        total = len(mp3s)
        _set(status="running", total=total, progress=0)
        _broadcast_scans()

        rows: list[dict[str, Any]] = []
        added = 0
        kept = 0
        filled = 0
        last_broadcast = 0.0
        for idx, mp3 in enumerate(mp3s):
            if _cancelled():
                _set(status="cancelled", finished_at=time.time())
                _broadcast_scans()
                return

            existing = existing_by_file.get(mp3.name)
            if existing and not read_bpm_key:
                rows.append(existing)
                kept += 1
            else:
                fresh = _scan_one_mp3(
                    mp3,
                    read_bpm_key=read_bpm_key,
                    analyze_missing=analyze_missing,
                    settings=s,
                )
                merged, label = _merge_scan_row(mp3, fresh, existing)
                rows.append(merged)
                if label == "added":
                    added += 1
                elif label == "filled":
                    filled += 1
                else:
                    kept += 1

            with SCANS_LOCK:
                SCANS[scan_id]["progress"] = idx + 1
                SCANS[scan_id]["log"].append(f"{idx+1}/{total}: {mp3.name}")
                if len(SCANS[scan_id]["log"]) > 500:
                    SCANS[scan_id]["log"] = SCANS[scan_id]["log"][-300:]
            now = time.time()
            if now - last_broadcast >= 0.5:
                last_broadcast = now
                _broadcast_scans()

        # One more cancellation check before the CSV write.
        if _cancelled():
            _set(status="cancelled", finished_at=time.time())
            _broadcast_scans()
            return

        _write_library(rows)
        _set(
            status="done",
            finished_at=time.time(),
            total_rows=len(rows),
            added=added,
            kept=kept,
            filled=filled,
            csv=str(out_dir / "index.csv"),
            progress=total,
        )
        _broadcast_scans()
    except Exception as e:
        _set(status="failed", finished_at=time.time(), error=str(e))
        _broadcast_scans()


@app.post("/api/library/scan")
def api_scan_library(
    read_bpm_key: bool = False,
    analyze_missing: bool = False,
) -> dict[str, Any]:
    """Walk library_dir for MP3s and add rows for any not yet in index.csv.

    Default behavior is intentionally lightweight: only title / artist / album
    are read from each MP3's ID3 tags. BPM, Camelot, and key are LEFT BLANK.

    `read_bpm_key=true` — also pull TBPM / TKEY / TXXX:CAMELOT_KEY /
    TXXX:MUSICAL_KEY out of the file's existing tags. Fast, trusts whatever
    the previous tagger wrote.

    `analyze_missing=true` — implies read_bpm_key=true. RUNS IN BACKGROUND.
    Returns {scan_id, status: 'queued'} immediately; poll /api/library/scan/{id}
    or listen on /ws/jobs (scan_update messages). librosa analyze() takes
    ~3s/track so a 200-track folder is ~10 minutes; we don't want to block
    the HTTP request that long. Cancel with DELETE /api/library/scan/{id}.

    Merge-safe: existing rows are preserved; previously-unindexed MP3s get
    new rows; existing rows with blank BPM/key get filled in (never overwritten
    if they already have a value). Re-running scan repeatedly is idempotent.
    """
    # analyze_missing implies read_bpm_key — we always check existing tags
    # before falling back to librosa.
    if analyze_missing:
        read_bpm_key = True
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    out_dir = Path(s["library_dir"])
    if not out_dir.exists():
        raise HTTPException(404, f"folder doesn't exist: {out_dir}")

    # ── Background path: analyze_missing → kick off worker, return scan_id.
    if analyze_missing:
        scan_id = uuid.uuid4().hex[:9]
        with SCANS_LOCK:
            SCANS[scan_id] = {
                "id": scan_id,
                "status": "queued",
                "progress": 0,
                "total": 0,
                "log": [],
                "started_at": time.time(),
                "finished_at": None,
                "cancelled": False,
                "read_bpm_key": read_bpm_key,
                "analyze_missing": analyze_missing,
            }
        threading.Thread(
            target=_run_scan_thread,
            args=(scan_id, read_bpm_key, analyze_missing),
            daemon=True,
        ).start()
        _broadcast_scans()
        return {"scan_id": scan_id, "status": "queued"}

    # ── Synchronous path: tags-only or read_bpm_key (fast).
    existing_rows = _read_library()
    existing_by_file = {r.get("file"): r for r in existing_rows if r.get("file")}

    rows: list[dict[str, Any]] = []
    added = 0
    kept = 0
    filled = 0  # existing rows whose blank fields were populated from tags
    for mp3 in sorted(out_dir.rglob("*.mp3")):
        existing = existing_by_file.get(mp3.name)

        # Fast path: cancel branch with an existing row → keep as-is.
        if existing and not read_bpm_key:
            rows.append(existing)
            kept += 1
            continue

        fresh = _scan_one_mp3(
            mp3,
            read_bpm_key=read_bpm_key,
            analyze_missing=False,  # never run analysis on the sync path
            settings=s,
        )
        merged, label = _merge_scan_row(mp3, fresh, existing)
        rows.append(merged)
        if label == "added":
            added += 1
        elif label == "filled":
            filled += 1
        else:
            kept += 1

    _write_library(rows)
    return {
        "total": len(rows),
        "added": added,
        "kept": kept,
        "filled": filled,
        "csv": str(out_dir / "index.csv"),
    }


@app.get("/api/library/scan/{scan_id}")
def api_get_scan(scan_id: str) -> dict[str, Any]:
    """Poll a background scan's progress. Returns the full record (including
    any payload set by the worker on completion: total_rows, added, kept,
    filled, csv)."""
    with SCANS_LOCK:
        if scan_id not in SCANS:
            raise HTTPException(404, "scan not found")
        return _scan_snapshot(SCANS[scan_id])


@app.delete("/api/library/scan/{scan_id}")
def api_cancel_scan(scan_id: str) -> dict[str, Any]:
    """Best-effort cancel: sets a flag the worker checks between tracks. The
    in-progress track's librosa call finishes (no way to interrupt it cleanly).
    Status flips to 'cancelled' on the worker's next iteration."""
    with SCANS_LOCK:
        if scan_id not in SCANS:
            raise HTTPException(404, "scan not found")
        SCANS[scan_id]["cancelled"] = True
        # If still queued (worker hasn't started its loop), mark cancelled now.
        if SCANS[scan_id]["status"] == "queued":
            SCANS[scan_id]["status"] = "cancelled"
            SCANS[scan_id]["finished_at"] = time.time()
        snap = _scan_snapshot(SCANS[scan_id])
    _broadcast_scans()
    return snap


# ── Tracks ────────────────────────────────────────────────────────────
class TrackPatch(BaseModel):
    bpm: int | None = None
    camelot: str | None = None
    key: str | None = None  # full key name like "A minor"


@app.patch("/api/tracks/{track_id}")
def api_patch_track(track_id: str, patch: TrackPatch) -> dict[str, Any]:
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    rows = _read_library()
    row = _find_row(rows, track_id)
    if row is None:
        raise HTTPException(404, f"track {track_id} not found in CSV")
    if patch.bpm is not None:
        row["bpm"] = str(patch.bpm)
    if patch.camelot is not None:
        row["camelot"] = patch.camelot
    if patch.key is not None:
        row["key"] = patch.key
    out_dir = Path(s["library_dir"])
    _apply_row_to_disk(row, out_dir, key_format=s["key_format"])
    _write_library(rows)
    return _to_dashboard_track(row)


@app.delete("/api/tracks/{track_id}")
def api_delete_track(track_id: str) -> dict[str, Any]:
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    rows = _read_library()
    row = _find_row(rows, track_id)
    if row is None:
        raise HTTPException(404, f"track {track_id} not found in CSV")
    out_dir = Path(s["library_dir"])
    by_name, all_paths = _disk_index(out_dir)
    current = _find_disk_file(row, by_name, all_paths)
    if current is not None:
        try:
            current.unlink()
        except Exception:
            pass
    new_rows = [r for r in rows if r.get("spotify_id") != track_id]
    _write_library(new_rows)
    return {"deleted": track_id}


class TrackMove(BaseModel):
    to_bucket: str
    new_bpm: int | None = None  # if not provided, use bucket midpoint / half-time / double-time


@app.post("/api/tracks/{track_id}/move")
def api_move_track(track_id: str, move: TrackMove) -> dict[str, Any]:
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    rows = _read_library()
    row = _find_row(rows, track_id)
    if row is None:
        raise HTTPException(404, f"track {track_id} not found in CSV")
    old_bpm = int(row.get("bpm", 0) or 0)
    new_bpm = move.new_bpm
    if new_bpm is None:
        # Replicate the dashboard's auto-pick logic so server-side and client agree.
        from main import bpm_bucket as _bb
        target = move.to_bucket
        if target == "unknown-bpm":
            new_bpm = old_bpm
        else:
            try:
                lo, hi = (int(x) for x in target.split("-"))
            except Exception:
                raise HTTPException(400, f"invalid bucket name {target!r}")
            if old_bpm * 2 >= lo and old_bpm * 2 <= hi:
                new_bpm = old_bpm * 2
            elif round(old_bpm / 2) >= lo and round(old_bpm / 2) <= hi:
                new_bpm = round(old_bpm / 2)
            else:
                new_bpm = (lo + hi) // 2
    row["bpm"] = str(new_bpm)
    out_dir = Path(s["library_dir"])
    _apply_row_to_disk(row, out_dir, key_format=s["key_format"])
    _write_library(rows)
    return {"track": _to_dashboard_track(row), "old_bpm": old_bpm, "new_bpm": new_bpm}


@app.post("/api/tracks/{track_id}/reanalyze")
def api_reanalyze_track(track_id: str) -> dict[str, Any]:
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    rows = _read_library()
    row = _find_row(rows, track_id)
    if row is None:
        raise HTTPException(404, f"track {track_id} not found in CSV")
    out_dir = Path(s["library_dir"])
    by_name, all_paths = _disk_index(out_dir)
    current = _find_disk_file(row, by_name, all_paths)
    if current is None:
        raise HTTPException(404, "MP3 file not found on disk")
    result = analyze(
        current,
        bpm_min=s["bpm_min"],
        bpm_max=s["bpm_max"],
        duration=s["analysis_seconds"],
    )
    row["bpm"] = str(result.bpm)
    row["camelot"] = result.camelot
    row["key"] = result.key_name
    # Preserve existing TKEY format unless none can be detected.
    detected = _existing_tkey_format(current) or s["key_format"]
    _apply_row_to_disk(row, out_dir, key_format=detected)
    _write_library(rows)
    return _to_dashboard_track(row)


# ── Migrate keys ──────────────────────────────────────────────────────
class MigrateReq(BaseModel):
    key_format: str  # 'camelot' or 'musical'


@app.post("/api/migrate-keys")
def api_migrate_keys(req: MigrateReq) -> dict[str, Any]:
    if req.key_format not in ("camelot", "musical"):
        raise HTTPException(400, "key_format must be 'camelot' or 'musical'")
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    out_dir = Path(s["library_dir"])
    rows = _read_library()
    retagged = 0
    renamed = 0
    by_name, all_paths = _disk_index(out_dir)
    for row in rows:
        current = _find_disk_file(row, by_name, all_paths)
        if current is None:
            continue
        bpm_int = _row_bpm_int(row)
        if bpm_int is None or not row.get("camelot"):
            continue
        try:
            tag_file(
                current,
                title=row.get("title", "") or "",
                artist=row.get("artist", "") or "",
                album=row.get("album", "") or "",
                bpm=bpm_int,
                camelot=row["camelot"],
                key_name=row.get("key", "") or "",
                key_format=req.key_format,
            )
            retagged += 1
        except Exception:
            continue
        expected = _expected_filename(row, key_format=req.key_format) or current.name
        if current.name != expected:
            new_path = current.parent / expected
            try:
                safe_replace(current, new_path)
                renamed += 1
                row["file"] = expected
            except Exception:
                pass
    # Persist the chosen format as the new default.
    s["key_format"] = req.key_format
    save_settings(s)
    _write_library(rows)
    return {"retagged": retagged, "renamed": renamed}


# ── Jobs ──────────────────────────────────────────────────────────────
class JobReq(BaseModel):
    url: str
    sources: list[str] | None = None
    bucket_by_bpm: bool | None = None
    skip_existing: bool | None = None
    skip_analyze: bool | None = None
    key_format: str | None = None
    limit: int = 0


@app.post("/api/jobs")
def api_start_job(req: JobReq) -> dict[str, Any]:
    s = load_settings()
    # Behavioral flags: request body wins if explicit, otherwise pull from
    # persisted settings. Lets external callers override on a per-request basis
    # while the dashboard simply submits the saved settings unchanged.
    bucket = req.bucket_by_bpm if req.bucket_by_bpm is not None else s["bucket_by_bpm"]
    skip_e = req.skip_existing if req.skip_existing is not None else s["skip_existing"]
    skip_a = req.skip_analyze if req.skip_analyze is not None else s["skip_analyze"]
    job_id = _spawn_job(
        req.url,
        sources=req.sources or s["sources"],
        bucket_by_bpm=bucket,
        skip_existing=skip_e,
        skip_analyze=skip_a,
        key_format=req.key_format or s["key_format"],
        limit=req.limit,
    )
    # Broadcast immediately so connected WS clients see the new job appear
    # without waiting for the runner thread's first stdout line.
    _broadcast_jobs()
    with JOBS_LOCK:
        # Strip internal-only fields (e.g. _last_broadcast) before returning.
        return {k: v for k, v in JOBS[job_id].items() if not k.startswith("_")}


@app.get("/api/jobs")
def api_list_jobs() -> list[dict[str, Any]]:
    return _jobs_response()


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "job not found")
        return {k: v for k, v in JOBS[job_id].items() if not k.startswith("_")}


@app.get("/api/jobs/{job_id}/log")
def api_get_job_log(job_id: str, tail: int = 100) -> dict[str, Any]:
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "job not found")
        return {"id": job_id, "log": JOBS[job_id]["log"][-tail:]}


class BrowseReq(BaseModel):
    mode: str = "directory"   # 'directory' or 'file'
    title: str | None = None
    initial: str | None = None


@app.post("/api/browse")
def api_browse(req: BrowseReq) -> dict[str, Any]:
    """Pop a native OS folder/file picker on the user's machine. Server runs
    locally, so the dialog appears in front of their browser. Cancellation
    returns path=null. tkinter blocks the worker thread; uvicorn dispatches
    sync handlers off the event loop, so other endpoints stay responsive."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        raise HTTPException(500, f"tkinter unavailable: {e}")

    title = req.title or ("Choose folder" if req.mode == "directory" else "Choose file")
    initial = req.initial or None

    root = tk.Tk()
    root.withdraw()
    try:
        # On Windows the dialog can hide behind the browser without this hint.
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        if req.mode == "file":
            chosen = filedialog.askopenfilename(title=title, initialdir=initial)
        else:
            chosen = filedialog.askdirectory(title=title, initialdir=initial)
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    return {"path": chosen or None}


def _open_in_file_manager(path: Path) -> None:
    """Open a file or folder in the OS-native file manager.
    Windows: Explorer. macOS: Finder. Linux: xdg-open (whichever DE)."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


@app.post("/api/tracks/{track_id}/open-folder")
def api_open_track_folder(track_id: str) -> dict[str, Any]:
    """Open the folder containing a track's MP3 in the OS file manager."""
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    rows = _read_library()
    row = _find_row(rows, track_id)
    if row is None:
        raise HTTPException(404, "track not found")
    out_dir = Path(s["library_dir"])
    by_name, all_paths = _disk_index(out_dir)
    path = _find_disk_file(row, by_name, all_paths)
    if path is None:
        raise HTTPException(404, "MP3 file not found on disk")
    folder = path.parent
    try:
        _open_in_file_manager(folder)
    except Exception as e:
        raise HTTPException(500, f"failed to open: {e}")
    return {"opened": str(folder)}


@app.post("/api/buckets/{bucket_name}/open-folder")
def api_open_bucket_folder(bucket_name: str) -> dict[str, Any]:
    """Open a BPM-bucket folder in the OS file manager."""
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(400, "library_dir not configured")
    target = Path(s["library_dir"]) / bucket_name
    if not target.exists():
        raise HTTPException(404, f"bucket folder '{bucket_name}' does not exist")
    try:
        _open_in_file_manager(target)
    except Exception as e:
        raise HTTPException(500, f"failed to open: {e}")
    return {"opened": str(target)}


@app.post("/api/spotify/authorize")
def api_spotify_authorize() -> dict[str, Any]:
    """Trigger spotipy's OAuth flow. Opens the user's browser to
    accounts.spotify.com, blocks until they complete the flow (or until
    spotipy's local callback server gives up). Token cached to .spotify_cache.

    Wrapped in a worker thread with a 5-minute join timeout so a user who
    abandons the browser flow doesn't pin a request handler indefinitely.
    """
    s = load_settings()
    if not s["spotify_client_id"] or not s["spotify_client_secret"]:
        raise HTTPException(400, "Set Spotify Client ID and Secret in Settings first")

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _do_auth() -> None:
        try:
            sp = _spotify_client(s["spotify_client_id"], s["spotify_client_secret"])
            me = sp.current_user()
            name = me.get("display_name") or me.get("id") or "unknown"
            result["user"] = name
        except BaseException as e:  # capture everything; surface to caller
            error["e"] = e

    t = threading.Thread(target=_do_auth, daemon=True)
    t.start()
    t.join(timeout=300)
    if t.is_alive():
        raise HTTPException(504, "authorization timed out — try again from Settings")
    if "e" in error:
        e = error["e"]
        raise HTTPException(500, f"authorize failed: {type(e).__name__}: {e}")
    return {"status": "authorized", "user": result.get("user", "unknown")}


@app.get("/api/spotify/status")
def api_spotify_status() -> dict[str, Any]:
    """Lightweight check: are creds set + is a token cached. Doesn't
    re-validate the token (that would force a network round-trip).

    Also surfaces scope mismatches: if the cached token's `scope` field
    doesn't include `user-library-read` (added in 2026 for the Liked Songs
    picker), set scope_mismatch=true. The frontend nudges the user to re-
    auth. We do NOT auto-invalidate the cache — user decides when to refresh.

    cache_age_days = days since the cache file's mtime (rounded down). Gives
    the UI a hint for "this auth is months old, refresh might fail" copy.
    """
    s = load_settings()
    cache_path = PROJECT_ROOT / ".spotify_cache"
    has_creds = bool(s["spotify_client_id"] and s["spotify_client_secret"])
    has_cache = cache_path.exists()
    authorized = has_creds and has_cache

    out: dict[str, Any] = {
        "configured": has_creds,
        "authorized": authorized,
        "scope_mismatch": None,
        "cache_age_days": None,
    }
    if not authorized:
        return out

    # Cache age: floor(now - mtime, days). 0 for a freshly-minted cache.
    try:
        mtime = cache_path.stat().st_mtime
        age_secs = max(0.0, time.time() - mtime)
        out["cache_age_days"] = int(age_secs // 86400)
    except Exception:
        out["cache_age_days"] = None

    # Scope detection: parse the cache JSON. Spotipy writes a "scope" field
    # as a space-separated string (sometimes also as a list — handle both).
    out["scope_mismatch"] = False  # default to "looks fine" once cached
    try:
        cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
        scope_field = cache_data.get("scope") or ""
        if isinstance(scope_field, list):
            scopes = {str(x).strip() for x in scope_field}
        else:
            scopes = {p for p in str(scope_field).split() if p}
        if "user-library-read" not in scopes:
            out["scope_mismatch"] = True
    except Exception:
        # Malformed cache: don't claim mismatch, but don't pretend it's fine
        # either — leaving the field None signals "couldn't determine".
        out["scope_mismatch"] = None

    return out


# ── Spotify: Liked Songs as a virtual playlist ────────────────────────
@app.get("/api/spotify/liked")
def api_spotify_liked() -> dict[str, Any]:
    """Return the authenticated user's Liked Songs metadata as a playlist-
    shaped object. The dashboard treats this as a virtual playlist; the
    download flow will recognize the `spotify:liked` URL sentinel separately.
    Only fetches the count (limit=1) — we don't need the full track list here."""
    s = load_settings()
    cache_path = PROJECT_ROOT / ".spotify_cache"
    if not s["spotify_client_id"] or not s["spotify_client_secret"] or not cache_path.exists():
        raise HTTPException(401, "spotify not authorized")
    try:
        sp = _spotify_client(s["spotify_client_id"], s["spotify_client_secret"])
        result = sp.current_user_saved_tracks(limit=1)
        total = int(result.get("total", 0)) if result else 0
    except spotipy.SpotifyException as e:
        # 401/403 → unauthorized. Anything else → upstream API failure.
        status = getattr(e, "http_status", 0)
        if status in (401, 403):
            raise HTTPException(401, "spotify not authorized")
        raise HTTPException(502, f"spotify api error: {e}")
    except Exception as e:
        raise HTTPException(502, f"spotify api error: {e}")
    return {
        "name": "Liked Songs",
        "url": "spotify:liked",
        "track_count": total,
    }


# ── Spotify: list user playlists ──────────────────────────────────────
@app.get("/api/spotify/playlists")
def api_spotify_playlists() -> dict[str, Any]:
    """Return up to 200 of the authenticated user's playlists (saved + created).
    Paginated 50 at a time (Spotify max page size). `owner` is the literal
    'you' if the caller owns the playlist, else the owner's display name / id."""
    s = load_settings()
    cache_path = PROJECT_ROOT / ".spotify_cache"
    if not s["spotify_client_id"] or not s["spotify_client_secret"] or not cache_path.exists():
        raise HTTPException(401, "spotify not authorized")
    try:
        sp = _spotify_client(s["spotify_client_id"], s["spotify_client_secret"])
        # Cache the user id so we don't call current_user() once per playlist.
        me_id = sp.current_user().get("id", "")
        playlists: list[dict[str, Any]] = []
        for page in range(4):  # 4 pages × 50 = 200 cap
            result = sp.current_user_playlists(limit=50, offset=page * 50)
            items = (result or {}).get("items") or []
            if not items:
                break
            for p in items:
                if not p:
                    continue
                name = p.get("name")
                if not name:
                    continue  # skip null / empty names
                owner = p.get("owner") or {}
                owner_id = owner.get("id", "")
                if owner_id and owner_id == me_id:
                    owner_label = "you"
                else:
                    owner_label = owner.get("display_name") or owner_id or "unknown"
                playlists.append({
                    "name": name,
                    "url": (p.get("external_urls") or {}).get("spotify", ""),
                    "track_count": int(((p.get("tracks") or {}).get("total")) or 0),
                    "owner": owner_label,
                })
            if not result.get("next"):
                break
    except spotipy.SpotifyException as e:
        status = getattr(e, "http_status", 0)
        if status in (401, 403):
            raise HTTPException(401, "spotify not authorized")
        raise HTTPException(502, f"spotify api error: {e}")
    except Exception as e:
        raise HTTPException(502, f"spotify api error: {e}")
    return {"playlists": playlists}


# ── Auto-update version check ─────────────────────────────────────────
def _compare_semver(current: str, latest: str) -> bool:
    """Return True if `latest > current` under naive 3-part-int semver.
    Non-numeric / malformed parts coerce to 0 rather than raising — keeps
    the endpoint from 500-ing on weird upstream tag names."""
    def _parts(v: str) -> tuple[int, int, int]:
        bits = v.split(".")
        out = [0, 0, 0]
        for i in range(min(3, len(bits))):
            try:
                out[i] = int(bits[i])
            except (ValueError, TypeError):
                out[i] = 0
        return out[0], out[1], out[2]
    return _parts(latest) > _parts(current)


@app.get("/api/version")
def api_get_version() -> dict[str, Any]:
    """Compare running version against the latest GitHub release. Cached for
    60s so a refresh-happy dashboard doesn't hammer the GitHub API. Never
    raises — every failure mode (timeout, network, parse, rate-limit) returns
    a structured payload with `checked: false` so the dashboard can show a
    quiet "couldn't check" state instead of console errors."""
    now = time.time()
    cached = _VERSION_CACHE.get("value")
    if cached and (now - _VERSION_CACHE.get("fetched_at", 0)) < _VERSION_CACHE_TTL:
        return cached

    payload: dict[str, Any] = {
        "current": __version__,
        "latest": None,
        "update_available": False,
        "release_url": None,
        "checked": False,
        "error": None,
        "last_checked_at": None,
    }
    url = "https://api.github.com/repos/HighWalls/TuneHoard/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"TuneHoard/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").strip()
        latest = tag[1:] if tag.lower().startswith("v") else tag
        if latest:
            payload["latest"] = latest
            payload["update_available"] = _compare_semver(__version__, latest)
            payload["release_url"] = data.get("html_url") or None
            payload["checked"] = True
    except urllib.error.HTTPError as e:
        payload["error"] = "rate limited" if e.code == 403 else "network error"
    except (TimeoutError, urllib.error.URLError) as e:
        # urllib raises URLError(reason=...) on socket timeout too.
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            payload["error"] = "timeout"
        else:
            payload["error"] = "network error"
    except (json.JSONDecodeError, ValueError):
        payload["error"] = "network error"
    except Exception:
        payload["error"] = "network error"

    # Stamp last_checked_at on every cache write — successful or failed —
    # so the dashboard can show "checked 2 minutes ago" even when GitHub
    # rate-limited us. UTC, ISO 8601 with a trailing 'Z'.
    payload["last_checked_at"] = (
        _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    _VERSION_CACHE["value"] = payload
    _VERSION_CACHE["fetched_at"] = now
    return payload


# ── Audio preview streaming (with HTTP Range support) ─────────────────
def _parse_range_header(header: str, file_size: int) -> tuple[int, int] | None:
    """Parse an HTTP Range header like 'bytes=START-END' (END optional).
    Returns (start, end) inclusive, or None if the header is malformed.
    Clamps end to file_size-1 so a too-greedy client doesn't 416."""
    if not header or not header.lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].strip()
    # We only honour the first byte-range — multipart ranges aren't worth it
    # for an HTML5 <audio> seek use case.
    spec = spec.split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    start_str, end_str = spec.split("-", 1)
    try:
        if start_str == "" and end_str:
            # Suffix range: last N bytes.
            n = int(end_str)
            if n <= 0:
                return None
            start = max(0, file_size - n)
            end = file_size - 1
        else:
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= file_size:
        return None
    end = min(end, file_size - 1)
    return start, end


@app.get("/api/audio/{track_id:path}")
def api_get_audio(track_id: str, request: Request) -> Response:
    """Stream a track's MP3 with HTTP Range support so HTML5 <audio> can seek.
    `track_id:path` lets the route match IDs containing `:` (e.g. `scan:foo.mp3`)
    and `/`; FastAPI URL-decodes the value before we see it, so we just hand
    it straight to the existing CSV lookup helper."""
    s = load_settings()
    if not s["library_dir"]:
        raise HTTPException(404, "library_dir not configured")
    rows = _read_library()
    row = _find_row(rows, track_id)
    if row is None:
        raise HTTPException(404, "track not found")
    out_dir = Path(s["library_dir"])
    by_name, all_paths = _disk_index(out_dir)
    path = _find_disk_file(row, by_name, all_paths)
    if path is None or not path.exists():
        raise HTTPException(404, "MP3 file not found on disk")

    file_size = path.stat().st_size
    chunk_size = 64 * 1024
    range_header = request.headers.get("range") or request.headers.get("Range") or ""
    rng = _parse_range_header(range_header, file_size) if range_header else None

    if rng is None:
        # Full-file response. Still advertise Accept-Ranges so the player
        # knows it CAN seek — a follow-up Range request will get 206'd.
        def full_iter() -> Any:
            with path.open("rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        }
        return StreamingResponse(
            full_iter(),
            status_code=200,
            headers=headers,
            media_type="audio/mpeg",
        )

    start, end = rng
    length = end - start + 1

    def range_iter() -> Any:
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
    }
    return StreamingResponse(
        range_iter(),
        status_code=206,
        headers=headers,
        media_type="audio/mpeg",
    )


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process and all its descendants. yt-dlp spawns ffmpeg children,
    so a plain proc.terminate() leaves them as orphans on Windows."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        # /T = recursive (the whole tree), /F = force (no graceful close).
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        # POSIX: try terminate first, fall back to kill.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass


@app.delete("/api/jobs/{job_id}")
def api_cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a running job. Kills the subprocess + its child tree (yt-dlp +
    ffmpeg) and removes the job from the active list. The runner thread
    finishes asynchronously and skips its 'failed' / 'done' status update
    because we've already marked the job 'cancelled'."""
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "job not found")
        job = JOBS[job_id]
        proc = PROCS.get(job_id)
        already_done = job["status"] in ("done", "failed", "cancelled")
        job["status"] = "cancelled"
        job["finished_at"] = time.time()
        job["current"] = ""

    if proc is not None and not already_done:
        _kill_process_tree(proc)

    # Remove from the live JOBS dict so the dashboard's poll stops showing it.
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
        PROCS.pop(job_id, None)

    # Push the post-cancel snapshot so connected WS clients see the job vanish
    # without waiting on the next poll.
    _broadcast_jobs()
    return {"id": job_id, "status": "cancelled"}


@app.websocket("/ws/jobs")
async def ws_jobs(ws: WebSocket) -> None:
    """Push-only stream of JOBS state. Client receives a 'snapshot' on connect,
    then 'update' messages whenever JOBS state changes (job started, progress,
    terminal state, cancellation). The server captures the running event loop
    on first connect so that `_broadcast_jobs()` — called from the runner
    thread — can schedule sends back onto this loop."""
    global _WS_LOOP
    # Host-header guard for WS upgrades. HTTP middleware doesn't fire on the
    # websocket route, so we replicate the DNS-rebinding check here.
    host_hdr = (ws.headers.get("host") or "").strip().lower()
    allowed_hosts = {
        f"127.0.0.1:{_CHOSEN_PORT}",
        f"localhost:{_CHOSEN_PORT}",
        "127.0.0.1:8765",
        "localhost:8765",
    }
    if host_hdr not in allowed_hosts:
        await ws.close(code=1008)  # 1008 = policy violation
        return
    await ws.accept()
    # Capture the loop from this async context. `get_running_loop()` is the
    # robust call on Python 3.10+ — `get_event_loop()` is deprecated outside
    # an async context. Stashing it once is fine: uvicorn runs a single loop
    # for the lifetime of the server.
    if _WS_LOOP is None:
        _WS_LOOP = asyncio.get_running_loop()
    with _WS_LOCK:
        _WS_JOBS_CONNECTIONS.add(ws)
    try:
        await ws.send_json({"type": "snapshot", "jobs": _jobs_response()})
        # We don't expect any client→server messages, but receive_text() is the
        # idiomatic way to await a disconnect: it raises WebSocketDisconnect
        # when the peer closes. Without this loop we'd return immediately and
        # the connection would close.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # Swallow any other transport-level error; the finally block will clean up.
        pass
    finally:
        with _WS_LOCK:
            _WS_JOBS_CONNECTIONS.discard(ws)


# ── Static dashboard ──────────────────────────────────────────────────
@app.get("/")
def root() -> FileResponse:
    if not DASHBOARD_HTML.exists():
        raise HTTPException(500, f"dashboard not found at {DASHBOARD_HTML}")
    return FileResponse(DASHBOARD_HTML)


if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")


# ──────────────────────────────────────────────────────────────────────
# Launcher
# ──────────────────────────────────────────────────────────────────────
def find_free_port(host: str = "127.0.0.1", start: int = 8765, count: int = 5) -> int | None:
    """Probe `count` consecutive ports starting at `start` for one we can bind.
    Returns the first free port, or None if every probe fails. Used at startup
    so a stale prior instance on 8765 doesn't crash boot with a confusing
    OSError 10048; we transparently fall forward to 8766..8770 instead."""
    for port in range(start, start + count):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
        except OSError:
            continue
        return port
    return None


def main() -> None:
    import uvicorn

    global _CHOSEN_PORT
    host = "127.0.0.1"
    # Honor TUNEHOARD_PORT if set (for users who pin a specific port). Otherwise
    # walk 8765..8770 trying to bind.
    env_port = os.environ.get("TUNEHOARD_PORT")
    if env_port:
        try:
            start_port = int(env_port)
        except ValueError:
            start_port = 8765
    else:
        start_port = 8765

    port = find_free_port(host=host, start=start_port, count=5)
    if port is None:
        end = start_port + 4
        print(
            f"  ERROR: ports {start_port}-{end} all in use; "
            f"close another instance and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    _CHOSEN_PORT = port
    url = f"http://{host}:{port}/"

    print()
    print(f"  TuneHoard server starting on {url}")
    if port != start_port:
        print(f"  (port {start_port} was in use; falling back to {port})")
    print(f"  Press Ctrl+C to stop.")
    print()

    if os.environ.get("TUNEHOARD_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
