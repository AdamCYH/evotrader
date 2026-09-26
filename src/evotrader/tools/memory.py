"""Semantic memory — ChromaDB vector store for experience-based learning.

Stores and retrieves embedded trade experiences, market patterns, and
**user-provided notes/instructions** so agents can learn from the past
and incorporate human guidance into their decisions.

The memory system has three collection types:
1. **trade_experiences** — Embedded trade outcomes and reasoning
2. **market_patterns** — Market condition snapshots for similarity search
3. **user_notes** — Human-provided observations, instructions, and context

User notes are loaded from ``data/notes/`` directory (markdown files) and
automatically embedded on startup + file change. This gives the user a
simple way to inject knowledge: just write a markdown file.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

logger = logging.getLogger(__name__)

# Collection names
TRADE_EXPERIENCES = "trade_experiences"
MARKET_PATTERNS = "market_patterns"
USER_NOTES = "user_notes"


class SemanticMemory:
    """ChromaDB-backed semantic memory for EvoTrader agents.

    Provides embedding-based storage and retrieval for trade experiences,
    market patterns, and user-provided notes.
    """

    def __init__(self, persist_dir: Path) -> None:
        self._persist_dir = persist_dir
        persist_dir.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(
                anonymized_telemetry=False,
                allow_reset=False,
            ),
        )

        # Create or get collections
        self._trades = self._client.get_or_create_collection(
            name=TRADE_EXPERIENCES,
            metadata={"description": "Embedded trade outcomes and reasoning"},
        )
        self._patterns = self._client.get_or_create_collection(
            name=MARKET_PATTERNS,
            metadata={"description": "Market condition snapshots"},
        )
        self._notes = self._client.get_or_create_collection(
            name=USER_NOTES,
            metadata={"description": "User-provided notes and instructions"},
        )

        logger.info(
            "Semantic memory initialised — trades=%d, patterns=%d, notes=%d",
            self._trades.count(),
            self._patterns.count(),
            self._notes.count(),
        )

    # ═══════════════════════════════════════════════════════════════
    # Trade Experiences
    # ═══════════════════════════════════════════════════════════════

    def store_trade_experience(
        self,
        trade_id: int,
        direction: str,
        regime: str,
        algo_signal: float,
        llm_signal: float | None,
        hybrid_score: float,
        reasoning: str,
        outcome_pnl: float | None = None,
        holding_period_s: int | None = None,
        session_id: str | None = None,
        related_trade_id: int | None = None,
        action: str | None = None,
        timestamp: str | None = None,
        ticker: str | None = None,
    ) -> None:
        """Store a trade experience for similarity-based learning.

        The embedding text combines action, direction, regime, signals, reasoning,
        and final realized outcome so that similar past trades and their profitability
        can be retrieved when facing comparable market conditions.
        """
        # Build embedding text
        action_prefix = f" ({action})" if action else ""
        parts = [
            f"Trade #{trade_id}{action_prefix}: {direction} in {regime} regime.",
            f"Algo signal: {algo_signal:.3f}, LLM signal: {llm_signal or 'N/A'},",
            f"Hybrid score: {hybrid_score:.3f}.",
            f"Reasoning: {reasoning}",
        ]
        if outcome_pnl is not None:
            result_text = (
                "profit" if outcome_pnl > 0 else ("loss" if outcome_pnl < 0 else "breakeven")
            )
            parts.append(f"Outcome: ${outcome_pnl:+.2f} ({result_text}).")
        if holding_period_s is not None and holding_period_s > 0:
            parts.append(f"Held for {holding_period_s}s.")

        doc_text = " ".join(parts)

        now = datetime.now(UTC)
        metadata: dict[str, Any] = {
            "trade_id": trade_id,
            "direction": direction,
            "regime": regime,
            "algo_signal": algo_signal,
            "llm_signal": llm_signal or 0.0,
            "hybrid_score": hybrid_score,
            "outcome_pnl": float(outcome_pnl) if outcome_pnl is not None else 0.0,
            "has_outcome": outcome_pnl is not None,
            "holding_period_s": holding_period_s or 0,
            "timestamp": timestamp or now.isoformat(),
            "timestamp_epoch": now.timestamp(),
            # The instrument. Without it a QQQ-era experience is retrieved for an
            # MSTR decision as if it were the same market — and it was: all 141
            # experiences stored before 2026-09-18 carried no ticker at all.
            "ticker": (ticker or "").upper(),
        }
        if related_trade_id:
            metadata["related_trade_id"] = related_trade_id
        if action:
            metadata["action"] = action
        if session_id:
            metadata["session_id"] = session_id

        self._trades.upsert(
            ids=[f"trade_{trade_id}"],
            documents=[doc_text],
            metadatas=[metadata],
        )
        logger.debug("Stored trade experience #%d", trade_id)

    def update_trade_outcome(
        self,
        trade_id: int,
        outcome_pnl: float,
        holding_period_s: int | None = None,
    ) -> bool:
        """Update an existing trade experience with its final realized outcome and holding period."""
        try:
            entry_id = f"trade_{trade_id}"
            res = self._trades.get(ids=[entry_id], include=["documents", "metadatas"])
            if not res or not res.get("ids") or len(res["ids"]) == 0:
                return False

            doc_text = res["documents"][0] if res.get("documents") else f"Trade #{trade_id}"
            meta = res["metadatas"][0] if res.get("metadatas") else {}

            # Remove previous outcome snippet if present to avoid duplicates
            if "Outcome:" in doc_text:
                doc_text = doc_text.split("Outcome:")[0].strip()

            result_text = (
                "profit" if outcome_pnl > 0 else ("loss" if outcome_pnl < 0 else "breakeven")
            )
            doc_text = f"{doc_text} Outcome: ${outcome_pnl:+.2f} ({result_text})."
            if holding_period_s is not None and holding_period_s > 0:
                if "Held for" in doc_text:
                    doc_text = doc_text.split("Held for")[0].strip()
                doc_text = f"{doc_text} Held for {holding_period_s}s."

            meta["outcome_pnl"] = float(outcome_pnl)
            meta["has_outcome"] = True
            if holding_period_s is not None:
                meta["holding_period_s"] = holding_period_s

            self._trades.upsert(
                ids=[entry_id],
                documents=[doc_text],
                metadatas=[meta],
            )
            logger.info("Updated trade experience #%d with outcome $%.2f", trade_id, outcome_pnl)
            return True
        except Exception as e:
            logger.warning("Failed to update trade experience outcome for #%s: %s", trade_id, e)
            return False

    async def sync_experiences_from_journal(self, journal: Any) -> int:
        """Synchronize all closed and open decision trades from SQLite Journal into ChromaDB.

        Ensures realized PnL and holding periods match confirmed executions.
        """
        async with journal._db.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, ticker, action, direction, quantity, price, fill_price,
                       realized_pnl, holding_period_s, related_trade_id, session_id,
                       order_status, regime, algo_version, algo_signal, llm_signal,
                       hybrid_score, reasoning, timestamp
                FROM trades
                WHERE (order_status = 'FILLED' OR order_status IS NULL)
                AND regime != 'reconciliation'
                AND algo_version != 'system_sync'
                ORDER BY id ASC
                """
            )
            rows = await cursor.fetchall()

        if not rows:
            return 0

        trades_dict = {dict(r)["id"]: dict(r) for r in rows}

        # First pass: map exit trade outcomes back to their entry trades
        for _tid, d in trades_dict.items():
            pnl = d.get("realized_pnl")
            rel_id = d.get("related_trade_id")
            if rel_id and pnl is not None and rel_id in trades_dict:
                trades_dict[rel_id]["outcome_pnl"] = float(pnl)
                if d.get("holding_period_s") is not None:
                    trades_dict[rel_id]["holding_period_s"] = int(d["holding_period_s"])

        # Second pass: build batch records
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        now = datetime.now(UTC)
        for _tid, d in trades_dict.items():
            trade_id = d["id"]
            direction = str(d.get("direction") or "LONG")
            regime = str(d.get("regime") or "unknown")
            algo_signal = float(d.get("algo_signal") or 0.0)
            llm_signal = float(d["llm_signal"]) if d.get("llm_signal") is not None else 0.0
            hybrid_score = float(d.get("hybrid_score") or 0.0)
            reasoning = str(d.get("reasoning") or "")
            outcome_pnl = (
                float(d["realized_pnl"])
                if d.get("realized_pnl") is not None
                else (float(d["outcome_pnl"]) if "outcome_pnl" in d else None)
            )
            holding_period_s = (
                int(d["holding_period_s"]) if d.get("holding_period_s") is not None else None
            )
            action = str(d.get("action") or "TRADE").upper()
            rel_id = int(d["related_trade_id"]) if d.get("related_trade_id") is not None else None
            ts_str = d.get("timestamp") or now.isoformat()

            # Build embedding text
            action_prefix = f" ({action})" if action else ""
            parts = [
                f"Trade #{trade_id}{action_prefix}: {direction} in {regime} regime.",
                f"Algo signal: {algo_signal:.3f}, LLM signal: {llm_signal if d.get('llm_signal') is not None else 'N/A'},",
                f"Hybrid score: {hybrid_score:.3f}.",
                f"Reasoning: {reasoning}",
            ]
            if outcome_pnl is not None:
                result_text = (
                    "profit" if outcome_pnl > 0 else ("loss" if outcome_pnl < 0 else "breakeven")
                )
                parts.append(f"Outcome: ${outcome_pnl:+.2f} ({result_text}).")
            if holding_period_s is not None and holding_period_s > 0:
                parts.append(f"Held for {holding_period_s}s.")

            doc_text = " ".join(parts)
            meta: dict[str, Any] = {
                "trade_id": trade_id,
                "direction": direction,
                "regime": regime,
                "algo_signal": algo_signal,
                "llm_signal": llm_signal,
                "hybrid_score": hybrid_score,
                "outcome_pnl": outcome_pnl if outcome_pnl is not None else 0.0,
                "has_outcome": outcome_pnl is not None,
                "holding_period_s": holding_period_s or 0,
                "timestamp": ts_str,
                "timestamp_epoch": now.timestamp(),
                "action": action,
            }
            if rel_id:
                meta["related_trade_id"] = rel_id
            if d.get("session_id"):
                meta["session_id"] = d["session_id"]

            ids.append(f"trade_{trade_id}")
            documents.append(doc_text)
            metadatas.append(meta)

        # Batch upsert into ChromaDB in chunks of 50
        batch_size = 50
        for i in range(0, len(ids), batch_size):
            self._trades.upsert(
                ids=ids[i : i + batch_size],
                documents=documents[i : i + batch_size],
                metadatas=metadatas[i : i + batch_size],
            )

        logger.info("Synchronized %d trade experiences from journal into ChromaDB", len(ids))
        return len(ids)

    def query_similar_trades(
        self,
        query: str,
        n_results: int = 5,
        regime: str | None = None,
        ticker: str | None = None,
        max_age_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find similar past trade experiences.

        Args:
            query: Natural language description of current situation.
            n_results: Maximum number of results.
            regime: Optional filter to only return trades from this regime.
            ticker: Optional filter to this instrument only. History from a
                different instrument is a different market — an index ETF with
                a 1% ATR and a single stock with a 6.5% ATR do not share a base
                rate — so callers should pass the instrument they are trading.
            max_age_days: Optional recency window. Old experiences describe
                conditions, parameters and instructions that no longer exist.

        Returns:
            List of dicts with 'document', 'metadata', and 'distance'.
        """
        clauses: list[dict[str, Any]] = []
        if regime:
            clauses.append({"regime": regime})
        if ticker:
            clauses.append({"ticker": ticker.upper()})
        if max_age_days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).timestamp()
            clauses.append({"timestamp_epoch": {"$gte": cutoff}})
        where: dict[str, Any] | None
        if not clauses:
            where = None
        elif len(clauses) == 1:
            where = clauses[0]
        else:
            where = {"$and": clauses}

        results = self._trades.query(
            query_texts=[query],
            n_results=n_results,
            where=where,
        )

        return _flatten_results(results)

    def backfill_trade_tickers(self, trade_id_to_ticker: dict[int, str]) -> dict[str, int]:
        """Stamp `ticker` onto experiences stored before it was recorded.

        One-time repair. Every experience carries its `trade_id`, so the
        instrument is recoverable from the trades table; without this, the
        ticker filter above would silently exclude the entire pre-existing
        history rather than scope it.
        """
        stats = {"examined": 0, "updated": 0, "unresolved": 0}
        try:
            got = self._trades.get(include=["metadatas"])
        except Exception:
            logger.warning("backfill_trade_tickers: could not read experiences", exc_info=True)
            return stats
        ids, metas = got.get("ids") or [], got.get("metadatas") or []
        upd_ids: list[str] = []
        upd_metas: list[dict[str, Any]] = []
        for _id, md in zip(ids, metas, strict=False):
            stats["examined"] += 1
            md = dict(md or {})
            if md.get("ticker"):
                continue
            try:
                tid = int(md.get("trade_id"))
            except (TypeError, ValueError):
                stats["unresolved"] += 1
                continue
            ticker = trade_id_to_ticker.get(tid)
            if not ticker:
                stats["unresolved"] += 1
                continue
            md["ticker"] = ticker.upper()
            upd_ids.append(_id)
            upd_metas.append(md)
        if upd_ids:
            self._trades.update(ids=upd_ids, metadatas=upd_metas)
            stats["updated"] = len(upd_ids)
        return stats

    # ═══════════════════════════════════════════════════════════════
    # User Notes — The human knowledge interface
    # ═══════════════════════════════════════════════════════════════

    def store_note(
        self,
        note_id: str,
        text: str,
        source: str = "user",
        category: str = "general",
        priority: str = "normal",
    ) -> None:
        """Store a user-provided note or instruction.

        Args:
            note_id: Unique identifier for the note.
            text: The note content (will be embedded for semantic search).
            source: Where the note came from ('user', 'file', 'cli').
            category: Note category ('instruction', 'observation',
                'market_insight', 'strategy_hint', 'general').
            priority: 'critical', 'high', 'normal', or 'low'.
        """
        now = datetime.now(UTC)
        self._notes.upsert(
            ids=[note_id],
            documents=[text],
            metadatas=[
                {
                    "source": source,
                    "category": category,
                    "priority": priority,
                    "timestamp": now.isoformat(),
                    "timestamp_epoch": now.timestamp(),
                }
            ],
        )
        logger.info("Stored note '%s' (category=%s, priority=%s)", note_id, category, priority)

    def query_notes(
        self,
        query: str,
        n_results: int = 5,
        category: str | None = None,
        agent_note_max_age_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find relevant user notes for the current context.

        Args:
            query: Natural language description of what you're looking for.
            n_results: Maximum number of results.
            category: Optional filter by note category.

        Returns:
            List of dicts with 'document', 'metadata', and 'distance'.
        """
        clauses: list[dict[str, Any]] = []
        if category:
            clauses.append({"category": category})
        if agent_note_max_age_days is not None:
            # Human notes (source user/file/cli) are deliberate and never
            # filtered. AGENT-written notes describe the tape on the day they
            # were written — 447 of them accumulated through the QQQ era and
            # were being retrieved for MSTR decisions as if still current.
            cutoff = (datetime.now(UTC) - timedelta(days=agent_note_max_age_days)).timestamp()
            clauses.append(
                {
                    "$or": [
                        {"source": {"$ne": "agent_learning"}},
                        {"timestamp_epoch": {"$gte": cutoff}},
                    ]
                }
            )
        where: dict[str, Any] | None
        if not clauses:
            where = None
        elif len(clauses) == 1:
            where = clauses[0]
        else:
            where = {"$and": clauses}

        results = self._notes.query(
            query_texts=[query],
            n_results=n_results,
            where=where,
        )
        return _flatten_results(results)

    def get_all_notes(self) -> list[dict[str, Any]]:
        """Return all stored notes (useful for debugging / listing)."""
        result = self._notes.get()
        notes = []
        for i, doc in enumerate(result.get("documents", [])):
            meta = result["metadatas"][i] if result.get("metadatas") else {}
            note_id = result["ids"][i] if result.get("ids") else f"note_{i}"
            notes.append({"id": note_id, "text": doc, "metadata": meta})
        return notes

    def get_all_trade_experiences(self) -> list[dict[str, Any]]:
        """Return all stored trade experiences in ChromaDB."""
        result = self._trades.get()
        trades = []
        for i, doc in enumerate(result.get("documents", [])):
            meta = result["metadatas"][i] if result.get("metadatas") else {}
            trade_id = result["ids"][i] if result.get("ids") else f"trade_{i}"
            trades.append({"id": trade_id, "text": doc, "metadata": meta})
        return trades

    def delete_note(self, note_id: str) -> None:
        """Delete a note by its ID."""
        self._notes.delete(ids=[note_id])
        logger.info("Deleted note '%s'", note_id)

    # ═══════════════════════════════════════════════════════════════
    # Notes File Loader — watches data/notes/*.md
    # ═══════════════════════════════════════════════════════════════

    def load_notes_from_directory(self, notes_dir: Path) -> int:
        """Load all markdown files from the notes directory.

        Each file becomes a note (or multiple notes if separated by ``---``).
        Files are tracked by content hash to avoid re-embedding unchanged files.

        Args:
            notes_dir: Path to the notes directory (e.g., ``data/notes/``).

        Returns:
            Number of notes loaded or updated.
        """
        # Clear existing file notes to prevent stale/deleted file notes persisting in memory
        try:
            existing = self._notes.get(where={"source": "file"})
            if existing and existing.get("ids"):
                self._notes.delete(ids=existing["ids"])
        except Exception as e:
            logger.warning("Failed to clear old file notes from memory: %s", e)

        if not notes_dir.is_dir():
            logger.debug("Notes directory does not exist: %s", notes_dir)
            return 0

        count = 0
        for md_file in sorted(notes_dir.glob("*.md")):
            count += self._load_notes_file(md_file)

        # Also load .txt files for quick notes
        for txt_file in sorted(notes_dir.glob("*.txt")):
            count += self._load_notes_file(txt_file)

        if count > 0:
            logger.info("Loaded %d notes from %s", count, notes_dir)
        return count

    def _load_notes_file(self, file_path: Path) -> int:
        """Parse and store notes from a single file.

        Supports two formats:
        1. Single note — entire file is one note
        2. Multi-note — sections separated by ``---`` or ``## `` headings

        Metadata can be set via YAML-like frontmatter or inline tags:
        - ``priority: critical`` → sets priority
        - ``category: market_insight`` → sets category
        - ``#instruction`` → category shorthand
        """
        text = file_path.read_text().strip()
        if not text:
            return 0

        file_stem = file_path.stem
        content_hash = hashlib.md5(text.encode()).hexdigest()[:8]

        # Parse frontmatter if present
        meta = _parse_frontmatter(text)
        body = meta.pop("__body__", text)
        category = meta.get("category", _guess_category(file_stem))
        priority = meta.get("priority", "normal")

        # Split on --- separators for multi-note files
        sections = [s.strip() for s in body.split("\n---\n") if s.strip()]
        if not sections:
            sections = [body]

        count = 0
        for i, section in enumerate(sections):
            # Extract heading if present
            lines = section.strip().split("\n")
            heading = ""
            if lines[0].startswith("## "):
                heading = lines[0][3:].strip()
                section = "\n".join(lines[1:]).strip()

            note_id = f"file_{file_stem}_{content_hash}_{i}"
            if heading:
                section = f"{heading}: {section}"

            self.store_note(
                note_id=note_id,
                text=section,
                source="file",
                category=category,
                priority=priority,
            )
            count += 1

        return count

    # ═══════════════════════════════════════════════════════════════
    # Memory Pruning — prevent unbounded growth
    # ═══════════════════════════════════════════════════════════════

    def prune(self, max_age_days: int = 90) -> dict[str, int]:
        """Remove stale entries older than ``max_age_days``.

        Prunes trade experiences and market patterns. User notes
        (source='file' or 'user') are NEVER pruned — they represent
        deliberate human input.

        Agent-generated notes older than the cutoff ARE pruned, as
        they may reference outdated market conditions.

        Args:
            max_age_days: Maximum age in days (default 90).

        Returns:
            Dict with counts of pruned entries per collection.
        """
        cutoff_epoch = (datetime.now(UTC) - timedelta(days=max_age_days)).timestamp()

        pruned = {"trade_experiences": 0, "market_patterns": 0, "agent_notes": 0}

        # Prune old trade experiences
        try:
            old_trades = self._trades.get(
                where={"timestamp_epoch": {"$lt": cutoff_epoch}},
            )
            if old_trades and old_trades.get("ids"):
                self._trades.delete(ids=old_trades["ids"])
                pruned["trade_experiences"] = len(old_trades["ids"])
        except Exception as e:
            logger.warning("Failed to prune trade experiences: %s", e)

        # Prune old market patterns
        try:
            old_patterns = self._patterns.get(
                where={"timestamp_epoch": {"$lt": cutoff_epoch}},
            )
            if old_patterns and old_patterns.get("ids"):
                self._patterns.delete(ids=old_patterns["ids"])
                pruned["market_patterns"] = len(old_patterns["ids"])
        except Exception as e:
            logger.warning("Failed to prune market patterns: %s", e)

        # Prune old agent-generated notes (keep user/file notes forever)
        try:
            for agent_source in ("agent", "agent_learning"):
                old_notes = self._notes.get(
                    where={
                        "$and": [
                            {"source": agent_source},
                            {"timestamp_epoch": {"$lt": cutoff_epoch}},
                        ]
                    },
                )
                if old_notes and old_notes.get("ids"):
                    self._notes.delete(ids=old_notes["ids"])
                    pruned["agent_notes"] += len(old_notes["ids"])
        except Exception as e:
            logger.warning("Failed to prune agent notes: %s", e)

        total = sum(pruned.values())
        if total > 0:
            logger.info(
                "Memory pruned: %d entries removed (trades=%d, patterns=%d, notes=%d)",
                total,
                pruned["trade_experiences"],
                pruned["market_patterns"],
                pruned["agent_notes"],
            )

        return pruned

    def clear_all_memories(self) -> None:
        """Wipe all ChromaDB collections."""
        try:
            trades = self._trades.get()
            if trades and trades.get("ids"):
                self._trades.delete(ids=trades["ids"])
            patterns = self._patterns.get()
            if patterns and patterns.get("ids"):
                self._patterns.delete(ids=patterns["ids"])
            notes = self._notes.get()
            if notes and notes.get("ids"):
                self._notes.delete(ids=notes["ids"])
            logger.info("Successfully cleared all semantic memories.")
        except Exception as e:
            logger.error("Failed to clear semantic memories: %s", e)
            raise

    # ═══════════════════════════════════════════════════════════════
    # Stats
    # ═══════════════════════════════════════════════════════════════

    def stats(self) -> dict[str, int]:
        """Return collection sizes."""
        return {
            "trade_experiences": self._trades.count(),
            "market_patterns": self._patterns.count(),
            "user_notes": self._notes.count(),
        }


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _flatten_results(results: dict) -> list[dict[str, Any]]:
    """Flatten ChromaDB query results into a list of dicts.

    Results are re-ranked with a staleness penalty: older entries get a
    slightly higher adjusted distance so that recent, relevant entries
    rank ahead of stale ones when semantic similarity is close.

    Penalty: 1% per day of age (a 30-day-old note gets +30% distance).
    """
    items = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]
    ids = results.get("ids", [[]])[0]

    now = datetime.now(UTC)

    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else {}
        distance = dists[i] if i < len(dists) else None

        # Compute staleness in days from the timestamp metadata
        staleness_days = 0.0
        ts_str = meta.get("timestamp") if meta else None
        if ts_str and distance is not None:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                staleness_days = max(0.0, (now - ts).total_seconds() / 86400)
            except Exception:
                pass

        # Apply time-decay penalty: 1% per day
        adjusted_distance = distance
        if distance is not None and staleness_days > 0:
            adjusted_distance = distance * (1.0 + 0.01 * staleness_days)

        items.append(
            {
                "id": ids[i] if i < len(ids) else None,
                "document": doc,
                "metadata": meta,
                "distance": distance,
                "adjusted_distance": adjusted_distance,
                "staleness_days": round(staleness_days, 1),
            }
        )

    # Re-sort by adjusted distance (lower = better)
    items.sort(key=lambda x: x.get("adjusted_distance") or float("inf"))

    return items


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML-like frontmatter from text.

    Supports a simple format:
    ```
    ---
    category: instruction
    priority: high
    ---
    Actual note content here.
    ```
    """
    if not text.startswith("---"):
        return {"__body__": text}

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {"__body__": text}

    frontmatter = parts[1].strip()
    body = parts[2].strip()

    meta: dict[str, Any] = {"__body__": body}
    for line in frontmatter.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()

    return meta


def _guess_category(filename: str) -> str:
    """Guess note category from filename."""
    name = filename.lower()
    if "instruct" in name or "rule" in name:
        return "instruction"
    if "market" in name or "analysis" in name:
        return "market_insight"
    if "strategy" in name or "hint" in name:
        return "strategy_hint"
    if "observ" in name or "pattern" in name:
        return "observation"
    return "general"
