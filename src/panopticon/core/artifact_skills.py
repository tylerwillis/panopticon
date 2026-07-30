"""Universal artifact-publishing skill exposed to every task."""

from __future__ import annotations

from panopticon.core.models import Skill

ARTIFACT_SKILL = Skill(
    "artifacts",
    "Publish durable work products for operator review.",
    "Publish reviewer-readable work that is not the pull request itself as a task artifact. "
    "Use the `put_artifact` MCP tool, or send the artifact bytes with "
    "`PUT /tasks/{task_id}/artifacts/{name}` over REST. The operator opens published artifacts "
    "from the dashboard with the `a` hotkey. Good candidates include a specification or spec "
    "summary, review outputs, a triage summary, and stage or gate reports. Artifacts complement "
    "the pull request and its dedicated external task URL; they do not replace either one.",
)
