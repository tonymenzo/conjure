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
  ``agent_fixed_point``. LLM-callable wrappers around the
  Python combinators in ``combinator.combinators``.

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
