"""Fifth battery (2026-09-06, Opik 01a07860-f7c9 / 01a0785e-cc62 / 01a0785a-d39e /
01a0785d-1e56): a file asked for before any result is queried first and
rendered from the narration; a file format named in the message is carried;
a follow-up keeps the user's words; markdown tables render as tables."""
from sql_agent.services import export_builders as eb
from sql_agent.tools import interpreter
from sql_agent.tools.agent_tools import SQLAgentTools as T


def test_a_named_file_format_is_a_closed_vocabulary():
    assert interpreter.requested_file_format("Give me a PDF report of IRON MAN's detections") == "pdf"
    assert interpreter.requested_file_format("أريد تقرير تتبع JOEY بصيغة PDF") == "pdf"
    assert interpreter.requested_file_format("send it as a .docx") == "word"
    assert interpreter.requested_file_format("in other words, who was there?") is None
    assert interpreter.requested_file_format("track joey") is None


def _tools(monkeypatch, cameras):
    tools = T.__new__(T)
    monkeypatch.setattr(tools, "_all_cameras", lambda: [{"location": c} for c in cameras])
    return tools


def test_a_document_request_naming_a_person_queries_first_then_renders(monkeypatch):
    tools = _tools(monkeypatch, ["WEZARET DEFA3"])
    state = {"reasoning_steps_used": 0, "observations": [],
             "normalized_input": "أريد تقرير تتبع JOEY بصيغة PDF باللغة العربية",
             "identity_index": [{"display_name": "JOEY"}]}
    call = {"name": "generate_document", "arguments": {"format": "pdf", "language": "ar"}}
    trace = [{"tool": "generate_document", "committed": True,
              "signature": ["generate_document", "sig"]}]
    plan = tools._apply_model_tool_call(state, call, trace, {})
    assert plan.action == "query_database", "nothing to render yet: the data comes first"
    assert state["document_after_query"] == {"format": "pdf", "language": "ar"}
    assert state["sql_generation_input"].startswith("أريد تقرير تتبع JOEY")


def test_a_document_request_naming_nobody_still_asks(monkeypatch):
    tools = _tools(monkeypatch, ["WEZARET DEFA3"])
    state = {"reasoning_steps_used": 0, "observations": [],
             "normalized_input": "make me a PDF", "identity_index": [{"display_name": "JOEY"}]}
    call = {"name": "generate_document", "arguments": {"format": "pdf"}}
    trace = [{"tool": "generate_document", "committed": True,
              "signature": ["generate_document", "sig"]}]
    plan = tools._apply_model_tool_call(state, call, trace, {})
    assert plan.action == "clarify"
    assert "document_after_query" not in state


def test_a_query_whose_message_names_a_pdf_carries_the_file(monkeypatch):
    tools = _tools(monkeypatch, [])
    state = {"reasoning_steps_used": 0, "observations": [],
             "normalized_input": "Give me a PDF report of IRON MAN's detections",
             "identity_index": [{"display_name": "IRON MAN"}]}
    call = {"name": "query_database", "arguments": {"question": "detections of IRON MAN",
                                                    "response_shape": "report"}}
    trace = [{"tool": "query_database", "committed": True, "signature": ["query_database", "q"]}]
    plan = tools._apply_model_tool_call(state, call, trace, {})
    assert plan.action == "query_database"
    assert state["document_after_query"]["format"] == "pdf"


def test_a_follow_up_keeps_the_users_words_and_names_what_it_refers_to(monkeypatch):
    tools = _tools(monkeypatch, [])
    state = {"reasoning_steps_used": 0, "observations": [],
             "normalized_input": "Which of those cameras saw him the most?",
             "identity_index": [{"display_name": "IRON MAN"}]}
    call = {"name": "query_database", "arguments": {
        "question": "Return every row where IRON MAN was detected, with camera name and timestamp",
        "response_shape": "report", "uses_context": True}}
    trace = [{"tool": "resolve_person", "ok": True, "signature": ["resolve_person", "x"],
              "observation": {"status": "ok", "tool": "resolve_person"},
              "resolved_entity": {"tool": "resolve_person", "raw_text": "him",
                                  "identity_id": "p1", "canonical_name": "IRON MAN"}},
             {"tool": "query_database", "committed": True, "signature": ["query_database", "q"]}]
    candidates = {"last_result": {"question": "At which cameras was IRON MAN detected?", "row_count": 8}}
    tools._apply_model_tool_call(state, call, trace, candidates)
    composed = state["sql_generation_input"]
    assert composed == ('Which of those cameras saw him the most? (about IRON MAN; '
                        'a follow-up to: "At which cameras was IRON MAN detected?")')
    # nobody resolved, but the previous question names a stored person
    state3 = {"reasoning_steps_used": 0, "observations": [],
              "normalized_input": "وفي أي كاميرات؟", "identity_index": [{"display_name": "IRON MAN"}]}
    tools._apply_model_tool_call(
        state3, {"name": "query_database", "arguments": {"question": "at which cameras", "uses_context": True}},
        [{"tool": "query_database", "committed": True, "signature": ["query_database", "q"]}],
        {"last_result": {"question": "كم مرة تم رصد IRON MAN؟", "row_count": 1}})
    assert state3["sql_generation_input"].startswith("وفي أي كاميرات؟ (about IRON MAN;")
    # with nobody resolved, the previous question is the reference
    state2 = {"reasoning_steps_used": 0, "observations": [],
              "normalized_input": "Only on 2026-08-18", "identity_index": []}
    tools._apply_model_tool_call(
        state2, {"name": "query_database", "arguments": {"question": "detections on 2026-08-18",
                                                          "uses_context": True}},
        [{"tool": "query_database", "committed": True, "signature": ["query_database", "q"]}],
        {"last_result": {"question": "Which camera had the most detections?", "row_count": 1}})
    assert state2["sql_generation_input"] == (
        'Only on 2026-08-18 (a follow-up to: "Which camera had the most detections?")')


def test_the_render_node_takes_the_pending_file_from_the_query_turn(monkeypatch):
    tools = T.__new__(T)
    tools.conversation_memory = None
    state = {"planned_action": {"action": "query_database", "source": "tool_loop"},
             "document_after_query": {"format": "pdf", "language": "en"},
             "final_response": "JOEY appears at WEZARET DEFA3 on 2026-08-20 at 20:23:26.",
             "response_language": "en", "working_context": {}}
    try:
        tools.render_artifact(state)
    except Exception:
        pass  # rendering needs the export stack; the plan rewrite is what is pinned
    assert state["planned_action"]["action"] == "generate_document"
    assert state["planned_action"]["format"] == "pdf"
    assert "document_after_query" not in state


def test_markdown_tables_become_cell_grids():
    lines = ["| الكاميرا | التاريخ | الاسم |", "| --- | --- | --- |",
             "| MAD5AL AMEN | 2026-08-17 13:04:26 | IRON MAN |",
             "| IT-DIRECTORY | 2026-08-18 04:43:26 | IRON MAN |"]
    grid = eb._table_rows(lines)
    assert grid == [["الكاميرا", "التاريخ", "الاسم"],
                    ["MAD5AL AMEN", "2026-08-17 13:04:26", "IRON MAN"],
                    ["IT-DIRECTORY", "2026-08-18 04:43:26", "IRON MAN"]]
    runs = eb._split_table_runs(["**KEY FINDINGS**"] + lines + ["Summary line"])
    assert [is_table for is_table, _ in runs] == [False, True, False]


def test_a_pdf_with_a_markdown_table_builds_without_pipes_in_the_text():
    content = ("**KEY FINDINGS**\n| Camera | Detections |\n| --- | --- |\n"
               "| MAD5AL AMEN (1) | 4 |\n| IT-DIRECTORY | 2 |\n\nEnd of report.")
    data = eb.build_pdf_bytes("IRON MAN - tracking report", content, "2026-09-06", "Agent")
    assert data[:4] == b"%PDF"
    assert b"| ---" not in data


def test_the_readings_own_document_plan_queries_first_when_nothing_exists(monkeypatch):
    from sql_agent.tools import interpreter as _interp
    tools = _tools(monkeypatch, [])
    state = {"normalized_input": "أريد تقرير تتبع JOEY بصيغة PDF باللغة العربية",
             "identity_index": [{"display_name": "JOEY"}]}
    reading = _interp.Interpretation(wants=_interp.DOCUMENT, question="", people=[],
                                     language="ar", format="pdf", confidence=0.9)
    plan = tools._plan_from_reading(state, reading, {})
    assert plan.action == "query_database"
    assert state["document_after_query"] == {"format": "pdf", "language": "ar"}


def test_isolated_arabic_letters_are_shaped_to_glyphs_the_pdf_font_has():
    # The shipped Cairo subset lacks the ISOLATED presentation forms of alef,
    # teh, theh, reh, feh, waw and yeh; the shaper must emit the base letter
    # instead, never the missing form (which rendered as a box).
    shaped = eb._shape_rtl("التقارير الاستخبارات المراقبة ملخص تنفيذي رصد غير معروف تشابه")
    for missing_form in ("ﺍ", "ﺕ", "ﺙ", "ﺭ", "ﻑ", "ﻭ", "ﻱ"):
        assert missing_form not in shaped
    assert "ا" in shaped or "ﺎ" in shaped, "alef survives, as its base or joined form"

