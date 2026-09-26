# Running agents on a Claude subscription (Claude Code)

Two agents can run through **Claude Code** — Anthropic's `claude` command-line
agent — so that they use your Claude subscription's usage limits instead of
per-token API billing:

| Agent | How often it runs | Notes |
|---|---|---|
| **evolution** | Weekly | The natural fit: its job is reading code and results and writing proposals, which is what Claude Code is built for, and a stalled run can simply be retried |
| **strategy** | Every trading cycle | Optional. It cannot reach the broker (it only proposes), and an API agent stays ready as a fallback |

Every other agent always runs on the API. The choice is made per agent in
`settings.yaml`, and both runtimes produce the same proposals, database rows
and console timeline.

---

## Quick setup

```bash
uv sync --all-extras                           # installs claude-agent-sdk
node --version                                 # must be 18 or newer
npm install -g @anthropic-ai/claude-code
claude                                         # log in once with your subscription, then /exit
uv run python scripts/setup_claude_code.py     # preflight check
```

`./run.sh setup`, in its detailed mode, can write the settings below for you.
To do it by hand, add a runtime and point agents at it in `settings.yaml`:

```yaml
cli_runtimes:
  claude_code:
    driver: "claude_code"
    billing: "subscription"   # withhold the API key: a run that succeeds was paid by the subscription
    model: "opus"             # alias (opus, sonnet, haiku) or a full model id; "" = harness default
    thinking: "default"
    max_turns: 80             # hard cap, so a runaway run cannot use up your window
    permission_mode: "dontAsk"
    allow_file_tools: true    # read and search the repository; writing is always blocked
    quota:
      warn_utilization: 0.85  # near the limit, send the NEXT task to the API fallback
      on_exhausted: "api"     # at the wall: api (costs money) | skip | fail

agent_runtime:
  evolution: "claude_code"
  strategy: "claude_code"     # optional
```

Restart the app: the runtime is chosen at start-up. You can define two entries
with different models — for example a cheaper one for the strategy agent, which
runs every cycle, and a stronger one for the weekly evolution run. Every key is
explained in [settings_reference.md](settings_reference.md#4-agent-runtimes).

### On a machine without a browser

Mint a long-lived token where you *can* log in, and put it in the `.env` of the
machine that runs the app:

```bash
claude setup-token
# then, in .env:
CLAUDE_CODE_OAUTH_TOKEN=<the token>
```

### Checking it works

The start-up log states what was **requested**:

```text
Evolution runtime: claude_code driver=claude_code model=opus billing=subscription (requested; actual billing is logged after the run)
Strategy runtime: claude_code model=opus billing=subscription ...
```

and each run logs what **actually happened**, for example
`strategy: hosted run complete — turns=… billing=subscription notional_cost=$…`
(on a subscription the cost is *notional*: what the run would have cost through
the API). Then press **Evolve** in the console and watch the timeline: it should
look like any other run.

`scripts/setup_claude_code.py` checks the Python package, the CLI, Node and your
login. Two of its checks predate the `agent_runtime` setting: it warns when
`ANTHROPIC_API_KEY` is set (harmless with `billing: subscription`, which
withholds the key from Claude Code) and when the old `evolution.backend` key is
missing (also harmless when `agent_runtime` names a Claude Code runtime). The
start-up log is the authority.

If the SDK or the CLI is missing, the app logs a **COST WARNING** and uses the
API runtime instead, rather than losing the agent. Check the log if you
expected the subscription and see API charges.

---

## Billing is enforced, not assumed

Claude Code accepts both a subscription login and an API key, and **silently
prefers the API key** when one is in its environment. The app loads
`ANTHROPIC_API_KEY` from `.env` for its API agents, so without precautions a
"subscription" run would quietly bill the API.

With `billing: subscription` the app passes Claude Code an empty
`ANTHROPIC_API_KEY`, which it treats as unset. API billing becomes impossible:
if you are not logged in, the run fails loudly instead of charging you. The
app's own environment is untouched, because the API fallback still needs the
key. `billing: auto` tries the subscription and falls back to the API only on
an authentication failure (never on a quota limit), logging which it used;
`billing: api` bills per token.

---

## What stays the same

The evolution agent's **tools still run inside the EvoTrader process**.
Claude Code reaches them through an in-process MCP server
(`src/evotrader/evolution/claude_code_tools.py`) that exposes the same Python
functions the API agent calls. Nothing is reimplemented, so:

- **Proposals and code reviews** are written by the same code, to
  `evolution/proposals/` and `evolution/reviews/` in your data folder.
- **Algorithm and instruction versions** go through the same registry, rate
  limits and `evolution.auto_promote` gate.
- **Carry-forward notes** stay in `evolution/notes/carry_forward.md`.
- **The console** shows the same live timeline, run history and **Continue**
  button, and the same run records (running, success, failed, timed out,
  cancelled).

The hosted **strategy agent** gets the same tools as the API one, sees the same
market data and handoff note, and returns its proposal to the orchestrator, so
the risk manager and executor run exactly as before.

## What differs

| | API runtime | Claude Code |
|---|---|---|
| Billing | Per token | Your subscription's usage limits |
| Continuing an interrupted evolution run | Rebuilds the conversation from the database | Resumes Claude Code's own session (lossless) |
| Token telemetry | Every call in the console's Token Usage views | Per-run totals in the log and the run summary only |
| Reading the repository | Through the `read_source_file` and `list_project_files` tools | Also Claude Code's own read and search tools, which navigate code better |
| Model | `model.<mode>.evolution_agent` / `strategy_agent` | `cli_runtimes.<name>.model` |
| At a usage limit | Provider rate limits and retries | `quota` policy: fall back early, skip, or fail |

---

## Safety

An unattended agent proposing changes to a system that trades real money is
kept on a short leash:

- **Writing files, editing, running commands and web access are always
  blocked** (`Write`, `Edit`, `NotebookEdit`, `Bash`, `WebFetch`, `WebSearch`),
  whatever the settings say. Every change must go through a `propose_*` or
  `submit_code_review` tool, which keeps it visible and waiting for you in the
  console.
- **`permission_mode: dontAsk`** denies anything not explicitly allowed: there
  is nobody to answer a permission prompt.
- **No inherited setup.** Runs ignore your personal Claude Code settings, the
  project's `CLAUDE.md`, hooks, skills and MCP servers, so a run behaves the
  same on every machine.
- **`max_turns`** stops a runaway loop before it consumes your usage window.
- The gates that matter live inside the tools and apply on both runtimes:
  strategy proposals and code reviews are documents for a person to act on,
  new versions stay inactive while `auto_promote` is off, instruction changes
  have a weekly limit, and the risk manager's instructions cannot be changed.

## Terms of use

Anthropic's Agent SDK documentation says that, unless previously approved,
third-party developers may not offer claude.ai login or subscription rate
limits to the users of their products. This project does not offer anyone a
subscription: each user runs Claude Code on **their own** subscription, on
their own machine, for their own use. Don't share your login or token, and
don't run this as a service for other people. Read Anthropic's current terms
yourself; this page is not legal advice.

---

## Choosing what to host

- **Evolution** runs weekly and has plenty of time, so a usage limit costs at
  most a delay: retry later with **Continue**.
- **Strategy** runs every cycle, many times a trading day, sharing your
  subscription's limits with everything else you use Claude for. The quota
  policy exists for this: once the harness reports it is close to the limit,
  the next cycle goes to the API agent (the current one always finishes where
  it started), so a limit costs a different model for a while, not a missed
  decision. Set `on_exhausted: "skip"` if you would rather lose a cycle than
  pay for the API.
- Each hosted run also starts a CLI process, which adds some seconds per
  cycle.

## Troubleshooting

**`COST WARNING: agent_runtime.evolution requests the 'claude_code' runtime but it is unavailable`**
(or `COST/RUNTIME WARNING` for the strategy agent). The SDK or the CLI is
missing. Run `uv run python scripts/setup_claude_code.py`; it names the missing
piece.

**`No subscription credential found`.** Log in with `claude`, or set
`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. With `billing: subscription`
the run will fail until you do; with `auto` it falls back to the API.

**The run fails at once with an authentication error.** The cached login
expired. Run `claude` to log in again, or mint a new token with
`claude setup-token`.

**The agent describes changes but no proposal appears.** It wrote prose instead
of calling a `propose_*` tool. Check the timeline for a tool response marked as
an error; if it recurs, tighten the evolution agent's instructions
(`instructions/evolution/` in your data folder).

**`claude_agent_sdk does not support option(s) [...]`.** The installed SDK is
older or newer than this integration expects. Unknown options are dropped and
the run continues; `uv sync --extra claude-code --upgrade` usually settles it.

**The run hits `max_turns`.** Raise `cli_runtimes.<name>.max_turns`, or narrow
the evolution instructions so the agent spends fewer turns exploring.

**Usage limit reached during an evolution run.** What happens depends on
`quota.on_exhausted`: `api` restarts the run on the API (billed per token),
`skip` abandons it, `fail` records it as failed. After the window resets you
can use the console's **Continue** button to resume the Claude Code session.
