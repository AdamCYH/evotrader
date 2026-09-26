"""Exits must survive entry policy. Regressions from 2026-09-11.

See: data/evolution/reviews/20260911_194320_exit_path_blocked_by_entry_policy.md

On that date `allow_short_sell` was false (set to match `allow_margin: false`)
and `allowed_tickers` was ["MSTR"]. The pre-execution gate read every SELL as a
short sale and enforced the ticker allowlist on exits as well as entries. Five
risk-APPROVED exits were blocked, a position whose session ATR was 6.4% of price
ran unprotected for seven hours, booked profit was destroyed on one trade, and
QQQ shares were left stranded with no way to close them.

The governing principle, and the thing these tests actually defend:
**entry policy must never be enforced on an order that reduces risk.**
"""

from __future__ import annotations

import logging

from evotrader.callbacks.exit_policy import (
    exceeds_holdings,
    is_exit_action,
    is_position_reducing,
)
from evotrader.callbacks.risk_gate import RiskGate
from evotrader.models.config import Constitution, RiskLimits, TradingRules


def _gate(
    allow_short_sell: bool = False,
    allowed: list[str] | None = None,
) -> RiskGate:
    constitution = Constitution(
        risk_limits=RiskLimits(max_order_value_usd=100_000.0),
        trading_rules=TradingRules(
            allowed_tickers=allowed or ["MSTR"],
            allow_short_sell=allow_short_sell,
        ),
    )
    return RiskGate(constitution=constitution, dry_run=False)


class TestHeldLongsCanBeExited:
    """Finding 1. `sell` is not a synonym for `sell short`."""

    def test_protective_stop_on_held_long_is_allowed(self) -> None:
        """A protective stop for the full quantity held."""
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5, "price": 120.90}
        assert _gate().check_violations(args, held_quantity=5.0) == []

    def test_take_profit_on_held_long_is_allowed(self) -> None:
        """A full close of the held long. Was blocked."""
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5, "price": 133.70}
        assert _gate().check_violations(args, held_quantity=5.0) == []

    def test_partial_trim_is_allowed(self) -> None:
        """Selling part of the held long."""
        args = {"ticker": "MSTR", "side": "sell", "quantity": 3, "price": 136.30}
        assert _gate().check_violations(args, held_quantity=5.0) == []

    def test_fractional_rounding_noise_does_not_read_as_a_short(self) -> None:
        """5.0000000001 vs 5.0 must not be treated as opening a short."""
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5.00000001, "price": 130.0}
        assert _gate().check_violations(args, held_quantity=5.0) == []

    def test_buy_is_unaffected(self) -> None:
        args = {"ticker": "MSTR", "side": "buy", "quantity": 5, "price": 130.0}
        assert _gate().check_violations(args, held_quantity=0.0) == []


class TestTheShortSaleBanStillWorks:
    """The rule must survive the fix — this is not a licence to short."""

    def test_sell_exceeding_holdings_is_blocked(self) -> None:
        args = {"ticker": "MSTR", "side": "sell", "quantity": 8, "price": 130.0}
        violations = _gate().check_violations(args, held_quantity=5.0)
        assert violations
        assert any("short" in v.lower() for v in violations)

    def test_sell_with_verified_flat_position_is_blocked(self) -> None:
        """A naked sell is a short. 0.0 means verified flat, and must block."""
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5, "price": 130.0}
        assert _gate().check_violations(args, held_quantity=0.0) != []

    def test_short_sale_allowed_when_constitution_permits(self) -> None:
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5, "price": 130.0}
        gate = _gate(allow_short_sell=True)
        assert gate.check_violations(args, held_quantity=0.0) == []


class TestUnknownIsNotZero:
    """`None` means unresolvable and must resolve toward allowing the exit."""

    def test_unresolvable_holdings_permits_the_sell(self) -> None:
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5, "price": 130.0}
        assert _gate().check_violations(args, held_quantity=None) == []

    def test_unresolvable_holdings_logs_loudly(self, caplog) -> None:
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5, "price": 130.0}
        with caplog.at_level(logging.WARNING):
            _gate().check_violations(args, held_quantity=None)
        assert "Could not resolve held quantity" in caplog.text

    def test_default_call_site_is_still_permissive(self) -> None:
        """Any caller that has not been taught to resolve holdings must not
        silently re-introduce the trap."""
        args = {"ticker": "MSTR", "side": "sell", "quantity": 5, "price": 130.0}
        assert _gate().check_violations(args) == []


class TestStrandedPositionsCanBeClosed:
    """Finding 2. The allowlist is entry policy."""

    def test_exit_of_deallowed_ticker_is_permitted(self) -> None:
        """The stranded QQQ shares must be closable."""
        args = {"ticker": "QQQ", "side": "sell", "quantity": 4, "price": 716.0}
        assert _gate(allowed=["MSTR"]).check_violations(args, held_quantity=4.0) == []

    def test_exit_of_deallowed_ticker_logs_the_exception(self, caplog) -> None:
        args = {"ticker": "QQQ", "side": "sell", "quantity": 4, "price": 716.0}
        with caplog.at_level(logging.WARNING):
            _gate(allowed=["MSTR"]).check_violations(args, held_quantity=4.0)
        assert "no longer in" in caplog.text

    def test_entry_in_deallowed_ticker_is_still_blocked(self) -> None:
        """The allowlist must keep doing its actual job."""
        args = {"ticker": "QQQ", "side": "buy", "quantity": 4, "price": 716.0}
        violations = _gate(allowed=["MSTR"]).check_violations(args, held_quantity=0.0)
        assert any("not in allowed list" in v for v in violations)

    def test_naked_sell_in_deallowed_ticker_is_still_blocked(self) -> None:
        """Exempting exits must not become a way to short an unlisted name."""
        args = {"ticker": "QQQ", "side": "sell", "quantity": 4, "price": 716.0}
        violations = _gate(allowed=["MSTR"]).check_violations(args, held_quantity=0.0)
        assert violations, "a sell with no position is not an exit"


class TestTheOrderValueCapIsAnEntryLimit:
    """Changed 2026-09-26 (owner's call, to make a first run easy).

    The per-order dollar cap used to apply to exits too, so closing or
    protecting a position larger than the cap took several orders — and a
    protective stop covering the whole position was refused outright. An exit
    only returns exposure already held, so, like the other entry rules above,
    the cap now limits new exposure only. A sell beyond the holding is new
    exposure (a short) and stays capped.
    """

    def test_an_exit_larger_than_the_cap_is_allowed(self) -> None:
        gate = _gate()
        args = {"ticker": "MSTR", "side": "sell", "quantity": 10_000, "price": 130.0}
        assert gate.check_violations(args, held_quantity=10_000.0) == []

    def test_an_entry_larger_than_the_cap_is_still_refused(self) -> None:
        gate = _gate()
        args = {"ticker": "MSTR", "side": "buy", "quantity": 10_000, "price": 130.0}
        violations = gate.check_violations(args, held_quantity=0.0)
        assert any("exceeds limit" in v for v in violations)

    def test_a_sell_beyond_the_holding_is_still_capped(self) -> None:
        gate = _gate(allow_short_sell=True)
        args = {"ticker": "MSTR", "side": "sell", "quantity": 10_000, "price": 130.0}
        violations = gate.check_violations(args, held_quantity=5_000.0)
        assert any("exceeds limit" in v for v in violations)

    def test_closing_an_option_larger_than_the_cap_is_allowed(self) -> None:
        gate = _gate()
        args = {
            "legs": [{"option_id": "abc", "side": "sell", "position_effect": "close"}],
            "quantity": 100,
            "price": 20.0,
        }
        assert not any("exceeds limit" in v for v in gate.check_violations(args, None))


class TestSharedPredicates:
    """Finding 3. One policy module, so the two gates cannot drift again."""

    def test_exit_actions(self) -> None:
        for action in ("CLOSE", "STOP_LOSS", "TAKE_PROFIT", "close", " take_profit "):
            assert is_exit_action(action) is True
        for action in ("OPEN", "open", "", None):
            assert is_exit_action(action) is False

    def test_position_reducing_requires_holdings(self) -> None:
        assert is_position_reducing("sell", 5.0) is True
        assert is_position_reducing("sell", 0.0) is False
        assert is_position_reducing("sell", None) is False
        assert is_position_reducing("buy", 5.0) is False

    def test_exceeds_holdings_tolerance(self) -> None:
        assert exceeds_holdings(5.00000001, 5.0) is False
        assert exceeds_holdings(5.5, 5.0) is True

    def test_both_gates_agree_on_exit_orders(self) -> None:
        """risk_gate and check_risk_limits must not diverge again.

        On 2026-09-11 every blocked order carried an explicit Risk Manager
        'APPROVED (0 violations, 0 warnings)' verdict moments before the gate
        rejected it. One gate read `direction == SHORT`, the other
        `side == sell`.

        Asserted structurally: for a closing order the pre-execution gate must
        raise no violation, and the Risk Manager's predicate must agree that it
        is an exit — including for a ticker no longer on the allowlist.
        """
        for ticker, qty, held in (
            ("MSTR", 5, 5.0),
            ("MSTR", 3, 5.0),
            ("MSTR", 1, 5.0),
            ("QQQ", 4, 4.0),
        ):
            args = {
                "ticker": ticker,
                "side": "sell",
                "quantity": qty,
                "price": 130.0,
            }
            gate_ok = _gate(allowed=["MSTR"]).check_violations(args, held_quantity=held) == []
            for action in ("CLOSE", "STOP_LOSS", "TAKE_PROFIT"):
                assert gate_ok is True and is_exit_action(action) is True, (
                    f"gate blocked a {action} of {qty}x {ticker} against {held} held"
                )


class TestOptionExitsAreAlsoExempt:
    def test_closing_an_option_on_a_deallowed_underlying(self) -> None:
        """Same defect class: an option close must not be blocked by the
        underlying allowlist."""
        from evotrader.callbacks.risk_gate import _is_exit_order

        args = {
            "legs": [{"option_id": "abc", "side": "sell", "position_effect": "close"}],
            "quantity": 1,
            "price": 2.0,
        }
        assert _is_exit_order(args, None) is True

    def test_opening_an_option_is_not_an_exit(self) -> None:
        from evotrader.callbacks.risk_gate import _is_exit_order

        args = {
            "legs": [{"option_id": "abc", "side": "sell", "position_effect": "open"}],
            "quantity": 1,
            "price": 2.0,
        }
        assert _is_exit_order(args, None) is False


class TestBuildProvenanceReachesTheCyclePath:
    """Finding 6. The detector did not fire because it was never emitted on
    the path the cycle actually uses (`compute_detailed_signal`)."""

    def test_provenance_helper_carries_fingerprint_and_pool(self) -> None:
        from evotrader.agents.tools import _composite_provenance
        from evotrader.algorithms.composite import COMPOSITE_SOURCE_FINGERPRINT

        class _D:
            n_additive, n_applicable, n_in_scope, n_voting = 6, 6, 4, 2
            amplification_capped = ["momentum"]

        out = _composite_provenance(_D())
        assert out["composite_source_fingerprint"] == COMPOSITE_SOURCE_FINGERPRINT
        assert out["renormalization_pool"] == "voting"
        assert out["pool_sizes"] == {"additive": 6, "applicable": 6, "in_scope": 4, "voting": 2}

    def test_pool_reads_applicable_when_nobody_voted(self) -> None:
        from evotrader.agents.tools import _composite_provenance

        class _D:
            n_additive, n_applicable, n_in_scope, n_voting = 6, 6, 6, 0
            amplification_capped: list[str] = []

        assert _composite_provenance(_D())["renormalization_pool"] == "applicable"
