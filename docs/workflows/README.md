# Workflows: choosing how a task runs

Every task runs a **workflow**: the lifecycle that decides what states the task moves
through, **who advances each one** (you or the agent), what the agent must finish before
it can move on (its *responsibilities*), and which extra *skills* the agent has in its
container. Picking a workflow is how you set the line between what the agent may do on
its own and what needs your sign-off.

You choose a workflow when you create a task. On the dashboard press `n`, pick the repo,
then pick the workflow. This page is the catalog; each workflow has its own page with
the details. For the *task* itself — the object a workflow drives, its properties, and its
lifecycle — see [Tasks](../tasks.md).

## The built-in workflows

| Workflow | What it does, and when to use it | Where it ships |
|---|---|---|
| [`2119-human-spec`](2119-human-spec.md) | Defines the change as an RFC 2119 spec and reviewed tests before implementation. **You approve the spec** and the final reviewed PR. | A spec-backed GitHub PR |
| [`2119-auto-spec`](2119-auto-spec.md) | Runs the same spec-first lifecycle but lets the agent advance from specification into implementation. **You approve the final reviewed PR.** | A spec-backed GitHub PR |
| [`2119-auto-sol`](2119-auto-sol.md) | Runs the automatic spec-first lifecycle with two independent Sol reviewer slots by default. Use when same-model review is an intentional cost or availability tradeoff. | A spec-backed GitHub PR |
| [`github-peer-reviewed`](github-peer-reviewed.md) | Ships a GitHub PR that a **peer** reviews before it merges. Use for GitHub changes that need a second person's sign-off. | A GitHub PR (peer-approved) |
| [`github-self-reviewed`](github-self-reviewed.md) | Ships a GitHub PR that **you** review yourself, with no peer-review gate. | A GitHub PR (self-approved) |
| [`local-git-self-reviewed`](local-git-self-reviewed.md) | Keeps the work **local**: commits to a branch and merges it, with no GitHub, PR, or CI. Use when the change never leaves the machine. | A local branch merged into the base |
| [`spike`](spike.md) | **Open-ended** agent work with no gates. Use for exploration, debugging, and research, until you call it done. | Nothing lands on its own |
| [`orchestrator`](orchestrator.md) | An agent that **decomposes a goal into child tasks**, each pre-planned and handed to you ready to approve. Use to fan work out across agents. | New pre-planned child tasks |
| [`setup-repo`](setup-repo.md) | A host-side **setup utility** (no container) that dispatches auth setup for the repo's default harness. Launched from the repos screen, not the task picker. | Harness auth in the repo's env-file or credential directory |
| [`review`](review.md) | A hidden worker that reviews a governor task from a clean, different-harness context. An authoring workflow can create it; you do not select it from the task picker. | Approval or a `review.md` artifact on the governor task |

Any task can also be **dropped** at any time (dashboard `x`), which moves it to `DROPPED`
without merging or shipping anything.

## The planning step

Four change-making workflows (`github-peer-reviewed`, `github-self-reviewed`,
`local-git-self-reviewed`, and, per child, `orchestrator`) start in **PLANNING**. Before
the agent can leave that state it must:

- **Write a plan** as the task's `plan.md` artifact. This is your chance to redirect
  before any code is written.
- **Record a token estimate** so the task's projected cost is tracked.

The plan is a task **artifact**, which you read from the dashboard: highlight the task and
press `a` to open its `plan.md`. (Artifacts are the task's own documents, kept with the
task rather than in the repo, so the dashboard is where you read them.)

You approve the plan by advancing the task out of PLANNING (attach with `t`, run
`/advance`). `spike`, `review`, and `setup-repo` have no PLANNING state.

## The 2119 spec-first flow

The three `2119-*` workflows replace PLANNING with a stricter specification pipeline:

```
SPECIFYING → BUILDING → TESTING → REVIEWING → MERGING → COMPLETE
```

In SPECIFYING, the agent writes requirements under `specs/`, annotates a genuine test for
every MUST-level requirement, gets the tests judged from fresh contexts, and publishes the
specification as a task artifact. BUILDING implements only that contract. TESTING runs the
full suite, the 2119 check, the repo's own gate, and PR CI. REVIEWING requires two verified
reviews of the final diff, targeted mutation evidence, and explicit triage before you approve
the PR for MERGING.

The variants differ at two decision points:

- [`2119-human-spec`](2119-human-spec.md) waits for your approval after SPECIFYING;
  [`2119-auto-spec`](2119-auto-spec.md) and [`2119-auto-sol`](2119-auto-sol.md) advance
  automatically.
- `2119-human-spec` and `2119-auto-spec` default their final review slots to Fable plus Sol.
  `2119-auto-sol` defaults both slots to separate Sol runs. Repository reviewer settings can
  override those defaults.

## Who advances a state

Each state is advanced by either **you** or the **agent**:

- **You advance** the foreground states (plan or spec approval, sign-off before merge). The agent
  fills in its responsibilities and then waits; nothing proceeds until you say so. Attach
  with `t` and run `/advance` to approve, and the agent starts a fresh turn.
- **The agent advances** autonomous states. Depending on the workflow, those include
  specification, implementation, testing, and merging. Once a state's responsibilities are
  met, it moves the task on by itself.

## How workflows are offered

- **Default-on vs. opt-in.** `spike` and `orchestrator` are shown for every repo by
  default. The GitHub, local-git, and `2119-*` delivery workflows are **opt-in**: enable
  them per repo before they appear in the task-creation picker.
- **Hidden workflows.** `setup-repo` is launched from the repos screen's setup hotkey.
  `review` can be created as a governed worker by an authoring workflow. Neither appears in
  the normal task picker.

## Definitions and runtime state

Git stores the built-in workflow definitions and their documentation. A running Panopticon
installation keeps its operating state elsewhere:

- **Repository settings** — including workflow enablement and reviewer overrides — and
  **task records** — including lifecycle state and history — live in the task service's
  configured database.
- **Task artifacts** live in the task service's configured artifact store.
- **Secrets and host-resolved configuration files** stay in the runner host's Panopticon
  configuration rather than Git or the task database.
- **Custom workflow modules** loaded from `~/.config/panopticon/workflows/` or
  `--workflows-path` are outside this repository unless you version them separately.

Cloning the repository therefore recreates the shipped workflow definitions, not a running
fleet's repository choices, task history, artifacts, or credentials.

## Adding your own

Workflows are just `Workflow` subclasses. Drop a module defining one into
`~/.config/panopticon/workflows/` (or point the session service at a directory with
`--workflows-path`) and it registers automatically, with no change to the core service. See
[ADR 0004](../design/decisions/0004-workflow-abstraction.md) and
[`docs/design/ARCHITECTURE.md` §7](../design/ARCHITECTURE.md) for the authoring model.
