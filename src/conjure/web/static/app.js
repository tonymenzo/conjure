/* conjure web UI — vanilla JS, no build step.
 *
 * Data flow: one /api/snapshot poll (tree, cost, activity, permissions)
 * drives the always-visible chrome; each open chat column tails its
 * agent's event log by byte offset; graph / timeline / files refresh
 * only while visible. In replay mode nothing polls — every view
 * re-reads when the scrubber moves, passing ?at=<instant>.
 */
"use strict";
(() => {
  // ---------- state ----------
  const S = {
    token: null,
    meta: null,
    live: true,
    at: null,               // replay instant (null = end)
    snap: null,
    agents: new Map(),      // addr -> tree node
    order: [],              // addrs in tree (DFS) order, visible only
    selected: null,
    view: "chat",
    columns: [],            // ChatColumn instances
    focusedCol: 0,
    showInternal: false,
    toasts: new Map(),      // key -> toast element (permission toasts)
  };
  const POLL_SNAPSHOT_MS = 1000;
  const POLL_EVENTS_MS = 400;
  const POLL_DETAIL_MS = 2000;
  const POLL_VIEW_MS = 2000;
  const MAX_COLUMNS = 4;

  // ---------- tiny DOM helpers ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  /** replaceChildren that, like h(), skips null / false. */
  const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
  const SVG = "http://www.w3.org/2000/svg";
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null) continue;
      if (k === "class") el.setAttribute("class", v);
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid) el.append(kid);
    return el;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  // ---------- formatting ----------
  const pad = (n) => String(n).padStart(2, "0");
  function clock(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function dur(sec) {
    if (sec == null || !isFinite(sec)) return "";
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m${pad(sec % 60)}s`;
    return `${Math.floor(sec / 3600)}h${pad(Math.floor((sec % 3600) / 60))}m`;
  }
  const money = (x) => (x ? (x < 0.01 ? `$${x.toFixed(4)}` : `$${x.toFixed(2)}`) : "$0");
  const kfmt = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));
  const clip = (str, n) => (str.length > n ? str.slice(0, n - 1) + "…" : str);
  function bodyText(body) {
    if (typeof body === "string") return body;
    try { return JSON.stringify(body); } catch { return String(body); }
  }
  function argsPreview(args) {
    if (!args || typeof args !== "object") return clip(String(args ?? ""), 80);
    return Object.entries(args)
      .map(([k, v]) => `${k}=${clip(typeof v === "string" ? JSON.stringify(v) : bodyText(v), 50)}`)
      .join(", ");
  }
  const shortModel = (m) => String(m || "").replace(/^claude-/, "").replace(/-\d{8}$/, "");
  const ENGINE_NAMES = { claude_agent: "claude", codex: "codex", orchestral: "orchestral", scripted: "scripted" };
  /** The model chip: what the agent runs on, and how sure we are. */
  function modelChip(info, { withEngine = true } = {}) {
    if (!info) return null;
    const engine = ENGINE_NAMES[info.engine] || info.engine || "";
    const src = info.model_source || (info.model ? "observed" : "default");
    const cli = { claude_agent: "claude", codex: "codex" }[info.engine];
    const label = info.model ? shortModel(info.model) : cli ? "CLI default model" : "model unknown";
    const title = {
      observed: `Running on ${info.model}`,
      resolved: `${info.model} — what this agent's model setting resolves to on the ${cli || "engine"} CLI; confirmed by its first reply`,
      configured: `Configured as ${info.model}; the exact version shows after its first reply`,
      default: `Not pinned: uses the ${cli || "engine"} CLI's default model, which shows after the first reply. Set root.model in the config to choose one.`,
    }[src];
    return h("span", { class: `chip model ${src}`, title }, withEngine && engine ? `${engine} · ` : "", h("b", { text: label }));
  }
  const MCP_PREFIX = /^mcp__(conjure|spawn)__/;
  const toolName = (n) => String(n || "?").replace(MCP_PREFIX, "");
  function now() {
    if (!S.live) return S.at ?? S.meta?.last_event ?? Date.now() / 1000;
    return Date.now() / 1000;
  }

  // Markdown: fenced code, headings, lists (nested, ordered), tables,
  // blockquotes, rules, emphasis, links, and LaTeX math ($…$, \(…\),
  // $$…$$, \[…\], \begin{align}…) typeset by the vendored KaTeX.
  // Text goes through textContent and math through KaTeX (trust off), so
  // model output can't inject HTML.
  const FENCE = /^\s*(`{3,}|~{3,})\s*([\w+-]*)/;
  const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const QUOTE = /^\s{0,3}>\s?(.*)$/;
  const MATH_ENV = /^\s*\\begin\{(equation|align|alignat|gather|multline|eqnarray|split|cases|[pbvBV]?matrix)(\*?)\}/;
  const isDelimCell = (c) => /^:?-+:?$/.test(c);

  function math(tex, display) {
    const el = h(display ? "div" : "span", { class: display ? "math display" : "math" });
    if (window.katex) {
      try {
        window.katex.render(tex, el, { displayMode: display, throwOnError: false, trust: false, strict: "ignore", output: "htmlAndMathml" });
        return el;
      } catch { /* fall through to the source */ }
    }
    el.classList.add("raw");
    el.textContent = display ? tex : `$${tex}$`;
    return el;
  }

  /** Cells of a table row. Pipes inside `code` or $math$ don't split
   *  (|x| is everywhere in physics), unless the row's $ don't pair up. */
  function splitRow(line, protectMath = true) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const cells = [];
    let cur = "", code = false, inMath = false;
    for (let k = 0; k < s.length; k++) {
      const c = s[k];
      if (c === "\\" && (s[k + 1] === "|" || s[k + 1] === "$")) { cur += s[k + 1] === "|" ? "|" : "\\$"; k++; continue; }
      if (c === "`") code = !code;
      else if (c === "$" && !code && protectMath) inMath = !inMath;
      if (c === "|" && !code && !inMath) { cells.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    if (inMath) return splitRow(line, false);
    cells.push(cur.trim());
    return cells;
  }

  function tableAt(lines, i) {
    if (!lines[i].includes("|") || i + 1 >= lines.length) return null;
    const head = splitRow(lines[i]);
    const delim = splitRow(lines[i + 1]);
    if (!delim.every(isDelimCell) || delim.length !== head.length) return null;
    const align = delim.map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : null));
    const cell = (tag, text, k) => inline(h(tag, align[k] ? { style: `text-align:${align[k]}` } : null), text);
    const thead = h("thead", null, h("tr", null, head.map((c, k) => cell("th", c, k))));
    const tbody = h("tbody");
    let j = i + 2;
    while (j < lines.length && lines[j].trim() && lines[j].includes("|") && !FENCE.test(lines[j])) {
      const row = splitRow(lines[j]);
      tbody.append(h("tr", null, head.map((_, k) => cell("td", row[k] ?? "", k))));
      j++;
    }
    return [h("div", { class: "tablewrap" }, h("table", null, thead, tbody)), j];
  }

  /** Display math starting at line i: $$…$$, \[…\], or a bare
   *  \begin{env}…\end{env}. Unclosed (mid-stream) → null, shown as text. */
  function displayMathAt(lines, i) {
    const t = lines[i].trim();
    const env = t.match(MATH_ENV);
    let close, rest, keep = false;
    if (t.startsWith("$$")) { close = "$$"; rest = t.slice(2); }
    else if (t.startsWith("\\[")) { close = "\\]"; rest = t.slice(2); }
    else if (env) { close = `\\end{${env[1]}${env[2]}}`; rest = t; keep = true; }
    else return null;
    const parts = [];
    for (let j = i; j < lines.length; j++) {
      if (j > i) rest = lines[j].trim();
      const at = rest.indexOf(close);
      if (at >= 0) {
        parts.push(rest.slice(0, keep ? at + close.length : at));
        const tex = parts.join("\n").trim();
        return tex ? [math(tex, true), j + 1, rest.slice(at + close.length).trim()] : null;
      }
      parts.push(rest);
    }
    return null;
  }

  function listAt(lines, i) {
    const indentOf = (s) => s.replace(/\t/g, "    ").length;
    const root = { indent: -1, el: null, ordered: null };
    const stack = [];
    let j = i, top = null;
    while (j < lines.length) {
      const m = lines[j].match(LIST_ITEM);
      if (!m) {
        const line = lines[j];
        if (!line.trim()) {
          // A blank line ends the list unless another item follows.
          if (j + 1 < lines.length && LIST_ITEM.test(lines[j + 1])) { j++; continue; }
          break;
        }
        if (/^\s+\S/.test(line) && top) {
          const li = top.el.lastElementChild;
          if (li) { li.append("\n"); inline(li, line.trim()); }
          j++;
          continue;
        }
        break;
      }
      const indent = indentOf(m[1]);
      const ordered = /\d/.test(m[2]);
      while (stack.length && indent < stack[stack.length - 1].indent) stack.pop();
      top = stack[stack.length - 1];
      if (!top || indent > top.indent) {
        const el = h(ordered ? "ol" : "ul");
        if (ordered && parseInt(m[2], 10) !== 1) el.setAttribute("start", parseInt(m[2], 10));
        const parentLi = top?.el.lastElementChild;
        if (parentLi) parentLi.append(el);
        top = { indent, el, ordered };
        stack.push(top);
        if (!root.el) root.el = el;
      }
      top.el.append(inline(h("li"), m[3]));
      j++;
    }
    return [root.el, j];
  }

  function renderMarkdown(text) {
    const frag = document.createDocumentFragment();
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    let para = [];
    const flushPara = () => {
      if (para.length) frag.append(inline(h("p"), para.join("\n")));
      para = [];
    };
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(FENCE);
      if (fence) {
        flushPara();
        const code = [];
        const closeRe = new RegExp(`^\\s*${fence[1][0]}{${fence[1].length},}\\s*$`);
        i++;
        while (i < lines.length && !closeRe.test(lines[i])) code.push(lines[i++]);
        i++;
        const lang = fence[2].toLowerCase();
        if (lang === "math" || lang === "katex") frag.append(math(code.join("\n"), true));
        else if (window.ConjureCode) {
          const CC = window.ConjureCode;
          const hl = CC.langForTag(lang);
          const shell = ["sh", "bash", "shell", "zsh", "console", "terminal"].includes(lang);
          frag.append(CC.codeBlock(code.join("\n"), { lang: hl, kind: shell ? "shell" : undefined, langLabel: lang || "", collapse: 40 }));
        } else frag.append(h("pre", null, h("code", { text: code.join("\n") })));
        continue;
      }
      const dm = displayMathAt(lines, i);
      if (dm) {
        flushPara();
        frag.append(dm[0]);
        if (dm[2]) para.push(dm[2]);
        i = dm[1];
        continue;
      }
      const table = tableAt(lines, i);
      if (table) { flushPara(); frag.append(table[0]); i = table[1]; continue; }
      const head = line.match(HEADING);
      if (head) { flushPara(); frag.append(inline(h(`h${head[1].length}`), head[2])); i++; continue; }
      if (RULE.test(line)) { flushPara(); frag.append(h("hr")); i++; continue; }
      if (QUOTE.test(line)) {
        flushPara();
        const quoted = [];
        while (i < lines.length && QUOTE.test(lines[i])) quoted.push(lines[i++].match(QUOTE)[1]);
        frag.append(h("blockquote", null, renderMarkdown(quoted.join("\n"))));
        continue;
      }
      if (LIST_ITEM.test(line)) {
        flushPara();
        const [el, next] = listAt(lines, i);
        frag.append(el);
        i = next;
        continue;
      }
      if (!line.trim()) { flushPara(); i++; continue; }
      para.push(line);
      i++;
    }
    flushPara();
    return frag;
  }

  // One pass over the inline grammar; the first alternative to match at a
  // position wins, so code spans shield their contents from everything.
  const INLINE = new RegExp([
    /(`+)([\s\S]*?[^`])\1(?!`)/,                         // 1,2 code
    /\$\$([\s\S]+?)\$\$/,                                // 3 display math
    /\\\(([\s\S]+?)\\\)/,                                // 4 \( \)
    /\\\[([\s\S]+?)\\\]/,                                // 5 \[ \]
    /\$(?=[^\s$])((?:\\\$|[^$])*?[^\s\\$])\$(?!\d)/,     // 6 $math$ (not $5 or $5-$10)
    /\*\*(?=\S)([\s\S]*?\S)\*\*/,                        // 7 bold
    /(?<![\w])__(?=\S)([\s\S]*?\S)__(?![\w])/,           // 8 bold
    /~~(?=\S)([\s\S]*?\S)~~/,                            // 9 strike
    /\*(?=[^\s*])([\s\S]*?[^\s*])\*/,                    // 10 italic
    /(?<![\w])_(?=\S)([\s\S]*?\S)_(?![\w])/,             // 11 italic
    /\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/, // 12,13 link
    /(https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"*_])/,        // 14 bare URL
    /\\([\\`*_{}[\]()#+\-.!$|~<>])/,                     // 15 escape
  ].map((r) => r.source).join("|"), "g");

  function inline(el, text) {
    let last = 0;
    for (const m of text.matchAll(INLINE)) {
      if (m.index > last) el.append(text.slice(last, m.index));
      last = m.index + m[0].length;
      if (m[2] !== undefined) el.append(h("code", { text: m[2].replace(/^ (.*) $/s, "$1") }));
      else if (m[3] !== undefined) el.append(math(m[3].trim(), true));
      else if (m[4] !== undefined) el.append(math(m[4].trim(), false));
      else if (m[5] !== undefined) el.append(math(m[5].trim(), true));
      else if (m[6] !== undefined) el.append(math(m[6], false));
      else if (m[7] !== undefined || m[8] !== undefined) el.append(inline(h("b"), m[7] ?? m[8]));
      else if (m[9] !== undefined) el.append(inline(h("s"), m[9]));
      else if (m[10] !== undefined || m[11] !== undefined) el.append(inline(h("i"), m[10] ?? m[11]));
      else if (m[12] !== undefined) el.append(inline(h("a", { href: m[13], target: "_blank", rel: "noopener noreferrer" }), m[12]));
      else if (m[14] !== undefined) el.append(h("a", { href: m[14], target: "_blank", rel: "noopener noreferrer", text: m[14] }));
      else if (m[15] !== undefined) el.append(m[15]);
    }
    if (last < text.length) el.append(text.slice(last));
    return el;
  }

  // Mirror of chat.py's _summarize_tool_result.
  function summarizeResult(text, failed) {
    let parsed = null;
    const t = (text || "").trim();
    if (t.startsWith("{")) {
      try { parsed = JSON.parse(t); } catch {
        try { parsed = JSON.parse(t.replace(/'/g, '"').replace(/\bTrue\b/g, "true").replace(/\bFalse\b/g, "false").replace(/\bNone\b/g, "null")); } catch { parsed = null; }
      }
    }
    if (!parsed || typeof parsed !== "object") return failed ? clip(t, 1000) : clip(t.replace(/\s+/g, " "), 200);
    if (parsed.ok === false) return `${parsed.code || "error"}${parsed.error ? ": " + clip(String(parsed.error), 1000) : ""}`;
    for (const key of ["address", "result", "msg_id", "terminated", "envelopes", "toolkits", "next_seq"]) {
      if (key in parsed) {
        const v = parsed[key];
        if (Array.isArray(v)) return `${key}=[${v.length} item(s)]`;
        return `${key}=${clip(bodyText(v), 80)}`;
      }
    }
    return "ok";
  }

  /** A file's text for the files view, highlighted by its extension. */
  function highlightFile(d) {
    const el = h("code", { class: "src" });
    const CC = window.ConjureCode;
    if (CC) CC.highlightInto(el, d.content, CC.langForPath(d.path || d.name || ""));
    else el.textContent = d.content;
    return el;
  }
  // Tool calls: the command, file or diff up front (code.js), the raw
  // arguments one click away.
  const OPEN_KINDS = new Set(["shell", "write", "edit", "multiedit", "patch"]);
  function toolCallBlock(name, args) {
    const CC = window.ConjureCode;
    const label = toolName(name);
    if (!CC) {
      return h("details", { class: "toolcall" },
        h("summary", null, h("span", { class: "glyph", text: "⏺ " }), h("span", { class: "name", text: label }),
          h("span", { class: "args", text: `(${argsPreview(args)})` })),
        h("pre", { text: JSON.stringify(args, null, 2) }));
    }
    const kind = CC.toolKind(name, args);
    const sum = CC.callSummary(name, args);
    const head = h("summary", null,
      h("span", { class: "glyph", text: "⏺ " }),
      h("span", { class: "name", text: label }),
      sum
        ? [h("span", { class: "target" + (kind === "shell" ? " cmd" : "") + (sum.dim ? " dimmed" : ""), text: `${sum.prefix ? sum.prefix + " " : ""}${sum.text}` }),
           sum.extra ? h("span", { class: "args", text: sum.extra }) : null]
        : h("span", { class: "args", text: `(${argsPreview(args)})` }));
    return h("details", { class: `toolcall rich kind-${kind}`, open: OPEN_KINDS.has(kind) ? true : null },
      head, CC.renderCall(name, args, { renderMarkdown }));
  }
  /** Whether a result plausibly answers ``call`` (orchestral's dict
   *  results say what they are; a mismatch means the pairing slipped). */
  function resultFits(call, text) {
    const parsed = window.ConjureCode.parseLiteral(text);
    if (!parsed || typeof parsed !== "object" || parsed.ok === false) return true;
    const kind = window.ConjureCode.toolKind(call.name, call.args);
    if (kind === "shell") return "stdout" in parsed || "exit_code" in parsed || "returncode" in parsed;
    if (kind === "read") return "content" in parsed;
    if ("stdout" in parsed && "exit_code" in parsed) return false;
    return true;
  }

  // ---------- API ----------
  async function api(path, params = {}) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v != null && v !== "") q.set(k, v);
    if (!S.live && S.at != null) q.set("at", S.at);
    const res = await fetch(`/api/${path}?${q}`, { headers: { "X-Conjure-Token": S.token } });
    setConn(res.ok || res.status < 500);
    if (res.status === 401) { showGate(); throw new Error("unauthorized"); }
    const data = await res.json();
    if (data && data.ok === false && res.status >= 400) throw new Error(data.error || res.statusText);
    return data;
  }
  async function post(path, body) {
    const res = await fetch(`/api/${path}`, {
      method: "POST",
      headers: { "X-Conjure-Token": S.token, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({ ok: false, error: res.statusText }));
    if (!res.ok || data.ok === false) {
      toast(`${path}: ${data.error || res.statusText}`, { err: true });
      throw new Error(data.error || res.statusText);
    }
    return data;
  }
  function setConn(ok) {
    const el = $("#conn");
    el.className = "conn " + (ok ? "ok" : "bad");
    el.title = ok ? "connected" : "connection lost";
  }
  function showGate() { $("#gate").hidden = false; }

  // ---------- toasts ----------
  function toast(text, { err = false, onclick = null, key = null, ttl = 6000 } = {}) {
    if (key && S.toasts.has(key)) return S.toasts.get(key);
    const el = h("div", { class: "toast" + (err ? " err" : "") });
    if (key) { el.dataset.key = key; S.toasts.set(key, el); }
    if (text instanceof Node) el.append(text); else el.textContent = text;
    el.addEventListener("click", () => { onclick?.(); el.remove(); });
    $("#toasts").append(el);
    if (ttl) setTimeout(() => el.remove(), ttl);
    return el;
  }

  // ---------- snapshot: tree, activity, stats, permissions ----------
  async function refreshSnapshot() {
    let snap;
    try { snap = await api("snapshot"); } catch (e) { return; }
    S.snap = snap;
    S.agents.clear();
    S.order = [];
    const walk = (node, prefix, isLast, isRoot) => {
      if (!node) return;
      S.agents.set(node.addr, node);
      const visible = S.showInternal || !node.internal;
      if (visible) S.order.push({ node, prefix, isLast, isRoot });
      const kids = node.children.filter((c) => S.showInternal || !c.internal);
      const nextPrefix = isRoot ? "" : prefix + (isLast ? "   " : "│  ");
      kids.forEach((c, i) => walk(c, nextPrefix, i === kids.length - 1, false));
    };
    walk(snap.tree, "", true, true);
    if (!S.selected && snap.tree) select(snap.tree.addr, { quiet: true });
    renderTree();
    renderActivity();
    renderStats();
    handlePermissions();
    for (const col of S.columns) col.updateHeader();
  }

  function statusClass(node) {
    if (node.awaiting_permission) return "st-awaiting";
    return "st-" + node.status;
  }

  function renderTree() {
    const tree = $("#tree");
    // Same reason as the chat header: only rebuild on a visible change.
    const sig = JSON.stringify(S.order.map(({ node, prefix, isLast }) => [node.addr, node.label, node.status,
      node.awaiting_permission, node.model, node.cost?.toFixed?.(4), node.toolkits?.length, node.oneshot, prefix, isLast]));
    if (sig === renderTree.sig) return;
    renderTree.sig = sig;
    const frag = document.createDocumentFragment();
    for (const { node, prefix, isLast, isRoot } of S.order) {
      const guide = isRoot ? "" : prefix + (isLast ? "└─ " : "├─ ");
      const meta = [];
      meta.push(node.model ? shortModel(node.model) : "default model");
      if (node.cost) meta.push(money(node.cost));
      if (node.unpriced_tokens) meta.push(`${kfmt(node.unpriced_tokens)} tok unpriced`);
      else if (!node.cost && node.tokens) meta.push(`${kfmt(node.tokens)} tok`);
      const row = h("div", {
        class: `node ${node.status}${node.addr === S.selected ? " selected" : ""}`,
        dataset: { addr: node.addr },
        title: `${node.addr} · ${node.status}${node.budget != null ? ` · budget ${money(node.subtree_cost)}/${money(node.budget)}` : ""}`,
        onclick: () => select(node.addr),
        ondblclick: () => openChat(node.addr, { focus: true }),
      },
        h("span", { class: "guide", text: guide }),
        h("span", { class: `dot ${statusClass(node)}` }),
        h("span", { class: "lbl", text: node.label }),
        node.toolkits?.length ? h("span", { class: "kit", title: node.toolkits.join(", "), text: `⚒${node.toolkits.length}` }) : null,
        node.awaiting_permission ? h("span", { class: "flag", text: "needs you" }) : null,
        node.oneshot ? h("span", { class: "dim small", text: "1×" }) : null,
        h("span", { class: "meta", text: meta.join(" · ") }),
      );
      frag.append(row);
    }
    tree.replaceChildren(frag);
    const total = [...S.agents.values()].length;
    const alive = [...S.agents.values()].filter((n) => n.status !== "terminated").length;
    $("#agent-count").textContent = `${alive}/${total}`;
  }

  function labelOf(addr, fallback) {
    if (addr === "@user") return "you";
    if (addr === "@system") return "system";
    return S.agents.get(addr)?.label || fallback || addr;
  }

  function renderActivity() {
    const box = $("#activity");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
    const rows = [];
    for (const a of S.snap.activity || []) {
      const kind = a.headers?.kind;
      if (a.from === "@system" && typeof a.body === "object" && a.body?.kind === "child_event") {
        rows.push({ ts: a.ts, cls: "tool", who: labelOf(a.body.child_addr, a.body.child_label), arrow: "·", body: `${a.body.event}${a.body.reason ? " — " + a.body.reason : ""}` });
        continue;
      }
      rows.push({
        ts: a.ts,
        cls: kind === "toolkit_request" ? "req" : "",
        who: `${labelOf(a.from, a.from_label)} → ${labelOf(a.to, a.to_label)}`,
        arrow: kind === "toolkit_request" ? "⚒" : "",
        body: kind === "toolkit_request" ? `requests ${Object.keys(a.body?.toolkits || {}).join(", ")}` : clip(bodyText(a.body), 160),
      });
    }
    for (const e of S.snap.tool_events || []) {
      if (e.kind !== "tool_call") continue;
      rows.push({ ts: e.ts, cls: "tool", who: labelOf(e.from, e.from_label), arrow: "⏺", body: toolName(e.tool) });
    }
    rows.sort((a, b) => a.ts - b.ts);
    box.replaceChildren(...rows.slice(-150).map((r) =>
      h("div", { class: `act ${r.cls}` },
        h("span", { class: "t", text: clock(r.ts) }),
        h("span", null, h("span", { class: "who", text: r.who }), r.arrow ? h("span", { class: "arrow", text: ` ${r.arrow} ` }) : " ", h("span", { class: "body", text: r.body })))));
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function renderStats() {
    const c = S.snap.cost || {};
    const priced = c.unpriced_tokens && !c.total ? "unpriced" : `${money(c.total)}${c.unpriced_tokens ? " + unpriced" : ""}`;
    const parts = [`${priced}${c.subscription ? " + subscription" : ""}`];
    if (c.tokens) parts.push(`${kfmt(c.tokens)} tok`);
    $("#stats").textContent = parts.join(" · ");
    $("#stats").title = c.unpriced_tokens
      ? `${kfmt(c.unpriced_tokens)} tokens are on models with no known price: set llms.<entry>.pricing in the config to count them`
      : "";
    const auto = $("#auto-toggle");
    auto.classList.toggle("on", !!S.snap.auto_mode);
    auto.querySelector("b").textContent = S.snap.auto_mode ? "on" : "off";
  }

  function handlePermissions() {
    const pending = S.snap.permissions || [];
    const shown = new Set(S.columns.map((c) => c.addr));
    // A toast is only for a request you can't already see: drop it once
    // the request resolves or its agent's chat is open.
    const stillToast = new Set(pending.filter((p) => !shown.has(p.addr)).map((p) => p.req_id));
    for (const [key, el] of S.toasts) {
      if (!stillToast.has(key)) { el.remove(); S.toasts.delete(key); }
    }
    for (const p of pending) {
      if (shown.has(p.addr)) continue;
      toast(h("span", null, h("b", { text: `${p.addr_label || p.addr}` }), ` wants `, h("b", { text: toolName(p.tool_name) }), " — click to review"),
        { key: p.req_id, ttl: 0, onclick: () => openChat(p.addr, { newColumn: true }) });
    }
    for (const col of S.columns) col.renderPermission(pending.filter((p) => p.addr === col.addr));
  }

  // ---------- selection & keyboard nav ----------
  function select(addr, { quiet = false } = {}) {
    S.selected = addr;
    renderTreeSelection();
    syncHash();
    if (quiet) {
      if (!S.columns.length) openChat(addr);
      return;
    }
    if (S.view === "chat") openChat(addr);
    if (S.view === "graph") { Graph.select(addr); }
    if (S.view === "timeline") Timeline.render();
    if (S.view === "files") Files.open(addr);
  }
  function renderTreeSelection() {
    for (const el of document.querySelectorAll("#tree .node")) {
      el.classList.toggle("selected", el.dataset.addr === S.selected);
    }
    document.querySelector(`#tree .node[data-addr="${CSS.escape(S.selected || "")}"]`)?.scrollIntoView({ block: "nearest" });
  }
  function moveSelection(delta) {
    const addrs = S.order.map((o) => o.node.addr);
    if (!addrs.length) return;
    const i = Math.max(0, addrs.indexOf(S.selected));
    select(addrs[Math.min(addrs.length - 1, Math.max(0, i + delta))]);
  }

  // ---------- chat ----------
  /** Renders an agent's event stream into a container, mirroring
   *  chat.py: chunks accumulate into a streaming response block that
   *  stream_end finalizes with its tool calls; thinking_* / turn_*
   *  frame each turn with elapsed time and tokens. */
  class Renderer {
    constructor(container, { live }) {
      this.c = container;
      this.live = live;
      this.stream = null;   // {el, text, ts}
      this.turn = null;     // {el, start, tokens, done, src}
    }
    block(el, ts, sub = false) {
      el.classList.add("blk");
      if (sub) el.classList.add("follow");
      if (ts) el.dataset.ts = ts;
      this.c.append(el);
      return el;
    }
    apply(ev) {
      const k = ev.kind;
      switch (k) {
        case "turn_start":
        case "thinking_start":
          if (this.turn && !this.turn.done) {
            // turn_start (driver) then thinking_start (engine) frame
            // the same turn: keep the first header.
            if (this.turn.src !== k) return;
            this.endTurn(ev.ts);
          }
          this.startTurn(ev.ts, k);
          return;
        case "usage":
          if (this.turn) { this.turn.tokens = ev.tokens_out || 0; this.paintTurn(); }
          return;
        case "thinking_end":
        case "turn_end":
          if (this.turn && !this.turn.done) this.endTurn(ev.ts, !!ev.errored);
          if (this.stream) this.finishStream([]);
          return;
        case "chunk":
          if (this.turn && !this.turn.firstText) { this.turn.firstText = ev.ts; this.paintTurn(); }
          if (!this.stream) {
            const el = this.block(h("div", { class: "resp streaming" }), ev.ts);
            this.stream = { el, text: "" };
          }
          this.stream.text += ev.text || "";
          this.paintStream();
          return;
        case "stream_end":
          this.finishStream(ev.tool_calls || [], ev.ts);
          return;
        default:
          if (this.stream) this.finishStream([]);
      }
      switch (k) {
        case "response":
          this.response(ev.text || "", ev.tool_calls || [], ev.ts);
          return;
        case "assistant":
          this.response(ev.text || "", [], ev.ts);
          return;
        case "tool": this.toolResult(ev); return;
        case "user_input":
          this.block(h("div", { class: "userblk", text: ev.text || "" }), ev.ts);
          return;
        case "system_prompt":
          this.block(h("details", { class: "sysprompt" },
            h("summary", { text: `system prompt${ev.label ? " · " + ev.label : ""}` }),
            h("pre", { text: ev.text || "" })), ev.ts);
          return;
        case "error":
          this.block(h("div", { class: "errblk", text: ev.text || "error" }), ev.ts);
          return;
        case "notice":
          this.block(h("div", { class: "metablk retry", text: `⚠ ${ev.text || ""}` }), ev.ts);
          return;
        case "retry":
          this.block(h("div", { class: "metablk retry", title: ev.error || "",
            text: `⟳ LLM call failed (${(ev.error || "").split(":")[0] || "error"}); retry ${ev.attempt}/${ev.max} in ${Math.round(ev.delay_s || 0)}s` }), ev.ts);
          return;
        case "toolkits":
          this.block(h("div", { class: "metablk" }, "⚒ toolkits now: ",
            h("span", { class: "kits", text: (ev.tools || []).join(", ") || "(none)" }),
            ev.skills?.length ? h("div", null, "✦ skills: ", h("span", { class: "kits", text: ev.skills.join(", ") })) : null), ev.ts);
          return;
        default: return; // user (driver-wrapped prompt), perm_probe, spawned, unknown
      }
    }
    startTurn(ts, src) {
      this.pendingCalls = [];
      const el = this.block(h("div", { class: "turnhdr" }), ts);
      this.turn = { el, start: ts, tokens: 0, done: false, src, firstText: null };
      this.paintTurn();
    }
    endTurn(ts, errored = false) {
      this.turn.done = true;
      this.turn.end = ts;
      this.turn.errored = errored;
      this.paintTurn();
    }
    paintTurn() {
      const t = this.turn;
      if (!t) return;
      const live = this.live && !t.done;
      const end = t.done ? t.end : live ? Date.now() / 1000 : t.firstText || t.start;
      t.el.classList.toggle("live", live && !t.firstText);
      fill(t.el,
        h("span", { class: live ? "spin" : "dim", text: live ? "◆" : "◇" }),
        h("span", { class: "label", text: clock(t.start) }),
        h("span", { text: dur(end - t.start) }),
        t.tokens ? h("span", { text: `↓ ${kfmt(t.tokens)} tok` }) : null,
        t.errored ? h("span", { style: "color:var(--red)", text: "errored" }) : null,
      );
    }
    tick() { if (this.turn && !this.turn.done && this.live) this.paintTurn(); }
    /** Re-render the streaming reply as markdown at most every 150 ms:
     *  tables and math appear as they arrive, without re-typesetting
     *  per token. */
    paintStream() {
      const st = this.stream;
      if (!st || st.timer) return;
      st.timer = setTimeout(() => {
        st.timer = null;
        if (this.stream === st) st.el.replaceChildren(renderMarkdown(st.text));
      }, 150);
    }
    finishStream(toolCalls, ts) {
      const st = this.stream;
      this.stream = null;
      if (st) {
        clearTimeout(st.timer);
        st.el.classList.remove("streaming");
        st.el.replaceChildren(renderMarkdown(st.text));
        if (!st.text.trim()) st.el.remove();
      }
      this.toolCalls(toolCalls, ts);
    }
    response(text, toolCalls, ts) {
      if (text.trim()) this.block(h("div", { class: "resp" }, renderMarkdown(text)), ts);
      this.toolCalls(toolCalls, ts);
    }
    toolCalls(calls, ts) {
      // A new model response means the previous batch's results are
      // all in: results pair with this batch, in order.
      this.pendingCalls = [];
      for (const tc of calls || []) {
        const name = tc.name || tc.tool_name;
        const args = tc.args ?? tc.arguments ?? {};
        this.pendingCalls.push({ name, args });
        this.block(toolCallBlock(name, args), ts, true);
      }
    }
    toolResult(ev) {
      const text = ev.text || "";
      const failed = !!ev.failed;
      const CC = window.ConjureCode;
      if (CC) {
        let call = (this.pendingCalls || []).shift() || null;
        if (call && !resultFits(call, text)) call = null; // out of step: judge by shape
        const r = CC.renderResult(text, failed, call);
        if (r) {
          const bad = r.failed ?? failed;
          const cls = "toolres rich" + (bad ? " failed" : "");
          const summary = r.summary || summarizeResult(text, failed) || "result";
          this.block(r.el
            ? h("details", { class: cls, open: CC.toolKind(call?.name, call?.args) === "shell" || (!call && r.el.classList.contains("term")) ? true : null },
                h("summary", { text: summary }), r.el)
            : h("div", { class: cls }, h("details", null, h("summary", { text: summary }))), ev.ts, true);
          return;
        }
      }
      const summary = summarizeResult(text, failed) || (failed ? "(failed)" : "");
      if (!summary) return;
      const full = text.length > summary.length + 2 || /\n/.test(text);
      this.block(full
        ? h("details", { class: "toolres" + (failed ? " failed" : "") }, h("summary", { text: summary }), h("pre", { text }))
        : h("div", { class: "toolres" + (failed ? " failed" : "") }, h("details", null, h("summary", { text: summary }))), ev.ts, true);
    }
    flush() { if (this.stream) this.finishStream([]); }
  }

  class ChatColumn {
    constructor(addr) {
      this.epoch = 0;          // bumped by each load(); stale responses are dropped
      this.loaded = false;
      this.polling = false;
      this.pollAgain = false;
      this.addr = addr;
      this.offset = 0;
      this.start = 0;
      this.detail = null;
      this.seenSeq = new Set();
      this.inspectorOpen = false;
      this.el = h("section", { class: "chat pane" + (S.live ? "" : " readonly") });
      this.title = h("h2", { class: "pane-title" });
      this.head = h("div", { class: "chat-head" });
      this.inspector = h("div", { class: "inspector", hidden: true });
      this.body = h("div", { class: "chat-body" });
      this.permBox = h("div");
      this.textarea = h("textarea", { rows: 1, placeholder: "message this agent…  (enter to send · shift+enter newline)" });
      this.inputBox = h("div", { class: "chat-input pane" }, this.textarea,
        h("div", { class: "hint" }, h("span", { text: "" }), h("span", { text: "esc to leave" })));
      this.el.append(this.title, this.head, this.inspector, this.body, this.permBox, this.inputBox);
      this.el.addEventListener("mousedown", (e) => {
        const i = S.columns.indexOf(this);
        if (i < 0 || i === S.focusedCol || e.target.closest("button")) return;
        setFocusedColumn(i);
        if (S.selected !== this.addr) { S.selected = this.addr; renderTreeSelection(); syncHash(); }
      });
      this.textarea.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); }
        if (e.key === "Escape") this.textarea.blur();
      });
      this.textarea.addEventListener("input", () => {
        this.textarea.style.height = "auto";
        this.textarea.style.height = Math.min(200, this.textarea.scrollHeight) + "px";
      });
      this.load();
    }
    nearBottom() { return this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 60; }
    scrollEnd() { this.body.scrollTop = this.body.scrollHeight; }
    async load() {
      const epoch = ++this.epoch;
      this.loaded = false;
      this.body.replaceChildren();
      this.renderer = new Renderer(this.body, { live: S.live });
      this.seenSeq.clear();
      let data;
      try { data = await api("events", { addr: this.addr }); } catch { return; }
      if (epoch !== this.epoch) return;   // retargeted meanwhile
      this.start = data.start;
      this.offset = data.offset;
      this.loaded = true;
      if (data.start > 0) this.body.append(this.olderButton());
      for (const ev of data.events) this.renderer.apply(ev);
      if (!S.live) this.renderer.flush();
      await this.refreshDetail();
      this.scrollEnd();
    }
    olderButton() {
      return h("button", { class: "older", text: "load earlier", onclick: (e) => this.loadOlder(e.target) });
    }
    async loadOlder(btn) {
      const data = await api("events", { addr: this.addr, before: this.start });
      const holder = h("div");
      const r = new Renderer(holder, { live: false });
      for (const ev of data.events) r.apply(ev);
      r.flush();
      const prevHeight = this.body.scrollHeight;
      btn.replaceWith(...(data.start > 0 ? [this.olderButton()] : []), ...holder.childNodes);
      this.start = data.start;
      this.body.scrollTop += this.body.scrollHeight - prevHeight;
    }
    /** One poll at a time: send() polls right away while the interval's
     *  poll may be in flight, and two reads from the same offset would
     *  render the same events (your message) twice. A poll asked for
     *  meanwhile runs once the current one lands. */
    async poll() {
      if (!S.live) return;
      if (this.polling) { this.pollAgain = true; return; }
      this.polling = true;
      try {
        do { this.pollAgain = false; await this.pollOnce(); } while (this.pollAgain);
      } finally { this.polling = false; }
    }
    async pollOnce() {
      const epoch = this.epoch, since = this.offset;
      if (!this.loaded) return;
      let data;
      try { data = await api("events", { addr: this.addr, since }); } catch { return; }
      // A load (or retarget) since the request owns the log now.
      if (epoch !== this.epoch || since !== this.offset) return;
      if (!data.events.length) { this.renderer.tick(); return; }
      const stick = this.nearBottom();
      this.offset = data.offset;
      for (const ev of data.events) this.renderer.apply(ev);
      if (stick) this.scrollEnd();
    }
    async refreshDetail() {
      try { this.detail = await api("agent", { addr: this.addr }); } catch { return; }
      if (!this.detail.ok) return;
      this.mergeInbox(this.detail.inbox || []);
      this.updateHeader();
      if (this.inspectorOpen) this.renderInspector();
    }
    /** Messages from other agents (and supervision events) aren't in
     *  the event log; interleave them by timestamp. */
    mergeInbox(envs) {
      const stick = this.nearBottom();
      for (const e of envs) {
        if (this.seenSeq.has(e.seq) || e.from === "@user") continue;
        this.seenSeq.add(e.seq);
        let el;
        if (e.from === "@system" && e.body && e.body.kind === "child_event") {
          el = h("div", { class: "metablk blk", text: `⚙ ${e.body.child_label || e.body.child_addr}: ${e.body.event}${e.body.reason ? " — " + e.body.reason : ""}` });
        } else if (e.kind === "toolkit_request") {
          el = h("div", { class: "msgblk blk" },
            h("div", { class: "from", text: `⚒ ${labelOf(e.from, e.from_label)} requests toolkits` }),
            h("pre", { text: `${JSON.stringify(e.body.toolkits)}${e.body.reason ? "\nreason: " + e.body.reason : ""}` }));
        } else {
          el = h("div", { class: "msgblk blk" },
            h("div", { class: "from", text: `✉ ${labelOf(e.from, e.from_label)} · ${clock(e.ts)}` }),
            typeof e.body === "string"
              ? h("div", { class: "resp" }, renderMarkdown(e.body))
              : h("pre", { text: bodyText(e.body) }));
        }
        el.dataset.ts = e.ts;
        const after = [...this.body.children].find((c) => c.dataset.ts && parseFloat(c.dataset.ts) > e.ts);
        this.body.insertBefore(el, after || null);
      }
      if (stick) this.scrollEnd();
    }
    updateHeader() {
      const node = S.agents.get(this.addr);
      const d = this.detail || {};
      const label = node?.label || d.label || this.addr;
      // Rebuild only when something shown changes: a rebuild between a
      // button's mousedown and mouseup swallows the click.
      const sig = JSON.stringify([label, node?.status, node?.awaiting_permission, node?.model, node?.model_source,
        node?.engine, node?.cost?.toFixed?.(4), d.status, d.model, d.context && Math.round((100 * d.context.used) / (d.context.max || 1)),
        this.inspectorOpen, S.columns.length, S.live]);
      if (sig === this.headSig) return;
      this.headSig = sig;
      this.title.textContent = label;
      const ctx = d.context;
      let ctxEl = null;
      if (ctx && ctx.max) {
        const pct = Math.min(100, (100 * ctx.used) / ctx.max);
        ctxEl = h("span", { class: "meta", title: `context ${kfmt(ctx.used)} / ${kfmt(ctx.max)} tokens` },
          h("span", { class: `ctx${pct > 85 ? " hot" : pct > 60 ? " warn" : ""}` }, h("i", { style: `width:${pct.toFixed(1)}%` })), ` ${pct.toFixed(0)}%`);
      }
      const status = node ? (node.awaiting_permission ? "awaiting permission" : node.status) : d.status || "";
      fill(this.head,
        h("span", { class: `dot ${node ? statusClass(node) : "st-" + (d.status || "idle")}` }),
        modelChip(node || d),
        h("span", { class: "meta", text: [status, node?.cost ? money(node.cost) : null].filter(Boolean).join(" · ") }),
        ctxEl,
        h("div", { class: "actions" },
          h("button", { text: this.inspectorOpen ? "hide info" : "info", onclick: () => this.toggleInspector() }),
          h("button", { text: "files", onclick: () => { select(this.addr, { quiet: true }); setView("files"); } }),
          S.columns.length < MAX_COLUMNS ? h("button", { text: "split", title: "add a column; the next agent you pick shows there (s)", onclick: () => splitColumn() }) : null,
          S.live && node && node.status !== "terminated" ? h("button", { class: "danger", text: "terminate", onclick: () => this.terminate() }) : null,
          S.columns.length > 1 ? h("button", { text: "✕", title: "close column", onclick: () => closeColumn(this) }) : null,
        ),
      );
      this.textarea.disabled = !S.live || node?.status === "terminated";
      const model = (node || d).model;
      this.inputBox.querySelector(".hint span").textContent = node?.status === "terminated"
        ? "agent terminated"
        : `to ${label} · ${model ? shortModel(model) : "CLI default model"}`;
    }
    toggleInspector() {
      this.inspectorOpen = !this.inspectorOpen;
      this.inspector.hidden = !this.inspectorOpen;
      if (this.inspectorOpen) this.renderInspector();
      this.updateHeader();
    }
    renderInspector() { this.inspector.replaceChildren(inspectorContent(this.detail, S.agents.get(this.addr))); }
    renderPermission(reqs) {
      if (!reqs.length) { this.permBox.replaceChildren(); return; }
      const r = reqs[0];
      if (this.permBox.dataset.req === r.req_id) return;
      this.permBox.dataset.req = r.req_id;
      const decide = async (decision, scope) => {
        await post("permission", { req_id: r.req_id, decision, scope });
        this.permBox.replaceChildren();
        this.permBox.dataset.req = "";
        refreshSnapshot();
      };
      this.permBox.replaceChildren(h("div", { class: "perm pane" },
        h("h2", { class: "pane-title", text: `permission · ${toolName(r.tool_name)}${reqs.length > 1 ? ` (+${reqs.length - 1} more)` : ""}` }),
        h("div", { class: "choices" },
          h("button", { class: "allow", text: "allow once", onclick: () => decide("allow", "once") }),
          h("button", { class: "always", text: "allow always", title: "for this agent, this session", onclick: () => decide("allow", "session") }),
          h("button", { class: "deny", text: "deny", onclick: () => decide("deny", null) })),
        h("div", null, h("div", null, h("b", { text: toolName(r.tool_name) }), h("span", { class: "dim", text: ` · ${clock(r.ts)}` })),
          window.ConjureCode
            ? window.ConjureCode.renderCall(r.tool_name, r.args || {}, { renderMarkdown })
            : h("pre", { text: JSON.stringify(r.args, null, 2) }))));
      if (this.nearBottom()) this.scrollEnd();
    }
    async send() {
      const text = this.textarea.value.trim();
      if (!text) return;
      this.textarea.value = "";
      this.textarea.style.height = "auto";
      try { await post("send", { addr: this.addr, body: text }); } catch { this.textarea.value = text; return; }
      this.poll();
    }
    async terminate() {
      const node = S.agents.get(this.addr);
      const n = countDescendants(node);
      if (!confirm(`Terminate ${node?.label || this.addr}${n ? ` and its ${n} descendant(s)` : ""}?`)) return;
      await post("terminate", { addr: this.addr, cascade: true });
      refreshSnapshot();
    }
    retarget(addr) {
      this.addr = addr;
      this.offset = 0;
      this.detail = null;
      this.permBox.replaceChildren();
      this.permBox.dataset.req = "";
      this.load();
    }
  }

  function countDescendants(node) {
    if (!node) return 0;
    return node.children.reduce((n, c) => n + 1 + countDescendants(c), 0);
  }

  function inspectorContent(d, node) {
    if (!d || !d.ok) return h("div", { class: "empty", text: "no details" });
    const frag = document.createDocumentFragment();
    const budget = d.budget != null && node
      ? h("div", null, `${money(node.subtree_cost)} of ${money(d.budget)} (subtree)`,
          h("div", { class: `budget${node.subtree_cost > d.budget ? " over" : ""}` }, h("i", { style: `width:${Math.min(100, (100 * node.subtree_cost) / d.budget).toFixed(1)}%` })))
      : h("span", { class: "dim", text: node ? `uncapped · subtree ${money(node.subtree_cost)}` : "uncapped" });
    const kits = Object.entries(d.toolkits || {});
    const chipLink = (r) => h("span", { class: "chip link", text: r.label, title: r.addr, onclick: () => select(r.addr) });
    frag.append(
      h("dl", { class: "kv" },
        h("dt", { text: "address" }), h("dd", { text: d.addr }),
        h("dt", { text: "status" }), h("dd", { text: d.status }),
        h("dt", { text: "model" }), h("dd", null, modelChip(d)),
        h("dt", { text: "depth" }), h("dd", { text: String(d.depth) }),
        h("dt", { text: "spawned" }), h("dd", { text: clock(d.spawned_at) }),
        h("dt", { text: "budget" }), h("dd", null, budget),
        h("dt", { text: "parent" }), h("dd", null, d.parent ? chipLink(d.parent) : h("span", { class: "dim", text: "— (root)" })),
        h("dt", { text: "children" }), h("dd", null, d.children.length ? h("div", { class: "chips" }, d.children.map(chipLink)) : h("span", { class: "dim", text: "none" })),
        h("dt", { text: "can message" }), h("dd", null, h("div", { class: "chips" }, d.capabilities.map((c) => c.addr.startsWith("@") ? h("span", { class: "chip", text: labelOf(c.addr) }) : chipLink(c)))),
        h("dt", { text: "toolkits" }), h("dd", null, kits.length
          ? h("div", { class: "chips" }, kits.map(([k, g]) => h("span", { class: "chip kit", title: JSON.stringify(g), text: g.bundles || g.tools ? `${k}: ${[...(g.bundles || []).map((b) => "/" + b), ...(g.tools || [])].join(" ")}` : k })))
          : h("span", { class: "dim", text: d.toolbase_loadout ? `loadout ${d.toolbase_loadout}` : "none" })),
        d.may_grant ? [h("dt", { text: "may grant" }), h("dd", null, d.may_grant.length
          ? h("div", { class: "chips" }, d.may_grant.map((k) => h("span", { class: "chip kit", text: k })), h("span", { class: "dim small", text: ` (${d.authority_mode})` }))
          : h("span", { class: "dim", text: `nothing (${d.authority_mode}) — set toolbase.authority or tb activate` }))] : null,
        d.tools?.length ? [h("dt", { text: "tools" }), h("dd", { text: d.tools.join(", ") })] : null,
        Object.keys(d.permissions || {}).length ? [h("dt", { text: "permissions" }), h("dd", { text: Object.entries(d.permissions).map(([k, v]) => `${k}: ${v}`).join(", ") })] : null,
      ),
      h("h3", { text: "role prompt" }),
      h("pre", { text: d.role_prompt || "" }),
    );
    return frag;
  }

  /** Show ``addr`` in the chat view. Like tmux panes: the focused
   *  column (white border) shows whatever you select; ``split`` adds a
   *  column and focuses it. An agent already open in another column is
   *  focused there instead of opened twice. ``newColumn`` (permission
   *  toasts) opens beside the others rather than replacing. */
  function openChat(addr, { newColumn = false, focus = false } = {}) {
    if (!addr) return;
    if (S.view !== "chat") setView("chat");
    const focused = S.columns[S.focusedCol];
    const existing = S.columns.findIndex((c) => c.addr === addr);
    if (focused && focused.addr === addr) {
      // already showing
    } else if (existing >= 0) {
      setFocusedColumn(existing);
    } else if (!S.columns.length || (newColumn && S.columns.length < MAX_COLUMNS)) {
      addColumn(addr);
    } else {
      (focused || S.columns[0]).retarget(addr);
    }
    if (S.selected !== addr) { S.selected = addr; renderTreeSelection(); syncHash(); }
    if (focus) S.columns[S.focusedCol]?.textarea.focus();
    if (S.snap) handlePermissions();
  }
  function addColumn(addr) {
    const col = new ChatColumn(addr);
    S.columns.push(col);
    $("#chat-columns").append(col.el);
    setFocusedColumn(S.columns.length - 1);
    return col;
  }
  /** Split: a new column on the current agent, focused, so the next
   *  agent you pick lands in it. */
  function splitColumn() {
    if (S.columns.length >= MAX_COLUMNS) { toast(`at most ${MAX_COLUMNS} columns`); return; }
    if (S.view !== "chat") setView("chat");
    addColumn(S.selected || S.columns[S.focusedCol]?.addr || S.snap?.tree?.addr);
    toast("split — pick an agent in the tree to show it here", { ttl: 2500 });
  }
  function setFocusedColumn(i) {
    S.focusedCol = Math.max(0, Math.min(i, S.columns.length - 1));
    S.columns.forEach((c, j) => { c.el.classList.toggle("focused", j === S.focusedCol && S.columns.length > 1); c.updateHeader(); });
  }
  function closeColumn(col) {
    const i = S.columns.indexOf(col);
    if (i < 0 || S.columns.length === 1) return;
    S.columns.splice(i, 1);
    col.el.remove();
    setFocusedColumn(Math.min(S.focusedCol, S.columns.length - 1));
    const now = S.columns[S.focusedCol];
    if (now && S.selected !== now.addr) { S.selected = now.addr; renderTreeSelection(); syncHash(); }
    if (S.snap) handlePermissions();
  }

  // ---------- graph ----------
  const Graph = {
    selected: null,
    data: null,
    async refresh() {
      try { this.data = await api("graph"); } catch { return; }
      this.render();
    },
    select(addr) {
      this.selected = addr;
      this.render();
      this.inspect(addr);
    },
    async inspect(addr) {
      const box = $("#graph-inspector");
      if (!addr) { box.replaceChildren(h("div", { class: "empty", text: "select an agent" })); return; }
      let d;
      try { d = await api("agent", { addr }); } catch { return; }
      box.replaceChildren(h("div", { class: "files-head", text: d.label }),
        inspectorContent(d, S.agents.get(addr)),
        h("div", { style: "margin-top:10px" }, h("button", { text: "open chat", onclick: () => openChat(addr) })));
    },
    render() {
      const g = this.data;
      const svg = $("#graph");
      if (!g) return;
      const nodes = g.nodes.filter((n) => S.showInternal || !n.internal);
      const byId = new Map(nodes.map((n) => [n.addr, { ...n, kids: [] }]));
      const roots = [];
      for (const n of byId.values()) {
        const p = n.parent && byId.get(n.parent);
        if (p) p.kids.push(n); else roots.push(n);
      }
      const W = 176, H = 46, GX = 26, GY = 74;
      const usesUser = g.message.some((m) => m.from === "@user");
      let leaf = 0;
      const place = (n, depth) => {
        n.kids.sort((a, b) => (S.agents.get(a.addr)?.spawned_at || 0) - (S.agents.get(b.addr)?.spawned_at || 0));
        n.y = (depth + (usesUser ? 1 : 0)) * (H + GY) + 20;
        if (!n.kids.length) { n.x = leaf++ * (W + GX) + 20; return; }
        n.kids.forEach((k) => place(k, depth + 1));
        n.x = (n.kids[0].x + n.kids[n.kids.length - 1].x) / 2;
      };
      roots.forEach((r) => place(r, 0));
      if (usesUser) byId.set("@user", { addr: "@user", label: "you", status: "idle", x: roots[0]?.x ?? 20, y: 20, kids: [], sentinel: true });
      const width = Math.max(leaf, 1) * (W + GX) + 40;
      const height = Math.max(...[...byId.values()].map((n) => n.y)) + H + 40;
      svg.setAttribute("width", width);
      svg.setAttribute("height", height);
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      const defs = s("defs", null,
        s("marker", { id: "arr-msg", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, s("path", { d: "M0,0 L10,5 L0,10 z", fill: "var(--green)" })),
        s("marker", { id: "arr-cap", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, s("path", { d: "M0,0 L10,5 L0,10 z", fill: "var(--amber)" })));
      const edges = s("g");
      const center = (n) => ({ x: n.x + W / 2, y: n.y + H / 2 });
      const anchor = (a, b) => {
        // Point on a's box border towards b.
        const ca = center(a), cb = center(b);
        const dx = cb.x - ca.x, dy = cb.y - ca.y;
        const sx = dx ? (W / 2) / Math.abs(dx) : Infinity, sy = dy ? (H / 2) / Math.abs(dy) : Infinity;
        const k = Math.min(sx, sy);
        return { x: ca.x + dx * k, y: ca.y + dy * k };
      };
      const curve = (a, b, bend) => {
        const p = anchor(a, b), q = anchor(b, a);
        const mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
        const dx = q.x - p.x, dy = q.y - p.y, len = Math.hypot(dx, dy) || 1;
        const cx = mx - (dy / len) * bend, cy = my + (dx / len) * bend;
        return { d: `M${p.x},${p.y} Q${cx},${cy} ${q.x},${q.y}`, lx: (mx + cx) / 2, ly: (my + cy) / 2 };
      };
      if ($("#edge-spawn").checked) {
        for (const e of g.spawn) {
          const a = byId.get(e.from), b = byId.get(e.to);
          if (!a || !b) continue;
          const p = { x: a.x + W / 2, y: a.y + H }, q = { x: b.x + W / 2, y: b.y };
          const my = (p.y + q.y) / 2;
          edges.append(s("path", { class: "e-spawn", d: `M${p.x},${p.y} C${p.x},${my} ${q.x},${my} ${q.x},${q.y}` }));
        }
      }
      const t = now();
      if ($("#edge-message").checked) {
        for (const m of g.message) {
          const a = byId.get(m.from), b = byId.get(m.to);
          if (!a || !b || a === b) continue;
          const c = curve(a, b, 26);
          const recent = S.live && t - m.last_ts < 6;
          edges.append(s("path", { class: "e-msg" + (recent ? " recent" : ""), d: c.d, "stroke-width": Math.min(4, 1 + Math.log2(m.count)), "marker-end": "url(#arr-msg)" },
            s("title", { text: `${labelOf(m.from)} → ${labelOf(m.to)}: ${m.count} message(s), last ${clock(m.last_ts)}` })));
          if (m.count > 1) edges.append(s("text", { class: "e-label", x: c.lx, y: c.ly, "text-anchor": "middle", text: String(m.count) }));
        }
      }
      if ($("#edge-capability").checked) {
        for (const e of g.capability) {
          const a = byId.get(e.from), b = byId.get(e.to);
          if (!a || !b) continue;
          edges.append(s("path", { class: "e-cap", d: curve(a, b, -40).d, "marker-end": "url(#arr-cap)" },
            s("title", { text: `${labelOf(e.from)} was introduced to ${labelOf(e.to)}` })));
        }
      }
      const nodesG = s("g");
      for (const n of byId.values()) {
        const live = S.agents.get(n.addr);
        const status = live?.awaiting_permission ? "awaiting" : n.status;
        const sub = n.sentinel ? "human" : [live ? (live.model ? shortModel(live.model) : "default model") : null, live?.cost ? money(live.cost) : null, live?.toolkits?.length ? `⚒${live.toolkits.length}` : null].filter(Boolean).join(" · ") || n.status;
        nodesG.append(s("g", {
          class: `g-node ${status}${n.addr === (this.selected || S.selected) ? " selected" : ""}`,
          transform: `translate(${n.x},${n.y})`,
          onclick: () => { if (!n.sentinel) { S.selected = n.addr; renderTreeSelection(); this.select(n.addr); } },
          ondblclick: () => { if (!n.sentinel) openChat(n.addr); },
        },
          s("rect", { width: W, height: H, rx: 9 }),
          s("circle", { cx: 14, cy: 16, r: 4.5, class: "", fill: dotColor(status) }),
          s("text", { x: 26, y: 20, text: clip(n.label, 20) }),
          s("text", { x: 14, y: 37, class: "sub", text: clip(sub, 26) }),
          s("title", { text: `${n.label} (${n.addr}) · ${n.status}` })));
      }
      svg.replaceChildren(defs, edges, nodesG);
    },
  };
  function dotColor(status) {
    return {
      idle: "var(--green)", lazy: "var(--green)", running: "var(--yellow)", error: "var(--red)",
      awaiting: "var(--magenta)", awaiting_permission: "var(--magenta)", terminated: "var(--faint)",
    }[status] || "var(--dim)";
  }

  // ---------- timeline ----------
  const Timeline = {
    data: null,
    t0: null, t1: null,     // visible domain; null = fit
    async refresh() {
      try { this.data = await api("timeline"); } catch { return; }
      this.render();
    },
    domain() {
      const d = this.data;
      const lo = d.t_min ?? now() - 60;
      const hi = Math.max(d.now ?? d.t_max ?? lo + 60, lo + 5);
      if (this.t0 == null) return [lo, hi + (hi - lo) * 0.02];
      if ($("#tl-follow").checked && S.live) {
        const span = this.t1 - this.t0;
        return [hi + span * 0.02 - span, hi + span * 0.02];
      }
      return [this.t0, this.t1];
    },
    render() {
      const d = this.data;
      if (!d) return;
      const wrap = $("#timeline-wrap");
      const svg = $("#timeline");
      const LABEL = 210, ROW = 26, TOP = 26;
      const lanesAll = orderLanes(d.lanes);
      const lanes = lanesAll.filter((l) => S.showInternal || !l.internal);
      const width = Math.max(600, wrap.clientWidth - 4);
      const height = TOP + lanes.length * ROW + 10;
      const [t0, t1] = this.domain();
      this.view = { t0, t1, LABEL, width };
      const x = (t) => LABEL + ((t - t0) / (t1 - t0)) * (width - LABEL - 10);
      svg.setAttribute("width", width);
      svg.setAttribute("height", height);
      const g = s("g");
      // axis
      const axis = s("g", { class: "tl-axis" });
      const step = niceStep((t1 - t0) / Math.max(2, (width - LABEL) / 110));
      for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
        const xx = x(t);
        axis.append(s("line", { x1: xx, x2: xx, y1: TOP - 6, y2: height }), s("text", { x: xx + 3, y: TOP - 10, text: clock(t) }));
      }
      g.append(axis);
      const clipId = "tlclip";
      g.append(s("clipPath", { id: clipId }, s("rect", { x: LABEL, y: 0, width: width - LABEL, height })));
      const plot = s("g", { "clip-path": `url(#${clipId})` });
      lanes.forEach((l, i) => {
        const y = TOP + i * ROW;
        const mid = y + ROW / 2;
        g.append(s("rect", { class: `tl-lane-bg${i % 2 ? " alt" : ""}${l.addr === S.selected ? " selected" : ""}`, x: 0, y, width, height: ROW }));
        g.append(s("text", {
          class: `tl-label${l.status === "terminated" ? " terminated" : ""}`, x: 8 + Math.min(l.depth, 6) * 12, y: mid + 4,
          text: clip(l.label, 24 - Math.min(l.depth, 6)), onclick: () => select(l.addr),
        }));
        const end = l.end ?? (S.live ? now() : d.t_max);
        plot.append(s("line", { class: "tl-life", x1: x(l.start), x2: x(end), y1: mid, y2: mid }));
        for (const turn of l.turns) {
          const te = turn.end ?? (S.live ? now() : end);
          plot.append(s("rect", {
            class: `tl-turn${turn.errored ? " errored" : ""}${turn.end == null ? " open" : ""}`,
            x: x(turn.start), y: y + 6, width: Math.max(2, x(te) - x(turn.start)), height: ROW - 12, rx: 2,
            "data-tip": `${l.label} · turn ${clock(turn.start)} · ${dur(te - turn.start)}${turn.errored ? " · errored" : ""}`,
          }));
        }
        for (const tc of l.tools) {
          const te = tc.done ?? tc.ts;
          plot.append(s("rect", {
            class: `tl-tool${tc.failed ? " failed" : ""}`, x: x(tc.ts) - 1, y: y + 3, width: Math.max(2.5, x(te) - x(tc.ts)), height: ROW - 6, rx: 1,
            "data-tip": `${l.label} · ${toolName(tc.name)} · ${clock(tc.ts)}${tc.done ? " · " + dur(tc.done - tc.ts) : ""}${tc.failed ? " · failed" : ""}`,
          }));
        }
        for (const m of l.messages) {
          const xx = x(m.ts);
          plot.append(s("path", { class: "tl-msg", d: `M${xx - 4},${y + 1} L${xx + 4},${y + 1} L${xx},${y + 7} z`, "data-tip": `${l.label} ← ${labelOf(m.from, m.from_label)} · ${clock(m.ts)}` }));
        }
        for (const e of l.errors) plot.append(s("circle", { class: "tl-err", cx: x(e.ts), cy: mid, r: 4, "data-tip": `${l.label} · error · ${e.text}` }));
      });
      g.append(plot);
      if (S.live) { const xn = x(now()); if (xn > LABEL) g.append(s("line", { class: "tl-now", x1: xn, x2: xn, y1: TOP - 6, y2: height })); }
      else if (S.at != null) { const xa = x(S.at); g.append(s("line", { class: "tl-cursor", x1: xa, x2: xa, y1: TOP - 6, y2: height })); }
      if (!lanes.length) g.append(s("text", { x: 20, y: 60, class: "tl-label", text: "no agents yet" }));
      svg.replaceChildren(g);
    },
    zoom(factor, atX) {
      const v = this.view;
      if (!v) return;
      const frac = Math.min(1, Math.max(0, (atX - v.LABEL) / (v.width - v.LABEL - 10)));
      const pivot = v.t0 + frac * (v.t1 - v.t0);
      const span = Math.max(2, (v.t1 - v.t0) * factor);
      this.t0 = pivot - frac * span;
      this.t1 = this.t0 + span;
      if (factor < 1) $("#tl-follow").checked = false;
      this.render();
    },
    pan(dxPx) {
      const v = this.view;
      if (!v) return;
      const dt = (dxPx / (v.width - v.LABEL - 10)) * (v.t1 - v.t0);
      this.t0 = v.t0 - dt;
      this.t1 = v.t1 - dt;
      $("#tl-follow").checked = false;
      this.render();
    },
    timeAt(clientX) {
      const v = this.view;
      const rect = $("#timeline").getBoundingClientRect();
      const frac = (clientX - rect.left - v.LABEL) / (v.width - v.LABEL - 10);
      return v.t0 + frac * (v.t1 - v.t0);
    },
  };
  function niceStep(raw) {
    for (const st of [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400]) if (st >= raw) return st;
    return 86400;
  }
  function orderLanes(lanes) {
    const byParent = new Map();
    for (const l of lanes) {
      const k = l.parent || "";
      if (!byParent.has(k)) byParent.set(k, []);
      byParent.get(k).push(l);
    }
    const ids = new Set(lanes.map((l) => l.addr));
    const out = [];
    const walk = (k) => (byParent.get(k) || []).sort((a, b) => a.start - b.start).forEach((l) => { out.push(l); walk(l.addr); });
    walk("");
    for (const l of lanes) if (l.parent && !ids.has(l.parent) && !out.includes(l)) { out.push(l); walk(l.addr); }
    return out;
  }

  // ---------- files ----------
  const Files = {
    addr: null, path: "", file: null,
    async open(addr) {
      if (!addr) return;
      if (addr !== this.addr) { this.path = ""; this.file = null; $("#files-content").replaceChildren(h("div", { class: "empty", text: "select a file" })); }
      this.addr = addr;
      $("#files-agent").textContent = labelOf(addr);
      await this.list();
      await this.recent();
    },
    async list() {
      let d;
      try { d = await api("sandbox", { addr: this.addr, path: this.path }); } catch { return; }
      const list = $("#files-list");
      if (!d.ok) { list.replaceChildren(h("div", { class: "dim", text: d.error })); $("#files-crumbs").replaceChildren(); return; }
      const crumbs = [h("a", { text: "sandbox", onclick: () => { this.path = ""; this.list(); } })];
      let acc = "";
      for (const part of (d.path || "").split("/").filter(Boolean)) {
        acc = acc ? `${acc}/${part}` : part;
        const p = acc;
        crumbs.push(" / ", h("a", { text: part, onclick: () => { this.path = p; this.list(); } }));
      }
      $("#files-crumbs").replaceChildren(...crumbs);
      list.replaceChildren(...(d.entries.length ? d.entries.map((e) =>
        h("div", { class: `fentry${e.is_dir ? " dir" : ""}${e.path === this.file ? " selected" : ""}`, onclick: () => e.is_dir ? (this.path = e.path, this.list()) : this.show(e.path) },
          h("span", { class: "nm", text: e.name + (e.is_dir ? "/" : "") }), e.is_dir ? null : h("span", { class: "sz", text: size(e.size) })))
        : [h("div", { class: "dim", text: "empty" })]));
    },
    async recent() {
      let d;
      try { d = await api("sandbox_recent", { addr: this.addr }); } catch { return; }
      $("#files-recent").replaceChildren(...((d.entries || []).length ? d.entries.map((e) =>
        h("div", { class: `fentry${e.path === this.file ? " selected" : ""}`, onclick: () => this.show(e.path), title: clock(e.mtime) },
          h("span", { class: "nm", text: e.path }), h("span", { class: "sz", text: clock(e.mtime) })))
        : [h("div", { class: "dim", text: d.error || "nothing yet" })]));
    },
    async show(path) {
      this.file = path;
      let d;
      try { d = await api("sandbox", { addr: this.addr, path }); } catch { return; }
      const box = $("#files-content");
      if (!d.ok) { box.replaceChildren(h("div", { class: "empty", text: d.error })); return; }
      const lines = d.content.split("\n");
      box.replaceChildren(
        h("div", { class: "file-head" }, h("b", { text: d.path }), ` · ${size(d.size)}${d.truncated ? " · truncated" : ""}`),
        h("pre", { class: "code" }, h("span", { class: "ln", text: lines.map((_, i) => i + 1).join("\n") }), highlightFile(d)));
      this.list();
      this.recent();
    },
  };
  const size = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);

  // ---------- views ----------
  /** ``#<view>/<agent>`` — reloads keep their place, and a link can
   *  point at one agent's chat or timeline. */
  function syncHash() {
    const hash = `#${S.view}${S.selected ? "/" + S.selected : ""}`;
    if (location.hash !== hash) history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
  }
  function setView(view) {
    S.view = view;
    syncHash();
    for (const b of document.querySelectorAll("#views button")) b.classList.toggle("active", b.dataset.view === view);
    for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === `view-${view}`);
    if (view === "graph") { Graph.refresh(); Graph.inspect(S.selected); Graph.selected = S.selected; }
    if (view === "timeline") Timeline.refresh();
    if (view === "files") Files.open(S.selected);
    if (view === "chat" && !S.columns.length && S.selected) openChat(S.selected);
  }

  // ---------- replay scrubber ----------
  const Scrub = {
    playing: null,
    init() {
      $("#scrubber").hidden = false;
      const range = $("#scrub-range");
      let timer = null;
      range.addEventListener("input", () => {
        this.stop();
        this.setLabel(this.timeFor(range.value));
        clearTimeout(timer);
        timer = setTimeout(() => this.goto(this.timeFor(range.value)), 120);
      });
      $("#scrub-play").addEventListener("click", () => (this.playing ? this.stop() : this.play()));
      $("#scrub-end").addEventListener("click", () => { this.stop(); range.value = 1000; this.goto(null); });
      $("#timeline").addEventListener("click", (e) => {
        if (Timeline.dragged) return;
        const t = Timeline.timeAt(e.clientX);
        const [lo, hi] = this.bounds();
        if (t >= lo && t <= hi) { range.value = Math.round(((t - lo) / (hi - lo)) * 1000); this.goto(t); }
      });
      this.setLabel(null);
    },
    bounds() { return [S.meta.started ?? 0, S.meta.last_event ?? 1]; },
    timeFor(v) { const [lo, hi] = this.bounds(); return lo + (v / 1000) * (hi - lo); },
    setLabel(t) {
      const [lo, hi] = this.bounds();
      const at = t ?? hi;
      $("#scrub-time").textContent = `${clock(at)}  +${dur(at - lo)} / ${dur(hi - lo)}`;
    },
    async goto(t) {
      const [, hi] = this.bounds();
      S.at = t == null || t >= hi ? null : t;
      this.setLabel(t);
      await refreshAll();
    },
    play() {
      const range = $("#scrub-range");
      if (Number(range.value) >= 1000) range.value = 0;
      $("#scrub-play").textContent = "❚❚";
      const [lo, hi] = this.bounds();
      let t = this.timeFor(range.value);
      let busy = false;
      this.playing = setInterval(async () => {
        if (busy) return;
        busy = true;
        t += 0.25 * Number($("#scrub-speed").value);
        if (t >= hi) { range.value = 1000; this.stop(); await this.goto(null); busy = false; return; }
        range.value = Math.round(((t - lo) / (hi - lo)) * 1000);
        await this.goto(t);
        busy = false;
      }, 250);
    },
    stop() { clearInterval(this.playing); this.playing = null; $("#scrub-play").textContent = "▶"; },
  };

  async function refreshAll() {
    await refreshSnapshot();
    await Promise.all(S.columns.map((c) => c.load()));
    if (S.view === "graph") Graph.refresh();
    if (S.view === "timeline") Timeline.refresh();
  }

  // ---------- wiring ----------
  function wire() {
    for (const b of document.querySelectorAll("#views button")) b.addEventListener("click", () => setView(b.dataset.view));
    $("#auto-toggle").addEventListener("click", toggleAuto);
    $("#help-toggle").addEventListener("click", () => ($("#help").hidden = !$("#help").hidden));
    // Close on the backdrop or ✕, not on clicks inside the card.
    $("#help").addEventListener("click", (e) => { if (e.target === e.currentTarget) $("#help").hidden = true; });
    $("#help-close").addEventListener("click", () => ($("#help").hidden = true));
    $("#run-toggle").addEventListener("click", () => RunRecord.open());
    $("#run-close").addEventListener("click", () => ($("#run").hidden = true));
    $("#run").addEventListener("click", (e) => { if (e.target === e.currentTarget) $("#run").hidden = true; });
    $("#run-export").addEventListener("click", () => RunRecord.download());
    $("#theme-toggle").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      store.set("conjure-theme", next);
      if (S.view === "graph") Graph.render();
    });
    $("#show-internal").addEventListener("change", (e) => {
      S.showInternal = e.target.checked;
      refreshSnapshot();
      if (S.view === "graph") Graph.render();
      if (S.view === "timeline") Timeline.render();
    });
    for (const id of ["#edge-spawn", "#edge-message", "#edge-capability"]) $(id).addEventListener("change", () => Graph.render());
    $("#tl-fit").addEventListener("click", () => { Timeline.t0 = Timeline.t1 = null; $("#tl-follow").checked = true; Timeline.render(); });
    $("#tl-follow").addEventListener("change", () => Timeline.render());

    const wrap = $("#timeline-wrap");
    wrap.addEventListener("wheel", (e) => {
      if (!Timeline.view) return;
      e.preventDefault();
      if (Timeline.t0 == null) { Timeline.t0 = Timeline.view.t0; Timeline.t1 = Timeline.view.t1; }
      const rect = $("#timeline").getBoundingClientRect();
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) Timeline.pan(-e.deltaX);
      else Timeline.zoom(e.deltaY > 0 ? 1.2 : 1 / 1.2, e.clientX - rect.left);
    }, { passive: false });
    let drag = null;
    wrap.addEventListener("mousedown", (e) => { drag = { x: e.clientX, moved: false }; Timeline.dragged = false; });
    window.addEventListener("mousemove", (e) => {
      if (drag) {
        const dx = e.clientX - drag.x;
        if (Math.abs(dx) > 3) { drag.moved = true; Timeline.dragged = true; wrap.classList.add("dragging"); }
        if (drag.moved) {
          if (Timeline.t0 == null && Timeline.view) { Timeline.t0 = Timeline.view.t0; Timeline.t1 = Timeline.view.t1; }
          Timeline.pan(dx);
          drag.x = e.clientX;
        }
      }
      const tip = $("#tl-tip");
      const target = e.target.closest?.("[data-tip]");
      if (target && S.view === "timeline") {
        tip.textContent = target.getAttribute("data-tip");
        tip.style.display = "block";
        tip.style.left = `${e.clientX + 12}px`;
        tip.style.top = `${e.clientY + 12}px`;
      } else tip.style.display = "none";
    });
    window.addEventListener("mouseup", () => { drag = null; wrap.classList.remove("dragging"); setTimeout(() => (Timeline.dragged = false), 0); });

    // sidebar resize
    const saved = store.get("conjure-sidebar");
    if (saved) document.documentElement.style.setProperty("--sidebar-w", saved);
    let rz = false;
    $("#resizer").addEventListener("mousedown", (e) => { rz = true; e.preventDefault(); });
    window.addEventListener("mousemove", (e) => {
      if (!rz) return;
      const w = `${Math.min(60, Math.max(16, (100 * e.clientX) / window.innerWidth)).toFixed(1)}%`;
      document.documentElement.style.setProperty("--sidebar-w", w);
    });
    window.addEventListener("mouseup", () => {
      if (!rz) return;
      rz = false;
      store.set("conjure-sidebar", getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w").trim());
      if (S.view === "timeline") Timeline.render();
    });
    window.addEventListener("resize", () => { if (S.view === "timeline") Timeline.render(); });

    document.addEventListener("keydown", (e) => {
      const typing = /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement?.tagName);
      if (e.key === "F6" || (e.ctrlKey && e.key.toLowerCase() === "g")) { e.preventDefault(); toggleAuto(); return; }
      if (typing) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      switch (e.key) {
        case "1": setView("chat"); break;
        case "2": setView("graph"); break;
        case "3": setView("timeline"); break;
        case "4": setView("files"); break;
        case "j": case "ArrowDown": e.preventDefault(); moveSelection(1); break;
        case "k": case "ArrowUp": e.preventDefault(); moveSelection(-1); break;
        case "Enter": e.preventDefault(); openChat(S.selected, { focus: true }); break;
        case "s": splitColumn(); break;
        case "?": $("#help").hidden = !$("#help").hidden; break;
        case "Escape": $("#help").hidden = true; $("#run").hidden = true; break;
        case "r": $("#run").hidden ? RunRecord.open() : ($("#run").hidden = true); break;
        case " ": if (!S.live) { e.preventDefault(); Scrub.playing ? Scrub.stop() : Scrub.play(); } break;
      }
    });
  }

  // ---------- run record ----------
  /** Provenance and totals for the whole session (manifest + summary),
   *  and the export download. */
  const RunRecord = {
    async open() {
      $("#run").hidden = false;
      const body = $("#run-body");
      let data;
      try { data = await api("run"); } catch (e) { fill(body, h("p", { class: "dim", text: `couldn't load: ${e.message || e}` })); return; }
      if (!data.ok) { fill(body, h("p", { class: "dim", text: data.error || "no record" })); return; }
      this.render(body, data.manifest || {}, data.summary || {});
    },
    render(body, m, sm) {
      const t = sm.totals || {};
      const env = m.environment || {};
      const stat = (v, k) => h("div", { class: "run-stat" }, h("div", { class: "v", text: v }), h("div", { class: "k", text: k }));
      const secs = (x) => (x == null ? "—" : dur(x));
      const ex = m.export;
      const agents = (sm.agents || []).filter((a) => !a.internal);
      const row = (a) => h("tr", { onclick: () => { $("#run").hidden = true; setView("chat"); openChat(a.id, { focus: false }); } },
        h("td", { text: a.label || a.id }),
        h("td", { text: ENGINE_NAMES[a.engine] || a.engine || "—" }),
        h("td", { text: a.model ? shortModel(a.model) : "—", title: a.model_source ? `${a.model} (${a.model_source})` : "" }),
        h("td", { class: "num", text: a.turns ?? 0 }),
        h("td", { class: "num", text: `${a.tool_calls ?? 0}${a.tool_errors ? ` (${a.tool_errors}✗)` : ""}` }),
        h("td", { class: "num", text: kfmt(a.tokens_out || 0) }),
        h("td", { class: "num", text: secs(a.active_s) }),
        h("td", { class: "num", text: money(a.cost_usd || 0) }));
      const kv = (pairs) => h("dl", { class: "run-kv" }, pairs.filter(([, v]) => v != null && v !== "").flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: String(v) })]));
      const pkgs = Object.entries(env.packages || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
      const clis = Object.entries(env.clis || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
      const kits = Object.entries(m.toolbase?.toolkits || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
      fill(body,
        ex ? h("p", { class: "run-note", text: `Exported ${ex.exported_at_iso || ""} by conjure ${ex.conjure || ""}${ex.scrubbed ? " · paths scrubbed" : ""}${ex.contents?.omitted_files?.length ? ` · ${ex.contents.omitted_files.length} large file(s) not included` : ""}` }) : null,
        h("section", null, h("h3", { text: "Run" }), h("div", { class: "run-stats" },
          stat(clock(sm.started_at || m.started_at || 0), "started"),
          stat(secs(sm.wall_s), "wall time"),
          stat(secs(t.active_s), "agent time"),
          stat(String(sm.agent_count ?? 0), "agents"),
          stat(String(sm.messages ?? 0), "messages"),
          stat(`${t.turns ?? 0}${t.errored_turns ? ` (${t.errored_turns}✗)` : ""}`, "turns"),
          stat(`${t.tool_calls ?? 0}${t.tool_errors ? ` (${t.tool_errors}✗)` : ""}`, "tool calls"),
          stat(kfmt(t.tokens_out || 0), "output tokens"),
          stat(money(t.cost_usd || 0), "cost"))),
        h("section", null, h("h3", { text: "Agents" }), h("div", { class: "run-table-wrap" }, h("table", { class: "run-table" },
          h("thead", null, h("tr", null, ["agent", "engine", "model"].map((x) => h("th", { text: x })), ["turns", "tool calls", "tokens", "active", "cost"].map((x) => h("th", { class: "num", text: x })))),
          h("tbody", null, agents.map(row))))),
        h("section", null, h("h3", { text: "Environment" }), kv([
          ["session", m.session || sm.session], ["started", m.started_at_iso], ["platform", env.platform], ["python", env.python],
          ["packages", pkgs], ["CLIs", clis], ["toolbase project", m.toolbase?.project_root], ["toolkits", kits],
        ])),
        m.config ? h("section", null, h("details", null, h("summary", { text: "Config (frozen at start)" }), h("pre", { text: JSON.stringify(m.config, null, 2) }))) : null,
        !m.session ? h("p", { class: "dim", text: "No manifest: this session predates run records. Totals come from its journal and logs." }) : null,
      );
    },
    async download() {
      const btn = $("#run-export");
      btn.disabled = true; btn.textContent = "exporting…";
      try {
        const res = await fetch("/api/export", { headers: { "X-Conjure-Token": S.token } });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
        const blob = await res.blob();
        const name = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "conjure-export.tar.gz";
        const a = h("a", { href: URL.createObjectURL(blob), download: name });
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      } catch (e) {
        toast(`export failed: ${e.message || e}`, { err: true });
      } finally {
        btn.disabled = false; btn.textContent = "export";
      }
    },
  };

  async function toggleAuto() {
    if (!S.live) return;
    const on = !S.snap?.auto_mode;
    await post("auto_mode", { on });
    refreshSnapshot();
  }

  // ---------- loops ----------
  function loops() {
    if (!S.live) return;
    let snapBusy = false, evBusy = false, detBusy = false, viewBusy = false;
    setInterval(async () => { if (snapBusy) return; snapBusy = true; await refreshSnapshot(); snapBusy = false; }, POLL_SNAPSHOT_MS);
    setInterval(async () => { if (evBusy) return; evBusy = true; await Promise.all(S.columns.map((c) => c.poll())); evBusy = false; }, POLL_EVENTS_MS);
    setInterval(async () => { if (detBusy) return; detBusy = true; await Promise.all(S.columns.map((c) => c.refreshDetail())); detBusy = false; }, POLL_DETAIL_MS);
    setInterval(async () => {
      if (viewBusy || document.hidden) return;
      viewBusy = true;
      if (S.view === "graph") await Graph.refresh();
      else if (S.view === "timeline") await Timeline.refresh();
      else if (S.view === "files" && Files.addr) await Files.recent();
      viewBusy = false;
    }, POLL_VIEW_MS);
  }

  // ---------- boot ----------
  async function boot() {
    const theme = store.get("conjure-theme");
    if (theme) document.documentElement.dataset.theme = theme;
    else if (window.matchMedia?.("(prefers-color-scheme: light)").matches) document.documentElement.dataset.theme = "light";

    const url = new URL(location.href);
    const fromUrl = url.searchParams.get("token");
    try {
      if (fromUrl) sessionStorage.setItem("conjure-token", fromUrl);
      S.token = fromUrl || sessionStorage.getItem("conjure-token");
    } catch { S.token = fromUrl; }
    if (fromUrl) { url.searchParams.delete("token"); history.replaceState(null, "", url.pathname + url.search + url.hash); }
    if (!S.token) { showGate(); return; }

    wire();
    try { S.meta = await api("meta"); } catch { return; }
    S.live = S.meta.live;
    document.title = `conjure · ${S.meta.session}`;
    $("#session-name").textContent = S.meta.session;
    const badge = $("#mode-badge");
    badge.textContent = S.live ? "live" : "replay";
    badge.className = `badge ${S.live ? "live" : "replay"}`;
    if (!S.live) { $("#auto-toggle").hidden = true; Scrub.init(); }
    const [hashView, hashAgent] = location.hash.slice(1).split("/");
    if (hashAgent) S.selected = hashAgent;
    await refreshSnapshot();
    if (hashAgent && S.agents.has(hashAgent)) openChat(hashAgent);
    if (["graph", "timeline", "files"].includes(hashView)) setView(hashView);
    loops();
  }
  boot();
})();
