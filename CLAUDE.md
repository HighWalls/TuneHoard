# CLAUDE.md

Primer for any agent working on this project. Read this first — it's short on purpose.

## What this is

A Python CLI that takes a **Spotify, YouTube, or SoundCloud playlist URL**, gets the track list, downloads each track as 320k MP3, analyzes BPM + musical key locally, and writes ID3 tags that Rekordbox reads. Output is organized per-playlist with a sorted `index.csv` for DJ prep.

- **Spotify** playlists: tracks are searched on YouTube / SoundCloud and the first match is downloaded. Artist and title come from Spotify (reliable).
- **YouTube / SoundCloud** playlists: each entry's URL is downloaded directly (no search). Artist/title is best-effort parsed from the video title — `"Artist - Title"` split, falling back to the uploader as artist. Less reliable metadata than Spotify.

## Run it

```bash
python main.py <spotify_playlist_url>
    [--sources youtube,soundcloud]  # comma list, tried in order (default)
    [--out downloads]                # output directory
    [--limit N]                      # only process first N tracks
    [--skip-existing]                # skip tracks already in index.csv or on disk
    [--bucket-by-bpm]                # group into BPM-range subfolders + re-tag from CSV
    [--reanalyze]                    # re-run BPM/key on existing MP3s (implies --skip-existing)
```

`ffmpeg` must be on PATH (system install, not pip) and `pip install -r requirements.txt`. Spotify URLs additionally need `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` in `.env` and a one-time browser OAuth authorization on first run (cached to `.spotify_cache`); YouTube / SoundCloud URLs need neither. The redirect URI registered in the Spotify dashboard must match the one in `spotify_client.py` (default `http://127.0.0.1:8888/callback`).

## File map

| File | Responsibility |
|---|---|
| `main.py` | CLI, URL dispatch, orchestration, filename formatting, CSV export |
| `spotify_client.py` | Spotify playlist URL → `list[Track]` via spotipy (OAuth user flow). Also defines the `Track` dataclass. |
| `ytdlp_loader.py` | YouTube / SoundCloud playlist URL → `list[Track]` via yt-dlp. Entries carry a `source_url` for direct download. |
| `downloader.py` | yt-dlp wrapper with two modes: `download_url()` (direct) for YT/SC entries, `download_track()` (search) for Spotify-derived tracks. |
| `analyzer.py` | librosa: BPM (beat tracker) + key (Krumhansl-Schmuckler on chroma) |
| `camelot.py` | `(root, mode) → Camelot notation` lookup (e.g. `"A", "minor" → "8A"`) |
| `tagger.py` | mutagen ID3 writer: `TBPM`, `TKEY` (Camelot), `TIT2/TPE1/TALB`, `COMM` |

Dataflow: `URL dispatcher → list[Track] → download (direct for YT/SC, search for Spotify; youtube → soundcloud fallback) → librosa analyze → mutagen tag → atomic rename → atomic CSV + failures.txt`.

Tracks that fail on every source are written to `failures.txt` alongside `index.csv` with their Spotify URLs for manual recovery.

## Conventions

- **Target: Rekordbox.** `TKEY` holds Camelot notation (`"8A"`), not musical (`"Am"`). Rekordbox displays this verbatim in its Key column. Don't change to musical without asking.
- **BPM is stored as int string in `TBPM`** (Rekordbox convention). The half/double-time normalizer in `analyzer.py` clamps to 70–180 BPM — this is intentional for DJ use, not a bug.
- **Filename pattern:** `{camelot} - {bpm:03d} - {artist} - {title}.mp3`. Sorts nicely in file browsers and doubles as a visual fallback if tags get stripped.
- **BPM bucketing (`--bucket-by-bpm`).** Anchor band is `115-125` (11 wide, DJ-idiomatic), everything else is 10-wide: `126-135`, `136-145`, ..., `105-114`, `95-104`, etc. No-BPM tracks go to `unknown-bpm/`. The bucket name is derived from BPM each time — rerunning with `--skip-existing --bucket-by-bpm` reorganizes existing files in place (and cleans empty folders), so the flag is safe to toggle on an already-downloaded playlist. **During the sync pass it also re-writes ID3 tags from the CSV row**, so manual edits to `index.csv` (e.g., fixing a wrong BPM) propagate into the file's tags + folder location on the next run.
- **Fixing wrong BPMs.** Two workflows: (1) bulk auto-correct via `--reanalyze --bucket-by-bpm` (re-runs the improved detector on every existing MP3, updates tags + filenames + CSV + buckets); (2) surgical via editing `index.csv` then rerunning with `--skip-existing --bucket-by-bpm`. The detector uses `start_bpm=150` and clamps to `[85, 170]` to reduce half-time errors — genuine sub-85 BPM tracks (boom-bap) will get wrongly doubled and need the manual path.
- **OAuth user flow (not Client Credentials).** Spotify tightened Client Credentials access to `playlist_items` in 2025 — it now returns 401 even on public playlists. We use `SpotifyOAuth` with scopes `playlist-read-private playlist-read-collaborative`. This reads both public and private playlists owned by OR accessible to the authenticated user. Editorial/algorithmic playlists (IDs starting `37i9dQZF1...`) still 404 — that's a separate access tier. **YouTube / SoundCloud URLs don't need any auth at all.**
- **`spotify_id` column is a misnomer.** It's a generic primary key. Spotify tracks are raw Spotify IDs; YouTube entries are `"yt:<video_id>"`; SoundCloud are `"sc:<track_id>"`. Namespaced to prevent collisions across sources. Do not "clean up" by splitting into separate columns — it would break the existing `--skip-existing` dedup path.
- **Local analysis only.** We do not call Spotify Audio Features or any paid BPM/key API. See `docs/GOTCHAS.md` for why.
- **Windows-first.** The dev env is Windows 11. Paths use `pathlib`; filename sanitization strips `<>:"/\|?*` and control chars. stdout/stderr are reconfigured to UTF-8 at startup because the default cp1252 codepage can't print most track titles or the `→` progress arrows.
- **`--skip-existing` recovers from disk, not just CSV.** If `index.csv` is missing/corrupt but MP3s exist, the flag reconstructs rows from ID3 tags (BPM, Camelot) so you don't re-download 184 tracks. The `key` (full name) and `source` columns are lost in the reconstruction — that's OK, Rekordbox only reads BPM + Camelot.
- **CSV writes are atomic.** Written to `index.csv.tmp` then `replace()`'d. Prevents a crash in the sort/write block from wiping a valid index.

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
