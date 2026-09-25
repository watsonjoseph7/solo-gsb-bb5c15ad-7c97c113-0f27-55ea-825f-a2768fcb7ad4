"""Request/response schemas for the beat-grid API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .grid import parse_nonnegative_rational, parse_positive_decimal, parse_rational


class EventIn(BaseModel):
    """One sound or rest event; duration is a positive rational string."""

    kind: Literal["sound", "rest"]
    duration: str = Field(examples=["1/4", "2", "3/8"])

    @field_validator("duration")
    @classmethod
    def duration_must_be_positive_rational(cls, value: str) -> str:
        try:
            parse_rational(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value


class VoiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    events: list[EventIn] = Field(min_length=1)


class MeasureIn(BaseModel):
    """A measure: target duration plus at least two voices."""

    target_duration: str = Field(examples=["4/4", "1", "3/2"])
    voices: list[VoiceIn] = Field(min_length=2)

    @field_validator("target_duration")
    @classmethod
    def target_must_be_positive_rational(cls, value: str) -> str:
        try:
            parse_rational(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("voices")
    @classmethod
    def voice_names_must_be_unique(cls, voices: list[VoiceIn]) -> list[VoiceIn]:
        names = [voice.name for voice in voices]
        if len(names) != len(set(names)):
            raise ValueError("voice names must be unique within a measure")
        return voices


class Mismatch(BaseModel):
    """Per-voice duration mismatch; difference = actual - expected."""

    voice: str
    expected: str
    actual: str
    difference: str


class EventOut(BaseModel):
    id: str
    kind: Literal["sound", "rest"]
    duration: str
    start: str
    end: str


class VoiceOut(BaseModel):
    name: str
    events: list[EventOut]


class MeasureOut(BaseModel):
    id: int
    target_duration: str
    created_at: str
    voices: list[VoiceOut]


class SegmentOut(BaseModel):
    """Half-open segment [start, end); null means the voice rests."""

    start: str
    end: str
    events: dict[str, str | None]


class GridOut(BaseModel):
    measure_id: int
    target_duration: str
    voices: list[str]
    segments: list[SegmentOut]


class RhythmMatchOut(BaseModel):
    """One other voice rhythm-equivalent to the queried voice.

    ratio = candidate duration / source duration (one common positive
    rational for every event pair); shift is the cyclic offset such that
    candidate event j corresponds to source event (j + shift) mod N.
    """

    measure_id: int
    voice: str
    ratio: str
    shift: int


class RhythmMatchesOut(BaseModel):
    measure_id: int
    voice: str
    matches: list[RhythmMatchOut]


class LoopAlignmentIn(BaseModel):
    """Two saved measure ids looped together; either id may repeat."""

    a_measure_id: int = Field(examples=[1])
    b_measure_id: int = Field(examples=[2])


class ActiveLoopEvent(BaseModel):
    """A sounding event at some point of the repeated timeline."""

    event_id: str
    loop: int


class AlignmentSegmentOut(BaseModel):
    """Half-open segment [start, end); null means the voice rests."""

    start: str
    end: str
    a: dict[str, ActiveLoopEvent | None]
    b: dict[str, ActiveLoopEvent | None]


class LoopAlignmentOut(BaseModel):
    a_measure_id: int
    b_measure_id: int
    # First instant both loops restart together.
    horizon: str
    # Loop lengths and 1-based repetition counts reaching the horizon.
    length_a: str
    length_b: str
    repetitions: dict[str, int]
    voices: dict[str, list[str]]
    expanded_events: int
    segments: list[AlignmentSegmentOut]


class QuantizationPreviewIn(BaseModel):
    """Preview parameters: grid division count and per-point shift bound."""

    divisions: int = Field(ge=1, le=128, examples=[8])
    max_shift: str = Field(examples=["1/8"])

    @field_validator("max_shift")
    @classmethod
    def max_shift_must_be_nonnegative_rational(cls, value: str) -> str:
        try:
            parse_nonnegative_rational(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value


class QuantizedEventOut(BaseModel):
    """An original event requantized onto the tick grid."""

    id: str
    kind: Literal["sound", "rest"]
    start: str
    end: str
    duration: str


class QuantizedVoiceOut(BaseModel):
    name: str
    events: list[QuantizedEventOut]


class BoundaryMappingOut(BaseModel):
    """One shared original event boundary and its mapped grid point."""

    original: str
    tick: int
    quantized: str
    # quantized - original (may be negative).
    shift: str


class QuantizationPreviewOut(BaseModel):
    measure_id: int
    target_duration: str
    divisions: int
    grid_step: str
    max_shift: str
    total_abs_shift: str
    boundaries: list[BoundaryMappingOut]
    voices: list[QuantizedVoiceOut]


class AvoidancePreviewIn(BaseModel):
    """Two different voice names of the same saved measure to disentangle."""

    voice_a: str = Field(min_length=1, max_length=100, examples=["melody"])
    voice_b: str = Field(min_length=1, max_length=100, examples=["bass"])


class ConflictEventOut(BaseModel):
    """One sound event involved in a conflict, with its voice name."""

    id: str
    voice: str
    start: str
    end: str
    duration: str


class ConflictPairOut(BaseModel):
    """Two sound events whose half-open intervals overlap in positive length."""

    a: ConflictEventOut
    b: ConflictEventOut
    overlap: str


class MutedEventOut(BaseModel):
    """One whole sound event chosen for muting, with its original duration."""

    id: str
    voice: str
    start: str
    end: str
    duration: str


class AvoidancePreviewOut(BaseModel):
    measure_id: int
    target_duration: str
    voice_a: str
    voice_b: str
    # Whole events to mute; the sum of their durations is minimum.
    muted: list[MutedEventOut]
    total_muted: str
    # Every original positive-length sound/sound conflict.
    conflicts: list[ConflictPairOut]


class TempoSegmentIn(BaseModel):
    """One tempo segment: bpm runs linearly from start_bpm to end_bpm.

    ``end`` is the segment's end offset (whole note = 1); the segment
    starts at 0 or at the previous segment's end. BPM values are
    positive decimal strings counting quarter notes per minute.
    """

    end: str = Field(examples=["1/2"])
    start_bpm: str = Field(examples=["120"])
    end_bpm: str = Field(examples=["90"])

    @field_validator("end")
    @classmethod
    def end_must_be_positive_rational(cls, value: str) -> str:
        try:
            parse_rational(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("start_bpm", "end_bpm")
    @classmethod
    def bpm_must_be_positive_decimal(cls, value: str) -> str:
        try:
            parse_positive_decimal(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value


class TimePreviewIn(BaseModel):
    """Tempo segments chaining from offset 0 to the target duration."""

    segments: list[TempoSegmentIn] = Field(min_length=1)


class TimedEventOut(BaseModel):
    """An event with its boundaries integrated to seconds (6 decimals)."""

    id: str
    kind: Literal["sound", "rest"]
    start_seconds: str
    end_seconds: str


class TimedVoiceOut(BaseModel):
    name: str
    events: list[TimedEventOut]


class TimePreviewOut(BaseModel):
    measure_id: int
    target_duration: str
    # Seconds from the measure start to the final segment end.
    total_seconds: str
    voices: list[TimedVoiceOut]
