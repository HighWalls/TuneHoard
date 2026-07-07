"""Write BPM + key + basic metadata tags that DJ software reads.

MP3 uses ID3 (TBPM/TKEY/TIT2/TPE1/TALB/COMM + TXXX:CAMELOT_KEY/MUSICAL_KEY).
Other formats get the equivalent fields so a mixed-format library is tagged
consistently:
- FLAC / OGG / Opus → Vorbis comments (bpm / key / initialkey / camelot_key / ...)
- M4A / MP4 / ALAC  → MP4 atoms (tmpo + freeform iTunes KEY/CAMELOT_KEY/...)
- WAV / AIFF        → embedded ID3 (same frames as MP3)

Both key formats are always recorded so the file is portable across DJ software:
TKEY/`key` holds the user's chosen format; CAMELOT_KEY / MUSICAL_KEY always hold
their respective forms. Unknown/unsupported formats are skipped gracefully (the
values still live in index.csv, so the dashboard shows them either way).
"""

from pathlib import Path

from mutagen.id3 import COMM, ID3, ID3NoHeaderError, TALB, TBPM, TIT2, TKEY, TPE1, TXXX

from camelot import musical_key_short


_VORBIS_EXTS = {".flac", ".ogg", ".oga", ".opus"}
_MP4_EXTS = {".m4a", ".mp4", ".m4b", ".aac", ".alac"}
_ID3_CONTAINER_EXTS = {".wav", ".aiff", ".aif"}  # carry ID3 inside the container


def _bpm_str(bpm) -> str:
    try:
        return str(int(round(float(bpm))))
    except (TypeError, ValueError):
        return ""


def _fill_id3(tags, *, title, artist, album, bpm, camelot, musical, tkey_value, comm) -> None:
    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags["TALB"] = TALB(encoding=3, text=album)
    tags["TBPM"] = TBPM(encoding=3, text=_bpm_str(bpm))
    tags["TKEY"] = TKEY(encoding=3, text=tkey_value)
    if camelot:
        tags["TXXX:CAMELOT_KEY"] = TXXX(encoding=3, desc="CAMELOT_KEY", text=camelot)
    if musical:
        tags["TXXX:MUSICAL_KEY"] = TXXX(encoding=3, desc="MUSICAL_KEY", text=musical)
    tags["COMM"] = COMM(encoding=3, lang="eng", desc="", text=comm)


def tag_file(
    mp3_path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    bpm: int,
    camelot: str,
    key_name: str,
    key_format: str = "camelot",
) -> None:
    path = Path(mp3_path)
    ext = path.suffix.lower()
    musical = musical_key_short(key_name) if key_name else ""
    # In musical mode, fall back to camelot when the musical form is empty
    # (missing/odd key_name) so we never write a blank TKEY / key field.
    tkey_value = (musical or camelot) if key_format == "musical" else camelot
    comm = f"{camelot} | {bpm} BPM | {key_name}"

    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        _fill_id3(tags, title=title, artist=artist, album=album, bpm=bpm,
                  camelot=camelot, musical=musical, tkey_value=tkey_value, comm=comm)
        tags.save(path, v2_version=3)
        return

    # Non-MP3: best-effort per format. A tagging failure on an odd file must not
    # abort the surrounding operation — the analysis result is already stored in
    # index.csv, so the dashboard still shows BPM/key.
    try:
        import mutagen

        if ext in _ID3_CONTAINER_EXTS:
            audio = mutagen.File(path)
            if audio is None:
                return
            if audio.tags is None:
                audio.add_tags()
            _fill_id3(audio.tags, title=title, artist=artist, album=album, bpm=bpm,
                      camelot=camelot, musical=musical, tkey_value=tkey_value, comm=comm)
            audio.save()
            return

        if ext in _VORBIS_EXTS:
            audio = mutagen.File(path)
            if audio is None:
                return

            def setv(k, v):
                if v not in ("", None):
                    audio[k] = [str(v)]

            setv("title", title)
            setv("artist", artist)
            setv("album", album)
            setv("bpm", _bpm_str(bpm))
            setv("key", tkey_value)
            setv("initialkey", tkey_value)
            setv("camelot_key", camelot)
            setv("musical_key", musical)
            setv("comment", comm)
            audio.save()
            return

        if ext in _MP4_EXTS:
            from mutagen.mp4 import MP4, MP4FreeForm

            audio = MP4(path)
            if title:
                audio["\xa9nam"] = [title]
            if artist:
                audio["\xa9ART"] = [artist]
            if album:
                audio["\xa9alb"] = [album]
            b = _bpm_str(bpm)
            if b:
                audio["tmpo"] = [int(b)]
            if comm:
                audio["\xa9cmt"] = [comm]

            def free(name, val):
                if val:
                    audio["----:com.apple.iTunes:" + name] = [
                        MP4FreeForm(str(val).encode("utf-8"))
                    ]

            free("KEY", tkey_value)
            free("CAMELOT_KEY", camelot)
            free("MUSICAL_KEY", musical)
            audio.save()
            return
    except Exception as e:
        print(f"  ! tag write skipped for {path.name} ({type(e).__name__}: {e})")
        return

    # Unknown/unsupported container: leave the file untouched (values stay in CSV).
    return
