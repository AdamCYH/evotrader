# Settings reference

What each key in `settings.yaml` does, and why it has the default it has. The
file lives in your data folder (see the [README](../README.md#your-data-folder-code-public-data-private));
its own comments give one-line hints, and this page carries the reasoning.

- **Settings are read at start-up.** Restart the app after editing
  `settings.yaml`.
- **Hard risk limits are not here.** They live in `constitution.yaml`, which no
  agent can change. Its `_pct` fields are written in percent (`5` means 5%).
- The schema, with a description of every field, is in
  `src/evotrader/models/config.py`. A value of the wrong type or out of range
  stops the app at start-up with a message naming the field.

---

## 1. Mode and asset

### `mode`

`sim` (practice mode, the default) runs the whole pipeline against a simulated
broker with play money; `live` places real orders. Dry run is not a separate
switch: it simply means `sim`, and an old `dry_run:` block is ignored.
`./run.sh --mode sim|live` overrides this for one run, and `--mock-time` always
forces `sim`. See **Practice mode and going live** in the README before
setting `live`.

### `asset.primary_ticker`

The instrument the system trades. Nothing else in the code or the agent
instructions names a ticker, so changing instrument is a settings change — plus
adding the symbol to `trading_rules.allowed_tickers` in `constitution.yaml`.
`tests/unit/test_ticker_agnostic.py` fails if a literal symbol creeps back in.

Things worth checking when you choose or change the instrument:

- **How much it moves.** A broad index fund typically moves about 1% a day; a
  single volatile stock can move 5–8%. Stops are set in multiples of the ATR
  (average true range, the typical daily range) and scale by themselves, but
  anything written as a flat percentage of price — and the volatility target in
  section 2 — needs re-checking.
- **Whether it trades outside regular hours** (`allow_extended_hours` and
  `extended_hours_tickers` in the constitution), and whether your broker lets
  agents trade it at all.
- **Cost of trading it.** The bid–ask spread is a real cost on every round trip;
  a thin market can cost more than any signal earns.

### `asset.inverse_ticker`

A fund that rises when the instrument falls (for example a fund that tracks the
inverse of an index). It lets the system act on a bearish view by *buying*
something, without short selling (selling borrowed shares, which needs a margin
account) and without options. Leave it `null` when no such fund exists — most
single stocks have none — and the strategy instructions fall back to whatever
the constitution allows (buying puts if options are allowed, otherwise standing
aside). Never guess one: an inverse fund that tracks a different index is a bet
on something else.

### `asset.instruments`

Optional per-symbol notes, keyed by ticker: a short `description`, a `role`
(`primary`, `bearish vehicle`, …) and `leverage`. `leverage` changes the
arithmetic, not just the wording: a fund that moves −2× the instrument carries
twice the exposure per dollar, so it must be sized at half. A symbol without an
entry is shown as a bare ticker with leverage 1.0.

### `asset.universe`

Which other companies can move what you trade — used to spot earnings events
that matter and to trim market-wide research data down to relevant symbols.
It is resolved from live fund-holdings data for `primary_ticker`; a single
stock resolves to itself. `top_n` and `min_weight_pct` (a percentage of the
fund, `1.0` = 1%) choose the relevant holdings. `cache_ttl_days: 7` because
holdings change slowly, and a stale list is still better than none if a
refresh fails.

---

## 2. Position sizing

### `position_sizing.volatility_target_annual`

How much yearly price swing the sizing rule is willing to hold, as a fraction
(`0.15` = 15%). Each cycle the strategy agent can ask for a **risk budget**:

```text
risk budget = volatility_target_annual ÷ the instrument's recent annualised volatility
```

kept between a floor of 0.30 and the constitution's `max_total_exposure_pct`.
The agent then sizes a trade as roughly *risk budget × conviction* — smaller
when the market is wild, larger when it is calm.

The target only makes sense relative to the instrument:

| Instrument's yearly volatility | Target 0.15 gives | Target 0.40 gives |
|---|---|---|
| ~20% (a broad index fund) | 0.75× | 2.0× → **capped** at `max_total_exposure_pct` |
| ~80% (a volatile single stock) | 0.19× → **floor 0.30×** | 0.50× |

When the floor applies on every cycle, the budget is a constant rather than a
measurement and stops telling calm markets from wild ones. **Re-derive this
number whenever you change instrument**, and check what a typical day and a
bad day would do to the account at full size before raising it.

The other `position_sizing` keys are not read by the code (see
[Accepted but unused](#accepted-but-unused)).

---

## 3. Schedule

Times are five-field cron expressions (`minute hour day month weekday`) in US
Eastern time.

| Key | What it does |
|---|---|
| `description` | One sentence describing the cadence. It is given to the agents (the `{{SCHEDULE}}` placeholder) so they can judge how long a position may sit before the next look. |
| `cycle_cron_enabled`, `evolution_cron_enabled` | Whether the console's **AUTO** switches for trading cycles and evolution start switched on. |
| `cycle_cron` | When trading cycles run. A single expression or a **list**: a cycle fires when any entry matches. A list is the only way to say "every hour during the session, plus once after the close", because those differ in both the minute and the hour field. |
| `evolution_cron` | When the weekly evolution run starts. |
| `metrics_cron` | When the daily portfolio snapshot for the account chart is taken. |
| `max_cycle_events` | Tool calls and messages allowed in one cycle before it is stopped — a guard against an agent stuck in a loop. |
| `provider_retry_delay_seconds` | When a cycle stops on a temporary AI-provider error (overloaded, rate-limited, timed out) before any order could have been placed, wait this long and resume the same cycle once. 0 disables it. |
| `max_evolution_duration_seconds` | Time limit for one evolution run. |
| `overnight_interval_seconds` | Cycle interval the agents are told to expect outside regular hours. |

**Why evolution runs weekly, not daily.** It is an evidence decision, not a
cost one. A system that changes its exposure a few dozen times a year closes
few trades in a day, so most daily runs would have nothing new to learn from
and would invent work from the same data. Five runs a week, each proposing
changes from a fifth of the evidence, is worse than one run on a full week. If
you move it to daily, first add a rule that a run with no new closed trades
does nothing.

---

## 4. Agent runtimes

An agent's model can be reached in two ways:

| Runtime | How | Billing | What telemetry sees |
|---|---|---|---|
| `api` | ADK and LiteLLM call a completion endpoint | Per token | Every call and every token (the console's Token Usage panel) |
| a `cli_runtimes` entry | A coding-agent harness (Claude Code) run as a subprocess | Your subscription's usage limits | Turns and totals; the harness owns the loop |

Only the **strategy** and **evolution** agents can use a CLI runtime. Setting
one for another agent is logged as an error and ignored: the other agents are
always built for the API runtime, and the ones that hold broker tools or the
risk gate could not be hosted safely without new plumbing. Setup is in
[claude_code_evolution.md](claude_code_evolution.md).

### `agent_runtime`

Which runtime each agent uses: `api`, or the name of a `cli_runtimes` entry.
Agents not listed use `api`, so a typo in an agent name can never move an agent
onto a different runtime.

The strategy agent is the safe one to host on a harness: none of its tools can
place or cancel an order. It reads market data and positions, computes the risk
budget, logs its signal record, searches past trades and your notes, stores
learnings, and writes the note for the next cycle. It *proposes*; the risk
manager checks; the executor places, behind the risk gate. The API agent stays
built as a fallback, so a quota wall or a harness failure costs a different
model for one cycle, never a missed decision.

### `cli_runtimes.<name>`

The key names the runtime; `driver` names the adapter that implements it
(`claude_code` today). Two runtimes can share a driver with different models —
for example one for the strategy agent, which runs every cycle, and a stronger
one for the weekly evolution run.

| Key | Meaning |
|---|---|
| `billing` | `subscription`, `api` or `auto` — see below |
| `model` | Harness model alias (`opus`, `sonnet`, `haiku`) or a full model id; empty for the harness default |
| `thinking` | `default` (send nothing; recommended), `adaptive`, `off`, or a token budget |
| `effort` | `low`, `medium`, `high`, `xhigh`, `max`, or `null` |
| `max_turns` | Hard cap on agent turns, so a runaway run cannot use up the plan's window |
| `permission_mode` | `dontAsk` (deny anything not explicitly allowed) is right for unattended runs |
| `allow_file_tools` | Let the harness read the repository with its own read/search tools. Writing files, running commands and web access are always blocked |
| `quota.warn_utilization` | Once the harness reports this share of the usage window consumed, send the *next* task to the fallback. The current task finishes where it started |
| `quota.on_exhausted` | At the wall: `api` re-runs the task on the metered API (costs money, keeps trading), `skip` drops it (free, loses the cycle), `fail` raises so a person notices |

### Billing is enforced, not assumed

**This is the most important thing in this section.** Coding-agent harnesses
accept both a subscription login and an API key, and **silently prefer the API
key** when one is in the environment. Because the app loads
`ANTHROPIC_API_KEY` from `.env` for its API agents, a "subscription" run can
quietly bill the API — with no error, no log line and no difference in
behaviour. That is exactly what happened in this project's own use for about a
week before it was noticed.

So `billing: subscription` **withholds** the API key from the harness process
(it passes an empty value, which the CLI treats as unset, without touching the
app's own environment, where the API fallback still needs the key). API billing
becomes impossible: a run that succeeds is proof that the subscription paid for
it, and a missing login fails loudly instead of charging you.

| `billing` | Behaviour |
|---|---|
| `subscription` | Withhold the API key; the run fails if the harness is not logged in |
| `api` | Bill per token |
| `auto` | Try the subscription; fall back to the API on an **authentication** failure only (never on a quota wall), and log which was used |

Use `auto` until `claude setup-token` has been run and `CLAUDE_CODE_OAUTH_TOKEN`
is in `.env`, then switch to `subscription`. Each run records which billing was
actually used.

### Thinking costs real money

Models that "think" before answering bill that private reasoning as output,
the most expensive kind of token. Measured on the strategy agent, about 70% of
its output tokens were private reasoning. It is worth paying for on the agent
that makes the trade decision; it is much less clearly worth it on mechanical
steps. `default` lets the harness decide; pinning a value you did not choose
means a future default change can quietly rewrite your costs.

---

## 5. Models (API runtime)

```yaml
model:
  live:
    default: "..."           # used by every agent left null
    orchestrator: null
    strategy_agent: "..."
    news_sentiment_agent: null
    risk_manager_agent: null
    executor_agent: "..."
    evolution_agent: "..."   # also the fallback when a CLI runtime is unavailable
  sim:
    default: "..."
```

- With separate `live` and `sim` blocks, each mode uses its own; a single flat
  block applies to both. `null` means "use `default`".
- Gemini model names are used as they are. Other providers take a prefix and go
  through LiteLLM: `anthropic/<model>` needs `ANTHROPIC_API_KEY`,
  `openai/<model>` needs `OPENAI_API_KEY` (experimental). Gemini needs
  `GEMINI_API_KEY`.
- Put the money where reasoning converts into results: the strongest model on
  the strategy agent (the trade decision) and the evolution agent (reading code
  and results), a fast model for routing and news, and the cheapest one for the
  executor, whose job is mechanical tool calling.
- `./run.sh setup` writes a sensible set for the provider you choose.

---

## 6. Prompt caching (Anthropic models, API runtime)

Caching is a **prefix match** over `tools → system prompt → messages`. A cache
breakpoint stores everything up to itself, and the next request that starts
with the same prefix reads it at about a tenth of the normal input price. ADK
resends the whole conversation on every agent turn, so without a breakpoint
inside the messages the growing history is billed in full every turn.
`message_breakpoints` fixes that.

| Key | Meaning |
|---|---|
| `enabled` | Insert cache breakpoints at all |
| `message_breakpoints` | Breakpoints in the message history, newest turn first. Anthropic allows 4 per request and the system prompt uses one, so 3 is the maximum; 2 is recommended |
| `min_cacheable_chars` | Skip message breakpoints while the history is short, so a brief exchange doesn't pay for a cache write nothing reads |
| `require_multi_turn` | Don't cache a session's very first call, whose cache would never be read |
| `ttl` | `5m` (writes cost 1.25×) or `1h` (writes cost 2×). Reads cost 0.1× either way |

**Why not always `1h`?** A read refreshes the window, so with calls less than
five minutes apart a 5-minute entry never expires and the long TTL only costs
more. The long TTL pays off only for a prefix that is both stable and reused
after a gap of more than five minutes and less than an hour. The system prompt
and tools qualify; the tail of the conversation changes every cycle and is
never read again. With hourly cycles, the first call of every cycle misses a
5-minute cache.

**What it buys.** Most on the evolution agent's long runs, where well over 90%
of the input was served from cache in measured runs. Little on the strategy
agent, whose conversations are short.

---

## 7. Data sources

### `mcp.providers` and `mcp.roles`

MCP (Model Context Protocol) servers the agents use, bound to roles and listed
primary first. The `trading` role reaches the broker (quotes, orders,
portfolio); `research` reaches news and analysis. Each agent receives only the
toolsets for its role. Robinhood uses browser sign-in (`auth: "oauth"`), so no
broker token goes in `.env`. Alpha Vantage reads `ALPHA_VANTAGE_API_KEY`
(several keys, comma-separated, are rotated); `stock_analysis` is a local Yahoo
Finance server that ships as a dependency.

### `mcp.tool_filter`

Hides tool descriptions an agent cannot use, because every tool's description
is sent with every request and a broker exposes dozens of tools. Measured
before the filter, the executor carried more than 100,000 tokens of tool
descriptions on every call.

It is **exclusion-only by design**: `exclude` lists name fragments to hide per
agent, and anything not matched stays visible. A pattern that misses costs
tokens; the opposite mistake — an allow-list that forgets a tool — would leave
an agent silently unable to place an order. `always_keep` names tools that are
never hidden whatever the patterns say, as a backstop for the order path.

### `mcp.curation`

Some research endpoints return enormous responses. Economic indicator series,
for example, take no date range and return decades of data on every call —
one measured response was about 260,000 tokens. Curation trims them as they
arrive. Only the news agent reaches the curated tools, and no indicator or
algorithm uses those feeds (every quantitative signal comes from price and
volume), so curation cannot change a trading signal.

| Key | Note |
|---|---|
| `enabled` | Master switch |
| `dedupe_structured_content` | MCP servers often send each payload twice, as text and as structured data; about 46% of measured MCP bytes were that duplicate. Dropped only when the two are byte-identical, so no information is lost — which is why it is also safe on trading tools |
| `timeseries_max_rows: 120` | About six months of daily data, plus a summary of the **whole** history (latest value, 1-week to 1-year changes, all-time and 1-year highs and lows), so "near a one-year high" still works |
| `news_max_age_days: 21` | Keep news by age, not count: the news agent treats anything older than about five sessions as already priced in, and an age window adapts to how busy the week was. 0 turns it off |
| `news_max_articles` | Hard ceiling after the age window; 0 = none |
| `news_min_articles: 8` | Floor, so a quiet week never leaves the agent with nothing |
| `news_prefer_latest` | Ask news endpoints to sort by recency where their schema allows |
| `news_filter_ticker_sentiment` | Keep per-article sentiment rows only for symbols in the resolved universe. Titles, summaries and overall sentiment are untouched, and it switches itself off if the universe can't be resolved |
| `backstop_max_chars: 40000` | Safety net for tools with no curator: cut on a whole-entry boundary, and say so in the payload. 0 turns it off |
| `backstop_roles: [research]` | Research only. Trading tools return positions, orders and quotes, which are bounded, and where a partial answer is more dangerous than a large one |

Measured on the largest real payloads seen, curation cut them by about 94%.

---

## 8. Evolution

Which runtime the evolution agent uses is set in section 4.

| Key | Enforced? | Meaning |
|---|---|---|
| `auto_promote: false` | Yes | New algorithm and instruction versions stay inactive until you activate them in the console. Keep it `false` |
| `max_instruction_changes_per_week` | Yes | How many new instruction versions the evolution agent may propose per agent per week. Prompts need longer observation than parameters |
| `backtest_min_days`, `max_algo_changes_per_day`, `rollback_threshold_sharpe_diff` | **No** | Accepted, but no code reads them today; the evolution agent sees them only if its instructions mention them |

The older keys `evolution.backend` and `evolution.claude_code` still load: they
are migrated to `agent_runtime.evolution` and `cli_runtimes` with a warning
naming the replacement.

---

## 9. Simulated broker

| Key | Meaning |
|---|---|
| `slippage_model` | `spread` (default): buy at the ask and sell at the bid of the live quote, as a real market would. `fixed`: the quoted price moved against you by `slippage_bps`. `none`: fill at the quote with no cost — for debugging only; it flatters every strategy |
| `slippage_bps` | Used by `fixed` (1 basis point = 0.01%). Options get twice this |
| `auto_close_expired_options` | Close option positions at expiry in the simulation |

Cost assumptions decide results more often than strategy parameters do. In one
backtest the same strategy on a crypto asset went from a gain to a loss when
the assumed cost per trade rose from 2 to 10 basis points — and the real
measured spread was many times wider than either.

---

## 10. Web console and logging

| Key | Meaning |
|---|---|
| `require_trade_approval` | Default `true`: every order that passes the risk checks waits for **Approve** or **Reject** in the console. It works only while the console is running; `./run.sh offline` does not wait |
| `logging.level` | `INFO` by default; `DEBUG` for much more detail |

The console password (`DASHBOARD_PASSWORD`) and port (`EVOTRADER_PORT`) are
environment variables, not settings — see below.

---

## Environment variables

Set in `.env` (the data folder's `.env` overrides one in the project folder),
except `EVOTRADER_DATA_DIR`, which must be set in your shell because it says
where that `.env` is. A variable already set in your shell wins over both files
(the app says so when the values differ), so `EVOTRADER_PORT=8081 ./run.sh`
works for one run. An empty value (`KEY=`) means not set.

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | AI provider keys; you need the ones your `model:` choices use |
| `ALPHA_VANTAGE_API_KEY` | Optional market-data key; comma-separate several to rotate them |
| `DASHBOARD_PASSWORD` | Console password. Empty means no login at all — see [SECURITY.md](../SECURITY.md) |
| `EVOTRADER_PORT` | Console port (default 8080) |
| `EVOTRADER_HOST` | Where the console listens: empty = only this computer (`127.0.0.1`); `0.0.0.0` = other devices too, which requires `DASHBOARD_PASSWORD` |
| `EVOTRADER_CORS_ORIGINS` | Comma-separated web addresses allowed cross-origin access to the API, for a separate front end in development (default: none) |
| `EVOTRADER_DATA_DIR` | Data folder location (`--data-dir` overrides it for one run) |
| `EVOTRADER_SIGNIN_DIR` | Where the broker sign-in is kept (default `~/.evotrader`); the test suite points it at a temporary folder |
| `EVOTRADER_MOCK_TIME` | Pretend it is this time (ISO 8601); forces practice mode |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude subscription token for a CLI runtime on a machine without an interactive login (`claude setup-token`) |
| `ROBINHOOD_AUTH_TOKEN` | Leave empty. A static token that overrides browser sign-in |

---

## Accepted but unused

These keys load without error, so older settings files keep working, but no
code reads them today. Changing them does nothing.

- `strategy:` — `entry_threshold`, `min_confidence`, `regime_detection`,
  `algo_weights`, `regime_weights`. From an earlier design: the algorithm's
  weights now live in algorithm versions (`algorithms/<version>/config.yaml`),
  and the strategy agent makes the final call.
- `position_sizing.method`, `position_sizing.max_position_pct`,
  `position_sizing.default_stop_loss_atr_multiplier`. Position limits that
  matter are in the constitution.
- `schedule.max_cycle_duration_seconds`, `logging.trade_journal_verbose`.
- `portfolio_cache_seconds` (the console caches the account value per
  half-hour of the clock) and `dashboard_poll_interval_seconds`.
- `dry_run:` — replaced by `mode`.
