"""Tool group registry — resolve ``spec.tools`` string names into bound
tool instances.

Engines call ``build_tools(token, names)`` to materialize the tool list
for a freshly spawned agent. Each name resolves to a tuple of tool
classes; the classes are instantiated with ``runtime_token=token`` so
each agent's tools authenticate against the runtime as that agent.

Default groups:

- ``"primitive"`` — the seven capability tools (spawn, send, recv,
  wait_for, terminate, introduce, list_inbox).
- ``"combinator"`` — the four FP-style combinator tools (agent_map,
  agent_fold, agent_filter, agent_fixed_point).
- ``"all"`` — both of the above.
"""

from __future__ import annotations

from typing import Mapping

from spawn.tools._base import StatelessRuntimeTool
from spawn.tools.combinators import COMBINATOR_TOOL_CLASSES
from spawn.tools.filesystem import FILESYSTEM_TOOL_CLASSES
from spawn.tools.primitives import PRIMITIVE_TOOL_CLASSES


ToolGroupRegistry = Mapping[str, tuple[type[StatelessRuntimeTool], ...]]


DEFAULT_TOOL_GROUPS: ToolGroupRegistry = {
    "primitive": PRIMITIVE_TOOL_CLASSES,
    "combinator": COMBINATOR_TOOL_CLASSES,
    "filesystem": tuple(FILESYSTEM_TOOL_CLASSES),
    "all": (
        PRIMITIVE_TOOL_CLASSES
        + COMBINATOR_TOOL_CLASSES
        + tuple(FILESYSTEM_TOOL_CLASSES)
    ),
}


def build_tools(
    token: str,
    names: list[str],
    registry: ToolGroupRegistry | None = None,
) -> list[StatelessRuntimeTool]:
    """Instantiate the tools requested by ``names``, deduplicated."""
    reg = registry if registry is not None else DEFAULT_TOOL_GROUPS
    classes: list[type[StatelessRuntimeTool]] = []
    for name in names:
        if name not in reg:
            raise ValueError(
                f"unknown tool group: {name!r} (known: {sorted(reg)})"
            )
        classes.extend(reg[name])

    # Preserve insertion order while deduplicating.
    seen: set[type] = set()
    unique: list[type[StatelessRuntimeTool]] = []
    for cls in classes:
        if cls not in seen:
            seen.add(cls)
            unique.append(cls)
    return [cls(runtime_token=token) for cls in unique]
