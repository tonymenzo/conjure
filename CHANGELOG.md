# Changelog

All notable changes to conjure are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/) (pre-1.0: a minor
version may change behavior). Each GitHub release carries its section of
this file as its notes.

## [Unreleased]

### Added

- **Gateway-friendly LLM calls.** Each `llms:` entry takes
  `max_concurrency`: the most requests in flight at once across every
  agent on it. Set it under a gateway's parallel-request limit, e.g. 16
  for a limit of 20. A slot is held only while a request or stream is
  in flight, so a parent waiting on its children can't starve them.
- **Retries of transient failures** (dropped connections, broken
  streams, timeouts, 429, 5xx), with backoff, up to `max_retries`
  (default 5) per entry. A 429 waits for the gateway's stated reset
  time. A stream that breaks midway is re-requested; the conversation
  is untouched, since a reply is only recorded once its stream
  completes. Each retry shows in the chat, and a terminated agent stops
  retrying.

### Changed

- An agent's engine error prints one line on the console. The full
  traceback goes to the session's `conjure.log`.
- `openai_compatible` clients leave retries to conjure (one visible
  layer), instead of the OpenAI SDK's silent ones.

### Fixed

- **`Recv` and `WaitFor` could hang an agent.**
  - `Recv()` with no arguments always returned the inbox's first
    message, often the agent's own original task, so an agent waiting
    for results looped on it.
  - `WaitFor()` waited out its whole timeout even after the reply had
    arrived, and returned old messages along with new ones.
  - Both now return only messages the agent hasn't seen, and `WaitFor`
    returns as soon as one arrives (`max_n=N` still waits for N, for
    fan-in). Sender filters accept a child's label, `parent` or
    `caller`, and an unknown name fails at once instead of waiting
    out the timeout. Passing `since_seq` still pages through the whole
    inbox.

### Removed

- The design document is no longer part of the repository.

## [0.2.0] — 2026-10-15

A browser UI, a Codex engine, agent-curated toolbase toolkits, a model
catalog with gateway support, and auditable run records.

### Added

- **Web UI.** `conjure web <config>` serves a session in the browser,
  with no tmux needed. `conjure run --web` adds it next to the tmux
  cockpit, and `conjure web --replay <session>` reopens any journaled
  session read-only, with a time scrubber.
  - Side-by-side agent chats (split and close, tmux-style), an agent
    tree with live status, an activity feed, and the permission panel.
    Approvals for agents whose chat isn't open appear as toasts.
  - Graph, timeline and file-browser views, an agent inspector, and a
    model chip on every agent.
  - Markdown tables and LaTeX in chat (vendored KaTeX, works offline).
  - A run-record panel (`r`) and a keyboard-shortcuts dialog (`?`).
    Plus a light theme, a narrow layout, and deep links.
  - Every API call needs the session token, sent in a header. The
    server binds to loopback, and the URL file is readable only by you.
    Reach a remote session through an SSH tunnel.
- **Codex engine** (`engine: codex`). Agents run on the Codex CLI under
  your ChatGPT subscription. Each turn is a `codex exec --json` run that
  resumes the agent's thread. Each agent has its own `CODEX_HOME` with
  a link to your login and nothing else of `~/.codex`. Children left on
  `auto` stay on Codex. Configure it under `codex:`, and see
  `examples/codex_agent.yaml`.
- **Agent-curated toolbase toolkits.** Agents grant toolkit slices to
  the agents they spawn (`toolkits` on `Spawn`, `Call` and combinator
  specs, in `tb activate` grammar). Grants attenuate like addresses and
  budgets: a child's toolkits must be covered by its parent's.
  - Tools: `ToolCatalog`, `GrantToolkits` (live, additive) and
    `RequestToolkits`.
  - Servers run in-process through toolbase's orchestrator, scoped to
    the agent's sandbox. Each toolkit's config is resolved against the
    session's project.
  - Configure under `toolbase:` (`authority`, `project_root`,
    `loadout`, `config`, `prestart`), plus `root.toolkits`.
- **Toolkit skills travel with grants.** Claude and Codex agents get them
  as native skills. Orchestral agents read them with `LoadSkill`.
- **Model catalog.** `ModelCatalog` lists every model an agent may
  spawn on:
  - API LLMs from your config.
  - Every model an OpenAI-compatible gateway serves.
  - Claude subscription aliases, with the exact model each one
    resolves to.
  - Codex models.

  `Spawn(model="<id>")` picks the engine for you. A `models:` policy
  (`allow`, `default`, `notes`) bounds and guides the choice.
- **OpenAI-compatible provider** (`provider: openai_compatible`) for
  model access gateways (MAG), vLLM, LiteLLM and institutional proxies.
  Set `base_url`, plus `api_key_env`, `headers` and `discover: true`.
  See `examples/gateway_agent.yaml`.
- **Run records.** Every session writes `manifest.json` at start: the
  frozen config, software and toolkit versions, and the start time. It
  also journals what each agent actually ran on: engine, exact model,
  system prompt and cost.
- **`conjure export <session>`** writes a portable, read-only record.
  - Contents: per-agent rows (`agents.jsonl`), totals, the journal, the
    event logs, and the agents' working files.
  - Paths are scrubbed, and credentials are never included.
  - `--archive` also writes a `.tar.gz`, which
    `conjure web --replay` opens.
- **Inbox notices.** Any conjure tool result can carry
  `"inbox": {"unread": N, ...}` when messages are waiting. A parent busy
  in a long turn sees its workers' results without waiting for the turn
  to end.
- `Runtime.replay(until=…)` rebuilds a session as it stood at an instant.

### Changed

- **The root's toolkit authority defaults to your active toolbase
  loadout** (what you've `tb activate`-d), not every installed toolkit.
  `toolbase.authority: all` restores the old behavior, and a toolset
  such as `[heptapod]` pins it exactly.
- Each agent's prompt names its toolkit tools exactly as its engine
  shows them, and tells it to use them rather than the underlying
  programs. `ToolCatalog` separates what you can call from what you can
  grant. The root may grant itself toolkits
  (`GrantToolkits(child="self")`).
- A child's toolkit servers start at spawn, in the background
  (`toolbase.prestart: false` turns this off).
- The `toolbase_profile` spec field is now `toolbase_loadout`. The old
  key is still accepted.
- The claude_agent engine names the exact model before the first reply,
  from the CLI's model table.
- Prompts tell agents to wait for workers through their inbox rather
  than by reading worker files, and explain how steering reaches a busy
  worker.

### Fixed

- The toolbase hook launched `toolbase serve --profile`, which toolbase
  0.12+ rejects.
- Workers that had already delivered their reply were interrupted
  mid-`Send` during `Call` / `AgentMap` cleanup. The Claude CLI
  reported this to the model as a user refusal.
- Messages an agent had already read with `Recv` were pushed to it again
  on its next turn.
- The daemon's tool registry was missing `agent_race`, `agent_ensemble`,
  `agent_critic` and `agent_supervisor`.
- The claude_agent engine registered its tool server under a name that
  didn't match its pre-approval list.
- `conjure check` ignored an LLM's `api_key_env`, and required API keys
  that a Claude or Codex root never uses.

## [0.1.2] — 2026-06-10

### Added

- **`agent_supervisor`**: supervised fan-out. It's `agent_map` with
  one-for-one restarts. Errored workers are respawned on the same item
  up to `max_restarts` times. Items that exhaust the budget come back in
  `failed` instead of sinking the whole fan-out.
- **Hierarchical cost ceilings.** `AgentSpec.budget` (USD) caps an agent
  plus its whole subtree. Budgets attenuate like capabilities.
  - Spawning under an exhausted subtree raises `BudgetExceeded` (tool
    code `budget_exceeded`).
  - Drivers skip further steps, and the parent gets a one-time
    `budget_exceeded` event.

### Changed

- **Terminate interrupts in-flight steps.** `Runtime.terminate` calls
  `engine.interrupt()`, so killing a claude_agent aborts its in-flight
  LLM call instead of draining it.
- The control-plane snapshot behind the 2 Hz UI tick takes one registry
  lock per walk, not one per node. Idle agents now cost a `stat()`, not
  a log read and parse.
- Hero banner removed pending final art.

### Fixed

- Engine errors on already-terminated agents no longer emit spurious
  `errored` supervision events.

## [0.1.1] — 2026-06-09

### Fixed

- The README hero image uses an absolute URL, so the PyPI project page
  renders it.

### Added

- Project URLs (repository, changelog) in the package metadata.

## [0.1.0] — 2026-05-20

First release: the v0.1 substrate. It provides recursive spawn, addressable mailboxes and capability
passing, with FP-style combinators on top.

### Added

- **Value types:**
  - `Address`, `Envelope` and `CapabilitySet`.
  - A thread-safe `Mailbox` with per-inbox sequencing and one `read`
    primitive.
- **Runtime:** the spawn tree, agent registry and capability sets, plus
  an append-only JSONL journal that replays the spawn tree and inbox
  contents.
- **Driver, Agent and Engine:** a per-agent thread loop around a
  pluggable `Engine`. Production engines wrap `orchestral.Agent`, and
  tests use `ScriptedEngine`.
- **Primitive tools:** `spawn`, `send`, `recv`, `wait_for`,
  `terminate`, `introduce` and `list_inbox`. Capabilities are enforced
  in the tool layer.
- **Combinator tools:**
  - The FP core: `agent_map`, `agent_fold`, `agent_filter` and
    `agent_fixed_point`.
  - Patterns on top: `agent_race`, `agent_ensemble` and
    `agent_critic`.
- **Toolbase hook:** `AgentSpec.toolbase_profile` serves a toolbase
  profile to a claude_agent child.
- **Packaging:**
  - AGPL-3.0-only, typed (`py.typed`).
  - `textual` and `libtmux` are in the `[ui]` extra, and `mcp` is
    declared directly.
- **Tests:** 121 offline tests, plus toy programs covering recursion,
  map, tree decomposition, shared state, fixed points, capability
  violations, termination cascades and replay.

### Fixed

- `Spawn`'s optional fields were marked required by orchestral's schema
  generator.

[Unreleased]: https://github.com/tonymenzo/conjure/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/tonymenzo/conjure/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/tonymenzo/conjure/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/tonymenzo/conjure/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tonymenzo/conjure/releases/tag/v0.1.0
