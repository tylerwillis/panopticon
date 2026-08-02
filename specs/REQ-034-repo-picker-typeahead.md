# REQ-034: Repository picker typeahead

## Overview

Speed up new-task creation when many repositories are configured by letting the operator narrow
the repository picker directly from the keyboard. The filter is intentionally small: it matches
repository IDs from the beginning, ignores letter case, and leaves the existing selection and
cancel interaction intact.

## Requirements

### REQ-034.1: Prefix filtering

1. While the new-task repository picker is open, typing a printable character MUST append it to a
   visible search query and show only repository IDs whose prefix matches the complete query
   without regard to letter case.

2. While the new-task repository picker has a non-empty search query, pressing Backspace MUST
   remove the query's final character and recompute the visible repository IDs.

### REQ-034.2: Choosing a filtered repository

1. Selecting a visible repository after filtering MUST continue task creation with that exact
   repository ID.
