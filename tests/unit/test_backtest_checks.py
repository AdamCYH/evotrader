"""``runner --validate``: the checks, the placebo and the positive control on a real run."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from evotrader.backtest.checks import (
    cheating_signals,
    check_version,
    format_verdict_markdown,
    replay,
    shift,
    total_return_pct,
)
from evotrader.backtest.validation import out_of_sample_split
from evotrader.models.market import MarketSnapshot
from evotrader.models.signals import AlgoSignal


def _bars(sessions: int, start_price: float = 430.0, seed: int = 7) -> pd.DataFrame:
    """Hourly bars, seven a session, on a random walk."""
    rng = np.random.default_rng(seed)
    rows, opens, closes = [], [], []
    price = start_price
    for day in range(sessions):
        for hour in range(9, 16):
            rows.append(datetime(2025, 3, 1, hour, 30, tzinfo=UTC) + timedelta(days=day))
            opens.append(price)
            price *= 1 + rng.normal(0, 0.004)
            closes.append(price)
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) * 1.001 for o, c in zip(opens, closes, strict=True)],
            "low": [min(o, c) * 0.999 for o, c in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1_000_000.0] * len(rows),
        },
        index=pd.DatetimeIndex(rows),
    )


def _daily(start_price: float = 430.0) -> pd.DataFrame:
    dates = [datetime(2024, 12, 1, tzinfo=UTC) + timedelta(days=i) for i in range(160)]
    prices = [start_price + np.sin(i) for i in range(160)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 2 for p in prices],
            "low": [p - 2 for p in prices],
            "close": prices,
            "volume": [5e7] * 160,
        },
        index=pd.DatetimeIndex(dates),
    )


def _snapshots(sessions: int = 40, price: float = 430.0) -> list[MarketSnapshot]:
    from evotrader.backtest.runner import build_snapshots

    return build_snapshots(_bars(sessions, price), _daily(price), ticker="TEST")


def _version(starter_data_dir: Path) -> str:
    algorithms = starter_data_dir / "algorithms"
    return next(
        p.name for p in sorted(algorithms.iterdir()) if p.is_dir() and (p / "config.yaml").is_file()
    )


def _sig(value: float) -> AlgoSignal:
    return AlgoSignal(name="x", value=value, weight=1.0)


class TestShift:
    def test_zero_is_identity_and_rotation_wraps(self) -> None:
        sigs = [_sig(v) for v in (0.1, 0.2, 0.3, 0.4)]
        assert shift(sigs, 0) == sigs
        assert [s.value for s in shift(sigs, 1)] == [0.2, 0.3, 0.4, 0.1]


class TestCheatingSignals:
    def test_reads_the_move_the_decision_would_earn(self) -> None:
        snaps = _snapshots(sessions=3)
        opens = [s.recent_candles[-1].open for s in snaps]
        closes = [s.recent_candles[-1].close for s in snaps]

        next_open = cheating_signals(snaps, next_bar_open_fill=True)
        for i in range(len(snaps) - 2):
            assert next_open[i].value == np.sign(opens[i + 2] - opens[i + 1])
        assert next_open[-1].value == 0.0

        same_bar = cheating_signals(snaps, next_bar_open_fill=False)
        for i in range(len(snaps) - 1):
            assert same_bar[i].value == np.sign(closes[i + 1] - closes[i])


class TestCheckVersion:
    def test_replay_of_the_real_signal_reproduces_the_run(self, starter_data_dir: Path) -> None:
        """The placebos go through the same engine; unshifted, they must give the same answer."""
        from evotrader.backtest.runner import RiskParams, simulate_version

        snaps = _snapshots()
        risk = RiskParams()
        metrics, signals, engine = simulate_version(_version(starter_data_dir), snaps, risk=risk)
        again = replay(snaps, signals, risk)
        assert total_return_pct(again) == pytest.approx(total_return_pct(engine))
        assert round(total_return_pct(engine), 2) == metrics["total_return_pct"]

    def test_positive_control_passes_and_every_check_is_reported(
        self, starter_data_dir: Path
    ) -> None:
        from evotrader.backtest.runner import RiskParams, simulate_version

        snaps = _snapshots()
        risk = RiskParams()
        version = _version(starter_data_dir)
        _, signals, engine = simulate_version(version, snaps, risk=risk)
        verdict = check_version(version, snaps, signals, engine, risk, n_placebos=20)

        names = [c.name for c in verdict.report.checks]
        assert names[0] == "positive control"
        assert verdict.report.checks[0].passed, verdict.report.checks[0].detail
        for expected in ("clustered t-stat", "out-of-sample", "vs benchmark", "placebo"):
            assert expected in names
        assert len(verdict.placebo_returns) == 20
        assert verdict.control_return_pct is not None and verdict.control_return_pct > 0
        assert "Verdict" in format_verdict_markdown(verdict)

    def test_multiple_testing_only_when_several_were_tried(self, starter_data_dir: Path) -> None:
        from evotrader.backtest.runner import RiskParams, simulate_version

        snaps = _snapshots()
        risk = RiskParams()
        version = _version(starter_data_dir)
        _, signals, engine = simulate_version(version, snaps, risk=risk)
        one = check_version(version, snaps, signals, engine, risk, n_placebos=20)
        many = check_version(
            version, snaps, signals, engine, risk, n_placebos=20, n_variants_tested=10
        )
        assert "multiple testing" not in [c.name for c in one.report.checks]
        assert "multiple testing" in [c.name for c in many.report.checks]

    def test_unaffordable_instrument_fails_the_positive_control(
        self, starter_data_dir: Path
    ) -> None:
        """A price above the per-trade allocation turns every signal into no trade."""
        from evotrader.backtest.runner import RiskParams, simulate_version

        snaps = _snapshots(price=100_000.0)
        risk = RiskParams()  # $2,500 a trade, whole units only
        version = _version(starter_data_dir)
        _, signals, engine = simulate_version(version, snaps, risk=risk)
        verdict = check_version(version, snaps, signals, engine, risk, n_placebos=20)

        control = verdict.report.checks[0]
        assert control.name == "positive control"
        assert not control.passed
        assert "--fractional" in control.detail
        assert not verdict.passed

    def test_logs_one_failure_count_matching_the_report(
        self, starter_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The log and the report must agree on how many checks failed."""
        from evotrader.backtest.runner import RiskParams, simulate_version

        snaps = _snapshots()
        risk = RiskParams()
        version = _version(starter_data_dir)
        _, signals, engine = simulate_version(version, snaps, risk=risk)
        with caplog.at_level("WARNING"):
            verdict = check_version(version, snaps, signals, engine, risk, n_placebos=20)
        lines = [r.getMessage() for r in caplog.records if "validation check" in r.getMessage()]
        assert len(lines) == 1
        assert f"failed {len(verdict.report.failures)} validation check" in lines[0]

    def test_too_short_to_judge(self, starter_data_dir: Path) -> None:
        from evotrader.backtest.runner import RiskParams, simulate_version

        snaps = _snapshots(sessions=5)  # 35 bars
        risk = RiskParams()
        version = _version(starter_data_dir)
        _, signals, engine = simulate_version(version, snaps, risk=risk)
        verdict = check_version(version, snaps, signals, engine, risk)
        assert not verdict.report.checks[0].passed
        assert "too short" in verdict.report.checks[0].detail
        assert not verdict.passed


def test_out_of_sample_split_mixes_naive_and_zoned_times() -> None:
    """Trade times and the split may differ in having a time zone; that is not an error."""
    naive = [datetime(2025, 1, 1) + timedelta(days=i) for i in range(60)]
    res = out_of_sample_split([0.01] * 60, naive, datetime(2025, 1, 31, tzinfo=UTC))
    assert res.name == "out-of-sample"
    empty = out_of_sample_split([], [], datetime(2025, 1, 31, tzinfo=UTC))
    assert not empty.passed


class TestCli:
    def test_validate_from_csv_files_exits_3_when_nothing_survives(
        self,
        starter_data_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Both CSVs given: no download. A null result is exit 3, not a crash."""
        from evotrader.backtest import data as data_module
        from evotrader.backtest import runner

        intraday, daily = tmp_path / "i.csv", tmp_path / "d.csv"
        _bars(40).to_csv(intraday)
        _daily().to_csv(daily)

        def no_network(*_a: object, **_k: object) -> None:
            raise AssertionError("must not download when both CSVs are given")

        monkeypatch.setattr(data_module.yf, "download", no_network)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "runner",
                "--ticker",
                "TEST",
                "--intraday-csv",
                str(intraday),
                "--daily-csv",
                str(daily),
                "--validate",
                "--placebos",
                "20",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            runner.main()
        assert exc.value.code == 3
        out = capsys.readouterr().out
        assert "## Validation" in out
        assert "positive control" in out
        report = next((starter_data_dir / "backtest" / "reports").glob("report_*.md"))
        assert "## Validation" in report.read_text()
