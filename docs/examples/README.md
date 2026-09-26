# Examples of the evolution loop's output

Each week the evolution agent reads the recent trading cycles, the scored
record of every signal, and the source code, then writes its findings into the
data folder for a human to review (see [architecture](../architecture.md#the-self-evolve-loop)).
These are two real outputs, lightly edited and anonymised:

- [A strategy proposal](evolution-proposal-swing-failure-reversal.md) — a new
  rule-based strategy, designed from what separated the system's few good trades
  from a run of bad ones, with its replay expectations and test gates. Its note
  shows how the live record later exposed a flaw that the design did not.
- [A code review](code-review-composite-abstention.md) — a bug in how the
  algorithm combined its strategies' votes, with severity-ranked findings and
  suggested diffs, all three of which were implemented.

Proposals like these wait for you in the console (**Memory & Notes →
Proposals**). Nothing in them changes the running system until a person
approves and implements it.
