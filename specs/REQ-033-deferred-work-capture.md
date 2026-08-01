# REQ-033: Deferred-work capture

## Overview

Adversarial review findings that get correctly rejected-for-now or deferred are high-signal ideas
that currently evaporate at merge; the operator has been capturing them as GitHub issues by hand.
This contract makes that part of the built-in 2119 workflows' default review/merge process,
matching who knows what when: the triage summary written at the end of `REVIEWING` is where the
deferred-but-good ideas are visible, so it proposes placeholder issues there for the user to react
to while approving the PR; `MERGING` is where the agent next has a turn after that reaction window,
so it files the endorsed ones there, before merging.

## Requirements

### REQ-033.1: Triage summary suggested-issues section

1. Each built-in 2119 workflow's review skill instructions MUST require the triage summary PR
comment to end with a "Suggested placeholder issues" section.

### REQ-033.2: Suggested-issue entry content

1. The review skill instructions MUST require a one-paragraph "Suggested placeholder issues" entry
for each rejected-or-deferred review finding judged genuinely good, covering what the idea is, why
it was deferred rather than done now, and what an implementer would need to know.

### REQ-033.3: Simply-wrong findings excluded

1. The review skill instructions MUST direct the agent to exclude findings rejected as simply
wrong from the "Suggested placeholder issues" section.

### REQ-033.4: Framed as recommendations

1. The review skill instructions MUST frame the "Suggested placeholder issues" section as
recommendations for the user to react to at the PR approval gate.

### REQ-033.5: MERGING responsibility ordering

1. Each built-in 2119 workflow's `MERGING` state MUST declare a `deferred-issues-filed`
responsibility that precedes `pr-merged` in its responsibilities.

### REQ-033.6: Re-read suggestions and reactions before merging

1. The `MERGING`-stage skill instructions for each built-in 2119 workflow MUST direct the agent,
before merging, to re-read the triage summary's "Suggested placeholder issues" section and any
user PR comments reacting to those suggestions.

### REQ-033.7: Endorsed-or-unaddressed suggestions filed

1. The `MERGING`-stage skill instructions MUST direct the agent to file a GitHub issue with
`gh issue create` for each suggested issue the user endorsed or left without objection,
incorporating any user edits.

### REQ-033.8: Rejected suggestions skipped

1. The `MERGING`-stage skill instructions MUST direct the agent to skip any suggested issue the
user explicitly rejected.

### REQ-033.9: Self-contained issue content

1. The `MERGING`-stage skill instructions MUST require each filed issue to be self-contained, with
a title stating the idea and a body carrying context: a link to the PR, a reference to the review
comment it came from, why it was deferred, and what shipping it would involve.

### REQ-033.10: Zero-suggestions legality

1. The `deferred-issues-filed` responsibility description MUST state that filing zero issues is a
legal outcome.
