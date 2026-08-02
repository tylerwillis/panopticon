from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "2119.yml"
GUIDANCE = ROOT / "AGENTS.md"


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
    npm_pin = re.search(
        r"^\s*- run: npm install --global npm@10\.9\.3$", workflow, re.MULTILINE
    )
    versions = re.search(
        r"^\s+node --version\n\s+npm --version$", workflow, re.MULTILINE
    )
    major_guard = re.search(
        r'^\s+test "\$\(npm --version \| cut --delimiter=\. --fields=1\)" = "10" \|\| exit 1$',
        workflow,
        re.MULTILINE,
    )
    assert gate is not None
    assert npm_pin is not None and npm_pin.start() < gate.start()
    assert versions is not None and versions.start() < gate.start()
    assert major_guard is not None and major_guard.start() < npm_pin.start()


# 2119: 1.4, 1.5
def test_no_older_rfc2119_pin_remains_in_active_instructions() -> None:
    old_pin = re.compile(r"rfc2119@(?:0\.[0-6](?:\.\d+)?)")
    candidates = [GUIDANCE, *sorted((ROOT / ".github" / "workflows").glob("*"))]
    for path in candidates:
        if path.is_file():
            assert old_pin.search(path.read_text()) is None, path


# 2119: 2.1, 2.2, 2.3, 2.4
def test_adoption_diff_does_not_rewrite_legacy_identity_or_verdict_files() -> None:
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
    assert "lowercase kebab-case" in guidance
    assert "bare numbered section headings" in guidance
    assert "within that file" in guidance
    assert "coexist indefinitely" in guidance
    assert "# 2119-spec: repo-picker" in guidance
    assert "# 2119: 3.2" in guidance
    assert "# 2119: repo-picker.3.2" in guidance
    assert "both resolve to repo-picker.3.2" in guidance
    assert "# 2119: REQ-001.2.3" in guidance
    assert "renam" in guidance and "invalidat" in guidance
    for command in ("lint", "review --dispatch", "check"):
        assert f"npx --yes rfc2119@0.7.0 {command}" in guidance


# 2119: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4
def test_070_resolves_both_grammars_and_distinct_file_namespaces(tmp_path: Path) -> None:
    verdicts = ROOT / ".2119" / "verdicts"
    assert list(verdicts.glob("REQ-*.json"))
    assert list(verdicts.glob("file-scoped-requirement-ids.*.json"))
    result = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "check: all enforced requirements are covered and passing" in result.stdout

    (tmp_path / "specs").mkdir()
    (tmp_path / "tests").mkdir()
    for stem in ("alpha-feature", "beta-feature"):
        (tmp_path / "specs" / f"{stem}.md").write_text(
            f"# {stem}\n\n## Overview\n\nTest.\n\n## Requirements\n\n"
            "### 1: Behavior\n\n1. It MUST work.\n"
        )
    (tmp_path / "specs" / "REQ-900-legacy.md").write_text(
        "# REQ-900: Legacy\n\n## Overview\n\nTest.\n\n## Requirements\n\n"
        "### REQ-900.1: Behavior\n\n1. It MUST work.\n"
    )
    (tmp_path / "tests" / "coverage.py").write_text(
        "# 2119: alpha-feature.1.1, beta-feature.1.1, REQ-900.1.1\n"
    )
    discovery = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "lint"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert discovery.returncode == 0, discovery.stdout + discovery.stderr
    assert "3 spec file(s) clean" in discovery.stdout
    collision = subprocess.run(
        ["npx", "--yes", "rfc2119@0.7.0", "cover"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert collision.returncode == 0, collision.stdout + collision.stderr
    assert "3 requirement(s) covered, 0 uncovered" in collision.stdout


# 2119: 5.1
def test_adoption_waits_for_assigned_legacy_specs() -> None:
    assigned = {
        "REQ-035-taskservice-auth.md",
        "REQ-036-verified-reviewer-models.md",
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


# 2119: 5.2, 5.3
def test_agent_guidance_records_branch_base_boundary() -> None:
    guidance = GUIDANCE.read_text()
    assert "A branch based before this adoption may retain its already assigned legacy ID" in guidance
    assert "A branch based on this adoption uses a file-scoped ID" in guidance


# 2119: 6.1, 6.2, 6.3, 6.5, 6.6
def test_agent_guidance_records_lifecycle_assumption_and_failure_policy() -> None:
    guidance = GUIDANCE.read_text()
    assert "rfc2119@0.7.0 contains runnable compiled JavaScript" in guidance
    assert "rfc2119@0.7.0 declares no install-time build" in guidance
    assert "rfc2119@0.7.0 runtime dependency closure declares no install-time build" in guidance
    assert "future CLI or runtime dependency" in guidance
    assert "gate-breaking change" in guidance
    assert "lifecycle scripts" in guidance
    assert "silently enable" in guidance
