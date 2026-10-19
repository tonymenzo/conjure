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
    filePath: null,         // the files view's open file, and its workspace
    fileWs: null,
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
  /** A small line icon that takes the text colour (glyphs like ✉ blur
   *  into a box at this size). */
  function icon(name) {
    const paths = {
      mail: [["rect", { x: 1.75, y: 3.25, width: 12.5, height: 9.5, rx: 1.6 }], ["path", { d: "M2.5 4.5 8 8.75 13.5 4.5" }]],
    }[name];
    const el = document.createElementNS(SVG, "svg");
    for (const [k, v] of Object.entries({ class: `ico ico-${name}`, viewBox: "0 0 16 16", width: 13, height: 13, fill: "none", stroke: "currentColor", "stroke-width": 1.4, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) el.setAttribute(k, v);
    for (const [tag, attrs] of paths) {
      const part = document.createElementNS(SVG, tag);
      for (const [k, v] of Object.entries(attrs)) part.setAttribute(k, v);
      el.append(part);
    }
    return el;
  }
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

  /** Set while a file's markdown renders: ``image(src, alt)`` and
   *  ``link(href, label)`` resolve relative targets (files.js). */
  let MD_CTX = null;
  function renderMarkdown(text, ctx) {
    if (ctx === undefined) return markdownBlocks(text);
    const prev = MD_CTX;
    MD_CTX = ctx;
    try { return markdownBlocks(text); } finally { MD_CTX = prev; }
  }
  function markdownBlocks(text) {
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
    /!\[([^\]\n]*)\]\(<?([^\s)>]+)>?(?:\s+"[^"\n]*")?\)/,       // 16,17 image
    /\[([^\]\n]+)\]\(<?([^\s)>:]+)>?\)/,                    // 18,19 relative link
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
      else if (m[16] !== undefined) {
        const [alt, src] = [m[16], m[17]];
        if (MD_CTX?.image) el.append(MD_CTX.image(src, alt));
        else if (/^https?:\/\//.test(src)) el.append(h("a", { href: src, target: "_blank", rel: "noopener noreferrer", text: alt || src }));
        else el.append(h("span", { class: "md-rel", title: src, text: alt || src }));
      } else if (m[18] !== undefined) {
        if (MD_CTX?.link) el.append(MD_CTX.link(m[19], m[18]));
        else el.append(inline(h("span", { class: "md-rel", title: m[19] }), m[18]));
      }
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

  // Tool calls: one summary line (the command, file or target); the
  // code, diff or output (code.js) one click away.
  function toolCallBlock(name, original, roots, schemas) {
    const CC = window.ConjureCode;
    const label = toolName(name);
    const args = CC ? CC.relArgs(original, roots) : original;
    if (!CC) {
      return h("details", { class: "toolcall" },
        h("summary", null, h("span", { class: "glyph", text: "⏺ " }), h("span", { class: "name", text: label }),
          h("span", { class: "args", text: `(${argsPreview(args)})` })),
        h("pre", { text: JSON.stringify(args, null, 2) }));
    }
    const kind = CC.toolKind(name, args);
    // The tool's argument schema, when the engine reported one: previews
    // then lead with required arguments and skip defaults.
    const schema = schemas?.[name] || schemas?.[String(name).replace(/^mcp__(conjure|spawn|toolbase)__/, "")] || null;
    const sum = CC.callSummary(name, args, { schema });
    // Agents by name, not address (Send → worker, WaitFor value="worker").
    const named = (t) => (t ? String(t).replace(/\bag-[a-z0-9]{8,}\b/g, (id) => (S.agents.has(id) ? labelOf(id) : id)) : t);
    if (sum) { sum.text = named(sum.text); sum.extra = named(sum.extra); }
    // The summary: the call's line, and under it while collapsed a
    // line or two of what it writes or says.
    const line = h("span", { class: "callhead" },
      h("span", { class: "glyph", text: "⏺ " }),
      h("span", { class: "name", text: label }),
      sum
        ? [h("span", { class: "target" + (kind === "shell" ? " cmd" : "") + (sum.dim ? " dimmed" : ""), text: `${sum.prefix ? sum.prefix + " " : ""}${sum.text}` }),
           sum.extra ? h("span", { class: "args", text: sum.extra }) : null]
        : h("span", { class: "args", text: `(${argsPreview(args)})` }));
    const preview = CC.callPreview(name, args, { schema });
    for (const el of preview?.querySelectorAll(".pv-val") || []) el.textContent = named(el.textContent);
    const head = h("summary", null, line, preview);
    const fullPath = original && (original.file_path ?? original.path);
    if (typeof fullPath === "string" && fullPath !== (args.file_path ?? args.path)) head.title = fullPath;
    return h("details", { class: `toolcall rich kind-${kind}` },
      head, CC.renderCall(name, args, { renderMarkdown, original }));
  }
  /** An agent's sandbox paths, longest first (a nested sandbox before
   *  its parent's). */
  function sandboxRoots(detail) {
    const paths = (detail && detail.ok && detail.sandbox_paths) || [];
    return [...paths].filter((p) => p && p.length > 1).sort((a, b) => b.length - a.length);
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
  /** A file's bytes (the files view's images, PDFs and downloads). */
  async function apiBlob(path, params = {}) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v != null && v !== "") q.set(k, v);
    if (!S.live && S.at != null) q.set("at", S.at);
    const res = await fetch(`/api/${path}?${q}`, { headers: { "X-Conjure-Token": S.token } });
    if (res.status === 401) { showGate(); throw new Error("unauthorized"); }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || res.statusText);
    }
    return res.blob();
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
    // Agents that share a name get a number, in spawn order, wherever
    // they're named (tree, chat, graph, timeline, run record).
    const byLabel = new Map();
    for (const n of S.agents.values()) {
      if (!byLabel.has(n.label)) byLabel.set(n.label, []);
      byLabel.get(n.label).push(n);
    }
    for (const group of byLabel.values()) {
      if (group.length < 2) continue;
      group.sort((a, b) => (a.spawned_at || 0) - (b.spawned_at || 0) || a.addr.localeCompare(b.addr));
      group.forEach((n, i) => { n.base_label = n.label; n.label = `${n.label} #${i + 1}`; });
    }
    S.topRoots = topRoots();
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
      if (node.model) meta.push(shortModel(node.model));
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

  /** Every agent's workspace paths that aren't inside another one,
   *  longest first: messages show paths relative to the team's workspace. */
  function topRoots() {
    const all = new Set();
    for (const n of S.agents.values()) for (const p of n.sandbox_paths || []) if (p && p.length > 1) all.add(p);
    const paths = [...all];
    return paths.filter((p) => !paths.some((q) => q !== p && p.startsWith(q + "/"))).sort((a, b) => b.length - a.length);
  }
  /** Open a path from a message (relative to the root's workspace) in the files view. */
  function openPath(path) {
    if (!S.snap?.tree) return;
    S.filePath = path;
    S.fileWs = S.snap.tree.addr;
    setView("files");
  }

  // ---------- activity feed ----------
  // One line per event (who, and a summary); a message opens in place.
  const ACT_FILTERS = (() => {
    try { return new Set(JSON.parse(localStorage.getItem("conjure-activity-filters")) || ["msg", "tool", "life"]); }
    catch { return new Set(["msg", "tool", "life"]); }
  })();
  const actOpen = new Set();
  function activityRows() {
    const CC = window.ConjureCode;
    const rows = [];
    (S.snap.activity || []).forEach((a, i) => {
      const kind = a.headers?.kind;
      const key = `m:${a.ts}:${a.from}:${a.to}:${i}`;
      if (a.from === "@system" && typeof a.body === "object" && a.body?.kind === "child_event") {
        const ev = String(a.body.event || "");
        const tone = /error|fail|exhaust|crash/i.test(ev) ? "bad" : /terminat|ended|stopp/i.test(ev) ? "ended" : "ok";
        rows.push({ key, ts: a.ts, cat: "life", tone, who: labelOf(a.body.child_addr, a.body.child_label), body: `${ev}${a.body.reason ? " — " + a.body.reason.replace(/\bag-[a-z0-9]{8,}\b/g, (id) => labelOf(id)) : ""}` });
        return;
      }
      if (kind === "toolkit_request") {
        rows.push({ key, ts: a.ts, cat: "msg", tone: "req", who: labelOf(a.from, a.from_label), to: labelOf(a.to, a.to_label),
          body: `requests toolkits: ${Object.keys(a.body?.toolkits || {}).join(", ")}`, msg: a });
        return;
      }
      rows.push({ key, ts: a.ts, cat: "msg", who: labelOf(a.from, a.from_label), to: labelOf(a.to, a.to_label),
        body: CC ? CC.bodySummary(a.body, S.topRoots) : clip(bodyText(a.body), 160), msg: a });
    });
    for (const e of S.snap.tool_events || []) {
      if (e.kind !== "tool_call") continue;
      rows.push({ key: `t:${e.ts}:${e.from}:${e.tool}`, ts: e.ts, cat: "tool", who: labelOf(e.from, e.from_label), body: toolName(e.tool) });
    }
    rows.sort((a, b) => a.ts - b.ts);
    return rows.filter((r) => ACT_FILTERS.has(r.cat)).slice(-150);
  }
  function renderActivity() {
    const box = $("#activity");
    for (const b of document.querySelectorAll(".act-filters button")) b.classList.toggle("active", ACT_FILTERS.has(b.dataset.f));
    const rows = activityRows();
    const sig = JSON.stringify([rows.map((r) => r.key), [...ACT_FILTERS], [...actOpen], S.agents.size]);
    if (sig === renderActivity.sig) return;
    renderActivity.sig = sig;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
    const CC = window.ConjureCode;
    fill(box, rows.length ? rows.map((r) => {
      const open = actOpen.has(r.key);
      const glyph = { msg: icon("mail"), tool: "⏺", life: "●" }[r.cat];
      // A message: who → whom, and the summary under it; anything else on one line.
      const line = h("div", { class: `act-line${r.msg ? " two" : ""}` },
        h("span", { class: "t", text: clock(r.ts) }),
        h("span", { class: `g ${r.cat} ${r.tone || ""}`, "aria-hidden": "true" }, r.tone === "req" ? "⚒" : glyph),
        h("span", { class: "who" }, r.who, r.to ? h("span", { class: "arrow", text: " → " }) : null, r.to || null),
        h("span", { class: "sum", text: r.body }));
      const el = h("div", { class: `act ${r.cat}${r.tone ? " " + r.tone : ""}${open ? " open" : ""}${r.msg ? " expandable" : ""}`, dataset: { key: r.key } }, line);
      if (r.msg) {
        line.title = open ? "click to fold" : "click to read the message";
        line.addEventListener("click", () => { if (!actOpen.delete(r.key)) actOpen.add(r.key); renderActivity(); });
        if (open) {
          el.append(h("div", { class: "act-body" }, CC
            ? CC.messageView(r.msg.body, { roots: S.topRoots, renderMarkdown, openPath })
            : h("pre", { text: bodyText(r.msg.body) })));
        }
      }
      return el;
    }) : h("div", { class: "act-empty", text: ACT_FILTERS.size ? "nothing yet" : "every kind is filtered out" }));
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
      this.roots = [];      // the agent's sandbox paths (relative display)
      this.schemas = {};    // tool name -> {order, required, defaults}
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
        const el = this.block(toolCallBlock(name, args, this.roots, this.schemas), ts, true);
        this.pendingCalls.push({ name, args: window.ConjureCode ? window.ConjureCode.relArgs(args, this.roots) : args, el });
      }
    }
    toolResult(ev) {
      const text = ev.text || "";
      const failed = !!ev.failed;
      const CC = window.ConjureCode;
      if (CC) {
        let call = (this.pendingCalls || []).shift() || null;
        if (call && !resultFits(call, text)) call = null; // out of step: judge by shape
        const target = call?.el?.matches?.("details.toolcall.rich") ? call.el : call?.el?.querySelector?.("details.toolcall.rich");
        if (target) {
          // Fold the result into its call: a status chip on the call's
          // line, the output inside it.
          const res = CC.resultForCall(text, failed, call);
          const summary = target.querySelector(":scope > summary");
          (summary.querySelector(":scope > .callhead") || summary).append(h("span", { class: "res-chip" + (res.bad ? " bad" : ""), text: res.chip }));
          if (res.bad) target.classList.add("failed");
          if (res.body) target.append(h("div", { class: "resbody" }, res.body));
          return;
        }
        const r = CC.renderResult(text, failed, call);
        if (r) {
          const bad = r.failed ?? failed;
          const cls = "toolres rich" + (bad ? " failed" : "");
          const summary = r.summary || summarizeResult(text, failed) || "result";
          this.block(r.el
            ? h("details", { class: cls }, h("summary", { text: summary }), r.el)
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
      try {
        const [ev, detail] = await Promise.all([
          api("events", { addr: this.addr }),
          api("agent", { addr: this.addr }).catch(() => null),
        ]);
        data = ev;
        // Paths in tool calls show relative to the agent's sandbox.
        this.renderer.roots = sandboxRoots(detail);
        this.renderer.schemas = (detail && detail.tool_schemas) || {};
      } catch { return; }
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
      r.roots = this.renderer.roots;
      r.schemas = this.renderer.schemas;
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
      if (this.detail.tool_schemas && this.renderer) this.renderer.schemas = this.detail.tool_schemas;   // a live grant adds tools
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
          const reason = e.body.reason ? String(e.body.reason).replace(/\bag-[a-z0-9]{8,}\b/g, (id) => labelOf(id)) : "";
          el = h("div", { class: "metablk blk", text: `⚙ ${labelOf(e.body.child_addr, e.body.child_label || e.body.child_addr)}: ${e.body.event}${reason ? " — " + reason : ""}` });
        } else if (e.kind === "toolkit_request") {
          el = h("div", { class: "msgblk blk" },
            h("div", { class: "from", text: `⚒ ${labelOf(e.from, e.from_label)} requests toolkits` }),
            h("pre", { text: `${JSON.stringify(e.body.toolkits)}${e.body.reason ? "\nreason: " + e.body.reason : ""}` }));
        } else {
          el = h("div", { class: "msgblk blk" },
            h("div", { class: "from" }, icon("mail"), ` ${labelOf(e.from, e.from_label)} · ${clock(e.ts)}`),
            window.ConjureCode
              ? window.ConjureCode.messageView(e.body, { roots: S.topRoots, renderMarkdown, openPath })
              : typeof e.body === "string" ? h("div", { class: "resp" }, renderMarkdown(e.body)) : h("pre", { text: bodyText(e.body) }));
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
    const chipLink = (r) => h("span", { class: "chip link", text: labelOf(r.addr, r.label), title: r.addr, onclick: () => select(r.addr) });
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
  // ---------- graph (graph.js) ----------
  const GraphView = window.ConjureGraph({
    s, api, labelOf, clock, now, money, shortModel, clip, dotColor,
    ui: () => ({ showInternal: S.showInternal, agents: S.agents, live: S.live }),
    onSelect: (addr) => { S.selected = addr; renderTreeSelection(); syncHash(); Graph.select(addr); },
    onOpen: (addr) => openChat(addr),
  });
  const Graph = {
    refresh: () => GraphView.refresh(),
    render: () => GraphView.render(),
    key: (e) => GraphView.key(e),
    select(addr) {
      GraphView.select(addr);
      this.inspect(addr);
    },
    set selected(addr) { GraphView.selected = addr; },
    async inspect(addr) {
      const box = $("#graph-inspector-body");
      if (!addr) { box.replaceChildren(h("div", { class: "empty", text: "select an agent" })); return; }
      let d;
      try { d = await api("agent", { addr }); } catch { return; }
      box.replaceChildren(h("div", { class: "insp-title" }, h("span", { class: "dot", style: `background: ${dotColor(S.agents.get(addr)?.status || d.status)}` }), h("b", { text: labelOf(addr, d.label) })),
        inspectorContent(d, S.agents.get(addr)),
        h("div", { class: "insp-actions" },
          h("button", { text: "open chat", onclick: () => openChat(addr) }),
          h("button", { text: "files", onclick: () => setView("files") })));
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
      const LABEL = 236, TOP = 30;
      const lanes = orderLanes(d.lanes).filter((l) => S.showInternal || !l.internal);
      const width = Math.max(600, wrap.clientWidth - 2);
      // Rows grow to fill the pane when the team is small.
      const ROW = Math.round(Math.max(30, Math.min(46, lanes.length ? (wrap.clientHeight - TOP - 16) / lanes.length : 40)));
      const height = Math.max(TOP + lanes.length * ROW + 16, wrap.clientHeight - 2);
      const [t0, t1] = this.domain();
      this.view = { t0, t1, LABEL, width };
      const PW = width - LABEL - 16;
      const x = (t) => LABEL + ((t - t0) / (t1 - t0)) * PW;
      svg.setAttribute("width", width);
      svg.setAttribute("height", height);
      const span = $("#tl-span");
      if (span) span.textContent = dur(t1 - t0);
      const idx = new Map(lanes.map((l, i) => [l.addr, i]));
      const yOf = (i) => TOP + i * ROW;
      const g = s("g");
      g.append(s("defs", null, s("marker", { id: "tl-arr", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto" },
        s("path", { d: "M0,1 L10,5 L0,9 z", fill: "var(--amber)" }))));
      lanes.forEach((l, i) => g.append(s("rect", {
        class: `tl-lane-bg${i % 2 ? " alt" : ""}${l.addr === S.selected ? " selected" : ""}`, x: 0, y: yOf(i), width, height: ROW,
      })));
      // Time axis and its grid.
      const axis = s("g", { class: "tl-axis" });
      const step = niceStep((t1 - t0) / Math.max(2, PW / 120));
      const fmt = (t) => (step >= 60 ? clock(t).slice(0, 5) : clock(t));
      for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
        const xx = x(t);
        if (xx < LABEL) continue;
        axis.append(s("line", { class: "grid", x1: xx, x2: xx, y1: TOP - 4, y2: height }), s("text", { x: xx + 4, y: TOP - 10, text: fmt(t) }));
      }
      axis.append(s("line", { class: "split", x1: LABEL - 8, x2: LABEL - 8, y1: 0, y2: height }));
      g.append(axis);
      // Lane labels: the agent tree, as in the sidebar.
      const indent = (depth) => 12 + Math.min(depth, 8) * 14;
      const labels = s("g", { class: "tl-labels" });
      lanes.forEach((l, i) => {
        const mid = yOf(i) + ROW / 2;
        const pi = l.parent != null ? idx.get(l.parent) : undefined;
        if (pi !== undefined) {
          const gx = indent(lanes[pi].depth) + 4;
          labels.append(s("path", { class: "tl-guide", d: `M${gx},${yOf(pi) + ROW / 2 + 6} V${mid - 4} Q${gx},${mid} ${gx + 4},${mid} H${indent(l.depth) - 2}` }));
        }
        const node = S.agents.get(l.addr);
        const status = node?.awaiting_permission ? "awaiting" : (node?.status || l.status);
        const nx = indent(l.depth) + 13;
        labels.append(
          s("circle", { cx: indent(l.depth) + 4, cy: mid, r: 3.5, fill: dotColor(status) }),
          s("text", {
            class: `tl-label${l.status === "terminated" ? " terminated" : ""}${l.addr === S.selected ? " selected" : ""}`, x: nx, y: mid + 4,
            text: clip(labelOf(l.addr, l.label), Math.max(6, Math.floor((LABEL - 20 - nx) / 7.3))), onclick: () => select(l.addr),
          }, s("title", { text: `${labelOf(l.addr, l.label)}${node?.model ? ` · ${node.model}` : ""} · ${status}` })));
      });
      g.append(labels);
      g.append(s("clipPath", { id: "tlclip" }, s("rect", { x: LABEL, y: 0, width: width - LABEL, height })));
      const plot = s("g", { "clip-path": "url(#tlclip)" });
      const arrows = s("g", { class: "tl-arrows" });
      lanes.forEach((l, i) => {
        const y = yOf(i), mid = y + ROW / 2;
        const name = labelOf(l.addr, l.label);
        const end = l.end ?? (S.live ? now() : d.t_max);
        plot.append(s("line", { class: "tl-life", x1: x(l.start), x2: x(end), y1: mid, y2: mid }));
        for (const turn of l.turns) {
          const te = turn.end ?? (S.live ? now() : end);
          plot.append(s("rect", {
            class: `tl-turn${turn.errored ? " errored" : ""}${turn.end == null ? " open" : ""}`,
            x: x(turn.start), y: mid - 8, width: Math.max(2, x(te) - x(turn.start)), height: 16, rx: 3,
            "data-tip": `${name} · turn ${clock(turn.start)} · ${dur(te - turn.start)}${turn.errored ? " · errored" : ""}`,
          }));
        }
        // Tool calls as a thin strip inside the turn; failures stand out.
        for (const tc of l.tools) {
          const te = tc.done ?? tc.ts;
          plot.append(s("rect", {
            class: `tl-tool${tc.failed ? " failed" : ""}`,
            x: x(tc.ts) - (tc.failed ? 1 : 0.5), y: tc.failed ? mid - 8 : mid - 3, width: Math.max(tc.failed ? 2.5 : 1.5, x(te) - x(tc.ts)), height: tc.failed ? 16 : 6,
            "data-tip": `${name} · ${toolName(tc.name)} · ${clock(tc.ts)}${tc.done ? " · " + dur(tc.done - tc.ts) : ""}${tc.failed ? " · failed" : ""}`,
          }));
        }
        for (const m of l.messages) {
          const xx = x(m.ts);
          const si = idx.get(m.from);
          const tip = `${labelOf(m.from, m.from_label)} → ${name} · ${clock(m.ts)}`;
          if ($("#tl-arrows").checked && si !== undefined && si !== i) {
            const y1 = yOf(si) + ROW / 2, y2 = si < i ? y + 5 : y + ROW - 5;
            arrows.append(s("line", { class: "tl-arrow", x1: xx, x2: xx, y1, y2, "marker-end": "url(#tl-arr)", "data-tip": tip }));
          } else if (si === undefined) {
            plot.append(s("path", { class: "tl-msg", d: `M${xx - 4},${y + 2} L${xx + 4},${y + 2} L${xx},${y + 8} z`, "data-tip": tip }));
          }
        }
        for (const e of l.errors) plot.append(s("circle", { class: "tl-err", cx: x(e.ts), cy: mid, r: 4, "data-tip": `${name} · error · ${e.text}` }));
      });
      plot.append(arrows);
      g.append(plot);
      if (S.live) { const xn = x(now()); if (xn > LABEL) g.append(s("line", { class: "tl-now", x1: xn, x2: xn, y1: TOP - 6, y2: height })); }
      else if (S.at != null) { const xa = x(S.at); if (xa > LABEL) g.append(s("line", { class: "tl-cursor", x1: xa, x2: xa, y1: TOP - 6, y2: height })); }
      if (!lanes.length) g.append(s("text", { x: width / 2, y: 90, "text-anchor": "middle", class: "tl-empty", text: "no agents yet" }));
      svg.replaceChildren(g);
    },
    /** Zoom about the middle of the plot (the zoom buttons). */
    zoomBy(factor) {
      if (!this.view) return;
      if (this.t0 == null) { this.t0 = this.view.t0; this.t1 = this.view.t1; }
      this.zoom(factor, this.view.LABEL + (this.view.width - this.view.LABEL) / 2);
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

  // ---------- files (files.js) ----------
  const Files = window.ConjureFiles({
    h, fill, api, apiBlob, renderMarkdown,
    isLive: () => S.live,
    onPath: (path, ws) => { S.filePath = path; S.fileWs = path ? ws : null; syncHash(); },
  });

  // ---------- views ----------
  /** ``#<view>/<agent>`` — reloads keep their place, and a link can
   *  point at one agent's chat or timeline. */
  function syncHash() {
    // Files: #files/<workspace>/<path>, the workspace named by its first agent.
    const inFile = S.view === "files" && S.filePath && S.fileWs;
    const who = inFile ? S.fileWs : S.selected;
    const file = inFile ? "/" + S.filePath.split("/").map(encodeURIComponent).join("/") : "";
    const hash = `#${S.view}${who ? "/" + who + file : ""}`;
    if (location.hash !== hash) history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
  }
  function setView(view) {
    S.view = view;
    syncHash();
    for (const b of document.querySelectorAll("#views button")) b.classList.toggle("active", b.dataset.view === view);
    for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === `view-${view}`);
    if (view === "graph") { Graph.refresh(); Graph.inspect(S.selected); Graph.selected = S.selected; }
    if (view === "timeline") Timeline.refresh();
    if (view === "files") Files.open(S.filePath && S.fileWs ? S.fileWs : S.selected, S.filePath);
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
      api("timeline").then((d) => this.ticks(d)).catch(() => {});
    },
    /** Marks along the scrubber where messages, errors and failed tool
     *  calls happened, so the interesting moments are easy to find. */
    ticks(d) {
      const svg = $("#scrub-ticks");
      const [lo, hi] = this.bounds();
      if (!svg || !(hi > lo)) return;
      const at = (t) => ((t - lo) / (hi - lo)) * 1000;
      const marks = [];
      for (const l of d.lanes || []) {
        for (const m of l.messages || []) marks.push(s("rect", { class: "tick msg", x: at(m.ts) - 1.5, y: 9, width: 3, height: 7 }));
        for (const tc of l.tools || []) if (tc.failed) marks.push(s("rect", { class: "tick fail", x: at(tc.ts) - 1.5, y: 5, width: 3, height: 11 }));
        for (const e of l.errors || []) marks.push(s("rect", { class: "tick err", x: at(e.ts) - 2, y: 2, width: 4, height: 14 }));
      }
      svg.setAttribute("viewBox", "0 0 1000 16");
      svg.replaceChildren(...marks);
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
    $("#tl-fit").addEventListener("click", () => { Timeline.t0 = Timeline.t1 = null; $("#tl-follow").checked = true; Timeline.render(); });
    $("#tl-follow").addEventListener("change", () => Timeline.render());
    $("#tl-arrows").addEventListener("change", () => Timeline.render());
    $("#tl-zoom-in").addEventListener("click", () => Timeline.zoomBy(1 / 1.5));
    $("#tl-zoom-out").addEventListener("click", () => Timeline.zoomBy(1.5));
    $("#tl-span").addEventListener("click", () => { Timeline.t0 = Timeline.t1 = null; $("#tl-follow").checked = true; Timeline.render(); });
    for (const b of document.querySelectorAll(".act-filters button")) {
      b.addEventListener("click", () => {
        if (!ACT_FILTERS.delete(b.dataset.f)) ACT_FILTERS.add(b.dataset.f);
        store.set("conjure-activity-filters", JSON.stringify([...ACT_FILTERS]));
        renderActivity();
      });
    }

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
      if (S.view === "graph" && Graph.key(e)) { e.preventDefault(); return; }
      switch (e.key) {
        case "1": setView("chat"); break;
        case "2": setView("graph"); break;
        case "3": setView("timeline"); break;
        case "4": setView("files"); break;
        case "/": if (S.view === "files") { e.preventDefault(); Files.focusFilter(); } break;
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
      // Cost as the top bar shows it: tokens on a model with no known
      // price are "unpriced", never a misleading $0.
      const costText = (usd, unpriced) => (unpriced && !usd ? "unpriced" : `${money(usd || 0)}${unpriced ? " + unpriced" : ""}`);
      const tokTitle = (a) => `${(a.tokens || 0).toLocaleString()} tokens in and out · ${(a.tokens_out || 0).toLocaleString()} output${a.unpriced_tokens ? ` · ${(a.unpriced_tokens).toLocaleString()} unpriced` : ""}`;
      const row = (a) => h("tr", { onclick: () => { $("#run").hidden = true; setView("chat"); openChat(a.id, { focus: false }); } },
        h("td", { text: labelOf(a.id, a.label || a.id) }),
        h("td", { text: ENGINE_NAMES[a.engine] || a.engine || "—" }),
        h("td", { text: a.model ? shortModel(a.model) : "—", title: a.model_source ? `${a.model} (${a.model_source})` : "" }),
        h("td", { class: "num", text: a.turns ?? 0 }),
        h("td", { class: "num", text: `${a.tool_calls ?? 0}${a.tool_errors ? ` (${a.tool_errors}✗)` : ""}` }),
        h("td", { class: "num", text: kfmt(a.tokens ?? a.tokens_out ?? 0), title: tokTitle(a) }),
        h("td", { class: "num dim", text: kfmt(a.tokens_out || 0) }),
        h("td", { class: "num", text: secs(a.active_s) }),
        h("td", { class: `num${a.unpriced_tokens && !a.cost_usd ? " dim" : ""}`, text: costText(a.cost_usd, a.unpriced_tokens) }));
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
          stat(kfmt(t.tokens ?? t.tokens_out ?? 0), `tokens · ${kfmt(t.tokens_out || 0)} output`),
          stat(t.unpriced_tokens && !t.cost_usd ? "unpriced" : money(t.cost_usd || 0), t.unpriced_tokens ? `cost · + ${kfmt(t.unpriced_tokens)} tokens unpriced` : "cost"))),
        h("section", null, h("h3", { text: "Agents" }), h("div", { class: "run-table-wrap" }, h("table", { class: "run-table" },
          h("thead", null, h("tr", null, ["agent", "engine", "model"].map((x) => h("th", { text: x })), ["turns", "tool calls", "tokens", "output", "active", "cost"].map((x) => h("th", { class: "num", text: x, title: x === "tokens" ? "input + output, as the agent tree counts them" : null })))),
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
      else if (S.view === "files" && Files.addr) await Files.refresh();
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
    const [hashView, hashAgent, ...hashPath] = location.hash.slice(1).split("/");
    if (hashView === "files" && hashPath.length) S.filePath = hashPath.map(decodeURIComponent).join("/");
    if (hashAgent) S.selected = hashAgent;
    await refreshSnapshot();
    if (hashAgent && S.agents.has(hashAgent)) openChat(hashAgent);
    if (["graph", "timeline", "files"].includes(hashView)) setView(hashView);
    loops();
  }
  boot();
})();
