# REQ-002: New-task harness suggestion responsiveness

## Overview

Keep harness cycling responsive by discovering advisory model and effort suggestions once per
new-task memo modal opening.

## Requirements

### REQ-002.1: Responsive opening

1. The new-task memo modal MUST accept operator input before suggestion discovery for all registered harnesses has completed.

### REQ-002.2: One discovery per harness

1. Within one opening of the new-task memo modal, model and effort suggestion discovery for each registered harness MUST occur at most once regardless of how many times the operator cycles through harnesses.

### REQ-002.3: Per-open freshness

1. Each opening of the new-task memo modal MUST present model and effort suggestions obtained during that opening rather than results retained from an earlier opening.

### REQ-002.4: Early-cycle fallback

1. When the operator cycles to a harness whose suggestion discovery has not completed, the modal MUST present that harness's model and effort suggestions before the cycle completes.

### REQ-002.5: Cached cycle latency

1. After suggestion discovery for a registered harness has completed, changing the modal's selection to that harness MUST update its harness-dependent fields in less than 10 milliseconds.

### REQ-002.6: Quiet early close

1. Closing the new-task memo modal while suggestion discovery remains in progress MUST NOT surface an operator-visible worker error.

### REQ-002.7: No post-close update

1. After the new-task memo modal closes, suggestion discovery that was still in progress MUST NOT modify the closed modal's widgets.

### REQ-002.8: Suggestion fidelity

1. For a selected harness, the model and effort suggestions presented by the new-task memo modal MUST equal the suggestions discovered from that harness.
