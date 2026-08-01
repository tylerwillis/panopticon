# REQ-017: Rename a task slug from the details pane

## Overview

Let an operator rename the highlighted task's slug without leaving the dashboard. The action is
available from the open details pane and uses `v`, leaving `e` available for task snooze.

## Requirements

### REQ-017.1: Details-pane rename action

1. While the details pane is open on a highlighted task, pressing `v` MUST open a slug editor
   initialized with that task's current slug; while the details pane is closed, pressing `v` has no
   rename effect.

### REQ-017.2: Save the renamed slug

1. Submitting a non-empty value accepted by the task service from the slug editor MUST set the
   highlighted task's slug to the surrounding-whitespace-trimmed submitted value and refresh the
   dashboard's displayed task data; a rejected value leaves the existing slug displayed and the
   dashboard running.

### REQ-017.3: Cancel without renaming

1. Cancelling the slug editor MUST leave the highlighted task's slug unchanged.

### REQ-017.4: Discoverable key

1. When displaying a task, the open details pane MUST include `v: edit slug` in its trailing key
   hint.
