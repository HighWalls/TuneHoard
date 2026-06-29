"""Write ID3 tags that DJ software reads: TBPM, TKEY, TIT2, TPE1, TALB, COMM.

Both key formats are always written to the file:
- TKEY: holds whichever format the user picked ('8A' or 'Am'). This is
  the frame Rekordbox/Traktor/Serato display by default.
- TXXX:CAMELOT_KEY: always the Camelot value, regardless of user choice.
- TXXX:MUSICAL_KEY: always the short musical value, regardless of user choice.
- COMM: human-readable summary with both forms.

This means the file is portable across DJ software no matter the user's
TKEY preference — every tool can find the format it wants in some frame.
"""

from pathlib import Path

from mutagen.id3 import COMM, ID3, ID3NoHeaderError, TALB, TBPM, TIT2, TKEY, TPE1, TXXX

from camelot import musical_key_short


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
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags["TALB"] = TALB(encoding=3, text=album)
    tags["TBPM"] = TBPM(encoding=3, text=str(bpm))

    musical = musical_key_short(key_name) if key_name else ""
    # In musical mode, fall back to camelot when the musical form is empty
    # (missing/odd key_name) so we never write a blank TKEY that DJ software
    # would display as "no key".
    tkey_value = (musical or camelot) if key_format == "musical" else camelot
    tags["TKEY"] = TKEY(encoding=3, text=tkey_value)

    if camelot:
        tags["TXXX:CAMELOT_KEY"] = TXXX(encoding=3, desc="CAMELOT_KEY", text=camelot)
    if musical:
        tags["TXXX:MUSICAL_KEY"] = TXXX(encoding=3, desc="MUSICAL_KEY", text=musical)

    tags["COMM"] = COMM(
        encoding=3, lang="eng", desc="", text=f"{camelot} | {bpm} BPM | {key_name}"
    )
    tags.save(mp3_path, v2_version=3)
