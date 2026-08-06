# REQ-053: Directional task overflow indicators

## Overview

The dashboard deliberately gives task content the full terminal width by hiding Textual's
right-side vertical scrollbar. Operators still need an unmistakable signal when task rows exist
outside the viewport, in either vertical direction.

The last visible task-content line is reliably identifiable: Textual exposes the scrollable
content region together with the current and maximum vertical scroll offsets. The dashboard uses
those signals to overlay centered `↑ more tasks` and `↓ more tasks` messages on the first and
last visible task-content lines only while content exists beyond the corresponding edge.

The existing bottom line is not a general-purpose hint area. It contains Textual's horizontal
scrollbar thumb when the task columns overflow horizontally, and it disappears when those columns
fit even if the table overflows vertically. Directional overlays therefore communicate vertical
overflow without obscuring that scrollbar or making vertical discoverability depend on horizontal
overflow.

## Requirements

### REQ-053.1: Content below

1. While task content is vertically hidden below the current viewport, the task table MUST replace
   its last visible task-content line with a centered `↓ more tasks` indicator.

### REQ-053.2: Content above

1. While task content is vertically hidden above the current viewport, the task table MUST replace
   its first visible task-content line below the header with a centered `↑ more tasks` indicator.

### REQ-053.3: Directional absence

1. The task table MUST omit each directional indicator whenever no task content is hidden in that
   direction, including omitting both indicators when all task content fits in the viewport.

### REQ-053.4: Full-width and horizontal-scrollbar preservation

1. Indicator rendering MUST preserve equality between the task table's scrollable-content width
   and content-region width while leaving the horizontal scrollbar line unobscured when it exists.

### REQ-053.5: Navigation

1. With either directional indicator visible, keyboard navigation MUST remain able to reach every
   real task row.

## Non-goals

- The indicators do not describe horizontal overflow; the existing horizontal scrollbar retains
  that role.
- The indicators do not reserve a permanent task row or restore a right-side gutter.
- This change does not alter task ordering, selection, or scrolling semantics.
