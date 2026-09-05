"""The agent's tools: what it may do, described so a model can call them.

This is the "agentic" layer. Before it, the planner emitted one action per
turn from a fixed vocabulary and could not look anything up — so it guessed.
Asked "who was detected there?", it had no way to discover which camera
"there" meant; asked about "Ali", no way to check whether that person exists
or is spelled differently. Guessing produced fluent, confident, wrong answers.

Tools change that: the model can LOOK THINGS UP mid-turn and then decide,
which is what makes multi-step follow-up conversation work.

The authority split is unchanged and non-negotiable:

  * the model chooses a tool NAME and ARGUMENTS;
  * `validate_call` re-checks both against the schemas here;
  * the executor is ordinary Python holding every authorization decision.

A tool argument is never a SQL string, a file path, a user id, or an
ownership claim. READ_ONLY_TOOLS run inside a turn with no side effects;
ACTION_TOOLS are the existing graph actions, still routed through the
existing nodes, the AST guard and the artifact ownership checks.

Nothing here calls an LLM. Every function is deterministic and testable.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- vocabulary

# Look-ups the agent may perform DURING a turn to resolve a reference before
# acting. Read-only, owner-scoped, cheap, and safe to call more than once.
READ_ONLY_TOOLS = ("list_cameras", "resolve_person", "get_task_state",
                   "list_my_documents")

# Things that change something. Each maps onto an existing graph action, so
# they inherit the SQL chain's AST guard and the artifact ownership checks
# rather than becoming a second, weaker path to the same capability.
ACTION_TOOLS = ("query_database", "modify_active_query", "generate_document",
                "translate_document", "update_task_state", "answer_directly",
                "ask_clarifying_question")

# The two actions that are ALWAYS a safe response to anything. Answering and
# asking are what a greeting, a thank-you or a question about the assistant
# itself warrant; everything else DOES something and therefore has to have
# been asked for (the interpreter's reading of the turn says whether it was).
#
# With a report in the session, "hi" produced first a PDF and then a database
# query — both valid, neither requested.
ALWAYS_SAFE_TOOLS = ("answer_directly", "ask_clarifying_question")

# Actions that operate on something the conversation ALREADY holds. Kept as a
# named set because their refusal message can say what was reached for.
CONTEXT_CONSUMING_TOOLS = ("generate_document", "translate_document",
                           "modify_active_query")

ALL_TOOLS = READ_ONLY_TOOLS + ACTION_TOOLS

# Bounded loop. The model may look things up a few times before committing to
# an action; it may not wander. Three is enough for "which camera is active?"
# then "does this person exist?" then act, and small enough that a confused
# model cannot burn a turn's budget.
MAX_TOOL_STEPS = 3


def _spec(name: str, description: str, properties: dict,
          required: Optional[List[str]] = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                # No free-form extras: an argument we did not define is an
                # argument nothing validates.
                "additionalProperties": False,
            },
        },
    }


def tool_specs(include_actions: bool = True) -> List[dict]:
    """OpenAI/Ollama-shaped tool definitions.

    ONE source of truth: the same specs are sent as native `tools` to a model
    that supports function calling, and rendered into the prompt for one that
    does not. Two hand-maintained copies would drift, and the prompted path is
    exactly the one that gets tested least.
    """
    specs = [
        _spec("list_cameras",
              "List the cameras/pipelines that exist, with their ids and "
              "names. Call this before referring to a camera you have not "
              "seen in this conversation. Never invent a camera id.",
              {}),
        _spec("resolve_person",
              "Look up a person by name, tolerating misspellings. Returns the "
              "matching known identities. Call this before tracking or "
              "filtering by a person you have not already confirmed.",
              {"name": {"type": "string",
                        "description": "the name as the user wrote it"}},
              required=["name"]),
        _spec("get_task_state",
              "Read the current task state: active camera, time range, "
              "referenced person and document, and the earlier tasks you can "
              "return to. Call this when the user says 'there', 'the same', "
              "'that one', or 'go back'.",
              {}),
        _spec("list_my_documents",
              "List the documents already generated in this conversation "
              "(id, title, language). Use these ids; never invent one.",
              {}),
    ]
    if not include_actions:
        return specs

    specs.extend([
        _spec("query_database",
              "Answer a NEW question about the surveillance data. Give the "
              "question as a concise English paraphrase for the SQL "
              "specialist; preserve every constraint and copy person/camera "
              "names exactly in the user's original script. The system "
              "writes and runs SQL safely. Use this only for database facts, "
              "not greetings, recall of an answer already in the conversation, "
              "or questions about the assistant. Never write SQL yourself.",
              {"question": {"type": "string",
                            "description": "English paraphrase in plain words; "
                                           "names and literal values unchanged"},
               "response_shape": {
                   "type": "string", "enum": ["answer", "report"],
                   "description": "Use answer for one focused fact; use report "
                                  "when the user wants all relevant detections, "
                                  "cameras, and timestamps"},
               "uses_context": {
                   "type": "boolean",
                   "description": "True only when the current message refers "
                                  "to a person, camera, result, or constraint "
                                  "from the conversation state"}},
              required=["question"]),
        _spec("modify_active_query",
              "Re-run the PREVIOUS question with something changed: a "
              "different camera, person or period. Describe only the change.",
              {"change": {"type": "string",
                          "description": "e.g. only camera 3, or last week instead"}},
              required=["change"]),
        _spec("generate_document",
              "Turn an answer or report that already exists in this "
              "conversation into a downloadable PDF or Word file. Do not use "
              "when no result exists; ask what the user wants reported first.",
              {"format": {"type": "string", "enum": ["pdf", "word"],
                          "description": "The requested downloadable file type"},
               "language": {"type": "string", "enum": ["en", "ar"],
                            "description": "Optional output language; omit to "
                                           "preserve the answer's language"}}),
        _spec("translate_document",
              "Restate an EXISTING document in another language.",
              {"language": {"type": "string", "enum": ["en", "ar"]},
               "document_id": {"type": "string",
                               "description": "id from list_my_documents; "
                                              "omit for the most recent"}},
              required=["language"]),
        _spec("update_task_state",
              "Record a change the user made to the task itself: a "
              "correction, an added or removed filter. Change ONE field; the "
              "rest is preserved for you.",
              {"operation": {"type": "string",
                             "enum": ["ADD", "REPLACE", "REMOVE", "ROLLBACK"]},
               "field": {"type": "string",
                         "enum": ["active_camera", "active_time_range",
                                  "referenced_entity", "output_language"]},
               "value": {"type": "string",
                         "description": "the new value; omit for REMOVE"}},
              required=["operation", "field"]),
        _spec("answer_directly",
              "Choose a normal conversational response with no data lookup. "
              "Use for greetings, thanks, brainstorming, explanations, and "
              "questions about your abilities or what was already said. Set "
              "uses_context=true only when the reply depends on earlier "
              "conversation. Never assert a new person, camera, count, event, "
              "or other database fact through this tool, even if a read-only "
              "planning tool exposed that fact. Database answers must use "
              "query_database so authorization and provenance are retained.",
              {"answer": {"type": "string",
                          "description": "A concise proposed reply in the "
                                         "user's language; database claims are "
                                         "not allowed"},
               "uses_context": {
                   "type": "boolean",
                   "description": "True for recall or discussion of an earlier "
                                  "message; false for standalone small talk"}},
              required=["answer"]),
        _spec("ask_clarifying_question",
              "LAST RESORT. Ask ONE short question only after a look-up "
              "returned no match, or when the request points at something "
              "you have checked and still cannot resolve. Do NOT use this "
              "for a request that already says what it wants — answer it.",
              {"question": {"type": "string"}},
              required=["question"]),
    ])
    return specs


# ------------------------------------------------------------- validation

class ToolCallRejected(Exception):
    """The proposed call failed validation; nothing was executed."""


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_MAX_STRING_ARG = 500

# Arguments that must never carry executable content, whatever the model
# claims. SQL is composed by the SQL chain from a natural-language question;
# a tool argument containing SQL is a model trying to bypass that.
_SQL_SMELL = re.compile(
    r"\b(select|insert|update|delete|drop|alter|truncate|union)\b|;--", re.I)


def _coerce(tool: str, key: str, value: Any, prop: dict) -> Any:
    kind = prop.get("type")
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized not in ("true", "false", "1", "0", "yes", "no"):
            raise ToolCallRejected(f"{tool}: {key} must be a boolean")
        return normalized in ("true", "1", "yes")
    if kind == "string":
        if not isinstance(value, str):
            raise ToolCallRejected(f"{tool}: {key} must be a string")
        text = value.strip()
        if len(text) > _MAX_STRING_ARG:
            raise ToolCallRejected(f"{tool}: {key} is too long")
        if _SQL_SMELL.search(text):
            raise ToolCallRejected(
                f"{tool}: {key} looks like SQL. Describe the request in plain "
                f"words; the system writes the query.")
        if "enum" in prop:
            lowered = text.lower()
            if lowered not in [str(v).lower() for v in prop["enum"]]:
                raise ToolCallRejected(
                    f"{tool}: {key}={text[:40]!r} is not one of {prop['enum']}")
            return lowered
        return text
    return value


def validate_call(name: str, arguments: Any) -> Dict[str, Any]:
    """Normalize and allow-list one proposed tool call.

    Raises ToolCallRejected. This is the authority boundary for the tool
    layer, the same role validate_plan plays for the planner.
    """
    if name not in ALL_TOOLS:
        raise ToolCallRejected(f"unknown tool {str(name)[:40]!r}")

    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            raise ToolCallRejected(f"{name}: arguments are not valid JSON")
    if not isinstance(arguments, dict):
        raise ToolCallRejected(f"{name}: arguments must be an object")

    spec = next(s for s in tool_specs() if s["function"]["name"] == name)
    schema = spec["function"]["parameters"]
    allowed = set(schema["properties"])
    clean: Dict[str, Any] = {}

    for key, value in arguments.items():
        if key not in allowed:
            # Dropped, not fatal: models add stray keys, and refusing the whole
            # call for that would make the agent brittle for no safety gain.
            logger.info("[TOOL] %s: dropped unknown argument %r", name, str(key)[:40])
            continue
        clean[key] = _coerce(name, key, value, schema["properties"][key])

    for key in schema.get("required", []):
        if key not in clean or clean[key] in (None, ""):
            raise ToolCallRejected(f"{name}: missing required argument {key!r}")

    if name == "translate_document" and clean.get("document_id"):
        if not _UUID_RE.match(str(clean["document_id"])):
            raise ToolCallRejected(f"{name}: document_id is not an id")

    return clean


def render_tools_for_prompt(include_actions: bool = True) -> str:
    """The same tools, described for a model WITHOUT function calling.

    Production may run a model that ignores a `tools` payload entirely
    (qwen2.5:1.5b returns prose and no tool_calls). Rendering the identical
    specs keeps one source of truth, so the fallback path cannot drift into
    describing tools that no longer exist.
    """
    lines = ["You can use these tools:"]
    for spec in tool_specs(include_actions):
        function = spec["function"]
        params = function["parameters"]["properties"]
        arg_text = ", ".join(
            f"{k}: {v.get('type', 'string')}"
            + (f" one of {v['enum']}" if "enum" in v else "")
            for k, v in params.items()) or "no arguments"
        lines.append(f"- {function['name']}({arg_text})")
        lines.append(f"    {function['description']}")
    lines.append("")
    lines.append('Reply with ONLY JSON: {"tool": "<name>", "arguments": {...}}')
    return "\n".join(lines)


def parse_tool_response(raw: Any) -> Optional[Dict[str, Any]]:
    """Extract a tool call from EITHER a native reply or prompted JSON.

    Native function calling and the prompted fallback converge here, so
    everything downstream (validation, execution, auditing) has exactly one
    shape to handle and the two transports cannot behave differently.
    """
    # Native: LangChain puts them on .tool_calls or in additional_kwargs.
    calls = getattr(raw, "tool_calls", None)
    if not calls:
        extra = getattr(raw, "additional_kwargs", None) or {}
        calls = extra.get("tool_calls")
    if calls:
        if len(calls) != 1:
            return {"name": "__multiple_calls__", "arguments": {}}
        first = calls[0]
        if isinstance(first, dict):
            function = first.get("function") or {}
            return {"name": first.get("name") or function.get("name"),
                    "arguments": first.get("args") or function.get("arguments") or {}}

    # Prompted fallback: a JSON object in the text.
    text = raw if isinstance(raw, str) else str(getattr(raw, "content", "") or "")
    if not text.strip():
        return None
    from .planner import extract_json_object
    parsed = extract_json_object(text)
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("tool") or parsed.get("name") or parsed.get("function")
    if not name:
        return None
    return {"name": name,
            "arguments": parsed.get("arguments") or parsed.get("args") or {}}
