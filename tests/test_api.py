"""API tests using FastAPI's TestClient (requires fastapi + httpx)."""

import os
import tempfile

# Point the app at a throwaway database before it is imported.
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

# TestClient only runs the app's lifespan when used as a context
# manager, so initialize the schema explicitly here.
db.init_db()
client = TestClient(app)

VALID_PAYLOAD = {
    "target_duration": "4/4",
    "voices": [
        {
            "name": "melody",
            "events": [
                {"kind": "sound", "duration": "1/2"},
                {"kind": "rest", "duration": "1/4"},
                {"kind": "sound", "duration": "1/4"},
            ],
        },
        {
            "name": "bass",
            "events": [
                {"kind": "rest", "duration": "1/4"},
                {"kind": "sound", "duration": "3/4"},
            ],
        },
    ],
}


def test_create_measure_returns_201_and_stored_representation():
    response = client.post("/measures", json=VALID_PAYLOAD)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] >= 1
    assert body["target_duration"] == "1"  # 4/4 stored in canonical form
    assert [voice["name"] for voice in body["voices"]] == ["melody", "bass"]
    melody_events = body["voices"][0]["events"]
    assert [event["id"] for event in melody_events] == ["v1e1", "v1e2", "v1e3"]
    assert melody_events[0]["start"] == "0"
    assert melody_events[0]["end"] == "1/2"


def test_mismatched_voice_is_rejected_with_difference_and_not_saved():
    payload = {
        "target_duration": "1",
        "voices": [
            {
                "name": "too_short",
                "events": [{"kind": "sound", "duration": "3/4"}],
            },
            {
                "name": "too_long",
                "events": [{"kind": "sound", "duration": "5/4"}],
            },
        ],
    }
    response = client.post("/measures", json=payload)
    assert response.status_code == 422, response.text
    mismatches = response.json()["detail"]["mismatches"]
    by_voice = {entry["voice"]: entry for entry in mismatches}
    assert by_voice["too_short"] == {
        "voice": "too_short",
        "expected": "1",
        "actual": "3/4",
        "difference": "-1/4",
    }
    assert by_voice["too_long"]["difference"] == "1/4"

    # Nothing was saved: the next valid measure gets the very next id.
    ok = client.post("/measures", json=VALID_PAYLOAD)
    assert ok.status_code == 201
    assert client.get(f"/measures/{ok.json()['id']}").status_code == 200
    assert client.get(f"/measures/{ok.json()['id'] + 1}").status_code == 404


def test_grid_segments_cover_target_with_half_open_intervals():
    measure_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    response = client.get(f"/measures/{measure_id}/grid")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["voices"] == ["melody", "bass"]

    segments = body["segments"]
    assert [(s["start"], s["end"]) for s in segments] == [
        ("0", "1/4"),
        ("1/4", "1/2"),
        ("1/2", "3/4"),
        ("3/4", "1"),
    ]
    assert [s["events"] for s in segments] == [
        {"melody": "v1e1", "bass": None},
        {"melody": "v1e1", "bass": "v2e2"},
        {"melody": None, "bass": "v2e2"},
        {"melody": "v1e3", "bass": "v2e2"},
    ]
    # Segments tile [0, target) with no gaps or overlaps.
    assert segments[0]["start"] == "0"
    assert segments[-1]["end"] == body["target_duration"]
    for left, right in zip(segments, segments[1:]):
        assert left["end"] == right["start"]


def test_validation_errors():
    # Fewer than two voices.
    response = client.post(
        "/measures",
        json={
            "target_duration": "1",
            "voices": [{"name": "solo", "events": [{"kind": "sound", "duration": "1"}]}],
        },
    )
    assert response.status_code == 422

    # Non-positive / malformed durations.
    for bad_duration in ["0", "-1/2", "1.5", "abc", "1/0"]:
        response = client.post(
            "/measures",
            json={
                "target_duration": "1",
                "voices": [
                    {"name": "a", "events": [{"kind": "sound", "duration": bad_duration}]},
                    {"name": "b", "events": [{"kind": "rest", "duration": "1"}]},
                ],
            },
        )
        assert response.status_code == 422, bad_duration

    # Duplicate voice names.
    response = client.post(
        "/measures",
        json={
            "target_duration": "1",
            "voices": [
                {"name": "dup", "events": [{"kind": "sound", "duration": "1"}]},
                {"name": "dup", "events": [{"kind": "rest", "duration": "1"}]},
            ],
        },
    )
    assert response.status_code == 422


def test_unknown_measure_returns_404():
    assert client.get("/measures/999999").status_code == 404
    assert client.get("/measures/999999/grid").status_code == 404


# --- POST /measures/{id}/quantization-preview ------------------------------

# Melody edges at 0, 1/2, 3/4, 1 and bass edges at 0, 1/4, 1.
def test_quantization_preview_maps_boundaries_and_events_without_saving():
    measure_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    response = client.post(
        f"/measures/{measure_id}/quantization-preview",
        json={"divisions": 4, "max_shift": "0"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["measure_id"] == measure_id
    assert body["target_duration"] == "1"
    assert body["divisions"] == 4
    assert body["grid_step"] == "1/4"
    assert body["max_shift"] == "0"
    assert body["total_abs_shift"] == "0"

    assert [(b["original"], b["tick"], b["quantized"], b["shift"]) for b in body["boundaries"]] == [
        ("0", 0, "0", "0"),
        ("1/4", 1, "1/4", "0"),
        ("1/2", 2, "1/2", "0"),
        ("3/4", 3, "3/4", "0"),
        ("1", 4, "1", "0"),
    ]

    melody = body["voices"][0]
    assert melody["name"] == "melody"
    assert melody["events"] == [
        {"id": "v1e1", "kind": "sound", "start": "0", "end": "1/2", "duration": "1/2"},
        {"id": "v1e2", "kind": "rest", "start": "1/2", "end": "3/4", "duration": "1/4"},
        {"id": "v1e3", "kind": "sound", "start": "3/4", "end": "1", "duration": "1/4"},
    ]
    bass = body["voices"][1]
    assert bass["events"] == [
        {"id": "v2e1", "kind": "rest", "start": "0", "end": "1/4", "duration": "1/4"},
        {"id": "v2e2", "kind": "sound", "start": "1/4", "end": "1", "duration": "3/4"},
    ]

    # The preview is read-only: the stored measure is unchanged.
    stored = client.get(f"/measures/{measure_id}").json()
    assert stored["voices"][0]["events"][0]["end"] == "1/2"


def test_quantization_preview_shifts_onto_grid_and_minimizes_total_shift():
    # Thirds-against-halves measure, eighth grid: 1/3 -> tick 3 (3/8),
    # 1/2 -> tick 4 (1/2); |3/8 - 1/3| = 1/24 total.
    payload = {
        "target_duration": "1",
        "voices": [
            {"name": "x", "events": [
                {"kind": "sound", "duration": "1/3"},
                {"kind": "rest", "duration": "2/3"},
            ]},
            {"name": "y", "events": [
                {"kind": "sound", "duration": "1/2"},
                {"kind": "sound", "duration": "1/2"},
            ]},
        ],
    }
    measure_id = client.post("/measures", json=payload).json()["id"]
    response = client.post(
        f"/measures/{measure_id}/quantization-preview",
        json={"divisions": 8, "max_shift": "1/4"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["grid_step"] == "1/8"
    assert [(b["original"], b["tick"], b["shift"]) for b in body["boundaries"]] == [
        ("0", 0, "0"),
        ("1/3", 3, "1/24"),
        ("1/2", 4, "0"),
        ("1", 8, "0"),
    ]
    assert body["total_abs_shift"] == "1/24"
    quantized_x = body["voices"][0]["events"]
    assert quantized_x[0] == {
        "id": "v1e1", "kind": "sound", "start": "0", "end": "3/8",
        "duration": "3/8",
    }
    assert quantized_x[1]["start"] == "3/8"
    assert quantized_x[1]["end"] == "1"
    # Ticks strictly increase.
    ticks = [b["tick"] for b in body["boundaries"]]
    assert ticks == sorted(set(ticks))


def test_quantization_preview_infeasible_returns_422_and_does_not_save():
    measure_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    # A one-division grid only has ticks 0 and 1 for five distinct edges.
    response = client.post(
        f"/measures/{measure_id}/quantization-preview",
        json={"divisions": 1, "max_shift": "1"},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["divisions"] == 1
    assert detail["max_shift"] == "1"
    assert "reason" in detail
    assert "saved" in detail["message"]

    # A tight max_shift that no tick for 1/4 can satisfy on an eighth
    # grid? nearest eighth is exactly 2/8 = 1/4, so instead request a
    # grid whose tick spacing leaves no close tick: d = 3 (step 1/3),
    # boundary 1/2 sits between ticks 1 (1/3) and 2 (2/3), both 1/6
    # away; max_shift 1/8 < 1/6.
    response = client.post(
        f"/measures/{measure_id}/quantization-preview",
        json={"divisions": 3, "max_shift": "1/8"},
    )
    assert response.status_code == 422, response.text


def test_quantization_preview_404_for_missing_measure():
    response = client.post(
        "/measures/999999/quantization-preview",
        json={"divisions": 4, "max_shift": "0"},
    )
    assert response.status_code == 404


def test_quantization_preview_validates_payload():
    measure_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    # divisions out of the 1..128 range.
    for divisions in (0, 129, -1):
        response = client.post(
            f"/measures/{measure_id}/quantization-preview",
            json={"divisions": divisions, "max_shift": "0"},
        )
        assert response.status_code == 422, divisions
    # max_shift must be a non-negative rational string.
    for bad in ("-1/2", "abc", "1.5", "1/0"):
        response = client.post(
            f"/measures/{measure_id}/quantization-preview",
            json={"divisions": 4, "max_shift": bad},
        )
        assert response.status_code == 422, bad
    # "0" is a valid non-negative max_shift.
    response = client.post(
        f"/measures/{measure_id}/quantization-preview",
        json={"divisions": 4, "max_shift": "0"},
    )
    assert response.status_code == 200, response.text
    # Missing body / field.
    assert client.post(
        f"/measures/{measure_id}/quantization-preview", json={}
    ).status_code == 422


# --- POST /loop-alignments -------------------------------------------------

# Loop B: length 3/2 (target 3/2), "bass" rests all the way through.
LONG_LOOP_PAYLOAD = {
    "target_duration": "3/2",
    "voices": [
        {
            "name": "melody",
            "events": [
                {"kind": "sound", "duration": "1/2"},
                {"kind": "rest", "duration": "1"},
            ],
        },
        {
            "name": "bass",
            "events": [{"kind": "rest", "duration": "3/2"}],
        },
    ],
}


def test_loop_alignment_realigns_two_different_length_loops():
    a_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    b_id = client.post("/measures", json=LONG_LOOP_PAYLOAD).json()["id"]
    response = client.post(
        "/loop-alignments", json={"a_measure_id": a_id, "b_measure_id": b_id}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # lcm(1, 3/2) = 3: A repeats 3 times, B twice.
    assert body["horizon"] == "3"
    assert body["length_a"] == "1"
    assert body["length_b"] == "3/2"
    assert body["repetitions"] == {"a": 3, "b": 2}
    assert body["voices"] == {"a": ["melody", "bass"], "b": ["melody", "bass"]}
    # 5 A events per loop * 3 + 3 B events per loop * 2 = 21 copies.
    assert body["expanded_events"] == 21

    segments = body["segments"]
    assert segments[0]["start"] == "0"
    assert segments[-1]["end"] == "3"
    for left, right in zip(segments, segments[1:]):
        assert left["end"] == right["start"]

    # First segment: both loops in their first iteration.
    first = segments[0]
    assert first["start"] == "0"
    assert first["end"] == "1/4"
    assert first["a"] == {
        "melody": {"event_id": "v1e1", "loop": 1},
        "bass": None,
    }
    assert first["b"] == {
        "melody": {"event_id": "v1e1", "loop": 1},
        "bass": None,
    }

    # At t = 3/2 both loops restart together (A for the 2nd time, B too).
    # A's melody pattern rests for its first quarter-offset there, so the
    # half-open segment starting at 3/2 shows A resting while B's long
    # opening sound is tagged with loop 2.
    joint = next(segment for segment in segments if segment["start"] == "3/2")
    assert joint["a"]["melody"] is None
    assert joint["b"]["melody"] == {"event_id": "v1e1", "loop": 2}

    # A quarter later both are still in their second loops: A's final
    # event of the pattern, B still inside its opening half-unit sound.
    later = next(segment for segment in segments if segment["start"] == "7/4")
    assert later["end"] == "2"
    assert later["a"]["melody"] == {"event_id": "v1e3", "loop": 2}
    assert later["b"]["melody"] == {"event_id": "v1e1", "loop": 2}

    # The final segment [11/4, 3) is A's third loop while B is still in
    # its second; after it both are back at the start simultaneously.
    assert segments[-1]["start"] == "11/4"
    assert segments[-1]["end"] == "3"
    assert segments[-1]["a"]["melody"] == {"event_id": "v1e3", "loop": 3}
    assert segments[-1]["b"]["melody"] is None


def test_loop_alignment_accepts_same_measure_for_a_and_b():
    measure_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    response = client.post(
        "/loop-alignments",
        json={"a_measure_id": measure_id, "b_measure_id": measure_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["horizon"] == "1"
    assert body["repetitions"] == {"a": 1, "b": 1}
    # Same grid as the single measure, duplicated under a and b.
    assert [(s["start"], s["end"]) for s in body["segments"]] == [
        ("0", "1/4"),
        ("1/4", "1/2"),
        ("1/2", "3/4"),
        ("3/4", "1"),
    ]
    assert body["segments"][0]["a"] == body["segments"][0]["b"]


def test_loop_alignment_missing_measure_returns_404():
    measure_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    response = client.post(
        "/loop-alignments",
        json={"a_measure_id": measure_id, "b_measure_id": 999999},
    )
    assert response.status_code == 404
    assert "b" in response.json()["detail"]

    response = client.post(
        "/loop-alignments",
        json={"a_measure_id": 999998, "b_measure_id": 999999},
    )
    assert response.status_code == 404
    assert "segments" not in response.text


def test_loop_alignment_over_expansion_limit_returns_422_without_timeline():
    # One event per voice, two voices, length 1/1000: each loop repeats
    # 1000 times against a length-1 loop -> 4000 > 2000 copies.
    tiny = {
        "target_duration": "1/1000",
        "voices": [
            {"name": "a", "events": [{"kind": "sound", "duration": "1/1000"}]},
            {"name": "b", "events": [{"kind": "rest", "duration": "1/1000"}]},
        ],
    }
    one = {
        "target_duration": "1",
        "voices": [
            {"name": "a", "events": [{"kind": "sound", "duration": "1"}]},
            {"name": "b", "events": [{"kind": "rest", "duration": "1"}]},
        ],
    }
    tiny_id = client.post("/measures", json=tiny).json()["id"]
    one_id = client.post("/measures", json=one).json()["id"]
    response = client.post(
        "/loop-alignments",
        json={"a_measure_id": tiny_id, "b_measure_id": one_id},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["expanded_events"] == 2 * 1000 + 2
    assert detail["limit"] == 2000
    assert detail["horizon"] == "1"
    assert detail["repetitions"] == {"a": 1000, "b": 1}
    # No truncated timeline is output.
    assert "segments" not in detail
    assert "segments" not in response.text


# --- GET /measures/{id}/rhythm-matches -------------------------------------

# Source measure (length 1):
#   lead: sound 1/4, rest 1/4, sound 1/2  -> [S, R, S]
#   pad:  rest 1/2, rest 1/2              (different event count)
#   twin: sound 1/4, rest 1/4, sound 1/2  (same measure, same rhythm)
RHYTHM_SOURCE_PAYLOAD = {
    "target_duration": "1",
    "voices": [
        {
            "name": "lead",
            "events": [
                {"kind": "sound", "duration": "1/4"},
                {"kind": "rest", "duration": "1/4"},
                {"kind": "sound", "duration": "1/2"},
            ],
        },
        {"name": "pad", "events": [
            {"kind": "rest", "duration": "1/2"},
            {"kind": "rest", "duration": "1/2"},
        ]},
        {
            "name": "twin",
            "events": [
                {"kind": "sound", "duration": "1/4"},
                {"kind": "rest", "duration": "1/4"},
                {"kind": "sound", "duration": "1/2"},
            ],
        },
    ],
}

# Candidate measure (length 2):
#   echo: sound 1, sound 1/2, rest 1/2  -> lead doubled in tempo and
#         cyclically shifted by 2 (candidate j = source (j+2) mod 3)
#   lead: same *name* as the source but 4 events: name alone must not
#         match, and only the source's own (measure, voice) is excluded.
#   drone: rest 2                       (different event count)
RHYTHM_CANDIDATE_PAYLOAD = {
    "target_duration": "2",
    "voices": [
        {
            "name": "echo",
            "events": [
                {"kind": "sound", "duration": "1"},
                {"kind": "sound", "duration": "1/2"},
                {"kind": "rest", "duration": "1/2"},
            ],
        },
        {
            "name": "lead",
            "events": [
                {"kind": "sound", "duration": "1/4"},
                {"kind": "rest", "duration": "1/4"},
                {"kind": "sound", "duration": "1/2"},
                {"kind": "rest", "duration": "1"},
            ],
        },
        {"name": "drone", "events": [{"kind": "rest", "duration": "2"}]},
    ],
}


def test_rhythm_matches_returns_ratio_and_smallest_shift_in_order():
    source_id = client.post("/measures", json=RHYTHM_SOURCE_PAYLOAD).json()["id"]
    candidate_id = client.post("/measures", json=RHYTHM_CANDIDATE_PAYLOAD).json()["id"]

    response = client.get(f"/measures/{source_id}/rhythm-matches", params={"voice": "lead"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["measure_id"] == source_id
    assert body["voice"] == "lead"

    # Ordered by measure id then the voices' original stored order; the
    # source voice itself, "pad", "drone" and the same-named "lead" of
    # the other measure do not appear.
    assert body["matches"] == [
        {"measure_id": source_id, "voice": "twin", "ratio": "1", "shift": 0},
        {"measure_id": candidate_id, "voice": "echo", "ratio": "2", "shift": 2},
    ]


def test_rhythm_matches_empty_for_voice_without_equivalents():
    # Five events with a distinct kind pattern: no other saved voice in
    # the suite has five events, so no equivalent can exist even though
    # the database is shared across tests.
    payload = {
        "target_duration": "1",
        "voices": [
            {
                "name": "lonely",
                "events": [
                    {"kind": "sound", "duration": "1/5"},
                    {"kind": "rest", "duration": "1/5"},
                    {"kind": "sound", "duration": "1/5"},
                    {"kind": "rest", "duration": "1/5"},
                    {"kind": "rest", "duration": "1/5"},
                ],
            },
            {"name": "filler", "events": [{"kind": "rest", "duration": "1"}]},
        ],
    }
    source_id = client.post("/measures", json=payload).json()["id"]
    response = client.get(
        f"/measures/{source_id}/rhythm-matches", params={"voice": "lonely"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["matches"] == []


def test_rhythm_matches_404_for_missing_measure_or_voice():
    assert (
        client.get("/measures/999999/rhythm-matches", params={"voice": "lead"}).status_code
        == 404
    )
    source_id = client.post("/measures", json=RHYTHM_SOURCE_PAYLOAD).json()["id"]
    response = client.get(
        f"/measures/{source_id}/rhythm-matches", params={"voice": "nope"}
    )
    assert response.status_code == 404


def test_rhythm_matches_requires_voice_query_parameter():
    source_id = client.post("/measures", json=RHYTHM_SOURCE_PAYLOAD).json()["id"]
    assert client.get(f"/measures/{source_id}/rhythm-matches").status_code == 422


# --- POST /measures/{id}/avoidance-preview ---------------------------------

# Unique-optimum measure (target 1):
#   lead: sound [0, 1/3]
#   bass: sound [0, 1/4], sound [1/4, 3/8], rest to the end
# Conflicts (lead, bass): (v1e1, v2e1) overlap 1/4, (v1e1, v2e2) 1/12.
# Muting the lead event costs 1/3; muting both bass events costs
# 1/4 + 1/8 = 3/8, so the unique optimum is muting the lead event.
AVOIDANCE_PAYLOAD = {
    "target_duration": "1",
    "voices": [
        {"name": "lead", "events": [
            {"kind": "sound", "duration": "1/3"},
            {"kind": "rest", "duration": "2/3"},
        ]},
        {"name": "bass", "events": [
            {"kind": "sound", "duration": "1/4"},
            {"kind": "sound", "duration": "1/8"},
            {"kind": "rest", "duration": "5/8"},
        ]},
    ],
}


def test_avoidance_preview_returns_minimum_muting_and_conflicts():
    measure_id = client.post("/measures", json=AVOIDANCE_PAYLOAD).json()["id"]
    response = client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "lead", "voice_b": "bass"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["measure_id"] == measure_id
    assert body["target_duration"] == "1"
    assert body["voice_a"] == "lead"
    assert body["voice_b"] == "bass"

    assert body["conflicts"] == [
        {
            "a": {"id": "v1e1", "voice": "lead", "start": "0",
                  "end": "1/3", "duration": "1/3"},
            "b": {"id": "v2e1", "voice": "bass", "start": "0",
                  "end": "1/4", "duration": "1/4"},
            "overlap": "1/4",
        },
        {
            "a": {"id": "v1e1", "voice": "lead", "start": "0",
                  "end": "1/3", "duration": "1/3"},
            "b": {"id": "v2e2", "voice": "bass", "start": "1/4",
                  "end": "3/8", "duration": "1/8"},
            "overlap": "1/12",
        },
    ]

    # Unique optimum: mute the single lead event whole (full 1/3, not
    # just the 1/3 it sounds for across overlaps).
    assert body["muted"] == [
        {"id": "v1e1", "voice": "lead", "start": "0",
         "end": "1/3", "duration": "1/3"},
    ]
    assert body["total_muted"] == "1/3"


def test_avoidance_preview_tied_weights_still_cover_every_conflict():
    # VALID_PAYLOAD: melody sounds [0,1/2] and [3/4,1], bass sound
    # [1/4,1]. Both conflicts overlap 1/4 each. Covers {melody's two
    # sounds} and {bass's one sound} both cost 3/4: the response must be
    # one of these optimal covers and total exactly 3/4.
    measure_id = client.post("/measures", json=VALID_PAYLOAD).json()["id"]
    response = client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "melody", "voice_b": "bass"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert [
        (c["a"]["id"], c["b"]["id"], c["overlap"]) for c in body["conflicts"]
    ] == [
        ("v1e1", "v2e2", "1/4"),
        ("v1e3", "v2e2", "1/4"),
    ]
    muted = {(m["voice"], m["id"]) for m in body["muted"]}
    assert muted in (
        {("melody", "v1e1"), ("melody", "v1e3")},
        {("bass", "v2e2")},
    )
    assert body["total_muted"] == "3/4"

    # Every reported conflict is covered by at least one muted event.
    for conflict in body["conflicts"]:
        assert (
            ("melody", conflict["a"]["id"]) in muted
            or ("bass", conflict["b"]["id"]) in muted
        )


def test_avoidance_preview_no_conflict_mutes_nothing():
    payload = {
        "target_duration": "1",
        "voices": [
            {"name": "a", "events": [
                {"kind": "sound", "duration": "1/2"},
                {"kind": "rest", "duration": "1/2"},
            ]},
            {"name": "b", "events": [
                {"kind": "rest", "duration": "1/2"},
                {"kind": "sound", "duration": "1/2"},
            ]},
        ],
    }
    measure_id = client.post("/measures", json=payload).json()["id"]
    response = client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "a", "voice_b": "b"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Boundary contact at 1/2 is not a conflict: nothing to mute.
    assert body["conflicts"] == []
    assert body["muted"] == []
    assert body["total_muted"] == "0"


def test_avoidance_preview_is_read_only():
    measure_id = client.post("/measures", json=AVOIDANCE_PAYLOAD).json()["id"]
    before = client.get(f"/measures/{measure_id}").json()
    response = client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "lead", "voice_b": "bass"},
    )
    assert response.status_code == 200
    after = client.get(f"/measures/{measure_id}").json()
    assert after == before


def test_avoidance_preview_404_for_missing_measure():
    response = client.post(
        "/measures/999999/avoidance-preview",
        json={"voice_a": "lead", "voice_b": "bass"},
    )
    assert response.status_code == 404


def test_avoidance_preview_rejects_same_voice():
    measure_id = client.post("/measures", json=AVOIDANCE_PAYLOAD).json()["id"]
    response = client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "lead", "voice_b": "lead"},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["voice_a"] == "lead"
    assert detail["voice_b"] == "lead"
    assert "different" in detail["message"]


def test_avoidance_preview_rejects_unknown_voices():
    measure_id = client.post("/measures", json=AVOIDANCE_PAYLOAD).json()["id"]
    response = client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "lead", "voice_b": "drums"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["missing_voices"] == ["drums"]

    response = client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "drums", "voice_b": "piano"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["missing_voices"] == ["drums", "piano"]

    # Missing body / fields are also request-validation errors.
    assert client.post(
        f"/measures/{measure_id}/avoidance-preview", json={}
    ).status_code == 422
    assert client.post(
        f"/measures/{measure_id}/avoidance-preview",
        json={"voice_a": "lead"},
    ).status_code == 422


def test_avoidance_preview_404_takes_precedence_over_same_voice():
    response = client.post(
        "/measures/999999/avoidance-preview",
        json={"voice_a": "x", "voice_b": "x"},
    )
    assert response.status_code == 404
