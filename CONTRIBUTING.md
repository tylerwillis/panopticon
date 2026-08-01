# Contributing

This repository is an **experimental fork** of
[unsupervisedcom/panopticon](https://github.com/Unsupervisedcom/panopticon). It exists so I can
explore new features against my own fleet before upstreaming them. Branches here move fast, get
rebased, and sometimes get abandoned.

## Pull requests are not accepted here

Please open pull requests against the upstream project instead:
**[Unsupervisedcom/panopticon](https://github.com/Unsupervisedcom/panopticon)**.

This isn't about the quality of the contribution. Work merged into this fork doesn't reach other
users — it either lands upstream later or gets dropped — so a PR here is effort spent in a place
it can't benefit anyone but me. Upstream is where a fix becomes permanent.

There's a second reason worth being plain about: this fork drives a live fleet of coding agents in
containers that hold real credentials, and merged code runs on that machine. Keeping the merge
surface closed to outside changes is a deliberate boundary, not a judgment about who's asking.

## What is welcome

- **Using it.** Clone it, run it, break it.
- **Issues.** Bug reports, reproductions, and questions are genuinely useful, including for
  behavior you find in this fork specifically. If a report turns out to affect upstream too, it's
  worth filing there as well so the fix outlives this fork.
- **Discussion.** If you've found something interesting about how the fleet behaves at scale, I
  want to hear it.

## If you already opened a PR here

Thank you, and sorry for the wasted trip — this policy was documented after the fact. The change
is not lost: reopen it against
[Unsupervisedcom/panopticon](https://github.com/Unsupervisedcom/panopticon), where it can be
reviewed and released properly.
