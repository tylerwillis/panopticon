# REQ-053: Directional task overflow indicators

## Overview

The dashboard deliberately gives task content the full terminal width by hiding Textual's
right-side vertical scrollbar. Operators still need an unmistakable signal when task rows exist
outside the viewport, in either vertical direction.

The first and last visible task-content lines are reliably identifiable: Textual exposes the
scrollable content region together with the current and maximum vertical scroll offsets. The
dashboard uses those signals to composite short `↑ more` and `↓ more` markers onto the trailing
cells of the corresponding task rows only while content exists beyond that edge. The underlying
row remains rendered, including its leading identity and cursor highlighting.

The existing bottom line is not a general-purpose hint area. It contains Textual's horizontal
scrollbar thumb when the task columns overflow horizontally, and it disappears when those columns
fit even if the table overflows vertically. Directional overlays therefore communicate vertical
overflow without obscuring that scrollbar or making vertical discoverability depend on horizontal
overflow.

## Requirements

### REQ-053.1: Content below

1. While task content is vertically hidden below the current viewport, the task table MUST render
   a right-aligned `↓ more` in trailing blank cells of its last visible task-content line,
   abbreviating it to `↓` when only one trailing blank cell is available and omitting it when no
   trailing cell can be replaced without hiding row content.

### REQ-053.2: Content above

1. While task content is vertically hidden above the current viewport, the task table MUST render
   a right-aligned `↑ more` in trailing blank cells of its first visible task-content line below
   the header, abbreviating it to `↑` when only one trailing blank cell is available and omitting
   it when no trailing cell can be replaced without hiding row content.

### REQ-053.3: Directional absence

1. The task table MUST omit each directional indicator whenever no task content is hidden in that
   direction, including omitting both indicators when all task content fits in the viewport.

### REQ-053.4: Full-width and horizontal-scrollbar preservation

1. Indicator rendering MUST preserve equality between the task table's scrollable-content width
   and content-region width together with the existing horizontal scrollbar's visibility, size,
   and rendered thumb when horizontal overflow exists.

### REQ-053.5: Navigation

1. With either directional indicator visible, keyboard navigation MUST keep the selected task's
   identifying text and cursor highlighting visible after every step through all real task rows.

### REQ-053.6: Renderability across scroll offsets

1. At every vertical scroll offset, the task table MUST retain identifying text on each real task
   row that intersects the visible task-content region.

## Non-goals

- The indicators do not describe horizontal overflow; the existing horizontal scrollbar retains
  that role.
- The indicators do not reserve a permanent task row or restore a right-side gutter.
- This change does not alter task ordering, selection, or scrolling semantics.
