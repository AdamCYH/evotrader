"""Read-only access to the ADK telemetry database."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TelemetryReader:
    """Read-only access to the ADK telemetry database (spans table)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def get_llm_calls(
        self,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch local logs of LLM requests and responses from the telemetry database."""
        if not self._db_path.exists():
            return []

        query = """
            SELECT c.span_id, c.trace_id, c.start_time_unix_nano, c.end_time_unix_nano, c.session_id, c.attributes_json, p.name as parent_name
            FROM spans c
            LEFT JOIN spans p ON c.parent_span_id = p.span_id
            WHERE c.name = 'call_llm'
        """
        params: list[Any] = []

        if session_id:
            query += " AND (c.session_id = ? OR json_extract(c.attributes_json, '$.\"gcp.vertex.agent.session_id\"') = ?)"
            params.extend([session_id, session_id])

        query += " ORDER BY c.start_time_unix_nano DESC LIMIT ?"
        params.append(limit)

        logs = []
        try:
            # Connect sync since sqlite query is very fast
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            conn.close()

            for row in rows:
                attrs = {}
                if row["attributes_json"]:
                    try:
                        attrs = json.loads(row["attributes_json"])
                    except Exception:
                        pass

                # Calculate duration
                start_time = row["start_time_unix_nano"]
                end_time = row["end_time_unix_nano"]
                duration_ms = None
                if start_time and end_time:
                    duration_ms = (end_time - start_time) / 1_000_000.0

                # Extract request / response content
                llm_request_str = attrs.get("gcp.vertex.agent.llm_request", "{}")
                llm_response_str = attrs.get("gcp.vertex.agent.llm_response", "{}")

                llm_request = {}
                llm_response = {}
                try:
                    llm_request = json.loads(llm_request_str)
                except Exception:
                    pass
                try:
                    llm_response = json.loads(llm_response_str)
                except Exception:
                    pass

                # Extract input & output tokens
                input_tokens = attrs.get("gen_ai.usage.input_tokens")
                output_tokens = attrs.get("gen_ai.usage.output_tokens")
                reasoning_tokens = attrs.get("gen_ai.usage.reasoning.output_tokens")

                # Extract agent name
                parent_name = row["parent_name"]
                agent_name = "unknown"
                if parent_name and parent_name.startswith("invoke_agent "):
                    agent_name = parent_name.replace("invoke_agent ", "")
                elif attrs.get("gcp.vertex.agent.name"):
                    agent_name = attrs.get("gcp.vertex.agent.name")

                logs.append(
                    {
                        "span_id": row["span_id"],
                        "trace_id": row["trace_id"],
                        "session_id": row["session_id"] or attrs.get("gcp.vertex.agent.session_id"),
                        "agent_name": agent_name,
                        "model": attrs.get("gen_ai.request.model") or llm_request.get("model"),
                        "duration_ms": duration_ms,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "reasoning_tokens": reasoning_tokens,
                        "request": llm_request,
                        "response": llm_response,
                        "timestamp": datetime.fromtimestamp(
                            start_time / 1_000_000_000.0, tz=UTC
                        ).isoformat()
                        if start_time
                        else None,
                    }
                )
        except Exception as e:
            logger.error("Failed to read telemetry database: %s", e)
            raise

        return logs

    def get_llm_metrics_summary(self, period: str = "all", limit: int = 1000) -> dict[str, Any]:
        """Fetch and compute aggregated LLM metrics efficiently without parsing large JSONs."""
        if not self._db_path.exists():
            return {}

        query = """
            SELECT
                COALESCE(c.session_id, json_extract(c.attributes_json, '$."gcp.vertex.agent.session_id"'), 'unknown') as sid,
                CAST(json_extract(c.attributes_json, '$."gen_ai.usage.input_tokens"') AS INTEGER) as inp,
                CAST(json_extract(c.attributes_json, '$."gen_ai.usage.output_tokens"') AS INTEGER) as out,
                (c.end_time_unix_nano - c.start_time_unix_nano) / 1000000.0 as lat,
                c.start_time_unix_nano as ts,
                COALESCE(
                    json_extract(c.attributes_json, '$."gen_ai.request.model"'),
                    json_extract(json_extract(c.attributes_json, '$."gcp.vertex.agent.llm_request"'), '$.model'),
                    'unknown'
                ) as model,
                COALESCE(json_extract(c.attributes_json, '$."gcp.vertex.agent.name"'), p.name, 'unknown') as agent_name
            FROM spans c
            LEFT JOIN spans p ON c.parent_span_id = p.span_id
            WHERE c.name = 'call_llm'
              AND c.start_time_unix_nano > ?
            ORDER BY c.start_time_unix_nano DESC
            LIMIT ?
        """
        from evotrader.tools.market_hours import ET
        from evotrader.utils import get_period_cutoff_dt

        cutoff_dt = get_period_cutoff_dt(period, tzinfo=ET)
        cutoff_nano = int(cutoff_dt.timestamp() * 1e9) if cutoff_dt else 0

        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, [cutoff_nano, limit])
            rows = cursor.fetchall()
            conn.close()

            total_calls = len(rows)
            total_tokens = 0
            total_input = 0
            total_output = 0
            total_latency = 0.0
            models = {}
            sessions = {}

            for row in rows:
                inp = row["inp"] or 0
                out = row["out"] or 0
                lat = row["lat"]
                ts = row["ts"]
                model = row["model"] or "unknown"
                sid = row["sid"] or "unknown"
                agent_name = row["agent_name"] or "unknown"

                total_tokens += inp + out
                total_input += inp
                total_output += out
                if lat:
                    total_latency += lat

                if model not in models:
                    models[model] = {"calls": 0, "tokens": 0, "input": 0, "output": 0}
                models[model]["calls"] += 1
                models[model]["tokens"] += inp + out
                models[model]["input"] += inp
                models[model]["output"] += out

                if sid not in sessions:
                    sessions[sid] = {
                        "calls": 0,
                        "in": 0,
                        "out": 0,
                        "lat": 0.0,
                        "max_ts": 0,
                        "agents": set(),
                    }
                sessions[sid]["calls"] += 1
                sessions[sid]["in"] += inp
                sessions[sid]["out"] += out
                if lat:
                    sessions[sid]["lat"] += lat
                if ts and ts > sessions[sid]["max_ts"]:
                    sessions[sid]["max_ts"] = ts
                if agent_name:
                    sessions[sid]["agents"].add(agent_name.lower())

            avg_latency = (total_latency / total_calls) if total_calls > 0 else 0.0
            efficiency = (total_output / total_input) if total_input > 0 else 0.0

            session_list = []
            for sid, dat in sessions.items():
                # Convert max_ts from nano to iso string with UTC timezone
                last_active_iso = (
                    datetime.fromtimestamp(dat["max_ts"] / 1_000_000_000.0, tz=UTC).isoformat()
                    if dat["max_ts"] > 0
                    else None
                )
                agents = dat.get("agents", set())
                is_evolution = any("evolution" in a for a in agents) or "evol" in sid.lower()
                session_type = "evolution" if is_evolution else "trading"

                session_list.append(
                    {
                        "session_id": sid,
                        "session_type": session_type,
                        "calls_count": dat["calls"],
                        "input_tokens": dat["in"],
                        "output_tokens": dat["out"],
                        "avg_latency_ms": dat["lat"] / dat["calls"] if dat["calls"] > 0 else 0.0,
                        "last_active": last_active_iso,
                        "last_active_ts": dat["max_ts"],
                    }
                )

            # Sort by most recent first
            session_list.sort(key=lambda x: x["last_active_ts"], reverse=True)

            return {
                "total_calls": total_calls,
                "total_tokens": total_tokens,
                "avg_latency_ms": avg_latency,
                "token_efficiency_ratio": efficiency,
                "model_distribution": models,
                "session_metrics": session_list,
            }

        except Exception as e:
            logger.error("Failed to read telemetry database summary: %s", e)
            return {}
