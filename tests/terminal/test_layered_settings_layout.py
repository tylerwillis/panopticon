"""Painted-layout checks for layered-setting hints in the task-creation modal."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_dashboard import _FakeClient
from textual.widgets import Label, Static

from panopticon.terminal.dashboard import Dashboard


def _painted_text_with_marker(screenshot: str, marker: str) -> str:
    text_elements = [
        element for element in ET.parse(screenshot).getroot().iter() if element.tag.endswith("text")
    ]
    marker_element = next(
        element for element in text_elements if marker in " ".join((element.text or "").split())
    )
    style_class = marker_element.attrib["class"]
    return " ".join(
        " ".join((element.text or "").split())
        for element in text_elements
        if element.attrib.get("class") == style_class
    )


# 2119: layered-settings-hints.3.1
# 2119: layered-settings-hints.6.1
@pytest.mark.parametrize("terminal_size", [(80, 24), (120, 30)])
@pytest.mark.parametrize(
    ("repo_defaults", "workflow_defaults", "expected_source"),
    [
        ({"default_harness": "codex", "default_model": None}, {}, "repo default"),
        (
            {},
            {"default_harness": "codex", "default_model": "terra:high"},
            "workflow default",
        ),
        ({}, {}, "app default"),
    ],
)
async def test_task_creation_hint_is_painted_in_full(
    tmp_path: Path,
    terminal_size: tuple[int, int],
    repo_defaults: dict[str, str | None],
    workflow_defaults: dict[str, str],
    expected_source: str,
) -> None:
    fake = _FakeClient(
        [],
        repos=[
            {
                "id": "r1",
                "name": "r1",
                "git_url": "",
                "default_base": "main",
                **repo_defaults,
            }
        ],
        workflows=[{"name": "spike", "when_to_use": "", **workflow_defaults}],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test(size=terminal_size) as pilot:
        await pilot.pause()
        await pilot.press("n", "enter", "enter")
        await pilot.pause()

        screenshot = app.save_screenshot("task-creation-layered-hint.svg", str(tmp_path))
        summary = app.screen.query_one("#launch-summary", Static)
        painted_hint = _painted_text_with_marker(screenshot, "Harness/model precedence:")

        assert (
            "Harness/model precedence: workflow config; repo config > app default; "
            "change here to override this task." in painted_hint
        )
        assert f"set by {expected_source}" in painted_hint
        assert summary.styles.color.a == pytest.approx(0.6)
        assert summary.region.height >= 3


# 2119: layered-settings-hints.3.1
@pytest.mark.parametrize("terminal_size", [(80, 24), (120, 30)])
@pytest.mark.parametrize(
    ("key", "surface", "painted_fragments"),
    [
        (
            "w",
            "workflows",
            (
                "Reviewer defaults: repo config can override;",
                "Workflow availability: repo config filters;",
                "Harness/model defaults: override at per-task creation.",
            ),
        ),
        (
            "g",
            "repos",
            (
                "Reviewer defaults: workflow config;",
                "Workflow availability: workflow config;",
                "Harness/model defaults: override at per-task creation.",
            ),
        ),
    ],
)
async def test_table_screen_layered_hints_are_painted_in_full(
    tmp_path: Path,
    key: str,
    surface: str,
    painted_fragments: tuple[str, ...],
    terminal_size: tuple[int, int],
) -> None:
    fake = _FakeClient(
        [],
        repos=[{"id": "r1", "name": "r1", "git_url": "", "default_base": "main"}],
        workflows=[
            {
                "name": "spike",
                "when_to_use": "",
                "path": "/workflows/spike.py",
                "built_in": True,
            }
        ],
    )
    app = Dashboard(fake)  # type: ignore[arg-type]

    async with app.run_test(size=terminal_size) as pilot:
        await pilot.press(key)
        await pilot.pause()

        screenshot = app.save_screenshot(f"{surface}-layered-hint.svg", str(tmp_path))
        hint = app.screen.query_one(f"#{surface}-layered-settings-hint", Label)
        painted_hint = _painted_text_with_marker(screenshot, "Reviewer defaults:")

        for fragment in painted_fragments:
            assert fragment in str(hint.render())
        assert str(hint.render()) in painted_hint
        assert hint.region.height >= 2
