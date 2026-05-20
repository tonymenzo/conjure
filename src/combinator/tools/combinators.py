"""LLM-callable wrappers around the Python combinators.

Each tool exposes one combinator. The agent passes a ``spec`` template
plus the iterable; the tool constructs a ``spec_factory`` that
interpolates ``{item}`` (or ``{value}``) into the spec's ``role_prompt``
and ``initial_message`` fields, then delegates to the Python combinator.

All tools return ``{"ok": True, "result": ...}`` on success and
``{"ok": False, "code": ..., "error": ...}`` on failure.
"""

from __future__ import annotations

from typing import Any, Callable

from combinator.combinators import (
    agent_filter,
    agent_fixed_point,
    agent_fold,
    agent_map,
)
from combinator.errors import Timeout
from combinator.record import AgentSpec
from combinator.tools._base import (
    RuntimeField,
    StateField,
    StatelessRuntimeTool,
    resolve_token,
)


def _build_factory(spec_template: dict[str, Any]) -> Callable[[Any], AgentSpec]:
    """Build a ``spec_factory`` from an LLM-supplied dict template.

    ``role_prompt`` and ``initial_message`` strings are
    ``str.format``-interpolated with the item (under ``{item}``) and
    (for fixed-point usage) ``{value}``.
    """
    role_prompt = spec_template.get("role_prompt", "")
    label = spec_template.get("label", "")
    tools = list(spec_template.get("tools") or [])
    llm = spec_template.get("llm", "default")
    initial_message = spec_template.get("initial_message", "") or ""

    def factory(item: Any) -> AgentSpec:
        return AgentSpec(
            role_prompt=_safe_format(role_prompt, item=item, value=item),
            label=label,
            tools=tools,
            llm=llm,
            initial_message=(
                _safe_format(initial_message, item=item, value=item)
                if initial_message
                else None
            ),
        )

    return factory


def _safe_format(template: str, **kwargs: Any) -> str:
    """``str.format`` that tolerates absent placeholders."""
    try:
        return template.format(**kwargs)
    except (IndexError, KeyError):
        return template


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


# ---------- Tool classes ----------

class AgentMapTool(StatelessRuntimeTool):
    """Map a worker spec over a list of items in parallel."""

    spec: dict = RuntimeField(description="Spec template for each worker.")
    items: list = RuntimeField(description="List of items to dispatch.")
    timeout_s: float = RuntimeField(
        default=120.0, description="Maximum seconds to wait for all replies."
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        resolved = resolve_token(self.runtime_token)
        if resolved is None:
            return _err("no_runtime", "tool is not bound to a runtime")
        runtime, caller_addr = resolved
        factory = _build_factory(self.spec or {})
        try:
            result = agent_map(
                runtime, caller_addr, factory, list(self.items or []),
                timeout_s=float(self.timeout_s or 120.0),
            )
        except Timeout as e:
            return _err("timeout", str(e))
        return {"ok": True, "result": result}


class AgentFoldTool(StatelessRuntimeTool):
    """Fold a worker spec over a list of items sequentially."""

    spec: dict = RuntimeField(description="Spec template for each worker.")
    items: list = RuntimeField(description="List of items to fold.")
    init: Any = RuntimeField(description="Initial accumulator value.")
    timeout_s: float = RuntimeField(default=120.0, description="Maximum seconds.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        resolved = resolve_token(self.runtime_token)
        if resolved is None:
            return _err("no_runtime", "tool is not bound to a runtime")
        runtime, caller_addr = resolved
        factory = _build_factory(self.spec or {})
        try:
            result = agent_fold(
                runtime, caller_addr, factory, list(self.items or []),
                init=self.init,
                timeout_s=float(self.timeout_s or 120.0),
            )
        except Timeout as e:
            return _err("timeout", str(e))
        return {"ok": True, "result": result}


class AgentFilterTool(StatelessRuntimeTool):
    """Filter items by spawning a worker per item and keeping truthy
    verdicts."""

    spec: dict = RuntimeField(description="Spec template for each worker.")
    items: list = RuntimeField(description="List of items to filter.")
    timeout_s: float = RuntimeField(default=120.0, description="Maximum seconds.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        resolved = resolve_token(self.runtime_token)
        if resolved is None:
            return _err("no_runtime", "tool is not bound to a runtime")
        runtime, caller_addr = resolved
        factory = _build_factory(self.spec or {})
        try:
            result = agent_filter(
                runtime, caller_addr, factory, list(self.items or []),
                timeout_s=float(self.timeout_s or 120.0),
            )
        except Timeout as e:
            return _err("timeout", str(e))
        return {"ok": True, "result": result}


class AgentFixedPointTool(StatelessRuntimeTool):
    """Iterate a worker spec on its own output until it converges."""

    spec: dict = RuntimeField(description="Spec template for each iteration.")
    seed: Any = RuntimeField(description="Initial value.")
    max_iters: int = RuntimeField(default=16, description="Maximum iterations.")
    timeout_s: float = RuntimeField(default=600.0, description="Maximum seconds.")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        resolved = resolve_token(self.runtime_token)
        if resolved is None:
            return _err("no_runtime", "tool is not bound to a runtime")
        runtime, caller_addr = resolved
        factory = _build_factory(self.spec or {})
        try:
            value, converged = agent_fixed_point(
                runtime, caller_addr, factory, self.seed,
                max_iters=int(self.max_iters or 16),
                timeout_s=float(self.timeout_s or 600.0),
            )
        except Timeout as e:
            return _err("timeout", str(e))
        return {"ok": True, "result": value, "converged": converged}


COMBINATOR_TOOL_CLASSES = (
    AgentMapTool,
    AgentFoldTool,
    AgentFilterTool,
    AgentFixedPointTool,
)


def build_combinator_tools(token: str) -> list[StatelessRuntimeTool]:
    return [cls(runtime_token=token) for cls in COMBINATOR_TOOL_CLASSES]
