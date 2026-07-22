# REQ-009: Rename a task slug from the details pane

## Overview

Let an operator rename the highlighted task's slug without leaving the dashboard. The action is
available from the open details pane and uses `e`, for “edit”, as its memorable key.

## Requirements

### REQ-009.1: Details-pane rename action

1. While the details pane is open on a highlighted task, pressing `e` MUST open a slug editor
   initialized with that task's current slug; while the details pane is closed, pressing `e` has no
   rename effect.

### REQ-009.2: Save the renamed slug

1. Submitting a non-empty value from the slug editor MUST set the highlighted task's slug to that
   value through the task service and refresh the dashboard's displayed task data.

### REQ-009.3: Cancel without renaming

1. Cancelling the slug editor MUST leave the highlighted task's slug unchanged.

### REQ-009.4: Discoverable key

1. When displaying a task, the open details pane MUST include `e: edit slug` in its trailing key
   hint.
