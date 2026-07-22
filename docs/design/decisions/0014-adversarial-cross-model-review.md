# 0014 — Adversarial cross-model review as a workflow stage

- Status: Proposed
- Date: 2026-07-17
- Deciders: — (proposed; pending review)

## Context

Live practice on this codebase has converged on a review discipline that is now treated as a
**validated requirement**, not a hypothesis:

- A change is **built by one frontier model** and **reviewed by a *different* frontier model**.
- The reviewer runs in a **clean context** — no shared conversation state with the author. It
  sees the *artifact* (the diff, the plan) rather than the author's reasoning, so it cannot
  inherit the author's blind spots or be anchored by the author's rationalizations.
- The review covers **both correctness and simplicity/net-LoC**. It is not only "is this
  correct?" but "is this the smallest correct change?" — reviewers on this repo have caught a
  portability test that asserted host-specific behavior, a gateway-config regression, a
  host-vs-container default-path bug that made a feature *silently inert*, and net-negative
  "simplifications" of freshly written code.
- **Generation is never review.** Even the reviewer's *own* suggested fixes, once the author
  applies them, get re-reviewed by the other side. The party that writes code is never the
  party that clears it.

Two facts about panopticon make this expressible as first-class machinery rather than an
out-of-band habit:

1. **Multi-harness support exists.** A task records its `harness` (`claude`/`codex`/`pi`/…, ADR
   0004's harness seam) and `starting_model` at creation, both validated against the harness
   registry and otherwise treated as opaque strings the control plane records but never
   interprets. So "run this task on a different model family than that one" is already
   representable.
2. **One container is one model in one context.** A task's container runs exactly one harness,
   one conversation. "Clean context, different model" is therefore not a mode you can switch a
   running agent into — it is *physically a second container*.

The existing `github-peer-reviewed` workflow already has a `REVIEW` state, but its `pr-reviewed`
responsibility is satisfied by a **human** peer (or left to the operator). The orchestrator's
`review-task` skill lets an orchestrator agent review a child's diff and drop a `review.md`
artifact — but nothing guarantees the reviewer is a *different model*, nothing runs it in a
*clean* context by construction, and it is opt-in agent behavior rather than a workflow stage.

This ADR decides how adversarial cross-model review becomes a **first-class, workflow-declared
stage** while preserving the determinism invariant (the control plane makes **no LLM calls**;
ADR 0008 / ARCHITECTURE §3) and the principle that **the user is never boxed in** (any state is
reachable by a free move; ADR 0004).

The RFC 2119 keywords MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used as defined in RFC
2119.

## Decision

### 1. Review is a separate **governed review task**, not a state the author runs

The review stage MUST be realized as a **distinct task** — a *review task* — spawned to review
an *authoring task*, and MUST NOT be realized as work the authoring agent performs inside a
state of its own workflow.

This follows directly from the ground-truth requirements. "Clean context" and "different model"
are properties of *a container*, and a task owns exactly one container (one harness, one
conversation). Any design where the authoring task reviews itself — a `REVIEWING` state whose
responsibilities the authoring agent resolves — necessarily reviews **with the same model, in
the same conversation that wrote the code**. That is precisely the configuration the practice
forbids. A `REVIEWING` state *can* hold the turn while review happens; it cannot *be* the
review.

So the authoring workflow keeps a **`REVIEW` gate state** (the ones that want cross-model review
declare it — `github-peer-reviewed` already has it), and the actual review is delegated to a
separate review task linked to the authoring task. Concretely:

- The review task's `governor_task_id` MUST be the authoring task's id. This reuses the existing
  governor/ensemble machinery (ADR-era orchestrator): the review task renders under the
  authoring task in the dashboard's ensemble, and the relationship is a recorded fact, not new
  schema.
- The review task runs a lightweight **`review` workflow** — a single agent-driven state
  (`REVIEWING → COMPLETE`, plus inherited `DROPPED`), shaped like `Spike`/`Orchestrator` — whose
  one skill is the cross-model review procedure (§3). It is deliberately minimal: it reviews one
  diff and terminates.

The authoring task's `REVIEW` state is the **gate that consumes the verdict** (§3); the review
task is the **worker that produces it**.

### 2. "Different model" is a **deterministic create-time validation rule**

The control plane MUST guarantee the reviewer differs from the author **without making an LLM
call**. It does so by comparing the two tasks' recorded launch pairs, which is pure string
inequality over opaque fields — no interpretation of what the models *are*, so the determinism
invariant holds.

**The rule (MUST):** when a review task is created for an authoring task, the review task's
`(harness, starting_model)` pair MUST differ from the authoring task's pair in **at least the
harness**. Creation MUST be rejected (a validation error, like the existing unknown-harness
check in `create_task`) if the harnesses are equal.

- Requiring a **different harness** — not merely a different `starting_model` within one harness
  — is what buys a genuinely different frontier model family (`codex` vs `claude`), which is the
  point of the practice. Two `starting_model`s under one harness MAY *also* differ, but that
  alone MUST NOT satisfy the rule.
- The comparison is on **recorded fields only**. The control plane cannot prevent a reviewing
  agent from switching models mid-session (`/model`); that is a soft contract carried in the
  skill instructions, not a control-plane guarantee. This limitation is stated explicitly rather
  than papered over (§ Consequences).
- **Different harness does not always mean different model family.** Some harnesses are
  **multi-provider**: `pi` resolves Anthropic, OpenAI, Gemini, Groq, and other providers (and
  `outfitter` wraps `pi`). A `claude`-authored task reviewed by a `pi`-harness task running an
  *Anthropic* model passes the harness-inequality check while being the **same model family** —
  exactly the configuration the practice forbids — and string inequality cannot see this,
  because the models are opaque to the control plane. So the harness-inequality rule is
  **family-strong only between single-provider harnesses** (`claude` vs `codex`). When either
  side is a multi-provider harness, the workflow-declared pairing (§2) SHOULD name a specific
  `starting_model` from a different family, and — like the `/model` case — that is a **soft
  contract** in the skill/pairing declaration, not a control-plane guarantee. This ADR
  deliberately adds **no** enforcement machinery for it: encoding provider→family knowledge in
  the control plane would mean interpreting model identity, which the determinism invariant
  forbids. The honest guarantee is: harness inequality is enforced; family inequality is
  advisory whenever a multi-provider harness is in play.

**Where the pairing comes from.** Enforcement (the inequality MUST) and selection (which
harness/model to review *with*) are separated:

- The invariant is the **validation rule above** — always enforced, workflow-independent.
- The *choice* of reviewer pair SHOULD be **workflow-declared**: the authoring workflow declares
  a review pairing policy (e.g. a `review_harness`/`review_model`, or the sentinel "any
  registered harness ≠ the author's"). This reuses the existing `default_harness`/`default_model`
  declaration pattern on `Workflow` and its paired-registration validation.
- A repo MAY override the pairing (repo config), the same way a repo overrides
  `default_harness`. Repo config is a *convenience* input to selection; it MUST NOT be able to
  weaken the inequality invariant.

If selection yields a pair that fails the inequality rule (e.g. the declared review harness
equals the authoring harness), creation MUST fail closed rather than silently review with the
same model.

### 3. The reviewer receives a diff; it returns a verdict — all on existing primitives

The design invents no new persistence. It maps entirely onto artifacts, responsibilities, turn,
claims, and the governor link.

**What the reviewer receives.** The review task's container starts clean (that is the guarantee)
and pulls everything it needs over MCP/REST against the *authoring* task's id (governor link in
hand):

- the **diff / branch / PR** — via the authoring task's recorded `url` (`gh pr diff <url>`) or
  its `branch`/`clone`. This is what it reviews.
- the **`plan.md` artifact** — via `list_artifacts` + the returned MCP URI — so it can judge
  *scope* (did the change do what was planned, and no more?), not just local correctness.
- It MUST NOT receive the authoring conversation. Clean context is the feature.

**What the reviewer returns.** Two outcomes, exactly as the orchestrator's `review-task` skill
already models:

- **Approve** — the reviewer states approval and writes **no** findings artifact; the review
  task advances to `COMPLETE`.
- **Findings** — the reviewer writes a **`review.md` verdict artifact to the authoring task**
  (`put_artifact(task_id=<authoring id>, name="review.md", …)`), structured `Must fix` /
  `Suggestions`, with correctness *and* simplicity/net-LoC findings (the skill instructions
  carry the criteria, including the simplicity ladder), then advances to `COMPLETE`.

**How the verdict gates the author.** The authoring task's `REVIEW` state carries a
responsibility — `review-addressed` — that the *authoring* agent resolves:

- On entering `REVIEW`, the authoring workflow's `on_transition` lifecycle hook (deterministic,
  control-plane, no LLM — it only writes DB rows / creates a task) MUST create the governed
  review task (§1) with the validated reviewer pair (§2), and SHOULD set the authoring task
  `blocked` after the transition's automatic stale-block clear so the dashboard shows a fresh
  waiting marker for review. A later turn-to-agent write clears that marker presumptively; an
  agent still waiting on the reviewer sets it again explicitly.
- When the review task completes: if it approved (no `review.md`), the authoring agent resolves
  `review-addressed` = `MET` and `advance`s (`REVIEW → MERGING`). If it left findings, the
  authoring agent implements the `Must fix` items — a **free move back to `ITERATING`**
  (`set_state`, ungated), exactly as going-back-to-coding already works — then returns to
  `REVIEW`, which re-runs the hook and spawns a **fresh** review task.
- A fresh review task per round is deliberate: it is the cheapest way to guarantee each round's
  reviewer starts from a clean context (a re-used container would accumulate conversation). The
  round count is visible as sibling review tasks in the ensemble.

**Generation is never review (MUST).** The review task's agent MUST NOT edit the authoring
task's code — it only reads and writes `review.md`. Fixes are made by the authoring agent and
re-reviewed by a new review task on the other side. This is the invariant that makes the
reviewer's own fixes non-authoritative until the other model clears them.

Turn and claims need no change: the review task is independently claimed and spawned by a runner,
holds its own turn, and heartbeats its own liveness. The authoring task can explicitly remain
`blocked` while it waits; a user-to-agent turn handoff or its next state change clears that marker
automatically.

### 4. Failure modes resolve to free moves and the human — never a wedged task

The stage MUST degrade gracefully; no failure may leave a task unable to progress.

- **Reviewer refutes everything / review deadlock (author and reviewer disagree indefinitely).**
  The loop is bounded two ways. (a) The user is never boxed in: `REVIEW → MERGING` is reachable
  by a **free move** (`set_state`, ungated) — the operator can accept the change over the
  reviewer's objection, or `drop` the review task. (b) The `review` skill SHOULD note a soft
  round cap (e.g. surface "N rounds, still contested" to the operator) so a genuine standoff
  escalates to the human rather than burning tokens forever. The human is the tiebreaker,
  consistent with "the user is never boxed in."
- **Model / harness unavailable.** A review task is an ordinary task; if its harness can't be
  spawned it simply reads `down`/`failed` in the ensemble like any other container — it does not
  block the control plane, which made no call. The operator MAY (a) re-target the review to
  another available different-harness pair, (b) fall back to `github-self-reviewed` (the user
  self-reviews), or (c) free-move `REVIEW → MERGING`. The authoring task is never wedged waiting
  on an absent model.
- **No second harness registered at all.** If the registry offers no harness ≠ the author's, the
  inequality rule (§2) makes review-task creation fail closed. The `on_transition` hook MUST
  treat that failure as non-fatal to the authoring task — it SHOULD record the reason (e.g. a
  note/artifact) and leave the `REVIEW` state advanceable by the user (a free move / self-review
  fallback), rather than raising and stranding the transition.
- **Review task dropped mid-flight.** `drop` on the review task is the universal escape; the
  authoring task's `review-addressed` responsibility is then resolved by the operator's judgment
  (a free move or an explicit `MET`/`FAILED`), never auto-forced.

## Rejected alternatives

- **Review as a state the authoring agent resolves (a `REVIEWING` state, `advanced_by = AGENT`,
  responsibility = "findings resolved").** Rejected as the *primary* mechanism because it
  structurally cannot deliver the two non-negotiable properties: the review would run **on the
  authoring model, in the authoring conversation**. It reintroduces exactly the same-model,
  anchored-context review the practice exists to eliminate. (We keep a `REVIEW` *gate* state —
  but as the consumer of an external verdict, not the producer.)

- **A single long-lived review task re-reviewing each round via `/clear`.** Rejected: it leans
  on a harness-specific context-reset command and still risks state bleed between rounds, when a
  fresh task guarantees a clean context for free and costs only a cheap container spawn.
  Reviewing is the expensive-to-get-wrong step; pay a spawn to keep it honest.

- **Enforcing "different model" by having the control plane inspect/choose models.** Rejected:
  any control-plane logic that *reasons about* which model is "different enough" edges toward
  interpreting model identity, and the temptation to ask a model "are these different?" would
  violate the determinism invariant. Opaque **string inequality on a recorded field** is the
  whole enforcement — deterministic, trivial, and sufficient.

- **"Different model" as a soft convention in the skill instructions only (no create-time
  rule).** Rejected: the cross-model property is the *point* of the stage; leaving it to prose
  the agent might ignore makes the guarantee unfalsifiable. A validation MUST at creation is
  cheap and makes same-model review impossible by construction (for the *starting* pair).

- **A dedicated reviewer *harness/service* outside the task model.** Rejected as needless
  invention: a review task on a different harness already *is* a different-model reviewer in a
  clean context, and it inherits claims, liveness, artifacts, turn, and the ensemble view for
  free. Inventing a parallel reviewer subsystem duplicates all of that.

- **Author picks its own reviewer at runtime (authoring agent calls `create_task`).** Rejected:
  `create_task` is gated to `orchestrates = True` workflows, and — more importantly — letting the
  author choose its reviewer invites choosing a lenient one. Deterministic creation by the
  `on_transition` hook, under the workflow-declared/validated pairing, keeps reviewer selection
  out of the author's hands.

## Consequences

**Positive**

- The review discipline that already works in practice becomes **workflow-declared machinery**:
  cross-model, clean-context, correctness-*and*-simplicity, generation-never-reviews — all
  enforced or encoded rather than remembered.
- **Zero new persistence.** It composes `governor_task_id` and `blocked` (the authoring↔review
  link and the "awaiting review" marker), `harness`, `starting_model`, artifacts
  (`plan.md`/`review.md`), responsibilities, and the `on_transition` hook. The only genuinely new pieces are a small `review` workflow, one `REVIEW`-state
  responsibility, the pairing declaration on the authoring workflow, and the create-time
  inequality check.
- The determinism invariant is untouched: the control plane creates tasks and compares strings;
  every LLM call stays inside a container.
- Reviewer selection and enforcement are cleanly separated — repos/workflows tune *who* reviews;
  the invariant *that* it's a different model is non-negotiable and fails closed.

**Negative / limitations**

- **`starting_model` is a starting hint.** The control plane guarantees the reviewer *starts* on
  a different harness/model; it cannot prevent an agent switching models mid-review. Full
  enforcement would need harness cooperation (report/lock the active model), out of scope here.
- **Multi-provider harnesses weaken the guarantee to family-advisory (§2).** Because `pi` (and
  `outfitter` over it) can run Anthropic/OpenAI/Gemini/… models, a different *harness* is not
  always a different model *family*; the family guarantee is control-plane-strong only between
  single-provider harnesses (`claude` vs `codex`). Where a multi-provider harness is used, a
  different family rests on the workflow-declared pairing naming a specific `starting_model` — a
  soft contract, like `/model`. Enforcing it would require the control plane to interpret model
  identity, which the determinism invariant forbids.
- **Cost.** A fresh review task per round means extra container spawns. This is a deliberate
  trade — reviewing is the expensive-to-get-wrong step, and clean context is cheap to buy — but
  ensembles with many contested rounds will show many sibling review tasks.
- **Only workflows that declare it get it.** `github-self-reviewed` intentionally has no `REVIEW`
  state; this ADR does not add cross-model review there. Adopting it elsewhere is per-workflow
  opt-in (the `REVIEW` state + the pairing declaration).
- **Second-harness dependency.** The strongest guarantee requires ≥2 registered harnesses with
  working credentials. Single-harness deployments fall back to self-review or free moves; the
  stage is a no-op-with-a-reason there, not a hard failure.

## Related

- ADR 0004 — the workflow abstraction: states, responsibilities, the deterministic
  `on_transition` lifecycle hook, skills as in-container procedures, and the harness seam this
  stage builds on.
- ADR 0008 — the determinism invariant (control plane makes no LLM calls) that constrains how
  "different model" is enforced.
- ADR 0005 — composed images (base → harness → workflow → repo); the review task's harness layer.
- `github-peer-reviewed` / `github-self-reviewed` workflows — the `REVIEW` gate state this
  repurposes, and the self-review fallback.
- The `orchestrator` workflow's `review-task` skill and `review.md` artifact — the review
  procedure and verdict format this generalizes into a first-class stage.
- Governor / ensemble machinery (`governor_task_id`) — the authoring↔review task link and its
  dashboard rendering.
- Concrete shapes (the `review` workflow class, the `REVIEW`-state responsibility key, the
  pairing-declaration attribute, and the create-time validation) belong in
  `docs/design/ARCHITECTURE.md` and the workflow classes.

## Summary of key decisions

1. **Review is a separate governed review task, not a state the author resolves.** Clean context
   + different model are properties of a second container; a task is one container, so the
   reviewer MUST be a distinct task governed by the authoring task. The authoring workflow keeps a
   `REVIEW` state as the *gate that consumes the verdict*.
2. **"Different model" is a deterministic create-time validation rule** — the review task's
   `(harness, starting_model)` MUST differ in at least the harness, enforced by opaque string
   inequality (no LLM). *Selection* of the pair is workflow-declared (repo MAY override);
   *enforcement* is non-negotiable and fails closed.
3. **The reviewer receives the diff/branch + `plan.md`; it returns a `review.md` verdict on the
   authoring task** and gates the authoring `REVIEW` state's `review-addressed` responsibility —
   all on existing primitives (artifacts, responsibilities, turn, claims, governor,
   `on_transition`). The review task is spawned by the deterministic `on_transition` hook on
   entering `REVIEW`.
4. **Generation is never review:** the reviewer MUST NOT edit code; fixes are made by the author
   and re-reviewed by a fresh review task on the other side, round by round.
5. **Every failure mode degrades to a free move or the human** — refute-everything/deadlock,
   model-unavailable, no-second-harness, dropped-review — so a task is never wedged and the user
   is never boxed in.
