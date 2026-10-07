"""Serializable transcript contracts and deterministic timestamp reconciliation."""

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if not all(math.isfinite(t) for t in (self.start, self.end)) or not 0 <= self.start < self.end:
            raise ValueError("Word timestamps must be finite, positive-duration intervals")


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str

    def __post_init__(self) -> None:
        if not all(math.isfinite(t) for t in (self.start, self.end)) or not 0 <= self.start < self.end:
            raise ValueError("Speaker timestamps must be finite, positive-duration intervals")
        if not self.speaker:
            raise ValueError("Speaker ID cannot be empty")


@dataclass(frozen=True)
class Segment:
    id: str
    start: float
    end: float
    text: str
    speaker: str | None
    ambiguous: bool
    overlap: bool

    def to_dict(self) -> dict:
        return asdict(self)


def reconcile(words: list[Word], turns: list[Turn]) -> list[Segment]:
    """Assign words by coverage, retaining unknown and simultaneous speakers."""
    result = []
    for index, word in enumerate(words):
        matches: dict[str, float] = {}
        intersecting = []
        for turn in turns:
            overlap = max(0, min(word.end, turn.end) - max(word.start, turn.start))
            if overlap:
                matches[turn.speaker] = matches.get(turn.speaker, 0) + overlap
                intersecting.append(turn)
        ranked = sorted(matches, key=lambda speaker: (-matches[speaker], speaker))
        speaker = ranked[0] if ranked else None
        overlap = any(
            left.speaker != right.speaker
            and max(word.start, left.start, right.start) < min(word.end, left.end, right.end)
            for i, left in enumerate(intersecting) for right in intersecting[i + 1:]
        )
        ambiguous = (speaker is None or overlap or len(ranked) > 1
                     or matches.get(speaker, 0) < (word.end - word.start) * 0.5)
        result.append(Segment(f"s{index + 1:06d}", word.start, word.end,
                              word.text.strip(), speaker, ambiguous, overlap))
    return result
