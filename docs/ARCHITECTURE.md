# Architecture

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
      ▼  analyzer.analyze(mp3_path)   (loads first 120s @ 22050 Hz mono)
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
- Uses `spotipy.SpotifyOAuth` with scopes `playlist-read-private playlist-read-collaborative`. First run opens a browser for user authorization, caches token to `.spotify_cache`. Why not Client Credentials? See `docs/GOTCHAS.md` — Spotify started returning 401 on `playlist_items` for Client Credentials in 2025.

### `ytdlp_loader.py`

- `get_ytdlp_tracks(url, id_prefix) -> (folder_name, list[Track])` — handles both playlists and single videos/tracks. Detects "single" vs "playlist" by whether the yt-dlp `extract_info` response contains an `entries` key. Single tracks always return folder name `"singles"`. `id_prefix` is `"yt"` or `"sc"` and namespaces the `spotify_id` column (`yt:VIDEO_ID`, `sc:TRACK_ID`) so cross-source IDs can't collide.
- `_entry_to_track(entry, id_prefix)` — converts a yt-dlp entry to a `Track`. Best-effort parses `"Artist - Title"` from the video title (`_parse_artist_title`), falling back to the uploader/channel as artist. Strips common YouTube noise like `(Official Video)`, `[HD]`, `[Lyric]` from titles via `_NOISE_RE`.
- `is_youtube_url(url)` / `is_soundcloud_url(url)` — regex matchers used by `main.py` to dispatch.
- The `source_url` on each Track prefers `webpage_url` (canonical watch URL, stable) over `url` (sometimes a signed media URL that expires). Critical for single-video extracts, where `url` is the streaming endpoint.

### `downloader.py`

Two entry points sharing yt-dlp configuration via `_base_opts(out_dir)`:

- `download_url(url, out_dir) -> Path | None` — direct download from a known video/track URL. Used for YouTube/SoundCloud playlist entries where `Track.source_url` is set. No search.
- `download_track(query, out_dir, source) -> Path | None` — search-based download. Used for Spotify entries (no direct URL available). Uses yt-dlp's `default_search` with `ytsearch1:` or `scsearch1:` (first result only). If you need more matches, change `1` → `N` and filter in Python.
- Both: postprocessor pins output to mp3 @ 320k. Changing to FLAC/M4A: swap `preferredcodec`. Rekordbox handles all three; mp3 is the most portable.
- Output template `%(id)s.%(ext)s` — uses the platform's track ID so collisions are impossible within a source. Final rename happens in `main.py`.

### `analyzer.py`

- `analyze(mp3_path) -> Analysis` — loads mono @ 22050 Hz, **first 120s only**. That's enough for stable BPM/chroma and keeps per-track analysis under ~3s.
- `_detect_bpm`: `librosa.beat.beat_track` returns tempo in BPM. Half/double-time correction clamps to `[70, 180]` by doubling/halving. Genuine outliers (60 BPM ambient, 200 BPM DnB) will be misclassified — change the bounds in `analyzer.py` if the target genre needs it.
- `_detect_key`: mean chroma over the track, then correlate against 12 rotations of Krumhansl-Schmuckler major + minor profiles. Highest correlation wins mode and root.
- Algorithm ceiling is ~80–85% accurate. Common failures: tracks with strong V chord emphasis mis-classified as V instead of I, modal/atonal tracks, very bass-heavy tracks where chroma is dominated by harmonics.

### `camelot.py`

- Two dicts: `_MAJOR` and `_MINOR`, keyed by root note name (`"C"`, `"C#"`, ..., `"B"`). Values are Camelot strings (`"8B"`, `"8A"`, etc.).
- `to_camelot(root, mode)` — `mode` is `"major"` or `"minor"`.
- The Camelot wheel is a DJ convention: adjacent numbers are a perfect fifth apart, same number + letter swap is the relative major/minor. Mixing is "safe" within ±1 number or across the A/B swap.

### `tagger.py`

- `tag_file(mp3_path, ...)` — writes `TIT2` (title), `TPE1` (artist), `TALB` (album), `TBPM` (int string), `TKEY` (Camelot), `COMM` (human-readable summary).
- Saves as ID3v2.3 (`v2_version=3`) — Rekordbox supports both v2.3 and v2.4 but v2.3 has wider compatibility.
- Creates new `ID3()` if the file has no header (fresh MP3 from yt-dlp often does). Catches `ID3NoHeaderError` only — other errors should propagate.

### `main.py`

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
