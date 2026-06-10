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

from conjure.combinators import (
    agent_critic,
    agent_ensemble,
    agent_filter,
    agent_fixed_point,
    agent_fold,
    agent_map,
    agent_race,
    agent_supervisor,
)
from conjure.errors import BudgetExceeded, Timeout
from conjure.record import AgentSpec
from conjure.tools._base import (
    RuntimeField,
    StateField,
    StatelessRuntimeTool,
    resolve_token,
)


_TERSE_SUFFIX = (
    "\n\nIMPORTANT INSTRUCTIONS:\n"
    "1. The message you need to process is ALREADY SHOWN in this prompt "
    "(under 'You have N new message(s):'). Read it directly — do NOT "
    "call ``recv``, ``list_inbox``, or any other read tool.\n"
    "2. Compute the answer, then make ONE ``send`` call to the address "
    "in the message's ``reply_to`` field, with the result in ``body``.\n"
    "3. After the send returns ok, you are DONE. Reply with a single "
    "short sentence (one line) confirming completion. Do not narrate "
    "your reasoning."
)


def _build_factory(spec_template: dict[str, Any]) -> Callable[[Any], AgentSpec]:
    """Build a ``spec_factory`` from an LLM-supplied dict template.

    ``role_prompt`` and ``initial_message`` strings are
    ``str.format``-interpolated with the item (under ``{item}``) and
    (for fixed-point usage) ``{value}``. The role prompt is
    auto-augmented with a terseness clause so combinator workers don't
    drown the REPL in narration. The label is auto-suffixed with the
    worker's per-call index so siblings are distinguishable
    (``square-1``, ``square-2``, ...).
    """
    role_prompt = spec_template.get("role_prompt", "") or ""
    base_label = spec_template.get("label", "") or "worker"
    tools = list(spec_template.get("tools") or [])
    llm = spec_template.get("llm", "default")
    initial_message = spec_template.get("initial_message", "") or ""

    counter = {"n": 0}

    def factory(item: Any) -> AgentSpec:
        counter["n"] += 1
        idx = counter["n"]
        augmented_role = _safe_format(role_prompt, item=item, value=item) + _TERSE_SUFFIX
        return AgentSpec(
            role_prompt=augmented_role,
            label=f"{base_label}-{idx}",
            tools=tools,
            llm=llm,
            initial_message=(
                _safe_format(initial_message, item=item, value=item)
                if initial_message
                else None
            ),
        )

    return factory


def _build_spec(spec_template: dict[str, Any], label_suffix: str = "") -> AgentSpec:
    """Build a single ``AgentSpec`` from an LLM-supplied dict template.

    For HOFs where each spec is concrete (Race / Ensemble / Critic),
    so no per-item ``{item}`` interpolation is needed. ``label_suffix``
    is appended to keep parallel workers distinguishable.
    """
    role_prompt = (spec_template.get("role_prompt", "") or "") + _TERSE_SUFFIX
    label = spec_template.get("label", "") or "worker"
    if label_suffix:
        label = f"{label}-{label_suffix}"
    tools = list(spec_template.get("tools") or [])
    llm = spec_template.get("llm", "default")
    initial_message = spec_template.get("initial_message", "") or None
    return AgentSpec(
        role_prompt=role_prompt,
        label=label,
        tools=tools,
        llm=llm,
        initial_message=initial_message,
    )


def _safe_format(template: str, **kwargs: Any) -> str:
    """``str.format`` that tolerates absent placeholders."""
    try:
        return template.format(**kwargs)
    except (IndexError, KeyError):
        return template


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _timeout_payload(exc: Timeout, stage: str) -> dict[str, Any]:
    """Convert a ``Timeout`` raised by the combinator helpers into a
    structured tool response so the agent can see which workers
    replied, which didn't, and what made it back."""
    payload: dict[str, Any] = {
        "ok": False,
        "code": "timeout",
        "stage": stage,
        "error": str(exc),
    }
    if exc.workers is not None:
        payload["workers"] = list(exc.workers)
    if exc.received is not None:
        payload["received"] = exc.received
    if exc.expected is not None:
        payload["expected"] = exc.expected
    if exc.partial is not None:
        payload["partial"] = list(exc.partial)
    return payload


# ---------- Tool classes ----------

class AgentMapTool(StatelessRuntimeTool):
    """Map a worker spec over a list of items in parallel."""

    spec: dict = RuntimeField(description="Spec template for each worker.")
    items: list = RuntimeField(description="List of items to dispatch.")
    timeout_s: float = RuntimeField(
        default=60.0, description="Maximum seconds to wait for all replies."
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
            return _timeout_payload(e, stage="gather")
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
        return {"ok": True, "result": result}


class AgentFoldTool(StatelessRuntimeTool):
    """Fold a worker spec over a list of items sequentially. Pass
    ``trace=True`` to also receive the per-step accumulator history
    — required when the chain of intermediate values is the value
    (narration, drift detection, progress UIs). Without ``trace``
    you only see the final accumulator."""

    spec: dict = RuntimeField(description="Spec template for each worker.")
    items: list = RuntimeField(description="List of items to fold.")
    init: Any = RuntimeField(description="Initial accumulator value.")
    timeout_s: float = RuntimeField(default=60.0, description="Maximum seconds.")
    trace: bool = RuntimeField(
        default=False,
        description=(
            "When true, return ``{result, trace}`` where ``trace`` is "
            "``[init, acc_after_step_0, ..., final]``. Lets you see "
            "every intermediate accumulator instead of just the last."
        ),
    )
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
                trace=bool(self.trace),
            )
        except Timeout as e:
            return _timeout_payload(e, stage="gather")
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
        if isinstance(result, dict) and "trace" in result:
            return {"ok": True, "result": result["result"], "trace": result["trace"]}
        return {"ok": True, "result": result}


class AgentFilterTool(StatelessRuntimeTool):
    """Filter items by spawning a worker per item and keeping truthy
    verdicts."""

    spec: dict = RuntimeField(description="Spec template for each worker.")
    items: list = RuntimeField(description="List of items to filter.")
    timeout_s: float = RuntimeField(default=60.0, description="Maximum seconds.")
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
            return _timeout_payload(e, stage="gather")
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
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
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
        return {"ok": True, "result": value, "converged": converged}


class AgentRaceTool(StatelessRuntimeTool):
    """Race N specs on the same body; return the first reply, kill losers.

    Use when quality vs. latency is the tradeoff and you don't know which
    spec will give the best answer fastest — race haiku/sonnet/opus,
    race retrieval strategies, race reasoning approaches. Returns
    ``{winner_idx, result}`` so the caller can attribute the answer.
    """

    specs: list = RuntimeField(
        description="List of spec template dicts — one worker per spec."
    )
    body: Any = RuntimeField(description="Body dispatched to every worker.")
    timeout_s: float = RuntimeField(
        default=60.0,
        description="Maximum seconds to wait for the first reply.",
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        resolved = resolve_token(self.runtime_token)
        if resolved is None:
            return _err("no_runtime", "tool is not bound to a runtime")
        runtime, caller_addr = resolved
        specs_in = list(self.specs or [])
        if not specs_in:
            return _err("bad_args", "specs must be non-empty")
        specs = [_build_spec(s or {}, label_suffix=str(i + 1))
                 for i, s in enumerate(specs_in)]
        try:
            winner_idx, winner_body = agent_race(
                runtime, caller_addr, specs, self.body,
                timeout_s=float(self.timeout_s or 60.0),
            )
        except Timeout as e:
            return _timeout_payload(e, stage="race")
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
        return {"ok": True, "winner_idx": winner_idx, "result": winner_body}


class AgentEnsembleTool(StatelessRuntimeTool):
    """Best-of-N synthesis: fan out N specs on the same body, hand all
    replies to an aggregator spec, return the aggregator's synthesis.

    Use when quality through diversity matters — generate N drafts and
    synthesize; ask N differently-prompted critics and aggregate the
    verdict; vote-based classification. The aggregator receives
    ``{"item": [reply1, ..., replyN], "reply_to": ...}`` and is free to
    vote, synthesize, or pick. Foundation for AgentQuorum and
    AgentTournament-shaped workflows.
    """

    specs: list = RuntimeField(
        description="List of spec template dicts for the parallel workers."
    )
    body: Any = RuntimeField(description="Body dispatched to every worker.")
    aggregator_spec: dict = RuntimeField(
        description="Spec template for the agent that synthesizes the N replies."
    )
    timeout_s: float = RuntimeField(
        default=120.0,
        description="Maximum seconds for each stage (workers and aggregator).",
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        resolved = resolve_token(self.runtime_token)
        if resolved is None:
            return _err("no_runtime", "tool is not bound to a runtime")
        runtime, caller_addr = resolved
        specs_in = list(self.specs or [])
        if not specs_in:
            return _err("bad_args", "specs must be non-empty")
        if not self.aggregator_spec:
            return _err("bad_args", "aggregator_spec is required")
        worker_specs = [_build_spec(s or {}, label_suffix=str(i + 1))
                        for i, s in enumerate(specs_in)]
        agg_spec = _build_spec(self.aggregator_spec, label_suffix="agg")
        try:
            result = agent_ensemble(
                runtime, caller_addr, worker_specs, self.body, agg_spec,
                timeout_s=float(self.timeout_s or 120.0),
            )
        except Timeout as e:
            return _timeout_payload(e, stage="ensemble")
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
        return {"ok": True, "result": result}


class AgentCriticTool(StatelessRuntimeTool):
    """Generator + critic refinement loop. Each iteration, a fresh
    generator (with accumulated critique as ``feedback``) produces an
    output, a fresh critic reviews it. Stops when the critic returns
    ``{"ok": true, "notes": ...}`` or after ``max_iters``.

    Critic reply shape: dict ``{"ok": bool, "notes": str}``, JSON string
    of same shape, or plain text starting with ``ok``/``approved``/``lgtm``
    (case-insensitive) for approval. The real shape ``AgentFixedPoint``
    was reaching for — strict equality almost never fires on agent
    output; a critic that says "good enough" does. Common uses: writer
    + editor, code + linter, solution + verifier.
    """

    generator_spec: dict = RuntimeField(
        description="Spec template for the agent that produces drafts."
    )
    critic_spec: dict = RuntimeField(
        description="Spec template for the agent that reviews drafts."
    )
    body: Any = RuntimeField(description="Task body handed to the generator.")
    max_iters: int = RuntimeField(
        default=5,
        description="Iteration cap (generator+critic counted as one iter).",
    )
    timeout_s: float = RuntimeField(
        default=300.0,
        description="Maximum seconds per generator or critic step.",
    )
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        resolved = resolve_token(self.runtime_token)
        if resolved is None:
            return _err("no_runtime", "tool is not bound to a runtime")
        runtime, caller_addr = resolved
        if not self.generator_spec or not self.critic_spec:
            return _err(
                "bad_args", "generator_spec and critic_spec are required"
            )
        gen_spec = _build_spec(self.generator_spec, label_suffix="gen")
        crit_spec = _build_spec(self.critic_spec, label_suffix="crit")
        try:
            output, converged, iters = agent_critic(
                runtime, caller_addr, gen_spec, crit_spec, self.body,
                max_iters=int(self.max_iters or 5),
                timeout_s=float(self.timeout_s or 300.0),
            )
        except Timeout as e:
            return _timeout_payload(e, stage="critic")
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
        return {
            "ok": True,
            "result": output,
            "converged": converged,
            "iters": iters,
        }


class AgentSupervisorTool(StatelessRuntimeTool):
    """Supervised parallel map: like ``AgentMap``, but a worker whose
    engine errors is automatically torn down and respawned (same item,
    fresh agent) up to ``max_restarts`` times. Items that exhaust their
    restart budget come back in ``failed`` instead of sinking the whole
    fan-out.

    Use instead of ``AgentMap`` when workers are flaky (network tools,
    long chains, rate-limit-prone models) and partial progress matters
    more than fail-fast.
    """

    spec: dict = RuntimeField(description="Spec template for each worker.")
    items: list = RuntimeField(description="List of items to dispatch.")
    max_restarts: int = RuntimeField(
        default=2,
        description="Restart budget per item before it is marked failed.",
    )
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
            out = agent_supervisor(
                runtime, caller_addr, factory, list(self.items or []),
                max_restarts=int(self.max_restarts or 0),
                timeout_s=float(self.timeout_s or 120.0),
            )
        except Timeout as e:
            return _timeout_payload(e, stage="supervise")
        except BudgetExceeded as e:
            return _err("budget_exceeded", str(e))
        return {
            "ok": True,
            "result": out["results"],
            "restarts": out["restarts"],
            "failed": out["failed"],
        }


COMBINATOR_TOOL_CLASSES = (
    AgentMapTool,
    AgentFoldTool,
    AgentFilterTool,
    AgentFixedPointTool,
    AgentRaceTool,
    AgentEnsembleTool,
    AgentCriticTool,
    AgentSupervisorTool,
)


def build_combinator_tools(token: str) -> list[StatelessRuntimeTool]:
    return [cls(runtime_token=token) for cls in COMBINATOR_TOOL_CLASSES]
