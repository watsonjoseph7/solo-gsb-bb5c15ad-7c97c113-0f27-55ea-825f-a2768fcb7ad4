"""Pure-logic tests: runnable with stdlib unittest or pytest."""

import math
import unittest
from fractions import Fraction

from app.grid import (
    KIND_REST,
    KIND_SOUND,
    LoopAlignmentTooLarge,
    QuantizationInfeasible,
    TempoMapInvalid,
    align_loops,
    avoidance_muting,
    build_tempo_map,
    build_voice_events,
    compute_grid,
    find_rhythm_match,
    find_sound_conflicts,
    format_rational,
    format_seconds,
    measure_time_preview,
    offset_to_seconds,
    parse_nonnegative_rational,
    parse_positive_decimal,
    parse_rational,
    quantize_measure,
    rational_lcm,
    voice_total,
)


def make_voices(specs):
    """specs: list of (name, [(kind, duration_str), ...])."""
    voices = []
    for index, (name, events) in enumerate(specs):
        raw = [
            {"kind": kind, "duration": parse_rational(duration)}
            for kind, duration in events
        ]
        voices.append({"name": name, "events": build_voice_events(index, raw)})
    return voices


class ParseRationalTest(unittest.TestCase):
    def test_integer_and_fraction(self):
        self.assertEqual(parse_rational("2"), Fraction(2, 1))
        self.assertEqual(parse_rational("3/4"), Fraction(3, 4))
        self.assertEqual(parse_rational("6/8"), Fraction(3, 4))  # reduced

    def test_rejects_bad_input(self):
        for bad in ["", "abc", "1/0", "0", "0/4", "-1/2", "1.5", "1/2/3", "/2"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_rational(bad)

    def test_nonnegative_parser_accepts_zero_but_not_negative(self):
        self.assertEqual(parse_nonnegative_rational("0"), Fraction(0))
        self.assertEqual(parse_nonnegative_rational("0/1"), Fraction(0))
        self.assertEqual(parse_nonnegative_rational("3/8"), Fraction(3, 8))
        for bad in ["-1/2", "abc", "1/0", "1.5"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_nonnegative_rational(bad)

    def test_format_roundtrip(self):
        self.assertEqual(format_rational(Fraction(3, 4)), "3/4")
        self.assertEqual(format_rational(Fraction(2, 1)), "2")
        self.assertEqual(format_rational(Fraction(0, 1)), "0")


class AccumulationTest(unittest.TestCase):
    def test_exact_fraction_sum(self):
        # 1/3 + 1/3 + 1/3 == 1 exactly (floats would not guarantee this).
        raw = [{"kind": KIND_SOUND, "duration": parse_rational("1/3")}] * 3
        self.assertEqual(voice_total(raw), Fraction(1))

    def test_event_offsets_are_half_open(self):
        (voice,) = make_voices([("a", [(KIND_SOUND, "1/2"), (KIND_REST, "1/4")])])
        first, second = voice["events"]
        self.assertEqual((first["start"], first["end"]), (Fraction(0), Fraction(1, 2)))
        self.assertEqual(
            (second["start"], second["end"]), (Fraction(1, 2), Fraction(3, 4))
        )
        self.assertEqual(first["id"], "v1e1")
        self.assertEqual(second["id"], "v1e2")


class GridTest(unittest.TestCase):
    def setUp(self):
        # melody: sound 1/2, rest 1/4, sound 1/4  (total 1)
        # bass:   rest 1/4, sound 3/4             (total 1)
        self.voices = make_voices(
            [
                (
                    "melody",
                    [(KIND_SOUND, "1/2"), (KIND_REST, "1/4"), (KIND_SOUND, "1/4")],
                ),
                ("bass", [(KIND_REST, "1/4"), (KIND_SOUND, "3/4")]),
            ]
        )
        self.segments = compute_grid(self.voices)

    def test_segment_boundaries_are_union_of_event_edges(self):
        edges = [(s["start"], s["end"]) for s in self.segments]
        self.assertEqual(
            edges,
            [
                (Fraction(0), Fraction(1, 4)),
                (Fraction(1, 4), Fraction(1, 2)),
                (Fraction(1, 2), Fraction(3, 4)),
                (Fraction(3, 4), Fraction(1)),
            ],
        )

    def test_active_events_per_segment(self):
        active = [s["events"] for s in self.segments]
        self.assertEqual(
            active,
            [
                {"melody": "v1e1", "bass": None},
                {"melody": "v1e1", "bass": "v2e2"},
                {"melody": None, "bass": "v2e2"},
                {"melody": "v1e3", "bass": "v2e2"},
            ],
        )

    def test_segments_tile_target_without_gaps_or_overlap(self):
        # No lost or duplicated time: segments chain exactly onto [0, 1).
        self.assertEqual(self.segments[0]["start"], Fraction(0))
        self.assertEqual(self.segments[-1]["end"], Fraction(1))
        for left, right in zip(self.segments, self.segments[1:]):
            self.assertEqual(left["end"], right["start"])

    def test_boundary_instant_belongs_to_exactly_one_event_per_voice(self):
        # At t = 1/2 the melody switches sound -> rest: the segment
        # starting at 1/2 must show the rest (null), the one ending at
        # 1/2 must show the sound. Half-open intervals => no double
        # counting, no gap.
        before = next(s for s in self.segments if s["end"] == Fraction(1, 2))
        after = next(s for s in self.segments if s["start"] == Fraction(1, 2))
        self.assertEqual(before["events"]["melody"], "v1e1")
        self.assertIsNone(after["events"]["melody"])

    def test_unaligned_voices_produce_finer_grid(self):
        voices = make_voices(
            [
                ("x", [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")]),
                ("y", [(KIND_SOUND, "1/2"), (KIND_SOUND, "1/2")]),
            ]
        )
        segments = compute_grid(voices)
        edges = [(s["start"], s["end"]) for s in segments]
        # x has edges at 0, 1/3, 1; y at 0, 1/2, 1.
        self.assertEqual(
            edges,
            [
                (Fraction(0), Fraction(1, 3)),
                (Fraction(1, 3), Fraction(1, 2)),
                (Fraction(1, 2), Fraction(1)),
            ],
        )
        self.assertEqual(segments[0]["events"], {"x": "v1e1", "y": "v2e1"})
        self.assertEqual(segments[1]["events"], {"x": None, "y": "v2e1"})
        self.assertEqual(segments[2]["events"], {"x": None, "y": "v2e2"})


class RationalLcmTest(unittest.TestCase):
    def test_integer_lengths(self):
        # Loops of length 3 and 4 first restart together at t = 12.
        self.assertEqual(rational_lcm(Fraction(3), Fraction(4)), Fraction(12))

    def test_equal_lengths(self):
        self.assertEqual(rational_lcm(Fraction(1, 2), Fraction(1, 2)), Fraction(1, 2))

    def test_fractional_lengths(self):
        # lcm(3/2, 2/3): lcm(3,2)/gcd(2,3) = 6.
        self.assertEqual(rational_lcm(Fraction(3, 2), Fraction(2, 3)), Fraction(6))
        # lcm(1/3, 1/2) = 1.
        self.assertEqual(rational_lcm(Fraction(1, 3), Fraction(1, 2)), Fraction(1))


class AlignLoopsTest(unittest.TestCase):
    def test_loops_of_different_length_realign_at_lcm(self):
        # A: length 1, one voice "a": sound 1/2, rest 1/2.
        # B: length 2, one voice "b": sound 2.
        (voice_a,) = make_voices(
            [("a", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")])]
        )
        (voice_b,) = make_voices([("b", [(KIND_SOUND, "2")])])
        result = align_loops(Fraction(1), [voice_a], Fraction(2), [voice_b])

        self.assertEqual(result["horizon"], Fraction(2))
        self.assertEqual(result["repetitions"], {"a": 2, "b": 1})
        self.assertEqual(result["expanded_events"], 2 * 2 + 1)

        edges = [(s["start"], s["end"]) for s in result["segments"]]
        self.assertEqual(
            edges,
            [
                (Fraction(0), Fraction(1, 2)),
                (Fraction(1, 2), Fraction(1)),
                (Fraction(1), Fraction(3, 2)),
                (Fraction(3, 2), Fraction(2)),
            ],
        )
        active = [(s["a"]["a"], s["b"]["b"]) for s in result["segments"]]
        self.assertEqual(
            active,
            [
                ({"event_id": "v1e1", "loop": 1}, {"event_id": "v1e1", "loop": 1}),
                (None, {"event_id": "v1e1", "loop": 1}),
                ({"event_id": "v1e1", "loop": 2}, {"event_id": "v1e1", "loop": 1}),
                (None, {"event_id": "v1e1", "loop": 1}),
            ],
        )

    def test_same_measure_as_both_loops_restarts_after_one_cycle(self):
        (voice_a,) = make_voices([("a", [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")])])
        (voice_b,) = make_voices([("a", [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")])])
        result = align_loops(Fraction(1), [voice_a], Fraction(1), [voice_b])
        self.assertEqual(result["horizon"], Fraction(1))
        self.assertEqual(result["repetitions"], {"a": 1, "b": 1})
        edges = [(s["start"], s["end"]) for s in result["segments"]]
        self.assertEqual(edges, [(Fraction(0), Fraction(1, 3)), (Fraction(1, 3), Fraction(1))])
        self.assertEqual(
            result["segments"][0]["a"]["a"],
            {"event_id": "v1e1", "loop": 1},
        )
        self.assertIsNone(result["segments"][1]["b"]["a"])

    def test_loop_numbers_increase_with_each_repetition(self):
        # Three length-1 loops against one length-3 loop; event per loop
        # at its start must be tagged loop 1, 2 then 3.
        (voice_a,) = make_voices([("a", [(KIND_SOUND, "1/4"), (KIND_REST, "3/4")])])
        (voice_b,) = make_voices([("b", [(KIND_SOUND, "1/4"), (KIND_REST, "11/4")])])
        result = align_loops(Fraction(1), [voice_a], Fraction(3), [voice_b])
        starts = [
            segment["a"]["a"]["loop"]
            for segment in result["segments"]
            if segment["a"]["a"] is not None
        ]
        self.assertEqual(starts, [1, 2, 3])

    def test_expansion_over_limit_is_rejected_without_segments(self):
        (voice_a,) = make_voices([("a", [(KIND_SOUND, "1")])])
        (voice_b,) = make_voices([("b", [(KIND_SOUND, "1")])])
        with self.assertRaises(LoopAlignmentTooLarge) as ctx:
            align_loops(
                Fraction(3), [voice_a], Fraction(5), [voice_b], max_events=7
            )
        error = ctx.exception
        self.assertEqual(error.horizon, Fraction(15))
        self.assertEqual((error.repetitions_a, error.repetitions_b), (5, 3))
        self.assertEqual(error.expanded_events, 8)
        self.assertEqual(error.max_events, 7)


class FindRhythmMatchTest(unittest.TestCase):
    def make_events(self, spec):
        # spec: list of (kind, duration_str); voice index is irrelevant.
        return build_voice_events(
            0,
            [
                {"kind": kind, "duration": parse_rational(duration)}
                for kind, duration in spec
            ],
        )

    def test_identical_rhythm_matches_ratio_one_shift_zero(self):
        source = self.make_events(
            [(KIND_SOUND, "1/2"), (KIND_REST, "1/4"), (KIND_SOUND, "1/4")]
        )
        match = find_rhythm_match(source, source)
        self.assertIsNotNone(match)
        self.assertEqual(match["ratio"], Fraction(1))
        self.assertEqual(match["shift"], 0)

    def test_uniform_retempo_matches_with_common_ratio(self):
        source = self.make_events(
            [(KIND_SOUND, "1/3"), (KIND_REST, "1/3"), (KIND_SOUND, "1/3")]
        )
        # Every duration doubled.
        candidate = self.make_events(
            [(KIND_SOUND, "2/3"), (KIND_REST, "2/3"), (KIND_SOUND, "2/3")]
        )
        match = find_rhythm_match(source, candidate)
        self.assertEqual(match, {"ratio": Fraction(2), "shift": 0})

    def test_non_unit_fraction_ratio_is_exact(self):
        # Ratios like 3/2 must not be reduced through floating point.
        source = self.make_events(
            [(KIND_SOUND, "1/2"), (KIND_REST, "1/6"), (KIND_SOUND, "1/3")]
        )
        candidate = self.make_events(
            [(KIND_SOUND, "3/4"), (KIND_REST, "1/4"), (KIND_SOUND, "1/2")]
        )
        match = find_rhythm_match(source, candidate)
        self.assertEqual(match, {"ratio": Fraction(3, 2), "shift": 0})

    def test_cyclic_shift_matches(self):
        source = self.make_events(
            [(KIND_SOUND, "1/4"), (KIND_REST, "1/4"), (KIND_SOUND, "1/2")]
        )
        # Candidate event j = source event (j + 2) mod 3, same durations.
        candidate = self.make_events(
            [(KIND_SOUND, "1/2"), (KIND_SOUND, "1/4"), (KIND_REST, "1/4")]
        )
        match = find_rhythm_match(source, candidate)
        self.assertEqual(match, {"ratio": Fraction(1), "shift": 2})

    def test_smallest_shift_is_chosen(self):
        # A uniform rhythm (all durations equal) with all events of the
        # same kind matches at every shift: 0 must win.
        source = self.make_events([(KIND_SOUND, "1/4")] * 4)
        candidate = self.make_events([(KIND_SOUND, "1/2")] * 4)
        match = find_rhythm_match(source, candidate)
        self.assertEqual(match, {"ratio": Fraction(2), "shift": 0})

    def test_kind_mismatch_anywhere_fails(self):
        source = self.make_events(
            [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")]
        )
        candidate = self.make_events(
            [(KIND_SOUND, "1/2"), (KIND_SOUND, "1/2")]
        )
        self.assertIsNone(find_rhythm_match(source, candidate))

    def test_different_event_counts_fail(self):
        source = self.make_events(
            [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")]
        )
        candidate = self.make_events(
            [(KIND_SOUND, "1/3"), (KIND_REST, "1/3"), (KIND_REST, "1/3")]
        )
        self.assertIsNone(find_rhythm_match(source, candidate))

    def test_non_uniform_ratios_fail(self):
        source = self.make_events(
            [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")]
        )
        candidate = self.make_events(
            [(KIND_SOUND, "1/4"), (KIND_REST, "3/4")]
        )
        self.assertIsNone(find_rhythm_match(source, candidate))


class QuantizeMeasureTest(unittest.TestCase):
    def mapping(self, result):
        return [(b["original"], b["tick"], b["shift"]) for b in result["boundaries"]]

    def test_boundaries_already_on_grid_have_zero_shift(self):
        # Edges at 0, 1/4, 1/2, 1 and a 4-division grid: exact fit, and
        # max_shift 0 admits only this mapping.
        voices = make_voices(
            [
                ("melody", [(KIND_SOUND, "1/2"), (KIND_REST, "1/4"), (KIND_SOUND, "1/4")]),
                ("bass", [(KIND_REST, "1/4"), (KIND_SOUND, "3/4")]),
            ]
        )
        result = quantize_measure(Fraction(1), voices, 4, Fraction(0))
        self.assertEqual(result["grid_step"], Fraction(1, 4))
        self.assertEqual(
            [(b["original"], b["tick"]) for b in result["boundaries"]],
            [(Fraction(0), 0), (Fraction(1, 4), 1), (Fraction(1, 2), 2),
             (Fraction(3, 4), 3), (Fraction(1), 4)],
        )
        self.assertEqual(result["total_abs_shift"], Fraction(0))
        for boundary in result["boundaries"]:
            self.assertEqual(boundary["shift"], Fraction(0))
            self.assertEqual(boundary["quantized"], boundary["original"])

    def test_shared_boundary_across_voices_maps_once(self):
        # Both voices share the 1/2 boundary; it appears once in the
        # mapping and both events adjacent to it use the same new point.
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "1/2"), (KIND_SOUND, "1/2")]),
                ("b", [(KIND_REST, "1/2"), (KIND_SOUND, "1/2")]),
            ]
        )
        result = quantize_measure(Fraction(1), voices, 2, Fraction(0))
        ticks = {b["original"]: b["tick"] for b in result["boundaries"]}
        self.assertEqual(ticks, {Fraction(0): 0, Fraction(1, 2): 1, Fraction(1): 2})
        for voice in result["voices"]:
            self.assertEqual(voice["events"][0]["end"], Fraction(1, 2))
            self.assertEqual(voice["events"][1]["start"], Fraction(1, 2))

    def test_endpoints_are_fixed_to_first_and_last_tick(self):
        voices = make_voices(
            [("a", [(KIND_SOUND, "3/8"), (KIND_REST, "5/8")]),
             ("b", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")])]
        )
        result = quantize_measure(Fraction(1), voices, 4, Fraction(1))
        self.assertEqual(result["boundaries"][0]["tick"], 0)
        self.assertEqual(result["boundaries"][0]["quantized"], Fraction(0))
        self.assertEqual(result["boundaries"][-1]["tick"], 4)
        self.assertEqual(result["boundaries"][-1]["quantized"], Fraction(1))

    def test_lexicographically_smallest_ticks_break_equal_cost_ties(self):
        # Boundary 3/8 sits exactly between ticks 1 (= 1/4) and 2 (= 1/2)
        # on a 4-division grid: both shifts have magnitude 1/8. The
        # minimum total shift is tied, so the smaller tick wins.
        voices = make_voices(
            [("a", [(KIND_SOUND, "3/8"), (KIND_REST, "5/8")]),
             ("b", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")])]
        )
        result = quantize_measure(Fraction(1), voices, 4, Fraction(1))
        by_original = {b["original"]: b for b in result["boundaries"]}
        self.assertEqual(by_original[Fraction(3, 8)]["tick"], 1)
        self.assertEqual(by_original[Fraction(3, 8)]["shift"], Fraction(-1, 8))
        self.assertEqual(by_original[Fraction(1, 2)]["tick"], 2)
        self.assertEqual(result["total_abs_shift"], Fraction(1, 8))

    def test_optimization_is_global_not_independent_rounding(self):
        # Boundaries 3/10 and 3/5 on a 5-division grid (step 1/5):
        # independent nearest rounding would put them on ticks 2 and 3,
        # but 3/5 is exactly tick 3, and 3/10 can use tick 1 (shift
        # -1/10) leaving the exact point untouched; the global optimum is
        # ticks (1, 3) with total shift 1/10, not (2, 3) at 1/10 tied...
        # so use a case where greedy rounding is strictly worse:
        # boundaries 1/4 and 9/20, step 1/5, d = 5.
        #   1/4 nearest tick: 1 (1/5, shift -1/20)
        #   9/20 nearest tick: 2 (2/5, shift -1/20) -> (1, 2), cost 1/10
        # Alternative (1, 3): 9/20 -> 3/5 shift 3/20, worse. And
        # (0,..) forbidden since tick 0 is the fixed start.
        voices = make_voices(
            [("a", [(KIND_SOUND, "1/4"), (KIND_SOUND, "1/5"), (KIND_REST, "11/20")]),
             ("b", [(KIND_REST, "1")])]
        )
        result = quantize_measure(Fraction(1), voices, 5, Fraction(1))
        ticks = [b["tick"] for b in result["boundaries"]]
        self.assertEqual(ticks, [0, 1, 2, 5])
        self.assertEqual(result["total_abs_shift"], Fraction(1, 10))

    def test_global_optimum_spreads_colliding_independent_roundings(self):
        # Distinct boundaries 2/5, 1/2, 3/5 on a 4-division grid
        # (step 1/4). Independent nearest rounding would send all three
        # to tick 2 (1/2 is exact there; 2/5 and 3/5 are each 1/10 away),
        # but ticks must stay strictly increasing. The DP spreads them:
        # 2/5 -> tick 1 (shift -3/20), 1/2 -> tick 2 (0),
        # 3/5 -> tick 3 (shift +3/20), total 3/10 — strictly better than
        # pushing the tail up against the fixed end.
        voices = make_voices(
            [("a", [(KIND_SOUND, "2/5"), (KIND_SOUND, "1/5"), (KIND_REST, "2/5")]),
             ("b", [(KIND_REST, "1/2"), (KIND_SOUND, "1/2")])]
        )
        result = quantize_measure(Fraction(1), voices, 4, Fraction(1))
        self.assertEqual(
            [(b["original"], b["tick"]) for b in result["boundaries"]],
            [(Fraction(0), 0), (Fraction(2, 5), 1), (Fraction(1, 2), 2),
             (Fraction(3, 5), 3), (Fraction(1), 4)],
        )
        self.assertEqual(result["total_abs_shift"], Fraction(3, 10))

    def test_mapped_ticks_are_strictly_increasing_and_quantized_events_tile(self):
        voices = make_voices(
            [("x", [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")]),
             ("y", [(KIND_SOUND, "1/2"), (KIND_SOUND, "1/2")])]
        )
        result = quantize_measure(Fraction(1), voices, 6, Fraction(1))
        ticks = [b["tick"] for b in result["boundaries"]]
        self.assertEqual(ticks, sorted(ticks))
        self.assertEqual(len(ticks), len(set(ticks)))
        for voice in result["voices"]:
            self.assertEqual(voice["events"][0]["start"], Fraction(0))
            self.assertEqual(voice["events"][-1]["end"], Fraction(1))
            total = sum((event["duration"] for event in voice["events"]), Fraction(0))
            self.assertEqual(total, Fraction(1))
            for event in voice["events"]:
                self.assertEqual(
                    event["duration"], event["end"] - event["start"]
                )
                self.assertGreater(event["duration"], 0)

    def test_too_few_divisions_for_boundaries_is_infeasible(self):
        # Distinct edges at 0, 1/2, 1 but a one-division grid has only
        # ticks 0 and 1: the internal boundary cannot be placed.
        voices = make_voices(
            [("a", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")]),
             ("b", [(KIND_REST, "1/2"), (KIND_SOUND, "1/2")])]
        )
        with self.assertRaises(QuantizationInfeasible):
            quantize_measure(Fraction(1), voices, 1, Fraction(1))

    def test_max_shift_smaller_than_distance_to_any_tick_is_infeasible(self):
        voices = make_voices(
            [("a", [(KIND_SOUND, "3/8"), (KIND_REST, "5/8")]),
             ("b", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")])]
        )
        # 3/8 is 1/8 away from both neighboring quarter ticks.
        with self.assertRaises(QuantizationInfeasible):
            quantize_measure(Fraction(1), voices, 4, Fraction(1, 16))

    def test_zero_max_shift_forces_exact_grid_fit(self):
        voices = make_voices(
            [("x", [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")]),
             ("y", [(KIND_SOUND, "1/2"), (KIND_SOUND, "1/2")])]
        )
        # 1/3 is not on a quarter grid but is tick 2 of a sixth grid.
        with self.assertRaises(QuantizationInfeasible):
            quantize_measure(Fraction(1), voices, 4, Fraction(0))
        result = quantize_measure(Fraction(1), voices, 6, Fraction(0))
        self.assertEqual(result["total_abs_shift"], Fraction(0))

    def test_whole_measure_single_event_quantizes_to_endpoints(self):
        voices = make_voices(
            [("a", [(KIND_SOUND, "1")]), ("b", [(KIND_REST, "1")])]
        )
        result = quantize_measure(Fraction(1), voices, 1, Fraction(0))
        self.assertEqual(
            [(b["original"], b["tick"]) for b in result["boundaries"]],
            [(Fraction(0), 0), (Fraction(1), 1)],
        )
        event = result["voices"][0]["events"][0]
        self.assertEqual(
            (event["start"], event["end"], event["duration"]),
            (Fraction(0), Fraction(1), Fraction(1)),
        )

    def test_quantized_events_keep_original_ids_and_kinds(self):
        voices = make_voices(
            [("melody", [(KIND_SOUND, "1/2"), (KIND_REST, "1/4"), (KIND_SOUND, "1/4")]),
             ("bass", [(KIND_REST, "1/4"), (KIND_SOUND, "3/4")])]
        )
        result = quantize_measure(Fraction(1), voices, 8, Fraction(1))
        flat = [
            (event["id"], event["kind"])
            for voice in result["voices"]
            for event in voice["events"]
        ]
        self.assertEqual(
            flat,
            [
                ("v1e1", KIND_SOUND), ("v1e2", KIND_REST), ("v1e3", KIND_SOUND),
                ("v2e1", KIND_REST), ("v2e2", KIND_SOUND),
            ],
        )


class FindSoundConflictsTest(unittest.TestCase):
    def test_events_meeting_at_a_boundary_do_not_conflict(self):
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")]),
                ("b", [(KIND_REST, "1/2"), (KIND_SOUND, "1/2")]),
            ]
        )
        self.assertEqual(find_sound_conflicts(voices[0], voices[1]), [])
        # The reverse ordering must agree.
        self.assertEqual(find_sound_conflicts(voices[1], voices[0]), [])

    def test_rest_events_never_conflict(self):
        voices = make_voices(
            [("a", [(KIND_REST, "1")]), ("b", [(KIND_SOUND, "1")])]
        )
        self.assertEqual(find_sound_conflicts(voices[0], voices[1]), [])

    def test_positive_overlaps_are_reported_in_order(self):
        # a: sound [0, 3/5]
        # b: sound [0, 1/4], rest, sound [1/2, 3/4]
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "3/5"), (KIND_REST, "2/5")]),
                ("b", [
                    (KIND_SOUND, "1/4"), (KIND_REST, "1/4"),
                    (KIND_SOUND, "1/4"), (KIND_REST, "1/4"),
                ]),
            ]
        )
        conflicts = find_sound_conflicts(voices[0], voices[1])
        self.assertEqual(
            [(c["a"]["id"], c["b"]["id"]) for c in conflicts],
            [("v1e1", "v2e1"), ("v1e1", "v2e3")],
        )
        # min(3/5,1/4)-max(0,0) = 1/4 ; min(3/5,3/4)-max(0,1/2) = 1/10.
        self.assertEqual(
            [c["overlap"] for c in conflicts],
            [Fraction(1, 4), Fraction(1, 10)],
        )

    def test_zero_length_intersection_inside_an_event_chain_is_skipped(self):
        # b's first sound ends exactly at 1/3 where a's sound starts:
        # boundary contact, while b's second sound genuinely overlaps.
        voices = make_voices(
            [
                ("a", [(KIND_REST, "1/3"), (KIND_SOUND, "1/3"), (KIND_REST, "1/3")]),
                ("b", [(KIND_SOUND, "1/3"), (KIND_SOUND, "2/3")]),
            ]
        )
        conflicts = find_sound_conflicts(voices[0], voices[1])
        self.assertEqual(
            [(c["a"]["id"], c["b"]["id"], c["overlap"]) for c in conflicts],
            [("v1e2", "v2e2", Fraction(1, 3))],
        )


class AvoidanceMutingTest(unittest.TestCase):
    def test_no_conflict_mutes_nothing(self):
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")]),
                ("b", [(KIND_REST, "1/2"), (KIND_SOUND, "1/2")]),
            ]
        )
        result = avoidance_muting(voices[0], voices[1])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["muted"], [])
        self.assertEqual(result["total_muted"], Fraction(0))

    def test_single_conflict_mutes_the_shorter_event_whole(self):
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")]),
                ("b", [
                    (KIND_REST, "1/12"), (KIND_SOUND, "1/12"),
                    (KIND_REST, "5/6"),
                ]),
            ]
        )
        result = avoidance_muting(voices[0], voices[1])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["overlap"], Fraction(1, 12))
        self.assertEqual(
            [(m["event"]["id"], m["voice"]) for m in result["muted"]],
            [("v2e2", "b")],
        )
        # The full original event duration is muted, not just the overlap.
        self.assertEqual(result["total_muted"], Fraction(1, 12))

    def test_shared_long_event_is_muted_once_beating_two_shorter_events(self):
        # One A event (3/5) overlaps two B events (1/4 + 1/4): muting the
        # A event costs 3/5, muting both B events costs 1/2.
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "3/5"), (KIND_REST, "2/5")]),
                ("b", [
                    (KIND_SOUND, "1/4"), (KIND_REST, "1/4"),
                    (KIND_SOUND, "1/4"), (KIND_REST, "1/4"),
                ]),
            ]
        )
        result = avoidance_muting(voices[0], voices[1])
        self.assertEqual(
            [(m["event"]["id"], m["voice"]) for m in result["muted"]],
            [("v2e1", "b"), ("v2e3", "b")],
        )
        self.assertEqual(result["total_muted"], Fraction(1, 2))

    def test_pairwise_greedy_is_strictly_worse_than_global_cover(self):
        # Connected conflict path b1 - a1 - b2 - a2:
        #   edges (a1,b1), (a1,b2), (a2,b2)
        # weights: a1 = a2 = 1, b1 = 1/100, b2 = 101/100.
        # An edge-by-edge greedy (cheapest endpoint of each conflict in
        # order) mutes b1 (1/100), a1 (1 < 101/100), a2 (1 < 101/100):
        # 201/100 total. The global minimum cover {b1, b2} costs 51/50.
        #
        # a1 sound [0,1]; rest 1/20; a2 sound [21/20,41/20]; rest 9/20
        # b1 sound [0,1/100]; rest 49/100; b2 sound [1/2,151/100];
        # rest 99/100  (both voices total 5/2)
        voices = make_voices(
            [
                ("a", [
                    (KIND_SOUND, "1"), (KIND_REST, "1/20"),
                    (KIND_SOUND, "1"), (KIND_REST, "9/20"),
                ]),
                ("b", [
                    (KIND_SOUND, "1/100"), (KIND_REST, "49/100"),
                    (KIND_SOUND, "101/100"), (KIND_REST, "99/100"),
                ]),
            ]
        )
        result = avoidance_muting(voices[0], voices[1])
        self.assertEqual(
            [(c["a"]["id"], c["b"]["id"]) for c in result["conflicts"]],
            [("v1e1", "v2e1"), ("v1e1", "v2e3"), ("v1e3", "v2e3")],
        )
        self.assertEqual(
            [c["overlap"] for c in result["conflicts"]],
            [Fraction(1, 100), Fraction(1, 2), Fraction(23, 50)],
        )
        self.assertEqual(result["total_muted"], Fraction(51, 50))
        self.assertEqual(
            [(m["event"]["id"], m["voice"]) for m in result["muted"]],
            [("v2e1", "b"), ("v2e3", "b")],
        )

    def test_exact_fraction_weights_no_floating_point_error(self):
        # Durations in thirds/sevenths would misbehave in float.
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "1/7"), (KIND_REST, "6/7")]),
                ("b", [(KIND_SOUND, "1/3"), (KIND_REST, "2/3")]),
            ]
        )
        result = avoidance_muting(voices[0], voices[1])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["overlap"], Fraction(1, 7))
        self.assertEqual(result["total_muted"], Fraction(1, 7))
        muted_ids = {m["event"]["id"] for m in result["muted"]}
        self.assertEqual(muted_ids, {"v1e1"})

    def test_muted_set_is_a_cover_of_every_reported_conflict(self):
        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "3/5"), (KIND_REST, "2/5")]),
                ("b", [
                    (KIND_SOUND, "1/4"), (KIND_REST, "1/4"),
                    (KIND_SOUND, "1/4"), (KIND_REST, "1/4"),
                ]),
            ]
        )
        result = avoidance_muting(voices[0], voices[1])
        muted = {(m["voice"], m["event"]["id"]) for m in result["muted"]}
        self.assertTrue(muted)
        for conflict in result["conflicts"]:
            covered = (
                ("a", conflict["a"]["id"]) in muted
                or ("b", conflict["b"]["id"]) in muted
            )
            self.assertTrue(covered)


class TempoMapTest(unittest.TestCase):
    def test_parse_positive_decimal(self):
        self.assertEqual(parse_positive_decimal("120"), 120.0)
        self.assertEqual(parse_positive_decimal("90.5"), 90.5)
        self.assertEqual(parse_positive_decimal("0.75"), 0.75)
        for bad in [
            "", "0", "0.0", "-3", "+4", "abc", "1/2", "1.2.3", ".5",
            "5.", "1e2", "nan", "inf",
        ]:
            with self.assertRaises(ValueError, msg=bad):
                parse_positive_decimal(bad)

    def test_format_seconds_is_fixed_six_decimals(self):
        self.assertEqual(format_seconds(0.0), "0.000000")
        self.assertEqual(format_seconds(0.5), "0.500000")
        self.assertEqual(format_seconds(2.3862943611198906), "2.386294")

    def test_build_tempo_map_attaches_implicit_starts(self):
        segments = [
            {"end": Fraction(1, 2), "start_bpm": 120.0, "end_bpm": 120.0},
            {"end": Fraction(1), "start_bpm": 120.0, "end_bpm": 60.0},
        ]
        prepared = build_tempo_map(segments, Fraction(1))
        self.assertEqual(
            [segment["start"] for segment in prepared],
            [Fraction(0), Fraction(1, 2)],
        )
        self.assertEqual(
            [segment["end"] for segment in prepared],
            [Fraction(1, 2), Fraction(1)],
        )

    def test_build_tempo_map_rejects_invalid_segments(self):
        base = {"start_bpm": 120.0, "end_bpm": 60.0}
        # Empty map.
        with self.assertRaises(TempoMapInvalid):
            build_tempo_map([], Fraction(1))
        # Last end must equal the target duration.
        with self.assertRaises(TempoMapInvalid):
            build_tempo_map([{"end": Fraction(1, 2), **base}], Fraction(1))
        with self.assertRaises(TempoMapInvalid):
            build_tempo_map([{"end": Fraction(3, 2), **base}], Fraction(1))
        # Ends must be strictly increasing (repeat and decrease).
        with self.assertRaises(TempoMapInvalid):
            build_tempo_map(
                [
                    {"end": Fraction(1, 2), **base},
                    {"end": Fraction(1, 2), **base},
                    {"end": Fraction(1), **base},
                ],
                Fraction(1),
            )
        with self.assertRaises(TempoMapInvalid):
            build_tempo_map(
                [
                    {"end": Fraction(3, 4), **base},
                    {"end": Fraction(1, 2), **base},
                    {"end": Fraction(1), **base},
                ],
                Fraction(1),
            )

    def test_constant_tempo_seconds(self):
        # 120 BPM: one whole-note unit lasts 240/120 = 2 seconds.
        prepared = build_tempo_map(
            [{"end": Fraction(1), "start_bpm": 120.0, "end_bpm": 120.0}],
            Fraction(1),
        )
        self.assertEqual(offset_to_seconds(prepared, Fraction(0)), 0.0)
        self.assertAlmostEqual(offset_to_seconds(prepared, Fraction(1, 4)), 0.5)
        self.assertAlmostEqual(offset_to_seconds(prepared, Fraction(1)), 2.0)

    def test_linear_ramp_seconds(self):
        # 120 -> 60 BPM over [0, 1]: total is 240*ln(120/60)/60 = 4 ln 2.
        prepared = build_tempo_map(
            [{"end": Fraction(1), "start_bpm": 120.0, "end_bpm": 60.0}],
            Fraction(1),
        )
        self.assertAlmostEqual(
            offset_to_seconds(prepared, Fraction(1)), 4 * math.log(2)
        )
        # Halfway the BPM is 90: 240*(1/2)*ln(120/90)/30 = 4 ln(4/3).
        self.assertAlmostEqual(
            offset_to_seconds(prepared, Fraction(1, 2)), 4 * math.log(4 / 3)
        )

    def test_accelerating_ramp_seconds(self):
        # 60 -> 120 BPM over [0, 1]: total is 240*ln(120/60)/60 = 4 ln 2.
        prepared = build_tempo_map(
            [{"end": Fraction(1), "start_bpm": 60.0, "end_bpm": 120.0}],
            Fraction(1),
        )
        self.assertAlmostEqual(
            offset_to_seconds(prepared, Fraction(1)), 4 * math.log(2)
        )

    def test_cross_segment_integration_and_shared_offsets(self):
        # [0, 1/2] constant 120 BPM, then [1/2, 1] linear 120 -> 60.
        prepared = build_tempo_map(
            [
                {"end": Fraction(1, 2), "start_bpm": 120.0, "end_bpm": 120.0},
                {"end": Fraction(1), "start_bpm": 120.0, "end_bpm": 60.0},
            ],
            Fraction(1),
        )
        self.assertAlmostEqual(offset_to_seconds(prepared, Fraction(1, 4)), 0.5)
        self.assertAlmostEqual(offset_to_seconds(prepared, Fraction(1, 2)), 1.0)
        # Second segment: 240*(1/2)*ln(120/60)/60 = 2 ln 2 on top of 1 s.
        self.assertAlmostEqual(
            offset_to_seconds(prepared, Fraction(1)), 1.0 + 2 * math.log(2)
        )
        # Offset 3/4 sits halfway through the ramp (BPM 90):
        # 1 + 240*(1/4)*ln(120/90)/30 = 1 + 2 ln(4/3).
        self.assertAlmostEqual(
            offset_to_seconds(prepared, Fraction(3, 4)),
            1.0 + 2 * math.log(4 / 3),
        )

        voices = make_voices(
            [
                ("a", [(KIND_SOUND, "1/2"), (KIND_REST, "1/2")]),
                ("b", [(KIND_REST, "1/2"), (KIND_SOUND, "1/2")]),
            ]
        )
        preview = measure_time_preview(prepared, voices)
        a_events, b_events = preview[0]["events"], preview[1]["events"]
        # The shared boundary 1/2 reuses one cached seconds value, so the
        # floats (and their formatted strings) are identical everywhere.
        self.assertEqual(a_events[0]["end_seconds"], a_events[1]["start_seconds"])
        self.assertEqual(a_events[0]["end_seconds"], b_events[0]["end_seconds"])
        self.assertEqual(a_events[0]["end_seconds"], b_events[1]["start_seconds"])
        self.assertEqual(a_events[1]["end_seconds"], b_events[1]["end_seconds"])
        self.assertEqual(
            [event["id"] for event in a_events], ["v1e1", "v1e2"]
        )
        self.assertEqual(
            [event["kind"] for event in b_events], [KIND_REST, KIND_SOUND]
        )


if __name__ == "__main__":
    unittest.main()