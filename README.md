# conjure

**Agentic functional programming.** Conjure is a multi-agent runtime built
on three primitives — **recursive agent spawning**, **addressable
mailboxes**, and **capability passing** — with a library of FP-style
combinators (`agent_map`, `agent_fold`, `agent_filter`, `agent_race`,
`agent_ensemble`, `agent_critic`, `agent_supervisor`) built on top.
Agents spawn agents; topology is ordinary code, not a framework choice.

Most multi-agent frameworks bake in a topology — group chat,
supervisor/worker, DAG — and you fight the framework when your problem has
a different shape. Conjure inverts that: the primitives are the substrate,
and topology is ordinary code. The composition vocabulary is borrowed from
functional programming, where these problems have decades of prior art.

See [`DESIGN.md`](DESIGN.md) for the design philosophy and architecture.

## The three primitives

1. **Recursive spawning.** One root agent; every other agent is created by
   another agent via `spawn(spec) → address`. The spawn tree *is* the
   system's structure: callers own callees, lifecycle is parental,
   termination cascades.
2. **Addressable mailboxes.** Every live agent has a persistent, threaded,
   journaled inbox. Messages are async; parents are never blocked while
   children work. The user is just another address in the graph.
3. **Capability passing.** An agent can only message addresses it was
   given at spawn time or explicitly handed later. Possession of an
   address is permission — addresses compose like function references.

The load-bearing deviation from subagent-style frameworks: a spawned agent
does **not** vanish when it returns a value. It stays addressable — it can
be re-prompted, queried, and introduced to siblings — until its parent (or
the user) terminates it.

## Install

```bash
pip install conjure-agents                 # core library + headless CLI (repl)
pip install "conjure-agents[ui]"           # + the tmux/textual cockpit (conjure run)
pip install "conjure-agents[claude_agent]" # + claude-agent-sdk engine
pip install "conjure-agents[toolbase]"     # + agent-curated toolbase toolkits (Python ≥ 3.12)
```

The distribution is named `conjure-agents`; the import package and CLI are
plain `conjure`.

Requires Python ≥ 3.10 on POSIX (the daemon forks; Windows is unsupported).

## Quickstart — CLI

Point `conjure` at a YAML config describing the root agent:

```yaml
# agent.yaml
llms:
  default:
    provider: anthropic           # or openai / google / groq / ollama
    model: claude-haiku-4-5

root:
  role_prompt: |
    You are the root orchestrator. Spawn children when a specialist is
    warranted; otherwise just do the work.
  engine: orchestral
  llm: default
  tools: [primitive, combinator]
```

```bash
conjure config set ANTHROPIC_API_KEY sk-...   # stored in ~/.config/conjure/.env
conjure check agent.yaml                      # validate config + keys, no LLM calls
conjure repl agent.yaml                       # single-pane REPL, no tmux needed
conjure run agent.yaml                        # tmux cockpit (requires the [ui] extra)
```

Or skip tmux and use the browser (no extra dependencies):

```bash
conjure web agent.yaml                        # opens http://127.0.0.1:8765/?token=…
conjure web agent.yaml --detach               # as a daemon; `conjure web --attach` reprints the URL
conjure web --replay <session>                # read-only replay of a journaled session
conjure run agent.yaml --web                  # tmux cockpit and web UI on one session
```

The web UI has the cockpit's layout: an agent tree, an activity feed, and
streaming chat, with permission requests you can approve. It adds a few
views tmux can't show:

- side-by-side chats;
- a **graph** of spawn, message and introduction edges;
- a **timeline** of each agent's turns, tool calls and incoming messages;
- a sandbox **file browser**.

`--replay` reopens any journaled session with a time scrubber, so you can
watch it unfold again. The server binds to loopback, and every API call
needs the session token printed in the URL. On a remote host, use
`ssh -L 8765:localhost:8765`.

In tmux mode the session is daemonized: window 0 is your prompt to the
root agent, every spawned agent gets its own live window, and `Ctrl+B M`
opens a meta-view popup (spawn tree + inboxes + cost). Detach with
`Ctrl+B d`, reattach with `conjure run --attach`, stop with `conjure quit`.

## Quickstart — library

The runtime is a plain Python object; engines are pluggable. This example
uses the deterministic `ScriptedEngine` (no API key needed) to show the
mechanics — swap in the `orchestral` or `claude_agent` engine factories
for LLM-backed agents (see `examples/`):

```python
from conjure import AgentSpec, BehaviorRegistry, Runtime, agent_map
from conjure.tools.primitives import send_impl

def squarer(engine, prompt, envelopes):
    for env in envelopes:
        body = env.body
        if isinstance(body, dict) and "item" in body:
            send_impl(token=engine.token, to=body["reply_to"], body=body["item"] ** 2)
    return "ok"

reg = BehaviorRegistry()
reg.register("idle", lambda *a, **kw: "idle")
reg.register("squarer", squarer)

rt = Runtime(engine_factory=reg.factory())
root = rt.root(AgentSpec(role_prompt="idle"))

squares = agent_map(
    rt, root,
    lambda _item: AgentSpec(role_prompt="squarer"),
    [1, 2, 3, 4, 5],
    timeout_s=5.0,
)
assert squares == [1, 4, 9, 16, 25]   # workers spawned, gathered, terminated
rt.shutdown()
```

## Combinators

All of these are ordinary code over the three primitives — and LLM-callable
tools, so agents can invoke them on their own subtrees:

| Combinator          | Shape                                                        |
|---------------------|--------------------------------------------------------------|
| `agent_map`         | Fan out one worker per item in parallel; gather in order     |
| `agent_fold`        | Thread an accumulator sequentially through workers           |
| `agent_filter`      | Classification swarm; keep items that pass                   |
| `agent_fixed_point` | Iterate an agent on its own output until it stabilizes       |
| `agent_race`        | Speculative execution: first reply wins, losers terminated   |
| `agent_ensemble`    | Best-of-N: gather all replies, synthesize via an aggregator  |
| `agent_critic`      | Generator/critic refinement loop with principled termination |

## Engines

- **`orchestral`** — wraps `orchestral.Agent`; multi-provider out of the
  box (Anthropic, OpenAI, Google, Groq, Ollama).
- **`claude_agent`** (`pip install "conjure[claude_agent]"`) — agents run
  as `claude-agent-sdk` sessions with Claude Code's full tool surface
  (Bash, Edit, Grep, …), per-agent sandbox directories, and permission
  routing.
- **`codex`** — agents run as Codex CLI threads (`codex exec --json`,
  resumed turn to turn) on your ChatGPT subscription, with Codex's own
  sandbox for shell and edits. conjure's tools and granted toolkits
  reach them over a loopback MCP server. Children left on `auto` stay
  on Codex. See `examples/codex_agent.yaml`.
- API models behind a **model access gateway** or any OpenAI-compatible
  endpoint: an `openai_compatible` LLM with `base_url`, optionally
  `discover: true` to list every model the endpoint serves. See
  `examples/gateway_agent.yaml`.

Agents see every model they may spawn on through `ModelCatalog` (API,
gateway, Claude and Codex subscription models, with notes), and pick
one with `Spawn(model="<id>")`; the id picks the engine. A `models:`
section sets the policy (`allow` globs, `default`, `notes`).

- **`ScriptedEngine`** — deterministic behaviors for tests and offline
  development.

## Toolkits (toolbase)

With [toolbase](https://github.com/alexr314/toolbase) installed, agents
curate domain toolkits for the agents they spawn. Toolkits are
capabilities too: a child's toolkits must be covered by its parent's,
the same way addresses and budgets work.

```yaml
toolbase:
  authority: [heptapod]   # what the root may grant; default: your active toolbase loadout
root:
  role_prompt: ...
  toolkits: [heptapod/units]   # the root's own tools (optional)
```

- `ToolCatalog()` lists what the caller holds, so it can grant from it.
- `Spawn(..., toolkits=["heptapod/llp", "calculator__add"])` serves the
  child that slice. The grammar is `tb activate`'s: `toolkit`,
  `toolkit/bundle`, or `toolkit__tool`. `Call` and the combinator
  `spec`s take the same field.
- `GrantToolkits(child, toolkits)` widens a live descendant's toolset.
  The child picks it up at its next turn, and its conversation is kept.
- `RequestToolkits(toolkits, reason)` lets a child ask its parent for
  tools. The parent grants them, escalates to its own parent, or
  declines.

Each agent's toolkits are served in-process from toolbase's cache.
Nothing is written to your loadouts. File-aware tools are scoped to
the agent's sandbox. The servers start on the agent's first turn and
stop when it terminates. Version pins and `serve.yaml` blocklists come
from the project's active loadout. See
[`examples/toolbase_agent.yaml`](examples/toolbase_agent.yaml).

## Persistence & replay

Every spawn, send, and terminate is journaled to an append-only JSONL log
per session. `Runtime.replay(session_dir)` reconstructs the spawn tree and
all inbox contents for post-hoc inspection — every interaction in a
session is auditable.

## Limitations (v0.1)

Honest boundaries, documented rather than discovered:

- Single-process runtime; one OS thread per live agent.
- Termination does not interrupt an in-flight LLM call; it takes effect
  after the current step.
- Per-inbox FIFO ordering only — no global message order.
- Cost is tracked but not yet enforced as a ceiling.
- `agent_fixed_point` uses strict output equality, which stochastic LLMs
  rarely satisfy — prefer `agent_critic` for refinement loops.

See [`DESIGN.md`](DESIGN.md) §6 and §10 for where the FP framing breaks
down and which workloads the substrate fits poorly.

## Development

```bash
git clone <repo-url> && cd conjure
pip install -e ".[test,ui]"
pytest -q
```

## License

[AGPL-3.0-only](LICENSE). You can use, modify, and redistribute conjure
freely — but any distributed or network-served derivative must publish its
source under the same terms. For commercial licensing outside the AGPL,
contact the author.
