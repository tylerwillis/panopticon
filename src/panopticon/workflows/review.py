"""The governed cross-model review worker from ADR 0014.

A review task is a clean, separate agent context governed by the authoring task. It has one job:
read the governor's plan and diff, then approve or leave structured findings without editing code.
Its lifecycle is deliberately tiny: ``REVIEWING → COMPLETE`` plus the inherited ``DROPPED`` escape.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Actor, Skill, Tool
from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow

_REVIEW_INSTRUCTIONS = """\
Review the change owned by this review task's governor. Never edit the governor's code: generation
is not review, and any fixes belong to the author in a later, freshly reviewed round.

1. **Identify the governor.** Call `get_task` with your own review task id and read its
   `governor_task_id`. Call `get_task` again with that governor task id; note its `url`, `branch`,
   `clone`, slug, and memo. Do not retrieve, request, or use the author's conversation even if
   supplied. It is not review input; use only the recorded task facts and artifacts below.
2. **Read the plan.** Call `list_artifacts` on the governor task id, then read its `plan.md` through
   the returned MCP resource URI.
3. **Inspect the change.** If the governor has a recorded `url`, run `gh pr view <url>` and
   `gh pr diff <url>`. Otherwise use its recorded `branch` and `clone`: inspect the clone directly
   when it is accessible, or fetch the branch into `/workspace`, then run `git diff` against the
   base branch. Do not modify either checkout.
4. **Assess correctness and whether the change matches the plan without unplanned scope.** Also
   assess simplicity and net line count. Apply the simplicity ladder in this order:
   - delete unnecessary code;
   - reuse an existing primitive;
   - simplify existing code;
   - add the smallest new code necessary.
5. **Choose exactly one verdict:**
   - **Approve** — State the approval briefly. Write no artifact: no `review.md` or other verdict
     artifact. Call the `advance` operation to move this review task to `COMPLETE`.
   - **Findings** — Write the verdict on the governor task:
     `put_artifact(task_id=<governor_task_id>, name="review.md", content=<findings>)`.
     Use this format, omitting a heading only when it has no findings:

     ```markdown
     # Review — <slug>

     ## Must fix
     - <actionable correctness or simplicity finding>

     ## Suggestions
     - <optional lower-priority improvement>
     ```

     Keep every finding concrete and actionable. Then call the `advance` operation to move this
     review task to `COMPLETE`.
"""


class Review(Workflow):
    """A hidden internal worker that reviews its governor in a clean agent context.

    ``hidden`` keeps this workflow out of both operator menus because a review task without a
    governor has nothing to review. The deterministic authoring-workflow hook introduced in a
    later ADR-0014 stack creates it directly by name instead.
    """

    name: ClassVar[str] = "review"
    hidden: ClassVar[bool] = True
    when_to_use: ClassVar[str] = "Review a governor task's change in a clean, cross-model context."

    class Reviewing(InitialState):
        label = "REVIEWING"
        description = "Review the governor's plan and diff; approve or leave structured findings."
        turn_on_enter = Actor.AGENT
        advanced_by = Actor.AGENT
        transitions = (Complete,)

    initial = Reviewing

    def skills(self) -> Sequence[Skill]:
        """The single read-only cross-model review procedure."""
        return (
            Skill(
                "review-change",
                "Review the governor's diff and plan; approve or leave structured findings.",
                _REVIEW_INSTRUCTIONS,
            ),
        )

    def tools(self) -> Sequence[Tool]:
        """Name the GitHub CLI used when the governor records a PR URL."""
        return (Tool("gh", "the GitHub CLI, used read-only to inspect the governor's PR."),)

    def image_layer(self) -> str:
        """Install the GitHub CLI used by the review procedure."""
        return "RUN apt-get update && apt-get install --yes --no-install-recommends gh"
