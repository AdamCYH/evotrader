"""FastAPI Web Server for the EvoTrader Dashboard.

Provides JSON endpoints and Server-Sent Events (SSE) for monitoring agent thoughts,
approving trade proposals, triggering trading cycles, and reconciling positions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from evotrader.db.journal import TradeJournal
from evotrader.db.metrics import MetricsStore
from evotrader.db.reconciliation import ReconciliationService
from evotrader.indicators import normalize_snapshot_payload
from evotrader.tools.asset_context import primary_ticker
from evotrader.tools.market_hours import ET
from evotrader.utils import select_agentic_account, utc_timestamp_to_et_date

logger = logging.getLogger("evotrader.web")


def _log_time_iso(row: dict) -> str | None:
    """When an evolution-log row was written, as an ISO 8601 time with its zone.

    The row has two clocks. ``timestamp`` is written by Python with the zone
    included. ``created_at`` is SQLite's ``datetime('now')``: UTC, but with a
    space and no zone, which browsers read as LOCAL time (Chrome) or not at all
    (Safari), so a date shown from it is hours off or blank.
    """
    ts = row.get("timestamp")
    if ts:
        return str(ts)
    created = row.get("created_at")
    if not created:
        return None
    text = str(created).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?", text):
        return text.replace(" ", "T") + "+00:00"
    return text


# ═══════════════════════════════════════════════════════════════════════
# Event Streaming & Log Capturing Setup
# ═══════════════════════════════════════════════════════════════════════

active_listeners: set[asyncio.Queue] = set()
_active_app: FastAPI | None = None
pending_oauth: dict | None = None  # Tracks pending OAuth state for replay on reconnect
# How long a raised authorization prompt stays actionable. Matches the flow's
# own timeout in evotrader.mcp.oauth.wait_for_callback.
_OAUTH_PROMPT_TTL_S = 300.0


def broker_signin_pending() -> bool:
    """A broker sign-in prompt is up and still current, so broker calls would wait on it."""
    import time as _time

    if not isinstance(pending_oauth, dict):
        return pending_oauth is not None
    issued = pending_oauth.get("issued_at", 0.0)
    return not issued or _time.time() - issued <= _OAUTH_PROMPT_TTL_S


async def check_interactive_approval(args: dict[str, Any]) -> dict[str, Any] | None:
    """If the dashboard is running, suspend the agent and wait for user approval."""
    global _active_app
    if not _active_app:
        return None

    loop = asyncio.get_event_loop()
    future = loop.create_future()

    _active_app.state.pending_order_id_counter += 1
    order_id = _active_app.state.pending_order_id_counter

    # Set up pending state
    _active_app.state.pending_order = {
        "id": order_id,
        "args": args,
        "future": future,
    }
    _active_app.state.status = "waiting_approval"

    # Notify dashboard clients
    broadcast_sse_event("status", "waiting_approval")
    broadcast_sse_event(
        "pending_order",
        {
            "id": order_id,
            "args": args,
        },
    )

    logger.info("⏸️  Pending trade waiting for user approval in Web Console (ID: %d)...", order_id)

    # Wait for the future to be resolved by /api/trades/approve or /api/trades/reject
    result = await future
    return result


class SSELogHandler(logging.Handler):
    """Custom logging handler to broadcast logs to SSE clients in real-time."""

    def __init__(self) -> None:
        super().__init__()
        self.formatter = logging.Formatter(
            "%(asctime)s │ %(name)-18s │ %(levelname)-7s │ %(message)s", "%Y-%m-%d %H:%M:%S"
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_entry = self.format(record)
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(broadcast_sse_event, "log", log_entry)
        except Exception:
            pass


def broadcast_sse_event(event_type: str, data: Any) -> None:
    """Helper to broadcast formatted SSE events to all connected clients."""
    import json

    global pending_oauth
    # Track oauth state so new SSE connections can replay it. Stamped, so a
    # prompt that outlives its authorization window is never replayed.
    if event_type == "oauth_required":
        import time as _time

        pending_oauth = {**data, "issued_at": _time.time()} if isinstance(data, dict) else data

    payload = json.dumps({"type": event_type, "data": data})
    for q in active_listeners:
        q.put_nowait(payload)


def end_event_streams() -> None:
    """End every open live event stream, so a stopping server need not wait for browser tabs."""
    for q in active_listeners:
        q.put_nowait(None)


# ═══════════════════════════════════════════════════════════════════════
# FastAPI App Construction
# ═══════════════════════════════════════════════════════════════════════


class RevalidatedStaticFiles(StaticFiles):
    """The console's files, sent so the browser checks for a newer version on every load.

    With no Cache-Control header a browser picks its own freshness, and a file
    last modified days before it was fetched is reused for hours without
    asking. The page names its modules by fixed paths (``js/api.js``), so after
    an update a browser could run the new ``js/app.js`` with its old
    ``js/api.js``. With ``no-cache`` it asks every load; an unchanged file
    costs a 304.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _permitted_tickers(config: Any) -> tuple[str, ...]:
    """Symbols the agents may trade, primary first.

    Prefers the bound asset context, which is the same source the agent
    instructions render from — so the console shows what the agents were
    actually told, not a second opinion assembled from config. Falls back to
    config for the window before startup binds it.
    """
    from evotrader.tools.asset_context import allowed_tickers

    bound = allowed_tickers()
    if bound:
        return bound

    primary = (config.settings.asset.primary_ticker or "").upper()
    rules = getattr(config.constitution, "trading_rules", None)
    allowed = [str(t).upper() for t in (getattr(rules, "allowed_tickers", None) or [])]
    if primary and primary not in allowed:
        allowed.insert(0, primary)
    return tuple(allowed)


def _parse_yaml_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a markdown file (between --- markers)."""
    import yaml

    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _resolve_evolution_file(config: Any, file_id: str) -> tuple[Path | None, str]:
    """Find an evolution file by ID in reviews/ or proposals/ directories.

    Returns (file_path, item_type) or (None, "") if not found.
    """
    reviews_dir = config.data_dir / "evolution" / "reviews"
    review_path = reviews_dir / f"{file_id}.md"
    if review_path.is_file():
        return review_path, "code_review"

    proposals_dir = config.data_dir / "evolution" / "proposals"
    proposal_path = proposals_dir / f"{file_id}.md"
    if proposal_path.is_file():
        return proposal_path, "strategy_proposal"

    return None, ""


def _update_yaml_frontmatter_status(content: str, new_status: str) -> str:
    """Update the 'status' field in YAML frontmatter of a proposal file."""
    import re

    if not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content
    frontmatter = parts[1]
    updated_fm = re.sub(
        r"^status:\s*.*$",
        f"status: {new_status}",
        frontmatter,
        flags=re.MULTILINE,
    )
    return f"---{updated_fm}---{parts[2]}"


def create_app(
    db: Any,
    journal: TradeJournal,
    metrics: MetricsStore,
    mcp_toolset: Any,
    config: Any,
    runner_fn: Any,  # Function to run the orchestrator cycle
    memory: Any,  # Configured SemanticMemory instance
    evolution_service: Any = None,
    evolution_db: Any = None,  # The caller's open connection to the live DB, if any
) -> FastAPI:
    """Create and configure the FastAPI application."""
    global _active_app

    async def _execute_cycle(source: str) -> None:
        app.state.is_running = True
        app.state.status = "running"
        app.state.active_cycle_task = asyncio.current_task()
        app.state.thoughts = []
        broadcast_sse_event("clear_thoughts", {})
        broadcast_sse_event("status", app.state.status)
        logger.info("🎬  Starting %s trading cycle...", source)
        try:
            await app.state.runner_fn()
        except asyncio.CancelledError:
            logger.info("🛑  Active %s cycle execution was cancelled.", source)
        except Exception as e:
            logger.error("Error during %s cycle execution: %s", source, e, exc_info=True)
        finally:
            # Invalidate cache so completed cycle details reload fresh
            app.state.cached_portfolio = None
            app.state.cached_portfolio_time = 0.0
            app.state.is_running = False
            app.state.status = "idle"
            app.state.pending_order = None
            app.state.active_cycle_task = None
            broadcast_sse_event("status", app.state.status)
            logger.info("💤  Trading cycle complete. Agent returned to idle state.")

    async def _execute_evolution(source: str, user_comment: str = "") -> None:
        timeout = config.settings.schedule.max_evolution_duration_seconds
        logger.info("🎬  Starting %s self-evolution run... (timeout: %ds)", source, timeout)
        app.state.active_evolution_task = asyncio.current_task()
        try:
            await asyncio.wait_for(
                app.state.evolution_service.trigger(user_comment=user_comment), timeout=timeout
            )
        except asyncio.CancelledError:
            logger.info("🛑  Active %s evolution execution was cancelled.", source)
        except TimeoutError:
            logger.error(
                "⏰  %s evolution timed out after %d seconds — forcefully terminated.",
                source,
                timeout,
            )
            # Mark the service so it can distinguish timeout from user cancel
            app.state.evolution_service.mark_timeout()
        except Exception as e:
            logger.error("Error during %s evolution execution: %s", source, e, exc_info=True)
        finally:
            app.state.active_evolution_task = None

    def _keep_sync_result(result: dict[str, Any]) -> None:
        """Log the sync's cost-basis check and keep it for the next cycle's agents."""
        from evotrader.db.reconciliation import save_last_sync

        logger.info("[RECONCILE] Cost basis check: %s", result.get("cost_basis_check"))
        try:
            save_last_sync(config.data_dir, result)
        except Exception as e:  # never let bookkeeping break the sync
            logger.warning("Could not keep the position sync's result: %s", e)

    async def _execute_metrics(source: str) -> None:
        logger.info("📊  Starting %s daily metrics generation...", source)
        try:
            import datetime

            from evotrader.db.reconciliation import ReconciliationService
            from evotrader.models.config import TradingMode
            from evotrader.models.portfolio import DailyMetrics

            is_dry_run = (
                config.settings.dry_run.enabled
                if config.settings.mode != TradingMode.SIM
                else False
            )
            recon = ReconciliationService(app.state.journal, app.state.mcp_toolset, is_dry_run)
            ticker = config.settings.asset.primary_ticker
            sync_result = await recon.reconcile_positions(ticker=ticker)
            _keep_sync_result(sync_result)

            today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
            existing_list = await app.state.metrics.get_metrics_range(today, today)
            current_metric = (
                existing_list[0]
                if existing_list
                else DailyMetrics(
                    date=today,
                    portfolio_value=0.0,
                    cash_balance=0.0,
                    daily_pnl=0.0,
                    daily_return_pct=0.0,
                    cumulative_return=0.0,
                )
            )

            port_data = await get_portfolio()
            current_metric.portfolio_value = port_data["broker"]["total_value"]
            current_metric.cash_balance = port_data["broker"]["cash"]
            await app.state.metrics.save_daily_metrics(current_metric)
        except Exception as e:
            logger.error("Error during %s metrics execution: %s", source, e, exc_info=True)

    # The event loop keeps only a weak reference to a task; a scheduled cycle
    # that nothing else refers to can be garbage-collected mid-run. Hold one
    # until it finishes (see asyncio.create_task).
    background_tasks: set[asyncio.Task] = set()

    def _spawn(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    async def _record_skip(skip: Any) -> None:
        """A due trigger that could not start: logged, and a cycle or an
        evolution run kept in the run history (the metrics job is not a run)."""
        logger.warning(
            "Cron %s due %s ET skipped: %s.", skip.job, skip.due_at.strftime("%H:%M"), skip.reason
        )
        if skip.job not in ("cycle", "evolution"):
            return
        try:
            await app.state.thought_logger.record_skipped_run(
                "EVOLUTION" if skip.job == "evolution" else "TRADING", skip.due_at, skip.reason
            )
        except Exception as e:  # never let bookkeeping stop the scheduler
            logger.warning("Could not record the skipped %s run: %s", skip.job, e)

    from evotrader.web.schedule_slot import CronScheduler

    scheduler = CronScheduler()
    reported_deferrals: set[tuple[str, datetime]] = set()

    async def _scheduler_poll(now: datetime) -> None:
        """One poll of the scheduled jobs (see web.schedule_slot).

        A due trigger waits while another agent task runs and starts when it
        can, inside its grace window; a minute the loop did not see still
        starts late. A trigger that cannot start in time is recorded as a
        SKIPPED run instead of vanishing.
        """
        from evotrader.cron import iter_cron_expressions

        schedule_config = app.state.config.settings.schedule
        schedules = {
            "cycle": iter_cron_expressions(schedule_config.cycle_cron)
            if getattr(app.state, "cycle_cron_enabled", False)
            else [],
            "evolution": iter_cron_expressions(schedule_config.evolution_cron)
            if getattr(app.state, "evolution_cron_enabled", False)
            else [],
            "metrics": iter_cron_expressions(schedule_config.metrics_cron),
        }
        busy = bool(app.state.is_running) or bool(
            app.state.evolution_service and app.state.evolution_service.is_running()
        )
        tick = scheduler.tick(now, schedules, busy=busy)

        for skip in tick.skipped:
            _spawn(_record_skip(skip))
        for job, due in tick.deferred:
            if (job, due) not in reported_deferrals:
                reported_deferrals.add((job, due))
                logger.warning(
                    "Cron %s due %s ET waits: another task is running.", job, due.strftime("%H:%M")
                )
        for start in tick.start:
            if start.late_reason:
                logger.warning(
                    "Cron %s due %s ET starts late (%s): %s.",
                    start.job,
                    start.due_at.strftime("%H:%M"),
                    now.strftime("%H:%M"),
                    start.late_reason,
                )
            if start.job == "cycle":
                # Claimed now, not when the task first runs, so nothing else
                # can start in between.
                app.state.is_running = True
                app.state.cycle_due = {"due_at": start.due_at, "late_reason": start.late_reason}
                try:
                    _spawn(_execute_cycle("scheduled automated"))
                except Exception:
                    app.state.is_running = False
                    app.state.cycle_due = None
                    raise
            elif start.job == "evolution":
                _spawn(_execute_evolution("scheduled automated"))
            else:
                _spawn(_execute_metrics("scheduled automated"))

    async def scheduler_loop(app: FastAPI) -> None:
        from evotrader.tools.market_hours import _resolve_now

        while True:
            try:
                await asyncio.sleep(10)
                await _scheduler_poll(_resolve_now())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in scheduler background loop: %s", e, exc_info=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Open the evolution DB if the console opened it itself
        if app.state.owns_evolution_db:
            await app.state.evolution_db.initialize()

        # Synchronize ChromaDB trade experiences with confirmed journal records
        if getattr(app.state, "memory", None) and getattr(app.state, "journal", None):
            try:
                await app.state.memory.sync_experiences_from_journal(app.state.journal)
            except Exception as ex:
                logger.warning("Failed to synchronize ChromaDB experiences on startup: %s", ex)

        app.state.scheduler_task = asyncio.create_task(scheduler_loop(app))
        logger.info("⏰  Background scheduler loop started.")
        yield
        if getattr(app.state, "scheduler_task", None):
            app.state.scheduler_task.cancel()
            try:
                await app.state.scheduler_task
            except asyncio.CancelledError:
                pass
            logger.info("⏰  Background scheduler loop stopped.")

        if app.state.owns_evolution_db:
            await app.state.evolution_db.close()

    app = FastAPI(title="EvoTrader Console", lifespan=lifespan)
    _active_app = app

    # No cross-origin access by default. The console is served by this same
    # app, so it needs none — and granting it to every origin let any website
    # the user visited read the API and the live event stream, which a
    # browser's EventSource cannot put behind the password. A separate front end
    # in development can be named in EVOTRADER_CORS_ORIGINS (comma-separated).
    cors_origins = [
        origin.strip()
        for origin in os.environ.get("EVOTRADER_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    # In-memory queues for state sharing
    app.state.db = db
    app.state.journal = journal
    app.state.metrics = metrics
    app.state.mcp_toolset = mcp_toolset
    app.state.config = config
    app.state.runner_fn = runner_fn
    app.state.memory = memory
    app.state.evolution_service = evolution_service

    from pathlib import Path

    from evotrader.db.connection import Database
    from evotrader.db.evolution_log import EvolutionLogStore
    from evotrader.db.telemetry import TelemetryReader
    from evotrader.db.thought_log import ThoughtLogger

    app.state.thought_logger = ThoughtLogger(db)

    # Ensure evolution state is always shared by pointing to the live DB. Reuse
    # the caller's connection when there is one: writes take turns per
    # connection, so a second connection to the same file would bypass that.
    evolution_db_path = config.data_dir / "db" / "evotrader.db"
    app.state.owns_evolution_db = False
    if evolution_db is not None:
        app.state.evolution_db = evolution_db
    elif evolution_db_path == db._db_path:
        app.state.evolution_db = db
    else:
        app.state.evolution_db = Database(evolution_db_path)
        app.state.owns_evolution_db = True

    app.state.evolution_store = EvolutionLogStore(app.state.evolution_db)
    app.state.telemetry_reader = TelemetryReader(Path(config.db_dir) / "telemetry.db")

    # State tracking
    app.state.is_running = False
    # The scheduled cycle starting now: its due minute and why it is late, if
    # it is (read once by the cycle, which records it).
    app.state.cycle_due = None
    # One poll of the scheduled jobs; the loop calls it every ten seconds.
    app.state.scheduler_poll = _scheduler_poll
    app.state.cycle_cron_enabled = getattr(config.settings.schedule, "cycle_cron_enabled", False)
    app.state.evolution_cron_enabled = getattr(
        config.settings.schedule, "evolution_cron_enabled", False
    )
    app.state.status = "idle"  # idle, running, waiting_approval, evolving
    app.state.pending_order = None  # Holds {"future": Future, "args": dict, "id": int}
    app.state.pending_order_id_counter = 0
    app.state.thoughts = []
    app.state.cached_portfolio = None
    app.state.cached_portfolio_time = 0.0

    # Attach logger handler
    sse_handler = SSELogHandler()
    logging.getLogger().addHandler(sse_handler)

    # Security check dependency
    def verify_auth(authorization: str | None = Header(None)) -> None:
        password = os.environ.get("DASHBOARD_PASSWORD")
        if not password:
            return  # No password configured, bypass authentication

        expected_token = f"Bearer {password}"
        if not authorization or authorization != expected_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized access token.",
            )

    # Static assets directory
    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    # ── API Endpoints ───────────────────────────────────────────

    @app.post("/api/auth/verify")
    async def verify_password(payload: dict[str, str]) -> dict[str, str]:
        password = os.environ.get("DASHBOARD_PASSWORD")
        if not password:
            return {"status": "ok", "message": "No authentication required."}
        if payload.get("password") == password:
            return {"status": "ok", "token": password}
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")

    @app.post("/api/auth/oauth_callback", dependencies=[Depends(verify_auth)])
    async def oauth_callback(payload: dict[str, str]) -> dict[str, str]:
        global pending_oauth
        callback_url = payload.get("url")
        if not callback_url:
            raise HTTPException(status_code=400, detail="Missing callback url.")

        # Extract code and state from the pasted redirect URL
        import urllib.parse as _urlparse

        parsed = _urlparse.urlparse(callback_url)
        query = _urlparse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]

        if not code:
            raise HTTPException(
                status_code=400,
                detail="Could not extract authorization code from the URL. "
                "Make sure you pasted the full redirect URL.",
            )

        # Resolve the pending OAuth Future directly (no localhost HTTP needed)
        from evotrader.mcp.oauth import resolve_pending_callback

        resolved = resolve_pending_callback(code, state)
        if resolved is True:
            pending_oauth = None
            return {"status": "ok"}
        if isinstance(resolved, str):
            # State mismatch — return the error message to the UI
            raise HTTPException(status_code=400, detail=resolved)

        # No pending Future. Either the browser redirect already landed on
        # localhost:3893 before this paste, or the flow timed out and the system
        # reconnected on its own. Neither case needs a restart, and telling the
        # operator to restart a healthy system is how an hour gets lost.
        was_prompting = pending_oauth is not None
        pending_oauth = None
        broadcast_sse_event("oauth_complete", {})
        return {
            "status": "ok",
            "message": (
                "That authorization window had already closed — the system is "
                "not waiting on it. No restart is needed: it will prompt again "
                "the next time it needs broker access."
                if was_prompting
                else "Already authenticated."
            ),
        }

    @app.get("/api/portfolio", dependencies=[Depends(verify_auth)])
    async def get_portfolio(period: str = "today") -> dict[str, Any]:
        """Combine broker account value with local database metrics."""
        import time

        now = time.time()
        cached_data = getattr(app.state, "cached_portfolio", None)
        cached_time = getattr(app.state, "cached_portfolio_time", 0.0)

        # Align cache to 30-minute boundaries of the hour (e.g. XX:00 and XX:30)
        # 1800 seconds = 30 minutes. If both now and cached_time belong to the same
        # 30-minute slots since the epoch, they are within the same clock half-hour window.
        same_window = int(now // 1800) == int(cached_time // 1800)

        if cached_data and same_window:
            broker_total = cached_data["total_value"]
            broker_cash = cached_data["cash"]
            broker_bp = cached_data["buying_power"]
            broker_positions = cached_data["positions"]
        elif broker_signin_pending():
            # The broker can't answer until the sign-in is done; asking would
            # hold this request (and every later one) until the sign-in times out.
            broker_total = broker_cash = broker_bp = 0.0
            broker_positions = []
        else:
            from evotrader.models.config import TradingMode

            is_dry_run = (
                config.settings.dry_run.enabled
                if config.settings.mode != TradingMode.SIM
                else False
            )
            recon = ReconciliationService(journal, mcp_toolset, is_dry_run)

            # Default empty portfolio response
            broker_total = 0.0
            broker_cash = 0.0
            broker_bp = 0.0
            broker_positions = []

            try:
                positions = await recon.get_broker_positions()
                broker_positions = [
                    {
                        "symbol": p.get("symbol"),
                        "quantity": float(p.get("quantity", 0.0)),
                        "avg_price": float(p.get("average_buy_price", 0.0)),
                        "asset_type": p.get("asset_type", "EQUITY"),
                        "option_id": p.get("option_id"),
                        "option_type": p.get("option_type"),
                        "strike": p.get("strike"),
                        "expiration": p.get("expiration"),
                    }
                    for p in positions
                ]

                # Fetch current prices and compute unrealized P&L
                unique_symbols = {
                    bp["symbol"]
                    for bp in broker_positions
                    if bp["asset_type"] == "EQUITY" and bp["symbol"]
                }
                price_cache: dict[str, float] = {}
                for sym in unique_symbols:
                    price = await recon.get_current_stock_price(sym)
                    if price is not None:
                        price_cache[sym] = price

                for bp in broker_positions:
                    qty = bp["quantity"]
                    avg = bp["avg_price"]
                    if bp["asset_type"] == "EQUITY" and bp["symbol"] in price_cache:
                        cur = price_cache[bp["symbol"]]
                        bp["current_price"] = cur
                        # For shorts (qty < 0): profit when price drops
                        bp["unrealized_pnl"] = (cur - avg) * qty
                    elif bp["asset_type"] == "OPTION" and bp.get("option_id"):
                        opt_price = await recon.get_current_option_price(bp["option_id"])
                        if opt_price is not None:
                            bp["current_price"] = opt_price
                            bp["unrealized_pnl"] = (opt_price - avg) * abs(qty) * 100
                        else:
                            bp["current_price"] = avg
                            bp["unrealized_pnl"] = 0.0
                    else:
                        bp["current_price"] = avg
                        bp["unrealized_pnl"] = 0.0

                # Fetch cash & portfolio metrics
                if mcp_toolset:
                    session = await mcp_toolset._mcp_session_manager.create_session()
                    accounts_res = await session.call_tool("get_accounts", arguments={})
                    if not getattr(accounts_res, "isError", False) and accounts_res.content:
                        import json

                        accs = (
                            json.loads(accounts_res.content[0].text)
                            .get("data", {})
                            .get("accounts", [])
                        )
                        selected_acc = select_agentic_account(accs)

                        if selected_acc:
                            port_res = await session.call_tool(
                                "get_portfolio", arguments={"account_number": selected_acc}
                            )
                            if not getattr(port_res, "isError", False) and port_res.content:
                                port_data = json.loads(port_res.content[0].text).get("data", {})
                                broker_total = float(port_data.get("total_value", 0.0))
                                broker_cash = float(port_data.get("cash", 0.0))
                                broker_bp = float(
                                    port_data.get("buying_power", {}).get("buying_power", 0.0)
                                )

                # Save cache
                app.state.cached_portfolio = {
                    "total_value": broker_total,
                    "cash": broker_cash,
                    "buying_power": broker_bp,
                    "positions": broker_positions,
                }
                app.state.cached_portfolio_time = now
            except Exception as e:
                logger.warning("Could not fetch real-time broker metrics: %s", e)

        # Get local stats
        open_trades = await journal.get_open_trades()
        db_holdings = {}
        for t in open_trades:
            ticker = t["ticker"]
            qty = float(t["remaining_quantity"])
            if t["direction"] == "SHORT":
                qty = -qty
            if ticker not in db_holdings:
                db_holdings[ticker] = {"quantity": 0.0, "total_cost": 0.0}
            db_holdings[ticker]["quantity"] += qty
            db_holdings[ticker]["total_cost"] += qty * float(t["price"])

        if hasattr(journal, "get_period_metrics"):
            import inspect

            res = journal.get_period_metrics(period)
            period_metrics = await res if inspect.isawaitable(res) else res
        else:
            pnl_val = await journal.get_pnl(period)
            count_val = await journal.get_trade_count(period)
            w_l = await journal.get_period_win_loss_count(period)
            period_metrics = {
                "pnl": pnl_val,
                "trade_count": count_val,
                "wins": w_l[0] if isinstance(w_l, (tuple, list)) else 0,
                "losses": w_l[1] if isinstance(w_l, (tuple, list)) else 0,
            }

        streak = await journal.get_consecutive_losses()
        no_trade_streak = await journal.get_consecutive_no_trades(period)

        # Get active algorithms
        algo_active = config.algorithms_dir / "active.yaml"
        algo_version = "v001_initial"
        if algo_active.is_file():
            import yaml

            try:
                with open(algo_active) as f:
                    algo_version = yaml.safe_load(f).get("active_version", algo_version)
            except Exception:
                pass

        return {
            "broker": {
                "total_value": broker_total,
                "cash": broker_cash,
                "buying_power": broker_bp,
                "positions": broker_positions,
            },
            "local": {
                "open_trades": db_holdings,
                "period_pnl": period_metrics["pnl"],
                "consecutive_losses": streak,
                "period_trades": period_metrics["trade_count"],
                "period_wins": period_metrics["wins"],
                "period_losses": period_metrics["losses"],
                "consecutive_no_trades": no_trade_streak,
                "period": period,
                "dry_run": config.settings.dry_run.enabled,
                "algo_version": algo_version,
                "ticker": config.settings.asset.primary_ticker,
                "tickers": list(_permitted_tickers(config)),
                "db_path": (
                    str(config.db_path.relative_to(config.project_root))
                    if config.db_path.is_relative_to(config.project_root)
                    else str(config.db_path)
                ),
            },
            "status": app.state.status,
            "pending_order": (
                {
                    "id": app.state.pending_order["id"],
                    "args": app.state.pending_order["args"],
                }
                if app.state.pending_order
                else None
            ),
        }

    @app.get("/api/chart/tech", dependencies=[Depends(verify_auth)])
    async def get_tech_chart(
        limit: int = 500, period: str = "all", ticker: str | None = None
    ) -> dict[str, Any]:
        """One instrument's recent snapshots (indicators and the algorithm's signal).

        ``ticker`` picks the instrument; without it, the configured primary.
        Each instrument's signal is read on its own prices, so a chart of two
        (a stock and its inverse fund) zigzagged between a reading and its
        mirror image. ``tickers`` lists the instruments to choose from: the
        primary first, then every other one with snapshots, newest first.
        """
        logger_db = app.state.thought_logger
        points = []
        latest = None
        primary = (config.settings.asset.primary_ticker or "").strip().upper()
        tickers = [primary] if primary else []
        try:
            for row in await logger_db.snapshot_tickers():
                symbol = str(row["ticker"]).upper()
                if symbol not in tickers:
                    tickers.append(symbol)
        except Exception as e:
            logger.error("Failed to list the instruments with snapshots: %s", e)
        chosen = (ticker or "").strip().upper() or (tickers[0] if tickers else None)
        try:
            all_points = await logger_db.get_market_snapshots(limit=limit, ticker=chosen)
            if all_points:
                latest = all_points[0]  # Most recent snapshot in DB

            points = all_points
            if period and period != "all":
                from evotrader.utils import get_period_cutoff_utc_str

                cutoff_str = get_period_cutoff_utc_str(period)

                if cutoff_str:
                    filtered = [p for p in points if (p.get("timestamp") or "") >= cutoff_str]
                    # If period filter yields points, use them; otherwise fallback to recent points
                    points = filtered if filtered else all_points[:20]

            points.reverse()  # Reverse to chronological order (oldest to newest)
        except Exception as e:
            logger.error("Failed to fetch tech chart: %s", e)

        if not latest and hasattr(app.state, "journal") and app.state.journal:
            try:
                trades = await app.state.journal.get_recent_trades(limit=1)
                if trades and trades[0].get("market_snapshot"):
                    raw_snap = json.loads(trades[0]["market_snapshot"])
                    if isinstance(raw_snap, dict):
                        raw_snap.setdefault("timestamp", trades[0].get("timestamp"))
                        raw_snap.setdefault("ticker", trades[0].get("ticker") or primary_ticker())
                        # Only the chosen instrument's: another's would put its
                        # price and signal under this one's name.
                        if not chosen or str(raw_snap.get("ticker") or "").upper() == chosen:
                            latest = normalize_snapshot_payload(raw_snap)
            except Exception:
                pass

        return {"points": points, "latest": latest, "ticker": chosen, "tickers": tickers}

    @app.get("/api/chart/signal-daily", dependencies=[Depends(verify_auth)])
    async def get_signal_daily(period: str = "all") -> dict[str, Any]:
        """The combined signal per trading day, for the account chart's overlay.

        One row per US Eastern date (``web.signal_summary``), cut at the same
        date as the equity curve for the period, so the two line up day by day.
        """
        from evotrader.utils import get_period_cutoff_dt
        from evotrader.web.signal_summary import daily_signal_summary

        cutoff = get_period_cutoff_dt(period, ET) if period and period != "all" else None
        # A day of margin: an Eastern date starts four or five hours into the UTC one.
        since = (cutoff - timedelta(days=1)).astimezone(UTC).isoformat() if cutoff else None
        try:
            readings = await app.state.thought_logger.get_composite_readings(since)
        except Exception as e:
            logger.error("Failed to read the signal history: %s", e)
            return {"days": [], "error": "signal history unavailable"}
        primary = (config.settings.asset.primary_ticker or "").strip().upper() or None
        days = daily_signal_summary(readings, preferred=primary)
        if cutoff:
            first = cutoff.strftime("%Y-%m-%d")
            days = [d for d in days if d["date"] >= first]
        return {"days": days}

    @app.get("/api/thoughts", dependencies=[Depends(verify_auth)])
    async def get_thoughts(session_id: str | None = None) -> dict[str, Any]:
        """Fetch agent thoughts for the current cycle or a specific past session."""
        thoughts = []

        # If a specific session ID is requested OR if the in-memory thoughts list is empty
        if session_id or not getattr(app.state, "thoughts", []):
            logger_db = app.state.thought_logger
            try:
                target_session = session_id
                if not target_session:
                    # Find the most recent session_id in the logs
                    target_session = await logger_db.get_most_recent_session_id()

                if target_session:
                    db_thoughts = await logger_db.get_recent_thoughts(
                        limit=500, session_id=target_session
                    )
                    # Reverse so they appear chronologically in the frontend
                    db_thoughts.reverse()

                    for t in db_thoughts:
                        thought_type = t.get("event_type")
                        meta_dict = {}
                        if t.get("meta"):
                            try:
                                import json

                                meta_dict = json.loads(t.get("meta"))
                            except Exception:
                                pass

                        entry = {
                            "agent": t.get("agent_name"),
                            "type": thought_type,
                            "timestamp": t.get("timestamp"),
                        }
                        if thought_type == "thought":
                            entry["content"] = t.get("content")
                        elif thought_type == "tool_call":
                            entry["tool_name"] = t.get("content")
                            entry["args"] = meta_dict.get("args", {})
                            entry["tool_info"] = meta_dict.get("tool_info", {})
                        elif thought_type == "tool_response":
                            entry["tool_name"] = t.get("content")
                            entry["response"] = meta_dict.get("response", {})
                            entry["tool_info"] = meta_dict.get("tool_info", {})
                        elif thought_type == "cycle_complete":
                            entry["status"] = meta_dict.get("status")
                            entry["stages_completed"] = meta_dict.get("stages_completed", [])
                            entry["stages_skipped"] = meta_dict.get("stages_skipped", [])
                            entry["duration_ms"] = meta_dict.get("duration_ms", 0)
                            entry["error"] = meta_dict.get("error")
                        thoughts.append(entry)
            except Exception as e:
                logger.warning("Could not fetch thoughts from database: %s", e)
        else:
            thoughts = getattr(app.state, "thoughts", [])

        return {"thoughts": thoughts}

    @app.get("/api/metrics/llm", dependencies=[Depends(verify_auth)])
    async def get_llm_metrics(period: str = "all") -> dict[str, Any]:
        """Compute and return aggregated LLM metrics for the dashboard."""
        try:
            return app.state.telemetry_reader.get_llm_metrics_summary(period=period, limit=1000)
        except Exception as e:
            logger.error("Failed to aggregate llm metrics: %s", e)
            return {}

    @app.get("/api/telemetry/llm", dependencies=[Depends(verify_auth)])
    async def get_llm_logs(
        session_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch local logs of LLM requests and responses from the telemetry database.

        Filters:
        - session_id: Return logs belonging to a specific session run.
        - limit: Maximum number of records to return (defaults to 100).
        """
        try:
            logs = app.state.telemetry_reader.get_llm_calls(session_id, limit)
            return {"logs": logs}
        except Exception as e:
            logger.error("Failed to read telemetry database: %s", e)
            raise HTTPException(
                status_code=500, detail=f"Failed to read telemetry database: {e}"
            ) from e

    @app.get("/api/cycles", dependencies=[Depends(verify_auth)])
    async def get_cycles() -> dict[str, Any]:
        """Fetch unique session runs (cycles) from SQLite."""
        from evotrader.db.thought_log import ThoughtLogger

        logger_db = ThoughtLogger(app.state.db)
        try:
            cycles = await logger_db.get_unique_sessions(limit=50)
            return {"cycles": cycles}
        except Exception as e:
            logger.error("Could not fetch cycles: %s", e)
            raise HTTPException(
                status_code=500, detail=f"Database error fetching cycles: {e!s}"
            ) from e

    @app.get("/api/cycles/{session_id}", dependencies=[Depends(verify_auth)])
    async def get_cycle_detail(session_id: str) -> dict[str, Any]:
        """Fetch metadata for a single cycle session."""
        from evotrader.db.thought_log import ThoughtLogger

        logger_db = ThoughtLogger(app.state.db)
        try:
            cycles = await logger_db.get_unique_sessions(limit=200)
            match = next((c for c in cycles if c["session_id"] == session_id), None)
            if not match:
                raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
            return match
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Could not fetch cycle detail: %s", e)
            raise HTTPException(status_code=500, detail=f"Database error: {e!s}") from e

    @app.get("/api/trades", dependencies=[Depends(verify_auth)])
    async def get_trades(limit: int = 50, period: str = "all") -> dict[str, Any]:
        """Fetch historical journal entries, optionally filtered by period."""
        trades = await journal.get_recent_trades(limit=limit)

        # Apply period filtering client-side (the DB returns all recent trades)
        if period and period != "all":
            from evotrader.utils import get_period_cutoff_utc_str

            cutoff_str = get_period_cutoff_utc_str(period)

            if cutoff_str:
                trades = [t for t in trades if (t.get("timestamp") or "") >= cutoff_str]

        return {"trades": trades}

    @app.get("/api/evolution", dependencies=[Depends(verify_auth)])
    async def get_evolution() -> dict[str, Any]:
        """Fetch evolution log details, merged with file-based pending proposals."""
        try:
            rows = await app.state.evolution_store.get_recent(limit=50)
            evolution_list = list(rows)

            # Collect new_version values already in the DB so we don't duplicate
            known_versions = {r["new_version"] for r in evolution_list if r.get("new_version")}

            # ── Merge file-based code reviews that are PENDING_REVIEW ──
            reviews_dir = config.data_dir / "evolution" / "reviews"
            if reviews_dir.is_dir():
                for p in reviews_dir.glob("*.md"):
                    if p.stem in known_versions:
                        continue
                    try:
                        content = p.read_text()
                        status = "PENDING_REVIEW"
                        generated = ""
                        for line in content.splitlines():
                            if line.startswith("**Status**:"):
                                status = line.replace("**Status**:", "").strip()
                            elif line.startswith("**Generated**:"):
                                generated = line.replace("**Generated**:", "").strip()
                        if status == "PENDING_REVIEW":
                            evolution_list.append(
                                {
                                    "change_type": "CODE_REVIEW",
                                    "target_component": "infrastructure",
                                    "old_version": "",
                                    "new_version": p.stem,
                                    "status": "PENDING_REVIEW",
                                    "reasoning": "",
                                    "timestamp": generated,
                                }
                            )
                    except Exception:
                        pass

            # ── Merge file-based strategy proposals that are still proposed ──
            proposals_dir = config.data_dir / "evolution" / "proposals"
            if proposals_dir.is_dir():
                for p in proposals_dir.glob("*.md"):
                    if p.stem in known_versions:
                        continue
                    try:
                        content = p.read_text()
                        fm = _parse_yaml_frontmatter(content)
                        raw_status = str(fm.get("status", "proposed")).upper()
                        if raw_status in ("PROPOSED", "IN_PROGRESS"):
                            target = fm.get("target_strategy", p.stem)
                            generated = str(fm.get("created_at", ""))
                            evolution_list.append(
                                {
                                    "change_type": "ALGORITHM_NEW",
                                    "target_component": target,
                                    "old_version": "none",
                                    "new_version": p.stem,
                                    "status": "PENDING_REVIEW",
                                    "reasoning": "",
                                    "timestamp": generated,
                                }
                            )
                    except Exception:
                        pass

            # Re-sort by timestamp descending
            evolution_list.sort(
                key=lambda x: x.get("timestamp", "") or "",
                reverse=True,
            )
            return {"evolution": evolution_list}
        except Exception as e:
            logger.error("Failed to fetch evolution log: %s", e)
            return {"evolution": []}

    @app.post("/api/oauth/refresh", dependencies=[Depends(verify_auth)])
    async def refresh_oauth(background_tasks: BackgroundTasks) -> dict[str, str]:
        """Clear cached OAuth tokens and force a fresh re-authentication flow."""
        from evotrader.mcp.oauth import clear_oauth_cache

        clear_oauth_cache()

        # Close the existing in-memory MCP session so the next request
        # creates a fresh one that discovers the tokens are gone and
        # triggers the full OAuth redirect flow (SSE → modal).
        async def reconnect_mcp():
            if app.state.mcp_toolset:
                try:
                    mgr = app.state.mcp_toolset._mcp_session_manager
                    await mgr.close()
                    logger.info("🔌  Closed existing MCP session. Reconnecting...")
                    session = await mgr.create_session()
                    await session.call_tool("get_accounts", arguments={})
                except Exception as e:
                    # Expected — the OAuth redirect_handler fires before
                    # call_tool can complete, which is exactly what we want.
                    logger.debug("MCP reconnect triggered OAuth flow: %s", e)

        background_tasks.add_task(reconnect_mcp)
        return {"status": "ok", "message": "Token cleared. OAuth prompt will appear shortly."}

    @app.post("/api/trigger", dependencies=[Depends(verify_auth)])
    async def trigger_cycle(background_tasks: BackgroundTasks) -> dict[str, str]:
        """Spawn a background task to execute a trading cycle on demand."""
        if app.state.is_running or (
            app.state.evolution_service and app.state.evolution_service.is_running()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A cycle is already running or the system is self-evolving.",
            )

        background_tasks.add_task(_execute_cycle, "user-triggered")
        return {"status": "triggered", "message": "Trading cycle initiated."}

    @app.post("/api/cycles/cancel", dependencies=[Depends(verify_auth)])
    async def cancel_active_cycle() -> dict[str, str]:
        """Cancel the currently running trading cycle task."""
        active_task = getattr(app.state, "active_cycle_task", None)
        if not active_task:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active trading cycle is currently running.",
            )
        active_task.cancel()
        logger.info("🛑 User-triggered cancellation of the active trading cycle task.")
        return {"status": "cancelled", "message": "Trading cycle task cancellation initiated."}

    @app.post("/api/evolution/trigger", dependencies=[Depends(verify_auth)])
    async def trigger_evolution(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, str]:
        """Spawn a background task to execute a self-evolution run on demand.

        Accepts an optional JSON body with a ``user_comment`` field containing
        questions or comments from the operator for the evolution agent.
        """
        if app.state.is_running or (
            app.state.evolution_service and app.state.evolution_service.is_running()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A cycle is already running or the system is self-evolving.",
            )

        # Extract optional user comment from JSON body
        user_comment = ""
        try:
            body_bytes = await request.body()
            if body_bytes:
                body = await request.json()
                user_comment = (
                    (body.get("user_comment") or "").strip() if isinstance(body, dict) else ""
                )
                logger.info(
                    "📝  Evolution trigger received body: %s (comment length: %d)",
                    body,
                    len(user_comment),
                )
            else:
                logger.info("📝  Evolution trigger received no body.")
        except Exception as e:
            logger.warning("⚠️  Failed to parse evolution trigger body: %s", e, exc_info=True)

        if user_comment:
            logger.info("📝  Operator comment for evolution: %s", user_comment[:200])

        background_tasks.add_task(_execute_evolution, "user-triggered", user_comment)
        msg = "Self-evolution run initiated."
        if user_comment:
            msg += f" Operator comment received ({len(user_comment)} chars)."
        return {"status": "triggered", "message": msg}

    @app.post("/api/evolution/cancel", dependencies=[Depends(verify_auth)])
    async def cancel_active_evolution() -> dict[str, str]:
        """Cancel the currently running evolution cycle task."""
        active_task = getattr(app.state, "active_evolution_task", None)
        if not active_task:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active evolution cycle is currently running.",
            )
        active_task.cancel()
        logger.info("🛑 User-triggered cancellation of the active evolution cycle task.")
        return {"status": "cancelled", "message": "Evolution task cancellation initiated."}

    @app.post("/api/evolution/continue/{session_id}", dependencies=[Depends(verify_auth)])
    async def continue_evolution(
        session_id: str, background_tasks: BackgroundTasks
    ) -> dict[str, str]:
        """Resume a previously timed-out or failed evolution cycle.

        Reconstructs the conversation from DB logs and sends a "continue"
        prompt so the agent can pick up where it left off.
        """
        if app.state.is_running or (
            app.state.evolution_service and app.state.evolution_service.is_running()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A cycle is already running or the system is self-evolving.",
            )

        if not app.state.evolution_service:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Evolution service not initialised.",
            )

        # Verify the session exists and is in a resumable state
        thought_logger = app.state.evolution_service.thought_logger
        events = await thought_logger.get_session_events(session_id)
        if not events:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No events found for session {session_id}.",
            )

        async def _continue_wrapper() -> None:
            timeout = config.settings.schedule.max_evolution_duration_seconds
            logger.info(
                "🔄  Continuing evolution session %s... (timeout: %ds)",
                session_id,
                timeout,
            )
            app.state.active_evolution_task = asyncio.current_task()
            try:
                await asyncio.wait_for(
                    app.state.evolution_service.continue_session(session_id),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                logger.info("🛑  Continued evolution session was cancelled.")
            except TimeoutError:
                logger.error(
                    "⏰  Continued evolution timed out after %ds.",
                    timeout,
                )
                app.state.evolution_service.mark_timeout()
            except Exception as e:
                logger.error("Error during continued evolution: %s", e, exc_info=True)
            finally:
                app.state.active_evolution_task = None

        background_tasks.add_task(_continue_wrapper)
        return {
            "status": "continuing",
            "message": f"Resuming evolution session {session_id} ({len(events)} events to restore).",
        }

    @app.get("/api/cron/status", dependencies=[Depends(verify_auth)])
    async def get_cron_status() -> dict[str, Any]:
        """Get the status of the cron scheduler, next run times, and countdowns."""
        import datetime

        from evotrader.cron import CronTrigger

        schedule_config = app.state.config.settings.schedule

        cycle_cron = schedule_config.cycle_cron
        evolution_cron = schedule_config.evolution_cron
        metrics_cron = schedule_config.metrics_cron

        now_utc = datetime.datetime.now(datetime.UTC)
        next_cycle_dt = None
        next_evolution_dt = None
        seconds_until_next_cycle = None
        seconds_until_next_evolution = None

        if cycle_cron:
            try:
                # The soonest of every scheduled pattern — the dashboard should
                # count down to the NEXT cycle, not to the next one of whichever
                # expression happens to be listed first.
                from evotrader.cron import iter_cron_expressions

                candidates = [
                    dt
                    for dt in (
                        CronTrigger(expr).next_run(now_utc)
                        for expr in iter_cron_expressions(cycle_cron)
                    )
                    if dt is not None
                ]
                next_cycle_dt = min(candidates) if candidates else None
                if next_cycle_dt:
                    seconds_until_next_cycle = int((next_cycle_dt - now_utc).total_seconds())
            except Exception as e:
                logger.error("Failed to calculate next cycle run: %s", e)

        if evolution_cron:
            try:
                trigger = CronTrigger(evolution_cron)
                next_evolution_dt = trigger.next_run(now_utc)
                if next_evolution_dt:
                    seconds_until_next_evolution = int(
                        (next_evolution_dt - now_utc).total_seconds()
                    )
            except Exception as e:
                logger.error("Failed to calculate next evolution run: %s", e)

        return {
            "cycle_cron_enabled": getattr(app.state, "cycle_cron_enabled", False),
            "evolution_cron_enabled": getattr(app.state, "evolution_cron_enabled", False),
            "cron_expression": cycle_cron,
            "evolution_cron_expression": evolution_cron,
            "metrics_cron_expression": metrics_cron,
            "next_cycle_run": next_cycle_dt.isoformat() if next_cycle_dt else None,
            "next_evolution_run": next_evolution_dt.isoformat() if next_evolution_dt else None,
            "seconds_until_next_cycle": seconds_until_next_cycle,
            "seconds_until_next_evolution": seconds_until_next_evolution,
        }

    @app.post("/api/cron/toggle", dependencies=[Depends(verify_auth)])
    async def toggle_cron(payload: dict[str, Any]) -> dict[str, Any]:
        """Enable or disable an automated cron scheduler."""
        cron_type = payload.get("type")
        enabled = bool(payload.get("enabled", False))

        if cron_type == "cycle":
            app.state.cycle_cron_enabled = enabled
            msg = f"Cycle scheduler {'enabled' if enabled else 'disabled'}."
        elif cron_type == "evolution":
            app.state.evolution_cron_enabled = enabled
            msg = f"Evolution scheduler {'enabled' if enabled else 'disabled'}."
        else:
            raise HTTPException(
                status_code=400, detail="Invalid toggle type. Must be 'cycle' or 'evolution'."
            )

        broadcast_sse_event(
            "cron_toggle",
            {
                "cycle_cron_enabled": getattr(app.state, "cycle_cron_enabled", False),
                "evolution_cron_enabled": getattr(app.state, "evolution_cron_enabled", False),
            },
        )
        logger.info("⏰  %s status changed to: %s", msg, "ENABLED" if enabled else "DISABLED")
        return {
            "status": "success",
            "message": msg,
            "cycle_cron_enabled": getattr(app.state, "cycle_cron_enabled", False),
            "evolution_cron_enabled": getattr(app.state, "evolution_cron_enabled", False),
        }

    @app.get("/api/evolution/status", dependencies=[Depends(verify_auth)])
    async def get_evolution_status() -> dict[str, Any]:
        """Get the status of the evolution service."""
        if not app.state.evolution_service:
            return {"running": False, "last_run": None}
        return {
            "running": app.state.evolution_service.is_running(),
            "last_run": app.state.evolution_service.get_last_run(),
        }

    @app.get("/api/evolution/runs", dependencies=[Depends(verify_auth)])
    async def get_evolution_runs() -> list[dict[str, Any]]:
        """Fetch all recorded self-evolution runs."""
        try:
            from evotrader.db.thought_log import ThoughtLogger

            thought_logger = ThoughtLogger(db)
            runs = await thought_logger.get_evolution_runs()
            return [{**run, "llm_call_count": run.get("llm_call_count", 0)} for run in runs]
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch evolution runs: {e}"
            ) from e

    @app.get("/api/metrics/portfolio", dependencies=[Depends(verify_auth)])
    async def get_portfolio_metrics(period: str = "all") -> dict[str, Any]:
        """Fetch historical portfolio value metrics for equity curve tracking.

        Uses a **single consistent valuation source** for all days to
        avoid discontinuities in the chart.  Priority order for each
        calendar day:

        1. **Today**: live broker value from ``cached_portfolio`` (set by
           the ``/api/portfolio`` endpoint).
        2. **Historical day with a daily_metrics snapshot**: the broker
           ``portfolio_value`` stored by the scheduled metrics cron.
        3. **Gap day** (weekends, holidays, missing snapshots): carry
           forward the last known broker value.
        4. **Bootstrap fallback** (before first metric snapshot):
           ``cumulative_deposits + cumulative_realized_pnl``.

        This ensures every point on the equity curve comes from the same
        source (actual broker account value), eliminating the visible
        drop that occurred when reconstruction (deposits + realized P&L)
        was mixed with the live broker total.
        """
        try:
            import datetime

            # 1. Build daily realized P&L map from trade journal
            #    (used for cumulative P&L display AND bootstrap fallback)
            rows = await app.state.journal.get_realized_pnl_history()
            daily_trade_pnl: dict[str, float] = {}
            for row in rows:
                try:
                    ts_str = row["timestamp"]
                    date_str = utc_timestamp_to_et_date(ts_str)
                except Exception:
                    continue
                pnl = float(row["realized_pnl"] or 0.0)
                daily_trade_pnl[date_str] = daily_trade_pnl.get(date_str, 0.0) + pnl

            # 2. Fetch cash adjustments and build a daily deposit map
            adjustments_list = await app.state.metrics.get_cash_adjustments()
            daily_deposits: dict[str, float] = {}
            for a in adjustments_list:
                d = a["date"]
                daily_deposits[d] = daily_deposits.get(d, 0.0) + a["amount"]

            # 3. Determine date range
            first_trade_ts = await app.state.journal.get_first_trade_timestamp()
            first_trade_date = utc_timestamp_to_et_date(first_trade_ts) if first_trade_ts else None
            first_adj_date = min(a["date"] for a in adjustments_list) if adjustments_list else None

            candidates = [d for d in [first_trade_date, first_adj_date] if d]
            if not candidates:
                return {
                    "history": [],
                    "source": "empty",
                    "adjustments": adjustments_list,
                    "starting_value": 0.0,
                }

            start_date_str = min(candidates)
            start_dt = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=ET)
            end_dt = datetime.datetime.now(ET)
            today_str = end_dt.strftime("%Y-%m-%d")

            # 4. Fetch daily_metrics broker snapshots for the full range
            all_metrics = await app.state.metrics.get_metrics_range(
                start_date_str,
                today_str,
            )
            snapshot_map: dict[str, float] = {}
            for m in all_metrics:
                if m.portfolio_value and m.portfolio_value > 0:
                    snapshot_map[m.date] = m.portfolio_value

            # 5. Today's live broker value (most current)
            cached_data = getattr(app.state, "cached_portfolio", None)
            if cached_data and cached_data.get("total_value"):
                snapshot_map[today_str] = cached_data["total_value"]

            # 6. Walk day-by-day, preferring broker snapshots over
            #    reconstruction. Gap days carry forward the last known
            #    broker value to avoid discontinuities.
            history = []
            cumulative_pnl = 0.0
            cumulative_deposits = 0.0
            last_known_broker_value: float | None = None

            current_dt = start_dt
            while current_dt <= end_dt:
                d_str = current_dt.strftime("%Y-%m-%d")
                pnl_for_day = daily_trade_pnl.get(d_str, 0.0)
                deposits_for_day = daily_deposits.get(d_str, 0.0)
                cumulative_pnl += pnl_for_day
                cumulative_deposits += deposits_for_day

                # Reconstruction value (bootstrap fallback)
                reconstructed = cumulative_deposits + cumulative_pnl

                if d_str in snapshot_map:
                    # Broker snapshot available — use it
                    portfolio_value = snapshot_map[d_str]
                    last_known_broker_value = portfolio_value
                elif last_known_broker_value is not None:
                    # Gap day (weekend/holiday) — carry forward last
                    # known broker value, adjusted for any deposits or
                    # realized P&L that landed on this gap day.
                    portfolio_value = last_known_broker_value + deposits_for_day + pnl_for_day
                    last_known_broker_value = portfolio_value
                else:
                    # No broker snapshot yet — bootstrap with
                    # reconstruction (earliest days only).
                    portfolio_value = reconstructed
                    # Don't set last_known_broker_value here; wait for
                    # the first real broker snapshot.

                history.append(
                    {
                        "date": d_str,
                        "portfolio_value": portfolio_value,
                        "daily_pnl": pnl_for_day,
                        "cumulative_pnl": cumulative_pnl,
                        "cumulative_deposits": cumulative_deposits,
                    }
                )
                current_dt += datetime.timedelta(days=1)

            # 7. Compute total capital base (all deposits, regardless of period)
            total_capital_base = sum(a["amount"] for a in adjustments_list)

            # 8. Apply period filter
            if period and period != "all":
                from evotrader.utils import get_period_cutoff_dt

                cutoff = get_period_cutoff_dt(period, ET)
                if cutoff:
                    cutoff_str = cutoff.strftime("%Y-%m-%d")
                    pre_cutoff = [h for h in history if h["date"] < cutoff_str]
                    starting_value = pre_cutoff[-1]["portfolio_value"] if pre_cutoff else 0.0
                    history = [h for h in history if h["date"] >= cutoff_str]
                    adjustments_list = [a for a in adjustments_list if a["date"] >= cutoff_str]
                else:
                    starting_value = 0.0
            else:
                starting_value = 0.0

            return {
                "history": history,
                "source": "broker_snapshots",
                "adjustments": adjustments_list,
                "starting_value": starting_value,
                "total_capital_base": total_capital_base,
            }

        except Exception as e:
            logger.error("Failed to fetch/merge portfolio metrics: %s", e, exc_info=True)
            return {
                "history": [],
                "source": "error",
                "adjustments": [],
                "starting_value": 0.0,
                "total_capital_base": 0.0,
            }

    # ── Cash Adjustment Endpoints (deposit / withdrawal tracking) ─────

    @app.get("/api/adjustments", dependencies=[Depends(verify_auth)])
    async def get_cash_adjustments() -> dict[str, Any]:
        """List all cash adjustments (deposits and withdrawals)."""
        try:
            adjustments = await app.state.metrics.get_cash_adjustments()
            return {"adjustments": adjustments}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch adjustments: {e}") from e

    @app.post("/api/adjustments", dependencies=[Depends(verify_auth)])
    async def create_cash_adjustment(body: dict[str, Any]) -> dict[str, Any]:
        """Log a cash deposit or withdrawal.

        Body: {"date": "YYYY-MM-DD", "amount": 500.0, "note": "optional"}
        Positive amount = deposit, negative = withdrawal.
        """
        date_str = body.get("date")
        amount = body.get("amount")
        note = body.get("note", "")

        if not date_str or amount is None:
            raise HTTPException(status_code=400, detail="'date' and 'amount' are required.")
        try:
            amount = float(amount)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="'amount' must be a number.") from None
        if amount == 0:
            raise HTTPException(status_code=400, detail="'amount' must be non-zero.")

        try:
            row_id = await app.state.metrics.save_cash_adjustment(
                date=date_str,
                amount=amount,
                note=str(note),
            )
            return {"id": row_id, "status": "created"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save adjustment: {e}") from e

    @app.delete("/api/adjustments/{adjustment_id}", dependencies=[Depends(verify_auth)])
    async def delete_cash_adjustment(adjustment_id: int) -> dict[str, str]:
        """Delete a cash adjustment by ID."""
        try:
            deleted = await app.state.metrics.delete_cash_adjustment(adjustment_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="Adjustment not found.")
            return {"status": "deleted"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete adjustment: {e}") from e

    @app.delete("/api/cycles/{session_id}", dependencies=[Depends(verify_auth)])
    async def delete_cycle_endpoint(session_id: str) -> dict[str, str]:
        """Delete a trading or evolution cycle and revert all associated side effects."""
        try:
            from evotrader.db.thought_log import ThoughtLogger

            thought_logger = ThoughtLogger(db)

            # 1. Read all thought log events for this session using ThoughtLogger helper
            rows = await thought_logger.get_recent_thoughts(limit=1000, session_id=session_id)

            # 2. Iterate through events to find and revert side-effects
            for row in rows:
                content = row.get("content")  # tool name
                meta_str = row.get("meta")
                if not meta_str:
                    continue
                try:
                    meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                except Exception:
                    continue

                if not isinstance(meta, dict):
                    continue

                response = meta.get("response")
                if not response or not isinstance(response, dict):
                    continue

                # A. Revert store_learning (ChromaDB semantic note)
                if content == "store_learning":
                    note_id = response.get("note_id")
                    if note_id:
                        try:
                            memory.delete_note(note_id)
                            logger.info("Deleted semantic memory note %s", note_id)
                        except Exception as mem_err:
                            logger.warning(
                                "Failed to delete semantic memory note %s: %s", note_id, mem_err
                            )

                # B. Revert record_trade (trades table entry)
                elif content == "record_trade":
                    trade_id = response.get("trade_id")
                    if trade_id:
                        try:
                            await journal.delete_trade(int(trade_id))
                        except Exception as db_err:
                            logger.warning(
                                "Failed to delete trade %s from DB: %s", trade_id, db_err
                            )

                # C. Revert propose_instruction_change (proposed instruction markdown and metadata yaml)
                elif content == "propose_instruction_change":
                    agent_name = response.get("agent")
                    new_version = response.get("new_version")
                    prev_version = response.get("previous_version")
                    file_path_str = response.get("file_path")
                    if file_path_str:
                        path = Path(file_path_str)
                        if path.is_file():
                            path.unlink()
                        # Also delete metadata yaml
                        meta_path = path.parent / f"{path.stem}_metadata.yaml"
                        if meta_path.is_file():
                            meta_path.unlink()

                    # Revert active pointer if active.txt currently points to this version
                    if agent_name and new_version and prev_version:
                        active_ptr = config.instructions_dir / agent_name / "active.txt"
                        if active_ptr.is_file():
                            current_active = active_ptr.read_text().strip()
                            if current_active == new_version:
                                active_ptr.write_text(prev_version)
                                logger.info(
                                    "Reverted active pointer for agent %s to %s",
                                    agent_name,
                                    prev_version,
                                )

                    # Delete from evolution_log matching the new version
                    if new_version:
                        await app.state.evolution_store.delete_by_version(new_version)

                # E. Revert submit_code_review (delete code review file and evolution log entry)
                elif content == "submit_code_review":
                    review_id = response.get("review_id")
                    file_path_str = response.get("file_path")
                    if file_path_str:
                        path = Path(file_path_str)
                        if path.is_file():
                            path.unlink()
                            logger.info("Deleted code review file: %s", path)

                    if review_id:
                        await app.state.evolution_store.delete_by_version(review_id)

                # D. Revert save_evolved_strategy (evolved strategy file, params yaml, metadata yaml)
                elif content == "save_evolved_strategy":
                    version_dir_str = response.get("version_dir")
                    if version_dir_str:
                        version_dir = Path(version_dir_str)
                        version_name = version_dir.name

                        # Revert active algorithm version if this was active
                        from evotrader.algorithms.registry import AlgorithmRegistry

                        registry = AlgorithmRegistry(
                            config.algorithms_dir,
                            is_sim=(config.settings.mode.value == "sim"),
                        )
                        try:
                            if registry.get_active_version() == version_name:
                                versions = registry.list_versions()
                                prev_algo = "v001_initial"
                                for v in reversed(versions):
                                    v_ver = v.get("version")
                                    if v_ver and v_ver != version_name:
                                        prev_algo = v_ver
                                        break
                                registry.set_active_version(prev_algo)
                                logger.info("Reverted active algorithm to %s", prev_algo)
                        except Exception as reg_err:
                            logger.warning("Failed to revert active algorithm version: %s", reg_err)

                        # Delete the version directory recursively
                        if version_dir.is_dir():
                            import shutil

                            shutil.rmtree(version_dir)
                            logger.info("Deleted algorithm version directory: %s", version_dir)

                        # Remove version from registry.yaml
                        registry_path = config.algorithms_dir / "registry.yaml"
                        if registry_path.is_file():
                            try:
                                import yaml

                                with open(registry_path) as f:
                                    reg_data = yaml.safe_load(f) or {}
                                if "versions" in reg_data:
                                    reg_data["versions"] = [
                                        v
                                        for v in reg_data["versions"]
                                        if v.get("version") != version_name
                                    ]
                                    with open(registry_path, "w") as f:
                                        yaml.dump(
                                            reg_data, f, default_flow_style=False, sort_keys=False
                                        )
                            except Exception as reg_err:
                                logger.warning("Failed to update registry.yaml: %s", reg_err)

                        # Delete from evolution_log matching the new version
                        await app.state.evolution_store.delete_by_version(version_name)

            # 3. Clean up DB tables for the session using ThoughtLogger helper
            await thought_logger.delete_cycle_logs(session_id)

            logger.info("Successfully deleted and reverted cycle session: %s", session_id)
            return {
                "status": "success",
                "message": f"Cycle {session_id} successfully deleted and reverted.",
            }
        except Exception as e:
            logger.error("Failed to delete and revert cycle %s: %s", session_id, e, exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"Failed to delete and revert cycle: {e}"
            ) from e

    @app.post("/api/reconcile", dependencies=[Depends(verify_auth)])
    async def trigger_reconciliation() -> dict[str, Any]:
        """Manually trigger position synchronization and pending order reconciliation."""
        # Invalidate cache to force next reload to call broker
        app.state.cached_portfolio = None
        app.state.cached_portfolio_time = 0.0

        from evotrader.models.config import TradingMode

        is_dry_run = (
            config.settings.dry_run.enabled if config.settings.mode != TradingMode.SIM else False
        )
        recon = ReconciliationService(journal, mcp_toolset, is_dry_run)
        ticker = config.settings.asset.primary_ticker
        result = await recon.reconcile_positions(ticker=ticker)
        _keep_sync_result(result)

        # Also reconcile pending orders against Robinhood's actual order state
        from evotrader.agents.tools import reconcile_pending_orders

        try:
            pending_result = await reconcile_pending_orders()
            result["pending_orders"] = pending_result
        except Exception as e:
            logger.warning("Pending order reconciliation failed: %s", e)
            result["pending_orders"] = {"error": str(e)}

        # Save snapshot for Equity Curve tracking
        import datetime

        from evotrader.models.portfolio import DailyMetrics

        today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        existing_list = await app.state.metrics.get_metrics_range(today, today)
        current_metric = (
            existing_list[0]
            if existing_list
            else DailyMetrics(
                date=today,
                portfolio_value=0.0,
                cash_balance=0.0,
                daily_pnl=0.0,
                daily_return_pct=0.0,
                cumulative_return=0.0,
            )
        )

        # Force a fresh portfolio fetch
        port_data = await get_portfolio()
        current_metric.portfolio_value = port_data["broker"]["total_value"]
        current_metric.cash_balance = port_data["broker"]["cash"]

        await app.state.metrics.save_daily_metrics(current_metric)

        broadcast_sse_event("reconciled", result)
        return result

    @app.post("/api/trades/approve/{order_id}", dependencies=[Depends(verify_auth)])
    async def approve_order(order_id: int) -> dict[str, str]:
        """Approve a pending trade proposal from the queue."""
        if not app.state.pending_order or app.state.pending_order["id"] != order_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Pending order not found or expired."
            )

        # Invalidate cache
        app.state.cached_portfolio = None
        app.state.cached_portfolio_time = 0.0

        future = app.state.pending_order["future"]
        app.state.pending_order = None
        app.state.status = "running"
        broadcast_sse_event("status", app.state.status)

        # Resolve the future to ALLOW the execution agent to proceed
        future.set_result(None)
        logger.info("✅  User APPROVED order #%d. Resuming execution.", order_id)
        return {"status": "approved", "message": "Order approved."}

    @app.post("/api/trades/reject/{order_id}", dependencies=[Depends(verify_auth)])
    async def reject_order(order_id: int) -> dict[str, str]:
        """Reject a pending trade proposal from the queue."""
        if not app.state.pending_order or app.state.pending_order["id"] != order_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Pending order not found or expired."
            )

        # Invalidate cache
        app.state.cached_portfolio = None
        app.state.cached_portfolio_time = 0.0

        future = app.state.pending_order["future"]
        app.state.pending_order = None
        app.state.status = "running"
        broadcast_sse_event("status", app.state.status)

        # Resolve the future to BLOCK execution
        future.set_result(
            {
                "allowed": False,
                "action": "USER_REJECTED",
                "violations": ["Rejected by user manually via dashboard UI."],
            }
        )
        logger.info("❌  User REJECTED order #%d. Execution aborted.", order_id)
        return {"status": "rejected", "message": "Order rejected."}

    # ── Memory & Notes Endpoints ───────────────────────────────

    @app.get("/api/memory/stats", dependencies=[Depends(verify_auth)])
    async def get_memory_stats_api() -> dict[str, Any]:
        """Get sizes of ChromaDB collections."""
        return app.state.memory.stats()

    @app.get("/api/memory/notes", dependencies=[Depends(verify_auth)])
    async def get_memory_notes() -> dict[str, Any]:
        """Get all stored notes in ChromaDB."""
        return {"notes": app.state.memory.get_all_notes()}

    @app.delete("/api/memory/notes/{note_id}", dependencies=[Depends(verify_auth)])
    async def delete_memory_note(note_id: str) -> dict[str, str]:
        """Delete a specific note by ID."""
        app.state.memory.delete_note(note_id)
        return {"status": "success", "message": f"Deleted note {note_id}"}

    @app.get("/api/memory/trades", dependencies=[Depends(verify_auth)])
    async def get_memory_trades() -> dict[str, Any]:
        """Get all stored trade experiences in ChromaDB."""
        return {"trades": app.state.memory.get_all_trade_experiences()}

    @app.post("/api/memory/prune", dependencies=[Depends(verify_auth)])
    async def prune_memory() -> dict[str, Any]:
        """Run memory pruning for stale entries."""
        result = app.state.memory.prune()
        return {"status": "success", "message": "Memory pruning complete.", "pruned": result}

    @app.post("/api/memory/clear", dependencies=[Depends(verify_auth)])
    async def clear_memory() -> dict[str, str]:
        """Wipe ChromaDB collections and re-embed notes from disk."""
        app.state.memory.clear_all_memories()
        notes_dir = config.notes_dir
        app.state.memory.load_notes_from_directory(notes_dir)
        return {"status": "success", "message": "ChromaDB memory cleared; user notes re-embedded."}

    @app.get("/api/notes/files", dependencies=[Depends(verify_auth)])
    async def list_note_files() -> list[dict[str, Any]]:
        """List all markdown and text files in notes/ and evolution/notes/."""
        from evotrader.tools.memory import _guess_category, _parse_frontmatter

        # Collect from both user notes and evolution notes directories
        source_dirs = [
            ("user", config.notes_dir),
            ("evolution", config.data_dir / "evolution" / "notes"),
            # The trading agent's cycle-to-cycle handoff. A third source rather
            # than a file in notes/, because notes/ is ingested into semantic
            # memory at startup — a handoff there would pollute every search and
            # never expire.
            ("trading", config.data_dir / "trading" / "notes"),
        ]

        files_data = []
        for source, notes_dir in source_dirs:
            if not notes_dir.is_dir():
                continue
            for file_path in sorted(list(notes_dir.glob("*.md")) + list(notes_dir.glob("*.txt"))):
                try:
                    text = file_path.read_text().strip()
                    meta = _parse_frontmatter(text)
                    if source == "trading":
                        category = meta.get("category", "trading_handoff")
                    elif source == "evolution":
                        category = meta.get(
                            "category",
                            "carry_forward" if file_path.stem == "carry_forward" else "backlog",
                        )
                    else:
                        category = (
                            meta.get("category", _guess_category(file_path.stem)) or "general"
                        )
                    priority = meta.get("priority", "normal")

                    # Extract content body snippet
                    body = meta.get("__body__", text)
                    snippet = body[:150] + "..." if len(body) > 150 else body

                    files_data.append(
                        {
                            "filename": file_path.name,
                            "title": file_path.stem,
                            "category": category,
                            "priority": priority,
                            "size": file_path.stat().st_size,
                            "snippet": snippet,
                            "mtime": file_path.stat().st_mtime,
                            "source": source,
                        }
                    )
                except Exception as e:
                    logger.warning("Failed to parse note file '%s': %s", file_path.name, e)
        return files_data

    def _resolve_notes_dir(source: str | None) -> Path:
        """Resolve the notes directory for a given source."""
        if source == "evolution":
            d = config.data_dir / "evolution" / "notes"
            d.mkdir(parents=True, exist_ok=True)
            return d
        if source == "trading":
            d = config.data_dir / "trading" / "notes"
            d.mkdir(parents=True, exist_ok=True)
            return d
        return config.notes_dir

    @app.get("/api/notes/files/{filename}", dependencies=[Depends(verify_auth)])
    async def get_note_file(filename: str, source: str | None = None) -> dict[str, Any]:
        """Get parsed contents of a specific note file."""
        notes_dir = _resolve_notes_dir(source)
        file_path = notes_dir / filename
        if not file_path.resolve().is_relative_to(notes_dir.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        try:
            text = file_path.read_text()
            from evotrader.tools.memory import _guess_category, _parse_frontmatter

            meta = _parse_frontmatter(text)
            body = meta.get("__body__", text)
            if source == "evolution":
                category = meta.get(
                    "category", "carry_forward" if file_path.stem == "carry_forward" else "backlog"
                )
            else:
                category = meta.get("category", _guess_category(file_path.stem)) or "general"
            priority = meta.get("priority", "normal")

            return {
                "filename": filename,
                "title": file_path.stem,
                "category": category,
                "priority": priority,
                "content": body,
                "raw": text,
                "source": source or "user",
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read file: {e}") from e

    @app.post("/api/notes/files/{filename}", dependencies=[Depends(verify_auth)])
    async def save_note_file(filename: str, payload: dict[str, Any]) -> dict[str, str]:
        """Create or update a note file on disk and reload embeddings."""
        source = payload.get("source", "user")
        notes_dir = _resolve_notes_dir(source)
        notes_dir.mkdir(parents=True, exist_ok=True)
        file_path = notes_dir / filename
        if not file_path.resolve().is_relative_to(notes_dir.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        content = payload.get("content", "").strip()
        category = payload.get("category", "general").strip()
        priority = payload.get("priority", "normal").strip()

        if source == "evolution":
            # Evolution notes are freeform markdown — don't wrap in frontmatter
            # unless the content already has it or a category was explicitly set.
            full_text = content
        else:
            # Build YAML frontmatter format note
            frontmatter = f"---\ncategory: {category}\npriority: {priority}\n---\n"
            full_text = frontmatter + content

        try:
            file_path.write_text(full_text)
            # Re-sync ChromaDB embeddings (user notes only)
            if source != "evolution":
                app.state.memory.load_notes_from_directory(config.notes_dir)
            return {"status": "success", "message": "Note saved successfully."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save note: {e}") from e

    @app.delete("/api/notes/files/{filename}", dependencies=[Depends(verify_auth)])
    async def delete_note_file(filename: str, source: str | None = None) -> dict[str, str]:
        """Delete a note file from disk and refresh embeddings."""
        notes_dir = _resolve_notes_dir(source)
        file_path = notes_dir / filename
        if not file_path.resolve().is_relative_to(notes_dir.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        try:
            file_path.unlink()
            # Re-sync memory (user notes only — evolution notes are not embedded)
            if source != "evolution":
                app.state.memory.load_notes_from_directory(config.notes_dir)
            return {"status": "success", "message": "Note deleted successfully."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete note: {e}") from e

    # ── System Instructions Endpoints ────────────────────────────

    @app.get("/api/instructions", dependencies=[Depends(verify_auth)])
    async def list_instructions() -> dict[str, Any]:
        """List all agents, their active version, and all available versions."""
        instructions_dir = config.instructions_dir
        if not instructions_dir.is_dir():
            return {"agents": []}

        from evotrader.agents.instructions import get_active_version, list_agents

        agents_data = []
        is_sim = config.settings.mode.value == "sim"
        for agent_name in list_agents(instructions_dir, is_sim=is_sim):
            try:
                active_ver = get_active_version(agent_name, instructions_dir, is_sim=is_sim)
                agent_dir = instructions_dir / agent_name

                # Find all version md files in the directory
                versions = []
                for p in sorted(agent_dir.glob("*.md")):
                    versions.append(p.stem)  # e.g. "v001"

                # Check if there are any proposed versions
                has_proposed = False
                for p in agent_dir.glob("*_metadata.yaml"):
                    try:
                        import yaml

                        meta = yaml.safe_load(p.read_text()) or {}
                        if meta.get("status") == "proposed" and meta.get("version") != active_ver:
                            has_proposed = True
                            break
                    except Exception:
                        pass

                agents_data.append(
                    {
                        "name": agent_name,
                        "active_version": active_ver,
                        "versions": versions,
                        "has_proposed": has_proposed,
                    }
                )
            except Exception as e:
                logger.warning("Failed to load instructions meta for '%s': %s", agent_name, e)

        return {"agents": agents_data}

    @app.get("/api/instructions/{agent_name}/{version}", dependencies=[Depends(verify_auth)])
    async def get_instruction_version(agent_name: str, version: str) -> dict[str, Any]:
        """Get content of a specific version of instructions for an agent."""
        instructions_dir = config.instructions_dir
        agent_dir = instructions_dir / agent_name

        if not agent_dir.resolve().is_relative_to(instructions_dir.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        file_path = agent_dir / f"{version}.md"
        if not file_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Instruction version '{version}' not found for agent '{agent_name}'",
            )

        try:
            from evotrader.agents.instructions import get_active_version

            active_ver = get_active_version(
                agent_name,
                instructions_dir,
                is_sim=(config.settings.mode.value == "sim"),
            )
            content = file_path.read_text()

            # Load metadata if exists
            import yaml

            meta_path = agent_dir / f"{version}_metadata.yaml"
            metadata = None
            diff_text = ""
            if meta_path.is_file():
                try:
                    metadata = yaml.safe_load(meta_path.read_text()) or {}
                except Exception as me:
                    logger.warning("Failed to load metadata for %s %s: %s", agent_name, version, me)

            # Generate diff if there is a previous version
            if metadata and metadata.get("previous_version"):
                prev_version = metadata["previous_version"]
                prev_path = agent_dir / f"{prev_version}.md"
                if prev_path.is_file():
                    try:
                        import difflib

                        prev_content = prev_path.read_text()
                        diff_lines = list(
                            difflib.unified_diff(
                                prev_content.splitlines(keepends=True),
                                content.splitlines(keepends=True),
                                fromfile=f"{prev_version}.md",
                                tofile=f"{version}.md",
                            )
                        )
                        diff_text = "".join(diff_lines)
                    except Exception as de:
                        logger.warning(
                            "Failed to generate diff for %s %s: %s", agent_name, version, de
                        )

            return {
                "agent_name": agent_name,
                "version": version,
                "content": content,
                "is_active": (version == active_ver),
                "metadata": metadata,
                "diff": diff_text,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read instructions: {e}") from e

    @app.post(
        "/api/instructions/{agent_name}/{version}/activate", dependencies=[Depends(verify_auth)]
    )
    async def activate_instruction_version_endpoint(
        agent_name: str, version: str
    ) -> dict[str, Any]:
        """Activate a specific version of instructions for an agent."""
        instructions_dir = config.instructions_dir
        agent_dir = instructions_dir / agent_name
        if not agent_dir.resolve().is_relative_to(instructions_dir.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        file_path = agent_dir / f"{version}.md"
        if not file_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Instruction version '{version}' not found for agent '{agent_name}'",
            )

        try:
            from evotrader.evolution.instruction_evolver import InstructionEvolver

            evolver = InstructionEvolver(
                instructions_dir,
                is_sim=(config.settings.mode.value == "sim"),
            )
            res = evolver.activate_version(agent_name, version)

            if res.get("status") == "activated":
                # Also update the evolution log in DB to set status to 'ACTIVE'
                await app.state.evolution_store.update_status(version=version, status="ACTIVE")
                logger.info(
                    "Human manually activated instruction version '%s' for agent '%s'",
                    version,
                    agent_name,
                )
                return {
                    "status": "success",
                    "message": f"Instruction version '{version}' activated for agent '{agent_name}'.",
                }
            else:
                raise HTTPException(
                    status_code=400, detail=res.get("reason", "Failed to activate version")
                )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to activate instructions: {e}"
            ) from e

    @app.post(
        "/api/instructions/{agent_name}/{version}/reject", dependencies=[Depends(verify_auth)]
    )
    async def reject_instruction_version_endpoint(agent_name: str, version: str) -> dict[str, Any]:
        """Mark a specific version of instructions as rejected."""
        try:
            # Update the evolution log in DB to set status to 'REJECTED'
            updated = await app.state.evolution_store.update_status(
                version=version, status="REJECTED"
            )
            if not updated:
                raise HTTPException(
                    status_code=404,
                    detail=f"No evolution_log entry found for version '{version}' — nothing was rejected.",
                )
            logger.info(
                "Human manually rejected instruction version '%s' for agent '%s'",
                version,
                agent_name,
            )
            return {
                "status": "success",
                "message": f"Instruction version '{version}' rejected for agent '{agent_name}'.",
            }
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to reject instructions: {e}"
            ) from e

    # ── Algorithm Versions Endpoints ──────────────────────────────

    @app.get("/api/algorithms", dependencies=[Depends(verify_auth)])
    async def list_algorithms() -> dict[str, Any]:
        """List all available algorithm versions and the active version."""
        from evotrader.algorithms.registry import AlgorithmRegistry

        try:
            registry = AlgorithmRegistry(
                config.algorithms_dir,
                is_sim=(config.settings.mode.value == "sim"),
            )
            versions = registry.list_versions()
            active_ver = registry.get_active_version()

            # Enrich versions list with database created_at dates and metadata statuses
            try:
                for v in versions:
                    ver_name = v.get("version")
                    if ver_name:
                        meta = registry.load_metadata(ver_name)
                        if meta.get("status"):
                            v["status"] = meta["status"]
            except Exception:
                pass

            # A version's date lives in its metadata (save_version stamps it) or,
            # for versions saved before that, in the evolution log. This used to
            # call a method the store does not have, inside a bare `except: pass`,
            # so every proposal read "Unknown date" and nothing said why.
            try:
                for v in versions:
                    ver_name = v.get("version")
                    if ver_name and (not v.get("created_at") or v.get("created_at") == ""):
                        row = await app.state.evolution_store.get_by_version(ver_name)
                        when = _log_time_iso(row) if row else None
                        if when:
                            v["created_at"] = when
            except Exception as e:
                logger.warning("Could not read version dates from the evolution log: %s", e)

            return {"versions": versions, "active_version": active_ver}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to list algorithms: {e}") from e

    @app.get("/api/algorithms/{version}", dependencies=[Depends(verify_auth)])
    async def get_algorithm_version(version: str) -> dict[str, Any]:
        """Get parameter content, metadata, and diff of a specific algorithm version."""
        from evotrader.algorithms.registry import AlgorithmRegistry

        try:
            registry = AlgorithmRegistry(
                config.algorithms_dir,
                is_sim=(config.settings.mode.value == "sim"),
            )

            # Verify version directory exists
            version_dir = config.algorithms_dir / version
            if not version_dir.resolve().is_relative_to(config.algorithms_dir.resolve()):
                raise HTTPException(status_code=403, detail="Access denied")
            if not version_dir.is_dir():
                raise HTTPException(
                    status_code=404, detail=f"Algorithm version '{version}' not found"
                )

            config_path = version_dir / "config.yaml"
            if not config_path.is_file():
                raise HTTPException(
                    status_code=404, detail=f"Config file not found for version '{version}'"
                )

            content = config_path.read_text()
            metadata = registry.load_metadata(version)
            active_ver = registry.get_active_version()

            # Find previous version for diffing
            prev_version = None

            # 1. Query database for parent/older version of this evolution proposal
            db_created_at = None
            db_status = None
            try:
                row = await app.state.evolution_store.get_by_version(version)
                if row:
                    prev_version = row.get("old_version")
                    db_created_at = _log_time_iso(row)
                    if row.get("status") is not None:
                        db_status = str(row["status"])
            except Exception as e:
                logger.warning("Could not read %s from the evolution log: %s", version, e)

            if metadata:
                if (
                    not metadata.get("created_at") or metadata.get("created_at") == ""
                ) and db_created_at:
                    metadata["created_at"] = db_created_at
                if not metadata.get("status") and db_status:
                    metadata["status"] = db_status

            # 2. The parent recorded when the version was saved (save_version's
            #    lineage). Folder order is a guess; the parent is a fact.
            if not prev_version and metadata and metadata.get("parent_version"):
                parent = str(metadata["parent_version"])
                if parent != version and (config.algorithms_dir / parent / "config.yaml").is_file():
                    prev_version = parent

            # 3. Fallback to registry chronological sequence
            if not prev_version:
                try:
                    versions = registry.list_versions()
                    idx = next(
                        (i for i, v in enumerate(versions) if v.get("version") == version), None
                    )
                    if idx is not None and idx > 0:
                        prev_version = versions[idx - 1].get("version")
                except Exception:
                    pass

            diff_text = ""
            if prev_version:
                prev_path = config.algorithms_dir / prev_version / "config.yaml"
                if prev_path.is_file():
                    try:
                        import difflib

                        prev_content = prev_path.read_text()
                        diff_lines = list(
                            difflib.unified_diff(
                                prev_content.splitlines(keepends=True),
                                content.splitlines(keepends=True),
                                fromfile=f"{prev_version}/config.yaml",
                                tofile=f"{version}/config.yaml",
                            )
                        )
                        diff_text = "".join(diff_lines)
                    except Exception as de:
                        logger.warning(
                            "Failed to generate diff for algorithm version %s: %s", version, de
                        )

            return {
                "version": version,
                "content": content,
                "metadata": metadata,
                "is_active": (version.strip() == active_ver.strip()),
                "diff": diff_text,
                # Which version the diff compares against, so it can be read
                # as "changed from X" rather than guessed.
                "diff_against": prev_version,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to retrieve algorithm: {e}") from e

    @app.post("/api/algorithms/{version}/activate", dependencies=[Depends(verify_auth)])
    async def activate_algorithm_version_endpoint(version: str) -> dict[str, Any]:
        """Activate a specific version of algorithm parameters."""
        from evotrader.algorithms.registry import AlgorithmRegistry

        try:
            registry = AlgorithmRegistry(
                config.algorithms_dir,
                is_sim=(config.settings.mode.value == "sim"),
            )

            # Verify version directory exists
            version_dir = config.algorithms_dir / version
            if not version_dir.resolve().is_relative_to(config.algorithms_dir.resolve()):
                raise HTTPException(status_code=403, detail="Access denied")
            if not version_dir.is_dir():
                raise HTTPException(
                    status_code=404, detail=f"Algorithm version '{version}' not found"
                )

            registry.set_active_version(version)

            # Also update evolution_log in database to set status to 'ACTIVE'
            await app.state.evolution_store.update_status(version=version, status="ACTIVE")
            logger.info("Human manually activated algorithm version '%s'", version)
            return {"status": "success", "message": f"Algorithm version '{version}' activated."}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to activate algorithm: {e}") from e

    @app.post("/api/algorithms/{version}/reject", dependencies=[Depends(verify_auth)])
    async def reject_algorithm_version_endpoint(version: str) -> dict[str, Any]:
        """Mark a specific version of algorithm parameters as rejected."""
        from evotrader.algorithms.registry import AlgorithmRegistry

        try:
            registry = AlgorithmRegistry(
                config.algorithms_dir,
                is_sim=(config.settings.mode.value == "sim"),
            )

            # Verify version directory exists
            version_dir = config.algorithms_dir / version
            if not version_dir.resolve().is_relative_to(config.algorithms_dir.resolve()):
                raise HTTPException(status_code=403, detail="Access denied")
            if not version_dir.is_dir():
                raise HTTPException(
                    status_code=404, detail=f"Algorithm version '{version}' not found"
                )

            # Update metadata status to 'rejected'
            metadata_path = version_dir / "metadata.yaml"
            if metadata_path.is_file():
                try:
                    meta = registry._load_yaml(metadata_path)
                    meta["status"] = "rejected"
                    registry._save_yaml(metadata_path, meta)
                except Exception as e:
                    logger.warning(
                        "Failed to update metadata status to rejected for %s: %s", version, e
                    )

            # Update evolution_log in database
            updated = await app.state.evolution_store.update_status(
                version=version, status="REJECTED"
            )
            if not updated:
                raise HTTPException(
                    status_code=404,
                    detail=f"No evolution_log entry found for version '{version}' — nothing was rejected.",
                )
            logger.info("Human manually rejected algorithm version '%s'", version)
            return {"status": "success", "message": f"Algorithm version '{version}' rejected."}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to reject algorithm: {e}") from e

    # ── Code Reviews Endpoints ───────────────────────────────────

    @app.get("/api/reviews", dependencies=[Depends(verify_auth)])
    async def list_reviews() -> dict[str, Any]:
        """List all evolution proposals (code reviews + strategy proposals)."""

        items: list[dict] = []

        # ── Code reviews from data/evolution/reviews/ ──
        reviews_dir = config.data_dir / "evolution" / "reviews"
        if reviews_dir.is_dir():
            for p in reviews_dir.glob("*.md"):
                try:
                    content = p.read_text()
                    title = p.stem
                    status = "PENDING_REVIEW"
                    generated = ""
                    for line in content.splitlines():
                        if line.startswith("# Code Review:"):
                            title = line.replace("# Code Review:", "").strip()
                        elif line.startswith("**Generated**:"):
                            generated = line.replace("**Generated**:", "").strip()
                        elif line.startswith("**Status**:"):
                            status = line.replace("**Status**:", "").strip()
                    items.append(
                        {
                            "id": p.stem,
                            "title": title,
                            "status": status,
                            "generated": generated,
                            "type": "code_review",
                        }
                    )
                except Exception as e:
                    logger.error("Failed to parse code review file %s: %s", p, e)

        # ── Strategy proposals from data/evolution/proposals/ ──
        proposals_dir = config.data_dir / "evolution" / "proposals"
        if proposals_dir.is_dir():
            for p in proposals_dir.glob("*.md"):
                try:
                    content = p.read_text()
                    fm = _parse_yaml_frontmatter(content)
                    title = fm.get("target_strategy", p.stem)
                    raw_status = str(fm.get("status", "proposed")).upper()
                    # Normalise: proposal files use lowercase 'proposed'/'applied'/'rejected'
                    status = {
                        "PROPOSED": "PENDING_REVIEW",
                        "APPLIED": "APPLIED",
                        "REJECTED": "REJECTED",
                        "IN_PROGRESS": "PENDING_REVIEW",
                    }.get(raw_status, "PENDING_REVIEW")
                    generated = fm.get("created_at", "")
                    if generated:
                        generated = str(generated)
                    items.append(
                        {
                            "id": p.stem,
                            "title": title,
                            "status": status,
                            "generated": generated,
                            "type": "strategy_proposal",
                        }
                    )
                except Exception as e:
                    logger.error("Failed to parse strategy proposal file %s: %s", p, e)

        # Sort newest first by generated date
        items.sort(key=lambda x: x.get("generated", ""), reverse=True)
        return {"reviews": items}

    @app.get("/api/reviews/{review_id}", dependencies=[Depends(verify_auth)])
    async def get_review(review_id: str) -> dict[str, Any]:
        """Get the detailed content of a code review or strategy proposal."""
        file_path, item_type = _resolve_evolution_file(config, review_id)
        if not file_path:
            raise HTTPException(status_code=404, detail="Proposal not found")

        try:
            content = file_path.read_text()

            if item_type == "strategy_proposal":
                fm = _parse_yaml_frontmatter(content)
                title = fm.get("target_strategy", file_path.stem)
                raw_status = str(fm.get("status", "proposed")).upper()
                status = {
                    "PROPOSED": "PENDING_REVIEW",
                    "APPLIED": "APPLIED",
                    "REJECTED": "REJECTED",
                    "IN_PROGRESS": "PENDING_REVIEW",
                }.get(raw_status, "PENDING_REVIEW")
                generated = str(fm.get("created_at", ""))
            else:
                title = file_path.stem
                status = "PENDING_REVIEW"
                generated = ""
                for line in content.splitlines():
                    if line.startswith("# Code Review:"):
                        title = line.replace("# Code Review:", "").strip()
                    elif line.startswith("**Generated**:"):
                        generated = line.replace("**Generated**:", "").strip()
                    elif line.startswith("**Status**:"):
                        status = line.replace("**Status**:", "").strip()

            return {
                "id": review_id,
                "title": title,
                "status": status,
                "generated": generated,
                "content": content,
                "type": item_type,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read proposal: {e}") from e

    @app.post("/api/reviews/{review_id}/apply", dependencies=[Depends(verify_auth)])
    async def apply_review(review_id: str) -> dict[str, Any]:
        """Mark a code review or strategy proposal as applied."""
        file_path, item_type = _resolve_evolution_file(config, review_id)
        if not file_path:
            raise HTTPException(status_code=404, detail="Proposal not found")

        try:
            content = file_path.read_text()

            if item_type == "strategy_proposal":
                updated = _update_yaml_frontmatter_status(content, "applied")
            else:
                updated_lines = []
                found_status = False
                for line in content.splitlines():
                    if line.startswith("**Status**:"):
                        updated_lines.append("**Status**: APPLIED")
                        found_status = True
                    else:
                        updated_lines.append(line)
                if not found_status:
                    updated_lines.append("**Status**: APPLIED")
                updated = "\n".join(updated_lines)

            file_path.write_text(updated)

            # Also update evolution_log in database to set status to 'ACTIVE'
            await app.state.evolution_store.update_status(version=review_id, status="ACTIVE")

            kind = "strategy proposal" if item_type == "strategy_proposal" else "code review"
            logger.info("Human marked %s '%s' as APPLIED", kind, review_id)
            return {"status": "success", "message": f"Proposal '{review_id}' marked as applied."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to apply proposal: {e}") from e

    @app.post("/api/reviews/{review_id}/reject", dependencies=[Depends(verify_auth)])
    async def reject_review(review_id: str) -> dict[str, Any]:
        """Mark a code review or strategy proposal as rejected."""
        file_path, item_type = _resolve_evolution_file(config, review_id)
        if not file_path:
            raise HTTPException(status_code=404, detail="Proposal not found")

        try:
            content = file_path.read_text()

            if item_type == "strategy_proposal":
                updated = _update_yaml_frontmatter_status(content, "rejected")
            else:
                updated_lines = []
                found_status = False
                for line in content.splitlines():
                    if line.startswith("**Status**:"):
                        updated_lines.append("**Status**: REJECTED")
                        found_status = True
                    else:
                        updated_lines.append(line)
                if not found_status:
                    updated_lines.append("**Status**: REJECTED")
                updated = "\n".join(updated_lines)

            file_path.write_text(updated)

            # Also update evolution_log in database to set status to 'REJECTED'
            await app.state.evolution_store.update_status(version=review_id, status="REJECTED")

            kind = "strategy proposal" if item_type == "strategy_proposal" else "code review"
            logger.info("Human marked %s '%s' as REJECTED", kind, review_id)
            return {"status": "success", "message": f"Proposal '{review_id}' marked as rejected."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to reject proposal: {e}") from e

    # ── SSE Streaming Endpoint ───────────────────────────────────

    @app.get("/api/events")
    async def sse_events() -> StreamingResponse:
        """Server-Sent Events endpoint to stream live logs and order proposals."""

        async def event_generator() -> AsyncGenerator[str, None]:
            # Declared global because the stale-prompt check below clears it.
            # Without this, the assignment makes `pending_oauth` local to this
            # generator and the READ above it raises UnboundLocalError, which
            # kills the dashboard's entire event stream.
            global pending_oauth

            q: asyncio.Queue = asyncio.Queue()
            active_listeners.add(q)

            # Send initial state
            initial_status = json.dumps({"type": "status", "data": app.state.status})
            yield f"data: {initial_status}\n\n"

            # Replay pending OAuth state if auth is still needed — but never a
            # prompt older than one authorization window. An expired prompt is
            # not actionable: authorizing against it cannot resolve anything,
            # and it makes a working system look broken.
            if pending_oauth is not None:
                import time as _time

                issued = pending_oauth.get("issued_at", 0.0)
                if issued and _time.time() - issued > _OAUTH_PROMPT_TTL_S:
                    logger.info(
                        "Dropping an OAuth prompt that expired %.0fs ago",
                        _time.time() - issued - _OAUTH_PROMPT_TTL_S,
                    )
                    pending_oauth = None
                else:
                    oauth_event = json.dumps({"type": "oauth_required", "data": pending_oauth})
                    yield f"data: {oauth_event}\n\n"

            try:
                while True:
                    event = await q.get()
                    if event is None:  # the server is stopping
                        return
                    yield f"data: {event}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                active_listeners.remove(q)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ── Static File Server ───────────────────────────────────────

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/")
    async def get_index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/", RevalidatedStaticFiles(directory=static_dir), name="static")

    return app
