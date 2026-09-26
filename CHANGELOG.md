# Changelog

Notable changes to EvoTrader are recorded here, newest first. While the
version starts with 0, any release may change behaviour, settings or the data
folder layout; each entry says what you need to do.

## 0.2.0 — first public release (unreleased)

The first public version. Development before this happened in a private
repository, and that history is not included.

The project was called **Gold Digger** in private development; it was renamed
EvoTrader for this release. If you ran a private copy: the package is now
`evotrader` (was `gold_digger`), environment variables start `EVOTRADER_`
(were `GOLD_DIGGER_`), the databases are `evotrader*.db` (were
`gold_digger*.db`), the broker sign-in cache is `~/.evotrader/` (was
`~/.gold_digger/`), and strategy module paths in `algorithms/*.yaml` start
`evotrader.`. Rename those once, with the app stopped.

### What's in it

- **Trading cycle.** A team of agents built on Google's Agent Development Kit:
  an orchestrator, a news agent, a strategy agent, a risk manager and an
  executor. Each agent's model is set in `settings.yaml`; Google Gemini is the
  default, Anthropic Claude is supported, OpenAI is experimental.
- **Algorithm.** A composite engine that combines ten rule-based strategies with
  weights that depend on the market regime, lets a strategy abstain without
  diluting the others, and keeps its parameters in numbered algorithm versions.
- **Hard limits.** A constitution the agents cannot change, enforced by the
  risk manager's checks and by a risk gate in code on every order; protective
  stop orders are forced to good-till-cancelled; exits are never blocked by
  rules meant for new positions.
- **Approval gate.** Optionally, every order waits for your approval in the
  web console.
- **Practice mode**, the default: a simulated broker that fills orders against
  live Robinhood quotes, with its own journal and memory.
- **Robinhood** through its official MCP server, with sign-in on Robinhood's
  own page; the app never sees your password.
- **Signal attribution.** Every cycle records what the algorithm, the news and
  the strategy agent each said, and scores them against the price one and five
  trading days later.
- **Weekly evolution.** An evolution agent that writes strategy proposals, code
  reviews, carry-forward notes, and new algorithm and instruction versions that
  stay inactive until you approve them. It can run on the API or on a Claude
  subscription through Claude Code, as can the strategy agent.
- **Skills for coding agents.** `self-evolve` for implementing approved
  proposals with tests, plus `signal-validation`, `offline-iteration`,
  `mcp-debugger` and `fresh-start`.
- **Web console.** Dashboard, trade approval, the reasoning of every cycle,
  token usage, review of proposals and versions, and your notes to the agents.
- **Your own data folder**, separate from the code (`GOLD_DIGGER_DATA_DIR` or
  `--data-dir`), created from `starter_data/` by the first-run setup
  (`scripts/setup.sh`, `./run.sh setup`).
- **Offline tools.** An algorithm-only backtester and a scenario harness for
  testing agent instructions.
- **Cost controls.** Prompt caching for Anthropic models, trimming of oversized
  data-provider responses, and per-agent model choice.
- Apache License 2.0, continuous integration, and contributor documentation.

### Project name

The project will be renamed. The new name, and what an existing user needs to
change (environment variables, the data folder, the sign-in cache), will be
recorded here.
