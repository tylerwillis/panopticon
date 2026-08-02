"""Universal artifact-publishing skill exposed to every task."""

from __future__ import annotations

from panopticon.core.models import Skill

ARTIFACT_SKILL = Skill(
    "artifacts",
    "Publish durable work products for operator review.",
    "The system prompt establishes a standing expectation: publish each reviewable document as "
    "a task artifact when you produce it, without waiting for the user to ask. This skill is the "
    "procedure for fulfilling that expectation. An artifact is a durable task document that the "
    "user can review. Examples include a "
    "specification or spec summary, review outputs, a triage summary, and stage or gate reports, "
    "but these examples are not exhaustive. "
    "Use the `put_artifact` MCP tool, or send the artifact bytes with "
    "`PUT /tasks/{task_id}/artifacts/{name}` over REST. The operator opens published artifacts "
    "from the dashboard with the `a` hotkey. Artifacts complement the pull request and its "
    "dedicated external task URL; they do not replace either one. The substantial exception is "
    "GitHub URLs: record those in the task's external URL field so the dashboard `p` hotkey opens "
    "them, rather than publishing them as artifacts.",
)
