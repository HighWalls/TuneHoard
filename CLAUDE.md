# CLAUDE.md

Primer for any agent working on this project. Read this first — it's short on purpose.

## What this is

A Python CLI that takes a public Spotify playlist URL, finds each track on YouTube or SoundCloud, downloads it as 320k MP3, analyzes BPM + musical key locally, and writes ID3 tags that Rekordbox reads. Output is organized per-playlist with a sorted `index.csv` for DJ prep.

## Run it

```bash
python main.py <spotify_playlist_url>
    [--sources youtube,soundcloud]  # comma list, tried in order (default)
    [--out downloads]                # output directory
    [--limit N]                      # only process first N tracks
    [--skip-existing]                # skip tracks already in index.csv or on disk
```

Requires a `.env` with `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` (see `.env.example`) and `ffmpeg` on PATH. Install deps with `pip install -r requirements.txt`. First run opens a browser for Spotify OAuth authorization; the token is cached in `.spotify_cache` for subsequent runs. The redirect URI registered in the Spotify dashboard must match the one in `spotify_client.py` (default `http://127.0.0.1:8888/callback`).

## File map

| File | Responsibility |
|---|---|
| `main.py` | CLI, orchestration, filename formatting, CSV export |
| `spotify_client.py` | Playlist URL → list of `Track` via spotipy (Client Credentials flow) |
| `downloader.py` | yt-dlp wrapper: `ytsearch1:` / `scsearch1:` → 320k mp3 |
| `analyzer.py` | librosa: BPM (beat tracker) + key (Krumhansl-Schmuckler on chroma) |
| `camelot.py` | `(root, mode) → Camelot notation` lookup (e.g. `"A", "minor" → "8A"`) |
| `tagger.py` | mutagen ID3 writer: `TBPM`, `TKEY` (Camelot), `TIT2/TPE1/TALB`, `COMM` |

Dataflow: `Spotify → Track → yt-dlp download (youtube then soundcloud on miss) → librosa analyze → mutagen tag → atomic rename → atomic CSV + failures.txt`.

Tracks that fail on every source are written to `failures.txt` alongside `index.csv` with their Spotify URLs for manual recovery.

## Conventions

- **Target: Rekordbox.** `TKEY` holds Camelot notation (`"8A"`), not musical (`"Am"`). Rekordbox displays this verbatim in its Key column. Don't change to musical without asking.
- **BPM is stored as int string in `TBPM`** (Rekordbox convention). The half/double-time normalizer in `analyzer.py` clamps to 70–180 BPM — this is intentional for DJ use, not a bug.
- **Filename pattern:** `{camelot} - {bpm:03d} - {artist} - {title}.mp3`. Sorts nicely in file browsers and doubles as a visual fallback if tags get stripped.
- **OAuth user flow (not Client Credentials).** Spotify tightened Client Credentials access to `playlist_items` in 2025 — it now returns 401 even on public playlists. We use `SpotifyOAuth` with scopes `playlist-read-private playlist-read-collaborative`. This reads both public and private playlists owned by OR accessible to the authenticated user. Editorial/algorithmic playlists (IDs starting `37i9dQZF1...`) still 404 — that's a separate access tier.
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
