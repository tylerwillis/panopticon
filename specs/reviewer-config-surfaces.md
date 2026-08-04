# Reviewer configuration surfaces

## Overview

The RFC 2119 workflows own their reviewer defaults, while a repository may override those
defaults for its projects. Repository env files remain credentials transport and are no longer
the operator-facing source of reviewer preferences. The existing reviewer environment variables
remain the container-side transport: the session service renders the repository fields into them
after any env file is applied.

All reviewer settings are ordered atomic `<harness>:<model>` strings. The harness is validated by
Panopticon, while the model substring remains opaque to the control plane and is split only from
the first colon. The repository fields are named `honesty_reviewer`, `reviewer_1`, and
`reviewer_2`; a null or blank field means that the corresponding workflow default applies. These
names give the separate layered-settings work stable fields to signpost without adding those
hints here. Reviewer identity verification remains governed unchanged by REQ-036.2 through
REQ-036.4.

## Requirements

### 1: Workflow-owned defaults

1. Every built-in 2119 workflow MUST expose a subclass-overridable `honesty_reviewer` default of
   `ReviewerConfig("codex", "gpt-5.6-sol")` without removing the existing subclass override
   behavior of `reviewers` or `fable_reviews`.
2. Given a workflow's `honesty_reviewer` default and the task-container environment, test-honesty
   command resolution MUST prefer a nonblank `PANOPTICON_2119_HONESTY_REVIEWER` pair and otherwise
   construct the command from the workflow default instead of an independently hardcoded Codex
   command.

### 2: Repository-owned overrides

1. A repository MUST expose nullable `honesty_reviewer`, `reviewer_1`, and `reviewer_2` fields
   through its domain record, create/read/patch API, persistent store and migration, and editable
   repository form.
2. Repository create and patch operations MUST reject every nonblank reviewer override having a
   missing harness, missing model, unsupported harness, or malformed pair as an actionable
   configuration failure.

### 3: Spawn transport and precedence

1. Container spawn MUST render the repository's three reviewer fields respectively as
   `PANOPTICON_2119_HONESTY_REVIEWER`, `PANOPTICON_2119_REVIEWER_1`, and
   `PANOPTICON_2119_REVIEWER_2` after the repo env file, using an explicit empty value for each
   absent override so credentials-file contents cannot select reviewers.
2. Reviewer resolution MUST use a nonblank rendered repository override when present, otherwise
   use the corresponding workflow default, split an override only on its first colon, and reject
   an invalid nonblank workflow default or override before any reviewer command runs.
3. When a repo env file contains reviewer transport keys, a runner MUST warn on its first
   container spawn using that file, name all and only those inert reviewer keys, direct the
   operator to the repo reviewer fields without logging values, and suppress the warning on later
   spawns by that runner using the same file.

## Non-goals

- Adding layered-setting UI hints is owned by the related `layered-settings-hints` task.
- Reviewer model aliases remain opaque; Panopticon does not translate them to provider model IDs.
- Reviewer LLM calls remain confined to task containers.
- Changing any reviewer identity verification, evidence report, or review gate from REQ-036 is out
  of scope.
