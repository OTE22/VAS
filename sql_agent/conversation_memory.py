"""
Conversation Memory Module
==========================
Stores and retrieves conversation history for context-aware responses.
"""

import json
import logging
import os
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langchain_core.messages import message_to_dict, messages_from_dict

logger = logging.getLogger(__name__)


class ConversationMemory:
    """
    Manages conversation history with persistent storage.
    Supports user-based conversation sessions (like ChatGPT).
    Each user has their own persistent conversation history.
    """

    def __init__(self, storage_dir: str = "conversation_cache", user_id: Optional[int] = None):
        """
        Initialize conversation memory.

        Args:
            storage_dir: Directory to store conversation history
            user_id: User ID for user-specific storage. If None, uses default storage.
        """
        self.storage_dir = Path(storage_dir)
        self.user_id = user_id
        
        # Create user-specific directory if user_id is provided
        if user_id:
            self.storage_dir = self.storage_dir / f"user_{user_id}"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.current_session_id: Optional[str] = None
        self.messages: List[BaseMessage] = []

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

    def load_session(self, session_id: str) -> bool:
        """
        Load a previous conversation session.

        Args:
            session_id: Session ID to load

        Returns:
            True if session loaded successfully, False otherwise
        """
        session_file = self.storage_dir / f"{session_id}.json"

        if not session_file.exists():
            return False

        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.messages = messages_from_dict(data.get("messages", []))
                self.current_session_id = session_id
                return True
        except Exception as e:
            logger.warning(f"⚠️ Error loading session {session_id}: {e}")
            return False

    def save_session(self) -> bool:
        """
        Save current conversation session to disk.

        Returns:
            True if saved successfully, False otherwise
        """
        if not self.current_session_id:
            return False

        session_file = self.storage_dir / f"{self.current_session_id}.json"

        try:
            data = {
                "session_id": self.current_session_id,
                "created_at": datetime.utcnow().isoformat(),  # naive UTC (storage convention)
                "message_count": len(self.messages),
                "messages": [message_to_dict(msg) for msg in self.messages]
            }

            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            return True
        except Exception as e:
            logger.warning(f"⚠️ Error saving session: {e}")
            return False

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

