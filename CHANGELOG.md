# Changelog

All notable changes to conjure are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/) (pre-1.0: a minor
version may change behavior). Each GitHub release carries its section of
this file as its notes.

## [Unreleased]

### Added

- **Tool results name their call.** Every engine's `stream_end` tool
  calls carry the call's `id`, and each `tool` event the `tool_call_id`
  it answers, so a result pairs with its call without relying on order.
- **Token counts with the cache.** Each agent's `agent_meta` journals
  `token_usage`: `input` (every input token, cached ones included),
  `output`, `cache_read` and `cache_creation`, the same for every provider
  (`conjure.provenance.token_usage`). The Claude and Codex engines now
  count tokens too, from each turn's totals.
- **How an agent was paid for.** `agent_meta` journals `access`, `api` or
  `subscription`, and, for the Claude engine, `api_equivalent_cost_usd`:
  what the tokens cost at API prices even when a subscription paid.

## [0.2.13] — 2026-10-19

A replay plays back smoothly, at any speed, from a proper player.

### Added

- **Replay plays back smoothly.** The bar under a replay is now a player.
  It plays at any speed from ¼× to 2000× (a slider, presets, or `<` `>`).
  **Skip idle** hurries through stretches where nothing happens, such as
  model calls and long tools, and the track lights up the stretches
  where something does. `←` `→` step from event to event, and `Home`
  `End` jump to either end. While it plays, each chat reads its log
  ahead of the playhead and shows replies streaming in, scrolling to
  follow unless you scroll up. The tree, activity feed, graph and
  timeline keep pace, and an agent mid-turn shows as running. Reaching
  the end leaves the read-only view exactly as it was before playing.

### Fixed

- **Replays answer quickly.** Every moment of a replay shares one parse
  of the logs, and paths recorded on another machine are looked up
  once. Each lookup used to take about a quarter of a second, so
  scrubbing a run copied from a cluster stuttered.
- The agent tree updates when only an agent's token count changes, as
  on a model with no known price.

## [0.2.12] — 2026-10-18

Messages between agents, toolkit calls and the timeline read clearly, and
the run record's tokens agree with the agent tree.

### Changed

- **Messages read as messages.** In the activity feed and the chat, a
  message between agents sent as JSON shows as labelled fields under
  its kind (`handoff`, `correction_request`), with paths relative to the
  team's workspace (a file path opens in the files view) and long
  hashes shortened. `Send` calls preview and show their message the
  same way, and tool calls name agents instead of their addresses.
- **Toolkit calls preview their arguments.** Under a collapsed call, the
  preview lists the arguments its line had no room for (`initial t ·
  final W+, b`): required ones first, and none that only repeat the
  tool's default. Engines report each tool's argument order, required
  arguments and defaults, journaled with the agent's provenance so a
  replay previews the same way (runs from before this release list the
  arguments as passed). A result that reports `valid`, `success`,
  `passed` or `converged` false, or an error status, shows as failed.
- **The activity feed** has one line per event, plus a summary line for a
  message; click a message to read it in place. Filters show or hide
  messages, tool calls and lifecycle events.
- **The timeline** draws the agent tree in its lanes, turns as bars with
  tool calls as a strip inside them (failures in red), and messages as
  arrows between lanes. It sits in a titled pane with zoom controls,
  and its rows grow to fill the pane for a small team.
- **The replay scrubber** marks where messages, errors and failed tool
  calls happened.
- Agents that share a name are numbered (`specialist #1`, `#2`) wherever
  they are named.
- The agent tree no longer prints "default model" on every agent whose
  model isn't known, and the chat and graph say so when they're empty.

### Fixed

- **The run record's tokens match the agent tree's.** Its tokens were
  output only, so an agent that read 2.3M tokens and wrote 6.7k showed
  6.7k beside the tree's 2.3M. It now shows tokens in and out (as the
  tree counts them) with an output column, and marks tokens on a model
  with no known price as "unpriced" instead of $0. A live run's record
  includes its latest spawns and messages.

## [0.2.11] — 2026-10-18

The files tab browses every workspace in the run and previews what the
agents wrote, and the graph shows the whole team.

### Added

- **A files view worth opening.** The web UI's files tab shows every
  workspace in the run: the root's, shared by the agents that work in
  it, with any nested workspace (an agent given `sandbox_dir`) under it,
  each labelled with its agents.
  - A tree that opens folders in place, with keyboard navigation
    (arrows, Enter, `/` to search), search across the workspace, the
    recently modified files, and dotfiles on request
    (`.conjure/tool-output` holds full tool outputs).
  - Previews: code highlighted with line numbers; markdown rendered,
    its relative images shown and its links opening the files they
    name; CSV and TSV as tables; Jupyter notebooks as cells with their
    outputs; PNG, JPEG, GIF, WebP and SVG images; PDFs in the browser's
    viewer. Copy a path or download any file.
  - Live runs refresh the tree and the open file as agents write;
    `#files/<agent>/<path>` links to a file.
  - Read-only: listing never creates a workspace, and no path (through
    `..` or a symlink) leaves one. Images and PDFs load as blobs, so
    no file an agent wrote is opened as a page.

### Changed

- **The graph shows the whole team.** It fits the team to the pane on
  load and pans and zooms (scroll or pinch, drag, `f` to fit, `+` `−`
  `0`), with a minimap once the team is larger than the view.
  - An org-chart layout: a parent's leaf workers stack in a column
    under it, so a wide team stays narrow; top-down or left-to-right,
    chosen to fit the pane unless set.
  - Messages between a parent and a child ride on the connector between
    them with a count each way (`↓2 ↑5`); messages across branches and
    introductions are arcs above the row.
  - Agent cards show status, model, cost and toolkits; subtrees
    collapse (`c`, or the fold under a card); hovering an agent lights
    its connections; `/` finds an agent and brings it into view.
  - The agent's details slide over the graph's right edge when you click
    it (`Esc` closes), instead of taking width from the graph.

### Removed

- The web server's `/api/sandbox` and `/api/sandbox_recent` routes, which
  created a missing sandbox when asked to list it. The files view uses
  the read-only `/api/files/*` routes.

## [0.2.10] — 2026-10-18

Collapsed tool calls preview what they do, and a parent waiting on
workers that have stopped hears so at once.

### Changed

- **A glance at each tool call.** A collapsed call shows a line or two of
  what it does under its summary: a written file's first lines, an
  edit's first removed and added line, a Send's or Spawn's message, a
  tool's code argument (`code`, `proc_card`). Opening the call replaces
  the preview with the full blocks. Shell, Read and search calls keep
  their one line, which already says it.
- `Spawn` calls name the child and its model; the result chip is the
  child's address.

### Fixed

- **A wait on stopped agents ends.** `WaitFor` returns as soon as the
  agents it depends on (the named sender, or all of the caller's children
  and their workers) have ended their turns with nothing unread, with
  `child_idle` naming them, instead of sitting out the timeout for a
  reply that will not come. Each such state is reported once.
- **A refused toolkit grant says what can be granted.** Asking to give a
  child tools beyond your own grant names the refused tools (the first
  eight), the bundles you hold in full, and how to grant those instead.

## [0.2.9] — 2026-10-17

Agents' code reads as code in the web UI, delegated workers share their
parent's sandbox, and agents' shells no longer see credentials.

### Added

- **Code in the chat.** A tool call is one line with a status chip
  (`exit 0 · 4 lines`, `✓`, a red `exit 1`); opening it shows:
  - `Bash` / shell: the command highlighted as bash, with heredoc bodies
    in their own language (`python3 - <<'EOF'` as Python);
  - `Write`: the file, highlighted by extension, with line numbers;
  - `Edit`, `MultiEdit` and Codex file changes: a red/green diff;
  - the command's output as a terminal block with an exit badge,
    stderr in red, and a capped output's truncation notice set apart.

  Other tools show labelled fields; results that are dicts show their
  key fields on the line. Code fences in replies, the files view and
  permission prompts are highlighted too, with copy buttons and long
  blocks collapsed. Highlighting is highlight.js 11.9.0, vendored
  (BSD-3) so it works offline, coloured from the theme in light and
  dark.
- **Relative paths.** Paths and commands in tool calls show relative to
  the agent's sandbox (`cd work/ufo_sm`, not
  `cd /home/.../run/workspace/work/ufo_sm`), including in replays of
  runs recorded on another machine. File contents, copied text and
  permission prompts keep the exact paths; the full path is on hover.
  The agent detail API lists the sandbox's paths (`sandbox_paths`).
- **TUI previews.** The terminal chat shows a command, the first lines
  of a written file, or an edit's diff under each call, highlighted,
  and a command result's exit status and last lines.

### Changed

- **A child spawned without `sandbox_dir` shares its parent's
  sandbox.** It used to get an empty sandbox of its own, so every
  relative path its parent gave it failed (a worker from `AgentMap`
  found none of its inputs). A relative `sandbox_dir` still gives it
  its own directory inside the parent's. Every agent's system prompt
  states its working directory.

### Security

- **Agents' `Bash` runs without credentials.** Its environment drops
  every variable whose name contains `KEY`, `TOKEN`, `SECRET`,
  `PASSWORD`, `CREDENTIAL` or `AUTH`, and every configured provider's
  `api_key_env` whatever its name; the conjure process keeps them to
  call providers.

## [0.2.8] — 2026-10-17

Harness fixes from auditing a 24-trial benchmark campaign.

### Added

- **Run deadline and bounded waits.** `runtime.deadline` (Unix epoch
  seconds) and `runtime.wall_clock_s` (seconds from when the runtime is
  built; the earlier wins) let an embedding harness say when the run
  ends; `Runtime.deadline` can also be set on a live runtime. `WaitFor`,
  `Recv`, `Call`, combinator gathers and `Bash` are clipped to the time
  left. `runtime.max_wait_s` (default 1800) caps one `WaitFor`/`Recv`:
  a wait cut short returns `timed_out: true` with a `note` telling the
  agent to check its workers with `Peek`.
- **Tool-result cap.** `runtime.tool_output_max_chars` (default 40000;
  0 or null disables) caps every tool result an orchestral agent gets
  back (conjure tools, toolbase toolkits and MCP tools alike). A longer
  result keeps its head and tail around a `[... N chars truncated; full
  output ... saved to .conjure/tool-output/<file>]` notice; the full
  text is saved in the agent's sandbox, and the cut is logged as a
  `tool_output_truncated` event.
- `Read` takes `offset` and `limit` (lines) to page through long files.

### Fixed

- **One huge tool output killed the run.** A script printed 16M
  characters, all of it went into the agent's context, and the next
  model call failed with "prompt is too long". See the cap above.
- **`Bash` ran under `/bin/sh`.** Process substitution (`<(...)`) and
  other bash syntax failed with "syntax error near unexpected token".
  Commands now run under bash when there is one (`$CONJURE_SHELL`
  overrides), falling back to `/bin/sh`.
- **A worker's reply to "caller" went to `@system`.** `Spawn`'s
  `initial_message` was sent from `@system`, so a worker told to reply
  with `Send(to="caller")` replied into the void while its parent
  waited forever. The task now comes from the parent; `"caller"` falls
  back to the parent when the last sender was `@system`; a `Send` to
  `@system` is refused with a hint; the system prompt says how a
  spawned agent delivers its result.
- **A relative `Spawn(sandbox_dir=...)` landed outside the workspace.**
  It resolved against the process's working directory. It now resolves
  against the parent's sandbox, and a `sandbox_dir` outside the
  parent's sandbox is refused (`code=invalid_sandbox`) before the child
  is recorded or journaled.
- **A run cut off mid-turn recorded no cost.** Agent meta (cost,
  tokens, model) was journaled only at turn end, so an agent stopped
  inside a turn (a harness timeout while it blocked in `WaitFor`)
  recorded nothing. `Runtime.shutdown` now records every agent's meta
  first, and orchestral agents journal it every minute during a turn.

## [0.2.7] — 2026-10-16

Replays of finished runs show their cost and files, and a refused spawn
no longer leaves a ghost child.

### Fixed

- **A replayed session showed $0 and no tokens.** The web UI read cost
  and tokens from each agent's engine, and a replay has none. It now
  reads the cost each agent's turns journaled (`agent_meta.cost_usd`)
  and the tokens journaled beside it, which turns now record. Older
  journals (0.2.6 and before) take tokens from the event log's `usage`
  events. Tokens of a gateway model with no configured or list price
  show as unpriced, as they do live.
- **A replay of a run copied from another machine couldn't open its
  agents' files.** An agent whose sandbox was set to a path in the run
  directory (`<run>/workspace/...` beside `<run>/conjure-native`)
  recorded that machine's absolute path, and the Files view failed on
  it (`Operation not supported: '/home/...'` on macOS). Replay now
  maps such a path onto this copy of the run: the path's place relative
  to the recorded store, applied to the local store. Only paths inside
  the run directory, and only ones that exist locally, are mapped.
- **Every orchestral agent's event log carried its system prompt as an
  `unknown` event** (since 0.2.6, whose display-hook call logs the
  conversation from its first message). The hook now skips the system
  message; provenance keeps the prompt, and a child's log still opens
  with its role prompt.
- **A spawn refused for unknown tool groups no longer leaves a ghost
  child.** Tool groups were checked only while the child's engine was
  built, after the spawn had been recorded and journaled, so the refused
  spawn left a terminated (`spawn-failed`) record and a `spawn` journal
  entry. A benchmark that counts workers from the journal counted it: five
  of five FeynTable coordinated trials failed their one-worker gate after
  the root passed Claude Code tool names (`Read`, `Bash`) to an orchestral
  child and immediately retried with groups. Engine factories may now
  expose `validate_spec(spec, parent)`; `Runtime._spawn_batch` calls it
  with the model and toolkit checks, before any registry mutation. The
  orchestral factory checks tool-group names there (`check_tool_groups`,
  also used by `build_tools`), and `build_runtime`'s dispatcher routes the
  check to the engine each spec will run on, so `claude_agent` children,
  whose `tools` are Claude Code tool names, are unaffected. The error
  message is unchanged; a batch with one bad spec creates no children.
- **`Spawn`'s `tools` parameter says what it takes**: tool groups on the
  orchestral engine (`primitive`, `combinator`, `filesystem`, `toolkit`,
  `models`, `all`), Claude Code tool names on `claude_agent`, toolbase
  toolkits in `toolkits`. It said "names of tools", which is how an agent
  ended up passing `Read`.

## [0.2.6] — 2026-10-16

### Fixed

- **Tool results were missing from streaming orchestral agents' logs.**
  With an event log (every agent in the web UI and the TUI), an
  orchestral agent streams its replies by running orchestral's tool loop
  itself. That loop never called the display hook, which `Agent.run()`
  calls after each round of tool calls and which writes tool results to
  the event log. The model still got its results, but the log didn't:
  the web UI and the TUI showed tool calls without their results, and
  the activity timeline and tool-error counts missed them. Each round's
  results are now logged before the next reply streams, once each.

## [0.2.5] — 2026-10-16

### Fixed

- **`models.default` replaced the root's model.** It's meant for
  children spawned without a model, but it also applied to the root,
  overriding the `llms` entry's `model`. A config with
  `model: claude-opus-5-5` on its gateway entry and
  `models.default: amsc/claude-haiku-5-5` ran its root on Haiku. The root
  now runs on its own configuration, and `models.default` covers
  children only.

## [0.2.4] — 2026-10-16

Prices and token counts for model-gateway runs.

### Fixed

- **Gateway models showed as free.** An `openai_compatible` entry (a
  model access gateway, vLLM, LiteLLM) runs on orchestral's vLLM client,
  which prices every call at $0. Its calls are now priced from their
  token counts:
  - by the entry's new `pricing:` (USD per million tokens, by model id,
    id prefix, or `default`);
  - else by the public list price where orchestral's tables know the
    model (`claude-sonnet-5-5` takes `claude-sonnet-5`'s);
  - else counted as unpriced tokens, shown as such rather than as $0.
- **Orchestral agents reported no tokens.** They now emit per-turn
  `usage` events, so turn headers, the timeline and the run record count
  their tokens.

### Added

- The web UI shows tokens next to cost: the header reads
  `$1.35 · 290.0k tok`, or `unpriced` when no price is known, with a hint
  on how to set one. Each agent shows its cost, or its tokens when
  unpriced.
- `ModelCatalog` shows each API model's `price_per_mtok` where known, so
  agents can weigh cost when choosing a child's model.

## [0.2.3] — 2026-10-15

Agents stop when they're terminated, orchestral agents get a coding
harness's core tools, and safeguards against looping and against
releasing broken builds.

### Added

- **Orchestral agents get the core tools by default:** orchestration,
  combinators, and `Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob` in
  their sandbox, like a coding harness. A spec that lists `tools` keeps
  exactly that list. `Bash`, `Write` and `Edit` ask first, as on Claude
  Code agents; `runtime.auto_approve: true` starts a session with
  auto-approve on (for unattended runs; F6 toggles it live). The
  orchestral engine's permission check now honors auto-approve and
  "allow always", as the Claude engine's does.
- **Loop breaker.** An orchestral turn that calls a tool that doesn't
  exist three times, or gets the same tool error four times in a row,
  ends with a notice in the chat instead of another round.
- **Startup tool-name check.** An orchestral agent whose tools would be
  offered to the model under one name and dispatched under another (as
  in 0.2.1), or under duplicate names, refuses to start with an error
  naming them.
- **Tests run in CI** on every pull request and push to `main`, and the
  release workflow publishes only after they pass.
- `AgentMap` / `agent_map` take `max_concurrency` (default unlimited).
  Slots refill as workers finish, and one timeout covers the whole map.
- `Send` takes an optional `idempotency_key`. Retrying with the same key
  and message returns the original; the same key with a different
  message returns `idempotency_conflict`.

### Changed

- **Terminating an agent stops its work.**
  - Orchestral agents are now interrupted, as Claude and Codex agents
    already were: no further tool calls or model calls in the current
    turn.
  - Every agent's running `Bash` commands are stopped, including
    whatever they started (the whole process group).
  - Tool calls from an agent whose turn outlived its termination get
    `code=terminated` with its address, instead of a puzzling
    `no_runtime`.
- `Bash` runs each command in its own process group. A timeout stops the
  group, not just the shell, and reports the output so far. The default
  timeout is 120 s (was 30 s).
- `Send` no longer silently drops a message identical to one sent within
  the last 5 seconds. Identical messages are delivered as independent
  messages unless they share an `idempotency_key`.

### Fixed

- Drivers drain mailbox backlogs larger than 1,024 messages without
  waiting for another send to wake them.
- Sequence assignment and journal writes are serialized, so readers never
  see a message before it's journaled. Replay sorts out-of-order sends
  from older journals.
- A failed batch spawn terminates every child in the batch, revokes their
  tokens, and cleans up engines and logs already started. The records stay
  for inspection.
- Bounded mailbox reads no longer copy the remaining backlog, and the web
  UI parses only newly appended log lines.

## [0.2.2] — 2026-10-15

### Fixed

- **Every conjure tool call failed on the orchestral engine** in 0.2.1
  ("Tool 'catalog' not found!"). 0.2.1 renamed orchestral tools to
  match the prompt (`ToolCatalog`), but orchestral builds the tool list
  sent to the model with its own naming, so the model was offered
  `catalog`, `spawn` and `waitfor` while calls were routed under the
  new names. The tool list now carries the routed name. A new test
  drives a real tool call through an OpenAI-compatible gateway. Claude
  and Codex agents were not affected.

## [0.2.1] — 2026-10-15

Fixes from running on a model access gateway: requests capped and
retried, `Recv` / `WaitFor` no longer hang an agent, and one system
prompt for every engine.

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

- **One system prompt for every engine.** `system_prompts/agent.md` is
  the prompt for Claude, Codex and orchestral agents alike. Each engine
  fills in only its name, its file and shell tools, and how its tool
  names appear. The orchestral engine's separate prompt had drifted: it
  still said "Spawn framework", named tools that don't exist, and
  advised spawning children without the tools they need to reply. The
  shared prompt no longer assumes Claude models: spawn defaults come
  from the model catalog, and `Spawn`'s `initial_message` is documented.
- **Orchestral tools use the same names as everywhere else**
  (`WaitFor`, `GrantToolkits`, `ToolCatalog`, ...). orchestral had
  lowercased them and stripped every "Tool" (`waitfor`, `grantkits`,
  `catalog`), so prompts named tools that agents couldn't find.
- **Orchestral turns allow 50 tool rounds** (was 8). It's configurable
  as `runtime.max_tool_iterations`, and a turn that reaches it says so
  in the chat instead of ending silently mid-work.
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

[Unreleased]: https://github.com/tonymenzo/conjure/compare/v0.2.13...HEAD
[0.2.13]: https://github.com/tonymenzo/conjure/compare/v0.2.12...v0.2.13
[0.2.12]: https://github.com/tonymenzo/conjure/compare/v0.2.11...v0.2.12
[0.2.11]: https://github.com/tonymenzo/conjure/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/tonymenzo/conjure/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/tonymenzo/conjure/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/tonymenzo/conjure/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/tonymenzo/conjure/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/tonymenzo/conjure/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/tonymenzo/conjure/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/tonymenzo/conjure/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/tonymenzo/conjure/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/tonymenzo/conjure/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/tonymenzo/conjure/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/tonymenzo/conjure/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/tonymenzo/conjure/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/tonymenzo/conjure/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tonymenzo/conjure/releases/tag/v0.1.0
