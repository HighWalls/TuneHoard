"""Pitch class → Camelot wheel notation."""

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_MAJOR = {
    "C": "8B", "C#": "3B", "D": "10B", "D#": "5B", "E": "12B", "F": "7B",
    "F#": "2B", "G": "9B", "G#": "4B", "A": "11B", "A#": "6B", "B": "1B",
}
_MINOR = {
    "C": "5A", "C#": "12A", "D": "7A", "D#": "2A", "E": "9A", "F": "4A",
    "F#": "11A", "G": "6A", "G#": "1A", "A": "8A", "A#": "3A", "B": "10A",
}


def to_camelot(root: str, mode: str) -> str:
    table = _MAJOR if mode == "major" else _MINOR
    return table[root]


def pc_to_name(pc: int) -> str:
    return NOTE_NAMES[pc % 12]


def musical_key_short(key_name: str) -> str:
    """Convert 'A minor' / 'C major' / 'C# minor' to ID3 TKEY format
    ('Am' / 'C' / 'C#m'). Format expected by Traktor / Serato displays."""
    parts = key_name.split()
    if len(parts) != 2:
        return key_name
    root, mode = parts
    # Match the mode case-insensitively. An exact `mode == "major"` test would
    # silently mislabel "C Major" / "C MAJOR" as minor ("Cm").
    return root if mode.strip().lower().startswith("maj") else f"{root}m"
