# REQ-009: Discoverable launch selection

## Overview

The new-task memo modal lets an operator choose the agent harness, model, and thinking effort
without requiring prior knowledge of hidden focus targets or harness-specific model syntax.
The harness remains a selection-only control; type-ahead applies to the model and effort
vocabularies supplied by the selected harness. A field is touched when the operator changes its
text or accepts a candidate, not merely when the field receives focus.

## Requirements

### REQ-009.1: Visible launch controls

1. The new-task memo modal MUST show labeled harness, model, and effort controls with their current
   values before any launch control receives focus.

### REQ-009.2: Launch-control tab order

1. Forward tab navigation from the memo editor MUST visit the harness, model, and effort controls
   in that order.

### REQ-009.3: Type-ahead filtering

1. While the operator edits either the model or effort control, that control MUST display a
   candidate list containing all and only suggestions from the selected harness that
   case-insensitively contain the field's text.

### REQ-009.4: Candidate navigation and selection

1. Up and Down pressed while a candidate list is visible MUST move the highlight through the
   currently filtered candidates.

### REQ-009.5: Free-text launch values

1. The model and effort controls MUST accept and submit free-text values that are absent from the
   selected harness's suggestions.

### REQ-009.6: Harness-dependent suggestions

1. Changing the harness MUST refresh the model and effort candidate vocabularies from the newly
   selected harness.

### REQ-009.7: Untouched default tracking

1. Launch fields the operator has not touched MUST continue to track the effective workflow, repo,
   or application defaults.

### REQ-009.8: Established memo actions

1. Escape MUST cancel the memo modal regardless of which control is focused or whether a candidate
   list is visible.

### REQ-009.9: Modal width

1. The memo modal MUST retain its 64-column width while the launch controls and candidate lists are
   available.

### REQ-009.10: Candidate acceptance

1. Enter pressed while a candidate is highlighted MUST update the corresponding field to that
   candidate without submitting the memo.

### REQ-009.11: Touched values across harness changes

1. Changing the harness MUST leave touched model and effort values unchanged.

### REQ-009.12: Launch-summary source

1. The launch summary's single source label MUST report `this task` after any operator override and
   otherwise report the winning `workflow default`, `repo default`, or `app default` source.

### REQ-009.13: Memo submission

1. Enter pressed in the memo editor MUST submit the memo as the task's initial prompt.

### REQ-009.14: Set without submission

1. Ctrl+S MUST create the task with the memo without delivering it as the initial prompt.

### REQ-009.15: External memo editor

1. Ctrl+G MUST replace the memo editor's text with the content returned by the configured external
   editor.

### REQ-009.16: Harness cycling

1. Enter pressed on the focused harness control MUST cycle to the next registered harness without
   submitting the memo.

### REQ-009.17: Enter without a candidate

1. Enter pressed in a model or effort control without a highlighted candidate MUST leave the memo
   modal open.

### REQ-009.18: Untouched values after harness changes

1. Changing the harness MUST clear untouched model and effort values so the newly selected harness
   uses its own defaults.

### REQ-009.19: Registered harness values

1. The harness control MUST limit its selectable values to registered harness names.

### REQ-009.20: Empty filter results

1. A model or effort candidate list MUST remain visible as an empty state when no suggestion
   matches the field's text.
