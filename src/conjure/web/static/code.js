/* conjure web UI — code, diffs and terminal output for tool calls.
 *
 * Tool calls arrive as {name, args}; results as text (JSON, a Python
 * dict repr from orchestral tools, or plain text from Claude Code /
 * Codex). This module turns the common ones into readable blocks:
 * a shell command as highlighted bash, a written file as code in its
 * language, an edit as a diff, a command's result as terminal output
 * with its exit status. Everything else falls back to labelled fields.
 *
 * Highlighting is the vendored highlight.js (window.hljs); without it
 * code still renders, uncoloured. hljs escapes its input, and every
 * other string goes through textContent, so model output can't inject
 * HTML. Exposed as window.ConjureCode for app.js.
 */
"use strict";
(() => {
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const clip = (str, n) => (str.length > n ? str.slice(0, n - 1) + "…" : str);
  const lineCount = (s) => (s ? s.replace(/\n$/, "").split("\n").length : 0);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  // ---------- languages ----------

  const EXT_LANG = {
    py: "python", pyw: "python", pyi: "python", ipynb: "json",
    sh: "bash", bash: "bash", zsh: "bash", ksh: "bash", slurm: "bash", sbatch: "bash", pbs: "bash",
    f: "fortran", for: "fortran", f77: "fortran", f90: "fortran", f95: "fortran", f03: "fortran", f08: "fortran",
    c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", hxx: "cpp", cu: "cpp", C: "cpp",
    js: "javascript", mjs: "javascript", cjs: "javascript", ts: "typescript", tsx: "typescript", jsx: "javascript",
    json: "json", jsonl: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", cfg: "ini", conf: "ini",
    md: "markdown", markdown: "markdown", tex: "latex", sty: "latex", cls: "latex", bib: "latex",
    jl: "julia", cmake: "cmake", mk: "makefile", rs: "rust", go: "go", java: "java", kt: "kotlin",
    r: "r", R: "r", sql: "sql", lua: "lua", rb: "ruby", pl: "perl", php: "php", swift: "swift",
    html: "xml", xml: "xml", svg: "xml", lhe: "xml", css: "css", scss: "scss",
    diff: "diff", patch: "diff",
  };
  const NAME_LANG = { makefile: "makefile", gnumakefile: "makefile", dockerfile: "dockerfile", "cmakelists.txt": "cmake" };

  function hasLang(lang) {
    return !!(lang && window.hljs && window.hljs.getLanguage(lang));
  }
  /** The highlight.js language for a file path, or "" (plain). */
  function langForPath(path) {
    const base = String(path || "").split(/[\\/]/).pop();
    const named = NAME_LANG[base.toLowerCase()];
    if (named) return named;
    const m = base.match(/\.([A-Za-z0-9]+)$/);
    if (!m) return "";
    return EXT_LANG[m[1]] || EXT_LANG[m[1].toLowerCase()] || "";
  }
  /** A fence tag (```py, ```sh, ```f90) as a highlight.js language. */
  function langForTag(tag) {
    const t = String(tag || "").toLowerCase();
    if (!t) return "";
    if (hasLang(t)) return t;
    return EXT_LANG[t] || { shell: "bash", console: "bash", terminal: "bash", py3: "python", tex: "latex" }[t] || "";
  }

  // ---------- highlighting ----------

  const MAX_HIGHLIGHT = 200_000; // characters; past this, plain text

  /** Fill ``el`` with ``code`` highlighted as ``lang`` (plain if unknown). */
  function highlightInto(el, code, lang) {
    if (hasLang(lang) && code.length <= MAX_HIGHLIGHT) {
      try {
        el.innerHTML = window.hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
        el.classList.add("hljs");
        return el;
      } catch { /* fall through to plain */ }
    }
    el.textContent = code;
    return el;
  }

  // A shell command with heredocs: the heredoc bodies are often Python
  // or a file being written. Split them out so each part gets its own
  // language: ``python3 - <<'EOF'`` → python; ``cat > run.py <<EOF`` → the
  // target's language; anything else stays plain.
  const HEREDOC = /<<-?\s*(['"]?)([A-Za-z_][\w-]*)\1/;
  function shellSegments(command) {
    const lines = command.split("\n");
    const segs = [];
    let shell = [];
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      shell.push(line);
      const m = line.match(HEREDOC);
      if (!m) continue;
      const tag = m[2];
      const body = [];
      let j = i + 1;
      while (j < lines.length && lines[j].trim() !== tag) body.push(lines[j++]);
      if (j >= lines.length) continue; // unterminated: leave it to bash
      segs.push({ code: shell.join("\n"), lang: "bash" });
      shell = [];
      segs.push({ code: body.join("\n"), lang: heredocLang(line) });
      shell.push(lines[j]);
      i = j;
    }
    if (shell.length) segs.push({ code: shell.join("\n"), lang: "bash" });
    return segs;
  }
  function heredocLang(line) {
    if (/\b(python[\d.]*|ipython)\b/.test(line)) return "python";
    if (/\b(bash|sh|zsh)\s*(-s)?\s*<</.test(line)) return "bash";
    const target = line.match(/>\s*(['"]?)([^\s'"<>|;&]+)\1/);
    if (target) return langForPath(target[2]);
    if (/\bjulia\b/.test(line)) return "julia";
    return "";
  }
  function highlightShellInto(el, command) {
    const segs = shellSegments(command);
    if (segs.length <= 1 || !window.hljs) return highlightInto(el, command, "bash");
    el.replaceChildren(...segs.map((s, k) => {
      const part = highlightInto(h("span", { class: s.lang && s.lang !== "bash" ? "heredoc" : null }), s.code, s.lang);
      return k < segs.length - 1 ? [part, document.createTextNode("\n")] : [part];
    }).flat());
    el.classList.add("hljs");
    return el;
  }

  // ---------- copy ----------

  function copyButton(getText) {
    const btn = h("button", { class: "copy", type: "button", title: "Copy", text: "copy" });
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const text = getText();
      let ok = false;
      try { await navigator.clipboard.writeText(text); ok = true; } catch {
        const ta = h("textarea", { style: "position:fixed;opacity:0" });
        ta.value = text;
        document.body.append(ta);
        ta.select();
        try { ok = document.execCommand("copy"); } catch { ok = false; }
        ta.remove();
      }
      btn.textContent = ok ? "copied" : "copy failed";
      setTimeout(() => { btn.textContent = "copy"; }, 1200);
    });
    return btn;
  }

  // ---------- code blocks ----------

  const COLLAPSE_LINES = 16; // longer blocks show this many lines until expanded

  /** A titled, highlighted, copyable code block.
   *  opts: lang, title, kind ("shell" draws a $ prompt), lineNumbers,
   *  startLine, collapse (lines; 0 = never), className. */
  function codeBlock(code, opts = {}) {
    code = String(code ?? "").replace(/\n$/, "");
    const lang = opts.lang || "";
    const lines = lineCount(code);
    const codeEl = h("code", { class: lang ? `language-${lang}` : null });
    if (opts.kind === "shell") highlightShellInto(codeEl, code);
    else highlightInto(codeEl, code, lang);
    const pre = h("pre", { class: "cb-pre" + (opts.lineNumbers && lines > 1 ? " numbered" : "") + (opts.kind === "shell" ? " shell" : "") });
    if (opts.lineNumbers && lines > 1) {
      const start = opts.startLine || 1;
      const gutter = h("span", { class: "gutter", "aria-hidden": "true",
        text: Array.from({ length: lines }, (_, k) => String(start + k)).join("\n") });
      pre.append(gutter);
    }
    pre.append(codeEl);
    const label = opts.langLabel ?? (opts.kind === "shell" ? "bash" : lang);
    const head = h("div", { class: "code-head" },
      h("span", { class: "code-title", text: opts.title || "" }),
      h("span", { class: "code-meta", text: [label, lines > 1 ? plural(lines, "line") : ""].filter(Boolean).join(" · ") }),
      copyButton(() => opts.copyText ?? code));
    const box = h("div", { class: "codeblock" + (opts.className ? " " + opts.className : "") }, head, pre);
    const limit = opts.collapse ?? COLLAPSE_LINES;
    if (limit && lines > limit + 4) {
      box.classList.add("collapsed");
      box.style.setProperty("--collapse-lines", String(limit));
      const more = h("button", { class: "more", type: "button", text: `show all ${lines} lines` });
      more.addEventListener("click", (ev) => {
        ev.preventDefault();
        const open = box.classList.toggle("collapsed");
        more.textContent = open ? `show all ${lines} lines` : "show less";
      });
      box.append(more);
    }
    return box;
  }

  // ---------- diffs ----------

  /** Line diff of a → b (LCS). Returns [{op: " "|"-"|"+", text}]. */
  function lineDiff(a, b) {
    const A = a === "" ? [] : a.split("\n");
    const B = b === "" ? [] : b.split("\n");
    // Trim the common head and tail so the LCS table stays small.
    let pre = 0;
    while (pre < A.length && pre < B.length && A[pre] === B[pre]) pre++;
    let suf = 0;
    while (suf < A.length - pre && suf < B.length - pre && A[A.length - 1 - suf] === B[B.length - 1 - suf]) suf++;
    const a2 = A.slice(pre, A.length - suf);
    const b2 = B.slice(pre, B.length - suf);
    const out = A.slice(0, pre).map((t) => ({ op: " ", text: t }));
    if (a2.length * b2.length > 250_000) {
      out.push(...a2.map((t) => ({ op: "-", text: t })), ...b2.map((t) => ({ op: "+", text: t })));
    } else {
      const n = a2.length, m = b2.length;
      const L = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
      for (let i = n - 1; i >= 0; i--)
        for (let j = m - 1; j >= 0; j--)
          L[i][j] = a2[i] === b2[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (a2[i] === b2[j]) { out.push({ op: " ", text: a2[i] }); i++; j++; }
        else if (L[i + 1][j] >= L[i][j + 1]) out.push({ op: "-", text: a2[i++] });
        else out.push({ op: "+", text: b2[j++] });
      }
      while (i < n) out.push({ op: "-", text: a2[i++] });
      while (j < m) out.push({ op: "+", text: b2[j++] });
    }
    out.push(...A.slice(A.length - suf).map((t) => ({ op: " ", text: t })));
    return out;
  }

  /** Keep changed lines plus ``ctx`` lines of context; elide the rest. */
  function withContext(rows, ctx = 3) {
    const keep = rows.map(() => false);
    rows.forEach((r, k) => {
      if (r.op === " ") return;
      for (let d = -ctx; d <= ctx; d++) if (rows[k + d]) keep[k + d] = true;
    });
    const out = [];
    let skipped = 0;
    rows.forEach((r, k) => {
      if (keep[k]) {
        if (skipped) out.push({ op: "…", text: `${plural(skipped, "unchanged line")}` });
        skipped = 0;
        out.push(r);
      } else skipped++;
    });
    if (skipped) out.push({ op: "…", text: `${plural(skipped, "unchanged line")}` });
    return out;
  }

  function diffBlock(oldText, newText, opts = {}) {
    const rows = withContext(lineDiff(String(oldText ?? ""), String(newText ?? "")));
    return diffRows(rows, opts, () => String(newText ?? ""));
  }

  /** A unified diff string (Codex file changes, ``git diff`` output). */
  function unifiedDiffBlock(diff, opts = {}) {
    const rows = String(diff).replace(/\n$/, "").split("\n").map((line) => {
      if (/^(\+\+\+|---|diff |index |@@)/.test(line)) return { op: "@", text: line };
      const c = line[0];
      return c === "+" || c === "-" ? { op: c, text: line.slice(1) } : { op: " ", text: line.slice(c === " " ? 1 : 0) };
    });
    return diffRows(rows, opts, () => String(diff));
  }

  function diffRows(rows, opts, getText) {
    const lang = opts.lang || "";
    const added = rows.filter((r) => r.op === "+").length;
    const removed = rows.filter((r) => r.op === "-").length;
    const body = h("div", { class: "diff-body" });
    for (const r of rows) {
      const cls = { "+": "add", "-": "del", "…": "skip", "@": "hunk" }[r.op] || "same";
      const text = h("span", { class: "dtext" });
      if (r.op === "…" || r.op === "@") text.textContent = r.text;
      else highlightInto(text, r.text, lang);
      body.append(h("div", { class: `dline ${cls}` },
        h("span", { class: "dsign", "aria-hidden": "true", text: r.op === "…" ? "⋯" : r.op === "@" ? "" : r.op }), text));
    }
    const head = h("div", { class: "code-head" },
      h("span", { class: "code-title", text: opts.title || "" }),
      h("span", { class: "code-meta" },
        h("span", { class: "stat-add", text: `+${added}` }), " ",
        h("span", { class: "stat-del", text: `−${removed}` })),
      copyButton(getText));
    const box = h("div", { class: "codeblock diff" }, head, body);
    if (rows.length > COLLAPSE_LINES * 2 + 4) {
      box.classList.add("collapsed");
      box.style.setProperty("--collapse-lines", String(COLLAPSE_LINES * 2));
      const more = h("button", { class: "more", type: "button", text: `show all ${rows.length} lines` });
      more.addEventListener("click", (ev) => {
        ev.preventDefault();
        const shut = box.classList.toggle("collapsed");
        more.textContent = shut ? `show all ${rows.length} lines` : "show less";
      });
      box.append(more);
    }
    return box;
  }

  // ---------- parsing tool results ----------

  /** Parse JSON or a Python literal (``{'ok': True, ...}``, as
   *  orchestral tools return). Lenient about raw newlines inside
   *  strings (a capped output's truncation notice). ``undefined`` if
   *  it isn't one. */
  function parseLiteral(text) {
    const t = String(text ?? "").trim();
    if (!t || !"{[".includes(t[0])) return undefined;
    try { return JSON.parse(t); } catch { /* try Python */ }
    let i = 0;
    const ws = () => { while (i < t.length && /\s/.test(t[i])) i++; };
    const fail = () => { throw new Error("bad literal"); };
    function str() {
      const q = t[i];
      const triple = t.startsWith(q.repeat(3), i);
      i += triple ? 3 : 1;
      let out = "";
      for (;;) {
        if (i >= t.length) fail();
        const c = t[i];
        if (triple ? t.startsWith(q.repeat(3), i) : c === q) { i += triple ? 3 : 1; return out; }
        if (c === "\\") {
          const e = t[i + 1];
          i += 2;
          const simple = { n: "\n", t: "\t", r: "\r", "\\": "\\", "'": "'", '"': '"', a: "\x07", b: "\b", f: "\f", v: "\v", 0: "\0", "\n": "" };
          if (e in simple) out += simple[e];
          else if (e === "x") { out += String.fromCharCode(parseInt(t.substr(i, 2), 16)); i += 2; }
          else if (e === "u") { out += String.fromCharCode(parseInt(t.substr(i, 4), 16)); i += 4; }
          else if (e === "U") { out += String.fromCodePoint(parseInt(t.substr(i, 8), 16)); i += 8; }
          else out += "\\" + e;
          continue;
        }
        out += c;
        i++;
      }
    }
    function val() {
      ws();
      const c = t[i];
      if (c === "{") {
        i++;
        const o = {};
        ws();
        if (t[i] === "}") { i++; return o; }
        for (;;) {
          const k = val();
          ws();
          if (t[i] !== ":") fail();
          i++;
          o[typeof k === "string" ? k : JSON.stringify(k)] = val();
          ws();
          if (t[i] === ",") { i++; ws(); if (t[i] === "}") { i++; return o; } continue; }
          if (t[i] === "}") { i++; return o; }
          fail();
        }
      }
      if (c === "[" || c === "(") {
        const close = c === "[" ? "]" : ")";
        i++;
        const a = [];
        ws();
        if (t[i] === close) { i++; return a; }
        for (;;) {
          a.push(val());
          ws();
          if (t[i] === ",") { i++; ws(); if (t[i] === close) { i++; return a; } continue; }
          if (t[i] === close) { i++; return a; }
          fail();
        }
      }
      if (c === "'" || c === '"') return str();
      if (/[bru]/i.test(c) && (t[i + 1] === "'" || t[i + 1] === '"')) { i++; return str(); }
      const word = t.slice(i).match(/^(True|False|None|null|true|false|-?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|inf|nan))/);
      if (!word) fail();
      i += word[0].length;
      return { True: true, true: true, False: false, false: false, None: null, null: null }[word[0]] ?? Number(word[0]);
    }
    try {
      const v = val();
      ws();
      return i === t.length ? v : undefined;
    } catch {
      return undefined;
    }
  }

  // ---------- tool kinds ----------

  const MCP_PREFIX = /^mcp__(conjure|spawn|toolbase)__/;
  const bareName = (n) => String(n || "?").replace(MCP_PREFIX, "");
  /** "shell" | "write" | "edit" | "multiedit" | "patch" | "read" | "search" | "send" | "spawn" | "other" */
  function toolKind(name, args) {
    const n = bareName(name).toLowerCase();
    if (["bash", "shell", "exec_command", "local_shell", "run_shell", "terminal"].includes(n)) return "shell";
    if (n === "write" || n === "write_file" || n === "create_file") return "write";
    if (n === "edit" || n === "str_replace" || n === "edit_file") {
      if (args && Array.isArray(args.changes)) return "patch";
      return "edit";
    }
    if (n === "multiedit") return "multiedit";
    if (n === "apply_patch") return "patch";
    if (n === "read" || n === "read_file" || n === "view") return "read";
    if (n === "grep" || n === "glob" || n === "search") return "search";
    if (n === "send") return "send";
    if (n === "spawn") return "spawn";
    return "other";
  }
  // ---------- paths relative to the agent's sandbox ----------

  // Argument values that are file data, shown exactly as written.
  const DATA_KEYS = new Set(["content", "contents", "text", "old_string", "new_string", "old_str", "new_str", "old", "new", "body", "diff", "patch"]);
  const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const BEFORE = "(^|[\\s'\"=(:,;\\[<>|&])";
  const AFTER = "(?=$|[\\s'\";)\\]:,<>|&])";
  /** The prefixes to shorten, longest first: each sandbox root as
   *  ``.``, and its parent and grandparent as ``..`` and ``../..`` (a
   *  worker whose sandbox sits inside its parent's often reaches up). */
  function prefixes(roots) {
    const out = new Map();
    for (const root of roots || []) {
      if (!root) continue;
      if (!out.has(root)) out.set(root, ".");
      let dir = root, up = "";
      for (let k = 0; k < 2; k++) {
        dir = dir.replace(/\/[^/]+$/, "");
        up = up ? up + "/.." : "..";
        if (dir.split("/").filter(Boolean).length < 3) break; // not /home/user
        if (!out.has(dir)) out.set(dir, up);
      }
    }
    return [...out].sort((a, b) => b[0].length - a[0].length);
  }
  /** ``text`` with sandbox paths shortened: ``<root>/work/a.py`` →
   *  ``work/a.py``, a bare ``<root>`` → ``.``, ``<parent>/x`` → ``../x``. */
  function relText(text, roots) {
    if (!roots || !roots.length || typeof text !== "string") return text;
    let out = text;
    for (const [root, rel] of prefixes(roots)) {
      if (!out.includes(root)) continue;
      const r = reEsc(root);
      out = out
        .replace(new RegExp(`${BEFORE}${r}/(?=[^\\s'"])`, "g"), (_m, pre) => pre + (rel === "." ? "" : rel + "/"))
        .replace(new RegExp(`${BEFORE}${r}/?${AFTER}`, "g"), (_m, pre) => pre + rel);
    }
    return out;
  }
  /** A call's arguments for display: paths and commands relative to the
   *  sandbox, file contents untouched. */
  function relArgs(args, roots) {
    if (!roots || !roots.length) return args;
    const walk = (v, key) => {
      if (typeof v === "string") return DATA_KEYS.has(key) ? v : relText(v, roots);
      if (Array.isArray(v)) return v.map((x) => walk(x, key));
      if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x, k)]));
      return v;
    };
    return walk(args, "");
  }

  const pathOf = (a) => (a && (a.file_path ?? a.path ?? a.filename ?? a.notebook_path)) || "";
  const commandOf = (a) => {
    const c = a && (a.command ?? a.cmd ?? a.script);
    return Array.isArray(c) ? c.join(" ") : c == null ? "" : String(c);
  };
  const firstLine = (s) => String(s || "").split("\n").find((l) => l.trim()) || "";
  /** Markdown → plain text, for one-line previews. */
  const plainText = (s) => String(s).replace(/[*_`#>]+/g, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\s+/g, " ").trim();
  /** A dict result in one line: its scalar fields, ``key=value``. */
  function compactDict(o) {
    const parts = [];
    for (const [k, v] of Object.entries(o)) {
      if (k === "ok" || v == null || typeof v === "object") continue;
      parts.push(`${k}=${clip(String(v).replace(/\s+/g, " "), 48)}`);
      if (parts.length >= 4) break;
    }
    return parts.join(" · ");
  }

  /** The one-line summary after the tool name: what it ran or touched. */
  function callSummary(name, args) {
    args = args || {};
    const kind = toolKind(name, args);
    if (kind === "shell") {
      const cmd = commandOf(args);
      const n = lineCount(cmd);
      // ``cd .; …`` (into the sandbox it already runs in) says nothing.
      const line = firstLine(cmd).replace(/^\s*cd\s+\.\/?\s*(?:;|&&)\s*/, "");
      return { prefix: "$", text: clip(line || firstLine(cmd), 120), extra: n > 1 ? `+${n - 1} lines` : "" };
    }
    if (kind === "write") {
      const n = lineCount(String(args.content ?? args.contents ?? args.text ?? ""));
      return { text: pathOf(args), extra: plural(n, "line") };
    }
    if (kind === "edit") return { text: pathOf(args), extra: args.replace_all ? "replace all" : "" };
    if (kind === "multiedit") return { text: pathOf(args), extra: plural((args.edits || []).length, "edit") };
    if (kind === "patch") {
      const paths = (args.changes || []).map((c) => c && c.path).filter(Boolean);
      return { text: paths.length ? paths.join(", ") : "patch", extra: paths.length > 1 ? plural(paths.length, "file") : "" };
    }
    if (kind === "read") {
      const off = Number(args.offset || 0), lim = Number(args.limit || 0);
      const range = lim ? `lines ${off + 1}–${off + lim}` : off ? `from line ${off + 1}` : "";
      return { text: pathOf(args), extra: range };
    }
    if (kind === "search") {
      const where = args.path || args.glob || "";
      return { text: String(args.pattern ?? args.query ?? ""), extra: where ? `in ${where}` : "" };
    }
    if (kind === "other") {
      const head = headlineOf(args);
      if (head) return { text: head.text };
      const short = [], long = [];
      for (const [k, v] of Object.entries(args)) {
        if (v == null || v === "") continue;
        const text = typeof v === "string" ? v : JSON.stringify(v);
        (text.length <= 40 && !text.includes("\n") ? short : long).push(`${k}=${typeof v === "string" ? JSON.stringify(v) : text}`);
      }
      const shown = short.slice(0, 3);
      const more = short.length - shown.length + long.length;
      return { text: shown.join("  "), extra: more ? `+${plural(more, "field")}` : "", dim: true };
    }
    // The body is the preview's (callPreview), not the summary's.
    if (kind === "send") return { prefix: "→", text: String(args.to ?? "?") };
    if (kind === "spawn") {
      const on = [args.model, args.llm && args.llm !== "default" ? args.llm : "", args.engine && args.engine !== "auto" ? args.engine : ""];
      const kits = Array.isArray(args.toolkits) && args.toolkits.length ? plural(args.toolkits.length, "toolkit") : "";
      return { text: String(args.label || "child"), extra: [...on, kits].filter(Boolean).join(" · ") };
    }
    return null;
  }

  // ---------- collapsed preview ----------

  const PREVIEW_ROWS = 2;
  /** Up to ``n`` non-blank lines of ``text``, their shared indent removed. */
  function leadLines(text, n = PREVIEW_ROWS) {
    const lines = String(text ?? "").split("\n").filter((l) => l.trim()).slice(0, n);
    return dedent(lines);
  }
  function dedent(lines) {
    const pad = Math.min(...lines.map((l) => l.match(/^[ \t]*/)[0].length));
    return Number.isFinite(pad) ? lines.map((l) => l.slice(pad)) : lines;
  }
  /** The first paragraph of markdown as one line of plain text (bold,
   *  code ticks, headings and link targets dropped; ``snake_case`` kept). */
  function firstParagraph(s) {
    const para = String(s ?? "").trim().split(/\n\s*\n/)[0] || "";
    return para.replace(/^\s{0,3}(#{1,6}|>)\s*/gm, "").replace(/\*\*|__|`/g, "")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\s+/g, " ").trim();
  }
  /** One preview row of code. ``keyHidden`` keeps the key's width so
   *  a second row lines up under the first. */
  function codeRow(text, lang, { sign = "", key = "", keyHidden = false } = {}) {
    const code = h("code", { class: "pv-text" });
    highlightInto(code, text, lang);
    const cls = sign === "+" ? " add" : sign === "-" ? " del" : "";
    return h("div", { class: "pv-row" + cls },
      key ? h("span", { class: "pv-key" + (keyHidden ? " hidden" : ""), "aria-hidden": keyHidden ? "true" : null, text: key }) : null,
      sign ? h("span", { class: "pv-sign", "aria-hidden": "true", text: sign }) : null,
      code);
  }
  const proseRow = (text, key = "") =>
    text ? h("div", { class: "pv-row prose" }, key ? h("span", { class: "pv-key", text: key }) : null, h("span", { class: "pv-text", text: clip(text, 400) })) : null;
  /** The first removed and first added line of a change. */
  function changeRows(diffRows, lang) {
    const del = diffRows.find((r) => r.op === "-");
    const add = diffRows.find((r) => r.op === "+");
    const picked = [del, add].filter(Boolean);
    const texts = dedent(picked.map((r) => r.text));
    return picked.map((r, k) => codeRow(texts[k], lang, { sign: r.op }));
  }
  const unifiedRows = (diff) => String(diff).split("\n")
    .filter((l) => !/^(\+\+\+|---)/.test(l))
    .map((l) => ({ op: l[0] === "+" || l[0] === "-" ? l[0] : " ", text: l.slice(1) }));

  // The argument that says what a toolkit call is about, in this order.
  const HEADLINE_KEYS = ["process", "query", "pattern", "expression", "formula", "reaction", "action", "what", "task",
    "name", "target", "url", "path", "file", "file_path", "filename", "directory", "dir", "model", "value", "address", "id"];
  /** ``{keys, text}`` for a call's headline: ``a b → c d`` for initial
   *  and final states, else the first telling short string. */
  function headlineOf(args) {
    if (!args || typeof args !== "object") return null;
    const list = (v) => (Array.isArray(v) ? v.join(" ") : typeof v === "string" ? v : null);
    const [ini, fin] = [list(args.initial ?? args.initial_state), list(args.final ?? args.final_state)];
    if (ini && fin) return { keys: ["initial", "final", "initial_state", "final_state"], text: `${ini} → ${fin}` };
    const ok = (v) => typeof v === "string" && v.trim() && v.length <= 160 && !v.includes("\n");
    for (const k of HEADLINE_KEYS) if (ok(args[k])) return { keys: [k], text: args[k] };
    const first = Object.entries(args).find(([, v]) => ok(v));
    return first ? { keys: [first[0]], text: first[1] } : null;
  }
  const kvText = (v) => (Array.isArray(v) ? v.map((x) => (x && typeof x === "object" ? JSON.stringify(x) : String(x))).join(", ")
    : v && typeof v === "object" ? JSON.stringify(v) : String(v));

  /** A line or two under a collapsed call: what it writes, changes,
   *  says or runs, for a glance down the transcript. Null where the
   *  summary line already says it (shell, read, search). Opening the
   *  call replaces it with the full blocks. */
  function callPreview(name, args) {
    args = args || {};
    const kind = toolKind(name, args);
    let rows = [];
    if (kind === "write") {
      const lang = langForPath(pathOf(args));
      rows = leadLines(args.content ?? args.contents ?? args.text).map((l) => codeRow(l, lang));
    } else if (kind === "edit") {
      rows = changeRows(lineDiff(String(args.old_string ?? args.old_str ?? args.old ?? args.old_text ?? ""),
        String(args.new_string ?? args.new_str ?? args.new ?? args.new_text ?? "")), langForPath(pathOf(args)));
    } else if (kind === "multiedit") {
      const all = (args.edits || []).flatMap((e) => lineDiff(String(e?.old_string ?? ""), String(e?.new_string ?? "")));
      rows = changeRows(all, langForPath(pathOf(args)));
    } else if (kind === "patch") {
      const first = (args.changes || []).find(Boolean);
      const diff = first ? first.diff ?? first.unified_diff ?? first.patch : args.patch ?? args.input;
      const lang = langForPath(first?.path || "");
      if (typeof diff === "string" && diff.trim()) rows = changeRows(unifiedRows(diff), lang);
      else if (typeof first?.content === "string") rows = leadLines(first.content).map((l) => codeRow(l, lang));
    } else if (kind === "send") {
      rows = [proseRow(bodySummary(args.body))];
    } else if (kind === "spawn") {
      rows = [proseRow(firstParagraph(args.initial_message || args.role_prompt))];
    } else if (kind === "other") {
      // The first long argument (code as code, prose as text), then the
      // rest as ``key value`` pairs: a toolkit call's glance.
      const head = headlineOf(args);
      const used = new Set(head ? head.keys : []);
      for (const [k, v] of Object.entries(args)) {
        if (used.has(k) || typeof v !== "string" || !(v.includes("\n") || v.length > 40)) continue;
        used.add(k);
        if (PROSE_KEYS.has(k)) rows = [proseRow(firstParagraph(v), k)];
        else {
          const lang = k in CODE_KEYS ? (CODE_KEYS[k] || langForPath(pathOf(args))) : "";
          rows = leadLines(v).map((l, i) => codeRow(l, lang, { key: k, keyHidden: i > 0 }));
        }
        break;
      }
      // Only when a headline took the summary line; otherwise the line
      // already lists these.
      const rest = Object.entries(args).filter(([k, v]) => !used.has(k) && v != null && v !== "");
      if (head && rest.length && rows.length < PREVIEW_ROWS) {
        rows.push(h("div", { class: "pv-row kv" }, rest.flatMap(([k, v], i) => [
          i ? h("span", { class: "pv-sep", text: "·" }) : null,
          h("span", { class: "pv-key", text: k }),
          h("span", { class: "pv-val", text: clip(kvText(v), 60) }),
        ])));
      }
    }
    rows = rows.filter(Boolean);
    return rows.length ? h("div", { class: "callpreview" }, rows) : null;
  }

  /** The expanded body of a tool call. ``renderMarkdown`` (from app.js)
   *  renders prose fields such as a Send body or a role prompt. */
  function renderCall(name, args, { renderMarkdown, original } = {}) {
    args = args || {};
    const kind = toolKind(name, args);
    if (kind === "shell") {
      const cmd = commandOf(args);
      const copyText = original ? commandOf(original) : cmd;
      const notes = [args.description, args.timeout_s ? `timeout ${args.timeout_s}s` : args.timeout ? `timeout ${Math.round(args.timeout / 1000)}s` : "",
        args.run_in_background ? "background" : ""].filter(Boolean).join(" · ");
      return h("div", { class: "callbody" }, codeBlock(cmd, { kind: "shell", title: notes ? `command · ${notes}` : "command", collapse: 24, copyText }));
    }
    if (kind === "write") {
      const path = pathOf(args);
      const content = String(args.content ?? args.contents ?? args.text ?? "");
      return h("div", { class: "callbody" }, codeBlock(content, { lang: langForPath(path), title: path, lineNumbers: true }));
    }
    if (kind === "edit") {
      const path = pathOf(args);
      return h("div", { class: "callbody" }, diffBlock(
        args.old_string ?? args.old_str ?? args.old ?? args.old_text ?? "",
        args.new_string ?? args.new_str ?? args.new ?? args.new_text ?? "",
        { lang: langForPath(path), title: path }));
    }
    if (kind === "multiedit") {
      const path = pathOf(args);
      return h("div", { class: "callbody" }, (args.edits || []).map((e, k) =>
        diffBlock(e.old_string ?? "", e.new_string ?? "", { lang: langForPath(path), title: `${path} · edit ${k + 1}` })));
    }
    if (kind === "patch") {
      const changes = Array.isArray(args.changes) ? args.changes : [];
      if (!changes.length && typeof (args.patch ?? args.input) === "string")
        return h("div", { class: "callbody" }, unifiedDiffBlock(args.patch ?? args.input, { title: "patch" }));
      return h("div", { class: "callbody" }, changes.map((c) => {
        const diff = c && (c.diff ?? c.unified_diff ?? c.patch);
        const title = `${c?.kind ? c.kind + " " : ""}${c?.path || "?"}`;
        if (typeof diff === "string" && diff.trim()) return unifiedDiffBlock(diff, { lang: langForPath(c.path), title });
        if (typeof c?.content === "string") return codeBlock(c.content, { lang: langForPath(c.path), title, lineNumbers: true });
        return h("div", { class: "field" }, h("span", { class: "fkey", text: c?.kind || "change" }), h("span", { class: "fval", text: c?.path || JSON.stringify(c) }));
      }));
    }
    if (kind === "send" && renderMarkdown) {
      const rest = { ...args };
      delete rest.body;
      const body = parseBody(args.body);
      return h("div", { class: "callbody" }, fields(rest),
        typeof body === "string" ? h("div", { class: "prose" }, renderMarkdown(body || "")) : messageView(body, { renderMarkdown }));
    }
    return h("div", { class: "callbody" }, fields(args, { renderMarkdown }));
  }

  // Argument names whose string values are code, and in what language.
  const CODE_KEYS = { code: "python", python: "python", source: "", script: "bash", command: "bash", commands: "", query: "sql", sql: "sql", patch: "diff", diff: "diff", content: "", contents: "", text: "" };
  const PROSE_KEYS = new Set(["role_prompt", "initial_message", "body", "prompt", "message", "task", "instructions", "description"]);

  /** Arguments as labelled fields: short values inline, multi-line
   *  strings as code (or prose), objects as highlighted JSON. */
  function fields(args, { renderMarkdown, prose = false, depth = 0 } = {}) {
    if (!args || typeof args !== "object") return codeBlock(String(args ?? ""), {});
    const box = h("div", { class: "fields" });
    const path = pathOf(args);
    for (const [k, v] of Object.entries(args)) {
      if (v == null || v === "") continue;
      // A message (``prose``): text reads as text, a list of sentences
      // as a list, a nested record as fields; only code keys stay code.
      if (prose && renderMarkdown && Array.isArray(v) && v.length && v.every((x) => typeof x === "string")) {
        const short = v.every((x) => x.length <= 40 && !x.includes("\n"));
        box.append(short && v.length <= 6
          ? h("div", { class: "field" }, h("span", { class: "fkey", text: k }), h("span", { class: "fval-list" }, v.map((x) => h("code", { class: "fval", text: x }))))
          : h("div", { class: "field block" }, h("span", { class: "fkey", text: k }),
            h("ul", { class: "prose-list" }, v.map((x) => h("li", null, renderMarkdown(x))))));
        continue;
      }
      if (prose && v && typeof v === "object" && !Array.isArray(v) && depth < 2) {
        box.append(h("div", { class: "field block" }, h("span", { class: "fkey", text: k }), fields(v, { renderMarkdown, prose, depth: depth + 1 })));
        continue;
      }
      if (typeof v === "string" && (v.includes("\n") || v.length > 100)) {
        if ((PROSE_KEYS.has(k) || (prose && !(k in CODE_KEYS))) && renderMarkdown) {
          box.append(h("div", { class: "field block" }, h("span", { class: "fkey", text: k }), h("div", { class: "prose" }, renderMarkdown(v))));
        } else {
          const lang = k in CODE_KEYS ? (CODE_KEYS[k] || langForPath(path)) : "";
          box.append(h("div", { class: "field block" }, h("span", { class: "fkey", text: k }),
            codeBlock(v, { lang, kind: lang === "bash" ? "shell" : undefined, langLabel: lang || "text" })));
        }
      } else if (v && typeof v === "object") {
        const json = JSON.stringify(v, null, 2);
        if (json.length <= 80 && !json.includes("\n  {")) {
          box.append(h("div", { class: "field" }, h("span", { class: "fkey", text: k }), h("code", { class: "fval", text: JSON.stringify(v) })));
        } else {
          box.append(h("div", { class: "field block" }, h("span", { class: "fkey", text: k }), codeBlock(json, { lang: "json" })));
        }
      } else {
        box.append(h("div", { class: "field" }, h("span", { class: "fkey", text: k }), h("code", { class: "fval", text: String(v) })));
      }
    }
    return box;
  }

  // ---------- results ----------

  const TRUNCATED = /\[\.\.\. (\d+) chars truncated[^\]]*\]/;

  /** Terminal output: stdout, stderr in red, the exit status, and a
   *  capped output's truncation notice set apart. */
  function terminal({ stdout = "", stderr = "", exitCode = null, timedOut = false, note = "" }) {
    const pre = h("pre", { class: "term-out" });
    const out = h("span", { class: "term-lines" });
    pre.append(out);
    const addText = (text, cls) => {
      if (!text) return;
      // Pull the truncation notice out of the stream as its own line.
      let rest = text;
      for (;;) {
        const m = rest.match(TRUNCATED);
        if (!m) break;
        if (m.index) out.append(h("span", { class: cls, text: rest.slice(0, m.index).replace(/\n+$/, "\n") }));
        out.append(h("span", { class: "term-cut", text: `✂ ${m[0].slice(1, -1).replace(/^\.\.\. /, "").replace(/\s*\.\.\.$/, "")}` }));
        rest = rest.slice(m.index + m[0].length).replace(/^\n+/, "");
      }
      if (rest) out.append(h("span", { class: cls, text: rest }));
    };
    addText(stdout.replace(/\n$/, ""), "stdout");
    if (stdout && stderr) out.append("\n");
    addText(stderr.replace(/\n$/, ""), "stderr");
    const empty = !stdout && !stderr;
    if (empty) out.append(h("span", { class: "dim", text: "(no output)" }));
    const lines = lineCount(stdout) + lineCount(stderr);
    const status = timedOut ? h("span", { class: "badge bad", text: "timed out" })
      : exitCode == null ? null
      : h("span", { class: "badge " + (Number(exitCode) === 0 ? "good" : "bad"), text: `exit ${exitCode}` });
    const head = h("div", { class: "code-head" },
      h("span", { class: "code-title", text: note || "output" }),
      h("span", { class: "code-meta" }, status, lines > 1 ? ` ${plural(lines, "line")}` : ""),
      copyButton(() => [stdout, stderr].filter(Boolean).join("\n")));
    const box = h("div", { class: "codeblock term" }, head, pre);
    if (lines > COLLAPSE_LINES + 4) {
      box.classList.add("collapsed", "tail");
      box.style.setProperty("--collapse-lines", String(COLLAPSE_LINES));
      const more = h("button", { class: "more", type: "button", text: `show all ${lines} lines` });
      more.addEventListener("click", (ev) => {
        ev.preventDefault();
        const shut = box.classList.toggle("collapsed");
        more.textContent = shut ? `show all ${lines} lines` : "show less";
      });
      box.append(more);
    }
    return box;
  }

  /** A shell result in whichever shape the engine gave it. */
  function shellResult(text, parsed, failed) {
    if (parsed && typeof parsed === "object" && ("stdout" in parsed || "exit_code" in parsed || "returncode" in parsed)) {
      return {
        stdout: String(parsed.stdout ?? parsed.output ?? ""),
        stderr: String(parsed.stderr ?? ""),
        exitCode: parsed.exit_code ?? parsed.returncode ?? (parsed.ok === false ? 1 : null),
        timedOut: !!(parsed.timed_out || parsed.code === "timeout"),
        error: parsed.ok === false && !("stdout" in parsed) ? `${parsed.code || "error"}: ${parsed.error || ""}` : "",
      };
    }
    // Codex: "exit N\n<output>" on failure, the output otherwise.
    const m = String(text).match(/^exit (-?\d+)\n([\s\S]*)$/);
    if (m) return { stdout: m[2], stderr: "", exitCode: Number(m[1]) };
    // Claude Code: plain text, sometimes with an <exit_code> or
    // "Exit code N" line; failures carry is_error.
    const ec = String(text).match(/(?:^|\n)(?:Exit code:?|<exit_code>)\s*(-?\d+)/i);
    return { stdout: String(text), stderr: "", exitCode: ec ? Number(ec[1]) : null };
  }

  /** Read's result as code: orchestral's {content, offset} or Claude
   *  Code's ``cat -n`` text (``   12→line``). */
  function readResult(text, parsed, args) {
    const path = pathOf(args) || parsed?.path || "";
    const lang = langForPath(path);
    if (parsed && typeof parsed.content === "string") {
      const start = Number(parsed.offset || 0) + 1;
      const meta = parsed.total_lines ? `of ${parsed.total_lines}` : "";
      return codeBlock(parsed.content, { lang, title: [path, meta && `lines ${start}–${start + Math.max(0, lineCount(parsed.content) - 1)} ${meta}`].filter(Boolean).join(" · "), lineNumbers: true, startLine: start, collapse: 12 });
    }
    const lines = String(text).replace(/\n$/, "").split("\n");
    const numbered = lines.length && lines.every((l) => /^\s*\d+[→\t]/.test(l) || !l.trim());
    if (numbered) {
      const first = Number((lines[0].match(/^\s*(\d+)/) || [0, 1])[1]);
      const body = lines.map((l) => l.replace(/^\s*\d+[→\t]/, "")).join("\n");
      return codeBlock(body, { lang, title: path, lineNumbers: true, startLine: first, collapse: 12 });
    }
    return null;
  }

  /** The expanded body of a tool result, or ``null`` for the generic
   *  summary-and-text rendering app.js already does. ``call`` is the
   *  {name, args} this result answers, when known. */
  function renderResult(text, failed, call) {
    const parsed = parseLiteral(text);
    const kind = call ? toolKind(call.name, call.args) : guessKind(parsed);
    if (kind === "shell") {
      const r = shellResult(text, parsed, failed);
      if (r.error) return null;
      return { el: terminal(r), summary: shellSummary(r, failed), failed: failed || (r.exitCode != null && Number(r.exitCode) !== 0) || r.timedOut };
    }
    if (kind === "read" && !failed && !(parsed && parsed.ok === false)) {
      const el = readResult(text, parsed, call?.args);
      if (el) {
        const n = parsed?.content != null ? lineCount(parsed.content) : lineCount(text);
        return { el, summary: `${plural(n, "line")}${parsed?.total_lines ? ` of ${parsed.total_lines}` : ""}` };
      }
    }
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && !failed && parsed.ok !== false) {
      if ((kind === "write" || kind === "edit" || kind === "multiedit") && Object.keys(parsed).every((k) => ["ok", "path", "bytes"].includes(k)))
        return { el: null, summary: `✓ ${kind === "write" ? "wrote" : "edited"} ${parsed.path || pathOf(call?.args) || ""}`.trim() };
      const json = JSON.stringify(parsed, null, 2);
      const nested = Object.values(parsed).some((v) => v && typeof v === "object");
      if (JSON.stringify(parsed).length > 120 || nested) return { el: codeBlock(json, { lang: "json", title: "result", collapse: 20 }), summary: compactDict(parsed) || null };
    }
    return null;
  }
  /** A result folded into its call's line: a short status ``chip``
   *  for the summary (``exit 0 · 4 lines``, ``✓``, ``error: …``), whether
   *  it went ``bad``, and the ``body`` (output, file text, JSON or the
   *  error) shown when the call is opened. */
  function resultForCall(text, failed, call) {
    const parsed = parseLiteral(text);
    const kind = toolKind(call?.name, call?.args);
    const r = renderResult(text, failed, call);
    if (kind === "shell" && r?.el) {
      const sr = shellResult(text, parsed, failed);
      const lines = lineCount(sr.stdout) + lineCount(sr.stderr);
      const status = sr.timedOut ? "timed out" : sr.exitCode != null ? `exit ${sr.exitCode}` : failed ? "failed" : "";
      const cut = (sr.stdout + sr.stderr).match(TRUNCATED);
      return {
        chip: [status, lines ? plural(lines, "line") : "no output", cut ? "✂ cut" : ""].filter(Boolean).join(" · "),
        bad: r.failed, body: r.el,
      };
    }
    const err = parsed && typeof parsed === "object" && parsed.ok === false;
    // A tool that ran but reports a negative outcome (valid=false,
    // status=error) is a failure to the reader.
    const negative = !!parsed && typeof parsed === "object" && (["valid", "success", "passed", "converged"].some((k) => parsed[k] === false)
      || /^(error|failed|failure|invalid)$/i.test(String(parsed.status ?? "")));
    // A successful write, edit or send says nothing the call didn't.
    if (!failed && !err && ["write", "edit", "multiedit", "patch", "send"].includes(kind)) return { chip: "✓", bad: false, body: null };
    // A spawn's result is the child's address.
    if (!failed && !err && kind === "spawn" && parsed && typeof parsed === "object" && parsed.address)
      return { chip: `✓ ${parsed.address}`, bad: false, body: null };
    if (failed || err) {
      const msg = err ? `${parsed.code || "error"}${parsed.error ? ": " + parsed.error : ""}` : String(text || "failed");
      return { chip: clip(firstLine(msg), 70), bad: true, body: lineCount(msg) > 1 || msg.length > 70 ? codeBlock(msg, { title: "error", className: "err" }) : null };
    }
    if (r?.el) {
      const n = parsed?.content != null ? lineCount(parsed.content) : 0;
      const chip = kind === "read" ? r.summary : r.summary ? clip(r.summary, 70) : "✓";
      return { chip: chip || (n ? plural(n, "line") : "✓"), bad: negative, body: r.el };
    }
    if (parsed && typeof parsed === "object") {
      const brief = ["write", "edit", "multiedit", "patch", "send"].includes(kind) ? "" : compactDict(parsed);
      return { chip: brief ? clip(brief, 70) : "✓", bad: negative, body: null };
    }
    const t = String(text || "").trim();
    if (!t) return { chip: "✓", bad: false, body: null };
    const n = lineCount(t);
    if (n <= 1 && t.length <= 70) return { chip: t, bad: false, body: null };
    return { chip: plural(n, "line"), bad: false, body: codeBlock(t, { title: "result", collapse: 20 }) };
  }

  function guessKind(parsed) {
    if (!parsed || typeof parsed !== "object") return "other";
    if ("stdout" in parsed && ("exit_code" in parsed || "stderr" in parsed)) return "shell";
    if ("content" in parsed && "path" in parsed) return "read";
    return "other";
  }
  function shellSummary(r, failed) {
    const status = r.timedOut ? "timed out" : r.exitCode != null ? `exit ${r.exitCode}` : failed ? "failed" : "";
    const lines = lineCount(r.stdout) + lineCount(r.stderr);
    const last = (r.stderr && Number(r.exitCode) !== 0 ? r.stderr : r.stdout).replace(/\n+$/, "").split("\n").pop() || "";
    const cut = (r.stdout + r.stderr).match(TRUNCATED);
    return [status, lines ? plural(lines, "line") : "no output", cut ? `✂ ${Number(cut[1]).toLocaleString()} chars cut` : "", clip(last.trim(), 100)].filter(Boolean).join(" · ");
  }

  // ---------- messages between agents ----------

  const HASH = /^(?:sha\d*[:-])?[0-9a-f]{32,}$/i;
  /** A workspace-relative file path worth linking ("results/fit.json"). */
  const isRelPath = (v) => typeof v === "string" && v.length < 300 && !/\s/.test(v) && !/^(\/|~|\.\.|[a-z][\w+.-]*:)/i.test(v)
    && (v.includes("/") || /\.[a-z0-9]{1,8}$/i.test(v)) && !/^\d+(\.\d+)*$/.test(v);
  /** A message body as sent: agents often send JSON as a string. */
  function parseBody(body) {
    if (typeof body !== "string") return body;
    const t = body.trim();
    if ((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"))) {
      try { return JSON.parse(t); } catch { /* prose that happens to start with a brace */ }
    }
    return body;
  }
  const shortScalar = (v) => {
    const s = String(v);
    return HASH.test(s) ? `${s.slice(0, 10)}…` : clip(s.replace(/\s+/g, " "), 48);
  };
  function compactFields(o) {
    const parts = [];
    let more = 0;
    for (const [k, v] of Object.entries(o)) {
      if (v == null || v === "") continue;
      if (parts.length >= 3) { more++; continue; }
      if (Array.isArray(v)) parts.push(`${k}=[${v.length}]`);
      else if (typeof v === "object") parts.push(`${k}={…}`);
      else parts.push(`${k}=${shortScalar(v)}`);
    }
    return parts.join(" · ") + (more ? ` · +${more}` : "");
  }
  /** One line for a message: its first paragraph, or for a structured
   *  one its kind and first few fields, paths relative to ``roots``. */
  function bodySummary(body, roots) {
    const b = parseBody(body);
    if (typeof b === "string") return firstParagraph(relText(b, roots));
    if (Array.isArray(b)) return `${b.length} item${b.length === 1 ? "" : "s"}`;
    if (!b || typeof b !== "object") return String(b ?? "");
    const r = relArgs(b, roots);
    const keys = Object.keys(r);
    const inner = keys.length === 1 ? r[keys[0]] : null;
    if (inner && typeof inner === "object" && !Array.isArray(inner)) return `${keys[0]} · ${compactFields(inner)}`;
    const kind = typeof r.kind === "string" ? r.kind : typeof r.type === "string" ? r.type : "";
    if (kind) {
      const rest = { ...r };
      delete rest.kind; delete rest.type;
      return `${kind} · ${compactFields(rest)}`;
    }
    return compactFields(r);
  }
  /** A message, readable: prose as markdown; a structured one as
   *  labelled fields under its kind, file paths relative to ``roots``
   *  and opening in the files view (``openPath``), long hashes shortened. */
  function messageView(body, { roots, renderMarkdown, openPath } = {}) {
    const b = parseBody(body);
    if (typeof b === "string") {
      const text = relText(b, roots);
      return renderMarkdown ? h("div", { class: "resp" }, renderMarkdown(text)) : h("pre", { text });
    }
    if (!b || typeof b !== "object" || Array.isArray(b)) return codeBlock(JSON.stringify(b, null, 2), { lang: "json" });
    let r = relArgs(b, roots);
    let kind = "";
    const keys = Object.keys(r);
    if (keys.length === 1 && r[keys[0]] && typeof r[keys[0]] === "object" && !Array.isArray(r[keys[0]])) {
      kind = keys[0];
      r = r[keys[0]];
    } else if (typeof r.kind === "string") {
      kind = r.kind;
      r = { ...r };
      delete r.kind;
    }
    const box = fields(r, { renderMarkdown, prose: true });
    for (const el of box.querySelectorAll("code.fval")) {
      const t = el.textContent;
      if (HASH.test(t)) { el.textContent = `${t.slice(0, 12)}…`; el.title = t; el.classList.add("hash"); }
      else if (openPath && isRelPath(t)) {
        el.classList.add("path");
        el.title = "open in files";
        el.addEventListener("click", (e) => { e.stopPropagation(); openPath(t); });
      }
    }
    return h("div", { class: "msgview" }, kind ? h("span", { class: "msg-kind", text: kind }) : null, box);
  }

  window.ConjureCode = {
    codeBlock, diffBlock, unifiedDiffBlock, highlightInto, langForPath, langForTag,
    parseLiteral, toolKind, callSummary, callPreview, renderCall, renderResult, fields, terminal,
    resultForCall, relArgs, relText, bodySummary, messageView, parseBody,
  };
})();
