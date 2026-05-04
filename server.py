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

import csv
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────────────
# Reuse existing CLI logic. main.py defines several helpers we share.
# ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer import analyze
from camelot import musical_key_short
from main import (  # type: ignore[no-redef]
    CSV_FIELDS,
    _bpm_sort_key,
    _expected_filename,
    _existing_tkey_format,
    _find_disk_file,
    _key_prefix,
    _row_bpm_int,
    bpm_bucket,
    safe_filename,
    safe_replace,
)
from tagger import tag_file


# ──────────────────────────────────────────────────────────────────────
# Settings — persisted JSON next to the project.
# ──────────────────────────────────────────────────────────────────────
SETTINGS_FILE = PROJECT_ROOT / ".tunehoard_settings.json"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard" / "tunehoard"
DASHBOARD_HTML = DASHBOARD_DIR / "tunehoard.html"

DEFAULT_SETTINGS: dict[str, Any] = {
    "output_dir": str(PROJECT_ROOT / "downloads"),
    "library_dir": "",  # path to a single playlist directory containing index.csv
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "sources": ["youtube", "soundcloud"],
    "key_format": "camelot",
    "bucket_by_bpm": True,
    "bpm_min": 85,
    "bpm_max": 200,
    "analysis_seconds": 120,
    "ffmpeg_path": "",
}


def load_settings() -> dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(s: dict[str, Any]) -> None:
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
    """Atomic write — same shape as main.py's CSV write block."""
    csv_path = _csv_path()
    if csv_path is None:
        return
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
    bpm = int(row.get("bpm", 0) or 0)
    src = (row.get("source", "") or "").lower()
    src_short = {"youtube": "YT", "soundcloud": "SC", "spotify": "SP"}.get(src, "")
    return {
        "id": row.get("spotify_id", "") or "",
        "cam": cam,
        "key": musical,
        "bpm": bpm,
        "artist": row.get("artist", "") or "",
        "title": row.get("title", "") or "",
        "source": src_short,
        "bucket": bpm_bucket(bpm),
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
JOBS_LOCK = threading.Lock()


def _spawn_job(
    url: str,
    *,
    sources: list[str],
    bucket_by_bpm: bool,
    skip_existing: bool,
    key_format: str,
    limit: int,
) -> str:
    job_id = f"j{int(time.time() * 1000)}"
    s = load_settings()
    args = [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        url,
        "--out", s["output_dir"],
        "--sources", ",".join(sources or s["sources"]),
        "--key-format", key_format,
    ]
    if bucket_by_bpm:
        args.append("--bucket-by-bpm")
    if skip_existing:
        args.append("--skip-existing")
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
        "started_at": time.time(),
        "finished_at": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    def runner() -> None:
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
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                with JOBS_LOCK:
                    job["log"].append(line)
                    if len(job["log"]) > 2000:
                        job["log"] = job["log"][-1500:]
                    # crude progress parsing
                    if "tracks)" in line and "→" in line:
                        # "  → 'name' (184 tracks)"
                        try:
                            n = int(line.split("(")[1].split()[0])
                            job["total"] = n
                        except Exception:
                            pass
                    elif "→ " in line and "skipped" not in line:
                        job["current"] = line.split("→ ", 1)[1].strip()
                    elif "skipped" in line:
                        job["failed"] += 1
                    elif "Done." in line:
                        job["status"] = "done"
            proc.wait()
            with JOBS_LOCK:
                job["finished_at"] = time.time()
                if proc.returncode != 0 and job["status"] != "done":
                    job["status"] = "failed"
                elif job["status"] == "running":
                    job["status"] = "done"
        except Exception as e:
            with JOBS_LOCK:
                job["status"] = "failed"
                job["log"].append(f"!! server error: {e}")

    threading.Thread(target=runner, daemon=True).start()
    return job_id


# ──────────────────────────────────────────────────────────────────────
# FastAPI app + endpoints.
# ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="TuneHoard", version="0.1.0")


# ── Settings ──────────────────────────────────────────────────────────
class SettingsPatch(BaseModel):
    output_dir: str | None = None
    library_dir: str | None = None
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    sources: list[str] | None = None
    key_format: str | None = None
    bucket_by_bpm: bool | None = None
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


# ── Library ───────────────────────────────────────────────────────────
@app.get("/api/library")
def api_get_library() -> list[dict[str, Any]]:
    return [_to_dashboard_track(r) for r in _read_library()]


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
    result = analyze(current)
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
    bucket_by_bpm: bool = True
    skip_existing: bool = True
    key_format: str | None = None
    limit: int = 0


@app.post("/api/jobs")
def api_start_job(req: JobReq) -> dict[str, Any]:
    s = load_settings()
    job_id = _spawn_job(
        req.url,
        sources=req.sources or s["sources"],
        bucket_by_bpm=req.bucket_by_bpm,
        skip_existing=req.skip_existing,
        key_format=req.key_format or s["key_format"],
        limit=req.limit,
    )
    with JOBS_LOCK:
        return dict(JOBS[job_id])


@app.get("/api/jobs")
def api_list_jobs() -> list[dict[str, Any]]:
    with JOBS_LOCK:
        # Return a shallow snapshot — drop the full log to keep it small;
        # /api/jobs/{id}/log streams the full log.
        out = []
        for j in JOBS.values():
            out.append({**j, "log": j["log"][-3:]})  # tail only
        return out


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "job not found")
        return dict(JOBS[job_id])


@app.get("/api/jobs/{job_id}/log")
def api_get_job_log(job_id: str, tail: int = 100) -> dict[str, Any]:
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "job not found")
        return {"id": job_id, "log": JOBS[job_id]["log"][-tail:]}


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
def main() -> None:
    import uvicorn

    host = "127.0.0.1"
    port = int(os.environ.get("TUNEHOARD_PORT", "8765"))
    url = f"http://{host}:{port}/"

    print()
    print(f"  TuneHoard server starting on {url}")
    print(f"  Press Ctrl+C to stop.")
    print()

    if os.environ.get("TUNEHOARD_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
