"""Write ID3 tags that Rekordbox reads: TBPM, TKEY (Camelot), TIT2, TPE1, TALB, COMM."""

from pathlib import Path

from mutagen.id3 import COMM, ID3, ID3NoHeaderError, TALB, TBPM, TIT2, TKEY, TPE1


def tag_file(
    mp3_path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    bpm: int,
    camelot: str,
    key_name: str,
) -> None:
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags["TALB"] = TALB(encoding=3, text=album)
    tags["TBPM"] = TBPM(encoding=3, text=str(bpm))
    # Rekordbox reads TKEY and displays it in the Key column as-is. Camelot sorts nicely.
    tags["TKEY"] = TKEY(encoding=3, text=camelot)
    tags["COMM"] = COMM(
        encoding=3, lang="eng", desc="", text=f"{camelot} | {bpm} BPM | {key_name}"
    )
    tags.save(mp3_path, v2_version=3)
