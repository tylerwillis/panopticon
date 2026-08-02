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


def _claude_payload(model: str, *, result: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        },
        "modelUsage": {
            model: {
                "inputTokens": 1,
                "outputTokens": 2,
                "cacheReadInputTokens": 3,
                "cacheCreationInputTokens": 4,
            }
        },
    }
    if result is not None:
        payload["result"] = result
    return payload


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
    assert resolve_reviewers(defaults, {"PANOPTICON_2119_REVIEWER_1": "codex: model-with-spaces "})[
        0
    ] == ReviewerConfig("codex", " model-with-spaces ")


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
        {"modelUsage": {"Claude-Opus-5": {}}},
        {"modelUsage": {"claude-opus-5 ": {}}},
    ],
)
def test_claude_verification_rejects_missing_ambiguous_or_mismatched_identity(
    payload: dict[str, Any],
) -> None:
    # 2119: REQ-034.2.1
    with pytest.raises(ReviewerDispatchError):
        verify_claude_response(json.dumps(payload), requested_model="claude-opus-5")

    assert verify_claude_response(
        json.dumps(_claude_payload("claude-opus-5")),
        requested_model="claude-opus-5",
    ) == ("claude-opus-5", "claude-json:modelUsage")


def test_claude_verification_ignores_distinct_auxiliary_usage() -> None:
    # 2119: REQ-034.2.1
    payload = {
        "usage": {
            "input_tokens": 2,
            "output_tokens": 12,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
        },
        "modelUsage": {
            "claude-haiku-4-5": {
                "inputTokens": 521,
                "outputTokens": 8,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
            "claude-fable-5": {
                "inputTokens": 2,
                "outputTokens": 12,
                "cacheReadInputTokens": 100,
                "cacheCreationInputTokens": 20,
            },
        },
    }
    assert verify_claude_response(json.dumps(payload), "claude-fable-5") == (
        "claude-fable-5",
        "claude-json:modelUsage",
    )
    for counter in (
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
    ):
        requested_mismatch = json.loads(json.dumps(payload))
        requested_mismatch["modelUsage"]["claude-fable-5"][counter] += 1
        requested_mismatch["modelUsage"]["claude-haiku-4-5"] = dict(
            payload["modelUsage"]["claude-fable-5"]
        )
        with pytest.raises(ReviewerDispatchError, match="does not match requested model"):
            verify_claude_response(json.dumps(requested_mismatch), "claude-fable-5")
    ambiguous = json.loads(json.dumps(payload))
    ambiguous["modelUsage"]["claude-haiku-4-5"] = dict(ambiguous["modelUsage"]["claude-fable-5"])
    with pytest.raises(ReviewerDispatchError, match="exactly one responding"):
        verify_claude_response(json.dumps(ambiguous), "claude-fable-5")
    missing_top_level_usage = {"modelUsage": payload["modelUsage"]}
    with pytest.raises(ReviewerDispatchError, match="exactly one responding"):
        verify_claude_response(json.dumps(missing_top_level_usage), "claude-fable-5")
    missing_entry_counter = json.loads(json.dumps(payload))
    del missing_entry_counter["modelUsage"]["claude-fable-5"]["inputTokens"]
    with pytest.raises(ReviewerDispatchError, match="exactly one responding"):
        verify_claude_response(json.dumps(missing_entry_counter), "claude-fable-5")
    vacuous = {"usage": {}, "modelUsage": {"claude-fable-5": {}}}
    with pytest.raises(ReviewerDispatchError, match="exactly one responding"):
        verify_claude_response(json.dumps(vacuous), "claude-fable-5")


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
    duplicate = sessions / "duplicate.jsonl"
    write_rollout(duplicate, "thread-2", ("gpt-5.6-sol",))
    with pytest.raises(ReviewerDispatchError, match="matched 2 persisted rollouts"):
        verify_codex_response(events, tmp_path, requested_model="gpt-5.6-sol")
    duplicate.unlink()
    malformed = sessions / "malformed.jsonl"
    malformed.write_text("{truncated")
    assert verify_codex_response(events, tmp_path, requested_model="gpt-5.6-sol")[0] == (
        "gpt-5.6-sol"
    )
    malformed.unlink()

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
                    json.dumps({"type": "item.completed", "thread_id": "thread-2"}),
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

    matched.write_text(
        "\n".join(
            (
                json.dumps({"type": "session_meta", "payload": {"id": "thread-2"}}),
                json.dumps({"type": "response_item", "payload": {"model": "gpt-5.6-sol"}}),
            )
        )
    )
    with pytest.raises(ReviewerDispatchError):
        verify_codex_response(events, tmp_path, requested_model="gpt-5.6-sol")


def test_codex_dispatch_uses_isolated_command_and_never_publishes_failures(
    tmp_path: Path,
) -> None:
    # 2119: REQ-028.12.1
    # 2119: REQ-034.2.3
    # 2119: REQ-034.3.2
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "review.jsonl"

    def write_rollout(model: str) -> None:
        rollout.write_text(
            "\n".join(
                (
                    json.dumps({"type": "session_meta", "payload": {"id": "thread-1"}}),
                    json.dumps({"type": "turn_context", "payload": {"model": model}}),
                )
            )
        )

    events = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "No findings."},
                }
            ),
        )
    )
    expected_argv = [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "gpt-5.6-sol",
        "--json",
    ]
    posted: list[str] = []
    artifacts: list[str] = []

    write_rollout("gpt-5.6-sol")

    def successful_run(argv: list[str], prompt: str) -> dict[str, Any]:
        assert argv == expected_argv
        assert prompt == (
            "Review the exact final diff `main...abc123` in the current checkout. "
            "The reviewed commit is `abc123`.\n\n" + reviewer_prompt()
        )
        return {"exit_code": 0, "stdout": events}

    dispatch_review(
        ReviewerConfig("codex", "gpt-5.6-sol"),
        prompt=reviewer_prompt(),
        commit="abc123",
        round_number=1,
        run=successful_run,
        post_comment=posted.append,
        publish_artifact=artifacts.append,
        config_root=tmp_path,
        git_head=lambda: "abc123",
    )
    assert len(posted) == len(artifacts) == 1

    for result, rollout_model, expected_kind, expected_detail in (
        (
            {"exit_code": 2, "stderr": "command failed"},
            "gpt-5.6-sol",
            "command",
            "codex reviewer command failed: command failed",
        ),
        (
            {"exit_code": 0, "stdout": events},
            None,
            "identity",
            "Codex thread 'thread-1' matched 0 persisted rollouts; expected exactly one.",
        ),
        (
            {"exit_code": 0, "stdout": events},
            "gpt-5",
            "identity",
            "Codex responding model 'gpt-5' does not match requested model 'gpt-5.6-sol'.",
        ),
        (
            {"exit_code": 0, "stdout": events},
            "ambiguous",
            "identity",
            "Codex rollout must contain exactly one turn_context.payload.model.",
        ),
        (
            {
                "exit_code": 0,
                "stdout": json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            },
            "gpt-5.6-sol",
            "command",
            "Codex reviewer output did not contain a final agent message.",
        ),
    ):
        posted.clear()
        artifacts.clear()
        if rollout_model is None:
            rollout.unlink(missing_ok=True)
        elif rollout_model == "ambiguous":
            write_rollout("gpt-5.6-sol")
            with rollout.open("a") as stream:
                stream.write(
                    "\n"
                    + json.dumps(
                        {
                            "type": "turn_context",
                            "payload": {"model": "gpt-5.6-sol"},
                        }
                    )
                )
        else:
            write_rollout(rollout_model)
        with pytest.raises(ReviewerDispatchError) as raised:
            dispatch_review(
                ReviewerConfig("codex", "gpt-5.6-sol"),
                prompt=reviewer_prompt(),
                commit="abc123",
                round_number=1,
                run=lambda argv, prompt, value=result: value,
                post_comment=posted.append,
                publish_artifact=artifacts.append,
                config_root=tmp_path,
                git_head=lambda: "abc123",
            )
        assert posted == []
        assert artifacts == []
        assert raised.value.kind == expected_kind
        assert raised.value.requested_model == "gpt-5.6-sol"
        assert raised.value.detail == expected_detail
        assert "retry" in raised.value.remediation.lower()
        assert "model" in raised.value.remediation.lower()

    posted.clear()
    artifacts.clear()
    write_rollout("gpt-5.6-sol")
    statuses = iter(("before", "after"))
    with pytest.raises(ReviewerDispatchError, match="changed git status"):
        dispatch_review(
            ReviewerConfig("codex", "gpt-5.6-sol"),
            prompt=reviewer_prompt(),
            commit="abc123",
            round_number=1,
            run=successful_run,
            post_comment=posted.append,
            publish_artifact=artifacts.append,
            config_root=tmp_path,
            git_status=lambda: next(statuses),
            git_head=lambda: "abc123",
        )
    assert posted == []
    assert artifacts == []

    posted.clear()
    artifacts.clear()
    heads = iter(("abc123", "changed"))
    with pytest.raises(ReviewerDispatchError, match="checkout HEAD"):
        dispatch_review(
            ReviewerConfig("codex", "gpt-5.6-sol"),
            prompt=reviewer_prompt(),
            commit="abc123",
            round_number=1,
            run=successful_run,
            post_comment=posted.append,
            publish_artifact=artifacts.append,
            config_root=tmp_path,
            git_head=lambda: next(heads),
        )
    assert posted == []
    assert artifacts == []

    with pytest.raises(ReviewerDispatchError, match="no valid exit code"):
        dispatch_review(
            ReviewerConfig("codex", "gpt-5.6-sol"),
            prompt=reviewer_prompt(),
            commit="abc123",
            round_number=1,
            run=lambda argv, prompt: {},
            post_comment=posted.append,
            config_root=tmp_path,
            git_head=lambda: "abc123",
        )


def test_codex_dispatch_uses_the_last_completed_agent_message(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "review.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"type": "session_meta", "payload": {"id": "thread-1"}}),
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}),
            )
        )
    )
    events = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Progress update."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Final verdict."},
                }
            ),
        )
    )
    posted: list[str] = []
    dispatch_review(
        ReviewerConfig("codex", "gpt-5.6-sol"),
        prompt=reviewer_prompt(),
        commit="abc123",
        round_number=1,
        run=lambda argv, prompt: {"exit_code": 0, "stdout": events},
        post_comment=posted.append,
        config_root=tmp_path,
        git_head=lambda: "abc123",
    )
    assert posted[0].endswith("Final verdict.")
    assert "Progress update." not in posted[0]


def test_dispatch_verifies_before_posting_and_derives_evidence_comment() -> None:
    # 2119: REQ-034.2.1
    # 2119: REQ-034.3.1
    # 2119: REQ-034.3.2
    # 2119: REQ-034.3.3
    # 2119: REQ-034.4.1
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
                _claude_payload(
                    "claude-opus-5",
                    result="# claude-fable-5 — asserted by reviewer\n\nNo findings.",
                )
            ),
        }

    evidence = dispatch_review(
        ReviewerConfig("claude", "claude-opus-5"),
        prompt="Review correctness. Do not edit files.",
        commit="abc123",
        round_number=2,
        run=run,
        post_comment=posted.append,
        git_head=lambda: "abc123",
    )

    assert "main...abc123" in prompt_seen
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


def test_dispatch_rejects_a_reviewed_commit_that_is_not_checkout_head() -> None:
    # 2119: REQ-034.3.3
    called = False

    def forbidden_run(argv: list[str], prompt: str) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"exit_code": 0}

    with pytest.raises(ReviewerDispatchError) as raised:
        dispatch_review(
            ReviewerConfig("claude", "claude-opus-5"),
            prompt=reviewer_prompt(),
            commit="claimed",
            round_number=1,
            run=forbidden_run,
            post_comment=lambda comment: None,
            git_head=lambda: "actual",
        )
    assert called is False
    assert raised.value.requested_model == "claude-opus-5"
    assert "does not match checkout HEAD" in raised.value.detail
    with pytest.raises(ReviewerDispatchError):
        dispatch_review(
            ReviewerConfig("claude", "claude-opus-5"),
            prompt=reviewer_prompt(),
            commit="abc123",
            round_number=1,
            run=forbidden_run,
            post_comment=lambda comment: None,
            git_head=lambda: "abc1234",
        )


def test_dispatch_binds_the_selected_base_ref_into_the_prompt() -> None:
    # 2119: REQ-034.3.3
    seen = ""

    def run(argv: list[str], prompt: str) -> dict[str, Any]:
        nonlocal seen
        seen = prompt
        return {
            "exit_code": 0,
            "stdout": json.dumps(_claude_payload("claude-opus-5", result="No findings.")),
        }

    dispatch_review(
        ReviewerConfig("claude", "claude-opus-5"),
        prompt=reviewer_prompt(),
        commit="abc123",
        round_number=1,
        run=run,
        post_comment=lambda comment: None,
        git_head=lambda: "abc123",
        base_ref="release/next",
    )
    assert "`release/next...abc123`" in seen


def test_generated_reviewer_prompt_contains_no_model_identity_or_heading_instruction() -> None:
    # 2119: REQ-028.12.1
    prompt = reviewer_prompt()
    assert prompt == (
        "Review the supplied final diff for correctness, simplicity, scope, and spec/test "
        "honesty. Do not edit files. Return only the substantive review body with must-fix "
        "findings, suggestions, and a verdict."
    )
    lowered = prompt.lower()
    for forbidden in ("fable", "opus", "sol", "gpt-", "your model", "model heading"):
        assert forbidden not in lowered
    assert "Do not edit files" in prompt


@pytest.mark.parametrize("harness, model", [("claude", "claude-opus-5"), ("codex", "gpt-5.6-sol")])
@pytest.mark.parametrize("exit_code", [1, 2, 127])
def test_every_nonzero_reviewer_exit_is_a_typed_unpublished_failure(
    harness: str, model: str, exit_code: int
) -> None:
    # 2119: REQ-034.2.3
    posted: list[str] = []
    artifacts: list[str] = []
    with pytest.raises(ReviewerDispatchError) as raised:
        dispatch_review(
            ReviewerConfig(harness, model),
            prompt=reviewer_prompt(),
            commit="abc123",
            round_number=1,
            run=lambda argv, prompt: {"exit_code": exit_code, "stderr": "failed"},
            post_comment=posted.append,
            publish_artifact=artifacts.append,
            git_head=lambda: "abc123",
        )
    assert raised.value.kind == "command"
    assert raised.value.requested_model == model
    assert raised.value.detail == f"{harness} reviewer command failed: failed"
    assert "retry" in raised.value.remediation.lower()
    assert "model" in raised.value.remediation.lower()
    assert posted == []
    assert artifacts == []


@pytest.mark.parametrize(
    "result, expected_kind, expected_detail, expected_remediation",
    [
        (
            {"exit_code": 1, "stderr": "request failed"},
            "command",
            "claude reviewer command failed: request failed",
            "Choose an available reviewer model and retry; no review was recorded.",
        ),
        (
            {"exit_code": 0, "stdout": "not JSON"},
            "identity",
            "Claude reviewer returned invalid JSON identity evidence.",
            "Retry with a canonical model id or configure a model whose identity the CLI exposes.",
        ),
        (
            {"exit_code": 0, "stdout": json.dumps({"result": "No findings."})},
            "identity",
            "Claude identity evidence must contain modelUsage entries.",
            "Retry with a canonical model id or configure a model whose identity the CLI exposes.",
        ),
        (
            {
                "exit_code": 0,
                "stdout": json.dumps(
                    {
                        "result": "No findings.",
                        "modelUsage": {"claude-opus-5": {}, "claude-fable-5": {}},
                    }
                ),
            },
            "identity",
            "Claude identity evidence must identify exactly one responding modelUsage entry.",
            "Retry with a canonical model id or configure a model whose identity the CLI exposes.",
        ),
        (
            {
                "exit_code": 0,
                "stdout": json.dumps(_claude_payload("claude-fable-5", result="No findings.")),
            },
            "identity",
            "Claude responding model 'claude-fable-5' does not match requested model "
            "'claude-opus-5'.",
            "Retry with a canonical model id or configure a model whose identity the CLI exposes.",
        ),
    ],
)
def test_failed_command_or_identity_never_posts_review(
    result: dict[str, Any],
    expected_kind: str,
    expected_detail: str,
    expected_remediation: str,
) -> None:
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
            git_head=lambda: "abc123",
        )
    assert posted == []
    assert artifacts == []
    assert raised.value.kind == expected_kind
    assert raised.value.requested_model == "claude-opus-5"
    assert raised.value.detail == expected_detail
    assert raised.value.remediation == expected_remediation


@pytest.mark.parametrize("exit_code", [1, 2, 127])
@pytest.mark.parametrize(
    "failure",
    [
        "usage limit reached",
        "USAGE LIMIT REACHED",
        "UsAgE LiMiT reached",
        "model unavailable",
        "UNAVAILABLE",
        "UnAvAiLaBlE",
    ],
)
@pytest.mark.parametrize(
    "harness, model",
    [("claude", "claude-opus-5"), ("codex", "gpt-5.6-sol")],
)
def test_dispatch_failure_is_not_a_zero_findings_review(
    failure: str, exit_code: int, harness: str, model: str
) -> None:
    # 2119: REQ-034.3.2
    posted: list[str] = []

    def fail_run(argv: list[str], prompt: str) -> dict[str, Any]:
        return {"exit_code": exit_code, "stderr": failure}

    with pytest.raises(ReviewerDispatchError) as raised:
        dispatch_review(
            ReviewerConfig(harness, model),
            prompt="Review without editing.",
            commit="abc123",
            round_number=1,
            run=fail_run,
            post_comment=posted.append,
            git_head=lambda: "abc123",
        )
    assert raised.value.kind == "availability"
    assert posted == []


def test_availability_classification_requires_nonzero_exit_and_stderr() -> None:
    # 2119: REQ-034.3.2
    posted: list[str] = []
    dispatch_review(
        ReviewerConfig("claude", "claude-opus-5"),
        prompt=reviewer_prompt(),
        commit="abc123",
        round_number=1,
        run=lambda argv, prompt: {
            "exit_code": 0,
            "stderr": "usage limit reached",
            "stdout": json.dumps(_claude_payload("claude-opus-5", result="No findings.")),
        },
        post_comment=posted.append,
        git_head=lambda: "abc123",
    )
    assert len(posted) == 1

    with pytest.raises(ReviewerDispatchError) as raised:
        dispatch_review(
            ReviewerConfig("claude", "claude-opus-5"),
            prompt=reviewer_prompt(),
            commit="abc123",
            round_number=1,
            run=lambda argv, prompt: {
                "exit_code": 1,
                "stdout": "model unavailable",
                "stderr": "request failed",
            },
            post_comment=posted.append,
            git_head=lambda: "abc123",
        )
    assert raised.value.kind == "command"
    assert len(posted) == 1


def test_gate_requires_two_verified_final_commit_reviews_for_every_round() -> None:
    # 2119: REQ-034.4.1
    first = ReviewEvidence.from_verified_identity(
        ReviewerConfig("claude", "claude-opus-5"),
        verify_claude_response(json.dumps(_claude_payload("claude-opus-5")), "claude-opus-5"),
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
    reviewers = (
        ReviewerConfig("claude", "claude-opus-5"),
        ReviewerConfig("codex", "gpt-5.6-sol"),
    )
    assert (
        validate_review_gate(comments, reviewers=reviewers, commit="final", round_number=2) is None
    )

    invalid_sets = (
        comments[:1],
        (*comments, comments[0]),
        (comments[0], comments[0]),
        tuple(reversed(comments)),
        (
            comments[0].replace(
                "claude-json:modelUsage", "codex-rollout:turn_context.payload.model"
            ),
            comments[1],
        ),
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
            validate_review_gate(evidence, reviewers=reviewers, commit="final", round_number=2)
        effects: list[str] = []
        with pytest.raises(ReviewerDispatchError):
            complete_review_stage(
                evidence,
                reviewers=reviewers,
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
