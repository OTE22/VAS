"""A session id is a name, not a path.

`load_session()` and `delete_session()` built a filesystem path straight from
a caller-supplied string:

    session_file = self.storage_dir / f"{session_id}.json"

and the API accepted any non-empty string. So "../../../../etc/hosts"
resolved to /etc/hosts.json - outside the storage directory entirely. The
realistic damage is reading or deleting ANOTHER user's session file, which is
their whole conversation.

Ids the system issues are UUID hex. Anything that is not that shape is not an
id, so it is refused before it ever becomes a path.

    docker exec face_recognition_api python -m pytest tests/test_session_path_safety.py -v
"""

import pytest

from sql_agent.conversation_memory import is_safe_session_id


@pytest.mark.parametrize("evil", [
    "../../../../etc/hosts",
    "../other_user_session",
    "..",
    "a/../../b",
    "sub/dir",
    "back\slash",
    "/absolute",
    "C:\windows\system32",
    "with space",
    "semi;colon",
    "null\x00byte",
    "",
    "   ",
])
def test_a_path_is_not_an_id(evil):
    """THE fix. None of these may ever reach the filesystem."""
    assert not is_safe_session_id(evil), evil


@pytest.mark.parametrize("good", [
    "session_20260831_120000",
    "3f2b1c4d5e6f7a8b9c0d1e2f3a4b5c6d",
    "abc123",
    "a-b_c",
])
def test_a_real_id_is_accepted(good):
    """THE negative control.

    Refusing everything would 'fix' the traversal by breaking sessions, so
    the shapes this system actually issues must still pass.
    """
    assert is_safe_session_id(good), good


def test_the_id_cannot_be_a_dotfile_or_traverse_upward():
    assert not is_safe_session_id(".")
    assert not is_safe_session_id(".hidden")


def test_a_refused_id_never_touches_the_filesystem(tmp_path, monkeypatch):
    """Belt and braces: the loader must refuse, not just the validator."""
    from sql_agent.conversation_memory import ConversationMemory

    memory = ConversationMemory.__new__(ConversationMemory)
    memory.storage_dir = tmp_path
    assert memory.load_session("../../../../etc/hosts") is False
    assert memory.delete_session("../../../../etc/hosts") is False
