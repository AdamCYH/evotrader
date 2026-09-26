"""Derive JSON Schemas for local Python functions handed to a hosted agent.

Every CLI harness needs the same thing — a tool's name, description and
parameter schema — and every one of them derives it from the same place: the
function's signature and its Google-style docstring. So this lives beside the
backend contract rather than inside any one adapter or agent.

Mirrors what ADK's ``FunctionTool`` derives automatically, so an agent moved
between the API runtime and a CLI runtime is offered the same tool surface and
behaves the same way.
"""

from __future__ import annotations

import inspect
import re
import typing
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _json_type(annotation: Any) -> dict[str, Any]:
    """Map a Python annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # Optional[X] / X | None → schema of X (optionality is expressed via
    # `required`, not the type).
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _json_type(non_none[0])
        return {}

    if origin in (list, set, tuple):
        item = _json_type(args[0]) if args else {}
        return {"type": "array", "items": item or {}}

    if origin is dict:
        value = _json_type(args[1]) if len(args) > 1 else {}
        schema: dict[str, Any] = {"type": "object"}
        if value:
            schema["additionalProperties"] = value
        return schema

    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        dict: {"type": "object"},
        list: {"type": "array"},
    }.get(annotation, {})


def _split_docstring(fn: Callable) -> tuple[str, dict[str, str]]:
    """Return ``(summary, {param: description})`` from a Google-style docstring."""
    doc = inspect.getdoc(fn) or ""
    if not doc:
        return f"Call {fn.__name__}.", {}

    # Everything before Args:/Returns:/Raises: is the description.
    section_re = re.compile(r"^(Args|Arguments|Returns|Raises|Note|Notes):\s*$", re.M)
    match = section_re.search(doc)
    summary = (doc[: match.start()] if match else doc).strip()

    params: dict[str, str] = {}
    args_match = re.search(r"^(?:Args|Arguments):\s*$", doc, re.M)
    if args_match:
        tail = doc[args_match.end() :]
        end = section_re.search(tail)
        block = tail[: end.start()] if end else tail
        current: str | None = None
        for line in block.splitlines():
            entry = re.match(r"\s+(\*{0,2}\w+)\s*(?:\([^)]*\))?:\s*(.*)$", line)
            if entry:
                current = entry.group(1).lstrip("*")
                params[current] = entry.group(2).strip()
            elif current and line.strip():
                params[current] += " " + line.strip()

    return summary or f"Call {fn.__name__}.", params


def build_input_schema(fn: Callable) -> dict[str, Any]:
    """Derive a JSON Schema for *fn*'s parameters.

    A full JSON Schema (rather than the SDK's ``{"name": type}`` shorthand) is
    used because the shorthand marks every key required, and most evolution
    tools take optional arguments with meaningful defaults.
    """
    signature = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # pragma: no cover - unresolvable forward refs
        hints = {}

    _, param_docs = _split_docstring(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in signature.parameters.items():
        if name in ("self", "cls") or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        schema = _json_type(hints.get(name, param.annotation)) or {"type": "string"}
        description = param_docs.get(name)
        if description:
            schema = {**schema, "description": description}
        if param.default is not inspect.Parameter.empty:
            # Advertise the default so the model can leave it alone. A None
            # default is skipped: `"default": null` contradicts the declared
            # type and some validators reject it.
            if param.default is not None:
                schema = {**schema, "default": param.default}
        else:
            required.append(name)
        properties[name] = schema

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }
