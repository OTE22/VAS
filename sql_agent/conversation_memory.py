"""
Conversation Memory Module
==========================
Stores and retrieves conversation history for context-aware responses.
"""

import json
import re
import logging
import os
import tempfile
import threading
from typing import Any, List, Dict, Optional
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langchain_core.messages import message_to_dict, messages_from_dict

logger = logging.getLogger(__name__)

# Current schema of the working_context block inside a session file.
WORKING_CONTEXT_VERSION = 1

# The keys the agent understands today. Unknown keys are PRESERVED on save
# (see save_session) — this list is what migrate_working_context guarantees
# exists, not a whitelist of what may be stored.
_WORKING_CONTEXT_KEYS = (
    "last_result",        # bounded preview + durable reference, never raw rows
    "last_artifact_id",
    "last_query",
    "last_action",
    "active_filters",
    "selected_entity",
    "response_language",
    # The canonical dialogue state (sql_agent/dialogue_state.py): active
    # task, filters and references with per-field provenance. Committed ONLY
    # through application-validated deltas; carries its own version and
    # migration inside.
    "dialogue_state",
    # A DERIVED CACHE of older conversation, rebuilt whenever it is stale or
    # version-incompatible. Never a source of truth: exact values come from
    # dialogue_state and its provenance, so losing this costs nothing but a
    # regeneration.
    "conversation_summary",
)

# One lock per session FILE, process-wide. Two concurrent turns for the same
# user must not interleave read-merge-write: the loser would silently drop the
# winner's field. Keyed by resolved path so a user's sessions do not contend.
_session_locks: Dict[str, threading.Lock] = {}

# Bounded RAW transcript per session file. 40 messages = 20 exchanges — far
# more than any prompt window consumes (get_conversation_context reads 6).
# The bound is on raw turns ONLY; see the pruning comment in save_session.
_MAX_STORED_MESSAGES = 40
_session_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _session_locks_guard:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _session_locks[key] = lock
        return lock


def migrate_working_context(raw: Optional[dict]) -> dict:
    """Bring a stored working_context up to the current version.

    An explicit migration point, deliberately not a pile of .get() calls: the
    next schema change gets a place to live instead of becoming another silent
    compatibility trap. Unknown keys are carried through untouched.
    """
    if not isinstance(raw, dict):
        raw = {}
    ctx = dict(raw)
    version = ctx.get("version")

    # v0 (absent) -> v1: the pre-versioning shape. Nothing to rewrite; the
    # known keys simply default to None.
    if version is None:
        ctx["version"] = WORKING_CONTEXT_VERSION

    for key in _WORKING_CONTEXT_KEYS:
        ctx.setdefault(key, None)
    return ctx


def _json_safe_cell(value, max_cell: int = 80):
    """One SQL cell, guaranteed JSON-serializable and bounded.

    SQL rows carry datetimes, Decimals, UUIDs and memoryviews. Storing them
    raw made json.dump raise and take the whole session file down with it.
    """
    import datetime as _dt
    import decimal as _decimal
    import uuid as _uuid

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _decimal.Decimal):
        return float(value)
    if isinstance(value, _uuid.UUID):
        return str(value)
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= max_cell else text[:max_cell] + "…"


#: A session id is a NAME, not a path. Ids this system issues are UUID hex or
#: "session_<timestamp>"; anything else is not an id.
#:
#: `load_session` and `delete_session` built a path straight from a
#: caller-supplied string, and the API accepted any non-empty string, so
#: "../../../../etc/hosts" resolved outside the storage directory. The
#: realistic damage was reading or deleting another user's whole conversation.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def is_safe_session_id(session_id) -> bool:
    """Whether this may be turned into a filename at all.

    An allowlist, not an escape: separators, dots, drive letters, NUL bytes
    and empties are all simply not this shape, so no traversal has to be
    anticipated one form at a time.
    """
    return bool(isinstance(session_id, str)
                and _SAFE_SESSION_ID.match(session_id))


class ConversationMemory:
    """
    Manages conversation history with persistent storage.
    Supports user-based conversation sessions (like ChatGPT).
    Each user has their own persistent conversation history.
    """

    # The pre-2026-08-30 location: RELATIVE, resolved against CWD — inside the
    # container's writable layer with no volume behind it, so a
    # --force-recreate erased every user's conversational memory. Kept only so
    # existing session files can be migrated forward once.
    _LEGACY_STORAGE_DIR = "conversation_cache"

    def __init__(self, storage_dir: Optional[str] = None, user_id: Optional[int] = None):
        """
        Initialize conversation memory.

        Args:
            storage_dir: Directory to store conversation history. Defaults to
                settings.CONVERSATION_CACHE_DIR — a derived path under
                STORAGE_DIR, which the deployment's storage volume already
                covers, so memory survives a container recreate. NOTE: this is
                single-worker durability; the per-file locks here are
                process-local (see the config.py property's docstring).
            user_id: User ID for user-specific storage. If None, uses default storage.
        """
        if storage_dir is None:
            try:
                from config import settings
                storage_dir = settings.CONVERSATION_CACHE_DIR
            except Exception:      # config unavailable (isolated unit tests)
                storage_dir = self._LEGACY_STORAGE_DIR
        self.storage_dir = Path(storage_dir)
        self.user_id = user_id

        # Create user-specific directory if user_id is provided
        if user_id:
            self.storage_dir = self.storage_dir / f"user_{user_id}"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_sessions()

        self.current_session_id: Optional[str] = None
        self.messages: List[BaseMessage] = []
        # CACHE ONLY. The session file is the source of truth for working
        # context: a container restart, an LRU eviction of this instance, or a
        # future WORKERS>1 must not lose "the last report". Every read path
        # tolerates this being empty and reloads from disk.
        self.working_context: dict = migrate_working_context(None)

    def _migrate_legacy_sessions(self) -> None:
        """One-time move of session files from the old CWD-relative location.

        Without this, the location fix would itself do what it prevents:
        every user's "last report" and transcript would vanish on the deploy
        that ships it. Copy-then-keep (not move): the new file wins from now
        on, and the legacy file stays as a harmless remnant rather than being
        deleted from under an older process that may still hold it open.
        Never fatal, and skipped entirely when the legacy dir does not exist
        or IS the configured dir.
        """
        try:
            legacy_root = Path(self._LEGACY_STORAGE_DIR)
            legacy_dir = legacy_root / f"user_{self.user_id}" if self.user_id else legacy_root
            if not legacy_dir.is_dir():
                return
            if legacy_dir.resolve() == self.storage_dir.resolve():
                return
            for legacy_file in legacy_dir.glob("*.json"):
                target = self.storage_dir / legacy_file.name
                if target.exists():
                    continue           # the new location already has newer state
                import shutil
                shutil.copy2(legacy_file, target)
                logger.info("Migrated legacy session %s -> %s",
                            legacy_file, target)
        except Exception as e:
            logger.warning("Legacy session migration skipped: %s", e)

    def start_session(self, session_id: Optional[str] = None) -> str:
        """
        Start a new conversation session.
        For user-based memory, creates a persistent session that continues across requests.

        Args:
            session_id: Optional session ID. If None, generates a new one.
                       For user-based memory, uses "user_{user_id}_main" as default.

        Returns:
            Session ID
        """
        if session_id is None:
            if self.user_id:
                # User-specific persistent session
                session_id = f"user_{self.user_id}_main"
            else:
                # Default session-based
                session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        self.current_session_id = session_id
        
        # Try to load existing session for user-based memory
        if self.user_id:
            self.load_session(session_id)
        else:
            self.messages = []
            
        return session_id

    def reset_session(self) -> bool:
        """Begin a genuinely clean conversation, in RAM and on disk.

        `start_session()` reloads the user's persistent `user_{id}_main`
        session, which is right for an ordinary turn and wrong for someone
        asking for a new conversation: they got the old one back and were
        told it was new.

        The clearing has to be explicit. `save_session` is a READ-MERGE-WRITE
        on purpose — it preserves unknown top-level keys and MERGES the
        working context, so that a concurrent turn cannot lose a field — which
        means emptying `self.working_context` and saving would write the old
        context straight back. This replaces the document instead.

        What goes: the transcript, and the working context that makes "it",
        "the same one" and "the last report" resolvable — dialogue state, task
        history, last result and artifact references. What stays: the user's
        artifacts and query history, which are their data and live in the
        database, not here.
        """
        self.messages = []
        self.working_context = {}

        if not self.current_session_id:
            return True

        session_file = self.storage_dir / f"{self.current_session_id}.json"
        with _lock_for(session_file):
            try:
                self._write_atomic(session_file, {
                    "session_id": self.current_session_id,
                    "created_at": datetime.utcnow().isoformat(),
                    "message_count": 0,
                    "messages": [],
                    "working_context": {},
                })
                logger.info("[MEMORY] reset session %s", self.current_session_id)
                return True
            except Exception as e:
                logger.warning("⚠️ Error resetting session: %s", e)
                return False

    def load_session(self, session_id: str) -> bool:
        """
        Load a previous conversation session.

        Args:
            session_id: Session ID to load

        Returns:
            True if session loaded successfully, False otherwise
        """
        if not is_safe_session_id(session_id):
            logger.warning("[MEMORY] refused an unsafe session id")
            return False
        session_file = self.storage_dir / f"{session_id}.json"

        if not session_file.exists():
            return False

        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.messages = messages_from_dict(data.get("messages", []))
                self.working_context = migrate_working_context(data.get("working_context"))
                self.current_session_id = session_id
                return True
        except Exception as e:
            logger.warning(f"⚠️ Error loading session {session_id}: {e}")
            return False

    def _write_atomic(self, session_file: Path, data: dict) -> None:
        """Write the session file so a reader never sees a partial document.

        tmp in the SAME directory (os.replace is only atomic within a
        filesystem) -> flush -> fsync -> replace. Without this, a crash or a
        concurrent read during json.dump leaves a truncated file, and the whole
        session — every prior turn — is unreadable.
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=str(session_file.parent), prefix=f".{session_file.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                # default=str is deliberate INSURANCE, not laziness. A single
                # value this class did not anticipate (a datetime from a SQL
                # preview, once) otherwise raises mid-dump and the ENTIRE
                # session — every prior turn and the working context — fails
                # to save, leaving the agent quietly amnesiac. Degrading one
                # cell to its string form is always the better trade.
                json.dump(data, handle, indent=2, ensure_ascii=False,
                          default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, session_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def save_session(self) -> bool:
        """
        Save current conversation session to disk.

        READ-MERGE-WRITE, under a per-file lock. The previous version rebuilt
        the document from four fixed fields, so ANY other key — including the
        working context this agent depends on — was destroyed by the next
        autosave. Merging instead means a field added later by other code
        survives even if this class knows nothing about it.

        Returns:
            True if saved successfully, False otherwise
        """
        if not self.current_session_id:
            return False

        session_file = self.storage_dir / f"{self.current_session_id}.json"

        with _lock_for(session_file):
            try:
                existing: dict = {}
                if session_file.exists():
                    try:
                        with open(session_file, 'r', encoding='utf-8') as f:
                            loaded = json.load(f)
                        if isinstance(loaded, dict):
                            existing = loaded
                    except Exception:
                        # A corrupt file must not block the conversation; it is
                        # replaced by a good one below rather than inherited.
                        logger.warning("⚠️ Unreadable session file, rewriting: %s",
                                       session_file.name)

                # SEMANTIC cap, not a byte cap: the transcript is rewritten in
                # full on every save, so an unbounded message list makes turn
                # latency grow with conversation age forever. Only the OLDEST
                # RAW TURNS are pruned — working_context, unknown top-level
                # keys, artifact/result references and provenance are never
                # candidates, so nothing reference resolution depends on is
                # lost. (A rolling summary of pruned turns is Part C's job;
                # until then the durable working context carries the state
                # that matters across the horizon.)
                if len(self.messages) > _MAX_STORED_MESSAGES:
                    self.messages = self.messages[-_MAX_STORED_MESSAGES:]

                # Merge the WHOLE working context so a concurrent turn that set
                # a different field does not lose it.
                merged_context = migrate_working_context(existing.get("working_context"))
                merged_context.update(self.working_context or {})
                self.working_context = merged_context

                data = dict(existing)          # preserve unknown top-level keys
                data.update({
                    "session_id": self.current_session_id,
                    "created_at": datetime.utcnow().isoformat(),  # naive UTC (storage convention)
                    "message_count": len(self.messages),
                    "messages": [message_to_dict(msg) for msg in self.messages],
                    "working_context": merged_context,
                })

                self._write_atomic(session_file, data)
                return True
            except Exception as e:
                logger.warning(f"⚠️ Error saving session: {e}")
                return False

    # ------------------------------------------------------------------
    # Working context — the agent's memory of what "it" / "the last report"
    # refer to. Always written through to disk; never trusted from RAM alone.
    # ------------------------------------------------------------------
    def update_working_context(self, **fields: Any) -> bool:
        """Merge fields into the working context and persist immediately."""
        if not fields:
            return False
        self.working_context.update(fields)
        return self.save_session()

    def get_working_context(self, reload: bool = False) -> dict:
        """Return the working context, optionally re-reading it from disk.

        `reload=True` is for the request path: this instance may be a fresh
        object after a restart or an LRU eviction, and the file is authoritative.
        """
        if reload and self.current_session_id:
            session_file = self.storage_dir / f"{self.current_session_id}.json"
            if session_file.exists():
                try:
                    with open(session_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self.working_context = migrate_working_context(data.get("working_context"))
                except Exception as e:
                    logger.warning("⚠️ Could not reload working context: %s", e)
        return self.working_context

    @staticmethod
    def build_result_reference(rows: list, sql: Optional[str], purpose: Optional[str],
                              history_id: Optional[int] = None,
                              max_rows: int = 3, max_cell: int = 80,
                              question: Optional[str] = None) -> dict:
        """A bounded, non-sensitive summary of a SQL result for the planner.

        Session files are long-lived and hold surveillance query output, so the
        full result set is NEVER copied here: this keeps a tiny preview for the
        planner's context line plus `history_id`, a durable reference into
        user_query_history where the complete result already lives under the
        conversation-data retention and access policy.
        """
        rows = rows or []
        preview = []
        for row in rows[:max_rows]:
            if isinstance(row, dict):
                preview.append({k: _json_safe_cell(v, max_cell)
                                for k, v in list(row.items())[:8]})
        return {
            "columns": list(rows[0].keys())[:12] if rows and isinstance(rows[0], dict) else [],
            "row_count": len(rows),
            "preview": preview,
            "sql": (sql or "")[:1000] or None,
            "purpose": (purpose or "")[:200] or None,
            # The user's OWN words for what they asked. `purpose` is written
            # for the SQL generator and reads like it; `last_query` is only
            # "the last thing typed", which after an unrelated turn names the
            # wrong thing — a report titled "hi". The question belongs WITH
            # the result it produced.
            "question": (question or "")[:200] or None,
            "history_id": history_id,
        }

    def add_message(self, message: BaseMessage):
        """Add a message to the current conversation."""
        self.messages.append(message)
        # Auto-save after each message
        self.save_session()

    def add_user_message(self, content: str):
        """Add a user message to the conversation."""
        self.add_message(HumanMessage(content=content))

    def add_ai_message(self, content: str):
        """Add an AI response to the conversation."""
        self.add_message(AIMessage(content=content))

    def get_recent_messages(self, limit: int = 10) -> List[BaseMessage]:
        """
        Get recent messages from the conversation.

        Args:
            limit: Maximum number of messages to return

        Returns:
            List of recent messages
        """
        return self.messages[-limit:] if self.messages else []

    def get_conversation_context(self, limit: int = 5) -> str:
        """
        Get formatted conversation context for prompts.

        Args:
            limit: Number of recent messages to include

        Returns:
            Formatted conversation context string
        """
        recent = self.get_recent_messages(limit)

        if not recent:
            return ""

        context_lines = ["Previous conversation context:"]
        for msg in recent:
            if isinstance(msg, HumanMessage):
                context_lines.append(f"User: {msg.content}")
            elif isinstance(msg, AIMessage):
                context_lines.append(f"Assistant: {msg.content}")

        return "\n".join(context_lines)

    def clear(self):
        """Clear current conversation messages."""
        self.messages = []

    def list_sessions(self) -> List[Dict]:
        """
        List all available conversation sessions.

        Returns:
            List of session metadata dictionaries
        """
        sessions = []
        for session_file in self.storage_dir.glob("*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    sessions.append({
                        "session_id": data.get("session_id", session_file.stem),
                        "created_at": data.get("created_at", ""),
                        "message_count": data.get("message_count", 0)
                    })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("created_at", ""), reverse=True)

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a conversation session.

        Args:
            session_id: Session ID to delete

        Returns:
            True if deleted successfully, False otherwise
        """
        if not is_safe_session_id(session_id):
            logger.warning("[MEMORY] refused an unsafe session id")
            return False
        session_file = self.storage_dir / f"{session_id}.json"

        if session_file.exists():
            try:
                session_file.unlink()
                if self.current_session_id == session_id:
                    self.current_session_id = None
                    self.messages = []
                return True
            except Exception as e:
                logger.warning(f"⚠️ Error deleting session: {e}")
                return False

        return False

