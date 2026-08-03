# REQ-040: Dashboard artifact and pull-request prefixes

## Overview

The dashboard task list should make reviewable outputs visible without requiring the operator to
open each task's detail pane. Artifact and GitHub pull-request indicators belong directly beside
the task label while leaving the stored slug unchanged.

For this specification, a task has an associated GitHub pull request when its external URL is an
HTTP or HTTPS GitHub URL whose path contains a `/pull/<number>` segment. The `<number>` is a
non-empty sequence of decimal digits terminated by the end of the path or the next `/`.

## Requirements

### REQ-040.1: Artifact indicator

1. The dashboard MUST include `*a` in the displayed task-label prefix when the task has at least
   one artifact, including when the task has no associated GitHub pull request.

### REQ-040.2: GitHub pull-request indicator

1. The dashboard MUST include `*PR###` in the displayed task-label prefix, substituting the
   decimal pull-request number for `###`, when the task's external URL identifies an associated
   GitHub pull request.

### REQ-040.3: Indicator composition

1. The dashboard MUST render the artifact indicator before the pull-request indicator when both
   apply, separate the two indicators with one space, and append exactly ` | ` once between the
   complete indicator prefix and the existing task label.

### REQ-040.4: Existing label presentation

1. The dashboard MUST preserve the existing slug, first-line memo, tree connector, and disclosure
   presentation after applying any artifact and pull-request indicators, and display no indicator
   separator when neither indicator applies.

## Non-goals

- The indicators do not mutate the task's slug, memo, external URL, artifacts, or lifecycle state.
- External URLs that do not identify a GitHub pull request do not receive a pull-request indicator.
- This feature does not change the artifact or pull-request hotkeys.
