"""Core beat-grid logic.

Pure standard-library module: durations are exact ``fractions.Fraction``
values parsed from positive rational strings, and the grid is built from
the union of every voice's event boundaries.
"""

from __future__ import annotations

import math
import re
import sys
from bisect import bisect_right
from collections import deque
from fractions import Fraction
from math import gcd, lcm

# A positive rational: "3", "3/4", "12/8" ... (no sign, no decimals).
_RATIONAL_RE = re.compile(r"^(\d+)(?:/(\d+))?$")

KIND_SOUND = "sound"
KIND_REST = "rest"
KINDS = (KIND_SOUND, KIND_REST)

# Safety cap on the number of repeated event copies an alignment may
# expand to; over this the request is rejected and no timeline is built.
MAX_EXPANDED_EVENTS = 2000


def _parse_unsigned_rational(text: str) -> Fraction:
    """Shared parser for non-negative fraction strings (no sign)."""
    if not isinstance(text, str):
        raise ValueError(f"rational must be a string, got {type(text).__name__}")
    match = _RATIONAL_RE.match(text.strip())
    if not match:
        raise ValueError(f"invalid rational number: {text!r}")
    numerator = int(match.group(1))
    denominator = int(match.group(2)) if match.group(2) else 1
    if denominator == 0:
        raise ValueError(f"denominator must not be zero: {text!r}")
    return Fraction(numerator, denominator)


def parse_rational(text: str) -> Fraction:
    """Parse a positive rational string ("2", "3/4") into a Fraction.

    Raises ValueError on malformed input, zero denominators, or
    non-positive values.
    """
    value = _parse_unsigned_rational(text)
    if value <= 0:
        raise ValueError(f"duration must be positive, got {text!r}")
    return value


def parse_nonnegative_rational(text: str) -> Fraction:
    """Parse a non-negative rational string ("0", "2", "3/4") into a Fraction.

    Raises ValueError on malformed input or zero denominators.
    """
    return _parse_unsigned_rational(text)


def format_rational(value: Fraction) -> str:
    """Canonical string form of a Fraction ("3/4", "2", "0")."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def event_id(voice_index: int, event_index: int) -> str:
    """Deterministic event identifier, 1-based: v1e1, v1e2, v2e1, ..."""
    return f"v{voice_index + 1}e{event_index + 1}"


def build_voice_events(voice_index: int, events: list[dict]) -> list[dict]:
    """Attach id/start/end offsets to a voice's events.

    ``events`` items must have ``kind`` and a ``Fraction`` ``duration``.
    Offsets are cumulative exact fractions; event i spans the half-open
    interval [start, end).
    """
    built = []
    cursor = Fraction(0)
    for position, event in enumerate(events):
        end = cursor + event["duration"]
        built.append(
            {
                "id": event_id(voice_index, position),
                "kind": event["kind"],
                "duration": event["duration"],
                "start": cursor,
                "end": end,
                "position": position,
            }
        )
        cursor = end
    return built


def voice_total(events: list[dict]) -> Fraction:
    """Exact accumulated duration of built (or raw) events."""
    total = Fraction(0)
    for event in events:
        total += event["duration"]
    return total


def compute_grid(voices: list[dict]) -> list[dict]:
    """Build half-open time segments from the union of all boundaries.

    ``voices`` is a list of {"name": str, "events": [built events]}.
    Every voice must span the same total duration. Returns one segment
    per adjacent boundary pair [start, end); each segment maps voice
    names to the sounding event id, or None when the voice rests.

    Because segment edges are exactly the union of event edges, each
    segment lies fully inside exactly one event per voice: boundaries
    neither drop nor duplicate sound (half-open intervals: an event is
    active for start <= t < end).
    """
    points: set[Fraction] = set()
    for voice in voices:
        for event in voice["events"]:
            points.add(event["start"])
            points.add(event["end"])
    ordered = sorted(points)

    # Per voice, event end offsets are strictly increasing (durations
    # are positive), so bisect finds the event covering any segment.
    ends_per_voice = [[event["end"] for event in voice["events"]] for voice in voices]

    segments = []
    for index in range(len(ordered) - 1):
        start, end = ordered[index], ordered[index + 1]
        active: dict[str, str | None] = {}
        for voice, ends in zip(voices, ends_per_voice):
            slot = bisect_right(ends, start)
            event = voice["events"][slot]
            # The segment starts inside this event by construction.
            assert event["start"] <= start < event["end"]
            active[voice["name"]] = (
                event["id"] if event["kind"] == KIND_SOUND else None
            )
        segments.append({"start": start, "end": end, "events": active})
    return segments


def find_rhythm_match(
    source_events: list[dict], candidate_events: list[dict]
) -> dict | None:
    """Check whether a candidate voice is a cyclic re-tempo of a source.

    Each list holds built events with exact ``Fraction`` durations. The
    rhythms are considered equivalent when both sequences contain the
    same number N of events and some cyclic shift ``s`` in [0, N) makes,
    for every j, the candidate's j-th event correspond to the source's
    (j + s) mod N-th event:

    * the sound/rest kinds agree one by one, and
    * every candidate/source duration ratio equals the same positive
      rational (a uniform re-tempo, possibly by 1).

    Ratios are compared exactly with Fractions. Returns
    ``{"ratio": Fraction, "shift": int}`` for the smallest feasible
    shift (shifts are tried from 0 upward), or ``None`` when no shift
    works (including event-count mismatches).
    """
    count = len(source_events)
    if count == 0 or count != len(candidate_events):
        return None

    source_kinds = [event["kind"] for event in source_events]
    candidate_kinds = [event["kind"] for event in candidate_events]
    source_durations = [event["duration"] for event in source_events]
    candidate_durations = [event["duration"] for event in candidate_events]

    for shift in range(count):
        # Event durations are stored positive, so this ratio is positive.
        ratio = candidate_durations[0] / source_durations[shift]
        for j in range(count):
            source_index = (j + shift) % count
            if candidate_kinds[j] != source_kinds[source_index]:
                break
            if candidate_durations[j] != ratio * source_durations[source_index]:
                break
        else:
            return {"ratio": ratio, "shift": shift}
    return None


def rational_lcm(left: Fraction, right: Fraction) -> Fraction:
    """Least positive common multiple of two positive rationals.

    This is the first instant t > 0 at which two loops of lengths
    ``left`` and ``right`` are simultaneously back at their start:
    t = k * left = m * right for positive integers k, m.
    """
    return Fraction(
        lcm(left.numerator, right.numerator),
        gcd(left.denominator, right.denominator),
    )


class LoopAlignmentTooLarge(Exception):
    """Expansion would produce more than ``max_events`` event copies."""

    def __init__(
        self,
        *,
        horizon: Fraction,
        repetitions_a: int,
        repetitions_b: int,
        event_count_a: int,
        event_count_b: int,
        expanded_events: int,
        max_events: int,
    ) -> None:
        super().__init__(
            f"loop alignment expands to {expanded_events} events, "
            f"over the limit of {max_events}"
        )
        self.horizon = horizon
        self.repetitions_a = repetitions_a
        self.repetitions_b = repetitions_b
        self.event_count_a = event_count_a
        self.event_count_b = event_count_b
        self.expanded_events = expanded_events
        self.max_events = max_events


def align_loops(
    length_a: Fraction,
    voices_a: list[dict],
    length_b: Fraction,
    voices_b: list[dict],
    *,
    max_events: int = MAX_EXPANDED_EVENTS,
) -> dict:
    """Align two looping measures at their first common restart.

    Each measure is treated as a loop repeated from t = 0. The horizon
    is the first positive instant at which both loops restart together
    (the rational LCM of their lengths); events are repeated to cover
    [0, horizon). The timeline is cut into half-open segments by the
    union of every repeated event boundary. Per segment, every voice is
    tagged with the original event id and its 1-based loop number, or
    null while that voice rests.

    ``voices`` items are {"name": str, "events": [built events]} and
    each voice must span exactly its loop length. Raises
    LoopAlignmentTooLarge when the total number of repeated event copies
    (before any timeline is built) exceeds ``max_events``.
    """
    horizon = rational_lcm(length_a, length_b)
    reps_a = horizon // length_a
    reps_b = horizon // length_b

    def event_count(voices: list[dict]) -> int:
        return sum(len(voice["events"]) for voice in voices)

    count_a = event_count(voices_a)
    count_b = event_count(voices_b)
    expanded = reps_a * count_a + reps_b * count_b
    if expanded > max_events:
        raise LoopAlignmentTooLarge(
            horizon=horizon,
            repetitions_a=reps_a,
            repetitions_b=reps_b,
            event_count_a=count_a,
            event_count_b=count_b,
            expanded_events=expanded,
            max_events=max_events,
        )

    # Union of all repeated event boundaries. For loop r (0-based) and a
    # base measure boundary p the absolute time is r * length + p.
    points: set[Fraction] = set()
    for repetition in range(reps_a):
        origin = repetition * length_a
        for voice in voices_a:
            for event in voice["events"]:
                points.add(origin + event["start"])
                points.add(origin + event["end"])
    for repetition in range(reps_b):
        origin = repetition * length_b
        for voice in voices_b:
            for event in voice["events"]:
                points.add(origin + event["start"])
                points.add(origin + event["end"])
    ordered = sorted(points)

    def active_voices(length: Fraction, voices: list[dict], time: Fraction) -> dict:
        # time is a boundary strictly below horizon, hence it falls in
        # exactly one half-open event copy per voice.
        repetition = time // length
        offset = time - repetition * length
        result: dict[str, dict] = {}
        for voice in voices:
            ends = [event["end"] for event in voice["events"]]
            event = voice["events"][bisect_right(ends, offset)]
            # The boundary lies inside this event by construction.
            assert event["start"] <= offset < event["end"]
            result[voice["name"]] = (
                {"event_id": event["id"], "loop": repetition + 1}
                if event["kind"] == KIND_SOUND
                else None
            )
        return result

    segments = []
    for index in range(len(ordered) - 1):
        start, end = ordered[index], ordered[index + 1]
        segments.append(
            {
                "start": start,
                "end": end,
                "a": active_voices(length_a, voices_a, start),
                "b": active_voices(length_b, voices_b, start),
            }
        )

    return {
        "horizon": horizon,
        "repetitions": {"a": reps_a, "b": reps_b},
        "expanded_events": expanded,
        "segments": segments,
    }


class QuantizationInfeasible(Exception):
    """No strictly increasing tick mapping keeps every shift within bound."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"quantization infeasible: {reason}")
        self.reason = reason


def _floor_fraction(value: Fraction) -> int:
    """Floor of a Fraction: numerator // denominator (exact, any sign)."""
    return value.numerator // value.denominator


def _ceil_fraction(value: Fraction) -> int:
    """Ceiling of a Fraction: -floor(-value)."""
    return -((-value.numerator) // value.denominator)


def quantize_measure(
    length: Fraction,
    voices: list[dict],
    divisions: int,
    max_shift: Fraction,
) -> dict:
    """Quantize a saved measure's shared boundaries onto an even tick grid.

    The target grid divides ``length`` into ``divisions`` equal steps;
    every distinct event boundary of every voice is mapped to one grid
    tick. A feasible mapping must:

    * fix the first boundary (0) to tick 0 and the last boundary
      (``length``) to tick ``divisions``;
    * map shared boundaries (the same instant in several voices) to one
      tick, so the mapping stays consistent across voices;
    * stay strictly increasing with the boundary order;
    * move each boundary by at most ``max_shift`` in absolute value.

    Among all feasible mappings it minimizes the sum of absolute shifts;
    ties are broken by the lexicographically smallest tick sequence.
    Raises QuantizationInfeasible when no feasible mapping exists.

    ``voices`` items are {"name": str, "events": [built events]}. Returns
    the shared boundary mapping plus, per voice, every original event
    with its quantized start/end/duration.

    Optimization: let boundary p_i map to tick k_i. The cost is
    sum |k_i * length/divisions - p_i|, a sum of terms depending on one
    variable each, plus the strict-increase constraint k_{i-1} < k_i.
    A dynamic program over (boundary, tick) gives the minimum remaining
    cost suf[i][k]; ties are then resolved greedily by always choosing
    the smallest feasible tick, which yields the lexicographically
    smallest optimal tick sequence.
    """
    points: set[Fraction] = set()
    for voice in voices:
        for event in voice["events"]:
            points.add(event["start"])
            points.add(event["end"])
    boundaries = sorted(points)
    count = len(boundaries)

    assert boundaries[0] == Fraction(0)
    assert boundaries[-1] == length

    # Ticks are 0..divisions and mapped ticks must be strictly increasing,
    # so more distinct boundaries than ticks can never fit.
    if count > divisions + 1:
        raise QuantizationInfeasible("more distinct boundaries than grid ticks")

    step = length / divisions

    # Per-boundary tick windows [lower_i, upper_i] imposed by max_shift:
    # ticks k satisfying |k*step - point| <= max_shift, i.e.
    # (point - max_shift)/step <= k <= (point + max_shift)/step.
    windows: list[tuple[int, int]] = []
    for point in boundaries:
        lower = max(0, _ceil_fraction((point - max_shift) / step))
        upper = min(divisions, _floor_fraction((point + max_shift) / step))
        windows.append((lower, upper))
        if lower > upper:
            raise QuantizationInfeasible(
                f"boundary {format_rational(point)} has no grid tick "
                f"within max_shift {format_rational(max_shift)}"
            )

    # Greedy monotonic precheck: assign each boundary the smallest tick
    # above its predecessor inside its window. Failure certifies that no
    # strictly increasing mapping exists at all.
    previous = -1
    for index, (point, (lower, upper)) in enumerate(
        zip(boundaries, windows)
    ):
        tick = max(lower, previous + 1)
        if tick > upper:
            if index == count - 1:
                raise QuantizationInfeasible(
                    "not enough grid ticks to keep boundaries strictly increasing"
                )
            raise QuantizationInfeasible(
                f"boundary {format_rational(point)} cannot follow the "
                "preceding boundaries within max_shift"
            )
        previous = tick

    def shift_cost(index: int, tick: int) -> Fraction:
        return abs(tick * step - boundaries[index])

    # DP over boundaries from the end. rows[i][k] = minimum total shift
    # of boundaries i..n-1 given boundary i maps to tick k (with the
    # chain staying strictly increasing inside each window); None when
    # no completion exists. Rows have length divisions + 1.
    rows: list[list[Fraction | None]] = [
        [None] * (divisions + 1) for _ in range(count)
    ]

    last = count - 1
    # The final boundary is fixed to the last tick (precheck above has
    # certified that tick lies in its window).
    rows[last][divisions] = shift_cost(last, divisions)

    for index in range(last - 1, -1, -1):
        lower, upper = windows[index]
        row = rows[index]
        next_row = rows[index + 1]
        # suffix_min[k] = min(next_row[k..divisions]): the cheapest
        # strictly greater tick reachable from the tick chosen here.
        suffix_min: list[Fraction | None] = [None] * (divisions + 2)
        running: Fraction | None = None
        for tick in range(divisions, -1, -1):
            value = next_row[tick]
            if value is not None and (running is None or value < running):
                running = value
            suffix_min[tick] = running
        for tick in range(lower, upper + 1):
            candidate = suffix_min[tick + 1]
            if candidate is not None:
                row[tick] = shift_cost(index, tick) + candidate

    # The first boundary is fixed to tick 0 (it always lies in window 0;
    # None means the fixed endpoints leave no feasible middle chain).
    optimal_cost = rows[0][0]
    if optimal_cost is None:
        raise QuantizationInfeasible(
            "no strictly increasing tick mapping stays within max_shift"
        )

    # Greedy reconstruction: at each boundary pick the smallest tick that
    # can still complete an optimum. With earlier costs already fixed,
    # suffix equality is exactly the membership test for an optimal
    # completion, so the smallest such tick gives the lexicographically
    # smallest tick sequence.
    ticks = [0]
    spent = shift_cost(0, 0)
    for index in range(1, count):
        lower, upper = windows[index]
        chosen: int | None = None
        for tick in range(max(lower, ticks[-1] + 1), upper + 1):
            remaining = rows[index][tick]
            if remaining is not None and spent + remaining == optimal_cost:
                chosen = tick
                break
        if chosen is None:  # pragma: no cover - DP guarantees an optimum
            raise QuantizationInfeasible(
                "no strictly increasing tick mapping stays within max_shift"
            )
        ticks.append(chosen)
        spent += shift_cost(index, chosen)

    mapped = [tick * step for tick in ticks]

    quantized_voices = []
    index_of_point = {point: index for index, point in enumerate(boundaries)}
    for voice in voices:
        events_out = []
        for event in voice["events"]:
            # Shared boundaries share a mapped point by construction, so
            # coincident edges across voices never disagree.
            start = mapped[index_of_point[event["start"]]]
            end = mapped[index_of_point[event["end"]]]
            events_out.append(
                {
                    "id": event["id"],
                    "kind": event["kind"],
                    "start": start,
                    "end": end,
                    "duration": end - start,
                }
            )
        quantized_voices.append({"name": voice["name"], "events": events_out})

    return {
        "divisions": divisions,
        "grid_step": step,
        "max_shift": max_shift,
        "total_abs_shift": sum(
            abs(mapped_point - point)
            for point, mapped_point in zip(boundaries, mapped)
        ),
        "boundaries": [
            {
                "original": point,
                "tick": tick,
                "quantized": tick * step,
                "shift": tick * step - point,
            }
            for point, tick in zip(boundaries, ticks)
        ],
        "voices": quantized_voices,
    }


def find_sound_conflicts(
    voice_a: dict, voice_b: dict
) -> list[dict]:
    """Find every positive-length overlap between two voices' sounds.

    Each voice is {"name": str, "events": [built events]}; rest events
    never take part. Two half-open event intervals [start, end) conflict
    only when their intersection has strictly positive length, so events
    that merely meet at a boundary (one end == the other start) do not
    conflict. Returns conflict dicts ordered by (a position, b position):
    ``{"a", "b", "overlap"}`` with the two built sound events.
    """
    sounds_a = [
        event for event in voice_a["events"] if event["kind"] == KIND_SOUND
    ]
    sounds_b = [
        event for event in voice_b["events"] if event["kind"] == KIND_SOUND
    ]

    conflicts: list[dict] = []
    index_b = 0
    for event_a in sounds_a:
        # Skip B events that end at or before A starts (boundary contact
        # included: end == start is not a positive overlap).
        while index_b < len(sounds_b) and sounds_b[index_b]["end"] <= event_a["start"]:
            index_b += 1
        probe = index_b
        while probe < len(sounds_b) and sounds_b[probe]["start"] < event_a["end"]:
            event_b = sounds_b[probe]
            overlap = min(event_a["end"], event_b["end"]) - max(
                event_a["start"], event_b["start"]
            )
            if overlap > 0:
                conflicts.append(
                    {"a": event_a, "b": event_b, "overlap": overlap}
                )
            probe += 1
    return conflicts


class _FractionDinic:
    """Max flow on capacities kept as exact Fractions.

    Edges live in a flat table, added in forward/reverse pairs: the
    reverse of edge i is edge i ^ 1. All arithmetic stays in Fraction,
    so the resulting min-cut value is an exact rational.
    """

    def __init__(self, node_count: int) -> None:
        self.node_count = node_count
        self.adjacency: list[list[int]] = [[] for _ in range(node_count)]
        self.edges: list[dict] = []

    def add_edge(self, source: int, target: int, capacity: Fraction) -> None:
        forward_index = len(self.edges)
        self.edges.append({"to": target, "capacity": capacity})
        self.edges.append({"to": source, "capacity": Fraction(0)})
        self.adjacency[source].append(forward_index)
        self.adjacency[target].append(forward_index ^ 1)

    def max_flow(self, source: int, sink: int, limit: Fraction) -> Fraction:
        flow = Fraction(0)
        # The longest simple augmenting path has at most node_count edges;
        # keep the DFS comfortably off the recursion limit.
        previous_limit = sys.getrecursionlimit()
        needed = self.node_count + 50
        if needed > previous_limit:
            sys.setrecursionlimit(needed * 2)
        try:
            while flow < limit:
                levels = self._build_levels(source, sink)
                if levels is None:
                    break
                next_edge = [0] * self.node_count

                def augment(node: int, pushed: Fraction) -> Fraction:
                    if node == sink:
                        return pushed
                    outgoing = self.adjacency[node]
                    while next_edge[node] < len(outgoing):
                        edge_index = outgoing[next_edge[node]]
                        edge = self.edges[edge_index]
                        if (
                            edge["capacity"] > 0
                            and levels[edge["to"]] == levels[node] + 1
                        ):
                            sent = augment(
                                edge["to"], min(pushed, edge["capacity"])
                            )
                            if sent > 0:
                                edge["capacity"] -= sent
                                self.edges[edge_index ^ 1]["capacity"] += sent
                                return sent
                        next_edge[node] += 1
                    return Fraction(0)

                while flow < limit:
                    sent = augment(source, limit - flow)
                    if sent == 0:
                        break
                    flow += sent
        finally:
            if needed > previous_limit:
                sys.setrecursionlimit(previous_limit)
        return flow

    def _build_levels(self, source: int, sink: int) -> list[int] | None:
        levels = [-1] * self.node_count
        levels[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for edge_index in self.adjacency[node]:
                edge = self.edges[edge_index]
                if edge["capacity"] > 0 and levels[edge["to"]] < 0:
                    levels[edge["to"]] = levels[node] + 1
                    queue.append(edge["to"])
        return levels if levels[sink] >= 0 else None

    def reachable_from_source(self, source: int) -> set[int]:
        """Nodes reachable from source via residual edges after max flow."""
        seen = {source}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for edge_index in self.adjacency[node]:
                edge = self.edges[edge_index]
                if edge["capacity"] > 0 and edge["to"] not in seen:
                    seen.add(edge["to"])
                    queue.append(edge["to"])
        return seen


def avoidance_muting(voice_a: dict, voice_b: dict) -> dict:
    """Pick whole sound events to mute so no sound/sound overlap remains.

    The conflict graph is bipartite (one part per voice): a sound event
    is a vertex weighted by its own positive duration, and every
    positive-length overlap with an event of the other voice is an edge.
    Muting an event removes every incident conflict; events may never be
    split, so a feasible set of mutes is exactly a vertex cover. The
    minimum-weight vertex cover (weights = original event durations) is
    found exactly with a min-cut / max-flow reduction using Fraction
    capacities:

        source -> a-event, capacity duration
        b-event -> sink, capacity duration
        a-event -> b-event, capacity infinity for each conflict

    Vertices on the source side of a minimum cut are the chosen cover;
    total muted duration is the exact rational min-cut value. Rest
    events and boundary-only contacts are excluded. Returns
    ``{"conflicts": [...], "muted": [...], "total_muted": Fraction}``.
    """
    conflicts = find_sound_conflicts(voice_a, voice_b)

    sounds_a = [
        event for event in voice_a["events"] if event["kind"] == KIND_SOUND
    ]
    sounds_b = [
        event for event in voice_b["events"] if event["kind"] == KIND_SOUND
    ]
    index_a = {event["id"]: position for position, event in enumerate(sounds_a)}
    index_b = {event["id"]: position for position, event in enumerate(sounds_b)}

    total_sound = sum(
        (event["duration"] for event in sounds_a + sounds_b), Fraction(0)
    )

    source_node = 0
    a_nodes_start = 1
    b_nodes_start = a_nodes_start + len(sounds_a)
    sink_node = b_nodes_start + len(sounds_b)
    network = _FractionDinic(sink_node + 1)

    for position, event in enumerate(sounds_a):
        network.add_edge(
            source_node, a_nodes_start + position, event["duration"]
        )
    for position, event in enumerate(sounds_b):
        network.add_edge(
            b_nodes_start + position, sink_node, event["duration"]
        )
    # Infinity strictly larger than every finite cut (the sum of all
    # vertex weights) guarantees no conflict edge is ever cut.
    infinite = total_sound + 1
    for conflict in conflicts:
        network.add_edge(
            a_nodes_start + index_a[conflict["a"]["id"]],
            b_nodes_start + index_b[conflict["b"]["id"]],
            infinite,
        )

    network.max_flow(source_node, sink_node, limit=total_sound)
    reachable = network.reachable_from_source(source_node)

    # Min-cut cover: A vertices on the sink side, B vertices on the
    # source side. Stored voice order fixes the output ordering.
    muted: list[dict] = []
    for position, event in enumerate(sounds_a):
        if a_nodes_start + position not in reachable:
            muted.append({"voice": voice_a["name"], "event": event})
    for position, event in enumerate(sounds_b):
        if b_nodes_start + position in reachable:
            muted.append({"voice": voice_b["name"], "event": event})

    return {
        "conflicts": conflicts,
        "muted": muted,
        "total_muted": sum(
            (entry["event"]["duration"] for entry in muted), Fraction(0)
        ),
    }


# --- Tempo maps: integrating event offsets into seconds -------------------------

# A positive decimal string: "120", "90.5", "0.75" (no sign, no exponent,
# digits required on both sides of the optional decimal point).
_DECIMAL_RE = re.compile(r"^\d+(?:\.\d+)?$")

# Offset units are whole notes; BPM counts quarter notes per minute and a
# whole note holds 4 quarters, so at 1 BPM one offset unit lasts 240 s.
_SECONDS_PER_UNIT_AT_1_BPM = 240.0


def parse_positive_decimal(text: str) -> float:
    """Parse a positive decimal string ("120", "90.5") into a float.

    Raises ValueError on malformed input or non-positive values.
    """
    if not isinstance(text, str):
        raise ValueError(f"decimal must be a string, got {type(text).__name__}")
    if not _DECIMAL_RE.match(text.strip()):
        raise ValueError(f"invalid decimal number: {text!r}")
    value = float(text.strip())
    if value <= 0:
        raise ValueError(f"BPM must be positive, got {text!r}")
    return value


def format_seconds(value: float) -> str:
    """Six-decimal string of a seconds value ("0.500000")."""
    return f"{value:.6f}"


class TempoMapInvalid(Exception):
    """The tempo segments do not form a valid map over the measure."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid tempo segments: {reason}")
        self.reason = reason


def build_tempo_map(segments: list[dict], target: Fraction) -> list[dict]:
    """Validate raw tempo segments and attach implicit segment starts.

    ``segments`` items are {"end": Fraction, "start_bpm": float,
    "end_bpm": float}; a segment's start is 0 for the first segment and
    the previous segment's end otherwise, so the segments must tile
    [0, target] with strictly increasing ends and the last end equal to
    ``target`` (the measure's target duration). BPM may jump between
    segments. Returns the same segments with an explicit "start" offset.
    """
    if not segments:
        raise TempoMapInvalid("at least one tempo segment is required")
    prepared = []
    start = Fraction(0)
    for index, segment in enumerate(segments):
        end = segment["end"]
        if end <= start:
            raise TempoMapInvalid(
                f"segment {index + 1} ends at {format_rational(end)}, "
                f"not strictly after {format_rational(start)}"
            )
        prepared.append(
            {
                "start": start,
                "end": end,
                "start_bpm": segment["start_bpm"],
                "end_bpm": segment["end_bpm"],
            }
        )
        start = end
    if prepared[-1]["end"] != target:
        raise TempoMapInvalid(
            f"last segment ends at {format_rational(prepared[-1]['end'])}, "
            f"not at the target duration {format_rational(target)}"
        )
    return prepared


def _linear_bpm_seconds(start_bpm: float, end_bpm: float, span: float) -> float:
    """Seconds to cross ``span`` offset units, BPM linear in the offset.

    With BPM linear from ``start_bpm`` to ``end_bpm`` over the span, the
    exact integral of 240/BPM is 240 * span * ln(end/start)/(end-start)
    (span divided by the logarithmic mean of the endpoint BPMs); a
    constant BPM gives 240 * span / BPM.
    """
    if start_bpm == end_bpm:
        return _SECONDS_PER_UNIT_AT_1_BPM * span / start_bpm
    delta = end_bpm - start_bpm
    # log1p(delta/start) = ln(end/start), accurate even for tiny deltas.
    return (
        _SECONDS_PER_UNIT_AT_1_BPM
        * span
        * math.log1p(delta / start_bpm)
        / delta
    )


def offset_to_seconds(prepared: list[dict], offset: Fraction) -> float:
    """Seconds from the measure start to ``offset`` under the tempo map.

    ``prepared`` comes from build_tempo_map; whole segments before the
    offset are summed and the segment containing the offset contributes
    its partial integral (BPM interpolated linearly in the offset).
    """
    total = 0.0
    for segment in prepared:
        if offset > segment["end"]:
            total += _linear_bpm_seconds(
                segment["start_bpm"],
                segment["end_bpm"],
                float(segment["end"] - segment["start"]),
            )
            continue
        span = float(offset - segment["start"])
        full_span = float(segment["end"] - segment["start"])
        bpm_here = segment["start_bpm"] + (
            segment["end_bpm"] - segment["start_bpm"]
        ) * (span / full_span)
        return total + _linear_bpm_seconds(
            segment["start_bpm"], bpm_here, span
        )
    return total  # pragma: no cover - the last segment ends at the target


def measure_time_preview(prepared: list[dict], voices: list[dict]) -> list[dict]:
    """Map every voice's events to seconds under the tempo map.

    One cache maps each distinct offset to its seconds value, so an
    offset shared by several events (across or within voices) is
    integrated once and every event reuses the identical value. Returns
    [{"name": str, "events": [{"id", "kind", "start_seconds",
    "end_seconds"}]}] with float seconds.
    """
    cache: dict[Fraction, float] = {}

    def seconds_at(offset: Fraction) -> float:
        if offset not in cache:
            cache[offset] = offset_to_seconds(prepared, offset)
        return cache[offset]

    result = []
    for voice in voices:
        events = []
        for event in voice["events"]:
            events.append(
                {
                    "id": event["id"],
                    "kind": event["kind"],
                    "start_seconds": seconds_at(event["start"]),
                    "end_seconds": seconds_at(event["end"]),
                }
            )
        result.append({"name": voice["name"], "events": events})
    return result
