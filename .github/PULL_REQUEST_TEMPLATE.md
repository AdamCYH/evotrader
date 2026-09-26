## What and why

<!-- What does this change, and why? Link the issue if there is one. -->


## Checklist

- [ ] Tests added. For a bug fix, the new test failed before the fix.
- [ ] `uv run pytest -q` passes.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` pass.
- [ ] No live data: no `.env` files, keys, tokens, databases, logs with account
      details, account numbers, order ids, balances, or screenshots of an
      account — in the code, tests, docs or this description.
- [ ] If this touches the order path (order tools, the risk gate, the executor,
      reconciliation, the simulated broker): verified on a practice cycle
      (`./run.sh --mode sim`, then **Trade**), and I describe what I checked below.
- [ ] Docs and `CHANGELOG.md` updated if behaviour or setup changed.

## How I checked it

<!-- Tests run, practice cycle observed, anything a reviewer should look at. -->
