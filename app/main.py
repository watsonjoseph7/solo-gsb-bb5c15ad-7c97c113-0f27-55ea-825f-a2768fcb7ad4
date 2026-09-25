"""Ensemble beat-grid API.

Submit a measure (target duration + >= 2 voices of sound/rest events
with positive rational durations). Durations are accumulated as exact
fractions; a voice that does not fill the target exactly is reported
with its difference and nothing is saved. Valid measures are stored in
SQLite and their beat grid — half-open segments over the union of all
event boundaries — can be queried by id.

Two saved measures can also be looped together (POST /loop-alignments):
the service computes the first instant both loops restart together and
returns the repeated timeline cut at every event boundary, tagged with
each sounding event's original id and its 1-based loop number.

A saved measure's event boundaries can finally be quantized onto an
even tick grid (POST /measures/{id}/quantization-preview): all distinct
boundaries across voices are mapped to ticks of a target-division grid,
endpoints fixed, staying strictly increasing within a per-point shift
bound, minimizing total absolute shift. The result is a preview only;
nothing is written back to SQLite.

A saved voice can also be used as a rhythm query
(GET /measures/{id}/rhythm-matches): other saved voices whose
sound/rest pattern is the same sequence up to a cyclic shift and a
single uniform tempo ratio are returned with that ratio and the
smallest matching shift, all compared in exact fractions.

Two voices of one saved measure can finally be disentangled
(POST /measures/{id}/avoidance-preview): sound events of the two
voices that overlap in positive length are conflicts; the service
finds a set of whole events to mute minimizing the sum of their
original durations (a minimum-weight vertex cover over the bipartite
conflict graph, solved in exact fractions). It is a preview only;
nothing is written back to SQLite.

A saved measure's event offsets can also be previewed in wall-clock
seconds (POST /measures/{id}/time-preview): the request gives a chain
of tempo segments — each an end offset plus start/end BPM (quarter
notes per minute, linear within the segment, jumps allowed between
segments) — covering the target duration. Every event boundary is
integrated to seconds from the measure start (whole note = 1); each
distinct offset is converted once and the same value reused. Results
carry six decimal places. It is a preview only; nothing is written
back to SQLite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from fractions import Fraction

from fastapi import FastAPI, HTTPException

from . import db
from .grid import (
    MAX_EXPANDED_EVENTS,
    LoopAlignmentTooLarge,
    QuantizationInfeasible,
    TempoSegmentsInvalid,
    align_loops,
    avoidance_muting,
    build_tempo_segments,
    build_voice_events,
    compute_grid,
    compute_time_preview,
    find_rhythm_match,
    format_rational,
    format_seconds,
    parse_nonnegative_rational,
    parse_rational,
    quantize_measure,
    voice_total,
)
from .schemas import (
    AvoidancePreviewIn,
    AvoidancePreviewOut,
    GridOut,
    LoopAlignmentIn,
    LoopAlignmentOut,
    MeasureIn,
    MeasureOut,
    Mismatch,
    QuantizationPreviewIn,
    QuantizationPreviewOut,
    RhythmMatchesOut,
    TimePreviewIn,
    TimePreviewOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One command (docker compose up) therefore covers dependencies,
    # database initialization, and startup.
    db.init_db()
    yield


app = FastAPI(
    title="Ensemble Beat Grid API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
def root() -> dict:
    return {
        "service": "ensemble-beat-grid",
        "docs": "/docs",
        "endpoints": {
            "create_measure": "POST /measures",
            "get_measure": "GET /measures/{id}",
            "get_grid": "GET /measures/{id}/grid",
            "quantization_preview": "POST /measures/{id}/quantization-preview",
            "rhythm_matches": "GET /measures/{id}/rhythm-matches?voice={name}",
            "loop_alignments": "POST /loop-alignments",
            "avoidance_preview": "POST /measures/{id}/avoidance-preview",
            "time_preview": "POST /measures/{id}/time-preview",
        },
    }


@app.post("/measures", response_model=MeasureOut, status_code=201)
def create_measure(payload: MeasureIn) -> dict:
    target = parse_rational(payload.target_duration)

    prepared_voices = []
    mismatches: list[Mismatch] = []
    for voice_index, voice in enumerate(payload.voices):
        raw_events = [
            {"kind": event.kind, "duration": parse_rational(event.duration)}
            for event in voice.events
        ]
        total = voice_total(raw_events)
        if total != target:
            mismatches.append(
                Mismatch(
                    voice=voice.name,
                    expected=format_rational(target),
                    actual=format_rational(total),
                    difference=format_rational(total - target),
                )
            )
        prepared_voices.append(
            {"name": voice.name, "events": build_voice_events(voice_index, raw_events)}
        )

    if mismatches:
        # Nothing is saved: the whole measure is rejected.
        raise HTTPException(
            status_code=422,
            detail={
                "message": "voice durations do not add up to the target "
                "duration; measure was not saved",
                "mismatches": [m.model_dump() for m in mismatches],
            },
        )

    measure_id = db.insert_measure(format_rational(target), prepared_voices)
    stored = db.load_measure(measure_id)
    assert stored is not None
    return stored


@app.get("/measures/{measure_id}", response_model=MeasureOut)
def get_measure(measure_id: int) -> dict:
    measure = db.load_measure(measure_id)
    if measure is None:
        raise HTTPException(status_code=404, detail="measure not found")
    return measure


def _fraction_voices(measure: dict) -> tuple[Fraction, list[dict]]:
    """Rebuild exact Fraction offsets of a stored measure's events."""
    length = Fraction(measure["target_duration"])
    voices = []
    for voice in measure["voices"]:
        events = [
            {
                "id": event["id"],
                "kind": event["kind"],
                "duration": Fraction(event["duration"]),
                "start": Fraction(event["start"]),
                "end": Fraction(event["end"]),
            }
            for event in voice["events"]
        ]
        voices.append({"name": voice["name"], "events": events})
    return length, voices


@app.get("/measures/{measure_id}/grid", response_model=GridOut)
def get_grid(measure_id: int) -> dict:
    measure = db.load_measure(measure_id)
    if measure is None:
        raise HTTPException(status_code=404, detail="measure not found")

    _, voices = _fraction_voices(measure)
    segments = compute_grid(voices)
    return {
        "measure_id": measure["id"],
        "target_duration": measure["target_duration"],
        "voices": [voice["name"] for voice in measure["voices"]],
        "segments": [
            {
                "start": format_rational(segment["start"]),
                "end": format_rational(segment["end"]),
                "events": segment["events"],
            }
            for segment in segments
        ],
    }


@app.post(
    "/measures/{measure_id}/quantization-preview",
    response_model=QuantizationPreviewOut,
)
def quantization_preview(measure_id: int, payload: QuantizationPreviewIn) -> dict:
    # Read-only: the measure is loaded from SQLite and the quantized
    # result is computed in exact fractions, but never written back.
    measure = db.load_measure(measure_id)
    if measure is None:
        raise HTTPException(status_code=404, detail="measure not found")

    length, voices = _fraction_voices(measure)
    max_shift = parse_nonnegative_rational(payload.max_shift)
    try:
        result = quantize_measure(
            length, voices, payload.divisions, max_shift
        )
    except QuantizationInfeasible as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "no quantization of the measure's event boundaries "
                "onto the requested grid stays within max_shift; nothing was saved",
                "reason": exc.reason,
                "divisions": payload.divisions,
                "max_shift": format_rational(max_shift),
            },
        ) from exc

    return {
        "measure_id": measure["id"],
        "target_duration": measure["target_duration"],
        "divisions": result["divisions"],
        "grid_step": format_rational(result["grid_step"]),
        "max_shift": format_rational(result["max_shift"]),
        "total_abs_shift": format_rational(result["total_abs_shift"]),
        "boundaries": [
            {
                "original": format_rational(boundary["original"]),
                "tick": boundary["tick"],
                "quantized": format_rational(boundary["quantized"]),
                "shift": format_rational(boundary["shift"]),
            }
            for boundary in result["boundaries"]
        ],
        "voices": [
            {
                "name": voice["name"],
                "events": [
                    {
                        "id": event["id"],
                        "kind": event["kind"],
                        "start": format_rational(event["start"]),
                        "end": format_rational(event["end"]),
                        "duration": format_rational(event["duration"]),
                    }
                    for event in voice["events"]
                ],
            }
            for voice in result["voices"]
        ],
    }


@app.get(
    "/measures/{measure_id}/rhythm-matches",
    response_model=RhythmMatchesOut,
)
def get_rhythm_matches(measure_id: int, voice: str) -> dict:
    measure = db.load_measure(measure_id)
    if measure is None:
        raise HTTPException(status_code=404, detail="measure not found")
    if not any(stored["name"] == voice for stored in measure["voices"]):
        raise HTTPException(status_code=404, detail="voice not found in measure")

    _, source_voices = _fraction_voices(measure)
    source_events = next(
        stored["events"] for stored in source_voices if stored["name"] == voice
    )

    # Saved measures come back ordered by id; within each measure voices
    # keep their stored (original) position, which fixes result order.
    matches = []
    for candidate_measure in db.load_all_measures():
        _, candidate_voices = _fraction_voices(candidate_measure)
        for candidate_voice in candidate_voices:
            # Exclude the source voice itself, never a candidate.
            if (
                candidate_measure["id"] == measure_id
                and candidate_voice["name"] == voice
            ):
                continue
            match = find_rhythm_match(source_events, candidate_voice["events"])
            if match is not None:
                matches.append(
                    {
                        "measure_id": candidate_measure["id"],
                        "voice": candidate_voice["name"],
                        "ratio": format_rational(match["ratio"]),
                        "shift": match["shift"],
                    }
                )

    return {"measure_id": measure_id, "voice": voice, "matches": matches}


@app.post("/loop-alignments", response_model=LoopAlignmentOut)
def loop_alignments(payload: LoopAlignmentIn) -> dict:
    # Either id may be missing (they may also be identical); report
    # which one, and never fabricate a timeline.
    measure_a = db.load_measure(payload.a_measure_id)
    measure_b = db.load_measure(payload.b_measure_id)
    missing = [
        label
        for label, measure in (("a", measure_a), ("b", measure_b))
        if measure is None
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"measure not found: {' and '.join(missing)}",
        )
    assert measure_a is not None and measure_b is not None

    length_a, voices_a = _fraction_voices(measure_a)
    length_b, voices_b = _fraction_voices(measure_b)

    try:
        result = align_loops(
            length_a,
            voices_a,
            length_b,
            voices_b,
            max_events=MAX_EXPANDED_EVENTS,
        )
    except LoopAlignmentTooLarge as exc:
        # Rejected before any timeline is built: no segments output.
        raise HTTPException(
            status_code=422,
            detail={
                "message": "loop alignment expands to too many events; "
                "no timeline produced",
                "expanded_events": exc.expanded_events,
                "limit": exc.max_events,
                "a_measure_id": payload.a_measure_id,
                "b_measure_id": payload.b_measure_id,
                "horizon": format_rational(exc.horizon),
                "repetitions": {"a": exc.repetitions_a, "b": exc.repetitions_b},
                "event_counts": {
                    "a": exc.event_count_a,
                    "b": exc.event_count_b,
                },
            },
        ) from exc

    return {
        "a_measure_id": measure_a["id"],
        "b_measure_id": measure_b["id"],
        "horizon": format_rational(result["horizon"]),
        "length_a": format_rational(length_a),
        "length_b": format_rational(length_b),
        "repetitions": result["repetitions"],
        "voices": {
            "a": [voice["name"] for voice in voices_a],
            "b": [voice["name"] for voice in voices_b],
        },
        "expanded_events": result["expanded_events"],
        "segments": [
            {
                "start": format_rational(segment["start"]),
                "end": format_rational(segment["end"]),
                "a": segment["a"],
                "b": segment["b"],
            }
            for segment in result["segments"]
        ],
    }


@app.post(
    "/measures/{measure_id}/avoidance-preview",
    response_model=AvoidancePreviewOut,
)
def avoidance_preview(measure_id: int, payload: AvoidancePreviewIn) -> dict:
    # Read-only: conflicts and the minimum-duration set of whole events
    # to mute are computed in exact fractions, but SQLite is never
    # written back to.
    measure = db.load_measure(measure_id)
    if measure is None:
        raise HTTPException(status_code=404, detail="measure not found")

    if payload.voice_a == payload.voice_b:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "voice_a and voice_b must be two different voices",
                "voice_a": payload.voice_a,
                "voice_b": payload.voice_b,
            },
        )

    _, voices = _fraction_voices(measure)
    names = [voice["name"] for voice in voices]
    missing = [name for name in (payload.voice_a, payload.voice_b) if name not in names]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "voice not found in measure",
                "missing_voices": missing,
            },
        )

    voice_a = next(voice for voice in voices if voice["name"] == payload.voice_a)
    voice_b = next(voice for voice in voices if voice["name"] == payload.voice_b)
    result = avoidance_muting(voice_a, voice_b)

    def conflict_event(event: dict, voice_name: str) -> dict:
        return {
            "id": event["id"],
            "voice": voice_name,
            "start": format_rational(event["start"]),
            "end": format_rational(event["end"]),
            "duration": format_rational(event["duration"]),
        }

    return {
        "measure_id": measure["id"],
        "target_duration": measure["target_duration"],
        "voice_a": payload.voice_a,
        "voice_b": payload.voice_b,
        "muted": [
            {
                "id": entry["event"]["id"],
                "voice": entry["voice"],
                "start": format_rational(entry["event"]["start"]),
                "end": format_rational(entry["event"]["end"]),
                "duration": format_rational(entry["event"]["duration"]),
            }
            for entry in result["muted"]
        ],
        "total_muted": format_rational(result["total_muted"]),
        "conflicts": [
            {
                "a": conflict_event(conflict["a"], payload.voice_a),
                "b": conflict_event(conflict["b"], payload.voice_b),
                "overlap": format_rational(conflict["overlap"]),
            }
            for conflict in result["conflicts"]
        ],
    }


@app.post(
    "/measures/{measure_id}/time-preview",
    response_model=TimePreviewOut,
)
def time_preview(measure_id: int, payload: TimePreviewIn) -> dict:
    # Read-only: offsets are integrated against the tempo segments in
    # high-precision decimal arithmetic, but SQLite is never written to.
    measure = db.load_measure(measure_id)
    if measure is None:
        raise HTTPException(status_code=404, detail="measure not found")

    length, voices = _fraction_voices(measure)
    try:
        segments = build_tempo_segments(
            length,
            [segment.model_dump() for segment in payload.segments],
        )
    except TempoSegmentsInvalid as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "invalid tempo segments; nothing was saved",
                "reason": exc.reason,
                "target_duration": format_rational(length),
            },
        ) from exc

    result = compute_time_preview(voices, segments)
    return {
        "measure_id": measure["id"],
        "target_duration": measure["target_duration"],
        "total_seconds": format_seconds(result["total_seconds"]),
        "voices": [
            {
                "name": voice["name"],
                "events": [
                    {
                        "id": event["id"],
                        "kind": event["kind"],
                        "start_seconds": format_seconds(event["start_seconds"]),
                        "end_seconds": format_seconds(event["end_seconds"]),
                    }
                    for event in voice["events"]
                ],
            }
            for voice in result["voices"]
        ],
    }
