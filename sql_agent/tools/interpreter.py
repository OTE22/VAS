"""One reading of the turn, made by the model and checked against facts.

The agent had been growing a phrase list per bug: `track X`, then
`report for tracking X`, then "in Arabic" but not "make it Arabic", then
"are you sure". Every new way of saying the same thing was a new defect,
because the WORDS were being matched instead of the meaning. That does not
end; people phrase things however they like, in two languages.

So the division of labour changes. Understanding the message is what a
language model is for, and it is asked exactly once per turn, for a small
closed structure. Authority stays in Python, but it now checks the model's
ANSWER against the world - the people who are enrolled, the cameras that
exist, whether a report is actually held - instead of checking the user's
sentence against a regex. A name the model invents is dropped because it is
not in the identity index, not because it failed a pattern; a translation is
refused when no report exists to translate, not when the word "Arabic" is
missing.

Nothing here decides what is ALLOWED. The SQL guard, the camera scope and
the ownership checks are untouched and still run on everything downstream.
"""

import json
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

#: What a turn can want. Closed, because everything downstream branches on it.
DATA = "data"
TRANSLATION = "translation"
DOCUMENT = "document"
CONFIRMATION = "confirmation"
CHAT = "chat"
#: They are asking about something already said in THIS conversation - no
#: new query, the answer is in the transcript.
RECALL = "recall"
#: The model cannot tell what is meant and says so, with the question it
#: would ask. The alternative was a guess dressed up as an answer.
CLARIFY = "clarify"
WANTS = (DATA, TRANSLATION, DOCUMENT, CONFIRMATION, CHAT, RECALL, CLARIFY)

#: Below this the model's own reading is not acted on as data: a vague
#: request becomes a question back to the user, never a query built on a
#: guess.
CONFIDENCE_FLOOR = 0.5
#: How much of the conversation the reading may see. Bounded, most recent
#: last, one line per turn.
_MAX_RECENT_TURNS = 8
_MAX_TURN_CHARS = 160

#: The label the model returns, folded onto the closed set. A small model
#: says "query" or "translate" as readily as the word it was given, and
#: refusing its synonym only sends the turn back to the phrase lists. This
#: normalises the MODEL's own vocabulary - never the user's words.
_WANT_ALIASES = {w: w for w in WANTS}
_WANT_ALIASES.update({
    "query": DATA, "sql": DATA, "database": DATA, "search": DATA,
    "report": DATA, "lookup": DATA, "look_up": DATA, "question": DATA,
    "translate": TRANSLATION, "language": TRANSLATION,
    "export": DOCUMENT, "pdf": DOCUMENT, "word": DOCUMENT,
    "generate_document": DOCUMENT, "file": DOCUMENT,
    "confirm": CONFIRMATION, "verify": CONFIRMATION, "check": CONFIRMATION,
    "smalltalk": CHAT, "small_talk": CHAT, "greeting": CHAT, "none": CHAT,
    "history": RECALL, "remember": RECALL, "previous": RECALL, "earlier": RECALL,
    "clarification": CLARIFY, "ask": CLARIFY, "unclear": CLARIFY,
    "ambiguous": CLARIFY, "question_back": CLARIFY,
})

#: Bounds on what is put in the prompt. A list of every enrolled person is
#: both unaffordable and useless to a small model.
_MAX_NAMES = 40
_MAX_CAMERAS = 24
_MAX_QUESTION = 400

_SYSTEM = """You read ONE message from a security-camera operator and say what they want. You do NOT answer it.

Reply with a single JSON object and nothing else:

{"wants": "data",
 "confidence": 0.9,
 "question": "the request as one self-contained question",
 "question_for_user": null,
 "people": ["exact names copied from ENROLLED"],
 "camera": "exact name copied from CAMERAS, or null",
 "language": null,
 "format": null,
 "shape": "answer",
 "about_previous": false}

Pick ONE value for "wants" out of: data, chat, recall, clarify, translation, document, confirmation. Pick ONE for "shape" out of: report, answer. Never return a list of options or a value with "|" in it.

What each "wants" means:
- data: they want something out of the database - a report on someone, a count, when or where somebody was seen, who was at a camera, whether two people were seen together.
- recall: they are asking about something ALREADY SAID in this conversation ("what did you tell me about Ali earlier", "the report you gave before") - the answer is in RECENT CONVERSATION, no new query.
- clarify: you cannot tell what they mean - who "he" is when nobody is under discussion, which of two people, a request too vague to turn into one question. Put the question you would ask them in "question_for_user". Asking is the right answer; guessing is not.
- translation: they want THE ANSWER ALREADY GIVEN said again in another language ("make it Arabic", "in English please", "اجعل التقرير بالإنجليزية", "بالعربية"). Only when an answer exists. Naming a language is what makes it this, whatever language the message itself is written in.
- document: they want the answer already given as a PDF or Word file.
- confirmation: they doubt the answer just given and want it checked ("are you sure?", "really?", "هل أنت متأكد؟"). They name no language.
- chat: a greeting, thanks, or small talk that asks for nothing.

"shape" is how much they want back:
- report: everything you have on someone or something ("track X", "a report on X", "تقرير عن X"). The "question" must then ask for ALL of it - every detection, with camera name and timestamp.
- answer: one fact ("when was X last seen", "how many", "which camera").

Rules:
- Copy names EXACTLY as spelled in ENROLLED and CAMERAS. If the message names nobody on those lists, use [] and null - do not invent or correct names.
- "question" must stand on its own: replace "she", "him", "there", "that camera" with the names from SITUATION. Keep everything the user asked for, and add no filter they did not state.
- Set "language" ONLY if the message asks for a language — and when it does, "wants" is translation (or document, if they also asked for a file), never confirmation.
- "confidence" is how sure you are of the whole reading, 0 to 1. Below 0.5 the reading is not acted on: say what is unclear in "question_for_user".
- A name that is on neither list is not invented and not corrected: leave it in "people" as written and lower your confidence; the system will ask about it.
- The message may be in English or Arabic. Judge its meaning, not its words. Write "question" and "question_for_user" in the language the message is written in."""


@dataclass
class Interpretation:
    """What the user wants, after validation. Every field is either a fact
    (a stored name, a real camera) or the model's own words for the request."""

    wants: str
    question: str = ""
    people: List[str] = field(default_factory=list)
    unknown_people: List[str] = field(default_factory=list)
    camera: Optional[str] = None
    unknown_camera: Optional[str] = None
    language: Optional[str] = None
    format: Optional[str] = None
    shape: str = "answer"
    about_previous: bool = False
    confidence: float = 1.0
    #: The question to put to the user when `wants` is CLARIFY (or when the
    #: reading is too uncertain to act on).
    question_for_user: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "wants": self.wants, "question": self.question,
            "confidence": self.confidence,
            "question_for_user": self.question_for_user,
            "people": list(self.people), "unknown_people": list(self.unknown_people),
            "camera": self.camera, "unknown_camera": self.unknown_camera,
            "language": self.language, "format": self.format,
            "shape": self.shape, "about_previous": self.about_previous,
        }


def _situation(dialogue_state, *, has_result: bool, has_documents: bool,
               last_question: str, question_pending: bool,
               pending_question: str = "") -> str:
    fields = ((dialogue_state or {}).get("fields") or {})

    def _held(name):
        value = (fields.get(name) or {}).get("value")
        if isinstance(value, list):
            return ", ".join(str(v) for v in value if v)
        return str(value) if value else ""

    lines = []
    subject = _held("referenced_entity")
    if subject:
        lines.append(f"- the person under discussion: {subject}")
    camera = _held("active_camera")
    if camera:
        lines.append(f"- the camera under discussion: {camera}")
    if last_question:
        lines.append(f"- the question just answered: {last_question[:200]}")
    lines.append(f"- an answer is on hand to translate or export: "
                 f"{'yes' if has_result else 'no'}")
    if has_documents:
        lines.append("- the user has documents already made")
    if question_pending:
        lines.append("- the assistant asked the user a question and is "
                     "waiting for the answer"
                     + (f": {pending_question[:200]}" if pending_question else ""))
    return "\n".join(lines) or "- nothing has been discussed yet"


def _recent(recent_turns) -> str:
    """The last few exchanges, one bounded line each, oldest first. This is
    what "the report you gave me earlier" resolves against."""
    lines = []
    for turn in list(recent_turns or [])[-_MAX_RECENT_TURNS:]:
        text = " ".join(str(turn or "").split())
        if text:
            lines.append(f"- {text[:_MAX_TURN_CHARS]}")
    return "\n".join(lines) or "- (nothing yet)"


def _prompt(user_text: str, *, names: List[str], cameras: List[str],
            situation: str, recent: str = "- (nothing yet)") -> List[Any]:
    listed_names = "\n".join(f"- {n}" for n in names[:_MAX_NAMES]) or "- (nobody)"
    listed_cams = "\n".join(f"- {c}" for c in cameras[:_MAX_CAMERAS]) or "- (none)"
    return [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=(
            f"SITUATION\n{situation}\n\n"
            f"RECENT CONVERSATION (oldest first)\n{recent}\n\n"
            f"ENROLLED\n{listed_names}\n\n"
            f"CAMERAS\n{listed_cams}\n\n"
            f"MESSAGE\n{user_text}")),
    ]


def _parse(raw: str) -> Optional[dict]:
    """The object out of a reply that may carry prose or a code fence."""
    from .planner import extract_json_object

    parsed = extract_json_object(str(raw or ""))
    return parsed if isinstance(parsed, dict) else None


def _match(value: Any, known: List[str]) -> Optional[str]:
    """The STORED spelling of a name the model returned, or None. Matching
    is on the fold only: the stored form is what reaches the user and the
    SQL, so the model's capitalisation never overwrites it."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    folded = text.casefold()
    for candidate in known:
        if " ".join(str(candidate).split()).casefold() == folded:
            return candidate
    return None


def _first_known(label: Any) -> Optional[str]:
    """The label the model chose, folded onto the closed set.

    Small models echo the schema they were shown ("data|translation") or
    return the options as a list; the first recognisable one is the choice.
    """
    if isinstance(label, (list, tuple)):
        label = label[0] if label else ""
    for token in str(label or "").strip().lower().replace(",", "|").split("|"):
        known = _WANT_ALIASES.get(token.strip().strip("\"' "))
        if known:
            return known
    return None


def _echoes_a_user_turn(question: str, recent_turns) -> bool:
    """Does the question quote one of the user's own earlier messages? Then
    it is the transcript read back, not a question."""
    q = " ".join(str(question or "").split()).casefold()
    if len(q) < 12:
        return False
    for turn in recent_turns or []:
        text = " ".join(str(turn or "").split())
        if not text.casefold().startswith("user:"):
            continue
        said = text[5:].strip().casefold()
        if len(said) >= 12 and said in q:
            return True
        # A paraphrased quote: any run of 15+ characters of the earlier
        # message inside the question ("what do you mean by jeoy or similar
        # to this name" quoted the tail of "i mean jeoy or similar...").
        for i in range(0, max(1, len(said) - 15)):
            if said[i:i + 15] in q:
                return True
    return False


def validate(parsed: dict, *, names: List[str], cameras: List[str],
             user_text: str, has_result: bool, has_documents: bool,
             question_pending: bool = False, pending_request: str = "",
             recent_turns=None) -> Optional[Interpretation]:
    """Check the model's reading against the world. Returns None when the
    reply is unusable, which puts the turn back on the older path rather
    than acting on a guess."""
    wants = _first_known(parsed.get("wants") or parsed.get("intent"))
    if wants is None:
        return None

    people, unknown_people = [], []
    raw_people = parsed.get("people")
    if isinstance(raw_people, str):
        raw_people = [raw_people]
    for entry in (raw_people or [])[:4]:
        stored = _match(entry, names)
        if stored and stored not in people:
            people.append(stored)
        elif not stored:
            text = " ".join(str(entry or "").split())
            if len(text) >= 2 and text not in unknown_people:
                unknown_people.append(text)

    camera = _match(parsed.get("camera"), cameras)
    unknown_camera = None
    if not camera:
        text = " ".join(str(parsed.get("camera") or "").split())
        # "null" comes back as a string from small models often enough.
        if len(text) >= 2 and text.lower() not in ("null", "none", "n/a"):
            unknown_camera = text

    language = str(parsed.get("language") or "").strip().lower()
    language = language if language in ("ar", "en") else None
    fmt = str(parsed.get("format") or "").strip().lower()
    fmt = fmt if fmt in ("pdf", "word") else None

    question = " ".join(str(parsed.get("question") or "").split())[:_MAX_QUESTION]
    ask = " ".join(str(parsed.get("question_for_user") or "").split())[:_MAX_QUESTION]
    try:
        confidence = float(parsed.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))
    shape = str(parsed.get("shape") or "").strip().lower()
    # Same echo problem as the label: "report|answer" means neither.
    shape = next((s for s in shape.replace(",", "|").split("|")
                  if s.strip() in ("report", "answer")), "answer").strip()
    about_previous = bool(parsed.get("about_previous"))

    # --- what the world permits ----------------------------------------
    # These are facts, not phrasings: there is either something to restate
    # or there is not.
    if wants == TRANSLATION and not (has_result or has_documents):
        wants = DATA if question else CHAT
    if wants == DOCUMENT and not (has_result or has_documents):
        wants = DATA if question else CHAT
    if wants == CONFIRMATION and not has_result:
        wants = DATA if (people or camera) else CHAT
    if wants == DATA and not question:
        question = " ".join(str(user_text or "").split())[:_MAX_QUESTION]
    if wants == TRANSLATION and not language:
        # A translation with no target language is not one.
        wants = CONFIRMATION if about_previous and has_result else CHAT
    if wants == RECALL and not has_result:
        # Nothing has been said yet to recall.
        wants = DATA if question else CHAT
    if wants == CLARIFY and ask and _echoes_a_user_turn(ask, recent_turns):
        # "could you clarify what you mean by '<their own earlier message>'"
        # was the answer to "hi". A question that quotes the user back is
        # the transcript, not a question: a bare greeting is chat.
        ask = ""
        wants = CHAT
        about_previous = False
    if wants == CLARIFY and not ask:
        # A clarification with no question is a shrug; treat it as a
        # low-confidence reading and let the caller ask its own question.
        confidence = min(confidence, CONFIDENCE_FLOOR - 0.01)
        wants = DATA if question else CHAT
    if (question_pending and people and wants in (CHAT, RECALL, CLARIFY)):
        # We asked which person was meant and the answer names one who is
        # enrolled: that IS the answer, and the suspended request resumes
        # for them. Read as `recall`, the chat model told the user "we have
        # no records of Alio Abbass" without looking.
        wants = DATA
        confidence = max(confidence, CONFIDENCE_FLOOR)
        who = people[0]
        if pending_request:
            question = f"{pending_request} (the person is {who})"[:_MAX_QUESTION]
        elif who.casefold() not in question.casefold():
            question = f"all detections of {who} with camera name and timestamp"

    return Interpretation(
        wants=wants, question=question, people=people,
        unknown_people=unknown_people, camera=camera,
        unknown_camera=unknown_camera, language=language, format=fmt,
        shape=shape, about_previous=about_previous, confidence=confidence,
        question_for_user=ask)


LANGUAGE_QUESTION = ("Answer YES or NO and nothing else. Does the message "
                     "ask for the previous answer to be given again in "
                     "another language (for example: make it Arabic, in "
                     "English please, بالعربية)?")
DOUBT_QUESTION = ("Answer YES or NO and nothing else. Does the message "
                  "question whether the previous answer is correct (for "
                  "example: are you sure? really? هل أنت متأكد؟)?")


ASKS_QUESTION = ("Answer YES or NO and nothing else. Does the message ask "
                 "the assistant for anything - a question to answer, data to "
                 "look up, a change, a file, a check? Thanks, greetings, "
                 "acknowledgements and small talk are NO.")


def _mentions_any(user_text: str, names) -> bool:
    """Does the message itself contain any of these stored names (folded,
    whole or in part)? A slot the model filled from the situation alone is
    not something the message asked about."""
    from difflib import SequenceMatcher

    low = " ".join(str(user_text or "").split()).casefold()
    words = [w for w in re.findall(r"[^\W\d_]+", low) if len(w) >= 3]
    for name in names:
        key = " ".join(str(name or "").split()).casefold()
        if len(key) < 3:
            continue
        if key in low:
            return True
        # A misspelling still names the person ("jeoy" is JOEY): the same
        # closeness the resolver accepts for a did-you-mean.
        for part in key.split():
            if len(part) >= 3 and any(
                    SequenceMatcher(None, part, w).ratio() >= 0.75 for w in words):
                return True
    return False


def _yes_no(llm, question: str, user_text: str) -> Optional[bool]:
    """One closed question about the message alone. None on failure."""
    from .agent_loop import _says_yes

    try:
        reply = llm.invoke([SystemMessage(content=question),
                            HumanMessage(content=f"MESSAGE\n{user_text}")])
    except Exception as e:
        logger.warning("[INTERPRET] a yes/no question failed (%s)", e)
        return None
    return _says_yes(str(getattr(reply, "content", reply) or ""))


def _settle_doubt(llm, user_text: str, reading: "Interpretation",
                  message_language: str = "en") -> "Interpretation":
    """A `confirmation` reading is checked on the MESSAGE ALONE.

    The full reading sees the recent conversation, and once a verification
    sentence is in it a small model reads the next short message as another
    doubt: "make it Arabic" and "thank you" both came back `confirmation`.
    Two yes/no questions, each about the message by itself: a three-way
    word choice was already beyond the dev model. A language request
    written in one language targets the other unless the reading named one.
    """
    if _yes_no(llm, LANGUAGE_QUESTION, user_text):
        reading.wants = TRANSLATION
        reading.language = reading.language or (
            "ar" if message_language == "en" else "en")
        logger.info("[INTERPRET] settled: a language request, not a doubt")
        return reading
    doubt = _yes_no(llm, DOUBT_QUESTION, user_text)
    if doubt is None or doubt:
        reading.language = None
        logger.info("[INTERPRET] settled: a doubt")
        return reading
    # The full reading said "doubt" and the message alone says "neither":
    # the two disagree, so nothing is known well enough to act on. Ask,
    # with the two things it could have been. A wrong translation or a
    # fabricated "yes I am sure" is worse than one short question.
    reading.wants = CLARIFY
    reading.language = None
    reading.question_for_user = (
        "هل تريد أن أتحقق من الإجابة السابقة مرة أخرى، أم أن أعيدها بلغة "
        "أخرى، أم تقصد شيئًا آخر؟"
        if message_language == "ar" else
        "Do you want me to re-check the previous answer, give it in another "
        "language, or did you mean something else?")
    logger.info("[INTERPRET] settled: unclear; asking which was meant")
    return reading


def _repair(llm, user_text: str, *, names: List[str], cameras: List[str],
            has_result: bool, has_documents: bool) -> Optional[Interpretation]:
    """One more attempt, with the choice narrowed to the label alone.

    A small model that wandered off the schema usually still knows what the
    message wanted; asking for less gets it back.
    """
    try:
        reply = llm.invoke([
            SystemMessage(content=(
                "Reply with ONE JSON object and nothing else: "
                '{"wants": "data|translation|document|confirmation|chat", '
                '"shape": "report|answer", "question": "the request as one '
                'self-contained question"}. data = they want something from '
                "the database; translation = say the last answer in another "
                "language; document = the last answer as a file; "
                "confirmation = they are questioning the last answer; "
                "chat = they ask for nothing.")),
            HumanMessage(content=f"MESSAGE\n{user_text}"),
        ])
    except Exception as e:
        logger.warning("[INTERPRET] the second reading failed (%s)", e)
        return None
    parsed = _parse(getattr(reply, "content", reply))
    if not parsed:
        return None
    return validate(parsed, names=names, cameras=cameras, user_text=user_text,
                    has_result=has_result, has_documents=has_documents)


def interpret(llm, user_text: str, *, identity_index=None, camera_names=None,
              dialogue_state=None, has_result: bool = False,
              has_documents: bool = False, last_question: str = "",
              question_pending: bool = False, pending_question: str = "",
              pending_request: str = "", recent_turns=None,
              message_language: str = "en") -> Optional[Interpretation]:
    """Read the turn once. None means "no usable reading" — the caller
    falls back and nothing is guessed."""
    if llm is None or not str(user_text or "").strip():
        return None

    names = [str((e or {}).get("display_name") or "")
             for e in (identity_index or [])]
    names = [n for n in names if n]
    cameras = [str(c) for c in (camera_names or []) if c]

    try:
        reply = llm.invoke(_prompt(
            user_text, names=names, cameras=cameras,
            situation=_situation(dialogue_state, has_result=has_result,
                                 has_documents=has_documents,
                                 last_question=last_question,
                                 question_pending=question_pending,
                                 pending_question=pending_question),
            recent=_recent(recent_turns)))
    except Exception as e:
        logger.warning("[INTERPRET] the reading failed (%s); falling back", e)
        return None

    parsed = _parse(getattr(reply, "content", reply))
    reading = None
    if parsed:
        reading = validate(parsed, names=names, cameras=cameras,
                           user_text=user_text, has_result=has_result,
                           has_documents=has_documents,
                           question_pending=question_pending,
                           pending_request=pending_request,
                           recent_turns=recent_turns)
    if reading is None:
        # Say WHY, in structure only: the keys it returned and the label it
        # chose. Never the message or the question - the privacy rule that
        # keeps prompts out of the log holds here too.
        logger.info("[INTERPRET] unusable reading (keys=%s label=%r); asking "
                    "once more", sorted(parsed)[:8] if parsed else None,
                    str((parsed or {}).get("wants"))[:20])
        reading = _repair(llm, user_text, names=names, cameras=cameras,
                          has_result=has_result, has_documents=has_documents)
    if reading is None:
        logger.info("[INTERPRET] still unusable; falling back")
        return None

    # A doubt is confirmed on the message alone, away from the transcript
    # that biases the full reading toward it.
    if reading.wants == CONFIRMATION:
        reading = _settle_doubt(llm, user_text, reading, message_language)

    # A data reading whose every slot came from the SITUATION, none from
    # the message: "thank you" after two data turns was read as data with
    # the held camera copied in, and a query ran. Whether the message asks
    # for anything at all is one closed question about the message alone.
    if (reading.wants == DATA
            and not reading.unknown_people and not reading.unknown_camera
            and (reading.people or reading.camera)
            and not _mentions_any(user_text, reading.people + [reading.camera])):
        asks = _yes_no(llm, ASKS_QUESTION, user_text)
        if asks is False:
            reading.wants = CHAT
            reading.people, reading.camera = [], None
            logger.info("[INTERPRET] settled: the message asks for nothing")

    # Shapes only, never the message or the question: the privacy rule that
    # keeps SQL and prompts out of the log applies here too.
    logger.info("[INTERPRET] wants=%s shape=%s confidence=%.2f people=%d "
                "unknown=%d camera=%s language=%s format=%s about_previous=%s "
                "question_chars=%d asks=%s",
                reading.wants, reading.shape, reading.confidence,
                len(reading.people), len(reading.unknown_people),
                bool(reading.camera), reading.language, reading.format,
                reading.about_previous, len(reading.question),
                bool(reading.question_for_user))
    return reading
