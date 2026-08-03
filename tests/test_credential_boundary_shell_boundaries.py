"""Shell helper-boundary counterexample for the caller sweep."""

from __future__ import annotations

from pathlib import Path

import pytest
from credential_guard_helpers import assert_minimum_subjects, discover_shell_service_callers


def test_same_line_helper_tail_is_outside_the_exemption(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    source = tmp_path / "same-line.sh"
    source.write_text(
        '_panopticon_curl() { curl "$PANOPTICON_SERVICE_URL/a"; }; '
        'curl "$PANOPTICON_SERVICE_URL/b"\n'
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [(source, 1)]


def test_multiline_helper_closing_line_tail_is_outside_the_exemption(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    source = tmp_path / "closing-tail.sh"
    source.write_text(
        '_panopticon_curl() {\n  curl "$PANOPTICON_SERVICE_URL/a"\n'
        '}; curl "$PANOPTICON_SERVICE_URL/b"\n'
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [(source, 3)]


def test_quoted_prefix_and_unrelated_continuation_do_not_hide_calls(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    source = tmp_path / "syntax-boundaries.sh"
    source.write_text(
        '"noop"; curl "$PANOPTICON_SERVICE_URL/one"\ncurl "$PANOPTICON_SERVICE_URL/two"; echo \\\n'
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [
        (source, 1),
        (source, 2),
    ]


def test_quoted_command_after_separator_is_not_an_unquoted_call(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    source = tmp_path / "quoted-command.sh"
    source.write_text('true; "curl" "$PANOPTICON_SERVICE_URL/tasks"\n')
    [caller] = discover_shell_service_callers(tmp_path)
    assert caller.bare_curl_violations == ()


def test_shell_caller_floor_rejects_two_discovered_callers() -> None:
    # 2119: REQ-044.2.1
    with pytest.raises(AssertionError, match="shell"):
        assert_minimum_subjects(["one", "two"], 3, "shell")


def test_unrelated_url_text_does_not_make_other_curl_a_task_call(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    source = tmp_path / "unrelated-url.sh"
    source.write_text('curl https://other.example/tasks; echo "$PANOPTICON_SERVICE_URL"\n')
    [caller] = discover_shell_service_callers(tmp_path)
    assert caller.bare_curl_violations == ()


def test_indented_and_unspaced_separator_calls_are_detected(tmp_path: Path) -> None:
    # 2119: REQ-044.2.1
    source = tmp_path / "spacing.sh"
    source.write_text(
        '\tcurl "$PANOPTICON_SERVICE_URL/indented"\ntrue;curl "$PANOPTICON_SERVICE_URL/unspaced"\n'
    )
    [caller] = discover_shell_service_callers(tmp_path)
    assert [(item.path, item.line_number) for item in caller.bare_curl_violations] == [
        (source, 1),
        (source, 2),
    ]
