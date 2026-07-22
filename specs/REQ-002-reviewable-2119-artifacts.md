# REQ-002: Reviewable 2119 workflow artifacts

## Overview

Make the human-reviewable contracts and review results of both 2119 workflows available through
the dashboard's artifact browser while preserving the task's dedicated pull-request URL.

## Requirements

### REQ-002.1: Specification artifact responsibility

1. The `2119-human-spec` and `2119-auto-spec` workflows MUST give their `SPECIFYING` state a
responsibility that directs the agent to upload a `spec.md` task artifact.

### REQ-002.2: Specification briefing

1. When `spec.md` exists, the `2119-human-spec` and `2119-auto-spec` workflows MUST include its
canonical task-artifact URI in the agent briefing.

### REQ-002.3: Review artifact responsibility

1. The `2119-human-spec` and `2119-auto-spec` workflows MUST give their `REVIEWING` state a
responsibility that directs the agent to upload a `review.md` task artifact.

### REQ-002.4: Pull-request URL

1. The `2119-human-spec` and `2119-auto-spec` workflows MUST give their `BUILDING` state a
responsibility that directs the agent to record the pull-request URL in the task's external URL
field.

### REQ-002.5: Pull-request contract reference

1. The pull-request skill exposed by the `2119-human-spec` and `2119-auto-spec` workflows MUST
identify `spec.md` as the task artifact that supplies the change contract.

### REQ-002.6: Specification source

1. The `spec.md` responsibility MUST direct the agent to identify the repository specification file
whose contents the artifact mirrors.

### REQ-002.7: Independent review reports

1. The `review.md` responsibility MUST direct the agent to include the final Fable 5 review report.

### REQ-002.8: Finding dispositions

1. The `review.md` responsibility MUST direct the agent to include the accepted or rejected
disposition for every finding in the final review reports.

### REQ-002.9: Sol review report

1. The `review.md` responsibility MUST direct the agent to include the final Sol 5.6 review report.

### REQ-002.10: Finding disposition reasons

1. The `review.md` responsibility MUST direct the agent to include the reason for every finding's
disposition.
