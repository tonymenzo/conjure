# Conjure — Design

> A multi-agent harness built around recursive spawning, mailbox-based
> communication, and a small set of FP-style combinators. "Agentic
> functional programming."

## 1. Motivation

Multi-agent frameworks tend to fix a topology — group chat, supervisor /
worker, DAG orchestration — and define a vocabulary alongside it. The
result is a framework that's hard to bend to new shapes without rewriting
the substrate.

Conjure takes the opposite move. Two primitives — **recursive agent
spawning** and a **mailbox** — are treated as the substrate. Topology is
*expressible code*, not a baked-in pattern. The vocabulary used to compose
agents is borrowed from functional programming, where the relevant
problems (composition, immutability, expressing control flow as data) have
decades of prior art.

## 2. Core Architecture

Three load-bearing pieces, in priority order:

### 2.1 Recursive spawning (structural)

There is one entry-point agent — the *root* — instantiated by the user.
Every other agent is created by another agent via a tool call:
`spawn(spec) → address`. The spawning agent becomes the parent; the
spawned agent becomes the child. There is no special "top level" — the
same primitive is used at every depth.

What this buys:

- **Composition for free.** Same primitive at every level of the tree.
- **Authority for free.** Caller owns callee. Lifecycle is parental.
- **A natural org chart.** The spawn tree *is* the system's structure.

### 2.2 Mailbox (communication)

Every live agent has an addressable, persistent, threaded inbox.
Messages are async, ordered per-inbox, and journaled. Spawn returns an
address; the parent can send follow-up messages, the child replies at its
own pace.

What this buys:

- **Async by default.** Parents are not blocked while children work.
- **Sibling-to-sibling traffic.** Once a parent introduces two children to
  each other (capability passing — see §3), they communicate directly.
- **An audit log.** Every interaction is a stored envelope. Debuggable,
  replayable, observable by humans.
- **A place to put the user.** The user is just another address in the
  graph.

### 2.3 Persistence-while-alive (lifecycle)

A spawned agent does **not** vanish when it returns a value. It stays
addressable until its parent (or the user) terminates it. While alive, it
can be re-prompted, queried, included in new conversations, or assigned
new tasks.

This is the small but load-bearing deviation from how subagents typically
work in current tool-call frameworks. It's what lets the mailbox do real
structural work instead of being decoration.

```
                user
                  │
                root ───────────── (user can also mail any agent)
                ╱  ╲
         worker_a  worker_b
            ╱           ╲
       leaf_a      (mails worker_a directly via capability passed by root)
```

## 3. Visibility: Capability Passing

Who can talk to whom is its own design choice. The default is **lexical
scope plus capability passing**: an agent only knows the addresses it was
given at spawn time, or that have been explicitly handed to it. A parent
can "introduce" two children by passing one of them the other's address.
Possession of an address is permission.

| Model            | Rule                                                                  | Tradeoff                                                |
|------------------|-----------------------------------------------------------------------|---------------------------------------------------------|
| Lexical scope    | Only parent ↔ child                                                    | Cleanest, most restrictive; cross-tree traffic via ancestor |
| Capability pass  | Lexical, *plus* parent hands a child's address to a sibling            | Composes well; mirrors how function references behave   |
| Open directory   | Global registry; anyone finds anyone                                   | Maximum flexibility; coordination chaos at scale        |

Capability passing is the natural choice for an FP-flavored system because
addresses behave like function references — values that compose, can be
passed in, and grant capability by possession.

## 4. Why Functional Programming?

FP, stripped to essentials, cares about three things:

1. **Composition.** Build big things by combining small things in
   predictable ways.
2. **Immutability.** Values don't change; new versions are produced.
3. **Treating computation as evaluating expressions** rather than as a
   sequence of state mutations.

These map directly to things multi-agent systems struggle with:

1. Composing agents without their interactions becoming emergent in the
   bad sense.
2. Sharing state (docs, plans) without agents stepping on each other.
3. Making delegation reasonable to trace, debug, and reproduce.

So the FP framing is load-bearing, not decorative. The vocabulary gives us
patterns that solve real coordination problems.

## 5. Concrete Mappings

### 5.1 Higher-order agents

**FP:** Functions are first-class values. You can pass a function as an
argument, return one, store one.

**Conjure:** Agent *specs* (their role prompt, capabilities, allowed
tools) are values. A generic supervisor agent takes specs as arguments and
orchestrates them without knowing what they do. A `retry(agent, n)`
wrapper. A `with_critic(agent, critic)` wrapper.

**Why it matters:** Common coordination patterns become library functions
over agents, not bespoke orchestrator code.

### 5.2 `map`, `fold`, `filter`

**FP:** The three workhorses of bulk transformation. `map(f, xs)` applies
`f` to each element. `fold(f, init, xs)` threads state sequentially.
`filter(pred, xs)` keeps elements that pass a test.

**Conjure:**

- `agent_map(spec, items)` — spawn N children in parallel; each handles
  one item; gather results in order.
- `agent_fold(spec, items, init)` — sequential thread of state through
  children, each transforming it.
- `agent_filter(spec, items)` — classification swarm.

**Why it matters:** Most "swarm" or "team" patterns are one of these three
with a costume on. Naming them keeps the design honest.

### 5.3 Closures as spawn contexts

**FP:** A closure is a function bundled with a snapshot of its enclosing
environment — captured variables, references, partial state.

**Conjure:** A spawn prompt is exactly this — the task description plus
the context, addresses, and capabilities the child carries. Thinking of
spawning as "constructing a closure" sharpens the question to: *what is
the minimum environment this child needs to carry?*

**Why it matters:** Forces explicit thinking about each child's needs.
Smaller, sharper spawn prompts.

### 5.4 Continuation-passing style (CPS)

**FP:** Instead of *returning* a value, a function takes a "where to send
the result" parameter — a continuation — and calls it.

**Conjure:** This is literally the mailbox. "Don't return to me — send
your result to this address." Async messaging is CPS with extra steps.
The FP world worked out the patterns and pitfalls (avoiding callback
hell, composing continuations) decades ago.

**Why it matters:** Async agent flows are not a novel problem; they're a
known one with known shapes.

### 5.5 Tail-call handoff

**FP:** When a function's last action is to call another function, the
runtime can drop the current stack frame — no work remains on the way
back. Tail-call optimization.

**Conjure:** If a child's reply doesn't need to come back to its
parent (the parent would only forward it), the parent tells the child to
reply *directly* to the original requester. Saves a hop, shortens chains,
reduces parent context pressure.

**Why it matters:** Real savings in tokens, latency, and context window.

### 5.6 Immutable shared state

**FP:** State doesn't mutate; each "change" produces a new value.
Persistent data structures (and version control like git) make cheap
history.

**Conjure:** When agents share a document or plan, every edit produces
a new version. No agent can clobber another's work. You can branch ("two
agents explore alternative drafts"), merge, fork, and roll back.

**Why it matters:** Shared mutable state is the single most common source
of multi-agent chaos. Immutability removes a category of failure.

### 5.7 Lazy evaluation

**FP:** Don't compute a value until something demands it.

**Conjure:** Spawn an agent but don't actually start it until someone
reads from its inbox. Lets you set up hypothetical branches cheaply: "if
the review fails, this fixer will activate" — declared up front, paid for
on demand.

**Why it matters:** Cheap to express contingencies and what-ifs without
committing to running them all.

### 5.8 Fixed-point iteration

**FP:** "Apply `f` to its own output until the output stops changing."
A principled stopping rule, not a hand-tuned bound. Closely related to the
Y combinator (anonymous recursion).

**Conjure:** "Run the editor agent on its own output until it makes no
further changes." Iterative refinement loops with a structural termination
condition.

**Why it matters:** Loops gain a principled stopping rule.

### 5.9 Algebraic effects (footnote)

A newer FP idea: separate *requesting* an effect (in the function) from
*handling* it (decided by an outer scope). A child agent can request "I
need a web search" without knowing which sibling, sub-agent, or tool will
fulfill it; an outer handler decides at runtime.

Composes cleanly with capability passing, but not in v0.1.

## 6. Where the FP Analogy Breaks Down

The framing is load-bearing, but not total. Honest limits:

- **Agents are not pure.** A function with the same input gives the same
  output; an LLM call doesn't. Determinism, idempotence, and equational
  reasoning all weaken.
- **Identity matters in ways it doesn't for functions.** A function has
  no history. A persistent agent does. Persisting agents trades some of
  FP's nicest properties for memory and continuity.
- **Cost is real and asymmetric.** Spawning a child costs tokens,
  latency, and context budget. `agent_map(spec, 10_000_items)` is not
  free the way `map(f, xs)` is.
- **Observability is its own problem.** Functions either return or
  raise; agents can drift, plateau, hallucinate, or get stuck in subtle
  ways. The FP toolbox doesn't address this.
- **Stochasticity is sometimes a feature.** Pretending agents are
  functions can mask cases where variation is the goal.

These don't invalidate the framing; they shape where to lean on it
(composition, state, control flow) and where to bring in other tools
(observability, evaluation, cost accounting).

## 7. Minimal Substrate

The smallest viable build is three primitives plus an inbox service:

1. **Inbox service.** Per-agent persistent message store, threaded, with
   `send(to, msg)` and `recv(...)` operations.
2. **Spawn tool.** `spawn(spec) → address`. Spec includes role prompt,
   handed-in addresses, and termination conditions.
3. **Terminate tool.** `terminate(address)`. Parent-or-user authority
   only.
4. **A root agent** wired up with those three (and any other domain
   tools) and the ability to message any address it knows.
5. **A human view** into any agent's inbox — the user is a peer in the
   graph.

That's the substrate. The library of patterns from §5 (`agent_map`,
`agent_fold`, retry wrappers, fixed-point loops) is built on top, as
ordinary code using these primitives. No new vocabulary required.

## 8. Open Questions

- **Inbox semantics:** ordered per-sender? Globally ordered?
  Acknowledgements? Read receipts?
- **Capability revocation:** can a parent un-introduce two siblings
  without terminating either?
- **Termination cascade:** terminating an agent cascades to its
  children? (v0.1 default: yes — supervisor-tree style.)
- **Persistence across sessions:** do agents survive process restart? If
  yes, how is their context restored?
- **Cost ceilings:** how does a parent cap the resources a subtree
  consumes?
- **Failure handling:** when an agent crashes or hangs, who notices, and
  what do they do?

None of these block the v0.1 substrate, but each will demand an answer
once the system is in use.

## 9. Example Tasks the Framework Targets

Grouped by the FP pattern they exercise:

**Map-heavy (embarrassingly parallel)**
- **Benchmarking sweep** — `agent_map(bench_spec, cartesian(models,
  frameworks))`, sandbox per child. The canonical first integration test.
- **Multi-version dependency audit** — apply a patch across N library
  versions in parallel; report which break.
- **A/B prompt optimization** — N prompt variants × evaluation set.
  `agent_map` then `agent_filter`.
- **Bulk translation / format conversion / data migration** — trivial
  map.

**Recursive decomposition (tree of work)**
- **Research / literature sweep** — top-level researcher splits a
  question into sub-questions, spawns one researcher per branch, each may
  recurse. `agent_fold` over leaves to synthesize.
- **Hierarchical math or coding problems** — decomposer spawns sub-
  solvers; sub-solvers may decompose further. Tree bottoms out when
  leaves are tractable.

**Fan-out + fan-in (multiple perspectives on one artifact)**
- **Code review pipeline** — security / performance / style /
  correctness reviewers examine the same diff; aggregator merges. Capability
  passing earns its keep when reviewers talk directly.
- **Red-team / blue-team analysis** — two persistent agents with
  opposing roles debate. The mailbox handles the back-and-forth naturally.

**Fixed-point iteration**
- **Generator + critic loop** — writer produces, critic critiques,
  writer revises, until critic passes. Principled termination.
- **Edit / type-check / re-edit** — refactor agent edits, runs tests,
  reads failures, edits again, until clean.

**Single-agent (no spawning)**
- **Triage agent** — most days, the root agent just does the work. Spawn
  only when a specialist is genuinely warranted. Demonstrates that the
  framework doesn't *force* multi-agent.
- **Long-running async worker** — one agent works for hours or days;
  the user checks its inbox when convenient.

## 10. Where the Framework Doesn't Map Cleanly

The framework is not for every workload. Cases it fits poorly:

- **Tight real-time loops** (game AI, control systems, anything with
  millisecond budgets). The mailbox is async by construction; spawn and
  IPC are not free. Use a different substrate.
- **Strictly sequential, single-thread drafting tasks** with no fan-out.
  A single long-context LLM call is the right shape; spawning a child
  agent just adds an unnecessary IPC hop.
- **Adversarial or non-cooperative settings.** Capability passing
  assumes participants are cooperating on a goal. For settings where
  agents might lie about addresses, leak capabilities, or refuse to
  cooperate, you want a stronger protocol (signed messages, brokers,
  reputation) than this framework provides.
- **Tasks where the model's own context window is the right coordination
  medium.** Sometimes the cleanest "multi-agent" pattern is one agent
  with a well-structured prompt — no spawn needed. Conjure does not
  preclude this (the root agent can solve things alone), but if every
  task fits this shape, the substrate is overkill.
- **Workloads with strict ordering or transactional guarantees** across
  agents. v0.1 guarantees per-inbox FIFO, no global order. If you need
  causally-ordered or transactional messaging, that's a different
  framework.

These boundaries should be tested rather than assumed — the line between
"good fit" and "wrong tool" is empirical. Use the toy programs in the
test suite to develop a sense for which workloads the substrate actually
serves.
