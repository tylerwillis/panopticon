# REQ-019: Discoverable ensembles

## Overview

The task table makes governed child tasks discoverable while preserving ensemble collapse as
dashboard-session display state. Collapsed and expanded ensembles expose matching disclosure
affordances, and the help screen explains how to toggle them.

## Requirements

### REQ-019.1: Collapsed ensemble summary

1. A collapsed governor MUST replace its visible governed subtree with one dim slug-column summary
   whose text begins with a right-pointing disclosure marker and the current direct-child count,
   followed by an instruction to press Enter to expand.

### REQ-019.2: Governor disclosure state

1. A governor row MUST prefix its slug with a right-pointing disclosure marker while collapsed and
   a down-pointing disclosure marker while expanded, updating the marker whenever its ensemble is
   toggled.

### REQ-019.3: Session-local toggle

1. Enter on a governor MUST alternate between its collapsed summary and expanded child rows while
   keeping that display state local to the dashboard session and leaving task-service data
   unchanged.

### REQ-019.4: Placeholder navigation

1. Vertical keyboard navigation MUST skip a collapsed ensemble summary and land only on real task
   rows.

### REQ-019.5: Help discoverability

1. The dashboard help screen MUST identify Enter as the control for collapsing and expanding a
   governor's ensemble of governed tasks.
