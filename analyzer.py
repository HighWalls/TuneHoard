"""BPM + musical key detection from an audio file.

BPM: librosa beat tracker.
Key: Krumhansl-Schmuckler profiles correlated against the mean chroma vector.
"""

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np

from camelot import NOTE_NAMES, to_camelot

# Krumhansl-Schmuckler key profiles.
_MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
_MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)


@dataclass
class Analysis:
    bpm: int
    key_name: str     # e.g. "A minor"
    camelot: str      # e.g. "8A"


def _detect_key(y: np.ndarray, sr: int) -> tuple[str, str]:
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    mean_chroma = chroma.mean(axis=1)

    major_scores = np.array(
        [np.corrcoef(np.roll(_MAJOR_PROFILE, i), mean_chroma)[0, 1] for i in range(12)]
    )
    minor_scores = np.array(
        [np.corrcoef(np.roll(_MINOR_PROFILE, i), mean_chroma)[0, 1] for i in range(12)]
    )

    if major_scores.max() >= minor_scores.max():
        pc = int(major_scores.argmax())
        mode = "major"
    else:
        pc = int(minor_scores.argmax())
        mode = "minor"

    root = NOTE_NAMES[pc]
    return f"{root} {mode}", to_camelot(root, mode)


def _detect_bpm(y: np.ndarray, sr: int) -> int:
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    # Normalise obvious half/double-time errors toward the DJ-useful 70-180 range.
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return int(round(bpm))


def analyze(mp3_path: Path) -> Analysis:
    y, sr = librosa.load(str(mp3_path), sr=22050, mono=True, duration=120)
    bpm = _detect_bpm(y, sr)
    key_name, camelot = _detect_key(y, sr)
    return Analysis(bpm=bpm, key_name=key_name, camelot=camelot)
