# REQ-023: Per-context dashboard keybinding uniqueness

## Overview

Textual treats lower- and uppercase letter input as distinct key chords: `y` and `Y` do not
conflict. Panopticon's current details and help surfaces show those bare letters side by side,
however, which makes the uppercase copy-id chord look like a second label for the copy-slug chord.
The operator-facing label should spell the shifted chord explicitly.

Binding uniqueness is scoped to what can receive the same key event at one time. Reusing `escape`
to close separate modal screens is valid; assigning one chord to two action targets on the same
screen, or between that screen and a focusable widget active on it, is not.

## Requirements

### REQ-023.1: Unique actions in an active binding context

1. In each simultaneously active dashboard binding context, every Textual-normalized key chord
   MUST resolve to at most one action target.

An active context is the Dashboard's default screen or one Screen or ModalScreen defined in the
dashboard module, combined with the binding ancestry of one focusable widget in that screen's
compose tree. A screen with no focusable widgets forms a context by itself. Dashboard application
bindings suppressed beneath a modal by Textual do not participate in that modal's context, so
reusing a chord on separate screens remains valid.

### REQ-023.2: Inherited binding participation

1. Effective inherited Textual binding targets MUST participate in an active-context uniqueness
   check when their chord is also defined by Panopticon in that context.

### REQ-023.3: Case-sensitive chords

1. Textual chord normalization MUST preserve letter case, so `y` and `Y` remain distinct chords
   whose coexistence in one context is not a collision.

### REQ-023.4: Automatically discovered binding coverage

1. Adding a Screen or ModalScreen subclass defined in the dashboard module, or a focusable widget
   anywhere in one of those screens' compose trees, MUST bring its effective bindings under the
   automated uniqueness check without adding that screen, widget, chord, or action to a separate
   inventory.

### REQ-023.5: Collision rejection

1. When an active binding context contains one chord resolving to multiple action targets, the
   automated uniqueness check MUST fail.

### REQ-023.6: Actionable collision diagnostics

1. A uniqueness-check failure MUST identify the active context, Textual-normalized chord, and
   every conflicting action target.

### REQ-023.7: Unambiguous copy-chord help

1. The open details-pane hint and the full help screen MUST label the copy-slug binding as `y` and
   the copy-id binding as `Shift+Y`.
