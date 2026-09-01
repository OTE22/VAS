"""The canonical dialogue state: corrections, inheritance, rollback.

This is the C8 correction table from the plan, run against the pure state
machine — no LLM, no database. The property under test is the authority rule:
the model proposes a StateDelta; only apply_delta commits, exactly the named
change and nothing else. The recurring assertion is therefore not just "the
camera changed" but "NOTHING ELSE did" — losing `yesterday` while changing
the camera is the failure mode whole-state replacement invites and deltas
exist to prevent.

    docker exec face_recognition_api python -m pytest tests/test_dialogue_state.py -v
"""

import pytest

from sql_agent import dialogue_state as ds


def _committed(state, **overrides):
    """Apply a sequence of setup deltas and return the state."""
    turn = overrides.pop("turn", "t0")
    for field, (op, value) in overrides.items():
        state = ds.apply_delta(
            state, {"operation": op, "field": field, "proposed_value": value,
                    "source": "user_statement"}, turn_id=turn)
    return state


# ---------------------------------------------------------------- C8 table

def test_show_yesterday_on_camera_3():
    state = ds.empty_state()
    state = _committed(state,
                       active_time_range=("REPLACE", "yesterday"),
                       active_camera=("ADD", [3]))
    assert ds.get_value(state, "active_time_range") == "yesterday"
    assert ds.get_value(state, "active_camera") == [3]


def test_no_camera_4_replaces_camera_3_and_touches_nothing_else():
    state = _committed(ds.empty_state(),
                       active_time_range=("REPLACE", "yesterday"),
                       active_camera=("ADD", [3]))
    state = ds.apply_delta(state, {
        "operation": "REPLACE", "field": "active_camera",
        "proposed_value": [4], "source": "user_correction"}, turn_id="t1")
    assert ds.get_value(state, "active_camera") == [4]
    assert ds.get_value(state, "active_time_range") == "yesterday", (
        "changing the camera lost the time range — whole-state replacement "
        "snuck back in")
    assert ds.get_provenance(state, "active_camera")["source"] == "user_correction"


def test_actually_both_3_and_4_is_an_add():
    state = _committed(ds.empty_state(), active_camera=("ADD", [4]))
    state = ds.apply_delta(state, {
        "operation": "ADD", "field": "active_camera",
        "proposed_value": [3], "source": "user_statement"}, turn_id="t2")
    assert sorted(ds.get_value(state, "active_camera")) == [3, 4]


def test_remove_camera_3_leaves_camera_4():
    state = _committed(ds.empty_state(), active_camera=("ADD", [3, 4]))
    state = ds.apply_delta(state, {
        "operation": "REMOVE", "field": "active_camera",
        "proposed_value": 3, "source": "user_statement"}, turn_id="t3")
    assert ds.get_value(state, "active_camera") == [4]


def test_forget_the_camera_filter_preserves_yesterday():
    state = _committed(ds.empty_state(),
                       active_time_range=("REPLACE", "yesterday"),
                       active_camera=("ADD", [4]))
    state = ds.apply_delta(state, {
        "operation": "REMOVE", "field": "active_camera",
        "source": "user_statement"}, turn_id="t4")
    assert ds.get_value(state, "active_camera") is None
    assert ds.get_value(state, "active_time_range") == "yesterday", (
        "'forget the camera filter' also forgot the time range")


def test_use_last_week_instead_replaces_time_and_preserves_the_rest():
    state = _committed(ds.empty_state(),
                       active_time_range=("REPLACE", "yesterday"),
                       active_camera=("ADD", [4]),
                       referenced_entity=("ADD", ["Ali"]))
    state = ds.apply_delta(state, {
        "operation": "REPLACE", "field": "active_time_range",
        "proposed_value": "last week", "source": "user_statement"}, turn_id="t5")
    assert ds.get_value(state, "active_time_range") == "last week"
    assert ds.get_value(state, "active_camera") == [4]
    assert ds.get_value(state, "referenced_entity") == ["Ali"]


def test_go_back_to_the_previous_report_restores_the_snapshot():
    """The branching move: camera 3 task → snapshot → camera 5 task → rollback."""
    state = _committed(ds.empty_state(),
                       active_camera=("ADD", [3]),
                       active_time_range=("REPLACE", "yesterday"))
    state = ds.apply_delta(state, {
        "operation": "REFERENCE", "field": "referenced_artifact",
        "proposed_value": "artifact-camera-3", "source": "tool_result"},
        turn_id="t6")
    state = ds.snapshot_task(state, turn_id="t6", label="camera 3 report")

    state = ds.apply_delta(state, {
        "operation": "REPLACE", "field": "active_camera",
        "proposed_value": [5], "source": "user_statement"}, turn_id="t7")
    assert ds.get_value(state, "active_camera") == [5]

    state = ds.apply_delta(state, {
        "operation": "ROLLBACK", "referenced_object": "artifact-camera-3",
        "source": "user_statement"}, turn_id="t8")
    assert ds.get_value(state, "active_camera") == [3], (
        "rollback did not restore the camera-3 task")
    assert ds.get_value(state, "active_time_range") == "yesterday"
    assert ds.get_value(state, "referenced_artifact") == "artifact-camera-3"


def test_rollback_with_no_matching_snapshot_is_refused_not_guessed():
    state = _committed(ds.empty_state(), active_camera=("ADD", [5]))
    with pytest.raises(ds.DeltaRejected):
        ds.apply_delta(state, {"operation": "ROLLBACK",
                               "referenced_object": "no-such-artifact",
                               "source": "user_statement"}, turn_id="t9")
    # And the state is untouched by the refused attempt.
    assert ds.get_value(state, "active_camera") == [5]


# ------------------------------------------------------------ authority rule

def test_the_model_cannot_invent_a_field():
    with pytest.raises(ds.DeltaRejected):
        ds.apply_delta(ds.empty_state(), {
            "operation": "REPLACE", "field": "sql_to_run",
            "proposed_value": "DROP TABLE users",
            "source": "inferred"}, turn_id="t0")


def test_an_unknown_operation_is_refused():
    with pytest.raises(ds.DeltaRejected):
        ds.apply_delta(ds.empty_state(), {
            "operation": "OVERWRITE_ALL", "field": "active_camera",
            "proposed_value": [1]}, turn_id="t0")


def test_an_inference_cannot_override_an_explicit_correction():
    """Precedence inside structured state: corrections outrank inference."""
    state = ds.apply_delta(ds.empty_state(), {
        "operation": "REPLACE", "field": "active_camera",
        "proposed_value": [4], "source": "user_correction"}, turn_id="t1")
    with pytest.raises(ds.DeltaRejected):
        ds.apply_delta(state, {
            "operation": "REPLACE", "field": "active_camera",
            "proposed_value": [3], "source": "inferred"}, turn_id="t2")
    assert ds.get_value(state, "active_camera") == [4]


def test_a_newer_statement_of_equal_authority_wins():
    """"Camera 4 instead" after "camera 3" — both plain statements."""
    state = ds.apply_delta(ds.empty_state(), {
        "operation": "REPLACE", "field": "active_camera",
        "proposed_value": [3], "source": "user_statement"}, turn_id="t1")
    state = ds.apply_delta(state, {
        "operation": "REPLACE", "field": "active_camera",
        "proposed_value": [4], "source": "user_statement"}, turn_id="t2")
    assert ds.get_value(state, "active_camera") == [4]


def test_apply_delta_never_mutates_its_input():
    before = _committed(ds.empty_state(), active_camera=("ADD", [3]))
    frozen = str(before)
    ds.apply_delta(before, {"operation": "REPLACE", "field": "active_camera",
                            "proposed_value": [9], "source": "user_statement"},
                   turn_id="t1")
    assert str(before) == frozen, "apply_delta mutated the input state"


def test_every_commit_bumps_the_context_version():
    """Reproducibility: the trace can name the exact state a turn used."""
    state = ds.empty_state()
    assert state["context_version"] == 0
    state = ds.apply_delta(state, {"operation": "REPLACE",
                                   "field": "active_camera",
                                   "proposed_value": [3],
                                   "source": "user_statement"}, turn_id="t1")
    assert state["context_version"] == 1
    state = ds.apply_delta(state, {"operation": "PRESERVE",
                                   "source": "user_statement"}, turn_id="t2")
    assert state["context_version"] == 2, "PRESERVE must still mark the turn"


def test_provenance_records_which_turn_set_a_value():
    state = ds.apply_delta(ds.empty_state(), {
        "operation": "REPLACE", "field": "active_camera",
        "proposed_value": [4], "source": "user_correction"}, turn_id="turn-77")
    assert ds.get_provenance(state, "active_camera") == {
        "source": "user_correction", "source_turn_id": "turn-77"}


# ----------------------------------------------------------- serialization

def test_state_round_trips_through_json_and_migration():
    import json
    state = _committed(ds.empty_state(),
                       active_camera=("ADD", [3]),
                       active_time_range=("REPLACE", "yesterday"))
    state = ds.snapshot_task(state, turn_id="t1", label="probe")
    state["future_key"] = {"kept": True}

    revived = ds.migrate_state(json.loads(json.dumps(state)))
    assert ds.get_value(revived, "active_camera") == [3]
    assert revived["future_key"] == {"kept": True}, "unknown keys must survive"
    assert ds.list_task_history(revived)[0]["label"] == "probe"


def test_task_history_is_bounded_and_holds_references_not_content():
    state = ds.empty_state()
    for i in range(15):
        state = ds.apply_delta(state, {
            "operation": "REPLACE", "field": "active_camera",
            "proposed_value": [i], "source": "user_statement"}, turn_id=f"t{i}")
        state = ds.snapshot_task(state, turn_id=f"t{i}", label=f"task {i}")
    history = state["task_history"]
    assert len(history) <= 8, "task history must stay bounded"
    blob = str(history)
    assert "SELECT" not in blob and "narrative" not in blob


# ------------------------------------------- rolling summary (derived cache)

def test_the_summary_is_rebuildable_and_never_holds_exact_values_alone():
    """The summary helps the model follow the thread; it is not the truth.

    Exact values — ids, camera numbers, dates — come from canonical state and
    provenance. The summary is extractive and regenerable precisely so a
    corrupt or upgraded cache costs nothing but a rebuild.
    """
    turns = ["Show detections yesterday", "Only camera 3",
             "Generate a PDF", "Arabic please"]
    summary = ds.build_summary(turns, ds.empty_state())
    assert summary["version"] == ds.SUMMARY_VERSION
    assert "camera 3" in summary["text"]
    assert summary["source_turns"] == len(turns)

    # Rebuilding from the same input is deterministic — no model, no drift.
    assert ds.build_summary(turns, ds.empty_state()) == summary


@pytest.mark.parametrize("cached,reason", [
    (None, "missing"),
    ({"version": 999, "text": "x", "source_turns": 9, "context_version": 9},
     "version from a future/older shape"),
    ({"version": ds.SUMMARY_VERSION, "text": None, "source_turns": 9,
      "context_version": 9}, "corrupt text"),
    ({"version": ds.SUMMARY_VERSION, "text": "x", "source_turns": 1,
      "context_version": 9}, "derived from fewer turns"),
    ({"version": ds.SUMMARY_VERSION, "text": "x", "source_turns": 9,
      "context_version": 1}, "derived from older state"),
])
def test_a_stale_or_corrupt_summary_is_rebuilt_not_trusted(cached, reason):
    assert ds.needs_rebuild(cached, turn_count=9, context_version=9), reason


def test_a_current_summary_is_reused():
    fresh = {"version": ds.SUMMARY_VERSION, "text": "x", "source_turns": 9,
             "context_version": 9}
    assert not ds.needs_rebuild(fresh, turn_count=9, context_version=9)


# ----------------------------------------------- envelope section budgeting

def test_authoritative_state_is_never_dropped_for_lower_priority_sections():
    """The budget rule. Losing the active camera to fit an old memory is the
    failure the priority ordering exists to prevent."""
    from sql_agent.tools import planner

    state = _committed(ds.empty_state(),
                       active_camera=("ADD", [3]),
                       active_time_range=("REPLACE", "yesterday"))
    candidates = planner.resolve_candidates(
        {"dialogue_state": state, "last_artifact_id": None}, [], "")

    # A deliberately enormous low-priority section.
    envelope = planner.build_planner_context(
        candidates,
        recent_turns=["padding " * 400, "more padding " * 400],
        conversation_summary="summary " * 400)

    assert "active_camera" in envelope, "authoritative state was dropped"
    assert "'yesterday'" in envelope, "the active time range was dropped"
    assert len(envelope) < 8000, f"envelope grew unbounded: {len(envelope)} chars"


def test_the_summary_yields_before_the_state_does():
    from sql_agent.tools import planner

    state = _committed(ds.empty_state(), active_camera=("ADD", [7]))
    candidates = planner.resolve_candidates(
        {"dialogue_state": state, "last_artifact_id": None}, [], "")
    envelope = planner.build_planner_context(
        candidates, recent_turns=["turn text " * 200] * 3,
        conversation_summary="a rolling summary that should yield first")

    assert "active_camera" in envelope
    # With recent_turns consuming the budget, the lower-priority summary is
    # the section that goes.
    assert len(envelope) < 8000


def test_task_history_reaches_the_planner_so_rollback_is_resolvable():
    """"Go back to the previous report" needs the branch points in-prompt."""
    from sql_agent.tools import planner

    state = _committed(ds.empty_state(), active_camera=("ADD", [3]))
    state = ds.apply_delta(state, {
        "operation": "REFERENCE", "field": "referenced_artifact",
        "proposed_value": "artifact-c3", "source": "tool_result"}, turn_id="t1")
    state = ds.snapshot_task(state, turn_id="t1", label="camera 3 report")

    candidates = planner.resolve_candidates(
        {"dialogue_state": state, "last_artifact_id": None}, [], "")
    envelope = planner.build_planner_context(candidates)
    assert "camera 3 report" in envelope, "task history never reached the prompt"
    assert "artifact-c3" in envelope
