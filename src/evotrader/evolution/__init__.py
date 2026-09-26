"""Self-evolution engine for EvoTrader.

Provides the core infrastructure for autonomous system improvement:
- **Parameter evolution** — Tune strategy thresholds and weights
- **Code evolution** — Generate and validate new strategy code
- **Instruction evolution** — Improve agent system prompts
- **Knowledge evolution** — Grow the semantic memory from experience
- **Code review** — Analyse infrastructure code for improvements

All changes are versioned, rate-limited, and gated through the
constitution's safety rules.
"""
