import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _tracked_files() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [ROOT / path.decode() for path in output.rstrip(b"\0").split(b"\0")]


def test_tracked_text_files_do_not_contain_operator_markers() -> None:
    markers = ("/" + "Users/", "." + "ts.net", "claude.ai/code/" + "session_")
    findings: list[str] = []

    for path in _tracked_files():
        content = path.read_bytes()
        if b"\0" in content:
            continue
        text = content.decode(errors="replace")
        findings.extend(
            f"{path.relative_to(ROOT)}: {marker}" for marker in markers if marker in text
        )

    assert findings == []


def test_cutover_example_preserves_operator_placeholders() -> None:
    script = (ROOT / "s00-cutover.sh").read_text()

    assert re.search(r"export PWA_ORIGIN=.*https://phone\.example:443", script)
    assert re.search(r"export PHONE_BOARD_URL=.*https://phone\.example:443", script)
    assert re.search(r"export CANARY_TASK_ID=.*replace-with-nonterminal-task-id", script)
    assert re.search(
        r"export KNOWN_TASK_NAME=.*replace-with-task-name-visible-on-phone-board", script
    )
    assert re.search(r"export ISSUE_202_COMMIT=.*replace-with-closing-commit", script)


def test_private_working_files_are_ignored_and_untracked() -> None:
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    tracked = [path.relative_to(ROOT).as_posix() for path in _tracked_files()]

    assert "/.internal/" in ignore
    assert "agent-session-*.html" in ignore
    assert not any(path.startswith(".internal/") for path in tracked)
    assert not any(Path(path).name.startswith("agent-session-") for path in tracked)
