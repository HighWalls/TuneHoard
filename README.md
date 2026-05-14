# TuneHoard

Download Spotify, YouTube, and SoundCloud playlists or tracks as 320 kbps MP3s
with BPM and musical key auto-detected, tagged into ID3 frames, and organized
into BPM-range subfolders. Built for DJ prep — the output drops straight into
Rekordbox, Traktor, or Serato.

## Quick start (no terminal, no Python)

**1. Download the binary**

Grab the latest release for your OS from the
[Releases page](https://github.com/HighWalls/TuneHoard/releases/latest):

- Windows → `TuneHoard-windows-x64.exe`
- macOS → `TuneHoard-macos-x64.zip` (unzip first, then run the `TuneHoard-macos-x64` file)

ffmpeg is bundled inside the binary — no separate install.

**2. Run it**

Double-click the binary. A terminal window opens (logs scroll there) and your
default browser opens to the dashboard at <http://127.0.0.1:8765>.

On macOS, the first launch may show "TuneHoard can't be opened because Apple
cannot check it for malicious software." Right-click → **Open** → confirm.
The binary is unsigned (no Apple Developer cert); this is normal for indie tools.

**3. Pick a library folder**

The welcome modal asks for an output folder. Pick or create one — everything
downloaded from now on lands there, organized by BPM bucket. You can change
the folder later in Settings.

**4. Paste a URL**

YouTube, SoundCloud, or Spotify, playlist or single track. Hit `[ Download >> ]`.
Live progress appears in the Active Jobs panel.

YouTube and SoundCloud work immediately. For Spotify, see the next section.

## Spotify setup (5 minutes, one-time)

TuneHoard doesn't ship pre-baked Spotify credentials — Spotify rate-limits per
app, so a shared key would burn out within a few users. You create your own
free app instead. It's a one-time thing:

**1. Make a Spotify Developer app**

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   and sign in with the Spotify account whose playlists you want to download.
2. Click **Create app**. Name it anything ("TuneHoard personal" is fine). Tick
   the **Web API** checkbox under "Which API/SDKs are you planning to use?".
3. Under **Redirect URIs**, add **exactly** this:
   ```
   http://127.0.0.1:8888/callback
   ```
   ⚠️ Use the literal IP `127.0.0.1`, not `localhost` — Spotify stopped
   accepting `localhost` in 2025.
4. Save the app. On the app's page, click **Settings** → copy the
   **Client ID** and **Client secret**.

**2. Paste them into TuneHoard**

In the dashboard's Settings page (top-right `[Settings]` link):

- **Client ID** → paste, click somewhere else (auto-saves on blur)
- **Client Secret** → paste, blur
- **`[ Authorize ]`** → opens your browser; click *Agree*, you're redirected
  back, the page closes, and the status pill flips to green

You can now paste Spotify URLs into the URL bar. `[ Browse Spotify ]` appears
next to `[ Download >> ]` for picking from your saved playlists + Liked Songs.

> The Spotify app's default state is **Development mode**, capped at 25 named
> users. If only you're using your TuneHoard, that's fine. If you share it
> with friends, add their Spotify emails under your Spotify app's **User
> Management** page, or apply for an extended quota.

## Features

- **Sources**: Spotify, YouTube, SoundCloud — playlists or single tracks/videos
- **`spotify:liked`** virtual playlist — downloads your entire Liked Songs
- **Local analysis** — librosa BPM detection + Krumhansl-Schmuckler musical key
- **Tags** — `TBPM`, `TKEY`, `TXXX:CAMELOT_KEY`, `TXXX:MUSICAL_KEY`, `COMM`,
  `TIT2/TPE1/TALB`. Camelot (`8A`) or musical (`Am`) format choice.
- **Filename** — `8A - 128 - Artist - Title.mp3` (sortable in any file browser)
- **BPM buckets** — tracks land in `115-125/`, `126-135/`, ... subfolders
  (anchor band 11-wide, others 10-wide). Toggleable to flat layout.
- **Audio preview** — per-track ▶ play button, scrubbable now-playing bar at
  the bottom of the page, keyboard shortcuts (Space, ←/→, Esc)
- **Dashboard editing** — inline BPM/Camelot/Key correction, bulk Analyze
  / Delete / Move, drag-drop between buckets, right-click bucket menu
- **Library import** — `[ Scan folder ]` indexes pre-existing MP3 collections
  from filenames + ID3 tags. Optional background analyze-missing pass.
- **WebSocket-pushed job progress** with 10-second HTTP polling fallback
- **Failed-track recovery** — failures.txt panel with `[retry]` and `[dismiss]`
- **Atomic CSV writes** + ID3-tag recovery if the index is lost
- **Auto-update notifications** when a newer GitHub release is published

## Troubleshooting

### Downloads silently fail (no MP3 lands)

If you're running the **packaged binary**: ffmpeg is bundled, this shouldn't
happen. File a bug with the contents of the terminal window.

If you're running **from source**: ffmpeg isn't on PATH. Either install it
system-wide (`winget install ffmpeg`, `brew install ffmpeg`, or
`sudo apt install ffmpeg`) and restart the dashboard, or in Settings →
Advanced → ffmpeg path, point at `ffmpeg.exe` directly.

### Spotify authorization fails

Most common cause: the redirect URI on your Spotify app isn't registered
exactly as `http://127.0.0.1:8888/callback`. Specifically:

- Must be `127.0.0.1`, not `localhost` (Spotify policy change in 2025)
- Must use port `8888`, not the dashboard's port `8765`
- Must end in `/callback`, no trailing slash after that

After fixing the redirect URI, delete the `.spotify_cache` file in the project
root (binary builds keep it next to the executable) and re-authorize.

Other failure modes:

- **Client Secret typo** — the `[show]` link in Settings reveals what you
  pasted. The secret looks like 32 hex characters.
- **Development-mode user cap** — if you're using someone else's Client ID,
  your Spotify email needs to be added to that app's User Management list.
- **Editorial playlists (URLs starting `37i9dQZF1...`)** still 404 — that's a
  separate Spotify access tier no third-party app has. Copy the playlist into
  a personal one and download from there.

### Wrong BPM detected (half-time / double-time)

The BPM detector clamps to `[85, 200]` by default — anything below gets
doubled, anything above gets halved. Tuned for electronic / DJ libraries. If
your library is mostly hip-hop or boom-bap (genuine sub-85 BPM tracks), in
Settings → Advanced lower the `BPM clamp min` to `70` and re-run a job with
`[ Analyze ]` on the affected tracks.

The musical key detector is ~80-85% accurate by design (Krumhansl-Schmuckler
on chroma). Modal, atonal, and V-chord-heavy tracks routinely come out wrong.
Hand-correct via the inline edit form.

### "Port 8765 already in use"

TuneHoard auto-falls-back to 8766, 8767, ..., 8770. If all five are taken,
something else on your machine is hoarding the range — close those apps or
override with the `TUNEHOARD_PORT` environment variable.

### macOS "TuneHoard.app can't be opened"

Right-click → **Open** → confirm in the dialog. Subsequent launches work via
double-click. The binary is unsigned; signing requires a paid Apple Developer
account.

## Build from source

For contributors / users who want to run the latest code:

```bash
git clone https://github.com/HighWalls/TuneHoard.git
cd TuneHoard
python -m venv .venv
.venv/bin/activate          # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# install ffmpeg system-wide (one-time)
# Windows: winget install ffmpeg
# macOS:   brew install ffmpeg
# Linux:   sudo apt install ffmpeg

python server.py            # opens dashboard
# OR
python main.py "<url>"      # CLI
```

The CLI accepts the same source URLs as the dashboard. `python main.py --help`
lists every flag.

To produce a binary locally: `pip install pyinstaller && pyinstaller tunehoard.spec`
(without bundled ffmpeg unless you drop one in `bin/` first).

## Project layout

- `main.py` — CLI entry point + the actual download / analyze / tag pipeline
- `server.py` — FastAPI dashboard server, spawns `main.py` as subprocess jobs
- `dashboard/tunehoard/tunehoard.html` — single-file vanilla-JS dashboard
- `spotify_client.py` / `ytdlp_loader.py` — source-specific URL → track list
- `downloader.py` — yt-dlp wrapper
- `analyzer.py` — librosa BPM + chroma key detection
- `tagger.py` — mutagen ID3 writer
- `app_native.py` — optional pywebview entry point (native window)
- `tunehoard.spec` + `.github/workflows/build.yml` — PyInstaller build pipeline
- `docs/ARCHITECTURE.md` + `docs/GOTCHAS.md` — internals + design rationale

## License

MIT for the TuneHoard source code. Bundled in the binary:

- [ffmpeg](https://ffmpeg.org/) — LGPL v2.1+ (Windows gyan.dev essentials build / macOS evermeet.cx static build, dynamically linked)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — Unlicense / public domain
- [librosa](https://librosa.org/) — ISC
- [FastAPI](https://fastapi.tiangolo.com/) — MIT

Spotify, YouTube, and SoundCloud are trademarks of their respective owners.
TuneHoard is an independent tool and is not affiliated with or endorsed by any
of them. Use it to download content you have the right to download.
