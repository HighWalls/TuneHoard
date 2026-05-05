# CLAUDE.md

Primer for any agent working on **TuneHoard**. Read this first — it's short on purpose.

## What this is

TuneHoard is a Python CLI that takes a **Spotify, YouTube, or SoundCloud URL** — playlist *or* single track / video — gets the track list, downloads each track as 320k MP3, analyzes BPM + musical key locally, and writes ID3 tags that Rekordbox reads. Output is organized per-playlist (or under `singles/` for individual tracks) with a sorted `index.csv` for DJ prep.

Repo: https://github.com/HighWalls/TuneHoard

- **Spotify** playlists / tracks: search on YouTube / SoundCloud and download the first match. Artist and title come from Spotify (reliable).
- **YouTube / SoundCloud** playlists / videos: each entry's URL is downloaded directly (no search). Artist/title is best-effort parsed from the video title — `"Artist - Title"` split, falling back to the uploader as artist. Less reliable metadata than Spotify.
- **Single tracks** (any source): land in `<out>/singles/` so they accumulate together. The same `--skip-existing` dedup applies, so adding more singles incrementally won't re-download.

## Two ways to run

### CLI

```bash
python main.py <url>
    [--sources youtube,soundcloud]  # comma list, tried in order (default)
    [--out downloads]                # output directory
    [--into <folder>]                # override loader folder name (e.g. merge singles into a playlist dir)
    [--limit N]                      # only process first N tracks
    [--skip-existing]                # skip tracks already in index.csv or on disk
    [--bucket-by-bpm]                # group into BPM-range subfolders + re-tag from CSV
    [--reanalyze]                    # re-run BPM/key on existing MP3s (implies --skip-existing)
    [--key-format camelot|musical]   # TKEY + filename prefix for NEW downloads
    [--migrate-keys]                 # opt-in: also rewrite existing files to --key-format
    [--bpm-min 85]                   # half/double-time clamp lower bound (sub-bound BPMs are doubled)
    [--bpm-max 200]                  # half/double-time clamp upper bound (over-bound BPMs are halved)
    [--analysis-seconds 120]         # seconds of audio loaded for BPM/key analysis
    [--ffmpeg-location PATH]         # optional explicit ffmpeg path (else PATH lookup)
```

`<url>` can be a playlist or single-track URL on Spotify, YouTube, or SoundCloud.

### Dashboard

```bash
python server.py    # starts FastAPI at http://127.0.0.1:8765/, auto-opens browser
```

Same pipeline, GUI front-end. The dashboard at `dashboard/tunehoard/tunehoard.html` runs against `server.py`'s API and reads / writes the same `index.csv` files the CLI uses. Settings live in `.tunehoard_settings.json` (gitignored). The dashboard supports:

- URL preview with real titles from the backend (debounced, abortable). URL bar accepts paste *and* drag-drop (text/uri-list or text/plain). `spotify:liked` is recognized as a special URL that downloads the user's Liked Songs.
- Download jobs with **live progress streamed over WebSocket** (`/ws/jobs`); 10-second HTTP polling kept as a safety net if the socket drops and reconnects fail. Cancel kills the subprocess tree. First-run guard: if `library_dir` is unset, the Download button shows a modal pointing the user at Settings instead of letting the request 400.
- **Multi-playlist sidebar** on the left of the library view: auto-discovers sibling folders under `library_dir.parent` containing an `index.csv`, click to switch the dashboard between playlists. Hidden when ≤ 1 playlist exists.
- Library tree with collapsible BPM buckets (empty buckets are hidden); shift-click extends a range from the last-clicked anchor (in DOM order), ctrl-click toggles individuals.
- Inline edit form per track (Save / Re-analyze / Open in folder / Delete)
- **Per-track [▶] play button** that streams the MP3 from `GET /api/audio/{id}` into a single global hidden `<audio>` element — Range-aware so seeking doesn't redownload.
- Drag-drop tracks between buckets (auto-picks half-time / double-time / midpoint BPM)
- Bulk action bar on selected rows (Re-analyze / Delete / Move to bucket)
- Right-click bucket → Open folder in Explorer / Re-analyze all in bucket / Expand–Collapse all
- Source filter radios: All / Spotify / YouTube / SoundCloud / Scanned (matches `t.source === ""` for tracks indexed via Scan folder).
- Failed-tracks panel between jobs and library (hidden when count = 0): lists `× artist — title  [open Spotify]` rows, polled from `GET /api/failures` on a 30s timer + on focus.
- **Spotify picker** [ Browse Spotify ] button (visible when authorized) opens a modal listing ★ Liked Songs + ≫ user playlists; selecting one drops its url into the URL bar.
- **Auto-update banner** at the top of the page when `GET /api/version` reports a newer GitHub release tag. Dismissible per-version via localStorage; silent on network errors.
- Settings page with native file pickers (tkinter), Spotify OAuth (with 5-min auth timeout), source reorder, advanced BPM clamp / analysis duration / ffmpeg path inputs.
- Three-button "Scan folder" dialog for indexing pre-existing MP3 collections (read tags only / analyze missing via librosa / abort)
- Manual "Migrate library" button to rewrite TKEY / filename to a new key format
- Custom NFO-styled modal replaces native `confirm()` / `alert()`
- Theme switcher (top-left) with 3 presets + Randomize from a curated palette pool

See `docs/ARCHITECTURE.md` § Dashboard for the full API surface and helper-function reference.

### Native window (optional)

```bash
python app_native.py    # same dashboard, hosted in a 1280×800 pywebview window
```

`app_native.py` boots uvicorn on a daemon thread and opens the dashboard in a native OS window instead of the user's default browser. Closing the window flips `uvicorn.Server.should_exit` to shut the server down. Internally it monkey-patches `webbrowser.open` to a no-op before importing `server` to suppress the auto-open-tab.

### Distribution

`tunehoard.spec` is a PyInstaller onefile spec (`pyinstaller tunehoard.spec` → `dist/TuneHoard.exe`). `.github/workflows/build.yml` builds Windows + macOS binaries on every `v*.*.*` tag push and attaches them to the GitHub Release. PyInstaller is *not* in `requirements.txt` — it's installed in CI and as-needed locally.

### Setup (either path)

`ffmpeg` must be on PATH (system install, not pip) and `pip install -r requirements.txt`. Spotify URLs additionally need `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` in `.env` (CLI) or in the dashboard Settings page, plus a one-time browser OAuth authorization on first run (cached to `.spotify_cache`); YouTube / SoundCloud URLs need neither. The redirect URI registered in the Spotify dashboard must match the one in `spotify_client.py` (default `http://127.0.0.1:8888/callback`).

## File map

| File | Responsibility |
|---|---|
| `main.py` | CLI, URL dispatch, orchestration, filename formatting, CSV export |
| `server.py` | FastAPI dashboard server. Wraps every CLI helper as JSON endpoints; spawns `main.py` as a subprocess for download jobs. Pushes job state over `/ws/jobs` WebSocket. Settings persisted to `.tunehoard_settings.json`. |
| `dashboard/tunehoard/tunehoard.html` | Single-file vanilla-JS dashboard. Booted by `server.py`. On load it replaces its built-in mock data with `/api/library` + `/api/jobs` (HTTP) + `/ws/jobs` (WebSocket push). |
| `spotify_client.py` | Spotify playlist / track / Liked Songs → `list[Track]` via spotipy (OAuth user flow). Defines the `Track` dataclass; `get_playlist_tracks()` / `get_track()` / `get_liked_songs()`. |
| `app_native.py` | Optional pywebview entry point — runs `server.py` on a daemon thread and shows the dashboard in a native OS window instead of a browser tab. |
| `tunehoard.spec` | PyInstaller onefile spec for `dist/TuneHoard.exe`. Bundles `dashboard/`, the librosa+numba+llvmlite+uvicorn hidden-import tree, and `copy_metadata` for pydantic/fastapi/uvicorn. |
| `.github/workflows/build.yml` | GitHub Actions: builds Win+macOS binaries on tag pushes (`v*.*.*`), attaches them to the auto-generated GitHub Release. |
| `ytdlp_loader.py` | YouTube / SoundCloud playlist or single video URL → `list[Track]` via yt-dlp. Entries carry a `source_url` for direct download. Single-video URLs return folder name `"singles"`. |
| `downloader.py` | yt-dlp wrapper with two modes: `download_url()` (direct) for YT/SC entries, `download_track()` (search) for Spotify-derived tracks. |
| `analyzer.py` | librosa: BPM (beat tracker) + key (Krumhansl-Schmuckler on chroma) |
| `camelot.py` | `(root, mode) → Camelot notation` lookup (e.g. `"A", "minor" → "8A"`); `musical_key_short()` for ID3 TKEY musical form. |
| `tagger.py` | mutagen ID3 writer: `TBPM`, `TKEY`, `TXXX:CAMELOT_KEY`, `TXXX:MUSICAL_KEY`, `TIT2/TPE1/TALB`, `COMM` |

Dataflow: `URL dispatcher → list[Track] → download (direct for YT/SC, search for Spotify; youtube → soundcloud fallback) → librosa analyze → mutagen tag → atomic rename → atomic CSV + failures.txt`.

Tracks that fail on every source are written to `failures.txt` alongside `index.csv` with their Spotify URLs for manual recovery.

## Conventions

- **Both key formats are always written to ID3.** Every tagged file gets:
  - `TKEY` = whichever the user picked (`"8A"` or `"Am"`) — primary frame Rekordbox/Traktor/Serato display.
  - `TXXX:CAMELOT_KEY` = always Camelot (regardless of choice).
  - `TXXX:MUSICAL_KEY` = always musical (regardless of choice).
  - `COMM` = both, human-readable: `"8A | 128 BPM | A minor"`.
  This way the file is portable across any DJ software no matter the user's choice — every tool can find what it wants in *some* frame.
- **Default target: Rekordbox.** `TKEY` defaults to Camelot (`"8A"`). `--key-format musical` writes `"Am"` for Traktor / Serato users. The CSV `index.csv` also keeps both `camelot` and `key` columns populated.
- **Changing `--key-format` only affects NEW downloads.** Existing files keep their current TKEY format and filename prefix on subsequent syncs. To rewrite an entire library to the new format, pass `--migrate-keys` explicitly. The bucket-sync rename pass treats both `8A - 128 - …` and `Am - 128 - …` as valid filenames for the same row, so toggling `--key-format` on a re-run doesn't cascade-rename files.
- **BPM is stored as int string in `TBPM`** (Rekordbox convention). The half/double-time normalizer in `analyzer.py` defaults to clamping `[85, 200]` — intentional for DJ use, not a bug. Bounds are configurable via `--bpm-min` / `--bpm-max` (CLI) or the Settings → Advanced fields (dashboard); both halves of the bound are passed through to `analyze()` per call.
- **Filename pattern:** `{camelot} - {bpm:03d} - {artist} - {title}.mp3`. Sorts nicely in file browsers and doubles as a visual fallback if tags get stripped.
- **BPM bucketing (`--bucket-by-bpm`).** Anchor band is `115-125` (11 wide, DJ-idiomatic), everything else is 10-wide: `126-135`, `136-145`, ..., `105-114`, `95-104`, etc. No-BPM tracks go to `unknown-bpm/`. The bucket name is derived from BPM each time — rerunning with `--skip-existing --bucket-by-bpm` reorganizes existing files in place (and cleans empty folders), so the flag is safe to toggle on an already-downloaded playlist. **During the sync pass it also re-writes ID3 tags from the CSV row**, so manual edits to `index.csv` (e.g., fixing a wrong BPM) propagate into the file's tags + folder location on the next run.
- **Fixing wrong BPMs.** Two workflows: (1) bulk auto-correct via `--reanalyze --bucket-by-bpm` (re-runs the improved detector on every existing MP3, updates tags + filenames + CSV + buckets); (2) surgical via editing `index.csv` then rerunning with `--skip-existing --bucket-by-bpm`. The detector uses `start_bpm=150` and clamps to `[85, 200]` by default to reduce half-time errors — genuine sub-85 BPM tracks (boom-bap) get wrongly doubled and need the manual path (or `--bpm-min 70` for that single run).
- **OAuth user flow (not Client Credentials).** Spotify tightened Client Credentials access to `playlist_items` in 2025 — it now returns 401 even on public playlists. We use `SpotifyOAuth` with scopes `playlist-read-private playlist-read-collaborative user-library-read`. This reads public + private + collaborative + Liked Songs. Editorial/algorithmic playlists (IDs starting `37i9dQZF1...`) still 404 — that's a separate access tier. **YouTube / SoundCloud URLs don't need any auth at all.** The `user-library-read` scope was added in 2026 for the Spotify picker — pre-existing `.spotify_cache` tokens lack the scope and trigger a one-time re-auth on the next call to `current_user_saved_tracks()`.
- **Multi-playlist layout.** Each "playlist" is a sibling subfolder under `library_dir.parent`, each with its own `index.csv` and bucket subdirs. The dashboard sidebar walks the parent and offers per-playlist switching; the CLI achieves the same via `--out=parent --into=playlist_name`. There is no global cross-playlist index — that's deliberate, dedup is intra-playlist.
- **`spotify:liked` is a sentinel URL.** Lowercase-and-strip equality; not a real URL. Recognized in `main.py`'s URL dispatcher (before the Spotify regex) and in `server.py`'s `/api/preview`. Submitted via the dashboard's [ Browse Spotify ] picker.
- **WebSocket pushes are best-effort, polling is the truth-of-record.** `/ws/jobs` broadcasts on JOBS state changes (throttled to ~2 Hz on chatty stdout, unthrottled on terminal states). The dashboard treats WS messages as a fast hint — the 10-second `refreshJobs()` poll runs unconditionally so a dropped/missed message can never desync the UI for long.
- **`spotify_id` column is a misnomer.** It's a generic primary key. Spotify tracks are raw Spotify IDs; YouTube entries are `"yt:<video_id>"`; SoundCloud are `"sc:<track_id>"`. Namespaced to prevent collisions across sources. Do not "clean up" by splitting into separate columns — it would break the existing `--skip-existing` dedup path.
- **Local analysis only.** We do not call Spotify Audio Features or any paid BPM/key API. See `docs/GOTCHAS.md` for why.
- **Windows-first.** The dev env is Windows 11. Paths use `pathlib`; filename sanitization strips `<>:"/\|?*` and control chars. stdout/stderr are reconfigured to UTF-8 at startup because the default cp1252 codepage can't print most track titles or the `→` progress arrows.
- **`--skip-existing` recovers from disk, not just CSV.** If `index.csv` is missing/corrupt but MP3s exist, the flag reconstructs rows from ID3 tags (BPM, Camelot) so you don't re-download 184 tracks. The `key` (full name) and `source` columns are lost in the reconstruction — that's OK, Rekordbox only reads BPM + Camelot.
- **CSV writes are atomic.** Written to `index.csv.tmp` then `replace()`'d. Prevents a crash in the sort/write block from wiping a valid index.
- **Single path setting.** The dashboard exposes one folder (labeled "Output Folder" in the UI, `library_dir` in the API + JSON). Downloads go there, the library tree reads from there. The earlier `output_dir`/`library_dir` split was confusing and got merged; old settings files are migrated on load.
- **Scan can index pre-existing MP3 collections.** Empty library + `[ Scan folder ]` button → walks all MP3s and builds an `index.csv` from filenames + ID3 tags. Two modes: "Read tags only" (fast, leaves unlabeled tracks blank) and "Analyze missing" (also runs librosa on tracks without BPM/key tags — slow but populates everything). Smart-fill: re-running scan never overwrites filled values; only blank fields get populated from tags.
- **Subprocess job kills are tree-wide.** Cancel button → `taskkill /F /T /PID` on Windows so yt-dlp + ffmpeg children don't orphan. POSIX uses `terminate` then `kill` after 2s.

## Landmines (read before editing)

1. **Spotify Audio Features is deprecated for new apps (Nov 2024).** Do not "fix" the local analyzer by switching to `sp.audio_features()` — it will 403. See `docs/GOTCHAS.md`.
2. **ffmpeg is a system dependency, not pip.** Missing it produces a confusing yt-dlp postprocessor error. Check PATH first.
3. **Krumhansl key detection is ~80–85% accurate by design.** Wrong-key reports aren't bugs to fix in the algorithm — they're the ceiling of chroma-based detection. If accuracy matters more, the upgrade path is `essentia` (hard to install on Windows) or a paid API fallback.
4. **Don't re-read a track file right after tagging to "verify."** mutagen errors on failure; trust it.

## Adding features

- **New download source?** Add `{source_name}: "{prefix}search1:"` to the dict in `downloader.py:13`. yt-dlp supports many (`scsearch`, `ytsearch`, `bcsearch` for Bandcamp, etc.).
- **Different DJ software?** See `docs/ARCHITECTURE.md` § Tagging. Serato uses `GEOB` frames; Traktor reads standard ID3 but prefers key in musical notation.
- **Private playlists?** Swap `SpotifyClientCredentials` for `SpotifyOAuth` in `spotify_client.py` and wire a redirect URI. Non-trivial; ask user first.

## More detail

- `docs/ARCHITECTURE.md` — pipeline internals, module contracts, extension points
- `docs/GOTCHAS.md` — design rationale for non-obvious choices, failure modes
