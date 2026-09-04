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
from ..skills import resolver as skill_resolver

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
- A follow-up modifies the current task; it does not start a new one.
- answer_directly is for small talk and questions about the assistant
  itself. A question about the DATA is answered by query_database, even when
  a look-up found nothing: the query is what proves there is nothing, and it
  is how the user learns what does exist."""


# Per-model memory of whether native function calling actually works. Probed
# once (see the loop below) rather than configured, because the answer differs
# between the dev and production models and a setting is one more thing to get
# wrong on a deploy.
_NATIVE_SUPPORT: Dict[str, bool] = {}
#: When a model was demoted to the prompted fallback. One prose reply used
#: to demote a model for the life of the process; a capable model that
#: answered one greeting in prose then ran every later turn on the weaker
#: mechanism. Re-probed after this many seconds.
_NATIVE_DEMOTED_AT: Dict[str, float] = {}
_NATIVE_REPROBE_SECONDS = 600.0


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


def asked_for_an_action(llm, user_text: str, *,
                        question_pending: bool = False) -> bool:
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
    # Facts first. A message that names a camera asks about the data, in
    # any language; no model needs to be consulted. "من تم رصده في كاميرا
    # wezaret؟" was judged NOT a request and answered as small talk.
    if camera_named_by_user(user_text):
        return True

    try:
        # A message answering a question the assistant ASKED is a request,
        # however little it says on its own. Judged without that fact, every
        # answer to every question looks like small talk - "yes" after "which
        # one did you mean?" was refused an action and answered with a
        # greeting.
        situation = ("\n\nThe assistant asked the user a question on the "
                     "previous turn and is waiting for the answer. A message "
                     "that answers it CONTINUES that request."
                     if question_pending else "")
        situation += ("\n\nThe message may be in any language, Arabic "
                      "included. Judge its MEANING. Reply with the single "
                      "English word YES or NO and nothing else.")

        reply = llm.invoke([
            SystemMessage(content=INTENT_FIT_PROMPT + situation),
            HumanMessage(content=f"User's message: {user_text}"),
        ])
    except Exception as e:
        logger.warning("[TOOL_LOOP] intent-fit check failed (%s); allowing", e)
        return True

    text = str(getattr(reply, "content", reply) or "").strip()
    if not text:
        return True                      # no answer is not a refusal
    return _says_yes(text)


_YES_WORDS = ("yes", "y", "نعم", "أجل", "اجل", "ايوه", "أيوه")
_NO_WORDS = ("no", "n", "لا", "كلا")


def _says_yes(text: str) -> bool:
    """Read a YES/NO verdict from a reply that may carry markdown, quotes,
    punctuation or an Arabic word. Fails toward NO on anything unclear."""
    stripped = re.sub(r"^[\s\*\"'`_\-\.:\(\[]+", "", str(text or ""))
    first = re.split(r"[\s\*\"'`_\.,:;!\)\]؟،]+", stripped, maxsplit=1)[0]
    first = first.casefold()
    if first in _YES_WORDS:
        return True
    if first in _NO_WORDS:
        return False
    return stripped.casefold().startswith("yes")


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


def _names_offered(trace, prior_observations) -> List[str]:
    """Names the system itself put on the table this turn.

    Candidates from an ambiguous look-up, and the person a look-up resolved.
    A clarifying question may name these even though the user never typed
    them: "Ali Abbass or Ali Hassan?" after "track ali" is the right
    question, and the stranger guard used to reject it.
    """
    names: List[str] = []
    for entry in list(trace or []) + list(prior_observations or []):
        entry = entry or {}
        for cand in entry.get("clarification_candidates") or []:
            cand = cand or {}
            names.append(cand.get("display_name") or cand.get("name") or "")
        resolved = entry.get("resolved_entity") or {}
        names.append(resolved.get("canonical_name") or "")
    return [n for n in names if n]


#: A token that is not a word: at least one digit or underscore among
#: letters, four or more characters. Camera and pipeline identifiers.
_IDENTIFIER_SHAPES = re.compile(
    r"\b(?=[A-Za-z0-9_-]*[0-9_])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{4,}\b")

#: English and Arabic. "كاميرا" with and without the definite article and
#: the attached prepositions the script glues on (بكاميرا, الكاميرا, للكاميرا).
_CAMERA_WORDS = ("camera", "cam", "pipeline", "كاميرا", "الكاميرا",
                 "بكاميرا", "بالكاميرا", "لكاميرا", "للكاميرا", "كامرة")
_CAMERA_WORD_RE = "|".join(re.escape(w) for w in _CAMERA_WORDS)
_CAMERA_LEAD = re.compile(rf"^(?:{_CAMERA_WORD_RE})\s+", re.I)
_CAMERA_NAMED = re.compile(rf"(?:^|\s)(?:{_CAMERA_WORD_RE})\s+([^\s?.,;:!؟،]+)",
                           re.I)

#: A message CONTINUES the previous task only when it says so. Anaphora and
#: connectives in both languages; anything else that stands on its own is a
#: new question, whatever the transcript looks like. "Show me all detections
#: from today" was treated as "the wezaret query, but today" because the
#: model read it against the previous turn; the message itself never pointed
#: back.
_CONTINUATION_MARKERS = (
    # English: anaphora, pronouns, connectives
    "same", "that", "those", "these", "this one", "it", "them", "also",
    "too", "only", "just", "instead", "as well", "again", "more", "rest",
    "other", "previous", "last one", "earlier", "before", "now",
    "he", "she", "her", "him", "his", "hers", "they", "their", "whom",
    "with whom", "who else", "and who",
    # Arabic (with the article and glued prepositions where common)
    "نفس", "ذلك", "تلك", "هذه", "هذا", "هؤلاء", "أيضا", "أيضاً", "ايضا",
    "فقط", "بدلا", "بدلاً", "كذلك", "السابق", "السابقة", "مرة أخرى",
    "مجددا", "مجدداً", "الباقي", "غيره", "غيرها", "منهم", "منها",
    "هو", "هي", "هم", "معه", "معها", "معهم", "معهما", "مع من", "ومن",
    "هناك", "بعدها", "قبلها",
)
_SINGLE_WORD_MARKERS = frozenset(m for m in _CONTINUATION_MARKERS
                                 if " " not in m)
_MULTI_WORD_MARKERS = tuple(m for m in _CONTINUATION_MARKERS if " " in m)
_LEADING_CONNECTIVES = re.compile(r"^(?:and|but|or|so|plus|then|و|لكن|ثم|أو|او)\b",
                                  re.I)


#: Function words and politeness, in the language the paraphrase is written
#: in. Whatever is left of a message after these are the words a paraphrase
#: of it must share.
_PARAPHRASE_STOPWORDS = frozenset("""
can could would will should please hey hi hello the a an me us i we you to of
for in on at by with and or is are was were be been do does did what which who
whom how give get tell show list find want need like it its this that these
those there here about any some he she her him his hers they them their when
where why
""".split())


def paraphrase_ignores_user(question: str, user_text: str,
                            known_names=()) -> bool:
    """Does the paraphrase share NOTHING with the user's message?

    "can you track joey" was paraphrased as "What are the most active
    pipelines?" - the previous turn's question, word for word. A paraphrase
    of a message contains at least one of its content words. Latin words
    only: an Arabic message is paraphrased in English, so its Arabic content
    words cannot be required, but a Latin name typed inside it can.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", str(user_text or ""))
    content = [w.casefold() for w in words
               if w.casefold() not in _PARAPHRASE_STOPWORDS]
    if not content:
        return False
    q = str(question or "").casefold()
    # A one-word paraphrase carries no sentence to compare; only a phrase
    # can be "about something else".
    if len(q.split()) < 2:
        return False
    # A name a look-up resolved this turn IS the user's word, corrected:
    # "track Jeoy" is rightly paraphrased with "JOEY".
    for offered in known_names or ():
        for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", str(offered or "")):
            content.append(w.casefold())
    # Prefix match so "detections" is satisfied by "detection events".
    return not any(w[:4] in q for w in content)


def carried_over(question: str, user_text: str, dialogue_state) -> Optional[str]:
    """A held camera or person that the paraphrase names but the user did
    not. For a self-contained question that is the transcript leaking in:
    "Show all detection events at WEZARET DEFA3 today" for a user who asked
    for all detections today."""
    fields = ((dialogue_state or {}).get("fields") or {})
    held: List[str] = []
    for name in ("active_camera", "referenced_entity"):
        value = (fields.get(name) or {}).get("value")
        held.extend(value if isinstance(value, list) else [value] if value else [])
    q = " ".join(str(question or "").replace("_", " ").split()).casefold()
    u = " ".join(str(user_text or "").replace("_", " ").split()).casefold()
    for item in held:
        key = " ".join(str(item or "").replace("_", " ").split()).casefold()
        if key and key in q and key not in u:
            return str(item)
    return None


#: Messages that ask for nothing and mean "received". Answered with a fixed
#: phrase and no model, no transcript, no FACTS block: "thank you" was
#: answered by repeating the previous completion line ("The report on Iron
#: Man has been translated into Arabic."), and "ok" with "It seems you're
#: providing context about our conversation" - the model reading the
#: prompt scaffolding as the user's words.
_ACK_WORDS = frozenset("""
ok okay okey k kk fine good great nice cool perfect thanks thank thx ty
thankyou cheers noted understood alright right sure yes yep yeah no nope
done bye goodbye
شكرا شكراً تمام حسنا حسناً طيب ماشي اوك أوك نعم لا ممتاز رائع جيد عظيم
تم وصل فهمت مفهوم اوكي سلام جزاك
""".split())
_ACK_FILLER = frozenset(
    "you very much a lot so lot it that is jazakallah".split()
    + ["جزاك", "الله", "خير", "جزيلا", "جزيلاً", "لك", "كتير",
       "كثيرا", "كثيراً", "على", "المساعدة"])
_THANKS_WORDS = frozenset({"thanks", "thank", "thx", "ty", "thankyou", "cheers",
                           "شكرا", "شكراً", "جزاك"})


def is_acknowledgement(user_text: str) -> bool:
    """Every word is an acknowledgement or its filler, and there are few."""
    words = re.findall(r"[^\W\d_]+", str(user_text or "").casefold())
    if not words or len(words) > 5:
        return False
    return all(w in _ACK_WORDS or w in _ACK_FILLER for w in words) and any(
        w in _ACK_WORDS for w in words)


def is_thanks(user_text: str) -> bool:
    words = set(re.findall(r"[^\W\d_]+", str(user_text or "").casefold()))
    return bool(words & _THANKS_WORDS)


#: "with whom", "was she alone": the answer comes from the co-appearance
#: enrichment, which only runs on the SUBJECT'S detection rows. So for such
#: a question the query must be the subject's detections - the paraphrase is
#: fixed here rather than left to the model, which asked for cameras.
_COMPANION_QUESTION = re.compile(
    r"(with whom|with who\b|who (?:was|were) with|whom was .* with|"
    r"\balone\b|accompan|together with|مع من|برفقة|وحده|وحدها|لوحده|"
    r"لوحدها|بمفرده|بمفردها)", re.I)


def is_companion_question(text: str) -> bool:
    return bool(_COMPANION_QUESTION.search(" ".join(str(text or "").split())))


def companion_query(user_text: str, dialogue_state, identity_index) -> Optional[dict]:
    """The one query a companion question needs, or None.

    The enrichment that answers "with whom" / "alone?" runs on the
    SUBJECT'S detection rows, so that is the query - whoever the subject
    is: named in the message, or held from the previous turn. A fact, so
    the model is not consulted: left to it, "with whom she was" spent four
    rejections asking who "she" was and the planner then wrote "people
    detected with Joey today", and "هل كانت وحدها" became a query about
    cameras.
    """
    if not is_companion_question(user_text):
        return None
    subject = (names_a_known_person(user_text, identity_index)
               or held_subject(dialogue_state))
    if not subject:
        return None
    return {"name": "query_database",
            "arguments": {"question": (f"all detections of {subject} with camera "
                                       f"name and timestamp, most recent first")}}


def held_subject(dialogue_state) -> Optional[str]:
    fields = ((dialogue_state or {}).get("fields") or {})
    value = (fields.get("referenced_entity") or {}).get("value")
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None


_PRONOUNS = frozenset("""
he she her him his hers they them their it its who whom someone anyone
هو هي هم هن معه معها له لها إياه إياها
""".split())


def is_pronoun_or_empty(name) -> bool:
    text = " ".join(str(name or "").split()).casefold().strip("\"'“”‘’{}[]()")
    if not text:
        return True
    return all(w in _PRONOUNS for w in re.findall(r"[^\W\d_]+", text)) and bool(
        re.findall(r"[^\W\d_]+", text))


#: "in Arabic", "بالعربية", "to English": the user wants what they already
#: have, restated. Paired with a reference to the report, the answer is a
#: TRANSLATION of the last report - never a new query, never a new document
#: titled with the request. "can you make the report in arabic" produced a
#: fresh PDF titled "can you make the report in arabic".
_LANGUAGE_REQUEST = re.compile(
    r"(?:\b(?:in|to|into)\s+(?P<en_lang>arabic|english)\b|"
    r"\b(?P<en_lang2>arabic|english)\s+(?:version|translation)\b|"
    r"(?P<ar_ar>بالعربي(?:ة)?|إلى العربية|الى العربية|للعربية)|"
    r"(?P<ar_en>بالإنجليزي(?:ة)?|بالانجليزي(?:ة)?|إلى الإنجليزية|الى الانجليزية))",
    re.I)
_REFERS_TO_REPORT = re.compile(
    r"(\breport\b|\bit\b|\bthat\b|\bthis\b|\bsame\b|\bthe result\b|\bthe answer\b|"
    r"التقرير|النتيجة|الإجابة|ذلك|هذا|نفس)", re.I)


def wants_translation(user_text: str) -> Optional[str]:
    """The target language of a 'give me that in <language>' request, or
    None. Only when the message also points at what exists (the report,
    it, that): "track joey in arabic" is a NEW request with an output
    language, and is left alone."""
    text = " ".join(str(user_text or "").split())
    match = _LANGUAGE_REQUEST.search(text)
    if not match or not _REFERS_TO_REPORT.search(text):
        return None
    if match.group("ar_ar"):
        return "ar"
    if match.group("ar_en"):
        return "en"
    lang = (match.group("en_lang") or match.group("en_lang2") or "").lower()
    return "ar" if lang == "arabic" else "en"


def names_a_known_person(user_text: str, identity_index) -> Optional[str]:
    """An enrolled person's name in the message: a data request by
    construction. "does joey was alone the last time shwe was seen" was
    answered "I'm not aware of any information about a person named Joey
    or Shwe" by a model that never queried."""
    low = " ".join(str(user_text or "").split()).casefold()
    if not low:
        return None
    for entry in identity_index or []:
        name = " ".join(str((entry or {}).get("display_name") or "").split())
        if len(name) >= 3 and re.search(
                rf"(?<![^\W\d_]){re.escape(name.casefold())}(?![^\W\d_])", low):
            return name
    return None


def _resolved_this_turn(trace) -> List[str]:
    """Names a resolve_person look-up settled in this pass."""
    names = []
    for entry in trace or []:
        resolved = (entry or {}).get("resolved_entity") or {}
        if resolved.get("canonical_name"):
            names.append(str(resolved["canonical_name"]))
    return names


#: ONE decision per turn: does this message need the data, or is it chat?
#: Facts first - each is something Python holds - and a single model
#: judgement only when no fact settles it. Decided once, at planning time,
#: and obeyed downstream: the loop seeds `is_a_request` from it instead of
#: re-judging in the middle of tool selection.
DATA = "data"
CHAT = "chat"
UNDECIDED = "undecided"


def route_turn(user_text: str, *, dialogue_state=None, identity_index=None,
               has_result: bool = False, clarification_answered: bool = False):
    """(kind, reason). `kind` is DATA, CHAT or UNDECIDED; UNDECIDED means
    the one model judgement in the loop decides."""
    text = " ".join(str(user_text or "").split())
    if not text:
        return CHAT, "empty"
    # An answer to a question WE asked wins over its own words: "yes" after
    # "which one did you mean?" is data, not an acknowledgement.
    fields = ((dialogue_state or {}).get("fields") or {})
    if clarification_answered or (fields.get("pending_clarification") or {}).get("value"):
        return DATA, "answers the question we asked"
    if is_acknowledgement(text):
        return CHAT, "acknowledgement"
    if is_greeting(text):
        return CHAT, "greeting"
    from .planner import deterministic_request_plan

    if deterministic_request_plan(text) is not None:
        return DATA, "a track command"
    if camera_named_by_user(text):
        return DATA, "names a camera"
    known = names_a_known_person(text, identity_index)
    if known:
        return DATA, f"names an enrolled person"
    if is_a_continuation(text):
        if has_result or any((fields.get(n) or {}).get("value")
                             for n in ("referenced_entity", "active_task",
                                       "active_camera")):
            return DATA, "continues a data task"
    return UNDECIDED, "no fact settles it"


def is_a_continuation(user_text: str) -> bool:
    """Does the message point back at the previous task?

    A fact about the words, not a judgement about intent: a marker, a
    leading connective, or a fragment too short to stand alone. Everything
    else states its own question.
    """
    text = " ".join(str(user_text or "").split())
    if not text:
        return False
    low = text.casefold()
    if _LEADING_CONNECTIVES.match(low):
        return True
    # Whole words, so "he" does not fire on "hello" and "it" not on "item".
    words = set(re.findall(r"[^\W\d_]+", low))
    if words & _SINGLE_WORD_MARKERS:
        return True
    if any(m in low for m in _MULTI_WORD_MARKERS):
        return True
    # "yes", "the pdf", "camera 3": fragments only make sense as answers -
    # unless the fragment IS a complete command the planner recognises on
    # its own ("track joey").
    if len(low.split()) <= 3:
        from .planner import deterministic_request_plan

        # A greeting is short too, and points at nothing. ("yes", "ok" DO
        # point back - at a question the assistant asked.)
        if words and all(w in _GREETING_WORDS for w in words):
            return False
        return deterministic_request_plan(text) is None
    return False


def is_greeting(user_text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", str(user_text or "").casefold())
    return bool(words) and len(words) <= 4 and all(
        w in _GREETING_WORDS or w in _ACK_FILLER for w in words)


_GREETING_WORDS = frozenset(
    "hi hello hey hiya morning evening afternoon greetings there "
    "مرحبا مرحباً أهلا أهلاً اهلا السلام عليكم صباح مساء الخير النور".split())


def camera_named_by_user(user_text: str) -> Optional[str]:
    """The camera token the user introduced with the word camera, if any."""
    match = _CAMERA_NAMED.search(" ".join(str(user_text or "").split()))
    return match.group(1) if match else None


def a_query_already_ran(prior_observations) -> bool:
    for o in prior_observations or []:
        if o.get("tool") == "query_database" and o.get("status") == "ok":
            return True
    return False


def names_a_camera(name: str, user_text: str) -> bool:
    """Is the `resolve_person` argument a CAMERA the user named?

    A matter of fact, not judgement: the user wrote "camera X", so X is a
    camera. "Who was detected at camera MD5AL_3EIN_7LWE?" was answered with
    "What person were you referring to?" because the model looked the
    camera token up as a person and, finding nobody, asked about them.
    """
    token = " ".join(str(name or "").split())
    if not token:
        return False
    if _CAMERA_LEAD.match(token):
        return True
    token = token.casefold()
    text = " ".join(str(user_text or "").split()).casefold()
    return any(re.search(rf"(?:^|\s){re.escape(word)}\s+{re.escape(token)}", text)
               for word in _CAMERA_WORDS)


def names_a_stranger(question: str, user_text: str,
                     known_names=()) -> Optional[str]:
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
    known = [str(n).lower() for n in (known_names or []) if n]
    for quoted, capitalised in _NAME_SHAPES.findall(question or ""):
        candidate = (quoted or capitalised or "").strip()
        if not candidate or candidate.lower() in _NOT_NAMES:
            continue
        if len(candidate) < 3:
            continue
        if candidate.lower() in haystack:
            continue
        # Offered by a look-up this turn: the system said it, not a
        # stranger. Substring, because the shape regex may capture one
        # word of a two-word name.
        if any(candidate.lower() in k for k in known):
            continue
        return candidate
    # Identifier-shaped tokens - camera ids like MD5AL_3EIN_7LWE - are not
    # name-shaped, and "What camera is MD5AL_3EIN_7LWE?" was asked in reply
    # to "Who was detected at camera wezaret?": the model carried the
    # PREVIOUS turn's camera into a question about this one.
    for candidate in _IDENTIFIER_SHAPES.findall(question or ""):
        low = candidate.lower()
        if low in _NOT_NAMES or low in haystack:
            continue
        if any(low in k for k in known):
            continue
        return candidate
    return None


def run_tool_loop(llm, *, user_text: str, context_block: str,
                  db, dialogue_state: Optional[dict],
                  artifact_index: Optional[List[dict]],
                  identity_index: Optional[List[dict]] = None,
                  prior_observations: Optional[List[dict]] = None,
                  known_request: Optional[bool] = None,
                  has_result: bool = False,
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
    system_prompt = skill_resolver.compose(
        TOOL_SYSTEM_PROMPT,
        has_result=has_result,
        has_documents=bool(artifact_index),
    )
    messages = [
        SystemMessage(content=system_prompt),
        _user_message(user_text, context_block,
                      prompted=not supports_native_tools),
    ]

    model_id = str(getattr(llm, "model", None) or getattr(llm, "model_name", ""))
    if _NATIVE_SUPPORT.get(model_id) is False:
        import time as _time

        demoted_at = _NATIVE_DEMOTED_AT.get(model_id, 0.0)
        if _time.time() - demoted_at >= _NATIVE_REPROBE_SECONDS:
            logger.info("[TOOL_LOOP] re-probing native tool calling for %s",
                        model_id)
            _NATIVE_SUPPORT.pop(model_id, None)
            _NATIVE_DEMOTED_AT.pop(model_id, None)
        else:
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
    #
    # Unless the caller already KNOWS. When Python has matched the message to
    # a candidate the assistant offered, the message continues that request as
    # a matter of fact - asking a model to judge it can only introduce error,
    # and did: "seed_person_016" was matched to its candidate and then refused
    # an action because, read alone, it asks for nothing.
    is_a_request = known_request
    # A CONTINUATION of a data task is a request by construction. "with whom
    # she was", asked right after "when was joey last seen", was judged "not
    # a request" on its own words and answered with a greeting. The message
    # points back; the task it points back at asked for data; no model
    # needs to be consulted about that.
    if is_a_request is None and is_a_continuation(user_text):
        fields = ((dialogue_state or {}).get("fields") or {})
        holds_a_task = has_result or any(
            (fields.get(name) or {}).get("value")
            for name in ("referenced_entity", "active_task", "active_camera"))
        if holds_a_task:
            is_a_request = True
            logger.info("[TOOL_LOOP] continuation of a data task: a request "
                        "by construction")
    if is_a_request is None and names_a_known_person(user_text, identity_index):
        is_a_request = True
        logger.info("[TOOL_LOOP] names an enrolled person: a request by "
                    "construction")
    # answer_directly on a turn that asked for data is refused once, not
    # forever: the second proposal is the model insisting, and it wins.
    direct_answer_refused = False
    camera_clarification_refused = False
    continuation_refused = False

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
                import time as _time

                _NATIVE_DEMOTED_AT[model_id] = _time.time()
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
                is_a_request = asked_for_an_action(
                    llm, user_text,
                    question_pending=bool(
                        ((dialogue_state or {}).get("fields") or {})
                        .get("pending_clarification", {}).get("value")))
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

        if (name == "answer_directly" and is_a_request is None
                and not is_acknowledgement(user_text)
                and not is_greeting(user_text)):
            # The model wants to answer in prose before anything has been
            # established. For a greeting that is right; for "check the
            # situation" it produced "It seems we're starting fresh". One
            # judgement call settles which - greetings and acknowledgements
            # are exempt, so small talk still costs nothing.
            is_a_request = asked_for_an_action(
                llm, user_text,
                question_pending=bool(
                    ((dialogue_state or {}).get("fields") or {})
                    .get("pending_clarification", {}).get("value")))

        if (name == "answer_directly" and is_a_request is True
                and direct_answer_refused):
            # Told once, and it still wants to answer a data question from
            # memory. Prose is never the answer to a data request; ending
            # the loop hands the turn to the guidance path, which says what
            # was understood and asks for what is missing.
            logger.info("[TOOL_LOOP] answer_directly insisted on a data "
                        "request; ending the loop for guidance")
            trace.append({"tool": name, "rejected": "answered data without a query",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "UNQUERIED_ANSWER"}})
            return None, trace, is_a_request

        if (name == "answer_directly" and is_a_request is True
                and not direct_answer_refused):
            # The user asked for DATA - established, not guessed: either a
            # look-up or an action was proposed first and judged, or the
            # message answers a question we asked. Answering without a
            # query invents an answer ("I don't have any information about
            # that camera"). Refused ONCE; a model that insists is allowed
            # through, because it may know something the guard does not.
            direct_answer_refused = True
            logger.info("[TOOL_LOOP] refused answer_directly: the user "
                        "asked for data and nothing was queried")
            rejections += 1
            trace.append({"tool": name, "rejected": "answered data without a query",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "UNQUERIED_ANSWER"}})
            messages.append(HumanMessage(content=(
                "The user asked about the data. Do not answer from memory: "
                "use query_database (a query that returns nothing is a valid "
                "answer and is reported honestly), or ask_clarifying_question "
                "only if a look-up left the request genuinely ambiguous.")))
            continue

        if name == "resolve_person" and is_pronoun_or_empty(
                arguments.get("name", "")):
            # "she", "him", "" are not names. "with whom she was" was looked
            # up as a person called "she" and answered "No person named
            # 'she' is enrolled"; an empty name printed "{}".
            subject = held_subject(dialogue_state)
            logger.info("[TOOL_LOOP] refused resolve_person: the argument is "
                        "a pronoun or empty")
            rejections += 1
            trace.append({"tool": name, "rejected": "not a name",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "NOT_A_NAME"}})
            messages.append(HumanMessage(content=(
                f"{arguments.get('name', '')!r} is not a person's name. "
                + (f"The person under discussion is {subject!r}; use that "
                   f"exact name, or query the database directly."
                   if subject else
                   "No person is under discussion; ask the user who they "
                   "mean with ask_clarifying_question."))))
            continue

        if name == "resolve_person" and names_a_camera(
                arguments.get("name", ""), user_text):
            # The user said "camera X". X is not a person, and looking it
            # up as one finds nobody - which the model then asks about.
            logger.info("[TOOL_LOOP] refused resolve_person: the name is a "
                        "camera the user named")
            rejections += 1
            trace.append({"tool": name, "rejected": "named a camera",
                          "observation": {"status": "rejected",
                                          "tool": name,
                                          "reason_code": "NOT_A_PERSON"}})
            messages.append(HumanMessage(content=(
                f"{arguments.get('name', '')!r} is a camera the user named, "
                f"not a person. Cameras need no look-up: query the database "
                f"for it directly, or list_cameras if you need its exact "
                f"identifier.")))
            continue

        target_language = wants_translation(user_text)
        if (target_language and (has_result or artifact_index)
                and name in ("query_database", "generate_document",
                             "modify_active_query")):
            # The user has a report and wants it in another language. That
            # is a translation of what exists, whatever the model proposed:
            # a new query answers a question nobody asked, and a new
            # document gets titled with the request.
            logger.info("[TOOL_LOOP] language request about the existing "
                        "report: translating instead of %s", name)
            name = "translate_document"
            arguments = {"document_id": "", "language": target_language}

        if name in ("query_database", "modify_active_query"):
            fact = companion_query(user_text, dialogue_state, identity_index)
            if fact and (name != "query_database"
                         or arguments.get("question") != fact["arguments"]["question"]):
                logger.info("[TOOL_LOOP] companion question: querying "
                            "the subject's detections")
                name = fact["name"]
                arguments = dict(fact["arguments"])

        # A continuation's content is elsewhere by definition ("with whom
        # she was" is about the previous subject), so only a self-contained
        # message can have its paraphrase checked against its own words.
        if (name == "query_database" and not is_a_continuation(user_text)
                and paraphrase_ignores_user(
                    arguments.get("question", ""), user_text,
                    known_names=_names_offered(trace, prior_observations))):
            # The paraphrase is about something else entirely - typically
            # the previous question. A fact about the words, so refused
            # every time; the rejection budget bounds a model that insists.
            logger.info("[TOOL_LOOP] refused query_database: the paraphrase "
                        "shares nothing with the user's message")
            rejections += 1
            trace.append({"tool": name, "rejected": "paraphrase ignores the message",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "PARAPHRASE_MISMATCH"}})
            messages.append(HumanMessage(content=(
                f"Your paraphrase does not reflect the user's message. "
                f"Paraphrase THIS message and nothing else: {user_text!r}. "
                f"Keep every name exactly as they wrote it.")))
            continue

        if (name == "query_database" and not is_a_continuation(user_text)):
            leaked = carried_over(arguments.get("question", ""), user_text,
                                  dialogue_state)
            if leaked:
                # A NEW question, paraphrased with the previous turn's
                # camera or person folded in. The words came from the
                # transcript, not from the user. A fact, so refused every
                # time it recurs.
                logger.info("[TOOL_LOOP] refused query_database: the "
                            "paraphrase carries over something the user "
                            "did not mention")
                rejections += 1
                trace.append({"tool": name, "rejected": "carried over context",
                              "observation": {"status": "rejected",
                                              "tool": name,
                                              "reason_code": "CARRIED_OVER"}})
                messages.append(HumanMessage(content=(
                    f"Your paraphrase mentions {leaked!r}, which the user "
                    f"did not. Their message {user_text!r} is a NEW question: "
                    f"paraphrase exactly what it asks and nothing from "
                    f"earlier turns.")))
                continue

        if (name in ("modify_active_query", "update_task_state")
                and not is_a_continuation(user_text)):
            # The message states its own question. Re-running the PREVIOUS
            # one with a change drags its filters along: "Show me all
            # detections from today" became "the wezaret camera, but today"
            # and was answered about a camera the user never mentioned.
            # Refused every time: the message does not change when the
            # model insists, and "once" let a wrong report through.
            continuation_refused = True
            logger.info("[TOOL_LOOP] refused %s: the message does not point "
                        "back at the previous task", name)
            rejections += 1
            trace.append({"tool": name, "rejected": "not a continuation",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "NEW_QUESTION"}})
            messages.append(HumanMessage(content=(
                f"The message {user_text!r} states its own question and does "
                f"not refer back to the previous one (no 'same', 'that', "
                f"'also', 'only'...). Treat it as NEW: use query_database "
                f"with exactly what it asks, and carry over no camera, "
                f"person or time window from before.")))
            continue

        resolved_names = _resolved_this_turn(trace)
        low_user = " ".join(str(user_text or "").split()).casefold()
        low_question = str(arguments.get("question", "")).casefold()
        if name == "ask_clarifying_question" and any(
                rn.casefold() in low_user or rn.casefold() in low_question
                for rn in resolved_names):
            # "Can you clarify what you mean by Joey?" - asked AFTER the
            # look-up resolved Joey. The person is settled; the question is
            # quitting. Query them.
            logger.info("[TOOL_LOOP] refused a clarification about a person "
                        "this turn already resolved")
            rejections += 1
            trace.append({"tool": name, "rejected": "asked about a resolved person",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "PERSON_RESOLVED"}})
            messages.append(HumanMessage(content=(
                f"The look-up already resolved {resolved_names[0]!r}. There "
                f"is nothing to clarify: use query_database with that exact "
                f"name.")))
            continue

        named_camera = camera_named_by_user(user_text)
        if (name == "ask_clarifying_question" and named_camera
                and not a_query_already_ran(prior_observations)
                and not camera_clarification_refused):
            # "Which camera is wezaret?" - asked with the camera list in
            # hand. A camera the user named needs no clarification before a
            # query: the query runs, and an empty result is resolved against
            # the real cameras (misspelling corrected and re-run, or the
            # real names offered). Asking first is quitting before starting.
            camera_clarification_refused = True
            logger.info("[TOOL_LOOP] refused a clarification about a camera "
                        "the user named; query it")
            rejections += 1
            trace.append({"tool": name, "rejected": "asked about a named camera",
                          "observation": {"status": "rejected", "tool": name,
                                          "reason_code": "CAMERA_NAMED"}})
            messages.append(HumanMessage(content=(
                f"The user named the camera: {named_camera!r}. Do not ask "
                f"which camera; use query_database with that camera exactly "
                f"as written. If it does not match, the system resolves it "
                f"against the real camera list and corrects the spelling.")))
            continue

        if name == "ask_clarifying_question":
            stranger = names_a_stranger(
                arguments.get("question", ""), user_text,
                known_names=_names_offered(trace, prior_observations))
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
