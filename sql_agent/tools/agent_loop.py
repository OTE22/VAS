"""The bounded tool loop: look things up, then commit to one action.

This is what makes the agent agentic rather than a one-shot classifier. A
turn now runs:

    model -> (read-only look-up) -> model -> ... -> ONE action

bounded by MAX_TOOL_STEPS. The look-ups are what remove the guessing: the
model can ask which cameras exist, how a name is really spelled, or what the
current task state is, and *then* decide. "Who was detected there?" becomes
answerable because "there" can be resolved before the SQL is written.

What this deliberately is NOT: an open-ended autonomous agent. The loop only
ever executes READ-ONLY look-ups. The moment the model picks an action tool
the loop stops and hands a validated PlannedAction to the existing graph, so
every action still goes through the nodes that own the AST guard, artifact
ownership and the audit trail. There is no path here that writes anything.

Two engines, one behaviour: a model with native function calling gets real
`tools`; one without gets the identical specs rendered into its prompt
(production's qwen2.5:1.5b ignores a tools payload entirely). Both converge
on the same validated call via parse_tool_response, so the fallback cannot
quietly become a different agent.
"""

import json
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from . import tool_registry as tr
from . import tool_executors as tx

logger = logging.getLogger(__name__)

TOOL_SYSTEM_PROMPT = """You are the assistant for a security-camera database.

Your DEFAULT is to ACT, not to ask. Choose the action tool by what the
request is ABOUT:

- about the DATA, a new question          -> query_database
- the SAME question with something changed -> modify_active_query
- the result just produced, as a file      -> generate_document
- a file that already exists, in another
  language                                 -> translate_document

Check the conversation state above before choosing. If it shows a
last_result or a document, a short follow-up almost certainly refers to that
rather than starting a new question — running a fresh query for "make that a
PDF" answers a question nobody asked.

You do not need to know which cameras exist before asking about them; the
query finds that out. A PERSON is different — see stage 1.

Work in two stages:

1. LOOK UP:
   - anything the request POINTS AT without naming: a camera, "there",
     "the same", "that one", "go back". Never guess a camera id.
   - any PERSON the request names, with resolve_person, unless this turn
     already resolved them. The name as typed is usually not the name as
     stored, so a query filtered on it finds nothing — and "no rows" would
     then be indistinguishable from "no such person", which are different
     answers the user needs to be able to tell apart.
2. Then call exactly ONE action tool, using the resolved name if there was
   one. If several people matched, ask which one.

Rules:
- Never write SQL. Describe the question in plain words; the system writes it.
- Use ids exactly as a look-up returned them.
- ask_clarifying_question is a LAST resort, not an opening move. Use it only
  after a look-up came back with no match, or when the request refers to
  something and you have checked and cannot tell what. Asking the user a
  question they already answered is worse than a query that returns nothing.
- A follow-up modifies the current task; it does not start a new one."""


# Per-model memory of whether native function calling actually works. Probed
# once (see the loop below) rather than configured, because the answer differs
# between the dev and production models and a setting is one more thing to get
# wrong on a deploy.
_NATIVE_SUPPORT: Dict[str, bool] = {}


def _describe(arguments: Optional[dict]) -> str:
    """Argument SHAPE for the log: keys, types, sizes — not values.

    A `resolve_person` argument is a person's name and a `question` is the
    user's own words; neither belongs in a log file, which is the same rule
    the audit line already follows. Set SQL_AGENT_TRACE_CONTEXT=1 in
    development to get the values instead.
    """
    from config import settings

    if not arguments:
        return "{}"
    # These arguments are RAW: they have not been through validate_call yet,
    # so a model can hand us a bare string, a list, or anything else. A
    # logger that assumes a dict here takes the whole turn down with it,
    # which is a strictly worse outcome than the missing log line it was
    # added to provide. Never trust the shape.
    try:
        if settings.SQL_AGENT_TRACE_CONTEXT:
            return json.dumps(arguments, ensure_ascii=False, default=str)[:300]
        if not isinstance(arguments, dict):
            size = len(arguments) if hasattr(arguments, "__len__") else "?"
            return f"<{type(arguments).__name__}:{size}>"
        parts = []
        for key, value in sorted(arguments.items(), key=lambda kv: str(kv[0])):
            if isinstance(value, str):
                parts.append(f"{key}=<str:{len(value)}>")
            elif isinstance(value, (list, tuple, dict)):
                parts.append(f"{key}=<{type(value).__name__}:{len(value)}>")
            else:
                parts.append(f"{key}=<{type(value).__name__}>")
        return "{" + " ".join(parts) + "}"
    except Exception as e:
        return f"<undescribable: {type(e).__name__}>"


def _describe_result(result: dict) -> str:
    """What a look-up returned: shape and size, never the rows themselves."""
    if not isinstance(result, dict):
        return f"<{type(result).__name__}>"
    if result.get("error"):
        return f"error={str(result['error'])[:80]}"
    parts = []
    for key, value in sorted(result.items()):
        if isinstance(value, (list, tuple)):
            parts.append(f"{key}[{len(value)}]")
        elif isinstance(value, dict):
            parts.append(f"{key}{{{len(value)}}}")
        else:
            parts.append(key)
    return "ok " + " ".join(parts)


def _user_message(user_text: str, context_block: str,
                  prompted: bool) -> HumanMessage:
    """The turn message, with the tool specs inlined for a prompted model."""
    return HumanMessage(content=(
        (context_block + "\n\n" if context_block else "")
        + (tr.render_tools_for_prompt() + "\n\n" if prompted else "")
        + f"User: {user_text}"))


#: Candidates carried into the NEXT turn's prompt, so this is bounded for
#: the same reason a look-up result is.
_MAX_STORED_CANDIDATES = 5

#: How many invalid proposals may be corrected before the turn gives up.
#: Separate from the look-up budget, so a refusal costs a correction rather
#: than the turn's only chance to do useful work - and still bounded, so a
#: model that proposes nothing valid cannot loop.
_MAX_REJECTIONS = 3

#: Observations carried between actions of one turn. Bounded because they ride
#: in every subsequent prompt: an unbounded record is a context-explosion bug
#: wearing a reasoning-loop costume.
_MAX_TURN_OBSERVATIONS = 8


def _tool_result_message(name: str, result: dict) -> HumanMessage:
    """Feed a look-up result back as an observation.

    A plain message, not a role="tool" turn: the prompted-fallback engine has
    no tool role at all, and using one shape for both is what keeps the two
    paths behaving identically.
    """
    return HumanMessage(content=(
        f"Result of {name}:\n{json.dumps(result, ensure_ascii=False)[:1500]}\n\n"
        f"Use this. Do not call {name} again."))


INTENT_FIT_PROMPT = """You judge ONE thing about a user's message.

Is the user ASKING the assistant to do something with the security-camera
data or with a document — a question to answer, a change to make, a file to
produce?

Answer with exactly one word:
YES  - the message asks for something, or refers to something the assistant
       already holds. Examples: "how many cameras", "track Ali",
       "make it Arabic", "only camera 3", "send that as a PDF", "the report"
NO   - the message asks for nothing. Examples: a greeting, thanks, small
       talk, a question about the assistant itself, an acknowledgement

Judge ONLY the message below, not the earlier conversation. A short message
is not automatically NO — "only camera 3" asks for something."""


def asked_for_an_action(llm, user_text: str) -> bool:
    """Did the user ask for anything at all, or is this a greeting?

    The reasoning layer checks whether an action SUCCEEDED; nothing checked
    whether it FIT. So a valid PDF, and later a valid query, produced in
    answer to "hi" passed every guard — the artifact existed, the observation
    said success, the user was told their document was ready.

    Whether a message expresses a want is semantic, so the model is asked.
    The question is narrow and closed, and Python decides what to do with the
    answer. It fails SAFE toward not acting: anything but a clear YES is a
    NO, because acting on something nobody asked for is worse than asking
    what they meant.

    A model failure means the turn proceeds as before — this may refuse an
    action, never break one.
    """
    try:
        reply = llm.invoke([
            SystemMessage(content=INTENT_FIT_PROMPT),
            HumanMessage(content=f"User's message: {user_text}"),
        ])
    except Exception as e:
        logger.warning("[TOOL_LOOP] intent-fit check failed (%s); allowing", e)
        return True

    text = str(getattr(reply, "content", reply) or "").strip().upper()
    if not text:
        return True                      # no answer is not a refusal
    return text.startswith("YES")


_ACTION_DESCRIPTIONS = {
    "generate_document": "turn the previous result into a downloadable file",
    "translate_document": "restate an existing document in another language",
    "modify_active_query": "re-run the previous question with something changed",
    "query_database": "run a query against the surveillance data",
    "update_task_state": "change the active task",
}


REQUEST_DONE_PROMPT = """You judge ONE thing.

The user asked for something. One action has just been carried out and it
succeeded. Decide whether the user's FULL request has now been carried out,
or whether another step is still needed.

Answer with exactly one word:
DONE  - everything the user asked for has been done
MORE  - part of the request is still outstanding
        (e.g. they asked for a report AND a PDF, and only the report exists)

Judge the user's original message against what has been done. If in doubt,
answer DONE."""


def request_is_satisfied(llm, user_text: str, done_summary: str) -> bool:
    """Has the user's whole request been carried out?

    Used to decide whether a turn re-enters the loop after a SUCCESSFUL
    action. Without it the agent could only ever take one action, so "track
    Joey and send it as a PDF" needed two turns.

    Fails SAFE toward FINISHING: anything other than a clear MORE is treated
    as done. Looping when the user is already served wastes their time and
    the budget; stopping early leaves them able to ask again.
    """
    try:
        reply = llm.invoke([
            SystemMessage(content=REQUEST_DONE_PROMPT),
            HumanMessage(content=(f"User's message: {user_text}\n"
                                  f"Already carried out: {done_summary}")),
        ])
    except Exception as e:
        logger.warning("[TOOL_LOOP] completion check failed (%s); finishing", e)
        return True

    text = str(getattr(reply, "content", reply) or "").strip().upper()
    return not text.startswith("MORE")


#: A quoted phrase, or a run of Capitalised Words — the shapes a name takes
#: in a question the model writes. Deliberately crude: this only has to find
#: candidates, and each one is then checked against the user's own message.
_NAME_SHAPES = re.compile(r"[\"'\u201c\u2018]([^\"'\u201d\u2019]{2,60})[\"'\u201d\u2019]"
                          r"|\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")

#: Words that start a sentence or are simply common; naming one proves nothing
#: about staleness, and treating them as names would reject good questions.
def _own_vocabulary() -> frozenset:
    """Values this system itself defines — formats, languages, actions.

    "Do you want the report as a PDF or Word?" is a perfectly grounded
    question, but "Word" is capitalised and absent from "make that a
    document", so a naive check calls it a stranger. Our own enum values are
    never somebody's name.

    Read FROM the enums rather than restated, so adding a format cannot leave
    this list behind.
    """
    from .planner import ACTIONS, FORMATS, LANGUAGES, TARGETS

    words = set()
    for value in list(FORMATS) + list(LANGUAGES) + list(TARGETS) + list(ACTIONS):
        words.update(str(value).replace("_", " ").split())
    words.update({"english", "arabic"})      # the languages spelled out
    return frozenset(w.lower() for w in words)


#: Sentence-openers and ordinary nouns. Naming one proves nothing about
#: staleness, and treating them as names would reject good questions.
_COMMON_WORDS = frozenset({
    "could", "can", "did", "do", "does", "would", "should", "what", "which",
    "who", "whom", "when", "where", "why", "how", "you", "your", "i", "the",
    "a", "an", "is", "are", "was", "were", "please", "sorry", "there", "this",
    "that", "these", "those", "camera", "cameras", "person", "people",
    "detections", "data", "result", "results", "query", "system",
})

_NOT_NAMES = _COMMON_WORDS | _own_vocabulary()


def names_a_stranger(question: str, user_text: str) -> Optional[str]:
    """A name the QUESTION uses that the user's message never mentioned.

    Returns that name, or None when the question stays within what the user
    just said.

    Asked "track iron man", the agent replied "Can you clarify what you mean
    by Joey?" — Joey being a previous turn's subject. The model authors these
    questions and sees the whole transcript, so it can name whoever dominates
    it rather than whoever was just asked about.

    A factual test — is this string in what they typed? — not a guess about
    what a person's name looks like.
    """
    haystack = " ".join((user_text or "").split()).lower()
    for quoted, capitalised in _NAME_SHAPES.findall(question or ""):
        candidate = (quoted or capitalised or "").strip()
        if not candidate or candidate.lower() in _NOT_NAMES:
            continue
        if len(candidate) < 3:
            continue
        if candidate.lower() not in haystack:
            return candidate
    return None


def run_tool_loop(llm, *, user_text: str, context_block: str,
                  db, dialogue_state: Optional[dict],
                  artifact_index: Optional[List[dict]],
                  identity_index: Optional[List[dict]] = None,
                  prior_observations: Optional[List[dict]] = None,
                  supports_native_tools: bool = True,
                  max_steps: int = tr.MAX_TOOL_STEPS
                  ) -> Tuple[Optional[Dict[str, Any]], List[dict], Optional[bool]]:
    """Run look-ups until the model commits to an action.

    Returns (action_call, trace, is_a_request):

      * `action_call` — a VALIDATED {"name", "arguments"} for an action tool,
        or None if the model never committed to one (the caller then falls
        back to the planner);
      * `trace` — the TOOLS performed, for the audit line. Only tools; the
        fit check below is not one and does not belong in the count;
      * `is_a_request` — whether the turn asks for anything, or None if the
        question never needed asking. The chat node uses it to decide whether
        the prior-turns block is relevant, so a greeting is answered rather
        than continued.

    Never raises: a broken tool step degrades to "no action chosen", which the
    caller handles, rather than failing the turn.
    """
    messages = [
        SystemMessage(content=TOOL_SYSTEM_PROMPT),
        _user_message(user_text, context_block,
                      prompted=not supports_native_tools),
    ]

    model_id = str(getattr(llm, "model", None) or getattr(llm, "model_name", ""))
    if _NATIVE_SUPPORT.get(model_id) is False:
        supports_native_tools = False

    model = llm
    if supports_native_tools:
        try:
            model = llm.bind(tools=tr.tool_specs(), tool_choice="auto")
        except Exception as e:
            logger.info("[TOOL_LOOP] native binding unavailable (%s); "
                        "using the prompted fallback", e)
            supports_native_tools = False

    if not supports_native_tools:
        messages[1] = _user_message(user_text, context_block, prompted=True)

    # State the mechanism on EVERY turn, not once per process. Which of the
    # two paths a turn took is the first thing anyone asks when they doubt
    # the agent is really calling tools, and it was previously only visible
    # in the one log line emitted when the fallback was first triggered.
    mechanism = "native" if supports_native_tools else "prompted"

    # What the model was actually GIVEN, as counts. Two transports handing
    # the same turn different context is invisible otherwise, and it is the
    # difference that decides whether "make that a PDF" reads as a follow-up
    # or as a brand-new question. These are all our own rendered markers and
    # collection sizes — never the user's text.
    block = context_block or ""
    logger.info("[TOOL_LOOP] start model=%s mechanism=%s tools=%d budget=%d "
                "context={chars=%d last_result=%s documents=%s state_fields=%d "
                "artifacts=%d}",
                model_id or "?", mechanism, len(tr.tool_specs()), max_steps,
                len(block),
                "n" if "last_result: none" in block
                else ("y" if "last_result:" in block else "-"),
                "n" if "already generated: none" in block
                else ("y" if "already generated" in block else "-"),
                len(((dialogue_state or {}).get("fields") or {})),
                len(artifact_index or []))

    trace: List[dict] = []
    seen: set = set()

    # Everything THIS TURN already did, carried in from the canonical record
    # on the state. Without it a second action re-enters blind and repeats
    # work the turn has already paid for.
    if prior_observations:
        messages.append(HumanMessage(content=(
            "Already done this turn:\n"
            + "\n".join(f"  {o.get('sequence')}. {o.get('tool')} -> "
                        f"{o.get('status')}"
                        + (f" ({o['summary']})" if o.get("summary") else "")
                        for o in prior_observations[-_MAX_TURN_OBSERVATIONS:])
            + "\n\nDo not repeat these. Build on them.")))
        # A look-up already performed must not be performed again.
        for o in prior_observations:
            # Only work that SUCCEEDED is already done. Blocking a retry of
            # something that failed would strand the turn on its first bad
            # attempt - the opposite of self-correction.
            if o.get("signature") and o.get("status") == "ok":
                seen.add(tuple(o["signature"]))
    # Computed lazily the first time an action is proposed, then reused: it
    # is a property of the user's message, not of the tool.
    is_a_request = None

    # THREE bounds, because they bound three different things.
    #
    # Every rejection used to consume the same budget as a useful look-up: the
    # counter WAS `for step in range(max_steps)`, and all six rejection paths
    # advanced it. In FAST mode, where the budget is 1, one refusal therefore
    # ended the turn - the model was handed a reason it never got to read. A
    # rejection is a correction opportunity, not work performed.
    #
    # `iterations` is the hard ceiling and no path can escape it, so
    # termination stays arithmetic rather than a matter of the model choosing
    # to stop.
    lookups = 0
    rejections = 0
    iterations = 0
    ceiling = max(1, max_steps) + _MAX_REJECTIONS

    while iterations < ceiling:
        step = iterations
        iterations += 1
        if lookups >= max_steps:
            logger.info("[TOOL_LOOP] look-up budget spent (%d); acting now",
                        max_steps)
            break
        if rejections > _MAX_REJECTIONS:
            logger.info("[TOOL_LOOP] %d rejections without a valid action",
                        rejections)
            break

        try:
            reply = model.invoke(messages)
        except Exception as e:
            logger.warning("[TOOL_LOOP] step %d model call failed: %s", step, e)
            return None, trace, is_a_request

        call = tr.parse_tool_response(reply)
        if call and call.get("name"):
            logger.info("[TOOL_LOOP] step=%d mechanism=%s proposed=%s args=%s",
                        step, mechanism, call.get("name"),
                        _describe(call.get("arguments")))
        if not call or not call.get("name"):
            # CAPABILITY PROBE. A model that ignores a `tools` payload answers
            # in prose with no call — measured on qwen2.5:1.5b, which is what
            # production runs. Fall back to the prompted rendering of the same
            # specs, once, and remember it for this model so later turns take
            # the right path immediately.
            if supports_native_tools and step == 0:
                logger.info("[TOOL_LOOP] %s produced no native tool call; "
                            "switching to the prompted fallback", model_id)
                _NATIVE_SUPPORT[model_id] = False
                supports_native_tools = False
                mechanism = "prompted"
                model = llm
                messages[1] = _user_message(user_text, context_block,
                                            prompted=True)
                # Working out how to TALK to a model is not the model failing
                # to decide. Charged to neither budget, and recorded rather
                # than silent - it used to consume step 0 invisibly.
                ceiling += 1
                trace.append({"tool": None, "capability_probe": mechanism})
                continue
            logger.info("[TOOL_LOOP] step %d produced no tool call", step)
            return None, trace, is_a_request
        if supports_native_tools:
            _NATIVE_SUPPORT.setdefault(model_id, True)

        name = call["name"]
        try:
            arguments = tr.validate_call(name, call.get("arguments"))
        except tr.ToolCallRejected as rejection:
            # Tell the model WHY and let it correct itself — that is the whole
            # value of a loop. A rejection is not a turn failure.
            logger.info("[TOOL_LOOP] rejected %s: %s", name, rejection)
            rejections += 1
            trace.append({"tool": name, "rejected": str(rejection),
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "INVALID_ARGUMENTS"}})
            messages.append(HumanMessage(content=(
                f"That call was rejected: {rejection}. "
                f"Choose a valid tool and arguments.")))
            continue

        # "Has a look-up actually RUN?" — not "is the trace non-empty".
        #
        # The trace also carries rejections and repeats, and this guard
        # appends one itself. So testing `not trace` let the model's SECOND
        # clarification through on the strength of the first being refused,
        # which is precisely the retry the guard exists to stop. Caught by
        # test_agent_e2e over SSE: a document request came back as "Could you
        # please clarify which camera you are referring to?".
        looked_up = any("ok" in entry for entry in trace)

        if name == "ask_clarifying_question" and not looked_up:
            # A clarification CLAIMS something cannot be resolved. At step 0
            # nothing has been tried, so the claim is untested — and the loop
            # exists so the model can test it. Refuse it once and say why.
            #
            # An earlier version of this guard also required the session to
            # be empty. That made it almost never fire: `artifact_index` is
            # non-empty for anybody who has ever generated a document, so a
            # plain question was still answered with a question. Whether the
            # claim has been CHECKED is the property that matters, and it
            # does not depend on session contents.
            #
            # Structural: it reads the trace, never the user's words. The
            # legitimate path is intact — clarify after a look-up comes back
            # empty, which is where asking genuinely beats guessing.
            logger.info("[TOOL_LOOP] refused a clarification at step %d: "
                        "nothing has been looked up yet", step)
            rejections += 1
            trace.append({"tool": name, "rejected": "premature clarification",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "LOOKUP_AVAILABLE"}})
            messages.append(HumanMessage(content=(
                "You have not looked anything up yet, so you cannot know the "
                "request is ambiguous. Either call a look-up tool to check, "
                "or answer the request with the action tool that matches what "
                "it is about. Ask only if a look-up comes back with nothing.")))
            continue

        if name in tr.ACTION_TOOLS and name not in tr.ALWAYS_SAFE_TOOLS:
            # Every action DOES something, so it has to have been asked for.
            # Answering and asking are the exceptions: they are what a
            # greeting warrants.
            #
            # The verdict is a property of the user's MESSAGE, not of the
            # tool, so it is computed once and reused for the rest of the
            # turn — at most one extra call, and none at all when the model
            # goes straight to answer_directly.
            if is_a_request is None:
                is_a_request = asked_for_an_action(llm, user_text)
            if not is_a_request:
                doing = _ACTION_DESCRIPTIONS.get(name, "do that")
                logger.info("[TOOL_LOOP] refused %s: the user did not ask "
                            "for it", name)
                rejections += 1
                trace.append({"tool": name,
                              "rejected": "not what the user asked",
                              "observation": {"status": "rejected",
                                              "tool": name,
                                              "reason_code": "NOT_REQUESTED"}})
                messages.append(HumanMessage(content=(
                    f"The user did not ask you to {doing}. Their message was: "
                    f"{user_text!r}. Respond to what they actually said — if "
                    f"it is a greeting or small talk, use answer_directly.")))
                continue

        if name == "ask_clarifying_question":
            stranger = names_a_stranger(arguments.get("question", ""),
                                        user_text)
            if stranger:
                # The question names somebody the user did not mention. The
                # model sees the whole transcript and can ask about whoever
                # dominates it: "track iron man" was answered "Can you clarify
                # what you mean by Joey?".
                logger.info("[TOOL_LOOP] clarification named %r, which the "
                            "request does not mention; regrounding", stranger)
                rejections += 1
                trace.append({"tool": name, "rejected": "named a stranger",
                              "observation": {"status": "rejected",
                                              "tool": name,
                                              "reason_code": "NAMED_A_STRANGER"}})
                messages.append(HumanMessage(content=(
                    f"Your question mentioned {stranger!r}, which the user did "
                    f"not. Their message was: {user_text!r}. Ask about THAT, "
                    f"or answer it.")))
                continue

        if name in tr.ACTION_TOOLS:
            # An ACTION can repeat too. "track joey and give me the report in
            # arabic" ran the query, correctly decided it was not finished,
            # and ran the same query again before reporting - right answer,
            # twice the time. The commit used to return before ever reaching
            # the duplicate check below.
            action_signature = (name, json.dumps(arguments, sort_keys=True))
            if action_signature in seen:
                lookups += 1
                logger.info("[TOOL_LOOP] refused a repeat of %s; the result "
                            "is already in context", name)
                trace.append({
                    "tool": name, "repeated": True,
                    "observation": {"status": "rejected", "tool": name,
                                    "reason_code": "DUPLICATE_TOOL_CALL"}})
                messages.append(HumanMessage(content=(
                    f"You already ran {name} with those exact arguments this "
                    f"turn and it succeeded. The result is in the context "
                    f"above - USE it. Choose the step that is still "
                    f"outstanding, or answer.")))
                continue

            trace.append({"tool": name, "committed": True,
                          "signature": list(action_signature)})
            logger.info("[TOOL_LOOP] committed to %s after %d look-up(s) "
                        "via %s calling", name, len(trace) - 1, mechanism)
            return {"name": name, "arguments": arguments}, trace, is_a_request

        # A read-only look-up. Repeats are refused rather than executed: a
        # model that loops on list_cameras would otherwise burn every step.
        signature = (name, json.dumps(arguments, sort_keys=True))
        if signature in seen:
            messages.append(HumanMessage(content=(
                f"You already called {name} with those arguments. "
                f"Use the result above and choose an ACTION now.")))
            # Charged to the LOOK-UP budget, not the rejection allowance.
            # A repeated identical call is not the model correcting itself,
            # it is the model looping - the exact thing the budget exists to
            # stop. Only a VALIDATION rejection earns a correction.
            lookups += 1
            trace.append({"tool": name, "repeated": True,
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "DUPLICATE_TOOL_CALL"}})
            continue
        seen.add(signature)

        result = tx.execute_read_only(
            name, arguments, db=db, dialogue_state=dialogue_state,
            artifact_index=artifact_index,
            identity_index=identity_index)
        logger.info("[TOOL_LOOP] step=%d lookup=%s -> %s",
                    step, name, _describe_result(result))
        lookups += 1
        # The signature travels WITH the observation so a later action in the
        # same turn inherits the duplicate guard. Without it the record says
        # what was done but not precisely enough to avoid redoing it.
        entry = {"tool": name, "ok": "error" not in result,
                 "signature": list(signature)}
        if name == "resolve_person" and result.get("status") == "ambiguous":
            # Kept so the question we are about to ask can be ANSWERED next
            # turn: without the candidate list, "the second one" refers to
            # nothing and the answer looks like a brand-new request.
            entry["clarification_candidates"] = (
                result.get("candidates") or [])[:_MAX_STORED_CANDIDATES]
        if name == "resolve_person" and result.get("status") == "resolved":
            # Carried on the trace rather than through another return value:
            # this function already returns three things, and adding a fourth
            # is how a caller silently unpacks the wrong arity.
            identity = result.get("identity") or {}
            entry["resolved_entity"] = {
                "tool": name,
                "raw_text": result.get("query"),
                "identity_id": identity.get("identity_id"),
                "canonical_name": identity.get("display_name")}
        trace.append(entry)
        messages.append(_tool_result_message(name, result))

    logger.info("[TOOL_LOOP] finished without an action "
                "(look-ups=%d/%d rejections=%d/%d iterations=%d/%d)",
                lookups, max_steps, rejections, _MAX_REJECTIONS,
                iterations, ceiling)
    return None, trace, is_a_request


def action_to_planned(call: Dict[str, Any], candidates: dict) -> Optional[dict]:
    """Translate a committed tool call into the planner's action shape.

    The graph, the audit line and every existing test speak PlannedAction, so
    the tool layer converts rather than introducing a second routing
    vocabulary. Returning the planner's own dict means the dispatcher
    validation, precondition downgrades and ownership re-checks all still
    apply — the tool loop widens how the agent DECIDES, not what it may do.
    """
    from .planner import validate_plan

    name = call["name"]
    arguments = call.get("arguments") or {}

    if name == "query_database":
        raw = {"action": "query_database", "confidence": 0.9}
    elif name == "modify_active_query":
        raw = {"action": "modify_previous_query", "confidence": 0.9,
               "modification": arguments.get("change")}
    elif name == "generate_document":
        raw = {"action": "generate_document", "confidence": 0.9,
               "format": arguments.get("format") or "pdf",
               "language": arguments.get("language")}
    elif name == "translate_document":
        raw = {"action": "translate_artifact", "confidence": 0.9,
               "language": arguments.get("language"),
               "artifact_id": arguments.get("document_id"),
               "target": "artifact"}
    elif name == "answer_directly":
        raw = {"action": "chat", "confidence": 0.9}
    elif name == "ask_clarifying_question":
        raw = {"action": "clarify", "confidence": 0.9,
               "clarify_question": arguments.get("question")}
    elif name == "update_task_state":
        # A pure state change still needs an action to run; treat it as a
        # modification of the active query and carry the delta for the
        # application to commit AFTER that succeeds.
        raw = {"action": "modify_previous_query", "confidence": 0.85,
               "modification": (f"{arguments.get('operation','').lower()} "
                                f"{arguments.get('field','')} "
                                f"{arguments.get('value','')}").strip(),
               "state_delta": {"operation": arguments.get("operation"),
                               "field": arguments.get("field"),
                               "proposed_value": arguments.get("value"),
                               "source": "user_correction"}}
    else:
        return None

    plan = validate_plan(raw, candidates)
    return plan.as_dict() if plan else None
