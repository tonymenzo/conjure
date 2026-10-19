"""Tool argument schemas, compacted for the UI.

The web UI previews a collapsed tool call by its arguments: the
required ones first, and none that merely repeat the tool's default.
For that it needs, per tool, the argument order, which arguments are
required, and their defaults. Engines report this for the tools they
offer (``tool_schemas()``); the driver journals it with the agent's
other provenance (``agent_meta``), so a replay or an export previews
calls the same way.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable


def compact(schema: dict[str, Any] | None) -> dict[str, Any]:
    """``{"order", "required", "defaults"}`` from a JSON input schema."""
    props = (schema or {}).get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    required = [k for k in (schema or {}).get("required") or [] if k in props]
    defaults = {}
    for key, prop in props.items():
        if isinstance(prop, dict) and "default" in prop and _plain(prop["default"]):
            defaults[key] = prop["default"]
    return {"order": list(props), "required": required, "defaults": defaults}


def from_tools(tools: Iterable[Any], *, name: Callable[[Any, Any], str] | None = None) -> dict[str, dict[str, Any]]:
    """Compact schemas for orchestral-style tools (``get_tool_spec()``),
    keyed by the name the model calls them by (``name(spec, tool)``)."""
    out: dict[str, dict[str, Any]] = {}
    for tool in tools:
        try:
            spec = tool.get_tool_spec()
            key = name(spec, tool) if name else spec.name
            schema = compact(spec.input_schema)
        except Exception:
            continue
        # orchestral leaves defaults out of the schema the model sees;
        # a pydantic tool still declares them on its fields.
        fields = getattr(tool, "model_fields", None) or {}
        for arg in schema["order"]:
            if arg in schema["required"] or arg in schema["defaults"] or arg not in fields:
                continue
            default = getattr(fields[arg], "default", None)
            if default is not None and type(default).__name__ != "PydanticUndefinedType" and _plain(default):
                schema["defaults"][arg] = default
        out[key] = schema
    return out


def _plain(value: Any) -> bool:
    """Whether a default is worth journaling: JSON, and small."""
    try:
        return len(json.dumps(value)) <= 200
    except (TypeError, ValueError):
        return False
