"""The first-run onboarding: questions in, keys and settings out.

Owner's ask (2026-09-25): a script that collects the keys and a few key
settings and gets the app running — an easy mode that puts every agent on one
provider (Gemini, Claude or ChatGPT) and a detailed mode with more options.
These tests drive the real questions with scripted answers.
"""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from evotrader import onboarding
from evotrader.config import _load_settings

SETTINGS = textwrap.dedent(
    """\
    # Settings — a small but valid file, shaped like starter_data/settings.yaml.
    asset:
      primary_ticker: "SPY"

    # ── Models ──
    model:
      live:
        default: "gemini-3.7-flash"
      sim:
        default: "gemini-3.7-flash"

    cli_runtimes:
      claude_code:
        driver: "claude_code"
        billing: "subscription"
        model: "opus"

    # Agents not listed use "api".
    agent_runtime: {}

    # ── Caching ──
    caching:
      enabled: true
    """
)

KEY = "AIzaSyTESTKEY000000000000000000000000"
PASSWORD = "correct horse #1 'battery'"


@pytest.fixture
def data(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "settings.yaml").write_text(SETTINGS)
    # Keep the real project's .env out of "already saved" detection.
    monkeypatch.setattr(onboarding.paths, "project_root", lambda: tmp_path)
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: None)  # no Claude Code
    return folder


class Script:
    """Answers questions in order; records everything shown to the user."""

    def __init__(self, answers: list[str], secrets: list[str]) -> None:
        self.answers, self.secrets, self.shown = list(answers), list(secrets), []

    def ask(self, prompt: str) -> str:
        self.shown.append(prompt)
        return self.answers.pop(0)

    def ask_secret(self, prompt: str) -> str:
        self.shown.append(prompt)
        return self.secrets.pop(0)

    def say(self, text: str) -> None:
        self.shown.append(text)


def run(
    data: Path, answers: list[str], secrets: list[str]
) -> tuple[onboarding.Plan | None, Script]:
    script = Script(answers, secrets)
    plan = onboarding.Setup(data, script.ask, script.ask_secret, script.say).run()
    if plan is not None:
        onboarding.apply(plan, data)
    assert not script.answers and not script.secrets, "every scripted answer was asked for"
    return plan, script


def models(data: Path, mode: str = "live") -> dict:
    return yaml.safe_load((data / "settings.yaml").read_text())["model"][mode]


class TestEasyMode:
    def test_gemini_for_everyone(self, data) -> None:
        run(
            data,
            answers=["1", "1", "", "", "y"],  # easy, Gemini, keep ticker, default play money, save
            secrets=[KEY, PASSWORD, PASSWORD, ""],
        )
        live = models(data)
        assert live["default"] == "gemini-3.7-flash"
        assert live["strategy_agent"] == live["evolution_agent"] == "gemini-3.1-pro-preview"
        assert live["executor_agent"] == "gemini-3.1-flash-lite"
        assert models(data, "sim") == live, "practice mode gets the same models"
        env = dotenv_values(data / ".env")
        assert env["GEMINI_API_KEY"] == KEY
        assert env["DASHBOARD_PASSWORD"] == PASSWORD, "quotes, # and spaces read back exactly"
        assert "ALPHA_VANTAGE_API_KEY" not in env

    @pytest.mark.parametrize(("choice", "provider"), [("2", "anthropic"), ("3", "openai")])
    def test_claude_or_chatgpt_for_everyone(self, data, choice, provider) -> None:
        p = onboarding.PROVIDERS[provider]
        run(
            data,
            answers=["1", choice, "", "", "y"],
            secrets=["sk-ant-x" if provider == "anthropic" else "sk-x", PASSWORD, PASSWORD, ""],
        )
        live = models(data)
        assert live["default"] == p.standard
        assert live["strategy_agent"] == p.strong
        assert dotenv_values(data / ".env")[p.env_var].startswith("sk-")

    def test_the_result_is_settings_the_app_accepts(self, data) -> None:
        run(data, answers=["1", "2", "", "", "y"], secrets=["sk-ant-x", PASSWORD, PASSWORD, ""])
        settings = _load_settings(data)
        assert settings.model.live.for_agent("strategy_agent") == "anthropic/claude-opus-5"
        assert settings.model.sim.for_agent("orchestrator") == "anthropic/claude-sonnet-5"

    def test_the_rest_of_the_file_is_untouched(self, data) -> None:
        run(data, answers=["1", "1", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        text = (data / "settings.yaml").read_text()
        for kept in (
            'primary_ticker: "SPY"',
            "# ── Caching ──",
            "caching:\n  enabled: true",
            'billing: "subscription"',
            "agent_runtime: {}",
        ):
            assert kept in text


class TestSecrets:
    def test_the_key_file_is_private(self, data) -> None:
        (data / ".env").write_text("# my notes\nOTHER=1\n")
        (data / ".env").chmod(0o644)
        run(data, answers=["1", "1", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        assert stat.S_IMODE((data / ".env").stat().st_mode) == 0o600
        text = (data / ".env").read_text()
        assert "# my notes" in text and "OTHER=1" in text, "existing lines are kept"

    def test_no_secret_is_ever_shown(self, data) -> None:
        _, script = run(
            data, answers=["1", "1", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, "AVKEY123"]
        )
        shown = "\n".join(script.shown)
        for secret in (KEY, PASSWORD, "AVKEY123"):
            assert secret not in shown

    def test_a_rerun_keeps_saved_values_on_enter(self, data) -> None:
        run(data, answers=["1", "1", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, "AVKEY123"])
        run(data, answers=["1", "1", "", "y"], secrets=["", "", ""])  # Enter, Enter, Enter
        env = dotenv_values(data / ".env")
        assert env["GEMINI_API_KEY"] == KEY
        assert env["DASHBOARD_PASSWORD"] == PASSWORD
        assert env["ALPHA_VANTAGE_API_KEY"] == "AVKEY123"

    def test_a_gemini_key_saved_as_google_api_key_counts_as_saved(self, data) -> None:
        # Found 2026-09-26: Google's own guides name it GOOGLE_API_KEY and the app
        # accepts it, but setup asked for the key again as if none were saved.
        (data / ".env").write_text(f"GOOGLE_API_KEY={KEY}\n")
        _, script = run(data, answers=["1", "1", "", "", "y"], secrets=["", PASSWORD, PASSWORD, ""])
        assert any("Google Gemini key (saved" in shown for shown in script.shown)
        env = dotenv_values(data / ".env")
        assert env["GOOGLE_API_KEY"] == KEY
        assert "GEMINI_API_KEY" not in env, "no second copy under the other name"

    def test_a_new_gemini_key_replaces_every_saved_copy(self, data) -> None:
        # With both names saved, updating one left the old key under the other,
        # and Google's library prefers GOOGLE_API_KEY: the new key was ignored.
        (data / ".env").write_text("GOOGLE_API_KEY=old-key\nGEMINI_API_KEY=old-key\n")
        run(data, answers=["1", "1", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        env = dotenv_values(data / ".env")
        assert env["GOOGLE_API_KEY"] == env["GEMINI_API_KEY"] == KEY

    def test_a_short_or_mistyped_password_is_asked_again(self, data) -> None:
        _, script = run(
            data,
            answers=["1", "1", "", "", "y"],
            secrets=[KEY, "short", PASSWORD, "typo", PASSWORD, PASSWORD, ""],
        )
        shown = "\n".join(script.shown)
        assert "Too short" in shown and "didn't match" in shown
        assert dotenv_values(data / ".env")["DASHBOARD_PASSWORD"] == PASSWORD

    def test_saying_no_saves_nothing(self, data) -> None:
        before = (data / "settings.yaml").read_text()
        plan, _ = run(data, answers=["1", "1", "", "", "n"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        assert plan is None
        assert (data / "settings.yaml").read_text() == before
        assert not (data / ".env").exists()


class TestDetailedMode:
    def test_mixed_providers_ask_for_each_key(self, data) -> None:
        answers = [
            "2",  # detailed
            "1",  # main provider Gemini
            "",
            "",
            "anthropic/claude-opus-5",
            "",
            "",
            "openai/gpt-5",  # six agents
            "9090",  # port
            "",  # ticker: keep
            "",  # play money: default
            "y",
        ]
        run(data, answers=answers, secrets=[KEY, "sk-ant-x", "sk-x", PASSWORD, PASSWORD, ""])
        live = models(data)
        assert live["strategy_agent"] == "anthropic/claude-opus-5"
        assert live["evolution_agent"] == "openai/gpt-5"
        assert live["executor_agent"] == "gemini-3.1-flash-lite"
        env = dotenv_values(data / ".env")
        assert {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"} <= set(env)
        assert env["EVOTRADER_PORT"] == "9090"

    def test_claude_subscription_switches_the_two_agents(self, data, monkeypatch) -> None:
        monkeypatch.setattr(onboarding.shutil, "which", lambda _name: "/usr/local/bin/claude")
        run(
            data, answers=["1", "2", "y", "", "", "y"], secrets=["sk-ant-x", PASSWORD, PASSWORD, ""]
        )
        runtimes = _load_settings(data).agent_runtime
        assert runtimes == {"strategy": "claude_code", "evolution": "claude_code"}

    def test_a_rerun_does_not_undo_a_hand_made_runtime_setup(self, data, monkeypatch) -> None:
        """An existing Claude setup survives a re-run that picks another provider."""
        custom = 'agent_runtime:\n  evolution: "claude_code"\n'
        text = onboarding.replace_block(
            (data / "settings.yaml").read_text(), "agent_runtime", custom
        )
        (data / "settings.yaml").write_text(text)
        run(data, answers=["1", "1", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        assert _load_settings(data).agent_runtime == {"evolution": "claude_code"}


class TestReplaceBlock:
    def test_keeps_the_next_sections_header(self) -> None:
        text = "a:\n  x: 1\n\n# header of b\nb:\n  y: 2\n"
        assert (
            onboarding.replace_block(text, "a", "a:\n  x: 9\n")
            == "a:\n  x: 9\n\n# header of b\nb:\n  y: 2\n"
        )

    def test_appends_a_missing_block(self) -> None:
        assert onboarding.replace_block("a: 1\n", "b", "b: 2\n") == "a: 1\n\nb: 2\n"

    def test_does_not_match_a_longer_key(self) -> None:
        text = "model_extra: 1\nmodel:\n  x: 1\n"
        assert (
            onboarding.replace_block(text, "model", "model: {}\n") == "model_extra: 1\nmodel: {}\n"
        )


class TestPracticeMoney:
    def test_an_empty_practice_account_gets_play_money_once(self, data) -> None:
        import sqlite3

        def cash() -> float:
            with sqlite3.connect(onboarding.practice_db(data)) as conn:
                return conn.execute("SELECT SUM(cash_balance) FROM sim_accounts").fetchone()[0]

        run(data, answers=["1", "1", "", "$25,000", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        assert cash() == 25_000
        run(data, answers=["1", "1", "", "y"], secrets=["", "", ""])  # not asked again
        assert cash() == 25_000

    def test_a_nonsense_amount_is_asked_again(self, data) -> None:
        _, script = run(
            data,
            answers=["1", "1", "", "lots", "0", "0.4", "", "y"],
            secrets=[KEY, PASSWORD, PASSWORD, ""],
        )
        # 0.4 was accepted once and summarised as "$0 of play money" (2026-09-26).
        assert sum("Please type an amount" in line for line in script.shown) == 3


class TestTicker:
    @pytest.fixture
    def starter(self, data: Path) -> Path:
        root = Path(__file__).resolve().parents[2] / "starter_data"
        (data / "settings.yaml").write_text((root / "settings.yaml").read_text())
        (data / "constitution.yaml").write_text((root / "constitution.yaml").read_text())
        return data

    def test_a_new_ticker_switches_settings_and_limits_together(self, starter) -> None:
        from evotrader.config import _load_constitution

        run(starter, answers=["1", "1", "aapl", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        settings = _load_settings(starter)
        assert settings.asset.primary_ticker == "AAPL"
        assert list(settings.asset.instruments) == ["AAPL"], "the old ticker's entry is renamed"
        assert _load_constitution(starter).trading_rules.allowed_tickers == ["AAPL"], (
            "without this every entry in the new ticker is refused"
        )

    def test_something_that_is_not_a_ticker_is_asked_again(self, starter) -> None:
        _, script = run(
            starter, answers=["1", "1", "12$", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""]
        )
        assert any("doesn't look like a ticker" in line for line in script.shown)
        assert _load_settings(starter).asset.primary_ticker == "SPY"

    def test_customised_settings_are_left_for_a_person(self, data) -> None:
        """The small test settings have no instruments entry, so nothing is guessed."""
        (data / "constitution.yaml").write_text('trading_rules:\n  allowed_tickers: ["SPY"]\n')
        _, script = run(
            data, answers=["1", "1", "AAPL", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""]
        )
        assert any("change the ticker by hand" in line for line in script.shown)
        assert _load_settings(data).asset.primary_ticker == "SPY"


class TestARerunKeepsYourChoices:
    """Found 2026-09-26 by a first-time-user walkthrough: a re-run always
    started from Easy + Gemini, so pressing Enter throughout replaced a detailed
    setup's models with Gemini's, and asked someone on Claude for a Gemini key."""

    def test_easy_claude_stays_claude(self, data) -> None:
        run(data, answers=["1", "2", "", "", "y"], secrets=["sk-ant-x", PASSWORD, PASSWORD, ""])
        before = (data / "settings.yaml").read_text()
        # Enter at every question: mode, provider, ticker; then save.
        _, script = run(data, answers=["", "", "", "y"], secrets=["", "", ""])
        assert (data / "settings.yaml").read_text() == before
        assert not any("Google Gemini key" in line for line in script.shown)

    def test_a_detailed_setup_stays_as_it_was(self, data) -> None:
        answers = [
            *("2", "1", "", "", "anthropic/claude-opus-5", "", "", "openai/gpt-5"),
            *("9090", "", "", "y"),  # port, ticker, play money, save
        ]
        run(data, answers=answers, secrets=[KEY, "sk-ant-x", "sk-x", PASSWORD, PASSWORD, ""])
        before = (data / "settings.yaml").read_text()
        # Enter at mode, provider, six models, port and ticker; then save.
        _, script = run(data, answers=[""] * 10 + ["y"], secrets=[""] * 5)
        assert (data / "settings.yaml").read_text() == before
        assert any("Console port [9090]" in line for line in script.shown)

    def test_the_main_provider_is_the_default_model(self, data) -> None:
        """Four agents typed onto OpenAI no longer make OpenAI the default."""
        answers = ["2", "2", *["openai/gpt-5-mini"] * 4, "", "", "", "", "", "y"]
        run(data, answers=answers, secrets=["sk-x", "sk-ant-x", PASSWORD, PASSWORD, ""])
        assert models(data)["default"] == "anthropic/claude-sonnet-5"
        assert models(data)["orchestrator"] == "openai/gpt-5-mini"


class TestModelNames:
    def test_a_bare_name_that_is_not_gemini_is_queried(self, data) -> None:
        """Found 2026-09-26: "gpt-5" without "openai/" was taken as a Gemini model."""
        answers = [
            *("2", "1", "gpt-5", "n", "openai/gpt-5"),  # typed, queried, retyped
            *("", "", "", "", ""),  # the other five agents
            *("", "", "", "y"),  # port, ticker, play money, save
        ]
        _, script = run(data, answers=answers, secrets=["sk-x", KEY, PASSWORD, PASSWORD, ""])
        assert any("Did you mean “openai/gpt-5”?" in line for line in script.shown)
        assert models(data)["orchestrator"] == "openai/gpt-5"


def _quit(_prompt: str) -> str:
    raise KeyboardInterrupt


class TestQuitting:
    """Found 2026-09-26: quitting setup said "Nothing was saved" but left the new
    data folder behind, and the app then started from it with no console
    password and no AI key."""

    def test_the_folder_it_made_is_taken_away(self, tmp_path, monkeypatch, capfd) -> None:
        monkeypatch.setattr("builtins.input", _quit)
        folder = tmp_path / "new-data"
        assert onboarding.main(["--data-dir", str(folder)]) == 1
        assert not folder.exists()
        shown = capfd.readouterr().out
        assert f"Data folder: {folder}" in shown, "--data-dir is honoured"
        assert "Nothing was saved" in shown
        assert "+ created" not in shown, "one line, not a line per starter file"

    def test_what_was_already_there_stays(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("builtins.input", _quit)
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / ".env").write_text("GEMINI_API_KEY=mine\n")
        assert onboarding.main(["--data-dir", str(folder)]) == 1
        assert [p.name for p in folder.iterdir()] == [".env"]
        assert (folder / ".env").read_text() == "GEMINI_API_KEY=mine\n"


def test_the_next_steps_name_a_data_folder_of_your_own(tmp_path) -> None:
    shown: list[str] = []
    plan = onboarding.Plan(how="easy mode", models={}, port=9090)
    onboarding.next_steps(plan, shown.append, tmp_path / "mine")
    assert f'./run.sh --data-dir "{tmp_path / "mine"}"' in shown[0]
    assert "http://127.0.0.1:9090" in shown[0]


class TestMissingKeys:
    """The start-up warning's source: a key the agents' models need but nobody set."""

    @pytest.fixture(autouse=True)
    def _no_keys(self, monkeypatch) -> None:
        for p in onboarding.PROVIDERS.values():
            for name in (p.env_var, *p.other_env_vars):
                monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)

    def test_every_agent_needs_the_gemini_key(self, data) -> None:
        missing = onboarding.missing_keys(_load_settings(data))
        assert list(missing) == [onboarding.PROVIDERS["gemini"]]
        assert len(missing[onboarding.PROVIDERS["gemini"]]) == len(onboarding.AGENTS)

    def test_either_name_of_the_gemini_key_will_do(self, data, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "set")
        assert onboarding.missing_keys(_load_settings(data)) == {}

    def test_agents_on_a_claude_subscription_need_no_key(self, data) -> None:
        text = (data / "settings.yaml").read_text()
        text = text.replace('default: "gemini-3.7-flash"', 'default: "anthropic/claude-sonnet-5"')
        text = onboarding.replace_block(
            text, "agent_runtime", onboarding.agent_runtime_block(use_subscription=True)
        )
        (data / "settings.yaml").write_text(text)
        missing = onboarding.missing_keys(_load_settings(data))
        agents = missing[onboarding.PROVIDERS["anthropic"]]
        assert "strategy_agent" not in agents and "evolution_agent" not in agents
        assert "orchestrator" in agents


class TestKeyCheck:
    """Found 2026-09-26: a mistyped key showed only at the first trading cycle,
    after the Robinhood sign-in. Setup now asks the provider whether it works."""

    def run_checked(self, data, answers, secrets, verdicts):
        script = Script(answers, secrets)
        asked: list[tuple[str, str]] = []

        def check(provider, key):
            asked.append((provider.key, key))
            return verdicts.pop(0)

        plan = onboarding.Setup(
            data, script.ask, script.ask_secret, script.say, check_key=check
        ).run()
        if plan is not None:
            onboarding.apply(plan, data)
        assert not script.answers and not script.secrets and not verdicts
        return script, asked

    def test_a_refused_key_is_asked_for_again(self, data) -> None:
        script, asked = self.run_checked(
            data,
            answers=["1", "1", "n", "", "", "y"],  # ... "Save it anyway?" no
            secrets=["AIza-typo", KEY, PASSWORD, PASSWORD, ""],
            verdicts=[False, True],
        )
        assert asked == [("gemini", "AIza-typo"), ("gemini", KEY)]
        assert any("doesn't accept this key" in line for line in script.shown)
        assert dotenv_values(data / ".env")["GEMINI_API_KEY"] == KEY

    def test_a_refused_key_can_be_saved_anyway(self, data) -> None:
        self.run_checked(
            data,
            answers=["1", "1", "y", "", "", "y"],
            secrets=["AIza-mine", PASSWORD, PASSWORD, ""],
            verdicts=[False],
        )
        assert dotenv_values(data / ".env")["GEMINI_API_KEY"] == "AIza-mine"

    def test_no_answer_saves_it_with_a_note(self, data) -> None:
        script, _ = self.run_checked(
            data,
            answers=["1", "1", "", "", "y"],
            secrets=[KEY, PASSWORD, PASSWORD, ""],
            verdicts=[None],
        )
        assert any("Couldn't check it" in line for line in script.shown)
        assert dotenv_values(data / ".env")["GEMINI_API_KEY"] == KEY

    def test_a_saved_key_kept_with_enter_is_not_sent_again(self, data) -> None:
        run(data, answers=["1", "1", "", "", "y"], secrets=[KEY, PASSWORD, PASSWORD, ""])
        _, asked = self.run_checked(
            data, answers=["", "", "", "y"], secrets=["", "", ""], verdicts=[]
        )
        assert asked == []


class _Answer:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestKeyWorks:
    """The provider's own answer, read without spending anything (a model list)."""

    @pytest.fixture
    def answer(self, monkeypatch):
        import urllib.request

        sent: list = []

        def respond_with(outcome):
            def urlopen(request, timeout):
                sent.append(request)
                if isinstance(outcome, BaseException):
                    raise outcome
                return _Answer(outcome)

            monkeypatch.setattr(urllib.request, "urlopen", urlopen)
            return sent

        return respond_with

    @staticmethod
    def refusal(code: int, body: str = ""):
        import io
        import urllib.error

        return urllib.error.HTTPError("https://x", code, "no", {}, io.BytesIO(body.encode()))

    def test_accepted(self, answer) -> None:
        sent = answer(200)
        assert onboarding.key_works(onboarding.PROVIDERS["gemini"], "AIza-good") is True
        assert "AIza-good" not in sent[0].full_url, "the key travels in a header"
        assert sent[0].get_header("X-goog-api-key") == "AIza-good"

    @pytest.mark.parametrize("provider", ["anthropic", "openai"])
    def test_refused(self, answer, provider) -> None:
        answer(self.refusal(401))
        assert onboarding.key_works(onboarding.PROVIDERS[provider], "bad") is False

    def test_gemini_says_invalid_with_a_400(self, answer) -> None:
        answer(self.refusal(400, '{"error": {"details": [{"reason": "API_KEY_INVALID"}]}}'))
        assert onboarding.key_works(onboarding.PROVIDERS["gemini"], "bad") is False

    @pytest.mark.parametrize("code", [400, 429, 500, 503])
    def test_anything_else_is_unknown(self, answer, code) -> None:
        answer(self.refusal(code, "busy"))
        assert onboarding.key_works(onboarding.PROVIDERS["anthropic"], "k") is None

    def test_no_connection_is_unknown(self, answer) -> None:
        import urllib.error

        answer(urllib.error.URLError("no route"))
        assert onboarding.key_works(onboarding.PROVIDERS["openai"], "k") is None
