from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "2119.yml"
GUIDANCE = ROOT / "AGENTS.md"
RFC2119_ENV = {**os.environ, "NPM_CONFIG_IGNORE_SCRIPTS": "true"}


# 2119-spec: file-scoped-requirement-ids


# 2119: 1.1, 1.2, 1.3, 1.6, 6.4
def test_2119_workflow_pins_cli_and_npm_and_hardens_fetch() -> None:
    workflow = WORKFLOW.read_text()
    gate = re.search(
        r"^\s*- run: npx --yes rfc2119@0\.7\.0 check\n"
        r"\s+env:\n\s+NPM_CONFIG_IGNORE_SCRIPTS: \"true\"$",
        workflow,
        re.MULTILINE,
    )
    npm_pin = re.search(r"^\s*- run: npm install --global npm@10\.9\.3$", workflow, re.MULTILINE)
    versions = list(re.finditer(r"^\s+node --version\n\s+npm --version$", workflow, re.MULTILINE))
    major_guard = re.search(
        r'^\s+test "\$\(npm --version \| cut --delimiter=\. --fields=1\)" = "10" \|\| exit 1$',
        workflow,
        re.MULTILINE,
    )
    assert gate is not None
    assert "\n      if:" not in workflow
    assert "continue-on-error" not in workflow
    assert npm_pin is not None and npm_pin.start() < gate.start()
    assert all(npm_pin.end() < match.start() for match in re.finditer(r"\bnpx\b", workflow))
    active_versions = [
        version for version in versions if npm_pin.end() < version.start() < gate.start()
    ]
    assert len(active_versions) == 1
    assert all(
        active_versions[0].end() < match.start() for match in re.finditer(r"\bnpx\b", workflow)
    )
    assert major_guard is not None and major_guard.start() < npm_pin.start()


# 2119: 1.4, 1.5
def test_no_older_rfc2119_pin_remains_in_active_instructions() -> None:
    old_pin = re.compile(r"rfc2119@(?:0\.[0-6](?:\.\d+)?|0\.7\.0-[0-9A-Za-z.-]+)(?![\d.])")
    tracked_markdown = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    candidates = [
        *(ROOT / path for path in tracked_markdown),
        *sorted((ROOT / ".github" / "workflows").glob("*")),
    ]
    for path in candidates:
        if path.is_file():
            assert old_pin.search(path.read_text()) is None, path


# 2119: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2
def test_adoption_diff_does_not_rewrite_legacy_identity_or_verdict_files() -> None:
    if _adoption_is_already_on_base():
        pytest.skip("adoption is on the base; the migration window has closed")
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            merge_base,
            "--",
            "specs/REQ-*.md",
            ".2119/verdicts/REQ-*.json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert changed == []

    changed_tests = subprocess.run(
        ["git", "diff", "--name-only", merge_base, "--", "tests"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert set(changed_tests) <= {"tests/test_2119_file_scoped_ids.py"}


# 2119: 2.5, 2.6, 2.7, 2.8, 3.5, 3.6, 4.5, 4.6
def test_agent_guidance_teaches_scoped_authoring_and_stable_identity() -> None:
    guidance = GUIDANCE.read_text()
    normalized = " ".join(guidance.split())
    assert (
        "New specs use a lowercase kebab-case filename as their namespace, such as "
        "`specs/repo-picker.md`, and bare numbered section headings such as `### 3: Selection`. "
        "Allocate numbers only within that file; item 2 in that section has canonical ID "
        "`repo-picker.3.2`. Legacy `REQ-NNN-*` specs and new file-scoped specs coexist "
        "indefinitely."
    ) in normalized
    assert (
        "A test file may import its spec with `# 2119-spec: repo-picker`; a later bare "
        "annotation `# 2119: 3.2` and the full annotation `# 2119: repo-picker.3.2` both "
        "resolve to repo-picker.3.2. Full canonical IDs remain available for cross-spec and "
        "legacy references, for example `# 2119: REQ-001.2.3`."
    ) in normalized
    assert (
        "Renaming a file-scoped spec changes its canonical IDs, invalidates its verdicts, "
        "and requires re-review."
    ) in normalized
    for command in ("lint", "review --dispatch", "check"):
        assert f"npx --yes rfc2119@0.7.0 {command}" in guidance
    assert re.search(r"\bnpx(?:\s+--yes)?\s+rfc2119(?!@0\.7\.0)", guidance) is None


# 2119: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4
def test_070_resolves_both_grammars_and_distinct_file_namespaces(tmp_path: Path) -> None:
    verdicts = ROOT / ".2119" / "verdicts"
    assert list(verdicts.glob("REQ-*.json"))
    assert list(verdicts.glob("file-scoped-requirement-ids.*.json"))
    result = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "check", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=RFC2119_ENV,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    legacy_verdict_ids = {
        json.loads(path.read_text())["requirementId"] for path in verdicts.glob("REQ-*.json")
    }
    assert report["ok"] is True
    assert report["staleReviews"] == []
    assert report["requirementCount"] >= len(legacy_verdict_ids) + 35
    assert report["coveredCount"] == report["requirementCount"]

    (tmp_path / "specs").mkdir()
    (tmp_path / "tests").mkdir()
    for stem in ("alpha-feature", "beta-feature"):
        (tmp_path / "specs" / f"{stem}.md").write_text(
            f"# {stem}\n\n## Overview\n\nTest.\n\n## Requirements\n\n"
            "### 1: Setup\n\n1. It MUST initialize.\n\n"
            "### 2: Behavior\n\n1. It MUST work.\n"
        )
    (tmp_path / "specs" / "REQ-900-legacy.md").write_text(
        "# REQ-900: Legacy\n\n## Overview\n\nTest.\n\n## Requirements\n\n"
        "### REQ-900.1: Behavior\n\n1. It MUST work.\n"
    )
    (tmp_path / "tests" / "coverage.py").write_text(
        "# 2119: alpha-feature.1.1, alpha-feature.2.1, "
        "beta-feature.1.1, beta-feature.2.1, REQ-900.1.1\n"
    )
    discovery = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "lint"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=RFC2119_ENV,
    )
    assert discovery.returncode == 0, discovery.stdout + discovery.stderr
    assert "3 spec file(s) clean" in discovery.stdout
    collision = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "cover"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=RFC2119_ENV,
    )
    assert collision.returncode == 0, collision.stdout + collision.stderr
    assert "5 requirement(s) covered, 0 uncovered" in collision.stdout

    pending = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "review", "--dispatch"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=RFC2119_ENV,
    )
    assert pending.returncode == 1
    for requirement_id in (
        "alpha-feature.1.1",
        "alpha-feature.2.1",
        "beta-feature.1.1",
        "beta-feature.2.1",
        "REQ-900.1.1",
    ):
        assert f"- {requirement_id} (test-quality):" in pending.stdout

    for instruction in sorted((tmp_path / ".2119" / "reviews").glob("*.md")):
        match = re.search(r"npx rfc2119 pass ([^ ]+) --summary", instruction.read_text())
        assert match is not None
        recorded = subprocess.run(
            [
                "npx",
                "--yes",
                "rfc2119@0.7.0",
                "pass",
                match.group(1),
                "--summary",
                "fixture verdict",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=RFC2119_ENV,
        )
        assert recorded.returncode == 0, recorded.stdout + recorded.stderr

    mixed_check = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "check", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=RFC2119_ENV,
    )
    assert mixed_check.returncode == 0, mixed_check.stdout + mixed_check.stderr
    mixed_report = json.loads(mixed_check.stdout)
    assert mixed_report["ok"] is True
    assert mixed_report["requirementCount"] == 5
    assert mixed_report["coveredCount"] == 5


# 2119: 5.1
def test_adoption_waits_for_assigned_legacy_specs() -> None:
    if _adoption_is_already_on_base():
        pytest.skip("adoption is on the base; the migration window has closed")
    assigned = {
        "REQ-035-task-service-authentication.md",
        "REQ-036-verified-configurable-reviewer-models.md",
        "REQ-037-safe-cross-host-task-migration.md",
        "REQ-038-snoozed-task-order.md",
    }
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base_names = set(
        subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", merge_base, "specs"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    assert {f"specs/{name}" for name in assigned} <= base_names
    for name in assigned:
        base_content = subprocess.run(
            ["git", "show", f"{merge_base}:specs/{name}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert (ROOT / "specs" / name).read_text() == base_content


def _adoption_is_already_on_base() -> bool:
    return (
        subprocess.run(
            [
                "git",
                "cat-file",
                "-e",
                "origin/main:specs/file-scoped-requirement-ids.md",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


# 2119: 5.2, 5.3
def test_agent_guidance_records_branch_base_boundary() -> None:
    guidance = GUIDANCE.read_text()
    normalized = " ".join(guidance.split())
    assert (
        "A branch based before this adoption may retain its already assigned legacy ID. "
        "A branch based on this adoption uses a file-scoped ID."
    ) in normalized


# 2119: 6.1, 6.2, 6.3, 6.5, 6.6
def test_agent_guidance_records_lifecycle_assumption_and_failure_policy() -> None:
    guidance = GUIDANCE.read_text()
    normalized = " ".join(guidance.split())
    assert (
        "`rfc2119@0.7.0 contains runnable compiled JavaScript`, `rfc2119@0.7.0 declares no "
        "install-time build`, and its `rfc2119@0.7.0 runtime dependency closure declares no "
        "install-time build`. CI therefore disables dependency lifecycle scripts while fetching "
        "the gate. An install-time build introduced by a future CLI or runtime dependency is a "
        "gate-breaking change while lifecycle scripts remain disabled; lifecycle scripts cannot "
        "be silently enabled to accommodate it. The workflow checks the runner's documented npm "
        "10 major before installing exact npm 10.9.3, so a runner-image npm-major change fails "
        "visibly."
    ) in normalized
