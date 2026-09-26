"""First-run setup: ask a few questions, then save the keys and settings.

Run it with ``./run.sh setup`` (``scripts/setup.sh`` does it too, after
installing everything on a fresh clone). Two ways through:

* Easy — one AI provider for every agent: one key and a console password.
* Detailed — choose the provider and model for each agent, run the strategy and
  evolution agents on a Claude subscription, set the market-data key and port.

Keys go into ``<data folder>/.env``, readable only by your user account.
Choices go into ``<data folder>/settings.yaml``. Nothing is sent anywhere, and
running it again keeps every value you don't change.

The broker is not set up here. The first time the app needs Robinhood, the
console shows a link to Robinhood's own sign-in page (the terminal prints it
too), so neither this script nor the app ever sees the broker password.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from evotrader import paths

# ── Providers and models ─────────────────────────────────────────────


@dataclass(frozen=True)
class Provider:
    """An AI provider the agents can run on, and which model plays each role."""

    key: str
    label: str
    env_var: str
    key_url: str
    strong: str  # deep reasoning: strategy and evolution
    standard: str  # everything else
    light: str  # mechanical tool calling: the executor
    note: str = ""
    key_prefix: str = ""  # what a valid key starts with, for a friendly warning
    other_env_vars: tuple[str, ...] = ()  # other names the same key works under


PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        key="gemini",
        label="Google Gemini",
        env_var="GEMINI_API_KEY",
        key_url="https://aistudio.google.com/app/apikey",
        strong="gemini-3.1-pro-preview",
        standard="gemini-3.7-flash",
        light="gemini-3.1-flash-lite",
        note="recommended: lowest cost, and the system is developed and tested on it",
        key_prefix="AIza",
        other_env_vars=("GOOGLE_API_KEY",),  # the name Google's own guides use
    ),
    "anthropic": Provider(
        key="anthropic",
        label="Anthropic Claude",
        env_var="ANTHROPIC_API_KEY",
        key_url="https://console.anthropic.com/settings/keys",
        strong="anthropic/claude-opus-5",
        standard="anthropic/claude-sonnet-5",
        light="anthropic/claude-haiku-4-5-20251001",
        note="strongest reasoning; can also run on a Claude subscription",
        key_prefix="sk-ant-",
    ),
    "openai": Provider(
        key="openai",
        label="OpenAI ChatGPT",
        env_var="OPENAI_API_KEY",
        key_url="https://platform.openai.com/api-keys",
        strong="openai/gpt-5",
        standard="openai/gpt-5-mini",
        light="openai/gpt-5-mini",
        note="experimental: not yet run through a full trading cycle",
        key_prefix="sk-",
    ),
}

# The agents, in the order a cycle meets them, and the role each one plays.
AGENTS: dict[str, tuple[str, str]] = {
    "orchestrator": ("standard", "runs the cycle and routes work"),
    "news_sentiment_agent": ("standard", "reads the news"),
    "strategy_agent": ("strong", "decides what to trade"),
    "risk_manager_agent": ("standard", "checks every order against the limits"),
    "executor_agent": ("light", "places the orders"),
    "evolution_agent": ("strong", "reviews results weekly and proposes changes"),
}

# Agents that can run on a Claude subscription through Claude Code, by the
# names settings.yaml's `agent_runtime` uses.
SUBSCRIPTION_AGENTS = ("strategy", "evolution")
SUBSCRIPTION_RUNTIME = "claude_code"

# Practice mode trades a simulated account that starts empty.
DEFAULT_PRACTICE_CASH = 10_000.0
MAX_PRACTICE_CASH = 10_000_000.0

# What it trades: a plain symbol — letters, digits, a dot (BRK.B). The default
# is whatever the settings already name (the starter data's), never a literal.
_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

CONSOLE_PASSWORD = "DASHBOARD_PASSWORD"
MARKET_DATA_KEY = "ALPHA_VANTAGE_API_KEY"
PORT = "EVOTRADER_PORT"
DEFAULT_PORT = 8080
MIN_PASSWORD_LENGTH = 8


def models_for(provider: Provider) -> dict[str, str]:
    """Every agent on one provider: the role picks the model."""
    return {agent: getattr(provider, role) for agent, (role, _) in AGENTS.items()}


def provider_of(model: str) -> Provider | None:
    """The provider that serves ``model``: "anthropic/…" → Anthropic, a bare name → Gemini.

    None for any other "prefix/…" name: the model layer (LiteLLM) may well serve
    it, but this script doesn't know which key it needs.
    """
    return PROVIDERS.get(model.split("/", 1)[0] if "/" in model else "gemini")


def missing_keys(settings: object) -> dict[Provider, list[str]]:
    """Providers the agents' models need but whose key is not set, with those agents.

    Agents on a Claude subscription runtime need no key, and Gemini through
    Vertex AI (GOOGLE_GENAI_USE_VERTEXAI) uses cloud credentials instead of one.
    """
    from evotrader.models.config import AgentRuntimeKind

    vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true")
    missing: dict[Provider, list[str]] = {}
    for agent in AGENTS:
        kind, _ = settings.runtime_for(agent.removesuffix("_agent"))  # type: ignore[attr-defined]
        model = settings.active_model.for_agent(agent)  # type: ignore[attr-defined]
        provider = provider_of(model) if model else None
        if kind != AgentRuntimeKind.API or provider is None:
            continue
        if provider.key == "gemini" and vertex:
            continue
        if not any(os.environ.get(name) for name in (provider.env_var, *provider.other_env_vars)):
            missing.setdefault(provider, []).append(agent)
    return missing


# A cheap, read-only request per provider (list its models) that fails on a bad key.
_KEY_CHECKS: dict[str, tuple[str, Callable[[str], dict[str, str]]]] = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        lambda key: {"x-goog-api-key": key},
    ),
    "anthropic": (
        "https://api.anthropic.com/v1/models?limit=1",
        lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"},
    ),
    "openai": ("https://api.openai.com/v1/models", lambda key: {"Authorization": f"Bearer {key}"}),
}


def key_works(provider: Provider, key: str, timeout: float = 10.0) -> bool | None:
    """Whether the provider accepts ``key``: True, False, or None when it can't be told.

    Only a clear refusal counts as False. No connection, a timeout, rate
    limiting or an outage are None, and setup saves the key anyway. The key
    goes only to its own provider, in a header rather than the address.
    """
    import urllib.error
    import urllib.request

    if provider.key not in _KEY_CHECKS:
        return None
    url, headers = _KEY_CHECKS[provider.key]
    request = urllib.request.Request(url, headers=headers(key))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 or None
    except urllib.error.HTTPError as refused:
        if refused.code in (401, 403):
            return False
        body = refused.read(4096).decode("utf-8", "replace")
        return False if refused.code == 400 and "API_KEY_INVALID" in body else None  # Gemini
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ── Editing settings.yaml ────────────────────────────────────────────


def _models_of(settings_text: str, mode: str) -> dict:
    """``model.<mode>`` (``model`` itself when it isn't split into live and sim)."""
    import yaml

    block = (yaml.safe_load(settings_text) or {}).get("model") or {}
    part = block.get(mode, block) if isinstance(block, dict) else {}
    return part if isinstance(part, dict) else {}


def current_models(settings_text: str, mode: str = "live") -> dict[str, str]:
    """Each agent's model as the settings name it now, for the agents setup asks about."""
    part = _models_of(settings_text, mode)
    models = {agent: part.get(agent) or part.get("default") for agent in AGENTS}
    return {agent: str(model) for agent, model in models.items() if model}


def current_default_model(settings_text: str, mode: str = "live") -> str | None:
    """The model the settings give any agent without its own (``default``)."""
    default = _models_of(settings_text, mode).get("default")
    return str(default) if default else None


def practice_models_differ(settings_text: str) -> bool:
    """True when practice mode (``model.sim``) has model choices of its own."""
    return any(
        pick(settings_text, "sim") != pick(settings_text, "live")
        for pick in (current_models, current_default_model)
    )


def written_by_setup(settings_text: str) -> bool:
    """True once setup has written the model choices (the starter data's are not)."""
    return "Written by `./run.sh setup`" in settings_text


def mode_block_text(settings_text: str, mode: str) -> str | None:
    """The lines of ``model.<mode>`` exactly as written, comments included."""
    lines = settings_text.splitlines(keepends=True)
    inside = False
    for i, line in enumerate(lines):
        if re.match(r"model:(\s|$)", line):
            inside = True
        elif inside and line.strip() and line[0] not in " \t":
            return None  # the next top-level key: no such block
        elif inside and re.match(rf"  {re.escape(mode)}:(\s|$)", line):
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or lines[end].startswith("   ")):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return "".join(lines[i:end])
    return None


def easy_provider(models: dict[str, str]) -> Provider | None:
    """The provider whose easy-mode picks these models are, if they are exactly that."""
    return next((p for p in PROVIDERS.values() if models == models_for(p)), None)


def model_block(
    models: dict[str, str], how: str, default: str | None = None, sim_text: str | None = None
) -> str:
    """The top-level ``model:`` block: the same choice for live and practice mode.

    ``default`` (the most common model when not given) is what any agent not
    listed uses; agents that differ get their own line, the rest stay null
    (which means "use default"). ``sim_text`` keeps practice mode's own block
    as it was written instead.
    """
    if default is None:
        counts: dict[str, int] = {}
        for model in models.values():
            counts[model] = counts.get(model, 0) + 1
        default = max(counts, key=lambda m: (counts[m], m == next(iter(models.values()))))
    lines = [
        "model:",
        f"  # Written by `./run.sh setup` ({how}). null = use `default`.",
    ]
    for mode in ("live", "sim"):
        if mode == "sim" and sim_text is not None:
            lines.append(sim_text.rstrip("\n"))
            continue
        lines.append(f"  {mode}:")
        lines.append(f'    default: "{default}"')
        for agent, model in models.items():
            value = "null" if model == default else f'"{model}"'
            lines.append(f"    {agent}: {value}")
    return "\n".join(lines) + "\n"


def agent_runtime_block(use_subscription: bool) -> str:
    """The top-level ``agent_runtime:`` block: which agents run on a Claude subscription."""
    if not use_subscription:
        return "agent_runtime: {}  # every agent on its API model; see `cli_runtimes`\n"
    lines = [
        "agent_runtime:",
        "  # Written by `./run.sh setup`: billed to your Claude subscription.",
    ]
    lines += [f'  {agent}: "{SUBSCRIPTION_RUNTIME}"' for agent in SUBSCRIPTION_AGENTS]
    return "\n".join(lines) + "\n"


def replace_block(text: str, key: str, block: str) -> str:
    """Replace the top-level ``key:`` block of a YAML file and keep the rest as it was.

    A block runs from its ``key:`` line to the next line that starts in column 0
    (another key or a comment), so the comment introducing the next section and
    the blank lines before it stay where they are. A missing block is appended.
    """
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if re.match(rf"{re.escape(key)}:(\s|$)", line)), None
    )
    if start is None:
        return text + ("" if not text or text.endswith("\n") else "\n") + "\n" + block
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    body_end = end
    while body_end > start + 1 and not lines[body_end - 1].strip():
        body_end -= 1
    return "".join(lines[:start]) + block + "".join(lines[body_end:])


def subscribed_agents(settings_text: str) -> set[str]:
    """The agents running on a Claude Code runtime now, by their ``agent_runtime`` names."""
    import yaml

    runtimes = (yaml.safe_load(settings_text) or {}).get("agent_runtime") or {}
    return {
        str(agent) for agent, name in runtimes.items() if str(name).startswith(SUBSCRIPTION_RUNTIME)
    }


def subscription_in_use(settings_text: str) -> bool:
    """True when any agent already runs on a Claude Code runtime."""
    import yaml

    runtimes = (yaml.safe_load(settings_text) or {}).get("agent_runtime") or {}
    return any(str(name).startswith(SUBSCRIPTION_RUNTIME) for name in runtimes.values())


def retarget(settings_text: str, constitution_text: str, ticker: str) -> tuple[str, str] | None:
    """Point the settings and the constitution at ``ticker``; None when they are customised.

    Three places name the traded ticker and must agree: ``asset.primary_ticker``,
    its entry under ``asset.instruments``, and the constitution's
    ``allowed_tickers`` (without which every entry in the new ticker is refused).
    An inverse fund configured for the old ticker tracked the old ticker, so it is
    cleared. Only the layout the starter data ships is edited; anything a person
    has customised is left for that person to change.
    """
    import yaml

    current = ((yaml.safe_load(settings_text) or {}).get("asset") or {}).get("primary_ticker")
    if not current:
        return None
    if current == ticker:
        return settings_text, constitution_text
    s, n_primary = re.subn(
        rf'^(  primary_ticker:[ \t]*)"?{re.escape(current)}"?[ \t]*$',
        rf'\g<1>"{ticker}"',
        settings_text,
        count=1,
        flags=re.M,
    )
    s, n_entry = re.subn(
        rf"^(    ){re.escape(current)}:[ \t]*$", rf"\g<1>{ticker}:", s, count=1, flags=re.M
    )
    s = re.sub(r"^(  inverse_ticker:[ \t]*)(?!null\b)\S.*$", r"\g<1>null", s, count=1, flags=re.M)
    # The entry's description named the old instrument.
    lines = s.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.rstrip() == f"    {ticker}:":
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j].startswith("      "):
                    break
                if lines[j].lstrip().startswith("description:"):
                    lines[j] = '      description: "the traded instrument"\n'
            break
    s = "".join(lines)
    c, n_allowed = re.subn(
        r"^(  allowed_tickers:[ \t]*)\[[^\]]*\]",
        rf'\g<1>["{ticker}"]',
        constitution_text,
        count=1,
        flags=re.M,
    )
    if not (n_primary and n_entry and n_allowed):
        return None
    return s, c


def has_runtime(settings_text: str, name: str) -> bool:
    """True when ``cli_runtimes`` defines the runtime ``name``."""
    in_runtimes = False
    for line in settings_text.splitlines():
        if re.match(r"cli_runtimes:(\s|$)", line):
            in_runtimes = True
        elif line[:1] not in ("", " ", "\t", "#"):
            in_runtimes = False
        elif in_runtimes and re.match(rf"  {re.escape(name)}:(\s|$)", line):
            return True
    return False


# ── Editing .env ─────────────────────────────────────────────────────


def _quote(value: str) -> str:
    """A .env value that reads back exactly: single quotes are never expanded."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """Set ``KEY=value`` lines, keeping comments and every key not being changed."""
    remaining = dict(updates)
    out = []
    for line in text.splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            out.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out += [f"{key}={_quote(value)}" for key, value in remaining.items()]
    return "\n".join(out) + "\n"


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, readable and writable only by this user.

    Written to a private temporary file first and then moved into place, so the
    secret is never briefly readable by others and a crash never leaves half a
    file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def saved_values(data_dir: Path) -> dict[str, str]:
    """Values already saved, from the project's .env and the data folder's (which wins)."""
    from dotenv import dotenv_values

    values: dict[str, str] = {}
    for env_file in (paths.project_root() / ".env", data_dir / ".env"):
        if env_file.is_file():
            values.update({k: v for k, v in dotenv_values(env_file).items() if v})
    return values


# ── The conversation ─────────────────────────────────────────────────


Ask = Callable[[str], str]


@dataclass
class Plan:
    """What the answers add up to, before anything is written."""

    how: str
    models: dict[str, str]
    default_model: str | None = None  # None: the most common of ``models``
    env: dict[str, str] = field(default_factory=dict)
    # None: not asked, or the answer matches what is saved — leave that part of
    # settings.yaml alone, so a re-run never undoes a hand-made runtime setup.
    use_subscription: bool | None = None
    port: int = DEFAULT_PORT
    ticker: str | None = None  # None: keep what the settings trade now
    practice_cash: float | None = None  # play money for an empty practice account
    keep_practice_models: bool = False  # practice mode keeps its own hand-made models


class Setup:
    """The questions. ``ask``/``ask_secret``/``say`` are injectable for tests."""

    def __init__(
        self,
        data_dir: Path,
        ask: Ask,
        ask_secret: Ask,
        say: Callable[[str], None],
        check_key: Callable[[Provider, str], bool | None] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.ask = ask
        self.ask_secret = ask_secret
        self.say = say
        self.saved = saved_values(data_dir)
        self.check_key = check_key  # None: new keys are not checked (tests)
        self.subscribed_now: set[str] = set()  # agents on a Claude subscription runtime now

    @property
    def first_time(self) -> bool:
        """Nothing saved yet: no AI key and no console password."""
        names = {CONSOLE_PASSWORD}
        for p in PROVIDERS.values():
            names.update((p.env_var, *p.other_env_vars))
        return not names & self.saved.keys()

    def provider_with_a_key(self) -> Provider | None:
        """The one provider whose key is saved or exported, if there is exactly one."""
        found = [
            p
            for p in PROVIDERS.values()
            for name in (p.env_var, *p.other_env_vars)
            if name in self.saved or os.environ.get(name)
        ]
        return found[0] if len(set(found)) == 1 else None

    def saved_port(self) -> int:
        port = str(self.saved.get(PORT, DEFAULT_PORT))
        return int(port) if port.isdigit() else DEFAULT_PORT

    # small helpers

    def choose(self, question: str, options: list[tuple[str, str]], default: int = 1) -> int:
        self.say(question)
        for n, (label, note) in enumerate(options, 1):
            self.say(f"  [{n}] {label}" + (f" — {note}" if note else ""))
        while True:
            answer = self.ask(f"Choose 1-{len(options)} [{default}]: ").strip() or str(default)
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer)
            self.say(f"Please type a number from 1 to {len(options)}.")

    def yes(self, question: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        answer = self.ask(f"{question} [{hint}]: ").strip().lower()
        return default if not answer else answer in ("y", "yes")

    def secret(
        self,
        env_var: str,
        what: str,
        *,
        required: bool,
        check: Callable[[str], str] | None = None,
        also: tuple[str, ...] = (),
    ) -> str | None:
        """A secret, or None to keep the saved one, use one from the environment, or skip it.

        ``also`` names other variables the same secret works under.
        """
        if env_var in self.saved:
            answer = self.ask_secret(f"{what} (saved — press Enter to keep it): ").strip()
            return answer or None
        # Exported in the shell: the app uses it (the shell wins over .env).
        exported = next((name for name in (env_var, *also) if os.environ.get(name)), None)
        if exported:
            answer = self.ask_secret(
                f"{what} (found in your environment as {exported} — press Enter to use it): "
            ).strip()
            return answer or None
        while True:
            answer = self.ask_secret(
                f"{what}{'' if required else ' (optional — press Enter to skip)'}: "
            ).strip()
            if answer or not required:
                if answer and check:
                    warning = check(answer)
                    if warning:
                        self.say(f"  Note: {warning}")
                return answer or None
            self.say("  This one is needed to continue.")

    def pick_provider(self, question: str, default: str = "gemini") -> Provider:
        keys = list(PROVIDERS)
        options = [(p.label, p.note) for p in PROVIDERS.values()]
        return PROVIDERS[keys[self.choose(question, options, keys.index(default) + 1) - 1]]

    def key_names(self, provider: Provider) -> list[str]:
        """Names to save the provider's key under: each one already in use, else the usual one.

        A Gemini key works as GEMINI_API_KEY or GOOGLE_API_KEY. Replacing only
        one of two saved names would leave the old key under the other, and a
        library that prefers that name would keep using it.
        """
        names = (provider.env_var, *provider.other_env_vars)
        return [name for name in names if name in self.saved] or [provider.env_var]

    def provider_key(self, provider: Provider) -> str | None:
        self.say(f"\n{provider.label} API key — get one at {provider.key_url}")

        def check(key: str) -> str:
            if provider.key_prefix and not key.startswith(provider.key_prefix):
                return f"{provider.label} keys usually start with “{provider.key_prefix}”."
            return ""

        name = self.key_names(provider)[0]
        others = tuple(n for n in (provider.env_var, *provider.other_env_vars) if n != name)
        while True:
            key = self.secret(
                name, f"{provider.label} key", required=True, check=check, also=others
            )
            if key is None or self.check_key is None:  # kept the saved key, or no checking
                return key
            # A mistyped key otherwise shows only at the first trading cycle,
            # after the broker sign-in.
            self.say(f"  Checking the key with {provider.label} …")
            works = self.check_key(provider, key)
            if works:
                self.say("  ✓ It works.")
                return key
            if works is None:
                self.say(
                    "  Couldn't check it (no connection, or the service is busy). Saved anyway."
                )
                return key
            self.say(f"  ✗ {provider.label} doesn't accept this key. Look for a missing character.")
            if self.yes("  Save it anyway?", default=False):
                return key

    def console_password(self) -> str | None:
        self.say(
            "\nPassword for the web console. The console can approve trades, so it always"
            f" has one. At least {MIN_PASSWORD_LENGTH} characters."
        )
        if CONSOLE_PASSWORD in self.saved:
            answer = self.ask_secret("Console password (saved — press Enter to keep it): ")
            if not answer:
                return None
        else:
            answer = ""
        while True:
            if not answer:
                answer = self.ask_secret("Console password: ")
            if len(answer) < MIN_PASSWORD_LENGTH:
                self.say(f"  Too short — use at least {MIN_PASSWORD_LENGTH} characters.")
                answer = ""
                continue
            if self.ask_secret("Type it again: ") != answer:
                self.say("  The two didn't match. Let's try again.")
                answer = ""
                continue
            return answer

    def pick_ticker(self, settings_text: str) -> str | None:
        """The ticker to trade, or None to keep the current one."""
        import yaml

        current = ((yaml.safe_load(settings_text) or {}).get("asset") or {}).get(
            "primary_ticker"
        ) or ""
        self.say(
            "\nWhich stock or ETF should it trade? It starts in practice mode with play"
            " money, so you can change this any time by running ./run.sh setup again."
        )
        while True:
            answer = self.ask(f"Ticker [{current}]: " if current else "Ticker: ").strip().upper()
            if answer == current or (not answer and current):
                return None
            if not answer:
                continue
            if not _TICKER.match(answer):
                self.say("  That doesn't look like a ticker — letters, like SPY or AAPL.")
                continue
            constitution = self.data_dir / "constitution.yaml"
            if constitution.is_file() and retarget(
                settings_text, constitution.read_text(encoding="utf-8"), answer
            ):
                return answer
            self.say(
                "  Your settings have been customised, so change the ticker by hand: see"
                " README.md, “Changing what it trades”. Keeping " + current + "."
            )
            return None

    def practice_money(self) -> float:
        self.say("\nPractice mode trades a simulated account with play money, and yours is empty.")
        while True:
            answer = self.ask(
                f"How much play money should it start with? [${DEFAULT_PRACTICE_CASH:,.0f}]: "
            )
            answer = answer.strip().replace("$", "").replace(",", "")
            if not answer:
                return DEFAULT_PRACTICE_CASH
            try:
                amount = float(answer)
            except ValueError:
                amount = -1.0
            if 1 <= amount <= MAX_PRACTICE_CASH:
                return amount
            self.say(f"  Please type an amount between $1 and ${MAX_PRACTICE_CASH:,.0f}.")

    def subscription(self, settings_text: str) -> bool | None:
        """Whether to switch the strategy and evolution agents onto a Claude subscription.

        None when there is nothing to change: Claude Code is not installed, the
        runtime isn't defined, or the answer matches the current setup.
        """
        if not shutil.which("claude") or not has_runtime(settings_text, SUBSCRIPTION_RUNTIME):
            return None
        current = subscription_in_use(settings_text)
        self.say(
            "\nClaude Code is installed. The strategy and evolution agents can run on your"
            " Claude subscription instead of paying per use (you need to be logged in to"
            " Claude Code)."
        )
        wanted = self.yes("Use your Claude subscription for them?", default=current)
        return None if wanted == current else wanted

    # the two paths

    def easy(self, settings_text: str, provider_now: Provider | None) -> Plan:
        provider = self.pick_provider(
            "\nWhich AI provider should the agents use?",
            default=(provider_now or PROVIDERS["gemini"]).key,
        )
        plan = Plan(
            how=f"easy mode, {provider.label}",
            models=models_for(provider),
            default_model=provider.standard,
            port=self.saved_port(),
        )
        if key := self.provider_key(provider):
            plan.env.update(dict.fromkeys(self.key_names(provider), key))
        if provider.key == "anthropic":
            plan.use_subscription = self.subscription(settings_text)
        elif self.subscribed_now:
            # Otherwise the summary would list the new provider's models for
            # agents that in fact keep running on the subscription.
            names = " and ".join(sorted(self.subscribed_now))
            self.say(
                f"\nThe {names} agent{'s' if len(self.subscribed_now) > 1 else ''} run on your"
                f" Claude subscription (Claude Code), not on {provider.label}."
            )
            if not self.yes("Keep them on your Claude subscription?", default=True):
                plan.use_subscription = False
        return plan

    def model_for(self, agent: str, what: str, suggested: str) -> str:
        """One agent's model. A bare name goes to Gemini, so one that isn't Gemini's is queried."""
        self.say(f"\n{agent} — {what}")
        while True:
            answer = self.ask(f"Model [{suggested}]: ").strip()
            if not answer:
                return suggested
            if "/" in answer or answer.startswith(("gemini", "gemma")):
                return answer
            prefix = (
                "openai/"
                if answer.startswith(("gpt", "o1", "o3", "o4", "chatgpt"))
                else "anthropic/"
                if answer.startswith("claude")
                else ""
            )
            self.say(f"  “{answer}” has no provider in front, so it would go to Google Gemini.")
            if prefix and self.yes(f"  Use “{prefix}{answer}” instead?", default=True):
                return prefix + answer
            if not prefix:
                self.say('  Other providers\' models start with the provider, like "anthropic/…".')
            if self.yes("  Use it as a Gemini model anyway?", default=False):
                return answer

    def detailed(self, settings_text: str) -> Plan:
        # A re-run suggests today's models while the main provider stays the same,
        # so pressing Enter throughout changes nothing.
        now = {} if self.first_time else current_models(settings_text)
        now_default = current_default_model(settings_text) if now else None
        now_main = provider_of(now_default) if now_default else None
        main_provider = self.pick_provider(
            "\nMain AI provider (the default for every agent):",
            default=(now_main or PROVIDERS["gemini"]).key,
        )
        keep = main_provider is now_main
        self.say(
            "\nFor each agent, press Enter to keep the suggestion, or type a model name:"
            ' "anthropic/…" or "openai/…", or a Gemini name such as "gemini-3.7-flash".'
        )
        models = {
            agent: self.model_for(
                agent, what, now[agent] if keep and agent in now else getattr(main_provider, role)
            )
            for agent, (role, what) in AGENTS.items()
        }
        plan = Plan(
            how="detailed mode",
            models=models,
            default_model=now_default if keep else main_provider.standard,
        )
        needed: dict[str, Provider] = {}
        for model in models.values():
            provider = provider_of(model)
            if provider is None:
                self.say(
                    f"\n“{model}” is from a provider this script doesn't know. Add its API key"
                    f" to {self.data_dir / '.env'} yourself (see the LiteLLM docs for the name)."
                )
            else:
                needed.setdefault(provider.key, provider)
        for provider in needed.values():
            if key := self.provider_key(provider):
                plan.env.update(dict.fromkeys(self.key_names(provider), key))
        plan.use_subscription = self.subscription(settings_text)
        plan.port = self.saved_port()
        port = self.ask(f"\nConsole port [{plan.port}]: ").strip()
        if port:
            if not port.isdigit() or not 1024 <= int(port) <= 65535:
                self.say("  Not a usable port (1024-65535); keeping the current one.")
            else:
                plan.port = int(port)
                plan.env[PORT] = port
        return plan

    def run(self) -> Plan | None:
        settings_path = self.data_dir / "settings.yaml"
        settings_text = settings_path.read_text(encoding="utf-8")
        # A re-run starts from how things are set up now: models that are one
        # provider's easy-mode picks mean easy mode, anything else detailed.
        # Models setup never wrote (the starter data's) mean a first setup.
        now = current_models(settings_text) if written_by_setup(settings_text) else {}
        if self.first_time:
            now = {}
        provider_now = easy_provider(now) if now else self.provider_with_a_key()
        self.subscribed_now = subscribed_agents(settings_text)
        mode = self.choose(
            "How much do you want to set up?",
            [
                ("Easy", "one AI provider for every agent (recommended)"),
                ("Detailed", "choose per agent, plus a Claude subscription, port and more"),
            ],
            default=2 if now and provider_now is None else 1,
        )
        plan = self.easy(settings_text, provider_now) if mode == 1 else self.detailed(settings_text)
        if practice_models_differ(settings_text):
            self.say("\nPractice mode has model choices of its own in settings.yaml (model: sim).")
            plan.keep_practice_models = not self.yes(
                "Use these choices for practice mode too?", default=False
            )
        plan.ticker = self.pick_ticker(settings_text)
        if password := self.console_password():
            plan.env[CONSOLE_PASSWORD] = password
        self.say(
            "\nAlpha Vantage adds news and market data — free key at https://www.alphavantage.co/support/#api-key"
        )
        if market_key := self.secret(MARKET_DATA_KEY, "Alpha Vantage key", required=False):
            plan.env[MARKET_DATA_KEY] = market_key
        if practice_account_is_empty(self.data_dir):
            plan.practice_cash = self.practice_money()
        self.summarise(plan)
        return plan if self.yes("\nSave these settings?", default=True) else None

    def summarise(self, plan: Plan) -> None:
        self.say("\nHere is what will be saved:")
        for agent, model in plan.models.items():
            short = agent.removesuffix("_agent")
            if plan.use_subscription and short in SUBSCRIPTION_AGENTS:
                model += " (switching to your Claude subscription)"
            elif short in self.subscribed_now:
                model = (
                    f"{model} (moving off your Claude subscription)"
                    if plan.use_subscription is False
                    else "your Claude subscription (Claude Code), as now"
                )
            self.say(f"  {agent:<22} {model}")
        if plan.keep_practice_models:
            self.say(f"  {'practice mode':<22} keeps its own models (model: sim)")
        if plan.ticker:
            self.say(f"  {'trades':<22} {plan.ticker} (allowed in constitution.yaml too)")
        if plan.practice_cash:
            self.say(f"  {'practice account':<22} ${plan.practice_cash:,.0f} of play money")
        for env_var in plan.env:
            self.say(
                f"  {env_var:<22} {'port ' + plan.env[env_var] if env_var == PORT else 'saved (hidden)'}"
            )
        self.say(
            f"  to {self.data_dir / '.env'} (only you can read it) and {self.data_dir / 'settings.yaml'}"
        )


def practice_db(data_dir: Path) -> Path:
    """The simulated broker's database (the path main.py opens in practice mode)."""
    return data_dir / "sim" / "db" / "sim_broker.db"


def practice_account_is_empty(data_dir: Path) -> bool:
    """True when the practice account has no cash and no positions yet."""
    import sqlite3

    path = practice_db(data_dir)
    if not path.is_file():
        return True
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            cash = conn.execute(
                "SELECT COALESCE(SUM(cash_balance), 0) FROM sim_accounts"
            ).fetchone()[0]
            held = conn.execute(
                "SELECT COUNT(*) FROM sim_positions WHERE quantity != 0"
            ).fetchone()[0]
    except sqlite3.Error:
        return True  # tables not created yet
    return cash <= 0 and held == 0


async def deposit_practice_cash(data_dir: Path, amount: float) -> None:
    from evotrader.sim import SimBroker

    broker = SimBroker(practice_db(data_dir))
    await broker.initialize()
    try:
        await broker.deposit(amount)
    finally:
        await broker.close()


def apply(plan: Plan, data_dir: Path) -> None:
    """Write the plan: keys into .env, choices into settings.yaml.

    The new settings are checked with the app's own loader before they replace
    the old file, so a mistake here can never leave settings the app refuses to
    start with.
    """
    from evotrader.config import _load_constitution, _load_settings

    settings_path = data_dir / "settings.yaml"
    constitution_path = data_dir / "constitution.yaml"
    text = settings_path.read_text(encoding="utf-8")
    sim_text = mode_block_text(text, "sim") if plan.keep_practice_models else None
    text = replace_block(
        text, "model", model_block(plan.models, plan.how, plan.default_model, sim_text)
    )
    if plan.use_subscription is not None:
        text = replace_block(text, "agent_runtime", agent_runtime_block(plan.use_subscription))
    constitution = None
    if plan.ticker:
        retargeted = retarget(text, constitution_path.read_text(encoding="utf-8"), plan.ticker)
        if retargeted is None:
            raise ValueError(f"cannot switch the settings to {plan.ticker}")
        text, constitution = retargeted
    with tempfile.TemporaryDirectory() as check_dir:
        Path(check_dir, "settings.yaml").write_text(text, encoding="utf-8")
        _load_settings(Path(check_dir))  # raises if the app would refuse it
        if constitution is not None:
            Path(check_dir, "constitution.yaml").write_text(constitution, encoding="utf-8")
            _load_constitution(Path(check_dir))
    settings_path.write_text(text, encoding="utf-8")
    if constitution is not None:
        constitution_path.write_text(constitution, encoding="utf-8")

    env_path = data_dir / ".env"
    current = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    write_private(env_path, update_env_text(current, plan.env))

    if plan.practice_cash:
        import asyncio

        asyncio.run(deposit_practice_cash(data_dir, plan.practice_cash))


def ensure_data_folder(data_dir: Path, say: Callable[[str], None]) -> Callable[[], None]:
    """Create the data folder from the starter data the first time.

    Returns how to take away what it created, for a setup that ends without
    saving: a half-made folder would start the app with no password and no key.
    """
    if (data_dir / "settings.yaml").is_file():
        return lambda: None
    existed = data_dir.exists()
    before = set(data_dir.iterdir()) if existed else set()

    def undo() -> None:
        created = set(data_dir.iterdir()) - before if existed else {data_dir}
        for path in created:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    say(f"Creating your data folder from the starter data: {data_dir}\n")
    try:
        subprocess.run(
            [sys.executable, str(paths.project_root() / "scripts" / "init_data.py"), "--quiet"],
            check=True,
            env={**os.environ, paths.DATA_DIR_ENV: str(data_dir)},
        )
    except BaseException:
        undo()
        raise
    return undo


def project_command(command: str) -> str:
    """A command run in the project folder, on the data folder in use."""
    root = paths.project_root()
    folder = paths.data_dir(root)
    data = f'EVOTRADER_DATA_DIR="{folder}" ' if folder != root / "data" else ""
    caller = os.environ.get(paths.CALLER_DIR_ENV, "").strip()
    move = f'cd "{root}" && ' if caller and Path(caller).resolve() != root.resolve() else ""
    return f"{move}{data}{command}"


def next_steps(plan: Plan, say: Callable[[str], None]) -> None:
    say(
        "\nAll set. Next:\n"
        f"  1. Start it:          {paths.run_command()}\n"
        f"  2. Open the console:  http://127.0.0.1:{plan.port}  (it asks for the console password)\n"
        "  3. The first time it needs Robinhood, the console shows a link to Robinhood's\n"
        "     own sign-in page (the terminal prints it too). Sign in there; you're sent\n"
        "     back to the console. Agentic trading must be enabled on the account —\n"
        "     practice mode needs it too, because market prices come from Robinhood.\n"
        "It starts in practice mode with play money. See README.md for switching to\n"
        f"live trading. To change these answers, including what it trades, run:\n"
        f"  {paths.run_command('setup')}"
    )
    if plan.use_subscription:
        say(
            "Check Claude Code is ready:  "
            + project_command("uv run python scripts/setup_claude_code.py")
        )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass

    import yaml
    from pydantic import ValidationError

    parser = argparse.ArgumentParser(
        prog="./run.sh setup",
        description="Answer a few questions, then save the keys and settings to your data folder.",
    )
    parser.add_argument(
        "--data-dir",
        help="Data folder to set up. Overrides EVOTRADER_DATA_DIR; default: data/ in the project.",
    )
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ[paths.DATA_DIR_ENV] = str(Path(args.data_dir).expanduser().resolve())

    data_dir = paths.data_dir()
    print("EvoTrader setup\n─────────────────")
    print(f"Data folder: {data_dir}\n")
    try:
        undo = ensure_data_folder(data_dir, print)
    except subprocess.CalledProcessError:
        print("\nThe data folder could not be made (see above). Nothing was saved.")
        return 1
    try:
        setup = Setup(data_dir, input, getpass.getpass, print, check_key=key_works)
        plan = setup.run()
        if plan is None:
            undo()
            print("Nothing was saved.")
            return 1
        apply(plan, data_dir)
    except (KeyboardInterrupt, EOFError):
        undo()
        print("\nStopped. Nothing was saved.")
        return 1
    except (yaml.YAMLError, ValidationError) as error:
        undo()
        print(
            f"\nThe settings in {data_dir} can't be read, so nothing was saved:\n{error}\n\n"
            "Fix the file named above and run setup again. (YAML: indentation and quotes"
            " matter.)"
        )
        return 1
    except BaseException:
        undo()
        raise
    next_steps(plan, print)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
