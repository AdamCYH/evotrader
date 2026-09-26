"""The contract every CLI-hosted agent harness must satisfy, plus the registry.

Adding a harness means writing one subclass and registering it. Nothing else in
the codebase changes: config refers to a backend by its ``driver`` string, and
callers receive an :class:`AgentResult` whichever harness produced it.

Registration is explicit rather than import-scanning. A backend that fails to
import (its optional dependency is not installed) must not break startup, so
``load_backend`` treats an import failure as "driver unavailable" and reports it
as text the caller can log — the same path as "CLI not on PATH".
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from evotrader.agents.cli.types import AgentRequest, AgentResult

logger = logging.getLogger(__name__)


class CliAgentBackend(ABC):
    """Drives one hosted-agent harness.

    Implementations must be safe to construct without the harness installed —
    availability is reported by :meth:`is_available`, never by raising from
    ``__init__`` — because config validation builds every configured backend to
    check its settings before anything runs.
    """

    driver: ClassVar[str]
    """Stable identifier used in settings. Never rename one in place."""

    display_name: ClassVar[str] = ""

    def __init__(self, options: Any = None) -> None:
        self.options = options

    @classmethod
    @abstractmethod
    def is_available(cls) -> tuple[bool, str]:
        """Whether this harness can run here, and why not when it cannot.

        Returns ``(ok, reason)``. The reason is surfaced to the operator on
        fallback, so it must name the concrete fix ("install the claude CLI",
        "run claude setup-token") rather than repeating the failure.
        """

    @abstractmethod
    async def run(self, request: AgentRequest) -> AgentResult:
        """Execute *request*.

        Must not raise for an expected operational failure — an exhausted quota,
        a harness that will not start, a run that hit its turn cap. Those come
        back as ``AgentResult(ok=False, ...)`` with ``quota`` or ``error`` set,
        because the caller's job is to fall back, not to catch vendor exceptions.
        Genuine programming errors should still propagate.
        """


# ── Registry ──────────────────────────────────────────────────────

_BACKENDS: dict[str, type[CliAgentBackend]] = {}


def register_backend(cls: type[CliAgentBackend]) -> type[CliAgentBackend]:
    """Register *cls* under its ``driver``. Usable as a decorator."""
    driver = getattr(cls, "driver", "")
    if not driver:
        raise ValueError(f"{cls.__name__} must define a non-empty `driver`")
    existing = _BACKENDS.get(driver)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"driver {driver!r} is already registered to {existing.__name__}; "
            f"drivers must be unique so settings resolve unambiguously"
        )
    _BACKENDS[driver] = cls
    return cls


def registered_drivers() -> tuple[str, ...]:
    """Every driver registered so far, sorted."""
    _import_builtin_backends()
    return tuple(sorted(_BACKENDS))


def get_backend_class(driver: str) -> type[CliAgentBackend]:
    """Look up a driver, with a message that lists the real alternatives."""
    _import_builtin_backends()
    try:
        return _BACKENDS[driver]
    except KeyError:
        raise ValueError(
            f"unknown CLI agent driver {driver!r}; registered drivers are "
            f"{', '.join(sorted(_BACKENDS)) or '(none)'}"
        ) from None


def load_backend(driver: str, options: Any = None) -> tuple[CliAgentBackend | None, str]:
    """Build a backend, or explain why it is unusable.

    Returns ``(backend, reason)`` where exactly one is meaningful: a backend and
    an empty reason, or None and a reason to log. Callers use this to decide
    whether to fall back without needing to know what can go wrong.
    """
    try:
        cls = get_backend_class(driver)
    except ValueError as exc:
        return None, str(exc)

    ok, reason = cls.is_available()
    if not ok:
        return None, reason
    return cls(options), ""


def _import_builtin_backends() -> None:
    """Import the adapters that ship with the project so they self-register.

    Kept lazy and failure-tolerant: an adapter whose optional dependency is
    missing should make that one driver unavailable, not prevent the registry
    from resolving the others.
    """
    from importlib import import_module

    for module in ("evotrader.agents.cli.claude_code",):
        try:
            import_module(module)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("CLI backend module %s did not load: %s", module, exc)
