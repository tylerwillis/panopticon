"""Executable contract for verified reviewer dispatch inside task containers."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from panopticon.container.reviewers import (
    ReviewerConfig,
    ReviewerDispatchError,
    ReviewEvidence,
    complete_review_stage,
    dispatch_review,
    dispatch_reviews,
    parse_review_comment,
    render_review_comment,
    resolve_reviewers,
    reviewer_prompt,
    validate_review_gate,
    verify_claude_response,
    verify_codex_response,
)
from panopticon.workflows.discovery import discover_workflows


def _workflows() -> dict[str, Any]:
    return discover_workflows(_home_workflows=Path("/nonexistent"))


def test_workflows_define_two_opaque_reviewer_pairs_and_sol_variant_uses_same_resolver() -> None:
    # 2119: REQ-034.1.1
    workflows = _workflows()
    expected = {
        "2119-human-spec": (
            ReviewerConfig("claude", "claude-fable-5"),
            ReviewerConfig("codex", "gpt-5.6-sol"),
        ),
        "2119-auto-spec": (
            ReviewerConfig("claude", "claude-fable-5"),
            ReviewerConfig("codex", "gpt-5.6-sol"),
        ),
        "2119-auto-sol": (
            ReviewerConfig("codex", "gpt-5.6-sol"),
            ReviewerConfig("codex", "gpt-5.6-sol"),
        ),
    }

    builtins = {name: workflow for name, workflow in workflows.items() if name.startswith("2119-")}
    assert set(builtins) == set(expected)
    for name, workflow in builtins.items():
        defaults = expected[name]
        assert workflow.reviewers == defaults
        assert len(workflow.reviewers) == 2
        assert all(isinstance(item, ReviewerConfig) for item in workflow.reviewers)
        assert resolve_reviewers(defaults, {}) == defaults
        assert resolve_reviewers(
            defaults,
            {"PANOPTICON_2119_REVIEWER_2": "claude:claude-opus-5"},
        ) == (defaults[0], ReviewerConfig("claude", "claude-opus-5"))
    opaque = ReviewerConfig("codex", "provider/model:effort:future")
    assert resolve_reviewers((opaque, opaque), {}) == (opaque, opaque)


def test_repo_overrides_preserve_slots_and_split_only_the_first_colon() -> None:
    # 2119: REQ-034.1.1
    defaults = (
        ReviewerConfig("claude", "claude-fable-5"),
        ReviewerConfig("codex", "gpt-5.6-sol"),
    )

    assert resolve_reviewers(
        defaults,
        {
            "PANOPTICON_2119_REVIEWER_1": "codex:gpt-5.6-sol:high",
            "PANOPTICON_2119_REVIEWER_2": "claude:claude-opus-5",
        },
    ) == (
        ReviewerConfig("codex", "gpt-5.6-sol:high"),
        ReviewerConfig("claude", "claude-opus-5"),
    )
    assert resolve_reviewers(defaults, {"PANOPTICON_2119_REVIEWER_1": "claude:claude-opus-5"}) == (
        ReviewerConfig("claude", "claude-opus-5"),
        defaults[1],
    )
    assert resolve_reviewers(defaults, {"PANOPTICON_2119_REVIEWER_2": "claude:claude-opus-5"}) == (
        defaults[0],
        ReviewerConfig("claude", "claude-opus-5"),
    )


@pytest.mark.parametrize(
    "value, message",
    [
        ("claude", "malformed"),
        (":claude-opus-5", "missing harness"),
        ("claude:", "missing model"),
        ("pi:anthropic/claude-opus-5", "unsupported harness"),
    ],
)
def test_invalid_override_fails_before_dispatch(
    value: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-034.1.1
    called = False

    def forbidden_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("an invalid configuration reached an LLM")

    for slot in (1, 2):
        with pytest.raises(ReviewerDispatchError, match=message):
            dispatch_reviews(
                (ReviewerConfig("claude", "claude-fable-5"),) * 2,
                {f"PANOPTICON_2119_REVIEWER_{slot}": value},
                prompt="Review without edits.",
                commit="abc123",
                round_number=1,
                run=forbidden_run,
                post_comment=lambda comment: None,
            )
    assert called is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"modelUsage": {}},
        {"modelUsage": {"claude-opus-5": {}, "claude-fable-5": {}}},
        {"modelUsage": {"claude-fable-5": {}}},
    ],
)
def test_claude_verification_rejects_missing_ambiguous_or_mismatched_identity(
    payload: dict[str, Any],
) -> None:
    # 2119: REQ-034.2.1
    with pytest.raises(ReviewerDispatchError):
        verify_claude_response(json.dumps(payload), requested_model="claude-opus-5")

    assert verify_claude_response(
        json.dumps({"modelUsage": {"claude-opus-5": {"inputTokens": 1}}}),
        requested_model="claude-opus-5",
    ) == ("claude-opus-5", "claude-json:modelUsage")


def test_codex_verification_correlates_thread_to_exact_rollout_model(tmp_path: Path) -> None:
    # 2119: REQ-034.2.2
    events = json.dumps({"type": "thread.started", "thread_id": "thread-2"})
    sessions = tmp_path / "sessions" / "2026" / "08" / "02"
    sessions.mkdir(parents=True)

    def write_rollout(path: Path, thread_id: str, models: tuple[str, ...]) -> None:
        records = [{"type": "session_meta", "payload": {"id": thread_id}}]
        records.extend({"type": "turn_context", "payload": {"model": model}} for model in models)
        path.write_text("\n".join(json.dumps(record) for record in records))

    write_rollout(sessions / "other.jsonl", "thread-1", ("gpt-5.6-sol",))
    matched = sessions / "matched.jsonl"
    write_rollout(matched, "thread-2", ("gpt-5.6-sol",))
    assert verify_codex_response(events, tmp_path, requested_model="gpt-5.6-sol") == (
        "gpt-5.6-sol",
        "codex-rollout:turn_context.payload.model",
    )

    for bad_events, models in (
        ("", ("gpt-5.6-sol",)),
        (
            "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps({"type": "thread.started", "thread_id": "thread-2"}),
                )
            ),
            ("gpt-5.6-sol",),
        ),
        (
            "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "thread-2"}),
                    json.dumps({"type": "other", "thread_id": "thread-1"}),
                )
            ),
            ("gpt-5.6-sol",),
        ),
        (
            "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "thread-2"}),
                    json.dumps({"type": "thread.started", "thread_id": "thread-2"}),
                )
            ),
            ("gpt-5.6-sol",),
        ),
        (events, ()),
        (events, ("gpt-5",)),
        (events, ("gpt-5.6-sol", "gpt-5.6-sol")),
    ):
        write_rollout(matched, "thread-2", models)
        with pytest.raises(ReviewerDispatchError):
            verify_codex_response(bad_events, tmp_path, requested_model="gpt-5.6-sol")

    write_rollout(matched, "different-thread", ("gpt-5.6-sol",))
    with pytest.raises(ReviewerDispatchError):
        verify_codex_response(events, tmp_path, requested_model="gpt-5.6-sol")


def test_dispatch_verifies_before_posting_and_derives_evidence_comment() -> None:
    # 2119: REQ-034.2.1
    # 2119: REQ-034.3.1
    # 2119: REQ-034.3.2
    posted: list[str] = []
    prompt_seen = ""

    def run(argv: list[str], prompt: str) -> dict[str, Any]:
        nonlocal prompt_seen
        prompt_seen = prompt
        assert argv == [
            "claude",
            "--print",
            "--output-format",
            "json",
            "--model",
            "claude-opus-5",
        ]
        return {
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "result": "# claude-fable-5 — asserted by reviewer\n\nNo findings.",
                    "modelUsage": {"claude-opus-5": {}},
                }
            ),
        }

    evidence = dispatch_review(
        ReviewerConfig("claude", "claude-opus-5"),
        prompt="Review correctness. Do not edit files.",
        commit="abc123",
        round_number=2,
        run=run,
        post_comment=posted.append,
    )

    assert "state your model" not in prompt_seen.lower()
    assert "model-labeled heading" not in prompt_seen.lower()
    assert evidence == ReviewEvidence(
        harness="claude",
        requested_model="claude-opus-5",
        verified_model="claude-opus-5",
        verification_source="claude-json:modelUsage",
        commit="abc123",
        round_number=2,
    )
    expected_comment = """# claude-opus-5 — Round 2 (verified)

- Harness: `claude`
- Requested model: `claude-opus-5`
- Verified responding model: `claude-opus-5`
- Verification source: `claude-json:modelUsage`
- Reviewed commit: `abc123`
- Review round: `2`

---

# claude-fable-5 — asserted by reviewer

No findings."""
    assert posted == [expected_comment]
    assert (
        render_review_comment(evidence, "# claude-fable-5 — asserted by reviewer\n\nNo findings.")
        == expected_comment
    )
    mismatched = replace(evidence, requested_model="requested-alias", verified_model="observed-id")
    mismatched_comment = render_review_comment(mismatched, "Body")
    assert mismatched_comment.startswith("# observed-id — Round 2 (verified)\n")
    assert "Requested model: `requested-alias`" in mismatched_comment
    assert "Verified responding model: `observed-id`" in mismatched_comment
    assert parse_review_comment(expected_comment) == (
        evidence,
        "# claude-fable-5 — asserted by reviewer\n\nNo findings.",
    )
    for required_line in expected_comment.splitlines()[2:8]:
        with pytest.raises(ReviewerDispatchError):
            parse_review_comment(expected_comment.replace(required_line + "\n", "", 1))
    with pytest.raises(ReviewerDispatchError):
        parse_review_comment(expected_comment.split("\n---\n", 1)[0])


def test_generated_reviewer_prompt_contains_no_model_identity_or_heading_instruction() -> None:
    prompt = reviewer_prompt()
    lowered = prompt.lower()
    for forbidden in ("fable", "opus", "sol", "gpt-", "your model", "model heading"):
        assert forbidden not in lowered
    assert "Do not edit files" in prompt


@pytest.mark.parametrize(
    "result",
    [
        {"exit_code": 1, "stderr": "request failed"},
        {"exit_code": 0, "stdout": "not JSON"},
        {"exit_code": 0, "stdout": json.dumps({"result": "No findings."})},
        {
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "result": "No findings.",
                    "modelUsage": {"claude-opus-5": {}, "claude-fable-5": {}},
                }
            ),
        },
        {
            "exit_code": 0,
            "stdout": json.dumps({"result": "No findings.", "modelUsage": {"claude-fable-5": {}}}),
        },
    ],
)
def test_failed_command_or_identity_never_posts_review(result: dict[str, Any]) -> None:
    # 2119: REQ-034.2.1
    # 2119: REQ-034.2.3
    # 2119: REQ-034.3.2
    posted: list[str] = []
    artifacts: list[str] = []
    with pytest.raises(ReviewerDispatchError) as raised:
        dispatch_review(
            ReviewerConfig("claude", "claude-opus-5"),
            prompt=reviewer_prompt(),
            commit="abc123",
            round_number=1,
            run=lambda argv, prompt: result,
            post_comment=posted.append,
            publish_artifact=artifacts.append,
        )
    assert posted == []
    assert artifacts == []
    assert raised.value.kind in {"command", "identity", "availability"}
    assert raised.value.requested_model == "claude-opus-5"
    assert raised.value.detail
    assert raised.value.remediation


@pytest.mark.parametrize(
    "failure", ["usage limit reached", "USAGE LIMIT REACHED", "model unavailable", "UNAVAILABLE"]
)
def test_dispatch_failure_is_not_a_zero_findings_review(failure: str) -> None:
    # 2119: REQ-034.3.2
    posted: list[str] = []

    def fail_run(argv: list[str], prompt: str) -> dict[str, Any]:
        return {"exit_code": 1, "stderr": failure}

    with pytest.raises(ReviewerDispatchError) as raised:
        dispatch_review(
            ReviewerConfig("claude", "claude-opus-5"),
            prompt="Review without editing.",
            commit="abc123",
            round_number=1,
            run=fail_run,
            post_comment=posted.append,
        )
    assert raised.value.kind == "availability"
    assert posted == []


def test_gate_requires_two_verified_final_commit_reviews_for_every_round() -> None:
    # 2119: REQ-034.4.1
    first = ReviewEvidence.from_verified_identity(
        ReviewerConfig("claude", "claude-opus-5"),
        verify_claude_response(json.dumps({"modelUsage": {"claude-opus-5": {}}}), "claude-opus-5"),
        commit="final",
        round_number=2,
    )
    second = ReviewEvidence.from_verified_identity(
        ReviewerConfig("codex", "gpt-5.6-sol"),
        ("gpt-5.6-sol", "codex-rollout:turn_context.payload.model"),
        commit="final",
        round_number=2,
    )
    comments = (render_review_comment(first, "A"), render_review_comment(second, "B"))
    assert validate_review_gate(comments, commit="final", round_number=2) is None

    invalid_sets = (
        comments[:1],
        (*comments, comments[0]),
        (
            comments[0],
            comments[1].replace("gpt-5.6-sol`\n- Verification", "other`\n- Verification", 1),
        ),
        (comments[0], comments[1].replace("codex-rollout:turn_context.payload.model", "asserted")),
        (comments[0], render_review_comment(replace(second, commit="stale"), "B")),
        (comments[0], render_review_comment(replace(second, round_number=1), "B")),
        (comments[0], "not an evidence-bearing review comment"),
    )

    def assert_rejected(evidence: tuple[str, ...]) -> None:
        with pytest.raises(ReviewerDispatchError):
            validate_review_gate(evidence, commit="final", round_number=2)
        effects: list[str] = []
        with pytest.raises(ReviewerDispatchError):
            complete_review_stage(
                evidence,
                commit="final",
                round_number=2,
                triage=lambda: effects.append("triage"),
                resolve_responsibility=lambda: effects.append("resolved"),
            )
        assert effects == []

    for evidence in invalid_sets:
        assert_rejected(evidence)

    for bad_index in (0, 1):
        bad = list(comments)
        requested = "claude-opus-5" if bad_index == 0 else "gpt-5.6-sol"
        bad[bad_index] = bad[bad_index].replace(
            f"Requested model: `{requested}`", "Requested model: `other`"
        )
        assert_rejected(tuple(bad))

    for bad_index in (0, 1):
        for old, new in (
            (
                "claude-json:modelUsage"
                if bad_index == 0
                else "codex-rollout:turn_context.payload.model",
                "asserted",
            ),
            ("Review round: `2`", "Review round: `1`"),
        ):
            bad = list(comments)
            bad[bad_index] = bad[bad_index].replace(old, new)
            assert_rejected(tuple(bad))

    for bad_index in (0, 1):
        bad = list(comments)
        bad[bad_index] = bad[bad_index].replace(
            "Reviewed commit: `final`", "Reviewed commit: `stale`"
        )
        assert_rejected(tuple(bad))


def test_dual_review_skills_forbid_counting_and_resolving_without_verified_comments() -> None:
    for workflow in _workflows().values():
        if not workflow.name.startswith("2119-"):
            continue
        skill = next(item for item in workflow.skills() if item.name.startswith("dual-review"))
        text = " ".join(skill.instructions.split())
        assert "Do not count an unverified report in triage." in text
        assert "Do not resolve the reviews-recorded responsibility" in text
        assert (
            "both PR comments carry valid verification evidence for the final reviewed commit"
            in text
        )
        assert (
            "Apply these same dispatch, verification, comment, and failure rules to re-review rounds."
            in text
        )


def test_verified_dispatch_plumbing_lives_only_in_the_container_package() -> None:
    root = Path(__file__).parents[2] / "src" / "panopticon"
    dispatch_module = root / "container" / "reviewers.py"
    assert dispatch_module.is_file()
    forbidden_dispatch_tokens = (
        "PANOPTICON_2119_REVIEWER_",
        "claude --print --output-format json",
        "turn_context.payload.model",
        "dispatch_review(",
    )
    for package in ("core", "taskservice", "sessionservice", "terminal", "workflows"):
        for source in (root / package).glob("**/*.py"):
            text = source.read_text()
            assert "from panopticon.container.reviewers import" not in text
            if package != "workflows":
                assert all(token not in text for token in forbidden_dispatch_tokens)
