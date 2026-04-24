# Architecture

Pipeline details and module contracts. Read `CLAUDE.md` first.

## Pipeline

```
Spotify playlist URL
      │
      ▼  spotify_client.get_playlist_tracks()   (OAuth, caches to .spotify_cache)
list[Track]  (id, title, artists, album, duration_ms, isrc)
      │
      ▼  [if --skip-existing: load index.csv + reconstruct from ID3 tags]
      │
      ▼  for each remaining track:
      │    for source in sources ("youtube,soundcloud" by default):
      │      downloader.download_track(query, tmp_dir, source)
      │      if hit → break
      │    else → append to failures list
tmp_dir/<video_id>.mp3
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

`out_dir = <args.out>/<sanitized_playlist_name>/`. The per-track `tmp_dir` is `out_dir/_tmp/` and is deleted at the end.

## Module contracts

### `spotify_client.py`

- `Track` — dataclass. `search_query` property builds `"primary_artist - title"` (the yt-dlp search string).
- `get_playlist_tracks(url, cid, secret) -> (playlist_name, list[Track])` — paginates `playlist_items` via `sp.next()`. Skips items where `track` is None or missing an ID (Spotify returns these for unavailable/local tracks).
- Uses `spotipy.SpotifyOAuth` with scopes `playlist-read-private playlist-read-collaborative`. First run opens a browser for user authorization, caches token to `.spotify_cache`. Why not Client Credentials? See `docs/GOTCHAS.md` — Spotify started returning 401 on `playlist_items` for Client Credentials in 2025.

### `downloader.py`

- `download_track(query, out_dir, source) -> Path | None` — returns final mp3 path or `None` on failure. Never raises; failures print a `!` line.
- Uses yt-dlp's `default_search` with `ytsearch1:` or `scsearch1:` (first result only). If you need more matches, change `1` → `N` and filter in Python.
- Postprocessor pins output to mp3 @ 320k. Changing to FLAC/M4A: swap `preferredcodec`. Rekordbox handles all three; mp3 is the most portable.
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
