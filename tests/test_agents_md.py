"""The operator glossary accurately describes attention-signal lifecycle and timing."""

from pathlib import Path


def _glossary() -> str:
    after_heading = (Path(__file__).parents[1] / "AGENTS.md").read_text().split("## Glossary", 1)[1]
    return after_heading.split("\n## ", 1)[0]


def _blocked_entry() -> str:
    return _glossary().split("- **Turn-flip / blocked**", 1)[1].split("\n- **Skill**", 1)[0]


def _blocked_lifecycle() -> str:
    normalized = " ".join(_blocked_entry().split())
    lifecycle = normalized.split("A turn-to-agent", 1)[1].split("Claude's", 1)[0]
    return "A turn-to-agent " + lifecycle.strip()


# 2119: REQ-008.8.1
# 2119: REQ-008.8.2
# 2119: REQ-008.8.3
# 2119: REQ-008.8.4
def test_glossary_states_the_complete_blocked_lifecycle_without_qualifiers() -> None:
    assert _blocked_lifecycle() == (
        "A turn-to-agent write clears `blocked`, because the user has addressed the task; "
        "every task state change clears `blocked`, because the state that raised it has ended. "
        "A turn-to-user write preserves `blocked`, and the agent can explicitly set `blocked` "
        "again after either automatic clear if it is still stuck."
    )


# 2119: REQ-008.8.1
def test_glossary_documents_agent_turn_blocked_clear() -> None:
    assert "A turn-to-agent write clears `blocked`" in _glossary()


# 2119: REQ-008.8.2
def test_glossary_documents_state_change_blocked_clear() -> None:
    entry = _blocked_entry()
    assert entry.count("every task state change clears `blocked`") == 1
    assert "except" not in entry.lower()


# 2119: REQ-008.8.3
def test_glossary_documents_user_turn_blocked_preservation() -> None:
    assert "A turn-to-user write preserves `blocked`" in _glossary()


# 2119: REQ-008.8.4
def test_glossary_documents_explicit_reblock() -> None:
    assert (
        "the agent can explicitly set `blocked` again after either automatic clear" in _glossary()
    )


# 2119: REQ-008.9.1
def test_glossary_documents_claude_turn_signal_floor() -> None:
    assert (
        "Claude's blocking `UserPromptSubmit` command hook runs before prompt processing; "
        "its floor is callback process startup plus the synchronous task-service write"
        in _glossary()
    )


# 2119: REQ-008.9.2
def test_glossary_documents_codex_turn_signal_floor() -> None:
    assert (
        "Codex's blocking `UserPromptSubmit` command hook runs before prompt processing; "
        "its floor is callback process startup plus the synchronous task-service write"
        in _glossary()
    )


# 2119: REQ-008.9.3
def test_glossary_documents_pi_turn_signal_floor() -> None:
    assert (
        "Pi's `input` event runs before prompt processing, and its handler waits for the "
        "task-service write" in _glossary()
    )
