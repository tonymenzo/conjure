# Changelog

## v0.1.0 — substrate release

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
