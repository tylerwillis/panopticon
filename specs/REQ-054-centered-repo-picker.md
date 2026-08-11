# REQ-054: Centered new-task repository picker

## Overview

Starting a new task opens a modal repository picker before the workflow and task-detail steps.
The picker is a transient choice dialog and should appear in the visual center of the terminal,
consistent with the other task-creation dialogs, rather than at the screen origin.

## Requirements

### REQ-054.1: Initial picker placement

1. When the new-task repository picker is displayed, its choice box MUST be horizontally and
   vertically centered within the modal screen's current dimensions.

## Non-goals

- This change does not alter repository ordering, filtering, selection, or cancellation.
- This change does not alter the size or visual styling of the repository picker box.
