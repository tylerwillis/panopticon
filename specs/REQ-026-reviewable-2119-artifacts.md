# REQ-026: Reviewable workflow artifacts

## Overview

Make artifact publishing a universal agent capability, keep the 2119 specification gate
lightweight, commit the current three-workflow 2119 family as built-ins, and preserve pull-request
URLs as first-class task metadata.

## Requirements

### REQ-026.1: Universal artifact skill

1. Every task in every workflow MUST expose a core artifact-publishing skill in addition to its
workflow-specific skills.

### REQ-026.2: Artifact publishing mechanics

1. The core artifact-publishing skill MUST explain both the `put_artifact` MCP tool and the REST
`PUT /tasks/{task_id}/artifacts/{name}` mechanism.

### REQ-026.3: Artifact review guidance

1. The core artifact-publishing skill MUST tell agents that operators open artifacts with the
dashboard `a` hotkey and encourage publishing reviewer-readable material that is not the pull
request, including specifications or summaries, review outputs, triage summaries, and stage or
gate reports.

### REQ-026.4: Specification artifact gate

1. Each built-in 2119 workflow MUST add exactly one artifact-related `SPECIFYING` responsibility
that requires the specification to be published as a task artifact for human review.

### REQ-026.5: Lightweight 2119 skill guidance

1. Each built-in 2119 workflow's `spec-2119` and review skills MUST instruct the agent to publish
the specification and the final review outputs plus triage summary, respectively, as task
artifacts.

### REQ-026.6: Pull-request URL retention

1. Each built-in 2119 workflow MUST retain the `BUILDING` responsibility that records the
pull-request URL in the task's external URL field.

### REQ-026.7: Built-in 2119 workflow family

1. Workflow discovery MUST register `2119-human-spec`, `2119-auto-spec`, and `2119-auto-sol` as
built-in workflows when no external workflow directory is present.

### REQ-026.8: Sol-only automatic workflow

1. Every built-in 2119 workflow MUST direct test-honesty review to Sol, while
`2119-human-spec` and `2119-auto-spec` use Fable plus Sol for final change review and
`2119-auto-sol` uses Sol only for final change review.

### REQ-026.9: Actionable duplicate migration failure

1. When a workflow file in the operator's home workflow directory duplicates a built-in workflow
name, discovery MUST fail with an actionable error that identifies the external file and directs
the operator to remove that copy.

### REQ-026.10: Artifact complementarity

1. Artifact guidance MUST describe artifacts as complements to, rather than replacements for, the
pull request and its dedicated task URL.

### REQ-026.11: Artifact definition and publishing boundary

1. The core artifact-publishing skill MUST define an artifact as a durable task document for user
review, direct the agent to publish anything it wants the user to review as an artifact regardless
of document type, and identify GitHub URLs as the substantial exception that belongs in the task's
external URL field for the dashboard `p` hotkey instead of the artifact list opened by `a`.
