# Full-width dashboard

## Overview

When enough tasks exist to overflow the dashboard vertically, Textual paints a vertical scrollbar
two columns wide at the right edge of the task table. At full-screen width that scrollbar reads as
a right-hand border and prevents task content from using the terminal's full width. The dashboard
keeps keyboard scrolling while removing that reserved gutter. The horizontal scrollbar at the
bottom of the task table is outside this change.

## Requirements

### 1: Full-width vertical overflow

1. A vertically overflowing dashboard task table MUST allocate its entire content-region width to
   scrollable task content without reserving columns for a right-side vertical scrollbar.
2. A vertically overflowing dashboard task table MUST remain keyboard-scrollable to every task
   row while its right-side vertical scrollbar occupies zero columns.

## Non-goals

- This change does not remove or restyle the task table's bottom horizontal scrollbar.
- This change does not alter the dashboard header, shortcut footer, detail pane, or task-table
  column allocation.
