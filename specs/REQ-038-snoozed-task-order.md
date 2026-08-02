# REQ-038: Snoozed task ordering

## Overview

The top of the dashboard is an operator-attention queue. A snooze deliberately removes a task
from that queue without making it terminal, so snoozed work belongs at the end of the active
section rather than ahead of work that may need the user.

This ordering uses the same display-time and attention-piercing rules as the existing snooze
presentation. It does not change the stored snooze fact or the ordering of terminal work.

## Requirements

### REQ-038.1: Snoozed active section

1. In both dashboard sort modes, the dashboard MUST place every ungoverned non-terminal task whose
   snooze is active and unpierced at the injected display time after all other ungoverned
   non-terminal tasks and before all ungoverned `COMPLETE` and `DROPPED` tasks, while expired
   snoozes and snoozes pierced by `Task.attention` retain ordinary non-terminal ordering.

## Non-goals

- Snoozing does not alter lifecycle state, turn, attention, timestamps, or the recorded deadline.
- This requirement does not reorder tasks within the snoozed section or terminal section beyond
  the dashboard's selected existing sort mode.
- Governed children retain the dashboard's existing adjacency to their governor; an ensemble is
  ordered by its ungoverned root rather than split apart by a child's snooze.
