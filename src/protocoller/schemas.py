"""Serializable transcript contracts and deterministic timestamp reconciliation."""

from dataclasses import asdict, dataclass
import heapq
import math


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if (any(type(t) not in (int, float) for t in (self.start, self.end))
                or not all(math.isfinite(t) for t in (self.start, self.end)) or not 0 <= self.start < self.end):
            raise ValueError("Word timestamps must be finite, positive-duration intervals")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Word text cannot be empty")


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str

    def __post_init__(self) -> None:
        if (any(type(t) not in (int, float) for t in (self.start, self.end))
                or not all(math.isfinite(t) for t in (self.start, self.end)) or not 0 <= self.start < self.end):
            raise ValueError("Speaker timestamps must be finite, positive-duration intervals")
        if not isinstance(self.speaker, str) or not self.speaker.strip():
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
    candidate_speakers: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def reconcile(words: list[Word], turns: list[Turn]) -> list[Segment]:
    """Assign words by coverage, retaining unknown and simultaneous speakers."""
    result = {}
    ordered = sorted(turns, key=lambda turn: (turn.start, turn.end, turn.speaker))
    active, endings, cursor = {}, [], 0
    for index, word in sorted(enumerate(words), key=lambda item: (item[1].start, item[1].end)):
        while cursor < len(ordered) and ordered[cursor].start < word.end:
            turn = ordered[cursor]
            active[cursor] = turn
            heapq.heappush(endings, (turn.end, cursor))
            cursor += 1
        while endings and endings[0][0] <= word.start:
            _, expired = heapq.heappop(endings)
            del active[expired]
        matches: dict[str, float] = {}
        intersecting = []
        for turn in active.values():
            overlap = max(0, min(word.end, turn.end) - max(word.start, turn.start))
            if overlap:
                intersecting.append(turn)
        # Union duplicate/overlapping intervals for each voice; coverage cannot exceed a word.
        for voice in {turn.speaker for turn in intersecting}:
            intervals = sorted((max(word.start, turn.start), min(word.end, turn.end))
                               for turn in intersecting if turn.speaker == voice)
            covered, last = 0.0, word.start
            for start, end in intervals:
                covered += max(0, end - max(start, last))
                last = max(last, end)
            matches[voice] = covered
        ranked = sorted(matches, key=lambda speaker: (-matches[speaker], speaker))
        speaker = ranked[0] if ranked else None
        overlap = any(
            left.speaker != right.speaker
            and max(word.start, left.start, right.start) < min(word.end, left.end, right.end)
            for i, left in enumerate(intersecting) for right in intersecting[i + 1:]
        )
        ambiguous = (speaker is None or overlap or len(ranked) > 1
                     or matches.get(speaker, 0) < (word.end - word.start) * 0.5)
        result[index] = Segment(f"s{index + 1:06d}", word.start, word.end,
                                word.text.strip(), speaker, ambiguous, overlap,
                                tuple(sorted(matches)))
    return [result[index] for index in range(len(words))]
