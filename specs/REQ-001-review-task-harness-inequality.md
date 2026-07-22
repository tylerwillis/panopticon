# REQ-001: Review task harness inequality

## Overview

Review-task creation enforces cross-harness review through deterministic validation of recorded opaque task fields. The workflow name `review` is the temporary marker until the review workflow from ADR-0014 stack 1 lands.

## Requirements

### REQ-001.1: Governor required

1. Creating a task whose workflow name is `review` without a `governor_task_id` MUST fail with a creation validation error.

### REQ-001.2: Equal harnesses rejected

1. Creating a task whose workflow name is `review` MUST fail with a creation validation error when the registry-resolved harness names that the two tasks would execute are equal, including when one task records the unset default sentinel and the other explicitly records that default harness while their starting-model strings differ.

### REQ-001.3: Different harnesses accepted

1. An otherwise-valid task creation whose workflow name is `review` MUST succeed when the registry-resolved harness name it would execute differs from the registry-resolved harness name its governor would execute, including when the two tasks' recorded starting-model strings are equal.

### REQ-001.4: Other workflows unaffected

1. An otherwise-valid task creation whose workflow name is not `review` MUST NOT be rejected by review-task governor or harness-inequality validation, whether or not it has a governor task and regardless of the tasks' recorded harness strings.
