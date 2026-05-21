# Agentic higher-order functions

> Living design doc. Tracks the space of higher-order functions over
> agents that the combinator substrate enables. Companion to
> `DESIGN.md`, which covers the underlying philosophy.

## 0. Why this doc

The current `combinator` package ships four FP-style combinators —
`AgentMap`, `AgentFold`, `AgentFilter`, `AgentFixedPoint`. They were
implemented first because they're recognizable to anyone with an FP
background and they stress-test the substrate. But they're a small
slice of the actual design space.

The real selling point is this: **once `spawn` + `send` + `WaitFor` +
supervision exist, the rest is library code.** Most known
concurrent/distributed/FP coordination patterns reduce to ~30–100
lines on top of the primitives. New patterns that only make sense
for LLM agents (debate, race, ensemble) are similarly small.

This doc enumerates the design space so we can pick what to build
next deliberately, not just whatever catches the eye.

---

## 1. Substrate summary (what we have to build on)

### 1.1 Primitives

| Tool | Purpose |
|---|---|
| `Spawn(spec, ...)` | Create a child agent. Returns its address. Recursive — any agent can spawn. |
| `Send(to, body)` | Deliver a message to an addressed inbox. Capability-checked. |
| `Recv(thread?, from_?, since_seq?, timeout_s?)` | Non-blocking peek of own inbox. |
| `WaitFor(predicate_kind, value, max_n, timeout_s)` | Block until `max_n` matching envelopes accumulate or deadline fires. Returns partial. |
| `Terminate(addr, cascade?)` | Kill a descendant. Cascades by default. |
| `Introduce(child, capability)` | Pass an address to a descendant as a capability. |
| `ListInbox(since_seq?, max_n?)` | Snapshot own inbox without consuming. |
| `Peek(addr, max_envelopes?)` | Snapshot a descendant's status + recent inbox. Ancestor-only. |

### 1.2 Structural properties

- **Mailboxes are addressable & persistent.** Per-inbox FIFO; every envelope journaled.
- **Capabilities passable.** Possession of an address grants permission to send.
- **Supervision events.** When a child terminates or its engine errors, the parent automatically receives a `@system → parent` envelope:
  ```json
  {"kind": "child_event", "event": "terminated"|"errored",
   "child_addr": ..., "child_label": ..., "reason": ...}
  ```
- **Lazy agents.** `spec.lazy=True` creates an addressable inbox without starting a driver — the basis for lazy collectors.
- **One-shot lifecycle.** `spec.oneshot=True` auto-terminates after the first successful step.
- **Address shortcuts.** `"self"` / `"parent"` / `"<label>"` resolve at the tool boundary.
- **Per-spec model selection.** Children default to `haiku`; opt up to `sonnet` / `opus` per spawn.
- **Lazy engine connect.** `Spawn` returns in ms; the child's SDK connects on its first turn. Multiple spawns connect in parallel.

### 1.3 Existing FP combinators (for reference)

- `AgentMap(spec, items)` — fan out N workers, gather replies in input order.
- `AgentFold(spec, items, init)` — sequential thread of state through workers.
- `AgentFilter(spec, items)` — keep items whose worker returns truthy.
- `AgentFixedPoint(spec, seed, max_iters)` — iterate until `eq(new, current)`.

Of the four, `AgentMap` and `AgentFilter` are workhorses. `AgentFold`
is niche (most fold-shapes flatten to a single agent with a growing
scratch). `AgentFixedPoint`'s strict-equality convergence almost
never fires in practice — what people want is closer to
`AgentCritic` (see §3.2).

---

## 2. Design space — by category

Each entry: **name**, signature sketch, problem solved, 1–3
concrete uses, implementation pointer.

### 2.1 Reliability & latency

These wrap a single piece of work with a control structure inherited from concurrency literature.

#### `AgentRace(specs, body)` — speculative execution
- **Solves:** quality-vs-latency tradeoff when you don't know which model / strategy will give the best answer.
- **Uses:** race haiku/sonnet/opus on a hard question; race three different retrieval strategies; race three reasoning approaches.
- **Sketch:** `AgentMap` shape but the collector returns on the *first* reply; `Terminate(cascade=True)` the losers in the finally block.

#### `AgentHedge(spec, body, n=3, delay=2.0)` — tail latency hedging
- **Solves:** p99 latency when occasional Claude calls land on slow shards.
- **Uses:** any user-facing single-shot agent invocation; "Tail at Scale" pattern (Dean & Barroso).
- **Sketch:** spawn the first worker immediately; if no reply by `delay`, spawn a second; etc. First reply wins, terminate the others.

#### `AgentRetry(spec, body, max=3, backoff=2.0)` — retry on error
- **Solves:** flaky tool calls, rate limits, transient model failures.
- **Uses:** any production wrap around a single agent invocation.
- **Sketch:** spawn worker, `Send` body, wait for either reply or `child_event errored` (from supervision). On error, terminate corpse, sleep `backoff^attempt`, retry with fresh worker. Composes naturally with `AgentRace` for retry-with-different-spec.

#### `AgentBudget(spec, body, max_cost, max_turns, max_time)` — hard caps
- **Solves:** runaway agents in production.
- **Uses:** any agent invocation whose cost / time / token use must be bounded.
- **Sketch:** spawn worker with `max_turns` on the SDK options; watch elapsed time and cost via control RPC; terminate when any budget hits zero, return partial result.

#### `AgentTimeout(spec, body, timeout_s, on_timeout)` — explicit deadline
- **Solves:** "if no reply in N seconds, give up and continue."
- **Uses:** any optional sub-task whose result is nice-to-have.
- **Sketch:** thin wrapper around `WaitFor`; on timeout, terminate worker and call `on_timeout` (which could be a fallback `Spawn` with a cheaper spec).

### 2.2 Multi-perspective deliberation (agent-native)

The FP world doesn't have these because functions don't have opinions. These are where the substrate starts to feel like its own programming model.

#### `AgentEnsemble(specs, body, aggregator_spec)` — best-of-N synthesis
- **Solves:** quality through diversity.
- **Uses:** generate N drafts of an email and synthesize; ask three differently-prompted critics and aggregate the verdict; vote-based classification.
- **Sketch:** `AgentMap` to fan out, but the collector hands the gathered replies to an aggregator agent (not just `return list`). The aggregator can vote, synthesize, or pick.

#### `AgentDebate(specs, prompt, rounds=3, judge_spec)` — adversarial reasoning
- **Solves:** surfacing weaknesses in an argument through opposition.
- **Uses:** scientific claim review (skeptic + advocate + judge); plan critique; pro/con/judge ethics analysis.
- **Sketch:** spawn each spec as a persistent agent; round 1 — `Send` each the prompt; round k>1 — `Send` each the previous round's transcript; after K rounds, `Send` full transcript to judge. References Anthropic's debate work.

#### `AgentTournament(specs, prompt, bracket="single-elim", judge_spec)` — ranking
- **Solves:** "which of these N candidates is best?"
- **Uses:** rank model outputs; pick between N proposed plans; pick between N candidate prompts in a prompt-optimization sweep.
- **Sketch:** pairwise matches dispatched as `AgentMap` over bracket rounds; each match is a judge invocation; advance the winner. Final winner is the "best."

#### `AgentQuorum(specs, body, threshold=2)` — consensus
- **Solves:** correctness via redundancy.
- **Uses:** safety-critical classification ("is this PII?"); decisions that must be defensible.
- **Sketch:** `AgentMap` to N replicas; collector accepts the answer once K of them agree (by exact match, semantic equiv, or a judge).

#### `AgentCritic(generator_spec, critic_spec, max_iters, max_time)` — generate / critique loop
- **Solves:** iterative refinement until quality threshold met (the *real* shape `AgentFixedPoint` was reaching for).
- **Uses:** writer + editor loop; code + linter loop; solution + verifier loop.
- **Sketch:** generator produces draft; critic produces `{ok: bool, notes: ...}`; if ok, return; else feed notes back to generator. Stop when ok or `max_iters` exhausted.

### 2.3 Spec composition (topology over specs)

The FP map/fold operate on *data*. These operate on *spec chains* — composing specs the way you'd compose functions.

#### `AgentCascade(spec_chain, body)` — sequential refinement pipeline
- **Solves:** multi-stage transformation where each stage needs a different specialist.
- **Uses:** outline → draft → polish → fact-check; rough plan → detailed plan → cost estimate; transcript → summary → bullet points.
- **Sketch:** spawn each spec, threading the previous stage's output to the next. Like FP `compose` but the intermediate types are strings / structured data, not typed values.

#### `AgentMapReduce(map_spec, reduce_spec, items)` — true MapReduce
- **Solves:** parallel batch + synthesis. Differs from `AgentMap` because the gather phase is an agent, not just `return list`.
- **Uses:** summarize each chapter then synthesize a book summary; analyze each file then build a project overview; per-item review then overall verdict.
- **Sketch:** `AgentMap` for the map stage; spawn a `reduce_spec` worker, feed it the gathered list, return its synthesis.

#### `AgentSweep(matrix, runner=AgentMap)` — cartesian benchmark sweep
- **Solves:** parameter exploration without writing nested loops.
- **Uses:** `AgentSweep({"model": [...], "prompt": [...], "input": [...]}, runner=AgentMap)` = a benchmark grid in one call.
- **Sketch:** generate the cartesian product of the matrix; flatten into items; hand to `runner`. Trivially supports any `runner` shape (`AgentMap`, `AgentRace`, `AgentEnsemble`).

#### `AgentPipeline(stage_specs, items, depth=2)` — stage-parallel pipeline
- **Solves:** throughput for a fixed-stage pipeline applied to many items.
- **Uses:** stream processing — stage A processes item N+1 while stage B processes item N.
- **Sketch:** N persistent stage workers; items flow through stages with `Send`. Like a CPU pipeline; throughput-optimal when each stage's latency is similar.

### 2.4 Coordination primitives (Erlang/OTP)

Once supervision exists (it does), these become natural.

#### `AgentSupervisor(spec_factory, max_concurrent, restart_strategy)` — pool with restart
- **Solves:** long-running worker pools with fault tolerance.
- **Uses:** a service that processes incoming jobs; a background worker pool that crawls / monitors / indexes; anything that needs to *stay up*.
- **Sketch:** maintain a set of `max_concurrent` workers; watch `child_event errored` from supervision; on error apply `restart_strategy` (`one_for_one` / `one_for_all` / `rest_for_one` — Erlang/OTP semantics).

#### `AgentMutex(resource_spec, accessors=[...])` — serialized access to a shared resource
- **Solves:** shared mutable state without race conditions.
- **Uses:** one agent owns a document; everyone else must `Send` it to read/write; same for a database session, a shared plan, a budget tracker.
- **Sketch:** spawn the resource agent once; its inbox is the queue; it processes requests serially. The "actor model" of mutex.

#### `AgentSemaphore(spec, max_in_flight, items)` — bounded parallelism
- **Solves:** `AgentMap` over too many items; backpressure when items > token budget.
- **Uses:** `AgentMap` over 10,000 items with max 8 in flight.
- **Sketch:** a sliding window of N workers; spawn the next item only after one of the current finishes. Implementation is `AgentMap` with a concurrency cap.

#### `AgentBarrier(specs, body)` — sync point
- **Solves:** staged rollouts where everyone must finish phase 1 before any starts phase 2.
- **Uses:** coordinated multi-agent simulation; coordinated rollback; rendezvous patterns.
- **Sketch:** all workers `Send` a "ready" message to the barrier agent; barrier broadcasts "proceed" once it has N.

### 2.5 Caching & optimization

#### `AgentMemo(spec, key_fn=hash, store=None)` — memoize agent calls
- **Solves:** repeated identical sub-tasks burning tokens.
- **Uses:** cache classification verdicts across runs; cache document summaries; speed up dev/test loops.
- **Sketch:** wrap any agent invocation; hash the (spec, body) tuple as key; lookup in `store`; on miss, run and cache. Free win because LLM calls at temperature 0 are deterministic enough.

#### `AgentBatch(spec, items, batch_size, batch_format)` — coalesce items
- **Solves:** when N items × N spawns > overhead of one spawn with N items inline.
- **Uses:** classify 1000 short strings in batches of 50.
- **Sketch:** chunk items into batches; one spawn per batch; format the batch into the worker's prompt; parse the worker's reply back into per-item results.

#### `AgentSpeculative(spec, body_fn, predicate)` — speculative pre-computation
- **Solves:** latency hiding when you can *predict* what comes next.
- **Uses:** while the user is reading the agent's answer, speculatively spawn the next likely sub-task.
- **Sketch:** speculative spawn; predicate decides if the speculation was on the critical path. If yes, await result. If no, terminate.

### 2.6 Wild / exploratory

#### `AgentEffect(handlers, body)` — algebraic effects
- **Solves:** plug-and-play tool replacement. The body agent says "I need a web search"; an outer handler picks which sibling fulfills it. Mentioned in DESIGN.md §5.9 as v0.2.
- **Uses:** swap a real web-search agent for a cached/replay one in tests; route effect requests to specialized providers.
- **Sketch:** body agent makes effect requests via `Send(to="effects")`; outer combinator's effect-router agent dispatches to the matching handler.

#### `AgentEvolve(spec, seed, mutate_spec, fitness_spec, generations)` — genetic algorithm
- **Solves:** optimization over text where the search space is too large for a single agent to enumerate.
- **Uses:** prompt optimization; constraint satisfaction; design exploration.
- **Sketch:** maintain a population of N candidates; each generation, `AgentMap` mutations, `AgentMap` fitness evals, keep top K, repeat.

#### `AgentSwarm(specs, topology="mesh"|"star"|"ring", rounds)` — graph communication
- **Solves:** when the topology of communication matters (not strict pairwise debate but flexible interaction).
- **Uses:** swarm intelligence simulations; social-dynamics experiments; ant-colony-style optimization.
- **Sketch:** spawn N agents; introduce them in the configured topology; let them message for K rounds; collect.

---

## 3. Priority ranking

Subjective; trade-off is impact ÷ implementation cost. Each entry is small (~30–100 lines) given the current primitives.

### Tier 1 — build these first (high impact, small)
1. **`AgentRace`** — turns the quality-vs-cost decision into a parallelism decision. Killer demo.
2. **`AgentSupervisor`** with restart strategies — turns the harness from "interesting framework" into a fault-tolerant production substrate. Composes with the supervision events we just shipped.
3. **`AgentEnsemble`** — the unlock for best-of-N workflows. Foundation for `AgentQuorum` and `AgentTournament`.
4. **`AgentMemo`** — quiet win. Caching agent calls is free latency + free money.
5. **`AgentDebate`** — the README demo. Makes people *get* the substrate.

### Tier 2 — strong, slightly more design (medium)
6. **`AgentCritic`** — the proper version of `AgentFixedPoint`. Generator + critic loop with a meaningful stop condition.
7. **`AgentHedge`** — niche but elegant; production p99 win.
8. **`AgentMapReduce`** — strict generalization of `AgentMap`; common shape.
9. **`AgentCascade`** — pipelines as first-class. Many real workflows are cascades.
10. **`AgentRetry`** — composes with supervision events; production hygiene.

### Tier 3 — situational (build when needed)
11. **`AgentTournament`** — useful for prompt/model ranking sweeps.
12. **`AgentSweep`** — convenience over the others.
13. **`AgentSemaphore`** — needed when `AgentMap` over thousands of items.
14. **`AgentMutex`** — needed for shared-state workflows.
15. **`AgentBudget`** / **`AgentTimeout`** — production safety; small.

### Tier 4 — exploratory (build to see what happens)
16. **`AgentEffect`** — algebraic effects; opens new composition stories.
17. **`AgentEvolve`** — research toy; might be a workhorse for prompt opt.
18. **`AgentPipeline`** — real value only when stage latencies are similar.
19. **`AgentSwarm`** — research-y; topology matters for some problems.
20. **`AgentBarrier`** — rarely needed in practice.

---

## 4. What the substrate doesn't enable (limits)

Honest accounting of where the FP framing breaks down. From `DESIGN.md §6`:

- **Workers aren't pure.** Same input → different output. So `AgentMemo`'s cache key must be hashed with care; `AgentFixedPoint`'s strict equality is the wrong stop condition.
- **Cost is asymmetric.** `AgentMap` over 10,000 items isn't free the way `map(f, xs)` is. Combinators need `AgentSemaphore` to be production-safe at scale.
- **Stochasticity is sometimes a feature.** `AgentRace` + `AgentEnsemble` lean *into* it; `AgentQuorum` tames it.
- **Identity matters.** Agents have history; functions don't. `AgentMutex` exists because a stateful resource agent makes sense; `AgentEffect`'s handlers do too.
- **No global ordering.** Per-inbox FIFO only. If you need causal ordering across inboxes, you'd need vector clocks (not currently planned).

---

## 5. Implementation notes (shared across all of them)

A few invariants every new HOF should preserve:

1. **Cleanup is local.** Each HOF terminates workers it spawned in a `try/finally` (or by setting `spec.oneshot=True` and trusting the runtime). Never leak addressable agents.
2. **Errors propagate via supervision.** Wrap `Send` + `WaitFor` pairs to also watch for `child_event errored` from the same children. Treat error as just another reply.
3. **Default to capability-passing.** Children get the collector address via `spec.capabilities`, not via global resolution.
4. **Timeouts are paired with partial results.** Every gather call should return what it has on timeout, not just raise.
5. **Composability is the point.** Every HOF should be usable as a worker spec for another HOF. `AgentRace(spec_list=[AgentMap(...), AgentRace(...)], ...)` should just work.

---

## 6. Open questions

- **Streaming results.** Most HOFs gather everything before returning. Some (e.g. `AgentMap` over 100 items) would benefit from streaming partial results back to the caller as they arrive. Needs a callback or stream-friendly return shape.
- **Distributed execution.** Currently the runtime is single-process. For `AgentMap` over thousands of items, you'd want workers on different machines. Out of scope for v0.1, in scope for some future v1.
- **Typed combinators.** `AgentMap[T]` style where the input/output types are checked. Would require a tool-arg type system. Maybe worth doing if Python's type system can carry the load.
- **Aggregator generalization.** `AgentEnsemble` and `AgentMapReduce` differ only in the gather phase. Maybe one combinator with a configurable gather mode (`list` / `synthesize` / `vote` / `pick`).

---

## 7. Cross-references

- `DESIGN.md` — the substrate's philosophy.
- `src/combinator/combinators.py` — Python impl of the existing four FP combinators.
- `src/combinator/tools/combinators.py` — LLM-callable wrappers.
- `src/combinator/system_prompts/claude_agent.md` — the LLM's view of the tool surface.
- Agent self-test report at `examples/.combinator/store/sandboxes/ag-2vwouvcfit6o4/combinator-mcp-test-results.md` — concrete UX feedback that surfaced several of these ideas.
