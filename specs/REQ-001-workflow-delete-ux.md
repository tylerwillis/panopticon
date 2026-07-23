# REQ-001: Workflow file deletion

## Overview

The terminal workflows screen supports deleting operator-authored workflow files while
protecting built-in workflows and accurately describing the running registry's additive-only
behavior.

## Requirements

### REQ-001.1: Delete action

1. The workflows screen MUST bind `x` to request deletion of the highlighted operator workflow by opening a confirmation dialog.

### REQ-001.2: Honest confirmation

1. The deletion confirmation dialog MUST offer explicit yes and no choices.

### REQ-001.3: Cancellation

1. Choosing no or pressing Escape in the deletion confirmation dialog MUST dismiss the dialog without deleting the selected workflow file.

### REQ-001.4: Confirmed deletion

1. Choosing yes in the deletion confirmation dialog MUST remove the selected workflow file and refresh the workflows table from the running service's current registry.

### REQ-001.5: Built-in protection

1. Requesting deletion of a built-in workflow MUST leave its file present without opening the deletion confirmation dialog.

### REQ-001.6: Registry lifecycle disclosure

1. The deletion confirmation dialog MUST explain that deletion removes the file while the running service retains the loaded workflow until its next restart.

### REQ-001.7: Modal styling

1. The deletion confirmation MUST present a centered, 56-column, auto-height panel with the same round accent border and surface background as the new-workflow dialog.

### REQ-001.8: Built-in refusal notification

1. Requesting deletion of a built-in workflow MUST notify the operator that built-in workflows cannot be deleted.
