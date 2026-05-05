# TuneHoard — Gotchas

Non-obvious design decisions and known failure modes in TuneHoard. If something looks "wrong," check here before "fixing" it.

## Do not call Spotify Audio Features

Spotify deprecated the `/audio-features` and `/audio-analysis` endpoints for all *new* apps in November 2024. If you're tempted to simplify `analyzer.py` by calling `sp.audio_features([id])` instead of running librosa, you'll get a 403 from any app created after the deprecation date.

This is why we do local analysis. Don't migrate back. If Spotify reverses the decision, we can add it as a fast-path *before* librosa — but the local analyzer stays as the fallback because it's source-of-truth for the file we actually downloaded (remixes, edits, and bootlegs won't match the Spotify track's features).

## Spotify response key: `item`, not `track`

In 2025 Spotify changed the `playlist_items` response shape. Per-entry track data used to live under `entry["track"]`; it now lives under `entry["item"]` (unifying tracks and podcast episodes under one key). `spotify_client.py:get_playlist_tracks` reads `entry.get("item") or entry.get("track")` so it tolerates both shapes.

If you see "0 tracks" on a playlist that clearly has items, and `sp.playlist()` returns metadata fine, this is almost certainly the culprit. Dump the raw `sp.playlist_items(pid, limit=1)` JSON and check whether `item` or `track` is populated. Do not restore `fields="items(track(...))"` in the spotipy call — that strict field selection throws away everything under `item` and reproduces the zero-tracks bug.

## ffmpeg is a system dependency, not a pip package

`yt-dlp` shells out to `ffmpeg` for the mp3 conversion postprocessor. If ffmpeg isn't on PATH, yt-dlp throws a confusing "ffprobe/ffmpeg not found" error during postprocessing — the file downloads fine but the mp3 conversion fails and no output is produced.

On Windows: `winget install Gyan.FFmpeg`, then restart the terminal so PATH picks up the new entry. On macOS: `brew install ffmpeg`. On Linux: `apt install ffmpeg` or equivalent.

`pip install ffmpeg-python` does NOT install the binary — it's just a Python wrapper that still requires the native ffmpeg. Do not add it to requirements.txt.

## Killing a job means killing the whole process tree

`server.py:_kill_process_tree` shells out to `taskkill /F /T /PID <pid>` on Windows rather than calling `proc.terminate()`. Reason: `main.py` spawns `yt-dlp`, which spawns `ffmpeg` for the mp3 postprocessor. Plain `proc.terminate()` ends only the direct Python child — `yt-dlp` and `ffmpeg` orphan to the OS and keep running until they finish their current task (which can take minutes). The `/T` flag walks the tree, `/F` forces termination.

POSIX falls back to `terminate` then `kill` after 2s — most distros put child processes in the same process group by default, and `terminate` propagates.

Verify cleanup after a cancel by checking `Get-Process ffmpeg,yt-dlp` — if those linger, the kill didn't take.

The runner thread that's reading `proc.stdout` has a guard at `proc.wait()` exit: if `job["status"]` is already `"cancelled"` (set by the DELETE handler), it skips the usual "set to failed if exit code != 0" branch. Without that guard, a cancelled job would flip to "failed" status as soon as the killed subprocess exited, confusing the dashboard.

## tkinter dialog runs from a uvicorn worker thread

`POST /api/browse` invokes `tkinter.filedialog.askdirectory()` / `askopenfilename()`. Tkinter is famously single-threaded on macOS and tetchy elsewhere — but uvicorn dispatches sync FastAPI handlers to a thread pool *off* the asyncio event loop, so the dialog runs in a worker thread, not the main thread. On Windows this works fine. On macOS it will likely complain unless we move it to a subprocess or main-thread-runner pattern.

The dialog blocks its worker thread for the duration of the picker (could be minutes if the user wanders off). Other endpoints stay responsive because uvicorn has a pool of workers. Don't try to "improve" this by making the handler `async def` and awaiting tk — there is no asyncio integration for tkinter and it'd just block the event loop hard.

## `analyze_missing=true` blocks the request

`POST /api/library/scan?analyze_missing=true` runs `analyzer.analyze()` synchronously in-process for every track that lacks a TBPM/TKEY tag. ~3 seconds per analyzed track. A folder with 200 unlabeled MP3s = ~10 minutes of HTTP request blocking. The dashboard sits on a "scanning…" toast the entire time.

This is acceptable for a local one-shot tool — but if a user runs it on a huge library and gets impatient, they'll close the browser tab and the analysis keeps running server-side until completion. There's no progressive feedback. If this becomes a real problem, port the scan to the same subprocess-based job pattern that downloads use (`POST /api/jobs` style) so the dashboard can show streaming progress and Cancel kills it.

## Smart-fill scan never overwrites existing values

`/api/library/scan` is merge-safe in two directions:

1. **Existing rows are kept**: re-running scan on an indexed folder doesn't clobber hand-edits. The merge keys on `file` (filename basename).
2. **Blanks get filled, non-blanks don't**: if you scan with `read_bpm_key=true` after a previous Cancel scan, blank BPM/Camelot/key fields are populated from tags. Non-blank values (your hand-edits, prior tag reads) are preserved.

This is intentional and load-bearing. Don't "simplify" by always overwriting from tags — users will lose work. The directionality is "fill gaps, never replace."

Corollary: scanning never touches the MP3 files' actual ID3 tags. We only read them. Tag values written by Rekordbox / Mixed In Key / TuneHoard remain on disk regardless of which scan mode the user picks.

## `output_dir` and `library_dir` were merged

The settings model used to expose two path fields: `output_dir` (download root) and `library_dir` (which subfolder to read into the dashboard). They were almost always equal and the split confused users — picking a "new output folder" wouldn't change which folder the library tree displayed. Merged in May 2026 into a single `library_dir`.

`load_settings()` migrates older configs on first read: if `library_dir` is empty/missing and `output_dir` is set, the value is folded in and `output_dir` is dropped. `save_settings()` strips `output_dir` on every write so it can never come back even if a new caller or ticked checkbox somehow passes one. The Pydantic `SettingsPatch` model also doesn't accept `output_dir` — passing it via `PATCH /api/settings` is silently ignored. Don't re-add the field — it's confusing without offering anything.

## Function declaration hoisting + addEventListener = silent dead wrappers

When wrapping a top-level `function foo(){}` later (`window.foo = async function(){...}`), any pre-existing `addEventListener('click', foo)` keeps its reference to the *original* lexical binding, not the reassigned `window.foo`. The wrapper effectively never fires.

Hit this once in the dashboard's Settings-page sync — wrapped `showSettings` to load settings on first open, never noticed the wrapper wasn't running. Fix was to move the settings-fetch out of the wrapper and into a standalone `(async function syncSettingsToDom(){})()` IIFE that runs at script load time.

When you need to inject behavior into a function that's already bound to event listeners, **don't reassign the global**. Either:
1. Edit the original definition.
2. Replace the bound listener with a new one that calls a wrapper.
3. Run your code in a separate IIFE / handler that doesn't depend on the wrap firing.

## OpenBLAS thread-pool race on Windows (server only)

`server.py` runs `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` before any heavy import. Without this, importing `librosa` (which transitively pulls in numpy + numba + OpenBLAS) at module import time triggers a thread-pool race on Windows:

```
OpenBLAS error: Memory allocation still failed after 10 retries, giving up.
```

The CLI `main.py` doesn't hit this because by the time librosa imports, the Python process has finished other startup; uvicorn's process model and thread pool are different and more aggressive. The two-line `os.environ.setdefault()` block at the top of `server.py` (before any other imports) is load-bearing — keep it there. If you ever add a worker pool or split the server into multiple processes, set both env vars in the parent shell before launching, or do it in the new entry point's first lines.

`from __future__ import annotations` was removed from `server.py` because future imports must be the first statement after the docstring — and we need the env-var setup to come *before* any heavy import. We're on Python 3.13 in dev so `int | None` and `dict[str, Any]` work natively without the future import. If you ever target older Python, restructure as: docstring → future import → env setup → other imports.

## Settings file is per-user, gitignored

`.tunehoard_settings.json` lives in the project root and holds `library_dir`, `output_dir`, `spotify_client_id`, `spotify_client_secret`, key format, and a few advanced values. It's in `.gitignore`. **The Spotify client secret is returned masked** (`********`) from `GET /api/settings`. The dashboard knows not to re-send the masked value — its `s-csec` blur handler skips PATCH if the field value is just asterisks. If you add a new endpoint or field that surfaces the secret, preserve this masking.

`save_settings()` writes atomically (`.json.tmp` then `replace()`). The same atomic-write discipline applies as for `index.csv` — a crash mid-write can never corrupt the existing settings.

## Dashboard mock-on-boot is intentional

`dashboard/tunehoard/tunehoard.html` ships with seed data hardcoded into `const LIB = [...]` and `const JOBS = [...]`. This is deliberate so the file renders standalone in a plain browser (no server) for design iteration. When `server.py` is the host, the API integration block at the bottom of the script overwrites both arrays in-place (`LIB.length = 0; LIB.push(...realData)`) within ~50 ms of page load.

The user sees mock rows briefly before real data takes over. Don't try to "fix" this by removing the seed — empty-tree-then-pop-in is a worse first-paint than placeholder-then-real. If you genuinely need an empty start, gate the seed behind a flag, don't delete it.

## Windows stdout defaults to cp1252

Windows Python opens `sys.stdout` with the system codepage (cp1252 for most installs), which can't encode most unicode track titles or even the `→` character used in progress output. `main.py` reconfigures both `stdout` and `stderr` to UTF-8 at import time. Do not remove this — it is the *only* reason the script doesn't crash on playlists with non-ASCII artist names. If you see a `UnicodeEncodeError` during printing, confirm that reconfigure block is still present.

Setting `PYTHONIOENCODING=utf-8` in the environment works too but isn't portable — code-level reconfigure is the reliable fix.

## CSV writes must be atomic

`main.py` writes `index.csv.tmp` then `replace()`s it onto `index.csv`. Earlier versions opened `index.csv` directly in `"w"` mode (truncating immediately) and sorted inline during the write loop — a `TypeError` in the sort comparator left us with a header-only file and 182 orphaned MP3s. The atomic pattern means even a mid-write crash leaves the prior CSV intact.

Don't "simplify" this back to a direct write. The cost (one extra rename) is nothing; the failure mode (destroy 30+ minutes of download work) is everything.

Corollary: `--skip-existing` falls back to reconstructing rows from MP3 ID3 tags if `index.csv` is missing or has no data rows. This is load-bearing recovery logic, not an optimization. If you change the tagging scheme (what goes in `TBPM`/`TKEY`), update `reconstruct_row_from_disk()` to match.

## Windows antivirus can lock freshly-written MP3s

`main.py:safe_replace()` wraps `os.replace()` in a retry loop (6 attempts, 0.5s apart). Windows Defender or the Search Indexer sometimes holds a transient exclusive lock on a freshly-written file for a few hundred ms while it scans/indexes. Without the retry, `os.replace()` raises `PermissionError: [WinError 5] Access denied` on random tracks — roughly one in every 50-200 files in our testing.

This is not the same as a true permission error (read-only file, ACL issue). Genuine errors fail after all retries. Do not catch broader exception types; the retry is targeted at this specific transient race.

## Krumhansl key detection accuracy is capped

`analyzer.py` uses Krumhansl-Schmuckler key profiles correlated against the mean chroma. Published benchmarks on this algorithm hit ~80–85% on pop/rock corpora. Failures concentrate in:

- **Bass-heavy electronic music** — low-frequency harmonics bleed across chroma bins and bias the root.
- **Modal tracks** (Dorian, Phrygian) — get forced into major or minor.
- **V-chord-heavy tracks** — classified as the dominant instead of the tonic (e.g., a C major track with lots of G chords gets called G major).
- **Very short tracks** — we only analyze the first 120 seconds; if the intro is atonal or in a different key, the whole track gets misclassified.

**Do not** try to "fix" wrong keys by tweaking the profile weights — that's whack-a-mole. If accuracy matters, the real upgrades are:

1. `essentia`'s `KeyExtractor` with the `edma` profile (~90%+ but painful to install on Windows — requires MSVC and is not officially pip-shipped for modern Python on Win).
2. A paid API (GetSongBPM free tier, Tunebat scraping, etc.) — covers mainstream well, misses underground.
3. Mixed In Key desktop app (gold standard, paid, manual workflow).

## BPM clamping to [85, 200] + start_bpm=150 is intentional

`analyzer.py:_detect_bpm()` passes `start_bpm=150` to librosa's beat tracker (overriding its default of 120) and clamps the result to `[85, 200]` — doubles anything under 85, halves anything over 200. Tuned for DJ electronic music including D&B and hardcore on the top end.

Why these specific numbers:

- `start_bpm=150` biases the autocorrelation pick toward DJ tempos. Default `start_bpm=120` systematically under-picks on D&B/fast-trap, reporting e.g. 83 BPM for a 166 BPM track (the autocorrelation has peaks at both 83 and 166, and ties go to whichever is closer to start_bpm).
- Clamp `[85, 200]`. The bounds are **non-overlapping**: doubling `< 85` caps at 170 (never triggers halve); halving `> 200` floors at 100 (never triggers double). This avoids an infinite loop you'd hit with bounds like `[90, 180]` where 90 doubles to 180, gets halved to 90, and oscillates.
- The upper bound was originally 170. That **wrongly halved genuine D&B and hardcore tracks** (user reported a 185 BPM track being destroyed). 200 is the right ceiling for house/techno/D&B/fast-trap libraries. Raise further only if the library includes breakcore / speedcore (200+ BPM).

The remaining tradeoff: genuine **sub-85 BPM tracks get wrongly doubled**. Boom-bap hip-hop at 75-85 BPM becomes 150-170. For those, the user edits `index.csv` manually and reruns with `--bucket-by-bpm` — the sync pass re-tags from the CSV. Alternatively, `--reanalyze` re-runs the detector on existing MP3s (useful after tuning the algorithm itself).

Do not widen the clamp without re-deriving the non-overlap guarantee. Specifically: `lower * 2 ≤ upper` AND `upper / 2 ≥ lower` must both hold. A single infinite loop in `_detect_bpm` hangs the whole run silently — there's no timeout.

## Rekordbox reads `TKEY` as-is

Rekordbox displays whatever string is in the `TKEY` ID3 frame in its Key column, unmodified. We put Camelot notation there directly (`"8A"`, `"12B"`). This is why `tagger.py` writes Camelot to `TKEY` — *not* to a custom `TXXX:KEY` user-defined frame or to the comment.

Other DJ software is different:

- **Traktor** — reads `TKEY` but defaults to displaying musical notation. Users can toggle to Camelot in prefs. If targeting Traktor, consider writing musical notation (`"Am"`) and letting the user opt into Camelot display.
- **Serato** — reads `TKEY` but will overwrite it with its own analysis on first import unless you disable "Auto Detect Key."
- **Engine DJ / Prime** — reads `TKEY` but prefers the Mixed In Key format variant (`"8A"` works).

If you generalize to multiple targets, add a `--key-format` flag rather than changing the default.

## Auth: OAuth user flow, not Client Credentials

`spotify_client.py` uses `SpotifyOAuth` with scopes `playlist-read-private playlist-read-collaborative`. This was **not** the original design — we started with `SpotifyClientCredentials` (app-level auth, no user login), but discovered in 2025 that Spotify tightened Client Credentials access to the `playlist_items` endpoint. Symptom: `sp.playlist(id)` returns metadata fine (including `public: True`), but `sp.playlist_items(id)` returns **401 "Valid user authentication required"** — even on public user-owned playlists. This happened to us with a public playlist owned by a different user.

Adding `market='US'` does not help. The fix is OAuth user flow, which we now use.

What works with OAuth:

- Public and private user-created playlists (owned by the authenticated user or anyone else)
- Collaborative playlists
- "Liked Songs" (via `sp.current_user_saved_tracks()` — not wired up yet)

What still **does not** work:

- **Spotify-curated editorial / algorithmic playlists** (IDs starting with `37i9dQZF1...`) — these return **404** as of November 2024. OAuth does not unlock these; they're a separate "extended access" tier that requires a manual application to Spotify. Workaround: the user duplicates the editorial playlist into a personal playlist (Spotify UI → `...` → Add to playlist → New playlist) and passes that URL instead.

First-run UX: opens the default browser to `accounts.spotify.com/authorize`, user clicks "Agree", Spotify redirects to `http://127.0.0.1:8888/callback` where spotipy's tiny local server catches the code. The token is cached to `.spotify_cache` in the project root (gitignored) and reused on subsequent runs — no more browser popups until the refresh token expires (~months).

The redirect URI is hardcoded in `spotify_client.py:get_playlist_tracks`. It must exactly match one registered in the Spotify app dashboard. Spotify stopped accepting `localhost` in 2025 — use the literal IP `127.0.0.1`.

## The sanitized filename limit is 120 chars

`main.py:safe_filename()` truncates to 120 chars. Windows' MAX_PATH is 260 and the full path includes the output directory, so a 120-char filename leaves room for a reasonable nested path. Don't raise it without checking the longest realistic output path.

## CSV Camelot sort is lexicographic

`main.py` sorts the CSV rows by `str(camelot)`. This puts `"10A"` before `"1A"` (lexicographic "10" < "1A" is false — actually `"10A" > "1A"` lexicographically because `"0" < "A"`). So the sort is *stable and unique* but not musically ordered.

If the user wants wheel-order sort (1A, 1B, 2A, 2B, ...), parse to `(int(num), letter)` before sorting. We haven't done this because the CSV is typically loaded into the DJ app which re-sorts by its own rules.

## The dev environment is Windows 11

All paths use `pathlib`, but shell examples in docs assume bash-on-Windows conventions (forward slashes, `python` on PATH). If running on Linux/macOS, the only real difference is `ffmpeg` installation. No code changes needed.
