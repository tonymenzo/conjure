# Changelog

## v0.1.2 — reliability layer

### Packaging / docs

- Package description rewritten around the three primitives (no longer
  leads with the orchestral dependency); README opening to match.
- Hero banner removed pending final art.

### Reliability

- **``agent_supervisor``** — supervised fan-out: ``agent_map`` with
  one-for-one restarts. Errored workers are torn down and respawned on
  the same item up to ``max_restarts`` times; items that exhaust the
  budget come back in ``failed`` instead of sinking the whole fan-out.
  Ships as the ``AgentSupervisor`` tool (registered in both MCP
  bridges + the claude_agent allowlist + system prompt).
- **Hierarchical cost ceilings** — ``AgentSpec.budget`` (USD) caps an
  agent plus its entire subtree. Budgets attenuate like capabilities:
  an agent is blocked when *any* ancestor's ceiling is spent. Spawns
  under an exhausted subtree raise ``BudgetExceeded`` (tool code
  ``budget_exceeded``); drivers skip further steps and emit a one-time
  ``budget_exceeded`` child_event to the parent. Zero overhead while
  no budgets are set (flag-gated fast path).
- **Terminate interrupts in-flight steps** — ``Runtime.terminate`` /
  ``terminate_batch`` now call ``engine.interrupt()`` (best-effort,
  outside the registry lock) on every terminated agent.
  ``ClaudeAgentEngine`` implements it via the SDK's ``interrupt()``,
  so killing an agent aborts its in-flight LLM call instead of
  draining it — races stop burning loser tokens. Engine errors on
  already-terminated agents no longer emit spurious ``errored``
  supervision events.

### Performance

- Control-plane snapshot (2 Hz UI tick): ``_tree`` walks the registry
  under one lock acquisition instead of one per node; ``_cost`` builds
  rows in a single pass (was 1 + N lock round-trips); ``_log_events``
  caches each agent's parsed log tail behind an mtime + size stat —
  idle agents cost a ``stat()`` instead of a read + JSON parse per
  tick.

## v0.1.1 — PyPI page fix

- README hero image references the absolute GitHub raw URL so the PyPI
  project page renders it (relative paths only resolve on GitHub).
- Project URLs added to the package metadata (repository, changelog).

## v0.1.0 — substrate release

### Packaging (PyPI release prep)

- Licensed under **AGPL-3.0-only** (``LICENSE`` + PEP 639 metadata).
- ``py.typed`` marker shipped — the package is typed for downstream
  checkers.
- ``textual`` and ``libtmux`` moved to the ``[ui]`` extra; the core
  install is library + ``conjure repl``. ``conjure run`` degrades with
  a pointer to ``pip install conjure-agents[ui]`` when the extra is absent.
- ``mcp`` declared directly (``orchestral.mcp.server`` imports it but
  orchestral-ai ≤1.6.2 doesn't declare it).
- ``SpawnTool``'s optional fields (``tools``, ``capabilities``,
  ``sandbox_dir``, ``permissions``, ``model``) use a new
  ``OptionalRuntimeField`` so orchestral's schema generator marks them
  optional — a plain ``default=None`` is treated as "no default" and
  made required, which forced every spawn call to pass every field.
- Test fakes updated for orchestral-ai 1.6.x's ``LLM`` abstract surface
  (``_format_tool_choice``); suite verified green against released
  orchestral-ai 1.4.0 and 1.6.2 and the local orchestral-core HEAD.

First tagged release. Ships the v0.1 substrate described in
[`DESIGN.md`](DESIGN.md): recursive spawn, addressable mailboxes,
capability passing, and FP-style combinators on top.

### Substrate

- **Address, Envelope, CapabilitySet** — frozen value types for routing
  and authorization.
- **Mailbox** — threadsafe FIFO inbox with per-inbox monotonic
  sequencing and a single ``read`` primitive that subsumes peek, recv,
  and wait_for.
- **Runtime** — owns the spawn tree, agent registry, capability sets,
  and an append-only JSONL persistence journal. Replay reconstructs
  spawn tree and inbox contents from disk.
- **Driver + Agent + Engine** — per-agent thread loop wrapped around a
  pluggable ``Engine`` protocol. Production engines wrap
  ``orchestral.Agent``; tests use ``ScriptedEngine``.

### Tools

- **Primitive tools**: ``spawn``, ``send``, ``recv``, ``wait_for``,
  ``terminate``, ``introduce``, ``list_inbox``. Each has both a pure-
  Python ``*_impl`` and an orchestral ``@define_tool``-wrapped class.
  Capability enforcement lives in the tool layer.
- **Combinator tools**: ``agent_map``, ``agent_fold``, ``agent_filter``,
  ``agent_fixed_point``, ``agent_race``, ``agent_ensemble``,
  ``agent_critic``. LLM-callable wrappers around the Python combinators
  in ``conjure.combinators``. The first four are the FP-style core; the
  last three are higher-order patterns (race for quality-vs-latency,
  ensemble for best-of-N synthesis via an aggregator agent, critic for
  generator/critic refinement loops).

### Integrations

- **Toolbase consumer**: ``AgentSpec`` gains ``toolbase_profile: str |
  None``. When set, the ``claude_agent`` engine wires a second MCP
  server (``toolbase serve --profile <name> --no-tui``) into the
  child's ``mcp_servers`` alongside spawn's own surface. Lets a parent
  agent curate which toolbase profile (and therefore which toolkits /
  tools) each subagent sees — agent-curates-tools-for-subagents.
  Strictly opt-in; ``None`` (default) means no toolbase wiring.
  Requires ``toolbase`` on ``PATH``.

### Tests

- **121 tests** total, all offline (no LLM, no network).
- Unit tests cover every value type, the mailbox, the runtime
  lifecycle, persistence + replay, the driver loop, primitive-tool
  capability enforcement, and the combinators.
- Toy programs (``tests/programs/``) demonstrate:
  - **Recursive spawn**: factorial as a depth-``n`` chain.
  - **Map**: sum-of-squares via ``agent_map``.
  - **Tree decomposition**: prime factorization with smallest-divisor
    recursion.
  - **Capability passing + shared state**: memoized Fibonacci with a
    shared cache agent introduced to each worker.
  - **Fixed-point iteration**: whitespace-string convergence.
  - **Capability violation**: siblings can't message without an
    introduction.
  - **Termination cascade**: middle-of-chain terminate kills
    descendants.
  - **Persistence replay**: sum-of-squares run, shut down, then
    rehydrated from journal.

### Deferred / Known Limitations

- No distributed / multi-process operation (in-memory only).
- No capability revocation.
- No mid-LLM-call interruption on terminate (in-flight ``step``
  finishes before the driver exits).
- Cost ceilings are tracked but not enforced.
- No CLI or human-facing UI.
- See ``DESIGN.md`` §6 and the implementation plan's "Open Decisions
  Deferred" for more.
