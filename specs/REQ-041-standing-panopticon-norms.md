# REQ-041: Standing panopticon working norms

## Overview

Orient every container agent to panopticon's durable collaboration surfaces and make prompt
artifact publication an ordinary working norm rather than behavior that depends on a repeated
operator request.

## Requirements

### REQ-041.1: Standing artifact expectation

1. The shared workflow overview MUST instruct the agent to publish each reviewable document as a
task artifact when the document is produced, without conditioning publication on a user request.

### REQ-041.2: Overview-to-skill contract

1. The shared workflow overview MUST identify the universal `artifacts` skill as the procedure for
the standing publication expectation while retaining the guidance that non-code deliverables do
not belong inline or solely in the ephemeral container filesystem.

### REQ-041.3: Skill-to-overview contract

1. The universal `artifacts` skill MUST state that the system prompt establishes the standing
expectation to publish reviewable documents when produced and explain how to fulfill it with the
`put_artifact` MCP tool or the REST artifact endpoint.

### REQ-041.4: Durable surface distinctions

1. The artifact guidance MUST preserve that operators open artifacts with the dashboard `a`
hotkey, artifacts complement a pull request and its task URL, and GitHub URLs instead belong in
the task external URL field opened with the dashboard `p` hotkey.

### REQ-041.5: Shared panopticon orientation

1. The base workflow overview MUST include a `## Working in panopticon` section that explains the
agent is inside a panopticon task container and identifies task artifacts, the task external URL
field, responsibilities, and `advance`/`drop` as the durable control-plane surfaces available to
the agent, including what the operator sees from each.

### REQ-041.6: Single shared rendering

1. The base `Workflow.overview()` output MUST contain the same orientation and standing artifact
expectation for workflows both with and without declared responsibilities or tools while
`spike`'s `ITERATING` responsibilities remain empty.

This requirement is implemented at the existing shared renderer; it introduces no new delivery
surface or workflow-specific copy.

### REQ-041.7: Claude delivery

1. The Claude harness MUST deliver the shared orientation and standing artifact expectation in
the workflow overview passed through `--append-system-prompt`.

### REQ-041.8: Codex delivery

1. The Codex harness MUST deliver the shared orientation and standing artifact expectation in the
workflow overview rendered as `developer_instructions`.

### REQ-041.9: Pi delivery

1. The Pi harness MUST deliver the shared orientation and standing artifact expectation in the
workflow overview passed through `--append-system-prompt`.
