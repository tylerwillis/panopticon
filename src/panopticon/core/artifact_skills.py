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
    "Use the `put_artifact` MCP tool. On a harness without MCP, send the artifact bytes with "
    "`_panopticon_had_xtrace=; case $- in *x*) set +x; "
    "_panopticon_had_xtrace=1 ;; esac; "
    'if [ -n "${PANOPTICON_SERVICE_AUTH_TOKEN:-}" ]; then '
    "printf 'header = \"Authorization: Bearer %s\"\\n' "
    "\"$PANOPTICON_SERVICE_AUTH_TOKEN\" | curl --disable --noproxy '*' --config - --fail --silent --show-error "
    "--request PUT --data-binary @<artifact-file> "
    '"$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID/artifacts/<name>"; else '
    "curl --fail --silent --show-error --request PUT --data-binary @<artifact-file> "
    '"$PANOPTICON_SERVICE_URL/tasks/$PANOPTICON_TASK_ID/artifacts/<name>"; fi; '
    "_panopticon_status=$?; "
    '[ -n "$_panopticon_had_xtrace" ] && set -x; (exit "$_panopticon_status")` over REST; this '
    "keeps the credential out of process arguments. The operator opens published artifacts "
    "from the dashboard with the `a` hotkey. Artifacts complement the pull request and its "
    "dedicated external task URL; they do not replace either one. The substantial exception is "
    "GitHub URLs: record those in the task's external URL field so the dashboard `p` hotkey opens "
    "them, rather than publishing them as artifacts.",
)
