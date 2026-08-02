"""Verified reviewer dispatch for RFC 2119 task containers.

This module is intentionally container-owned: reviewer subprocesses are LLM calls and must never
move into the deterministic control plane.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from panopticon.harnesses.base import ReviewerConfig

SUPPORTED_HARNESSES = frozenset({"claude", "codex"})
CLAUDE_SOURCE = "claude-json:modelUsage"
CODEX_SOURCE = "codex-rollout:turn_context.payload.model"
SUPPORTED_VERIFICATION_SOURCES = frozenset({CLAUDE_SOURCE, CODEX_SOURCE})
REVIEWER_TIMEOUT_SECONDS = 600

CommandResult: TypeAlias = Mapping[str, Any]
RunReviewer: TypeAlias = Callable[[list[str], str], CommandResult]
Publish: TypeAlias = Callable[[str], None]
GitStatus: TypeAlias = Callable[[], str]
GitHead: TypeAlias = Callable[[], str]
VerifiedIdentity: TypeAlias = tuple[str, str]


@dataclass(frozen=True)
class ReviewEvidence:
    """Machine-verification evidence attached to one completed review."""

    harness: str
    requested_model: str
    verified_model: str
    verification_source: str
    commit: str
    round_number: int

    @classmethod
    def from_verified_identity(
        cls,
        config: ReviewerConfig,
        identity: VerifiedIdentity,
        *,
        commit: str,
        round_number: int,
    ) -> ReviewEvidence:
        model, source = identity
        return cls(config.harness, config.model, model, source, commit, round_number)


class ReviewerDispatchError(RuntimeError):
    """Typed, actionable reviewer failure safe to surface to an operator."""

    def __init__(
        self,
        detail: str,
        *,
        kind: str = "configuration",
        requested_model: str = "unknown",
        remediation: str = "Correct the reviewer configuration or choose an available model.",
    ) -> None:
        self.kind = kind
        self.requested_model = requested_model
        self.detail = detail
        self.remediation = remediation
        super().__init__(f"{detail} Remediation: {remediation}")


def _invalid_config(detail: str, model: str = "unknown") -> ReviewerDispatchError:
    return ReviewerDispatchError(detail, requested_model=model)


def _parse_reviewer(value: str) -> ReviewerConfig:
    if ":" not in value:
        raise _invalid_config(f"malformed reviewer pair {value!r}; expected <harness>:<model>")
    harness, model = value.split(":", 1)
    if not harness:
        raise _invalid_config("reviewer pair has a missing harness", model)
    if not model:
        raise _invalid_config("reviewer pair has a missing model")
    if harness not in SUPPORTED_HARNESSES:
        raise _invalid_config(f"unsupported harness {harness!r}; choose claude or codex", model)
    return ReviewerConfig(harness, model)


def resolve_reviewers(
    defaults: Sequence[ReviewerConfig], environ: Mapping[str, str]
) -> tuple[ReviewerConfig, ReviewerConfig]:
    """Resolve two reviewer slots, validating both before any dispatch can begin."""

    if len(defaults) != 2:
        raise _invalid_config("reviewer configuration requires exactly two defaults")
    resolved = []
    for index, default in enumerate(defaults, 1):
        if default.harness not in SUPPORTED_HARNESSES or not default.model:
            raise _invalid_config(f"invalid default reviewer {index}", default.model)
        override = environ.get(f"PANOPTICON_2119_REVIEWER_{index}")
        resolved.append(_parse_reviewer(override) if override is not None else default)
    return resolved[0], resolved[1]


def _identity_error(detail: str, requested_model: str) -> ReviewerDispatchError:
    return ReviewerDispatchError(
        detail,
        kind="identity",
        requested_model=requested_model,
        remediation="Retry with a canonical model id or configure a model whose identity the CLI exposes.",
    )


def verify_claude_response(raw_stdout: str, requested_model: str) -> VerifiedIdentity:
    """Verify Claude's supported JSON response identity signal."""

    try:
        payload = json.loads(raw_stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _identity_error(
            "Claude reviewer returned invalid JSON identity evidence.", requested_model
        ) from exc
    usage = payload.get("modelUsage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict) or not usage:
        raise _identity_error(
            "Claude identity evidence must contain modelUsage entries.", requested_model
        )
    totals = payload.get("usage")
    counter_names = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
        "cacheCreationInputTokens": "cache_creation_input_tokens",
    }
    candidates = []
    if isinstance(totals, dict):
        for model, counters in usage.items():
            if isinstance(counters, dict) and all(
                isinstance(counters.get(model_key), int)
                and not isinstance(counters.get(model_key), bool)
                and isinstance(totals.get(total_key), int)
                and not isinstance(totals.get(total_key), bool)
                and counters[model_key] == totals[total_key]
                for model_key, total_key in counter_names.items()
            ):
                candidates.append(model)
    if len(candidates) != 1:
        raise _identity_error(
            "Claude identity evidence must identify exactly one responding modelUsage entry.",
            requested_model,
        )
    observed = candidates[0]
    if observed != requested_model:
        raise _identity_error(
            f"Claude responding model {observed!r} does not match requested model {requested_model!r}.",
            requested_model,
        )
    return observed, CLAUDE_SOURCE


def _json_lines(raw: str, *, requested_model: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in raw.splitlines():
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise TypeError
                records.append(record)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _identity_error(
            "Codex reviewer returned invalid JSONL identity evidence.", requested_model
        ) from exc
    return records


def _rollout_for_thread(
    config_root: Path, thread_id: str, requested_model: str
) -> list[dict[str, Any]]:
    matches: list[list[dict[str, Any]]] = []
    for path in (config_root / "sessions").glob("**/*.jsonl"):
        try:
            records = _json_lines(path.read_text(), requested_model=requested_model)
        except (OSError, UnicodeError, ReviewerDispatchError):
            continue
        session_ids = [
            record.get("payload", {}).get("id")
            for record in records
            if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict)
        ]
        if thread_id in session_ids:
            matches.append(records)
    if len(matches) != 1:
        raise _identity_error(
            f"Codex thread {thread_id!r} matched {len(matches)} persisted rollouts; expected exactly one.",
            requested_model,
        )
    return matches[0]


def verify_codex_response(
    raw_stdout: str, config_root: Path, requested_model: str
) -> VerifiedIdentity:
    """Verify Codex using JSONL thread correlation and its weaker persisted rollout signal."""

    events = _json_lines(raw_stdout, requested_model=requested_model)
    thread_ids = [record["thread_id"] for record in events if "thread_id" in record]
    if len(thread_ids) != 1 or not isinstance(thread_ids[0], str):
        raise _identity_error(
            "Codex JSONL must contain exactly one thread_id before rollout correlation.",
            requested_model,
        )
    rollout = _rollout_for_thread(config_root, thread_ids[0], requested_model)
    models = [
        record.get("payload", {}).get("model")
        for record in rollout
        if record.get("type") == "turn_context" and isinstance(record.get("payload"), dict)
    ]
    if len(models) != 1 or not isinstance(models[0], str):
        raise _identity_error(
            "Codex rollout must contain exactly one turn_context.payload.model.", requested_model
        )
    observed = models[0]
    if observed != requested_model:
        raise _identity_error(
            f"Codex responding model {observed!r} does not match requested model {requested_model!r}.",
            requested_model,
        )
    return observed, CODEX_SOURCE


def reviewer_prompt() -> str:
    """Return identity-neutral review instructions; the dispatcher owns the report heading."""

    return (
        "Review the supplied final diff for correctness, simplicity, scope, and spec/test honesty. "
        "Do not edit files. Return only the substantive review body with must-fix findings, "
        "suggestions, and a verdict."
    )


def _default_run(argv: list[str], prompt: str) -> CommandResult:
    try:
        completed = subprocess.run(
            argv,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=REVIEWER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "stderr": f"reviewer timed out after {REVIEWER_TIMEOUT_SECONDS} seconds: {exc}",
        }
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _git_status() -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReviewerDispatchError(
            f"git status failed: {completed.stderr.strip() or 'no error output'}",
            kind="command",
            remediation="Restore a readable Git checkout and retry the reviewer.",
        )
    return completed.stdout


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReviewerDispatchError(
            f"git rev-parse HEAD failed: {completed.stderr.strip() or 'no error output'}",
            kind="command",
            remediation="Restore a readable Git checkout and retry the reviewer.",
        )
    return completed.stdout.strip()


def _command_failure(config: ReviewerConfig, stderr: str) -> ReviewerDispatchError:
    lowered = stderr.lower()
    kind = "availability" if "usage limit" in lowered or "unavailable" in lowered else "command"
    return ReviewerDispatchError(
        f"{config.harness} reviewer command failed: {stderr.strip() or 'no error output'}",
        kind=kind,
        requested_model=config.model,
        remediation="Choose an available reviewer model and retry; no review was recorded.",
    )


def _codex_body(events: list[dict[str, Any]], requested_model: str) -> str:
    bodies = [
        item.get("item", {}).get("text")
        for item in events
        if item.get("type") == "item.completed"
        and isinstance(item.get("item"), dict)
        and item["item"].get("type") == "agent_message"
    ]
    if not bodies or not isinstance(bodies[-1], str):
        raise ReviewerDispatchError(
            "Codex reviewer output did not contain a final agent message.",
            kind="command",
            requested_model=requested_model,
            remediation="Retry the requested model and inspect its reviewer JSONL output.",
        )
    return bodies[-1]


def dispatch_review(
    config: ReviewerConfig,
    *,
    prompt: str,
    commit: str,
    round_number: int,
    run: RunReviewer = _default_run,
    post_comment: Publish,
    publish_artifact: Publish | None = None,
    config_root: Path | None = None,
    git_status: GitStatus = _git_status,
    git_head: GitHead = _git_head,
    base_ref: str = "main",
) -> ReviewEvidence:
    """Run, verify, then publish one review. Verification always precedes side effects."""

    if config.harness == "claude":
        argv = ["claude", "--print", "--output-format", "json", "--model", config.model]
    elif config.harness == "codex":
        argv = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            config.model,
            "--json",
        ]
    else:
        raise _invalid_config(f"unsupported harness {config.harness!r}", config.model)
    actual_head = git_head()
    if actual_head != commit:
        raise ReviewerDispatchError(
            f"Requested reviewed commit {commit!r} does not match checkout HEAD {actual_head!r}.",
            kind="configuration",
            requested_model=config.model,
            remediation="Check out the intended final commit and retry the reviewer.",
        )
    bound_prompt = (
        f"Review the exact final diff `{base_ref}...{commit}` in the current checkout. "
        f"The reviewed commit is `{commit}`.\n\n{prompt}"
    )
    status_before = git_status()
    result = run(argv, bound_prompt)
    status_after = git_status()
    head_after = git_head()
    if status_after != status_before or head_after != commit:
        raise ReviewerDispatchError(
            "Reviewer changed git status or checkout HEAD; refusing to record its output.",
            kind="command",
            requested_model=config.model,
            remediation="Revert the reviewer-authored changes and retry with a read-only prompt.",
        )
    if "exit_code" not in result or not isinstance(result["exit_code"], int):
        raise ReviewerDispatchError(
            "Reviewer command returned no valid exit code.",
            kind="command",
            requested_model=config.model,
            remediation="Retry the requested model and inspect the command runner result.",
        )
    stderr = str(result.get("stderr", ""))
    if result["exit_code"] != 0:
        raise _command_failure(config, stderr)
    stdout = str(result.get("stdout", ""))
    if config.harness == "claude":
        identity = verify_claude_response(stdout, config.model)
        payload = json.loads(stdout)
        body = payload.get("result")
        if not isinstance(body, str):
            raise ReviewerDispatchError(
                "Claude reviewer JSON has no textual result.",
                kind="command",
                requested_model=config.model,
                remediation="Retry and inspect the Claude JSON response.",
            )
    else:
        root = config_root or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        identity = verify_codex_response(stdout, root, config.model)
        body = _codex_body(_json_lines(stdout, requested_model=config.model), config.model)
    evidence = ReviewEvidence.from_verified_identity(
        config, identity, commit=commit, round_number=round_number
    )
    comment = render_review_comment(evidence, body)
    parse_review_comment(comment)
    post_comment(comment)
    if publish_artifact is not None:
        publish_artifact(comment)
    return evidence


def dispatch_reviews(
    defaults: Sequence[ReviewerConfig],
    environ: Mapping[str, str],
    **kwargs: Any,
) -> tuple[ReviewEvidence, ReviewEvidence]:
    """Resolve both slots before dispatching either reviewer."""

    first, second = resolve_reviewers(defaults, environ)
    return dispatch_review(first, **kwargs), dispatch_review(second, **kwargs)


def render_review_comment(evidence: ReviewEvidence, body: str) -> str:
    """Render durable evidence, deriving the heading only from observed identity."""

    return (
        f"# {evidence.verified_model} — Round {evidence.round_number} (verified)\n\n"
        f"- Harness: `{evidence.harness}`\n"
        f"- Requested model: `{evidence.requested_model}`\n"
        f"- Verified responding model: `{evidence.verified_model}`\n"
        f"- Verification source: `{evidence.verification_source}`\n"
        f"- Reviewed commit: `{evidence.commit}`\n"
        f"- Review round: `{evidence.round_number}`\n\n"
        f"---\n\n{body}"
    )


_COMMENT = re.compile(
    r"\A# (?P<heading>.+) — Round (?P<heading_round>\d+) \(verified\)\n\n"
    r"- Harness: `(?P<harness>[^`]+)`\n"
    r"- Requested model: `(?P<requested>[^`]+)`\n"
    r"- Verified responding model: `(?P<verified>[^`]+)`\n"
    r"- Verification source: `(?P<source>[^`]+)`\n"
    r"- Reviewed commit: `(?P<commit>[^`]+)`\n"
    r"- Review round: `(?P<round>\d+)`\n\n---\n\n(?P<body>.+)\Z",
    re.DOTALL,
)


def parse_review_comment(comment: str) -> tuple[ReviewEvidence, str]:
    """Parse the strict evidence comment format used by the review gate."""

    match = _COMMENT.fullmatch(comment)
    if match is None:
        raise _identity_error("Malformed or non-evidence-bearing review comment.", "unknown")
    values = match.groupdict()
    round_number = int(values["round"])
    if values["heading"] != values["verified"] or int(values["heading_round"]) != round_number:
        raise _identity_error(
            "Review heading does not match its verification evidence.", values["requested"]
        )
    evidence = ReviewEvidence(
        values["harness"],
        values["requested"],
        values["verified"],
        values["source"],
        values["commit"],
        round_number,
    )
    return evidence, values["body"]


def validate_review_gate(
    comments: Sequence[str],
    *,
    reviewers: Sequence[ReviewerConfig],
    commit: str,
    round_number: int,
) -> None:
    """Require exactly two independently parsed reviews of the expected final diff."""

    if len(comments) != 2:
        raise _identity_error(
            "Review gate requires exactly two evidence-bearing comments.", "unknown"
        )
    if len(reviewers) != 2:
        raise _identity_error("Review gate requires exactly two configured reviewers.", "unknown")
    parsed = [parse_review_comment(comment)[0] for comment in comments]
    if len(set(comments)) != 2:
        raise _identity_error("Review gate rejects duplicate evidence comments.", "unknown")
    expected_sources = {"claude": CLAUDE_SOURCE, "codex": CODEX_SOURCE}
    for evidence, expected in zip(parsed, reviewers, strict=True):
        if evidence.verification_source not in SUPPORTED_VERIFICATION_SOURCES:
            raise _identity_error(
                "Review uses an unsupported verification source.", evidence.requested_model
            )
        if evidence.requested_model != evidence.verified_model:
            raise _identity_error(
                "Requested and verified reviewer models differ.", evidence.requested_model
            )
        if evidence.harness != expected.harness or evidence.requested_model != expected.model:
            raise _identity_error(
                "Review evidence does not match its configured reviewer slot.",
                evidence.requested_model,
            )
        if evidence.verification_source != expected_sources.get(evidence.harness):
            raise _identity_error(
                "Review verification source is incompatible with its harness.",
                evidence.requested_model,
            )
        if evidence.commit != commit:
            raise _identity_error(
                "Review evidence targets a stale commit.", evidence.requested_model
            )
        if evidence.round_number != round_number:
            raise _identity_error(
                "Review evidence targets the wrong review round.", evidence.requested_model
            )


def complete_review_stage(
    comments: Sequence[str],
    *,
    reviewers: Sequence[ReviewerConfig],
    commit: str,
    round_number: int,
    triage: Callable[[], None],
    resolve_responsibility: Callable[[], None],
) -> None:
    """Order the evidence gate ahead of triage and responsibility resolution."""

    validate_review_gate(comments, reviewers=reviewers, commit=commit, round_number=round_number)
    triage()
    resolve_responsibility()
