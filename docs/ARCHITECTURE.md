# TuneHoard — Architecture

Pipeline details and module contracts. Read `CLAUDE.md` first.

## Pipeline

```
URL (Spotify / YouTube / SoundCloud — playlist OR single track)
      │
      ▼  main.main() dispatcher routes by URL pattern:
      │     - youtube.com / youtu.be → ytdlp_loader.get_ytdlp_tracks(url, "yt")
      │     - soundcloud.com         → ytdlp_loader.get_ytdlp_tracks(url, "sc")
      │     - open.spotify.com/track/X → spotify_client.get_track(url, ...)
      │     - open.spotify.com/playlist/X → spotify_client.get_playlist_tracks(url, ...)
      │
      │  Each loader returns (folder_name, list[Track]).
      │  Single-track URLs return folder_name = "singles" so they accumulate.
      │
list[Track]  (id, title, artists, album, duration_ms, isrc, source_url)
      │
      ▼  [if --skip-existing: load index.csv + reconstruct from ID3 tags]
      │
      ▼  for each remaining track:
      │    if track.source_url (YT/SC entries):
      │      downloader.download_url(source_url, tmp_dir)
      │    else (Spotify entries):
      │      for source in sources ("youtube,soundcloud" by default):
      │        downloader.download_track(query, tmp_dir, source)
      │        if hit → break
      │      else → append to failures list
tmp_dir/<id>.mp3
      │
      ▼  analyzer.analyze(mp3_path, bpm_min=85, bpm_max=200, duration=120)
Analysis(bpm: int, key_name: str, camelot: str)
      │
      ▼  tagger.tag_file(...)
ID3 tags written in place
      │
      ▼  main.safe_replace()  (retries on Windows AV lock)
out_dir/<camelot> - <bpm> - <artist> - <title>.mp3
      │
      ▼  after loop ends
out_dir/index.csv       (atomic write: tmp + replace, sorted by camelot, bpm)
out_dir/failures.txt    (if any track matched no source)
```

`out_dir = <args.out>/<sanitized_folder_name>/` where `folder_name` is the playlist title for playlists or the literal string `"singles"` for single-track URLs (so all standalone tracks pool together regardless of source). The per-track `tmp_dir` is `out_dir/_tmp/` and is deleted at the end.

## Module contracts

### `spotify_client.py`

- `Track` — dataclass shared across loaders. `source_url` is set for YT/SC entries (direct download) and `None` for Spotify entries (search-based download). `search_query` property builds `"primary_artist - title"`; falls back to bare title if `artists` is empty.
- `get_playlist_tracks(url, cid, secret) -> (playlist_name, list[Track])` — paginates `playlist_items` via `sp.next()`. Skips items where the per-entry payload is None or missing an ID (Spotify returns these for unavailable/local tracks). Reads from the `item` key (post-2025 schema) with a fallback to legacy `track` key.
- `get_track(url, cid, secret) -> ("singles", [Track])` — fetches a single track via `sp.track()`. Returns the literal folder name `"singles"` so any single-track download lands in `<out>/singles/` regardless of source.
- `get_liked_songs(cid, secret) -> ("Liked Songs", list[Track])` — paginates `current_user_saved_tracks(limit=50, offset=N)` until exhausted. Reads `entry["track"] or entry["item"]` for forward-compat (saved-tracks still uses `track`; the rename to `item` happened only on `playlist_items`). Skips null entries (unavailable tracks / podcast episodes). Used by the `spotify:liked` URL sentinel — `main.py`'s dispatcher recognizes that string before any regex and routes here.
- Uses `spotipy.SpotifyOAuth` with scopes `playlist-read-private playlist-read-collaborative user-library-read`. The `user-library-read` was appended in 2026 for the picker / liked-songs flow; pre-existing tokens trigger a one-time re-auth on the next saved-tracks call. First run opens a browser for user authorization, caches token to `.spotify_cache`. Why not Client Credentials? See `docs/GOTCHAS.md` — Spotify started returning 401 on `playlist_items` for Client Credentials in 2025.

### `ytdlp_loader.py`

- `get_ytdlp_tracks(url, id_prefix) -> (folder_name, list[Track])` — handles both playlists and single videos/tracks. Detects "single" vs "playlist" by whether the yt-dlp `extract_info` response contains an `entries` key. Single tracks always return folder name `"singles"`. `id_prefix` is `"yt"` or `"sc"` and namespaces the `spotify_id` column (`yt:VIDEO_ID`, `sc:TRACK_ID`) so cross-source IDs can't collide.
- `_entry_to_track(entry, id_prefix)` — converts a yt-dlp entry to a `Track`. Best-effort parses `"Artist - Title"` from the video title (`_parse_artist_title`), falling back to the uploader/channel as artist. Strips common YouTube noise like `(Official Video)`, `[HD]`, `[Lyric]` from titles via `_NOISE_RE`.
- `is_youtube_url(url)` / `is_soundcloud_url(url)` — regex matchers used by `main.py` to dispatch.
- The `source_url` on each Track prefers `webpage_url` (canonical watch URL, stable) over `url` (sometimes a signed media URL that expires). Critical for single-video extracts, where `url` is the streaming endpoint.

### `downloader.py`

Two entry points sharing yt-dlp configuration via `_base_opts(out_dir, ffmpeg_location="")`:

- `download_url(url, out_dir, ffmpeg_location="") -> Path | None` — direct download from a known video/track URL. Used for YouTube/SoundCloud playlist entries where `Track.source_url` is set. No search.
- `download_track(query, out_dir, source, ffmpeg_location="") -> Path | None` — search-based download. Used for Spotify entries (no direct URL available). Uses yt-dlp's `default_search` with `ytsearch1:` or `scsearch1:` (first result only). If you need more matches, change `1` → `N` and filter in Python.
- `ffmpeg_location` (when non-empty) is passed straight through as yt-dlp's `ffmpeg_location` opt — it accepts either a directory containing `ffmpeg(.exe)` or the full executable path. Empty string falls back to PATH lookup, the historical behavior. Threaded from `--ffmpeg-location` (CLI) / `settings["ffmpeg_path"]` (dashboard).
- Both: postprocessor pins output to mp3 @ 320k. Changing to FLAC/M4A: swap `preferredcodec`. Rekordbox handles all three; mp3 is the most portable.
- Output template `%(id)s.%(ext)s` — uses the platform's track ID so collisions are impossible within a source. Final rename happens in `main.py`.

### `analyzer.py`

- `analyze(mp3_path, *, bpm_min=85, bpm_max=200, duration=120) -> Analysis` — loads mono @ 22050 Hz, first `duration` seconds only. 120s is enough for stable BPM/chroma and keeps per-track analysis under ~3s; lowering it speeds up scans, raising it helps tracks with long atonal intros.
- `_detect_bpm(y, sr, bpm_min, bpm_max)`: `librosa.beat.beat_track` returns tempo in BPM. Half/double-time correction clamps to `[bpm_min, bpm_max]` by doubling/halving. Default bounds `[85, 200]` are tuned for DJ libraries including D&B; bounds must satisfy `bpm_min * 2 ≤ bpm_max` (else the loop oscillates — see `docs/GOTCHAS.md`).
- `_detect_key`: mean chroma over the track, then correlate against 12 rotations of Krumhansl-Schmuckler major + minor profiles. Highest correlation wins mode and root.
- Algorithm ceiling is ~80–85% accurate. Common failures: tracks with strong V chord emphasis mis-classified as V instead of I, modal/atonal tracks, very bass-heavy tracks where chroma is dominated by harmonics.

### `camelot.py`

- Two dicts: `_MAJOR` and `_MINOR`, keyed by root note name (`"C"`, `"C#"`, ..., `"B"`). Values are Camelot strings (`"8B"`, `"8A"`, etc.).
- `to_camelot(root, mode)` — `mode` is `"major"` or `"minor"`.
- The Camelot wheel is a DJ convention: adjacent numbers are a perfect fifth apart, same number + letter swap is the relative major/minor. Mixing is "safe" within ±1 number or across the A/B swap.

### `tagger.py`

- `tag_file(mp3_path, ..., key_format="camelot")` — writes `TIT2`, `TPE1`, `TALB`, `TBPM`, `TKEY`, `TXXX:CAMELOT_KEY`, `TXXX:MUSICAL_KEY`, and `COMM`.
- **Both key formats are always written.** `TXXX:CAMELOT_KEY` and `TXXX:MUSICAL_KEY` are populated unconditionally. `key_format` only chooses which value also goes into the canonical `TKEY` frame (`"camelot"` → `8A`, `"musical"` → `Am`). The COMM frame carries `f"{camelot} | {bpm} BPM | {key_name}"` regardless. This guarantees the file is portable across DJ software no matter the choice — every tool can read its preferred format from *some* frame.
- `camelot.musical_key_short(key_name)` is the conversion: `"A minor" -> "Am"`, `"C major" -> "C"`, `"C# minor" -> "C#m"`. Format expected by ID3v2.3 spec for `TKEY`.
- Saves as ID3v2.3 (`v2_version=3`) — Rekordbox supports both v2.3 and v2.4 but v2.3 has wider compatibility.
- Creates new `ID3()` if the file has no header (fresh MP3 from yt-dlp often does). Catches `ID3NoHeaderError` only — other errors should propagate.

### `main.py`

- `--key-format {camelot,musical}` — primary format for **new downloads**. Process_track always uses it for both TKEY and the filename prefix. For existing files (reanalyze + bucket-sync), the format is detected per-file and preserved unless `--migrate-keys` is also set.
- `--migrate-keys` — opt-in. Forces every existing file's TKEY tag and filename prefix to `--key-format` on the next sync/reanalyze. Without it, existing files keep whatever format they were originally tagged with even when `--key-format` differs.
- `--bpm-min` / `--bpm-max` / `--analysis-seconds` — passed straight to `analyzer.analyze(..., bpm_min, bpm_max, duration)`. Threaded through both `process_track` (new downloads) and `reanalyze_rows` (re-runs over existing files), so `--reanalyze --bpm-min 70` will rewrite a library with the tighter bound applied.
- `--ffmpeg-location PATH` — passed through `process_track` to `download_url` / `download_track` (and on into yt-dlp). Empty default = PATH lookup (the historical behavior).
- `_classify_key_format(s)` / `_existing_tkey_format(mp3_path)` — read a file's current TKEY and classify it as `'camelot'` (matches `\d+[AB]`) or `'musical'` (matches `[A-G][#b]?m?`) or `None`. Used to decide whether to preserve or migrate.
- `_key_prefix(camelot, key_name, key_format)` — single source of truth for the filename's leading chunk. Used in process_track, reanalyze_rows, and `_expected_filename`.
- Bucket-sync's rename step treats *both* `_expected_filename(row, "camelot")` and `_expected_filename(row, "musical")` as valid names for a given row. It only renames if the current name matches neither (e.g., wrong BPM/artist after a CSV edit) or if `--migrate-keys` was passed. So toggling `--key-format` on a follow-up run does not cascade-rename existing files.
- `safe_filename(s)` — strips characters illegal on Windows (`<>:"/\|?*` + control chars), truncates to 120 chars. Don't shorten further; collisions with long track names become likely.
- `safe_replace(src, dst)` — `os.replace()` with retries. Windows antivirus/indexer briefly locks freshly-written MP3s and throws `PermissionError`; retrying after 0.5s resolves it.
- `process_track(track, out_dir, sources)` — iterates `sources` (list), first hit wins. Returns a CSV row dict with the successful `source` recorded, or `None` if no source matched. Failed analysis still produces a (less useful) row — the file is kept but untagged and named `"Artist - Title.mp3"`.
- `reconstruct_row_from_disk(track, out_dir)` — pattern-matches `* - {artist} - {title}.mp3` in `out_dir`, reads `TBPM` + `TKEY` back out of ID3 tags. Used by `--skip-existing` to recover after a crash wiped `index.csv`. The `key` and `source` columns aren't reconstructible from tags (not stored), so they're left blank.
- CSV write is atomic: sort first (in memory), write to `index.csv.tmp`, then `replace()` the real file. A crash mid-write can't wipe a valid index.
- CSV is sorted by `(camelot, bpm)`. Camelot sort is lexicographic on the string (`"10A" < "1A"` — this is *wrong* musically but stable enough for DJ prep; if it matters, parse to `(int, letter)` before sorting). `_bpm_sort_key()` coerces BPM to int because rows loaded from CSV have it as str while fresh rows have it as int.
- At startup, `sys.stdout` and `sys.stderr` are reconfigured to UTF-8 (`errors="replace"`). Without this, Windows' default cp1252 codepage crashes on most track titles (kanji, emoji, symbols) and even on the `→` arrow used in progress output.
- `bpm_bucket(bpm)` returns the subfolder name for a given BPM. Anchor band `115-125` is 11 BPM wide (both bounds inclusive); everything else aligns to that anchor in 10-wide bands. The asymmetry is deliberate — DJs typically treat 115-125 as one "house/deep tempo" pocket.
- When `--bucket-by-bpm` is set, `process_track` writes new files into `out_dir/<bucket>/...`, and after the main loop `main()` runs a sync pass over *every* row (existing + new) that moves mismatched files into their correct bucket and `rmdir`s any emptied folders. This makes the flag idempotent: toggle it on a flat library and the next run reorganizes everything without re-downloading.

## Extension points

### Adding a new download source

yt-dlp supports many search prefixes. In `downloader.py`:

```python
search_prefix = {
    "youtube": "ytsearch1:",
    "soundcloud": "scsearch1:",
    "bandcamp": "bcsearch1:",   # add here
}[source]
```

Then add the choice to `argparse` in `main.py`.

### Adding a new DJ software target

Rekordbox reads standard ID3v2 (`TKEY`, `TBPM`). Other targets:

- **Serato** — uses proprietary `GEOB` frames (`Serato Analysis`, `Serato Markers2`, etc.). Standard `TKEY` and `TBPM` still show up but beat grids and cue points need the GEOB blobs. Writing those is hard; Serato will re-analyze on import and populate them itself, so don't bother unless you want to port cue points too.
- **Traktor** — reads `TKEY` but expects musical notation (`"Am"`, `"C"`) by default. Its "Key Display" setting can be switched to Camelot in prefs, but if you don't control the end user, write musical notation. Add a `--key-format {camelot,musical}` flag.
- **rekordbox XML** — for batch import including cue points/hot cues, generate a `rekordbox.xml` file alongside the mp3s. The schema is documented on Pioneer's site. Out of scope currently; tags are enough for BPM/key sorting.

### Adding BPM/key source fallback

The architecture assumes one analysis per track. To add a fast-path + fallback (e.g., GetSongBPM → local), wrap `analyze()` in a function that tries the fast source first and falls back to `analyze()` on miss or low confidence. Don't thread remote API calls through `analyzer.py` itself — keep it pure-local.

### "Liked Songs" support

OAuth already has the user token, so adding Liked Songs is a small change: accept a sentinel value (e.g., `--source liked` or pass `"liked"` as the playlist arg) and call `sp.current_user_saved_tracks()` instead of `sp.playlist_items()`. Add the `user-library-read` scope to the OAuth scopes string.

## Dashboard server (`server.py`)

FastAPI app that exposes the CLI's behavior over HTTP/JSON, plus serves the static dashboard HTML. Run with `python server.py` — listens on `127.0.0.1:8765`, auto-opens the browser, no Docker / build step.

### Why FastAPI subprocess (not in-process) for jobs

Download jobs spawn `main.py` as a child process via `subprocess.Popen`, parse stdout for progress lines, and serve the parsed status via `/api/jobs`. We do this instead of importing `main.main()` and calling it in-process because:

- librosa + numba are imported lazily once per process and hold significant memory + GPU/CPU resources. Running multiple analysis pipelines concurrently in-process is fragile.
- `main.py` writes to `index.csv` atomically and can be restarted/killed without leaving the server in a half-state.
- Stdout-line parsing is a stable contract: `main.py`'s existing log lines (`→ Artist - Title`, `Done. N/M tracks. ...`) double as a progress protocol. No need for a side-channel.

### Endpoint surface

All endpoints live under `/api/`. Server-side helpers are imported from `main.py` directly — `_expected_filename`, `_find_disk_file`, `_existing_tkey_format`, `_key_prefix`, `bpm_bucket`, `safe_replace`, etc. — so dashboard mutations behave identically to a CLI run with `--bucket-by-bpm --skip-existing`.

| Method + Path | Body / Response |
|---|---|
| `GET /api/settings` | Returns settings dict. `spotify_client_secret` is masked (`********`) so the client doesn't echo it back as a literal value. |
| `PATCH /api/settings` | Partial update; client must omit (not echo) the masked secret. Persists to `.tunehoard_settings.json`. The deprecated `output_dir` key is silently dropped by `save_settings`. |
| `GET /api/preview?url=` | Lightweight URL → title resolution. YT/SC use `extract_flat`; Spotify uses spotipy. Used by the dashboard's URL bar to replace the mock detection labels with real titles. Returns `{kind, label, name?, track_count?}`. Doesn't download. |
| `GET /api/library` | Reads `<library_dir>/index.csv`, returns `[{id, cam, key, bpm, artist, title, source, bucket, file}]`. `key` field is the *short* musical form (`"Am"`), not the full `"A minor"`. Tracks with empty BPM get `bucket: "unknown-bpm"` (not a numeric range). |
| `POST /api/library/scan?read_bpm_key=&analyze_missing=` | Walks `library_dir` for MP3s and merges new rows into `index.csv`. Always reads title/artist/album. `read_bpm_key=true` also pulls TBPM/TKEY/TXXX. `analyze_missing=true` (implies `read_bpm_key=true`) additionally runs `analyzer.analyze()` on tracks lacking BPM/Camelot. **The `analyze_missing=true` path is now async** — returns `{scan_id, status: "queued"}` immediately and runs in a daemon thread with state pushed via `/ws/jobs` as `{type: "scan_update", scan: {...}}`. The synchronous fast-paths (no flags, or `read_bpm_key=true` only) still return `{total, added, kept, filled, csv}` directly. Smart-fill: existing rows preserved, blank fields backfilled. |
| `GET /api/library/scan/{scan_id}` | Returns the current scan record `{id, status, progress, total, log, started_at, finished_at}`. 404 on unknown id. |
| `DELETE /api/library/scan/{scan_id}` | Cancels a running scan. Sets a `cancelled` flag the worker checks between tracks; in-flight `librosa.analyze()` finishes (it's not interruptible). Best-effort. |
| `PATCH /api/tracks/{id}` | Body `{bpm?, camelot?, key?}`. Updates the row, retags ID3, renames + re-buckets the file using existing `main.py` helpers. Atomic CSV write afterwards. |
| `DELETE /api/tracks/{id}` | Removes the MP3 from disk + the row from the CSV. |
| `POST /api/tracks/{id}/move` | Body `{to_bucket, new_bpm?}`. Drag-drop endpoint. If `new_bpm` is omitted, the server applies the same auto-pick rules as the dashboard's drag handler (half-time → double, double-time → half, else bucket midpoint). |
| `POST /api/tracks/{id}/reanalyze` | Re-runs `analyzer.analyze()` on the file with `bpm_min` / `bpm_max` / `duration` from settings, updates row, retags. Preserves the file's existing TKEY format (Camelot vs musical) — does not migrate. |
| `GET /api/failures` | Reads `<library_dir>/failures.txt` and returns `[{artist, title, spotify_url}]`. Skips blank lines and `#` comments; lenient on malformed rows (puts the whole left-of-tab into `title` rather than crashing). Returns `[]` with HTTP 200 when `library_dir` is unset or `failures.txt` is missing — never 404s, so the dashboard can poll unconditionally. |
| `POST /api/tracks/{id}/open-folder` | Resolves the track's MP3 path on disk, opens the parent folder in the OS file manager via `os.startfile` / `xdg-open` / `open`. |
| `POST /api/buckets/{name}/open-folder` | Opens `<library_dir>/<name>/` in the OS file manager. |
| `POST /api/migrate-keys` | Body `{key_format}`. Bulk: rewrites every file's TKEY + filename prefix to the requested format. Persists `key_format` as the new default in settings. |
| `POST /api/spotify/authorize` | Triggers the spotipy OAuth flow synchronously. Opens `accounts.spotify.com` in the user's browser, blocks until they authorize, then verifies via `sp.current_user()`. The verify call runs in a worker thread with a 5-minute `t.join` timeout — a stale OAuth tab returns HTTP 504 instead of hanging the request indefinitely. Token cached to `.spotify_cache`. Returns `{status: "authorized", user}`. |
| `GET /api/spotify/status` | `{configured, authorized, scope_mismatch, cache_age_days}`. `scope_mismatch: true` when `.spotify_cache`'s scope string is missing `user-library-read` (added 2026 for the Liked Songs picker — pre-existing tokens lack it). `cache_age_days` = floor days since `.spotify_cache` mtime; `null` when unauthenticated or cache malformed. Doesn't validate the cached token over the network. |
| `POST /api/browse` | Body `{mode: "directory"\|"file", title?, initial?}`. Pops a native tkinter folder/file picker. Used by the dashboard's `[Browse...]` buttons. tkinter dialog runs in the uvicorn worker thread (sync handler) — non-blocking for other endpoints. Returns `{path}` or `{path: null}` on cancel. |
| `POST /api/jobs` | Body `{url, sources?, bucket_by_bpm?, skip_existing?, key_format?, limit?}`. `url` may be `https://...` for any supported platform OR the literal `spotify:liked` sentinel for the user's Liked Songs. Spawns `main.py` as a subprocess with `--out=<library_dir.parent> --into=<library_dir.name>` so all downloads merge into the user's currently-viewed library. Also threads `--bpm-min` / `--bpm-max` / `--analysis-seconds` from settings, plus `--ffmpeg-location` if `settings["ffmpeg_path"]` is non-empty. Popen handle stored in `PROCS` dict for cancel. Returns the new job's id + initial state. Broadcasts the new job over `/ws/jobs` immediately. |
| `GET /api/version` | Returns `{current, latest, update_available, release_url, checked, error, last_checked_at}`. Queries the GitHub releases API for `HighWalls/TuneHoard` with a 5s timeout and a 60s in-process cache. Never 500s — every exception path collapses to `{checked: false, error: "..."}` so the dashboard banner can poll unconditionally. `last_checked_at` is an ISO 8601 UTC timestamp (with `Z` suffix) stamped on every cache write; the dashboard uses it for a "Update check: N ago — <status>" line in Settings. The `current` field comes from `server.__version__` at the top of `server.py` and is also threaded into `FastAPI(title=..., version=__version__)` so the OpenAPI doc stays in sync. |
| `GET /api/audio/{track_id:path}` | Streams the MP3 with HTTP Range support so HTML5 `<audio>` can seek without downloading the whole file. 64 KB chunked generator, 206 Partial Content for `Range: bytes=N-M` / `bytes=N-` / `bytes=-N`, 200 + `Accept-Ranges: bytes` for full-file. Uses `:path` converter so IDs like `scan:foo.mp3` (containing `:`) match. Reuses the existing `_find_disk_file` lookup chain. 404 on missing track. |
| `GET /api/spotify/liked` | `{name: "Liked Songs", url: "spotify:liked", track_count}`. Uses `current_user_saved_tracks(limit=1)["total"]` — doesn't enumerate. 401 if not authorized (creds + `.spotify_cache` both required, matching `/api/spotify/status`'s definition). |
| `GET /api/spotify/playlists` | `{playlists: [{name, url, track_count, owner}]}`. Paginates `current_user_playlists(limit=50)` 4× = up to 200 entries. Caches `current_user()["id"]` once per request to mark `owner: "you"`. Skips null/empty playlist names (Spotify returns these for some legacy items). 401 if not authorized; 502 on other API errors. |
| `WebSocket /ws/jobs` | Server-pushed job + scan state. On accept: sends `{type: "snapshot", jobs: [...]}` immediately so the client renders current state without waiting. Subsequent messages: `{type: "update", jobs: [...]}` for download-job state changes, `{type: "scan_update", scan: {...}}` for the async analyze-missing scan worker. Both throttled to ~2 Hz, unthrottled on terminal states. The runner threads schedule sends onto the captured uvicorn event loop via `asyncio.run_coroutine_threadsafe`. Client reconnects with exponential backoff (1s → 30s, reset to 1s on any successful message). Falls back to 10-second HTTP polling if the socket drops persistently. WS handshake also performs the host header check (rejects with code 1008 on mismatch). |
| `POST /api/failures/dismiss` | Body `{spotify_url}`. Removes the matching row from `<library_dir>/failures.txt` with the `.tmp` + `replace()` atomic pattern. Deletes the file entirely if only header lines remain (keeps the dashboard's hidden-when-empty UX accurate). Returns the updated failures list. 404 if URL not present or file missing. |
| `GET /api/jobs` | All jobs. `log` is truncated to last 3 lines per entry (full log via `/api/jobs/{id}/log`). |
| `GET /api/jobs/{id}` / `GET /api/jobs/{id}/log` | Full single-job state / full log tail (default 100 lines, override with `?tail=N`). |
| `DELETE /api/jobs/{id}` | Cancel a running job. Marks status `cancelled`, calls `_kill_process_tree` (Windows: `taskkill /F /T /PID` so yt-dlp + ffmpeg children don't orphan; POSIX: `terminate` then `kill` after 2s), removes from JOBS. Runner thread guard prevents the `proc.wait()` exit from overwriting the cancelled status with `failed`. |
| `GET /` | Serves `dashboard/tunehoard/tunehoard.html`. |
| `GET /static/*` | Anything else under `dashboard/tunehoard/` (e.g., user-uploaded MP3s for failed-track manual recovery). |

### Server-side helpers

- `_open_in_file_manager(path)` — Windows: `os.startfile`. macOS: `subprocess open`. Linux: `subprocess xdg-open`.
- `_kill_process_tree(proc)` — Windows: `taskkill /F /T /PID <pid>`. POSIX: terminate→wait(2s)→kill.
- `_short_musical_to_full(short)` — `"Am"` → `"A minor"`, `"C#"` → `"C# major"`. Inverse of `camelot.musical_key_short`. Used by the scan endpoint to populate the CSV's full-name `key` column from `TXXX:MUSICAL_KEY` short-form values.
- `_classify_key_format(s)` — regex match on TKEY values: `\d+[AB]` → `"camelot"`, `[A-G][#b]?m?` → `"musical"`, else None. Imported from `main.py`.
- `_apply_row_to_disk(row, out_dir, key_format)` — Single-track equivalent of the CLI's bucket-sync pass. Re-tags + renames + re-buckets one MP3 from a row dict. Used by every track-mutation endpoint so dashboard edits behave identically to a `--bucket-by-bpm` CLI run.
- `_to_dashboard_track(row)` — CSV row → JSON shape. Treats blank/0 BPM as `bucket="unknown-bpm"` rather than letting `bpm_bucket(0)` produce a nonsense range like `"-5-4"`.
- `_jobs_response()` — produces the dashboard payload (under `JOBS_LOCK`, with internal `_*`-prefixed keys stripped). Single source of truth shared between `GET /api/jobs` and the WS snapshot/update messages.
- `_broadcast_jobs()` / `_broadcast_scans()` — best-effort push to all connected `/ws/jobs` clients (jobs as `{type: "update"}`, scans as `{type: "scan_update"}`). Schedule `ws.send_json(...)` onto the captured uvicorn event loop via `asyncio.run_coroutine_threadsafe`, so they're safe to call from any worker thread. Silently no-op if no loop captured (e.g. before the first WS connect, or in tests) or if no clients are connected. Connection set + lock are module-level (`_WS_JOBS_CONNECTIONS`, `_WS_LOCK`); `_WS_LOOP` is captured on first WS accept.
- `find_free_port(host, start=8765, count=5)` — probes ports via `socket.bind()`. Returns the first free port or `None`. Used in `main()` startup; the chosen port becomes module-global `_CHOSEN_PORT` for the host-validation middleware to close over. Override the start with `TUNEHOARD_PORT=N` env var.
- `_host_header_guard` (FastAPI middleware) — for mutating methods (`POST/PUT/PATCH/DELETE`), checks the `Host:` header against an allowlist of `{127.0.0.1, localhost}:{_CHOSEN_PORT, 8765}`. Returns 403 on mismatch. Mitigates DNS rebinding without imposing CSRF token complexity that's overkill for localhost-only use.
- `_run_scan_thread(scan_id, ...)` — daemon-thread worker for the async `analyze_missing=true` scan path. Updates `SCANS[scan_id]` under `SCANS_LOCK`, broadcasts state changes via `_broadcast_scans()`, checks the `cancelled` flag between tracks (in-flight `librosa.analyze()` is NOT interruptible — the cancel finishes the current track then bails).

### Settings model

`load_settings()` merges `DEFAULT_SETTINGS` with the contents of `.tunehoard_settings.json` (gitignored). `save_settings()` writes atomically (`.json.tmp` then `replace()`). Every endpoint that mutates state calls `load_settings()` fresh — there's no in-memory settings cache. Cheap and avoids the "stale config" class of bugs.

`library_dir` is the **single source of truth** for the user's "where do my files live" choice — it serves as both the download destination (via `--out=<parent> --into=<name>` when spawning `main.py`) and the library tree's source. The earlier `output_dir` / `library_dir` split was confusing and got merged in May 2026; old configs are migrated automatically: if `library_dir` is empty and `output_dir` is set, the value is folded in and `output_dir` is dropped. `save_settings` strips `output_dir` on every write so it can never come back.

A multi-playlist sidebar that walked `library_dir.parent` for sibling CSVs was prototyped and removed. The schema (`parent/<name>/index.csv`) supports it, but the UX wasn't worth the additional surface area — switching playlists by editing `library_dir` in Settings is rare and explicit enough.

## Dashboard (`dashboard/tunehoard/tunehoard.html`)

Single self-contained HTML file (no build step, no bundler). Pure black-on-white aesthetic by default, palette switcher in the top-left toggles between three presets + a curated random palette pool. All CSS uses `--ink` / `--paper` custom properties; switching palettes is a one-line variable swap.

### Mock-on-boot pattern

The HTML ships with realistic mock data for `LIB` and `JOBS` so it renders standalone (developing the dashboard without the server is straightforward — just open the file in a browser). When `server.py` is running, the API integration block at the bottom of the script:

1. Defines a global `window.API` with helpers for every endpoint.
2. Calls `API.refreshLibrary()` and `API.refreshJobs()` immediately on load — these mutate the existing `LIB` and `JOBS` arrays in place via `array.length = 0; array.push(...newItems)`.
3. Polls `/api/jobs` every 2 seconds and `/api/library` every 30 seconds.
4. Refreshes both on `window.focus`.

The user briefly sees mock data on first paint (~50 ms) before real data takes over. Don't try to "fix" this by deleting the mock — empty render flicker is worse UX than a brief flash of placeholder rows. If you need the dashboard to start truly empty, set `LIB = []` and `JOBS = []` at the top of the script.

### Action wiring

Each interactive control fires a backend call after the local UI update succeeds (optimistic-update pattern). On backend failure, a `toast()` shows the error and the local state is rolled back where reasonable:

- **URL bar** → `GET /api/preview?url=` (debounced 450 ms with `AbortController` to ignore stale responses while user keeps typing). Also accepts drag-drop: `dragover` prevents default to allow the drop, `drop` reads `text/uri-list` (preferred — most browsers populate it for link drags) and falls back to `text/plain`, then re-fires preview detection.
- **Download button** → first `GET /api/settings`; if `library_dir` is empty, `showModal({ok: 'Open Settings', cancel: 'Cancel'})` → either opens Settings or aborts. Happy path → `POST /api/jobs` with the toggle-pill values (bucket / fallback / skip-existing).
- **Job `[X]` cancel** → `DELETE /api/jobs/{id}`. Optimistic local removal, server kills the subprocess tree.
- **Drag-drop track row** → `POST /api/tracks/{id}/move`. On failure, the track is restored to its source bucket and the BPM reverted.
- **Bucket-move bulk button** → inline `.ctx-menu`-styled bucket picker; click a bucket → loops `moveTrack` over selected ids.
- **Edit form Save / Re-analyze / Delete** → `PATCH` / `POST .../reanalyze` / `DELETE` per track.
- **Edit form "Open in folder"** → `POST /api/tracks/{id}/open-folder` (spawns Explorer / Finder).
- **Bulk Re-analyze / Delete** → loop the per-track endpoints sequentially with progress toasts every 5 tracks.
- **Bucket header right-click → "Open folder in Explorer"** → `POST /api/buckets/{name}/open-folder`.
- **Bucket header right-click → "Re-analyze all in bucket"** → loops `reanalyzeTrack` over the bucket's tracks with `showModal` confirmation.
- **Empty-library `[ Scan folder ]` button** → `showModal` 3-button dialog (Abort / Read tags only / Analyze missing) → `POST /api/library/scan` with the chosen flags.
- **Settings → Output Folder field blur** → `PATCH /api/settings` with `library_dir`. Triggers `refreshLibrary()` afterwards so the tree reflects the new folder.
- **Settings → Output Folder `[Browse...]`** → `POST /api/browse` (tkinter directory picker), then writes the result into the input and fires its blur handler.
- **Settings → ffmpeg path `[Browse...]`** → `POST /api/browse` (file picker).
- **Settings → Spotify Client ID / Secret blur** → `PATCH /api/settings`. The masked secret (`********`) is skipped on PATCH so the client doesn't echo it back.
- **Settings → `[ Authorize ]`** → `POST /api/spotify/authorize` (blocks while the user completes OAuth in a new browser tab).
- **Settings → status pill** → updated from `GET /api/spotify/status` on page load AND after Spotify field blurs.
- **Key-format radio** → `PATCH /api/settings`. Soft preference: existing files are NOT migrated.
- **Settings → `[ Migrate existing library ]`** → `showModal` confirm → `POST /api/migrate-keys` with the chosen format.
- **Source list `[↑]/[↓]`** → reorders DOM, then `PATCH /api/settings` with the new `sources` array.
- **Welcome modal `[ Continue >> ]`** → if the user picked a folder, `PATCH /api/settings` with `library_dir` and refresh the library.

### Initial settings sync

A standalone IIFE at module load (`syncSettingsToDom`) calls `GET /api/settings` once and applies all values to the DOM (output dir field, ffmpeg path, key-format radio, source order, Spotify status pill). This used to live inside a `window.showSettings = async function...` wrapper, but `function` declarations create a lexical binding that's captured by `addEventListener('click', showSettings)` — reassigning `window.showSettings` later doesn't change what the click handler points to. The wrapper never fired, so saved settings never reapplied to the DOM after page reload. The IIFE pattern bypasses the entire problem: it runs at script-load time, regardless of which view is currently visible.

### Library tree rendering

`renderLib()` skips empty buckets entirely (`if(all.length === 0) continue`). So if your library has tracks only at 95-104 BPM, you see one bucket header in the tree — not all 13 possible bands with `0 tracks` next to each. The order stays sorted (low→high, `unknown-bpm` last) for whichever buckets do render.

### Selection vs editing are mutually exclusive

Plain click on a track row → opens that row's inline edit form, closes any other open form, **clears multi-selection**. Shift/Ctrl-click → toggles selection only, **closes any open edit form**. This means the per-row edit form and the bulk-action bar are never on screen at the same time; users don't see redundant Re-analyze / Delete buttons fighting for attention. Implemented in the `tree.addEventListener('click', ...)` handler.

A module-level `lastSelectedId` tracks the anchor for shift-click range select. Plain click and ctrl-click update the anchor; pure shift-range click does *not*, so subsequent shift-clicks keep extending from the same anchor (matches Finder/Explorer behavior). Range membership is computed in DOM order — a `querySelectorAll('.track-row')` walk picks up the bucket-grouped + within-bucket-sorted order the user actually sees, not the raw `LIB` array order.

### Source filter

The filter bar's `Scanned` radio matches tracks where `t.source === ""`. The backend's `_to_dashboard_track` produces an empty `source` for any value not in the `{spotify, youtube, soundcloud}` abbreviation map — the scan endpoint writes `"scanned"` into the CSV's `source` column, which `_to_dashboard_track` then collapses to `""`. So `Scanned` is exactly the catch-all for "not from a download job." The other three radios match the abbreviated `"SP" / "YT" / "SC"` strings.

### Failures panel

`<div id="failures-box">` lives between the jobs and library boxes. Hidden via `.hidden` class when the failure count is 0; otherwise shows `~~ FAILED TRACKS (N) ~~` and a list of `× artist — title  [open Spotify]` rows (literal U+00D7 cross, em dash, `.tlink` for the URL). Polls `GET /api/failures` on the same 30s timer that refreshes the library, plus on `window.focus`. Real-time accuracy isn't critical here — a job's failures show up once you tab back to the dashboard.

### Audio preview

A single global `<audio id="audio-player" preload="none">` element sits near the bottom of body. Each `.track-row` has a `[▶]` (`.track-play`) span at the front; click sets `audio.src = '/api/audio/' + encodeURIComponent(id)` and starts playback. Toggling between play/pause on the same row swaps the icon (`▶` ↔ `❚❚`); clicking another row's button stops the previous one. `audio.addEventListener('ended' / 'error', ...)` clears state and re-renders so the icon flips back. The button's handler `e.stopPropagation()`s so it doesn't trigger row-edit/select.

The playing row also renders an inline scrub bar (click-to-seek; positioned via `getBoundingClientRect` + `e.clientX`, no drag). A pinned now-playing bar sits at the bottom of the viewport (`position:fixed; bottom:0`) with `▶ artist — title  [stop] [vol]`; clicking the title scrolls the playing row into view. The volume slider persists to `localStorage.tunehoard_volume` (default 0.7). Keyboard shortcuts (skip when typing in `INPUT/TEXTAREA/SELECT/contentEditable`): Space toggles play/pause, Left/Right seek ±5s, Esc stops + clears `audio.src` (Space won't resume after Esc — the row's ▶ button must be clicked again).

### Failures panel — retry / dismiss / timestamp

Each row now shows `× artist — title  [open Spotify] [retry] [×]`. `[retry]` calls `POST /api/jobs` with `{url: spotify_url}` to re-fire that single track (the failure stays — next failures.txt update reflects success or repeat-fail). `[×]` shows a `showModal` confirm then calls `POST /api/failures/dismiss`; on success the local list is replaced with the response. The header shows "last updated N ago" — pulled from the `Last-Modified` HTTP header when set; FastAPI doesn't set it for JSON returns, so the dashboard falls back to recording fetch-time client-side, meaning the timestamp is "since last poll" rather than "since file mtime."

### Job log viewer

Each running/completed job has an `[Expand log]` link. Expanding fetches `GET /api/jobs/{id}/log?tail=500` lazily and caches it in a module-level `LOG_CACHE = {[jobId]: {lines, fetchedAt}}` for the page's session — never invalidates, so users wanting live updates collapse + re-expand. The expanded pane is a scrollable 300px-max-height monospace block with three filter radios `( ) all  ( ) warnings  ( ) errors` (per-job state in `LOG_FILTERS[jobId]`); warnings match `/warn(ing)?/i`, errors match `/(error|failed|traceback)/i` or lines starting with `!`. A `[ copy ]` link uses `navigator.clipboard.writeText(...)` with a hidden-textarea + `execCommand('copy')` fallback for older browsers. Copy always grabs the unfiltered cached log — copying a filtered subset is rarely what users want.

### Settings — stale-data nudges

Three signals from `/api/spotify/status` and `/api/version` surface in the Settings page:

- **Scope-mismatch nudge** (`#s-scope-warn`): visible when `s.authorized && s.scope_mismatch === true`. Reads "Permissions out of date — re-authorize to enable Liked Songs picker"; the inline tlink fires `s-auth.click()` directly.
- **Cache-age hint** (`#s-cache-age`): muted "(authorized N days ago)" text when `s.cache_age_days >= 7`. Hidden otherwise — fresh auth doesn't need annotation.
- **Update-check status line** (`#s-update-check`): shows "Update check: 2 min ago — current version" / "...— update available (v0.2)" / "Update check failed: <error>" / "Update check: not yet checked." Driven by `/api/version`'s `last_checked_at` + `error` fields. Makes the silent-on-error behavior of the auto-update banner transparent for users who care.

### Spotify picker

`[ Browse Spotify ]` button next to `[ Download >> ]`, hidden until `/api/spotify/status` returns `authorized: true` (synced on initial load + `window.focus`). Click → `Promise.all(getLikedSongs(), getMyPlaylists())` → modal with title `~~ BROWSE SPOTIFY ~~` and a `.sp-pick-list` (max-height 60vh, scrollable). First row `★ Liked Songs (N tracks)`, then `≫ Playlist Name (N tracks) — owner` for each user playlist. Clicking a row writes the URL into `#url`, closes the modal, fires `setStatus()` for preview detection. 401 on either endpoint → close + `showModal({title: 'Not authorized', ...})`. `setStatus()` short-circuits on `spotify:`-prefix URLs and renders `Spotify: Liked Songs` client-side without hitting `/api/preview`.

### Auto-update banner

`<div id="update-banner">` at the very top of body, hidden by default. On page load, after the initial `refreshLibrary` / `refreshJobs` calls, fetches `/api/version`. If `update_available && checked && latest !== localStorage.tunehoard_dismissed_version`, shows the banner with the version string + `[ open release ]` (tlink) + `[ × ]` dismiss. Dismiss writes `r.latest` to localStorage so we don't re-show for the same version. Silent on `error || !checked` — the user doesn't care about failed update checks.

### WebSocket job streaming

`connectJobsWS()` lives inside the integration IIFE. Builds `ws://${host}/ws/jobs` (`wss://` if HTTPS), parses `{type: 'snapshot' | 'update', jobs: [...]}` messages, replaces the local `JOBS` array via length=0/push, calls `renderJobs()`. `onclose` / `onerror` schedule reconnect after 5 seconds. The polling fallback (`setInterval(refreshJobs, 10000)`) runs unconditionally as a safety net — WS does the real work, polling keeps the UI alive if the socket drops permanently. A `data-ws` attribute on `#jobs-label` renders `●` (connected) or `○` (polling-only) via a `::before` pseudo-element so `renderJobs()`'s `label.textContent` rewrites don't wipe it.

## Distribution

Two non-source-tree artifacts ship binary-friendly versions of the app:

### `tunehoard.spec` (PyInstaller)

Onefile spec, entry `server.py`, output `dist/TuneHoard.exe`. Bundles the entire `dashboard/` tree at the same relative path so `FileResponse('dashboard/tunehoard/tunehoard.html')` and the `/static` mount both resolve. Hidden imports cover the librosa lazy-load chain (`numba`, `llvmlite`, `soxr`, `pooch`) and the full `uvicorn.protocols.{http,websockets,lifespan,loops}.*` tree (uvicorn picks protocols by string at runtime via `importlib`; static analysis misses these). `copy_metadata` for `pydantic` + `fastapi` + `uvicorn` so frozen builds don't trip `PackageNotFoundError` on dist-info reads. `console=True` for alpha-friendly tracebacks; flip to `False` once the build is reliably stable.

PyInstaller is *not* in `requirements.txt` — the dev workflow installs it separately, and CI installs it just-in-time.

### `.github/workflows/build.yml`

Triggers on `v*.*.*` tag pushes + `workflow_dispatch`. Three jobs:

- `build-windows` (windows-latest): checkout → setup-python 3.13 → `pip install -r requirements.txt && pip install pyinstaller` → `pyinstaller tunehoard.spec` → rename to `TuneHoard-windows-x64.exe` → upload artifact.
- `build-macos` (macos-latest): same up to the build, then renames + zips to `TuneHoard-macos-x64.zip` (zip preserves +x; bare binaries lose it on most download paths).
- `release` (ubuntu-latest, gated on `startsWith(github.ref, 'refs/tags/v')`, `permissions: contents: write`): downloads both artifacts, creates / updates a GitHub Release for the tag via `softprops/action-gh-release@v2` with `generate_release_notes: true`.

Linux is intentionally skipped — librosa+pyinstaller can be brittle there and the dev env is Win11. Add it back when there's demand and bandwidth for testing.

### `app_native.py` (pywebview)

Optional native-window entry point. Imports `server`, runs `uvicorn` on a daemon thread, polls `127.0.0.1:8765` until reachable (5s timeout via `socket.create_connection`), then opens a 1280×800 `webview.create_window(...)` pointed at the dashboard. On window-close: `uvicorn.Server.should_exit = True` triggers graceful shutdown. The daemon thread guarantees process exit on main-thread return regardless.

Self-flagged cleverness: monkey-patches `webbrowser.open` to a no-op *before* importing `server`, so `server.py`'s startup `webbrowser.open(...)` doesn't fight the native window with a duplicate browser tab. Per-process patch, doesn't affect anything else.

The pywebview wrapper is independent of PyInstaller — you can run `python app_native.py` directly from source, OR build with `pyinstaller` against either entry point. The current spec uses `server.py` as entry; switch to `app_native.py` if you want the bundled binary to launch in a native window by default.

### Custom modal: `showModal({title, body, ok, cancel?, third?})`

Replaces native `confirm()` / `alert()` so the popup matches the dashboard's NFO aesthetic (reuses the `.overlay` checkered backdrop and `.welcome` box class from the welcome screen). Returns `Promise<true | false | 'third'>`:

- `cancel=null` → single-button (alert-style); always resolves true.
- `third=null` → two-button (confirm-style); resolves true|false.
- `third` set → three buttons left-to-right: `cancel`, `third`, `ok`. Used for the scan dialog's `[ Abort ] [ Read tags only ] [ Analyze missing ]`.
- Enter → ok (rightmost, primary). Esc / click-outside → cancel (only if `cancel` is set; without a cancel button the user has to pick an option).
