"""What the generated document actually shows.

The narration is markdown. The PDF builder converted escaped HTML tags but
nothing else, so the document rendered the MARKUP as text. Extracted from a
real generated file:

    # SECURITY INTELLIGENCE REPORT - JOEY
    ## 1. Executive Summary
    **20:23:26 - WEZARET DEFA3**
    > Also present within the same window:

Every figure in it was correct — 3 detections, the right camera, the right
timestamps. It simply did not look like a report, because the hashes, stars
and angle brackets were printed rather than applied.

This is deterministic formatting, so it belongs in code, not in a prompt
asking the model to please stop emitting markdown.

    docker exec face_recognition_api python -m pytest tests/test_export_markdown_rendering.py -v
"""

import pytest

from sql_agent.services import export_builders as eb


# ------------------------------------------------------------- inline marks

@pytest.mark.parametrize("source,expected", [
    ("**bold**", "<b>bold</b>"),
    ("plain **bold** tail", "plain <b>bold</b> tail"),
    ("**20:23:26 - WEZARET DEFA3**", "<b>20:23:26 - WEZARET DEFA3</b>"),
    ("*italic*", "<i>italic</i>"),
    ("`code`", "code"),
])
def test_inline_markup_becomes_formatting(source, expected):
    assert eb._markdown_inline(source) == expected


def test_text_without_markup_is_untouched():
    """THE control: ordinary prose must survive byte-for-byte."""
    plain = "Joey was tracked across a single camera, WEZARET DEFA3."
    assert eb._markdown_inline(plain) == plain


def test_a_lone_star_is_not_treated_as_formatting():
    """Unpaired marks are literal, or a stray asterisk eats the rest."""
    assert eb._markdown_inline("5 * 3 = 15") == "5 * 3 = 15"


# -------------------------------------------------------------- block marks

@pytest.mark.parametrize("source,level,text", [
    ("# SECURITY INTELLIGENCE REPORT", 1, "SECURITY INTELLIGENCE REPORT"),
    ("## 1. Executive Summary", 2, "1. Executive Summary"),
    ("### 2026-08-20", 3, "2026-08-20"),
    ("> Also present in the window", 0, "Also present in the window"),
    ("- Joey was detected", 0, "\u2022 Joey was detected"),
    ("Ordinary paragraph", 0, "Ordinary paragraph"),
])
def test_block_markers_are_applied_not_printed(source, level, text):
    got_level, got_text = eb._markdown_block(source)
    assert got_level == level
    assert got_text == text


def test_no_markdown_survives_a_realistic_report():
    """THE regression, on the text taken from the broken PDF."""
    report = (
        "# SECURITY INTELLIGENCE REPORT - JOEY\n"
        "## 1. Executive Summary\n"
        "Joey was tracked across **a single camera**, WEZARET DEFA3.\n"
        "### 2026-08-20\n"
        "**20:23:26 - WEZARET DEFA3**\n"
        "- Joey was detected with a 60.3% confidence match.\n"
        "> Also present within the same window: no one.\n")

    rendered = "\n".join(
        eb._markdown_block(line)[1] for line in report.splitlines())

    for leftover in ("#", "**", "> "):
        assert leftover not in rendered, (
            f"{leftover!r} is still printed in the document: {rendered!r}")


# ------------------------------------------------------------- safety kept

def test_escaping_is_not_undone():
    """reportlab parses XML-ish markup, so raw '<' from a model must stay
    escaped. Adding markdown support must not open that door."""
    hostile = "&lt;script&gt;alert(1)&lt;/script&gt; **bold**"
    out = eb._markdown_inline(hostile)

    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<b>bold</b>" in out


def test_the_builder_still_produces_a_pdf():
    """End to end: the conversion must not break document generation."""
    data = eb.build_pdf_bytes(
        "track joey",
        "# REPORT\n\n## Summary\n\n**3 detections** at WEZARET DEFA3.\n",
        "2026-09-01", "Agent")

    assert data and data[:5] == b"%PDF-", "no PDF produced"
