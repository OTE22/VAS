"""Deterministic ingestion for natural-language SQL-agent requests.

The raw request, the model-facing request, and the security-scanning form are
different values with different jobs.  Keeping them separate prevents a
normalizer (or a model) from silently becoming the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata
from typing import Optional


_ARABIC_RANGES = (
    ("\u0600", "\u06ff"),
    ("\u0750", "\u077f"),
    ("\u08a0", "\u08ff"),
    ("\ufb50", "\ufdff"),
    ("\ufe70", "\ufeff"),
)
_ARABIC_OUTPUT_MARKERS = ("بالعربية", "بالعربي")


class QueryInputError(ValueError):
    """The request cannot safely enter the agent graph."""


@dataclass(frozen=True)
class QueryEnvelope:
    """Immutable views of one user request.

    ``raw_text`` is audit/provenance data. ``normalized_text`` is the only
    natural-language value consumed by planning and generation.
    ``security_text`` is compatibility-normalized so visually disguised
    write verbs cannot bypass the deterministic pre-check.
    """

    raw_text: str
    normalized_text: str
    security_text: str
    input_language: str
    response_language: str


def _has_arabic(text: str) -> bool:
    return any(start <= char <= end
               for char in text for start, end in _ARABIC_RANGES)


def _collapse_outer_whitespace(text: str) -> str:
    """Collapse natural-language spacing without changing quoted values."""
    output = []
    quote = None
    pending_space = False
    escaped = False
    for char in text:
        if quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            if pending_space and output:
                output.append(" ")
            pending_space = False
            quote = char
            output.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and output:
                output.append(" ")
            pending_space = False
            output.append(char)
    return "".join(output).strip()


def _strip_format_controls(text: str) -> str:
    # Format controls include zero-width and bidi override characters. They
    # carry no query meaning here and can split a security keyword visually.
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def ingest_query(raw: object, *, max_chars: Optional[int] = None) -> QueryEnvelope:
    """Validate and normalize a request without semantic rewriting.

    Unicode is normalized and whitespace is collapsed, but words, names,
    literals, and constraints are never corrected, translated, or inferred.
    This function is intentionally idempotent and contains no model call.
    """

    if not isinstance(raw, str):
        raise QueryInputError("Query must be a string")
    if max_chars is not None and len(raw) > max_chars:
        raise QueryInputError(
            f"Query is too long. Maximum length is {max_chars} characters.")

    # NUL and non-whitespace C0 controls do not belong in natural language
    # and can create transport/log/parser disagreement.
    for char in raw:
        if char == "\x00" or (unicodedata.category(char) == "Cc" and not char.isspace()):
            raise QueryInputError("Query contains unsupported control characters")

    raw_text = raw
    normalized = unicodedata.normalize("NFC", raw).lstrip("\ufeff")
    normalized = _collapse_outer_whitespace(normalized)
    if not normalized:
        raise QueryInputError("Query is required")
    if max_chars is not None and len(normalized) > max_chars:
        raise QueryInputError(
            f"Query is too long. Maximum length is {max_chars} characters.")

    security = unicodedata.normalize("NFKC", normalized)
    security = " ".join(_strip_format_controls(security).split()).casefold()

    input_language = "ar" if _has_arabic(normalized) else "en"
    lowered = normalized.casefold()
    wants_arabic = (
        input_language == "ar"
        or "in arabic" in lowered
        or any(marker in normalized for marker in _ARABIC_OUTPUT_MARKERS)
    )

    return QueryEnvelope(
        raw_text=raw_text,
        normalized_text=normalized,
        security_text=security,
        input_language=input_language,
        response_language="ar" if wants_arabic else "en",
    )
