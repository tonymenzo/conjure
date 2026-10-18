/* conjure web UI — the files view.
 *
 * Every workspace in the run (the root's, shared by the agents that
 * work in it, and any nested one an agent was given), a lazily loaded
 * tree of each, search, the recently modified files, and a preview:
 * code highlighted with line numbers, markdown rendered (its relative
 * images and links resolved in the workspace), CSV as a table,
 * notebooks as cells, images, and PDFs in the browser's viewer.
 *
 * File bytes arrive through fetch (the token travels in a header) as
 * blobs. Images go in <img>, which runs no script, even in an SVG; a
 * PDF goes in a frame typed application/pdf, which the PDF viewer
 * renders; nothing an agent wrote is ever loaded as a page.
 * Exposed as window.ConjureFiles(deps) for app.js.
 */
"use strict";
(() => {
  window.ConjureFiles = function createFiles({ h, fill, api, apiBlob, renderMarkdown, isLive, onPath }) {
    const $ = (sel) => document.querySelector(sel);
    const CC = () => window.ConjureCode;

    const st = {
      workspaces: [],
      ws: null,                 // the open workspace's id
      agent: null,              // the agent it was opened for
      trees: new Map(),         // ws id -> { expanded: Set, cache: Map(dir -> entries) }
      tab: "tree",              // "tree" | "recent"
      query: "",
      hidden: false,
      results: null,            // search results for st.query
      resultsTruncated: false,
      recent: null,
      recentAt: 0,
      rows: [],                 // visible rows, in order, for the keyboard
      cursor: null,             // the keyboard's row (a path)
      file: null,               // { ws, path, meta }
      modes: {},                // preferred view per file type: rendered | source
      blobs: [],                // object URLs to revoke on the next preview
      seq: 0,                   // guards against stale responses
      wired: false,
    };

    // ---------- formatting ----------
    const size = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(2)} GB`);
    function ago(ts) {
      if (!ts) return "";
      const s = Math.max(0, Date.now() / 1000 - ts);
      if (s < 45) return "just now";
      if (s < 3600) return `${Math.round(s / 60)}m ago`;
      if (s < 86400) return `${Math.round(s / 3600)}h ago`;
      if (s < 7 * 86400) return `${Math.round(s / 86400)}d ago`;
      return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }
    const stamp = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");
    const extOf = (name) => { const m = /\.([^./]+)$/.exec(name || ""); return m ? m[1].toLowerCase() : ""; };
    const dirOf = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
    const baseOf = (path) => path.slice(path.lastIndexOf("/") + 1);
    const TYPES = {
      code: "py pyi ipynb js mjs ts tsx jsx c h cc cpp cxx hpp hh cu f f77 f90 f95 f03 for jl sh bash zsh rs go java kt rb pl lua r sql swift php cmake mk",
      data: "json jsonl yaml yml toml ini cfg conf csv tsv dat lhe hepmc root h5 hdf5 npz npy parquet xml txt log out err",
      doc: "md markdown rst tex bib sty cls html htm",
      img: "png jpg jpeg gif webp avif bmp ico svg eps",
      pdf: "pdf",
    };
    const TYPE_OF = new Map(Object.entries(TYPES).flatMap(([t, exts]) => exts.split(" ").map((e) => [e, t])));
    const typeOf = (name) => TYPE_OF.get(extOf(name)) || "other";
    const KIND_NAMES = { ipynb: "jupyter notebook", csv: "csv", tsv: "tsv", md: "markdown", txt: "text", log: "log" };

    // ---------- data ----------
    const ws = () => st.workspaces.find((w) => w.id === st.ws) || null;
    function tree(id = st.ws) {
      if (!st.trees.has(id)) st.trees.set(id, { expanded: new Set(), cache: new Map() });
      return st.trees.get(id);
    }
    const params = (extra) => ({ addr: st.ws, hidden: st.hidden ? 1 : "", ...extra });
    async function loadWorkspaces() {
      try {
        const d = await api("files/workspaces");
        st.workspaces = d.workspaces || [];
      } catch { /* keep the last list */ }
    }
    async function listDir(path) {
      const t = tree();
      try {
        const d = await api("files/list", params({ path }));
        t.cache.set(path, d.entries || []);
        return true;
      } catch (err) {
        t.cache.set(path, { error: err.message });
        return false;
      }
    }
    /** The workspace an agent works in: the one listing it. */
    const workspaceOf = (addr) => st.workspaces.find((w) => w.agents.some((a) => a.addr === addr)) || null;
    /** A nested workspace whose folder is ``path`` in the open one. */
    const nestedAt = (path) => st.workspaces.find((w) => w.parent === st.ws && w.rel === path) || null;

    // ---------- public ----------
    async function open(addr, path) {
      wire();
      await loadWorkspaces();
      const target = (addr && workspaceOf(addr)) || ws() || st.workspaces[0] || null;
      st.agent = addr || st.agent;
      if (!target) {
        renderWorkspaces();
        fill($("#files-tree"), h("div", { class: "fs-empty", text: "no workspace in this run" }));
        return;
      }
      await selectWorkspace(target.id, { path, keepFile: target.id === st.ws && !path });
    }
    async function selectWorkspace(id, { path = null, keepFile = false } = {}) {
      const changed = id !== st.ws;
      st.ws = id;
      if (changed) { st.results = null; st.recent = null; st.cursor = null; }
      renderWorkspaces();
      const w = ws();
      if (!w.exists) {
        fill($("#files-tree"), h("div", { class: "fs-empty", text: isLive() ? "not created yet: its agents haven't written a file" : "not in this copy of the run" }));
        if (!keepFile) clearPreview();
        return;
      }
      const t = tree();
      if (!t.cache.has("")) await listDir("");
      if (st.query) await runSearch();
      if (st.tab === "recent") await loadRecent(true);
      if (path) await reveal(path, { openFile: true });
      else {
        renderList();
        if (!keepFile || (st.file && st.file.ws !== id)) clearPreview();
      }
    }
    /** Expand the folders down to ``path`` and select it. */
    async function reveal(path, { openFile = false } = {}) {
      const t = tree();
      const parts = path.split("/").filter(Boolean);
      let acc = "";
      for (const part of parts.slice(0, -1)) {
        acc = acc ? `${acc}/${part}` : part;
        t.expanded.add(acc);
        if (!t.cache.has(acc)) await listDir(acc);
      }
      const entries = t.cache.get(dirOf(path));
      const entry = Array.isArray(entries) ? entries.find((e) => e.path === path) : null;
      if (entry?.dir) {
        t.expanded.add(path);
        if (!t.cache.has(path)) await listDir(path);
      }
      if (st.tab !== "tree" || st.query) { st.tab = "tree"; st.query = ""; $("#files-filter").value = ""; renderTabs(); }
      st.cursor = path;
      renderList();
      if (openFile && entry && !entry.dir) await show(path);
      else if (openFile && !entry && parts.length) await show(path);
    }
    async function refresh() {
      if (!st.ws) return;
      await loadWorkspaces();
      renderWorkspaces();
      const t = tree();
      const dirs = ["", ...t.expanded].filter((d) => t.cache.has(d));
      await Promise.all(dirs.map(listDir));
      if (st.tab === "recent" && Date.now() - st.recentAt > 10_000) await loadRecent(false);
      renderList({ keepScroll: true });
      // The open file changed on disk: show the new version.
      const f = st.file;
      if (f && f.ws === st.ws) {
        const listed = t.cache.get(dirOf(f.path));
        const now = Array.isArray(listed) ? listed.find((e) => e.path === f.path) : null;
        if (now && (now.mtime !== f.meta.mtime || now.size !== f.meta.size)) await show(f.path, { keepScroll: true });
      }
    }
    const focusFilter = () => { const el = $("#files-filter"); el.focus(); el.select(); };

    // ---------- workspaces ----------
    function renderWorkspaces() {
      const box = $("#files-workspaces");
      if (!st.workspaces.length) { fill(box, h("div", { class: "fs-empty", text: "none yet" })); return; }
      fill(box, st.workspaces.map((w) => {
        const labels = new Map();
        for (const a of w.agents) labels.set(a.label, (labels.get(a.label) || 0) + 1);
        const chips = [...labels].slice(0, 3).map(([label, n]) =>
          h("span", { class: "ws-agent" + (w.agents.some((a) => a.addr === st.agent && a.label === label) ? " me" : ""), text: n > 1 ? `${label} ×${n}` : label }));
        if (labels.size > 3) chips.push(h("span", { class: "ws-agent more", text: `+${labels.size - 3}` }));
        return h("button", {
          class: "ws" + (w.id === st.ws ? " active" : "") + (w.exists ? "" : " missing"),
          style: `--depth: ${w.depth}`,
          title: w.path,
          onclick: () => selectWorkspace(w.id),
        },
        h("span", { class: "ws-name" }, w.depth ? h("span", { class: "ws-rel", text: w.rel + "/" }) : h("span", { text: w.name + "/" })),
        h("span", { class: "ws-agents" }, chips));
      }));
    }

    // ---------- the list: tree, search results or recent ----------
    function renderTabs() {
      for (const b of document.querySelectorAll("#files-tabs button")) b.classList.toggle("active", b.dataset.tab === st.tab && !st.query);
    }
    function renderList({ keepScroll = false } = {}) {
      const box = $("#files-tree");
      const top = box.scrollTop;
      st.rows = [];
      let rows;
      if (st.query) rows = flatRows(st.results, { empty: st.results ? "no matches" : "searching…", search: true });
      else if (st.tab === "recent") rows = flatRows(st.recent, { empty: st.recent ? "no files yet" : "loading…", recent: true });
      else rows = treeRows("", 0);
      fill(box, rows);
      if (keepScroll) box.scrollTop = top;
      markCursor();
    }
    function treeRows(dir, depth) {
      const t = tree();
      const entries = t.cache.get(dir);
      if (entries === undefined) return depth ? [h("div", { class: "frow note", style: `--depth: ${depth}`, text: "loading…" })] : [];
      if (!Array.isArray(entries)) return [h("div", { class: "frow note err", style: `--depth: ${depth}`, text: entries.error })];
      if (!entries.length) return [h("div", { class: "frow note", style: `--depth: ${depth}`, text: depth ? "empty" : "empty workspace" })];
      const out = [];
      for (const e of entries) {
        out.push(row(e, depth));
        if (e.dir && t.expanded.has(e.path)) out.push(...treeRows(e.path, depth + 1));
      }
      return out;
    }
    function flatRows(entries, { empty, search = false, recent = false }) {
      if (!entries) return [h("div", { class: "frow note", text: empty })];
      if (entries.error) return [h("div", { class: "frow note err", text: entries.error })];
      if (!entries.length) return [h("div", { class: "frow note", text: empty })];
      const out = entries.map((e) => row(e, 0, { flat: true, recent }));
      if (search && st.resultsTruncated) out.push(h("div", { class: "frow note", text: "more matches: refine the search" }));
      return out;
    }
    function row(e, depth, { flat = false, recent = false } = {}) {
      const t = tree();
      const open = e.dir && t.expanded.has(e.path);
      const nested = e.dir ? nestedAt(e.path) : null;
      st.rows.push({ path: e.path, dir: e.dir, depth, flat });
      const selected = st.file && st.file.ws === st.ws && st.file.path === e.path;
      const where = flat ? dirOf(e.path) : "";
      return h("div", {
        class: `frow${e.dir ? " dir" : ""}${flat ? " flat" : ""}${selected ? " selected" : ""}${e.link ? " link" : ""}`,
        role: "treeitem",
        "aria-expanded": e.dir && !flat ? String(open) : null,
        "aria-selected": selected ? "true" : null,
        style: `--depth: ${depth}`,
        dataset: { path: e.path, dir: e.dir ? "1" : "" },
        title: e.path + (e.link ? " (symlink)" : ""),
      },
      h("span", { class: "chev", text: e.dir && !flat ? (open ? "▾" : "▸") : "" }),
      h("span", { class: `ficon t-${e.dir ? "dir" : typeOf(e.name)}`, "aria-hidden": "true" }),
      h("span", { class: "fname" }, h("span", { class: "nm", text: e.name + (e.dir ? "/" : "") }), where ? h("span", { class: "where", text: where }) : null),
      nested ? h("span", { class: "ws-badge", title: `workspace of ${nested.agents.map((a) => a.label).join(", ")}`, text: nested.agents[0].label }) : null,
      h("span", { class: "fmeta", title: stamp(e.mtime), text: recent ? ago(e.mtime) : e.dir ? "" : size(e.size) }));
    }
    function markCursor() {
      const box = $("#files-tree");
      let found = null;
      for (const el of box.querySelectorAll(".frow[data-path]")) {
        const on = el.dataset.path === st.cursor;
        el.classList.toggle("cursor", on);
        if (on) found = el;
      }
      if (found) { found.id = "files-cursor"; box.setAttribute("aria-activedescendant", "files-cursor"); }
      return found;
    }
    async function toggle(path) {
      const t = tree();
      if (t.expanded.has(path)) t.expanded.delete(path);
      else {
        t.expanded.add(path);
        if (!t.cache.has(path)) { renderList({ keepScroll: true }); await listDir(path); }
      }
      st.cursor = path;
      renderList({ keepScroll: true });
    }
    async function activate(path, isDir) {
      st.cursor = path;
      if (isDir && (st.query || st.tab === "recent")) return reveal(path);
      if (isDir) return toggle(path);
      markCursor();
      await show(path);
    }
    async function runSearch() {
      const q = st.query;
      const seq = ++st.seq;
      try {
        const d = await api("files/find", params({ q }));
        if (seq !== st.seq || q !== st.query) return;
        st.results = d.entries || [];
        st.resultsTruncated = !!d.truncated;
      } catch (err) {
        if (seq !== st.seq) return;
        st.results = { error: err.message };
      }
      st.cursor = st.results?.[0]?.path ?? null;
      renderList();
    }
    async function loadRecent(render = true) {
      try {
        const d = await api("files/recent", params());
        st.recent = d.entries || [];
      } catch (err) {
        st.recent = { error: err.message };
      }
      st.recentAt = Date.now();
      if (render) renderList();
    }

    // ---------- preview ----------
    function clearPreview() {
      st.file = null;
      revokeBlobs();
      fill($("#files-head"));
      fill($("#files-content"), h("div", { class: "empty" },
        h("div", { text: "select a file" }),
        h("div", { class: "dim small", text: "↑↓ move · → open folder · ← close · enter preview · / search" })));
      onPath?.(null, null);
    }
    function revokeBlobs() {
      for (const u of st.blobs) URL.revokeObjectURL(u);
      st.blobs = [];
    }
    async function blobUrl(path, type, wsId = st.ws) {
      let blob = await apiBlob("files/raw", { addr: wsId, path });
      if (type) blob = new Blob([blob], { type });
      const url = URL.createObjectURL(blob);
      st.blobs.push(url);
      return url;
    }
    async function show(path, { keepScroll = false } = {}) {
      const seq = ++st.seq;
      const content = $("#files-content");
      const top = content.scrollTop;
      let meta;
      try {
        meta = await api("files/read", { addr: st.ws, path });
      } catch (err) {
        if (seq !== st.seq) return;
        st.file = { ws: st.ws, path, meta: {} };
        fill($("#files-head"), head({ path, name: baseOf(path) }, []));
        fill(content, h("div", { class: "empty err", text: err.message }));
        return;
      }
      if (seq !== st.seq) return;
      revokeBlobs();
      st.file = { ws: st.ws, path: meta.path, meta };
      onPath?.(meta.path, st.ws);
      for (const el of document.querySelectorAll("#files-tree .frow")) {
        const on = el.dataset.path === meta.path;
        el.classList.toggle("selected", on);
        if (on) el.setAttribute("aria-selected", "true"); else el.removeAttribute("aria-selected");
      }
      const ext = extOf(meta.name);
      const modes = modesFor(meta, ext);
      const mode = modes.length ? (modes.includes(st.modes[ext]) ? st.modes[ext] : modes[0]) : null;
      fill($("#files-head"), head(meta, modes, mode));
      let body;
      try {
        body = await renderBody(meta, ext, mode);
      } catch (err) {
        body = h("div", { class: "empty err", text: `could not show this file: ${err.message}` });
      }
      if (seq !== st.seq) return;
      fill(content, body);
      content.scrollTop = keepScroll ? top : 0;
    }
    function modesFor(meta, ext) {
      if (meta.kind === "text" && (ext === "md" || ext === "markdown")) return ["rendered", "source"];
      if (meta.kind === "text" && (ext === "csv" || ext === "tsv")) return ["table", "source"];
      if (meta.kind === "text" && ext === "ipynb") return ["notebook", "source"];
      if (meta.kind === "image" && ext === "svg") return ["image", "source"];
      return [];
    }
    function head(meta, modes, mode) {
      const w = ws();
      const crumbs = [h("a", { class: "crumb root", text: w ? (w.depth ? w.rel : w.name) : "workspace", onclick: () => reveal("") })];
      const parts = (meta.path || "").split("/");
      let acc = "";
      parts.forEach((part, k) => {
        acc = acc ? `${acc}/${part}` : part;
        const p = acc;
        crumbs.push(h("span", { class: "sep", text: "/" }));
        crumbs.push(k === parts.length - 1 ? h("b", { class: "crumb", text: part }) : h("a", { class: "crumb", text: part, onclick: () => reveal(p) }));
      });
      const ext = extOf(meta.name);
      const lang = meta.kind !== "text" ? meta.kind : KIND_NAMES[ext] || CC()?.langForPath(meta.name) || ext || "text";
      const lines = typeof meta.content === "string" ? meta.content.split("\n").length - (meta.content.endsWith("\n") ? 1 : 0) : 0;
      const facts = [lang, meta.size != null ? size(meta.size) : "", lines ? `${lines.toLocaleString()} lines` : "",
        meta.mtime ? `modified ${ago(meta.mtime)}` : ""].filter(Boolean);
      const tools = [];
      if (modes.length) {
        tools.push(h("span", { class: "seg", role: "group" }, modes.map((m) =>
          h("button", { class: m === mode ? "active" : "", text: m, onclick: () => { st.modes[extOf(meta.name)] = m; show(meta.path, { keepScroll: false }); } }))));
      }
      tools.push(h("button", { class: "small-btn", title: "Copy the path, relative to the workspace", text: "copy path", onclick: (e) => copy(meta.path, e.currentTarget) }));
      if (meta.size != null) tools.push(h("button", { class: "small-btn", text: "download", disabled: meta.size > meta.raw_limit, title: meta.size > meta.raw_limit ? "too large to download here" : null, onclick: () => download(meta) }));
      return h("div", { class: "fh" },
        h("div", { class: "fh-path" }, crumbs),
        h("div", { class: "fh-row" },
          h("span", { class: "fh-facts", title: stamp(meta.mtime), text: facts.join(" · ") }),
          h("span", { class: "fh-tools" }, tools)));
    }
    async function copy(text, btn) {
      try { await navigator.clipboard.writeText(text); btn.textContent = "copied"; }
      catch { btn.textContent = "copy failed"; }
      setTimeout(() => { btn.textContent = "copy path"; }, 1200);
    }
    async function download(meta) {
      const url = await blobUrl(meta.path, "application/octet-stream");
      const a = h("a", { href: url, download: meta.name });
      document.body.append(a);
      a.click();
      a.remove();
    }
    async function renderBody(meta, ext, mode) {
      if (meta.kind === "image" && mode !== "source") return imageView(meta);
      if (meta.kind === "image") {
        const blob = await apiBlob("files/raw", { addr: st.ws, path: meta.path });
        return sourceView(await blob.text(), meta.name);
      }
      if (meta.kind === "pdf") {
        if (meta.size > meta.raw_limit) return tooLarge(meta);
        const url = await blobUrl(meta.path, "application/pdf");
        return h("iframe", { class: "fv-pdf", src: url, title: meta.name });
      }
      if (meta.kind === "binary") {
        return h("div", { class: "fv-binary" },
          h("div", { class: "big", text: "binary file" }),
          h("div", { class: "dim", text: `${size(meta.size)} · no preview` }),
          h("button", { class: "small-btn", text: "download", disabled: meta.size > meta.raw_limit, onclick: () => download(meta) }));
      }
      const text = meta.content || "";
      const parts = [];
      if (meta.truncated) {
        parts.push(h("div", { class: "fv-note" }, `Showing the first ${size(text.length)} of ${size(meta.size)}. `,
          h("a", { text: "download", onclick: () => download(meta) }), " for the rest."));
      }
      const view = mode === "rendered" ? markdownView(text, meta.path)
        : mode === "table" ? tableView(text, ext === "tsv" ? "\t" : ",")
        : mode === "notebook" ? notebookView(text)
        : sourceView(ext === "json" ? prettyJson(text) : text, meta.name);
      return parts.concat(view);
    }
    const tooLarge = (meta) => h("div", { class: "fv-binary" }, h("div", { class: "big", text: "too large to preview" }), h("div", { class: "dim", text: size(meta.size) }));

    function imageView(meta) {
      const img = h("img", { alt: meta.name, decoding: "async" });
      const box = h("div", { class: "fv-image fit" }, img);
      const dims = h("span", { class: "fv-dims" });
      img.addEventListener("load", () => { dims.textContent = `${img.naturalWidth} × ${img.naturalHeight}`; });
      img.addEventListener("error", () => fill(box, h("div", { class: "empty err", text: "this image could not be decoded" })));
      img.addEventListener("click", () => box.classList.toggle("fit"));
      img.title = "click: actual size / fit";
      blobUrl(meta.path, meta.name.toLowerCase().endsWith(".svg") ? "image/svg+xml" : null).then((u) => { img.src = u; })
        .catch((err) => fill(box, h("div", { class: "empty err", text: err.message })));
      return [box, h("div", { class: "fv-caption" }, dims)];
    }
    function sourceView(text, name) {
      const lines = text.endsWith("\n") ? text.slice(0, -1).split("\n") : text.split("\n");
      const code = h("code", { class: "src" });
      const lang = CC()?.langForPath(name) || "";
      if (CC() && text.length <= 300_000) CC().highlightInto(code, lines.join("\n"), lang);
      else code.textContent = lines.join("\n");
      return h("div", { class: "fv-source" },
        h("pre", { class: "code" }, h("span", { class: "ln", "aria-hidden": "true", text: lines.map((_, i) => i + 1).join("\n") }), code));
    }
    function prettyJson(text) {
      if (text.length > 2_000_000 || text.trim().split("\n").length > 3) return text;
      try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
    }

    /** Markdown, with relative images loaded from the workspace and
     *  relative links opening the file they name. */
    function markdownView(text, path) {
      // Resolve against the outermost workspace, so ``../../x`` from a
      // nested workspace reaches its parent's files.
      const chain = [];
      for (let w = ws(); w; w = st.workspaces.find((x) => x.id === w.parent)) chain.unshift(w);
      const prefixes = chain.map((_, k) => chain.slice(1, k + 1).map((w) => w.rel).join("/"));
      const here = [prefixes[prefixes.length - 1], dirOf(path)].filter(Boolean).join("/");
      const resolveRel = (href) => {
        const clean = href.split(/[?#]/)[0];
        const parts = (clean.startsWith("/") ? [] : here.split("/").filter(Boolean));
        for (const p of clean.split("/")) {
          if (!p || p === ".") continue;
          if (p === "..") parts.pop(); else parts.push(decodeURIComponent(p));
        }
        const full = parts.join("/");
        for (let k = chain.length - 1; k >= 0; k--) {
          const pre = prefixes[k];
          if (!pre || full === pre || full.startsWith(pre + "/")) return { ws: chain[k].id, path: pre ? full.slice(pre.length + 1) : full };
        }
        return { ws: st.ws, path: full };
      };
      const ctx = {
        image(src, alt) {
          if (/^[a-z][\w+.-]*:/i.test(src)) return h("span", { class: "md-img-ext", text: `[image: ${alt || src}]`, title: src });
          const img = h("img", { class: "md-img", alt, title: alt || src });
          const t = resolveRel(src);
          blobUrl(t.path, src.toLowerCase().endsWith(".svg") ? "image/svg+xml" : null, t.ws)
            .then((u) => { img.src = u; })
            .catch(() => img.replaceWith(h("span", { class: "md-img-ext", text: `[missing image: ${src}]` })));
          return img;
        },
        link(href, label) {
          if (href.startsWith("#")) return h("span", { text: label });
          const t = resolveRel(href);
          const go = (e) => { e.preventDefault(); t.ws === st.ws ? reveal(t.path, { openFile: true }) : selectWorkspace(t.ws, { path: t.path }); };
          return h("a", { class: "md-file-link", title: t.path, onclick: go }, label);
        },
      };
      return h("article", { class: "fv-markdown resp" }, renderMarkdown(text, ctx));
    }

    function parseDelimited(text, sep) {
      const rows = [];
      let row = [], field = "", quoted = false;
      for (let i = 0; i < text.length; i++) {
        const c = text[i];
        if (quoted) {
          if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
          else if (c === '"') quoted = false;
          else field += c;
        } else if (c === '"' && field === "") quoted = true;
        else if (c === sep) { row.push(field); field = ""; }
        else if (c === "\n" || c === "\r") {
          if (c === "\r" && text[i + 1] === "\n") i++;
          row.push(field); field = "";
          rows.push(row); row = [];
          if (rows.length > 5000) break;
        } else field += c;
      }
      if (field || row.length) { row.push(field); rows.push(row); }
      return rows;
    }
    function tableView(text, sep) {
      const rows = parseDelimited(text, sep).filter((r) => r.length > 1 || r[0] !== "");
      if (!rows.length) return h("div", { class: "empty", text: "empty table" });
      const LIMIT = 2000;
      const [header, ...body] = rows;
      const shown = body.slice(0, LIMIT);
      const numeric = header.map((_, k) => shown.length > 0 && shown.every((r) => r[k] === undefined || r[k] === "" || !isNaN(Number(r[k]))));
      const table = h("table", { class: "fv-table" },
        h("thead", null, h("tr", null, h("th", { class: "rn", text: "" }), header.map((c, k) => h("th", { class: numeric[k] ? "num" : "", text: c })))),
        h("tbody", null, shown.map((r, n) => h("tr", null, h("td", { class: "rn", text: String(n + 1) }), header.map((_, k) => h("td", { class: numeric[k] ? "num" : "", text: r[k] ?? "" }))))));
      const note = `${body.length.toLocaleString()} rows × ${header.length} columns` + (body.length > LIMIT ? ` · showing the first ${LIMIT.toLocaleString()}` : "");
      return [h("div", { class: "fv-caption", text: note }), h("div", { class: "fv-table-wrap" }, table)];
    }

    /** A Jupyter notebook: markdown cells rendered, code cells
     *  highlighted, and their text, image and error outputs. */
    function notebookView(text) {
      let nb;
      try { nb = JSON.parse(text); } catch { return sourceView(text, "notebook.json"); }
      const lang = nb?.metadata?.language_info?.name || nb?.metadata?.kernelspec?.language || "python";
      const src = (s) => (Array.isArray(s) ? s.join("") : String(s ?? ""));
      const ansi = (s) => s.replace(/\x1b\[[0-9;]*m/g, "");
      const cells = (nb.cells || []).slice(0, 300).map((cell) => {
        if (cell.cell_type === "markdown") return h("div", { class: "nb-cell md fv-markdown resp" }, renderMarkdown(src(cell.source)));
        if (cell.cell_type !== "code") return h("div", { class: "nb-cell raw" }, h("pre", { text: src(cell.source) }));
        const n = cell.execution_count;
        const outs = (cell.outputs || []).map((o) => {
          if (o.output_type === "stream") return h("pre", { class: "nb-out" + (o.name === "stderr" ? " err" : ""), text: ansi(src(o.text)) });
          if (o.output_type === "error") return h("pre", { class: "nb-out err", text: ansi((o.traceback || [`${o.ename}: ${o.evalue}`]).join("\n")) });
          const data = o.data || {};
          if (data["image/png"]) return h("img", { class: "nb-img", src: `data:image/png;base64,${src(data["image/png"]).replace(/\s/g, "")}`, alt: "output" });
          if (data["image/jpeg"]) return h("img", { class: "nb-img", src: `data:image/jpeg;base64,${src(data["image/jpeg"]).replace(/\s/g, "")}`, alt: "output" });
          if (data["text/markdown"]) return h("div", { class: "nb-out resp" }, renderMarkdown(src(data["text/markdown"])));
          if (data["text/plain"]) return h("pre", { class: "nb-out", text: src(data["text/plain"]) });
          return null;
        });
        return h("div", { class: "nb-cell code" },
          h("div", { class: "nb-prompt", text: `In [${n ?? " "}]` }),
          h("div", { class: "nb-body" }, CC() ? CC().codeBlock(src(cell.source), { lang, langLabel: lang }) : h("pre", { text: src(cell.source) }), outs));
      });
      if ((nb.cells || []).length > 300) cells.push(h("div", { class: "fv-note", text: `showing 300 of ${nb.cells.length} cells` }));
      return h("div", { class: "fv-notebook" }, cells);
    }

    // ---------- events ----------
    function wire() {
      if (st.wired) return;
      st.wired = true;
      const box = $("#files-tree");
      box.addEventListener("click", (e) => {
        const el = e.target.closest(".frow[data-path]");
        if (!el) return;
        if (e.target.closest(".ws-badge")) {
          const nested = nestedAt(el.dataset.path);
          if (nested) { selectWorkspace(nested.id); return; }
        }
        activate(el.dataset.path, !!el.dataset.dir);
        box.focus({ preventScroll: true });
      });
      box.addEventListener("keydown", (e) => {
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        const k = st.rows.findIndex((r) => r.path === st.cursor);
        const cur = st.rows[k];
        const go = (i) => {
          const r = st.rows[Math.max(0, Math.min(st.rows.length - 1, i))];
          if (!r) return;
          st.cursor = r.path;
          markCursor()?.scrollIntoView({ block: "nearest" });
        };
        const t = tree();
        let handled = true;
        switch (e.key) {
          case "ArrowDown": case "j": go(k < 0 ? 0 : k + 1); break;
          case "ArrowUp": case "k": go(k < 0 ? 0 : k - 1); break;
          case "Home": go(0); break;
          case "End": go(st.rows.length - 1); break;
          case "PageDown": go(k + 15); break;
          case "PageUp": go(k - 15); break;
          case "ArrowRight": case "l":
            if (cur?.dir && !cur.flat && !t.expanded.has(cur.path)) toggle(cur.path);
            else if (cur?.dir && !cur.flat) go(k + 1);
            break;
          case "ArrowLeft": case "h":
            if (cur?.dir && !cur.flat && t.expanded.has(cur.path)) toggle(cur.path);
            else if (cur && !cur.flat && dirOf(cur.path)) { st.cursor = dirOf(cur.path); markCursor()?.scrollIntoView({ block: "nearest" }); }
            break;
          case "Enter": case " ": if (cur) activate(cur.path, cur.dir); break;
          case "/": focusFilter(); break;
          default: handled = false;
        }
        if (handled) { e.preventDefault(); e.stopPropagation(); }
      });
      box.addEventListener("focus", () => { if (!st.cursor && st.rows.length) { st.cursor = st.rows[0].path; markCursor(); } });

      const filter = $("#files-filter");
      let timer = null;
      filter.addEventListener("input", () => {
        clearTimeout(timer);
        const q = filter.value.trim();
        timer = setTimeout(() => {
          st.query = q;
          st.results = null;
          renderTabs();
          if (q) { renderList(); runSearch(); } else renderList();
        }, 180);
      });
      filter.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { filter.value = ""; st.query = ""; renderTabs(); renderList(); box.focus(); e.preventDefault(); }
        else if (e.key === "ArrowDown" || e.key === "Enter") {
          e.preventDefault();
          box.focus();
          if (st.rows.length) { st.cursor = st.cursor || st.rows[0].path; markCursor(); }
          if (e.key === "Enter" && st.rows[0]) activate(st.cursor || st.rows[0].path, st.rows.find((r) => r.path === st.cursor)?.dir);
        }
        e.stopPropagation();
      });
      for (const b of document.querySelectorAll("#files-tabs button")) {
        b.addEventListener("click", async () => {
          st.tab = b.dataset.tab;
          if (st.query) { st.query = ""; filter.value = ""; }
          renderTabs();
          if (st.tab === "recent") { renderList(); await loadRecent(); } else renderList();
        });
      }
      $("#files-hidden").addEventListener("change", async (e) => {
        st.hidden = e.target.checked;
        const t = tree();
        t.cache.clear();
        await Promise.all(["", ...t.expanded].map(listDir));
        if (st.query) await runSearch();
        if (st.tab === "recent") await loadRecent(false);
        renderList({ keepScroll: true });
      });
    }

    return { open, refresh, focusFilter, get addr() { return st.ws; }, get path() { return st.file?.path || null; } };
  };
})();
