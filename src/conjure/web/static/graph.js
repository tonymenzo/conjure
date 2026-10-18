/* conjure web UI — the graph view.
 *
 * The team as a tidy tree: each agent a card under the agent that
 * spawned it, laid out top-down or left-to-right (whichever fits the
 * pane better, unless chosen). Messages between a parent and a child
 * ride on the connector between them with a count each way; messages
 * across branches and introductions are arcs. The view pans and zooms
 * (fit on load), subtrees collapse, hovering an agent lights up its
 * connections, and a minimap shows where you are in a large team.
 * Exposed as window.ConjureGraph(deps) for app.js.
 */
"use strict";
(() => {
  window.ConjureGraph = function createGraph({ s, api, ui, labelOf, clock, now, money, shortModel, clip, dotColor, onSelect, onOpen }) {
    const $ = (sel) => document.querySelector(sel);
    const W = 188, H = 54;                       // a card
    const GAP = { down: [22, 64], right: [14, 84] };  // [between siblings, between ranks]
    const STACK_GAP = 12, INDENT = 20;             // a column of leaf workers under their parent
    const PAD = 48;                                // around the content when fitting
    const K_MIN = 0.08, K_MAX = 2.5;

    const st = {
      data: null,
      layout: null,          // { nodes: Map, bounds, orient, key }
      view: { k: 1, x: 0, y: 0 },
      moved: false,          // the user panned or zoomed: don't refit
      orient: localPref("conjure-graph-orient", "auto"),
      collapsed: new Set(),
      hover: null,
      query: "",
      selected: null,
      dragging: null,
      pending: false,        // a render deferred until a drag ends
      wired: false,
      edgeEls: [],           // [{ el, a, b }] for hover highlighting
      nodeEls: new Map(),
    };
    function localPref(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } }
    function savePref(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }

    // ---------- data ----------
    async function refresh() {
      wire();
      try { st.data = await api("graph"); } catch { return; }
      render();
    }

    /** The visible tree: agents (minus internal ones unless shown, and
     *  minus those inside a collapsed subtree), and where each hidden
     *  agent shows (its nearest visible ancestor). */
    function visibleTree() {
      const g = st.data;
      const shown = g.nodes.filter((n) => ui().showInternal || !n.internal);
      const all = new Map(shown.map((n) => [n.addr, { ...n, kids: [], hidden: 0 }]));
      for (const n of all.values()) {
        const p = n.parent && all.get(n.parent);
        if (p) p.kids.push(n);
      }
      const agents = ui().agents;
      const born = (n) => agents.get(n.addr)?.spawned_at || 0;
      for (const n of all.values()) n.kids.sort((a, b) => born(a) - born(b) || a.label.localeCompare(b.label));
      const roots = [...all.values()].filter((n) => !(n.parent && all.get(n.parent)));
      const proxy = new Map();     // addr -> the address of the visible agent standing for it
      const count = (n) => n.kids.reduce((t, k) => t + 1 + count(k), 0);
      const walk = (n, standIn) => {
        proxy.set(n.addr, (standIn || n).addr);
        const shut = !standIn && st.collapsed.has(n.addr) && n.kids.length;
        if (shut) n.hidden = count(n);
        for (const k of n.kids) walk(k, standIn || (shut ? n : null));
      };
      roots.forEach((r) => walk(r, null));
      const visible = new Map([...all.values()].filter((n) => proxy.get(n.addr) === n.addr).map((n) => [n.addr, n]));
      for (const n of visible.values()) n.vkids = n.hidden ? [] : n.kids;
      return { visible, roots, proxy };
    }

    /** Tidy tree: leaves take the next slot along the breadth; a parent
     *  sits centred over its children. Top-down, two or more leaf
     *  workers of one parent stack in a column under it (an org chart's
     *  compact form), so a wide team stays narrow. */
    function place(tree, orient, withUser) {
      const [gb, gd] = GAP[orient];
      const stacking = orient === "down";
      const along = orient === "down" ? W + gb + INDENT : H + gb;
      const across = orient === "down" ? H + gd : W + gd;
      let slot = 0;
      const put = (n, depth) => {
        n.depth = depth + (withUser ? 1 : 0);
        n.stacked = false;
        const kids = n.vkids;
        if (!kids.length) { n.slot = slot++; return; }
        const leaves = stacking ? kids.filter((k) => !k.vkids.length) : [];
        if (leaves.length >= 2) {
          const col = slot++;
          leaves.forEach((k, i) => Object.assign(k, { depth: n.depth + 1, slot: col, stacked: true, row: i }));
          const branches = kids.filter((k) => k.vkids.length);
          branches.forEach((k) => put(k, depth + 1));
          n.slot = (col + (branches.length ? branches[branches.length - 1].slot : col)) / 2;
          return;
        }
        kids.forEach((k) => put(k, depth + 1));
        n.slot = (kids[0].slot + kids[kids.length - 1].slot) / 2;
      };
      tree.roots.forEach((r) => put(r, 0));
      const nodes = new Map(tree.visible);
      if (withUser) {
        const first = tree.roots[0];
        const mid = tree.roots.length ? (tree.roots[0].slot + tree.roots[tree.roots.length - 1].slot) / 2 : 0;
        nodes.set("@user", { addr: "@user", label: "you", status: "idle", sentinel: true, vkids: tree.roots, kids: tree.roots, depth: 0, slot: first ? mid : 0, hidden: 0 });
      }
      for (const n of nodes.values()) {
        if (orient === "down") {
          n.x = n.slot * along + (n.stacked ? INDENT : 0);
          n.y = n.depth * across + (n.stacked ? n.row * (H + STACK_GAP) : 0);
        } else { n.x = n.depth * across; n.y = n.slot * along; }
      }
      const xs = [...nodes.values()].map((n) => n.x), ys = [...nodes.values()].map((n) => n.y);
      const bounds = nodes.size
        ? { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs) + W, h: Math.max(...ys) - Math.min(...ys) + H }
        : { x: 0, y: 0, w: W, h: H };
      return { nodes, bounds, orient };
    }

    const size = () => { const r = $("#graph-wrap").getBoundingClientRect(); return { w: Math.max(200, r.width), h: Math.max(160, r.height) }; };
    function fitScale(bounds) {
      const { w, h } = size();
      return Math.min((w - PAD * 2) / bounds.w, (h - PAD * 2) / bounds.h, 1.15);
    }

    // ---------- render ----------
    function render() {
      if (!st.data) return;
      if (st.dragging) { st.pending = true; return; }
      const tree = visibleTree();
      const withUser = st.data.message.some((m) => m.from === "@user");
      let layout;
      if (st.orient === "auto") {
        const down = place(tree, "down", withUser);
        const right = place(visibleTree(), "right", withUser);
        // Prefer top-down (an org chart) unless left-to-right shows the
        // team clearly larger.
        layout = fitScale(right.bounds) > fitScale(down.bounds) * 1.35 ? right : down;
      } else layout = place(tree, st.orient, withUser);
      layout.proxy = tree.proxy;   // addresses only, so either layout can use it
      const key = `${layout.orient}|${[...layout.nodes.keys()].sort().join(",")}`;
      const changed = !st.layout || st.layout.key !== key;
      layout.key = key;
      st.layout = layout;
      draw();
      if (changed && !st.moved) fit({ animate: false });
      else applyView();
      renderControls();
    }

    function draw() {
      const L = st.layout;
      const svg = $("#graph");
      const vp = s("g", { class: "viewport" });
      const defs = s("defs", null,
        marker("arr-msg", "var(--green)"), marker("arr-cap", "var(--amber)"),
        s("filter", { id: "glow", x: "-20%", y: "-40%", width: "140%", height: "180%" },
          s("feDropShadow", { dx: 0, dy: 0, stdDeviation: 3.5, "flood-color": "var(--green)", "flood-opacity": 0.45 })));
      const under = s("g", { class: "edges" });
      const over = s("g", { class: "labels" });
      st.edgeEls = [];
      const showSpawn = $("#edge-spawn").checked, showMsg = $("#edge-message").checked, showCap = $("#edge-capability").checked;

      // Messages between the visible agents (hidden ones counted on the
      // collapsed ancestor that stands for them), per unordered pair.
      const pairs = new Map();
      const vis = (addr) => L.nodes.get(addr === "@user" ? "@user" : L.proxy.get(addr));
      for (const m of st.data.message) {
        const a = vis(m.from), b = vis(m.to);
        if (!a || !b || a === b) continue;
        const [p, q] = a.addr < b.addr ? [a, b] : [b, a];
        const key = `${p.addr}|${q.addr}`;
        const row = pairs.get(key) || { p, q, pq: 0, qp: 0, last: 0 };
        if (a === p) row.pq += m.count; else row.qp += m.count;
        row.last = Math.max(row.last, m.last_ts);
        pairs.set(key, row);
      }
      const t = now();
      const recent = (row) => ui().live && t - row.last < 6;
      const treeParent = new Map();
      for (const n of L.nodes.values()) for (const k of n.vkids || []) treeParent.set(k.addr, n.addr);
      const isTree = (a, b) => treeParent.get(b.addr) === a.addr;

      // Tree connectors, carrying the parent–child messages.
      for (const n of L.nodes.values()) {
        for (const k of n.vkids || []) {
          const [p, q] = n.addr < k.addr ? [n, k] : [k, n];
          const row = showMsg ? pairs.get(`${p.addr}|${q.addr}`) : null;
          const down = row ? (p === n ? row.pq : row.qp) : 0;   // parent → child
          const up = row ? (p === n ? row.qp : row.pq) : 0;
          if (!showSpawn && !row) continue;
          const d = elbow(n, k);
          const total = down + up;
          const cls = "e-tree" + (n.sentinel ? " user" : "") + (total ? " msg" : "") + (row && recent(row) ? " recent" : "");
          const el = s("path", { class: cls, d, "stroke-width": total ? Math.min(4, 1.3 + Math.log2(total) * 0.7) : null },
            s("title", { text: total ? `${labelOf(n.addr)} → ${labelOf(k.addr)}: ${down} · ${labelOf(k.addr)} → ${labelOf(n.addr)}: ${up}${row ? `, last ${clock(row.last)}` : ""}` : `${labelOf(n.addr)} spawned ${labelOf(k.addr)}` }));
          under.append(el);
          st.edgeEls.push({ el, a: n.addr, b: k.addr });
          if (total) {
            const pos = treeLabelAt(n, k);
            const arrows = L.orient === "down" ? ["↓", "↑"] : ["→", "←"];
            const text = [down ? `${arrows[0]}${down}` : "", up ? `${arrows[1]}${up}` : ""].filter(Boolean).join(" ");
            const pill = badge(pos.x, pos.y, text, "msg");
            over.append(pill);
            st.edgeEls.push({ el: pill, a: n.addr, b: k.addr });
          }
          if (row) pairs.delete(`${p.addr}|${q.addr}`);
        }
      }
      // Introductions: who was handed whose address.
      if (showCap) {
        for (const e of st.data.capability) {
          const a = vis(e.from), b = vis(e.to);
          if (!a || !b || a === b || isTree(a, b) || isTree(b, a)) continue;
          const c = arc(a, b, 0.55);
          const el = s("path", { class: "e-cap", d: c.d, "marker-end": "url(#arr-cap)" },
            s("title", { text: `${labelOf(e.from)} was introduced to ${labelOf(e.to)}` }));
          under.append(el);
          st.edgeEls.push({ el, a: a.addr, b: b.addr });
        }
      }
      // Messages across branches.
      if (showMsg) {
        for (const row of pairs.values()) {
          const { p, q } = row;
          const c = arc(p, q);
          const total = row.pq + row.qp;
          const el = s("path", {
            class: "e-msg" + (recent(row) ? " recent" : ""), d: c.d,
            "stroke-width": Math.min(4, 1.2 + Math.log2(total) * 0.7),
            "marker-end": row.pq ? "url(#arr-msg)" : null, "marker-start": row.qp ? "url(#arr-msg)" : null,
          }, s("title", { text: `${labelOf(p.addr)} → ${labelOf(q.addr)}: ${row.pq} · ${labelOf(q.addr)} → ${labelOf(p.addr)}: ${row.qp}, last ${clock(row.last)}` }));
          under.append(el);
          st.edgeEls.push({ el, a: p.addr, b: q.addr });
          if (total > 1) {
            const pill = badge(c.lx, c.ly, String(total), "msg");
            over.append(pill);
            st.edgeEls.push({ el: pill, a: p.addr, b: q.addr });
          }
        }
      }

      const nodesG = s("g", { class: "nodes" });
      st.nodeEls = new Map();
      for (const n of L.nodes.values()) {
        const el = card(n);
        st.nodeEls.set(n.addr, el);
        nodesG.append(el);
      }
      vp.append(under, nodesG, over);
      svg.replaceChildren(defs, vp);
      applyHighlight();
      drawMinimap();
    }
    function marker(id, color) {
      return s("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6.5, markerHeight: 6.5, orient: "auto-start-reverse" },
        s("path", { d: "M0,1 L10,5 L0,9 z", fill: color }));
    }
    /** A count in a rounded pill. */
    function badge(x, y, text, cls) {
      const w = text.length * 6.4 + 12;
      return s("g", { class: `e-badge ${cls}`, transform: `translate(${x - w / 2},${y - 8})` },
        s("rect", { width: w, height: 16, rx: 8 }),
        s("text", { x: w / 2, y: 11.5, "text-anchor": "middle", text }));
    }
    /** A polyline through ``pts`` with its corners rounded. */
    function rounded(pts, radius = 10) {
      const p = pts.filter((q, i) => i === 0 || Math.hypot(q[0] - pts[i - 1][0], q[1] - pts[i - 1][1]) > 0.5);
      let d = `M${p[0][0]},${p[0][1]}`;
      for (let i = 1; i < p.length - 1; i++) {
        const [a, b, c] = [p[i - 1], p[i], p[i + 1]];
        const l1 = Math.hypot(b[0] - a[0], b[1] - a[1]), l2 = Math.hypot(c[0] - b[0], c[1] - b[1]);
        const r = Math.min(radius, l1 / 2, l2 / 2);
        const s1 = [b[0] - ((b[0] - a[0]) / l1) * r, b[1] - ((b[1] - a[1]) / l1) * r];
        const s2 = [b[0] + ((c[0] - b[0]) / l2) * r, b[1] + ((c[1] - b[1]) / l2) * r];
        d += ` L${s1[0]},${s1[1]} Q${b[0]},${b[1]} ${s2[0]},${s2[1]}`;
      }
      const last = p[p.length - 1];
      return `${d} L${last[0]},${last[1]}`;
    }
    /** Parent → child connector: an elbow with rounded corners; into a
     *  stacked worker, down a spine at the column's left and in. */
    function elbow(a, b) {
      const L = st.layout;
      if (L.orient === "down") {
        const x1 = a.x + W / 2, y1 = a.y + H;
        const my = y1 + GAP.down[1] / 2;
        if (b.stacked) {
          const spine = b.x - INDENT / 2, cy = b.y + H / 2;
          return rounded([[x1, y1], [x1, my], [spine, my], [spine, cy], [b.x, cy]], 8);
        }
        const x2 = b.x + W / 2, y2 = b.y;
        return rounded([[x1, y1], [x1, my], [x2, my], [x2, y2]]);
      }
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
      const mx = x1 + (x2 - x1) / 2;
      return rounded([[x1, y1], [mx, y1], [mx, y2], [x2, y2]]);
    }
    /** Where a parent–child count sits: on the child's end of the
     *  connector, or on a stacked worker's corner. */
    function treeLabelAt(a, b) {
      if (b.stacked) return { x: b.x + W - 18, y: b.y };
      if (st.layout.orient === "down") {
        const y1 = a.y + H, y2 = b.y, my = y1 + (y2 - y1) / 2;
        return { x: b.x + W / 2, y: my + (y2 - my) / 2 + 1 };
      }
      const x1 = a.x + W, x2 = b.x, mx = x1 + (x2 - x1) / 2;
      return { x: mx + (x2 - mx) / 2, y: b.y + H / 2 };
    }
    /** An arc between two cards, bowing away from the children (up in
     *  a top-down layout, left in a left-to-right one); ``scale`` < 1
     *  bows less, so an introduction and messages between the same two
     *  agents stay apart. */
    function arc(a, b, scale = 1) {
      const ca = { x: a.x + W / 2, y: a.y + H / 2 }, cb = { x: b.x + W / 2, y: b.y + H / 2 };
      const border = (c, o) => {
        const dx = o.x - c.x, dy = o.y - c.y;
        const k = Math.min(dx ? (W / 2 + 3) / Math.abs(dx) : Infinity, dy ? (H / 2 + 3) / Math.abs(dy) : Infinity);
        return { x: c.x + dx * k, y: c.y + dy * k };
      };
      const p = border(ca, cb), q = border(cb, ca);
      const dx = q.x - p.x, dy = q.y - p.y, len = Math.hypot(dx, dy) || 1;
      const bend = scale * Math.min(90, 18 + len * 0.18);
      const mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
      let cx = mx - (dy / len) * bend, cy = my + (dx / len) * bend;
      const away = st.layout.orient === "down" ? cy > my : cx > mx;
      if (away) { cx = 2 * mx - cx; cy = 2 * my - cy; }
      return { d: `M${p.x},${p.y} Q${cx},${cy} ${q.x},${q.y}`, lx: (mx + cx) / 2, ly: (my + cy) / 2 };
    }

    function card(n) {
      const live = ui().agents.get(n.addr);
      const status = n.sentinel ? "user" : live?.awaiting_permission ? "awaiting" : (live?.status || n.status);
      const selected = n.addr === st.selected;
      const label = n.sentinel ? "you" : n.label;
      const facts = n.sentinel ? ["human"] : [
        live?.model ? shortModel(live.model) : "default model",
        live?.toolkits?.length ? `⚒ ${live.toolkits.length}` : null,
      ].filter(Boolean);
      const STATUS_WORD = { running: "running", error: "error", awaiting: "needs you", terminated: "ended" };
      const kids = n.kids?.length || 0;
      const g = s("g", {
        class: `g-node ${status}${selected ? " selected" : ""}${n.sentinel ? " sentinel" : ""}`,
        transform: `translate(${n.x},${n.y})`,
        "data-addr": n.addr,
        tabindex: n.sentinel ? null : "0",
        role: n.sentinel ? null : "button",
        "aria-label": n.sentinel ? null : `${label}, ${status}`,
      },
        s("rect", { class: "card", width: W, height: H, rx: 10, filter: selected ? "url(#glow)" : null }),
        n.sentinel ? null : s("rect", { class: "accent", x: 0, y: 8, width: 3, height: H - 16, rx: 1.5, fill: dotColor(status) }),
        s("circle", { class: "dot", cx: 16, cy: 19, r: 4, fill: n.sentinel ? "var(--cyan)" : dotColor(status) }),
        s("text", { class: "name", x: 27, y: 23, text: clip(label, live?.cost ? 15 : 21) }),
        live?.cost ? s("text", { class: "cost", x: W - 12, y: 23, "text-anchor": "end", text: money(live.cost) }) : null,
        s("text", { class: "facts", x: 16, y: 41, text: clip(facts.join(" · "), 26) }),
        STATUS_WORD[status] ? s("text", { class: `state ${status}`, x: W - 12, y: 41, "text-anchor": "end", text: STATUS_WORD[status] }) : null,
        s("title", { text: n.sentinel ? "you" : `${n.label} (${n.addr})\nstatus: ${status}${live?.model ? `\nmodel: ${live.model}` : ""}${live?.cost ? `\ncost: ${money(live.cost)}` : ""}${kids ? `\n${kids} child agent(s)` : ""}` }));
      if (kids && !n.sentinel) {
        const shut = n.hidden > 0;
        const [bx, by] = st.layout.orient === "down" ? [W / 2, H] : [W, H / 2];
        const text = shut ? `+${n.hidden}` : "−";
        const w = shut ? text.length * 6.6 + 12 : 16;
        g.append(s("g", { class: "fold" + (shut ? " shut" : ""), transform: `translate(${bx - w / 2},${by - 8})`, "data-fold": n.addr },
          s("rect", { width: w, height: 16, rx: 8 }),
          s("text", { x: w / 2, y: 11.5, "text-anchor": "middle", text }),
          s("title", { text: shut ? `show ${n.hidden} hidden agent(s)` : "collapse this subtree" })));
      }
      return g;
    }

    // ---------- highlight & find ----------
    function applyHighlight() {
      const svg = $("#graph");
      const focus = st.hover;
      const q = st.query.toLowerCase();
      const lit = new Set();
      if (focus) {
        lit.add(focus);
        for (const e of st.edgeEls) if (e.a === focus || e.b === focus) { lit.add(e.a); lit.add(e.b); }
      }
      svg.classList.toggle("focusing", !!focus);
      svg.classList.toggle("finding", !!q);
      for (const [addr, el] of st.nodeEls) {
        el.classList.toggle("lit", lit.has(addr));
        const label = addr === "@user" ? "you" : (st.layout.nodes.get(addr)?.label || "");
        el.classList.toggle("match", !!q && label.toLowerCase().includes(q));
      }
      for (const e of st.edgeEls) e.el.classList.toggle("lit", !!focus && (e.a === focus || e.b === focus));
    }
    function matches() {
      const q = st.query.toLowerCase();
      if (!q || !st.data) return [];
      return st.data.nodes.filter((n) => (ui().showInternal || !n.internal) && n.label.toLowerCase().includes(q));
    }
    /** Expand the folds hiding ``addr`` and bring it into view. */
    function reveal(addr, { onlyIfHidden = false } = {}) {
      if (!st.data) return;
      const byId = new Map(st.data.nodes.map((n) => [n.addr, n]));
      let changed = false;
      for (let p = byId.get(addr)?.parent; p; p = byId.get(p)?.parent) if (st.collapsed.delete(p)) changed = true;
      if (changed) render();
      center(addr, { onlyIfHidden: onlyIfHidden && !changed });
    }

    // ---------- view: pan, zoom, fit ----------
    function applyView() {
      const vp = $("#graph .viewport");
      if (vp) vp.setAttribute("transform", `translate(${st.view.x},${st.view.y}) scale(${st.view.k})`);
      const pct = $("#graph-zoom-pct");
      if (pct) pct.textContent = `${Math.round(st.view.k * 100)}%`;
      drawViewportBox();
    }
    function animateTo(target, ms = 260) {
      const from = { ...st.view };
      const t0 = performance.now();
      const ease = (u) => 1 - Math.pow(1 - u, 3);
      const step = (t) => {
        const u = Math.min(1, (t - t0) / ms), e = ease(u);
        st.view = { k: from.k + (target.k - from.k) * e, x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e };
        applyView();
        if (u < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
    function fit({ animate = true } = {}) {
      if (!st.layout) return;
      const b = st.layout.bounds, { w, h } = size();
      const k = Math.max(K_MIN, Math.min(K_MAX, fitScale(b)));
      const target = { k, x: (w - b.w * k) / 2 - b.x * k, y: (h - b.h * k) / 2 - b.y * k };
      st.moved = false;
      if (animate) animateTo(target); else { st.view = target; applyView(); }
    }
    function zoomAt(factor, cx, cy, animate = false) {
      const k = Math.max(K_MIN, Math.min(K_MAX, st.view.k * factor));
      const f = k / st.view.k;
      const target = { k, x: cx - (cx - st.view.x) * f, y: cy - (cy - st.view.y) * f };
      st.moved = true;
      if (animate) animateTo(target, 160); else { st.view = target; applyView(); }
    }
    function zoomCenter(factor) { const { w, h } = size(); zoomAt(factor, w / 2, h / 2, true); }
    /** Pan (keeping the zoom, at least readable) so ``addr`` is in view. */
    function center(addr, { onlyIfHidden = false } = {}) {
      const n = st.layout?.nodes.get(st.layout.proxy?.get(addr) || addr);
      if (!n) return;
      const { h } = size();
      const w = size().w - drawerWidth();      // the part the drawer leaves uncovered
      const k = Math.max(st.view.k, 0.6);
      const sx = n.x * st.view.k + st.view.x, sy = n.y * st.view.k + st.view.y;
      const inView = sx > 0 && sy > 0 && sx + W * st.view.k < w && sy + H * st.view.k < h;
      if (onlyIfHidden && inView) return;
      st.moved = true;
      animateTo({ k, x: w / 2 - (n.x + W / 2) * k, y: h / 2 - (n.y + H / 2) * k });
    }

    // ---------- minimap ----------
    function drawMinimap() {
      const mm = $("#graph-minimap");
      const L = st.layout;
      if (!mm || !L) return;
      const b = L.bounds;
      const MW = 168, MH = 112;
      const k = Math.min((MW - 12) / b.w, (MH - 12) / b.h);
      st.mini = { k, ox: (MW - b.w * k) / 2 - b.x * k, oy: (MH - b.h * k) / 2 - b.y * k, MW, MH };
      const g = s("g", { transform: `translate(${st.mini.ox},${st.mini.oy}) scale(${k})` });
      for (const n of L.nodes.values()) {
        const live = ui().agents.get(n.addr);
        const status = n.sentinel ? "user" : live?.awaiting_permission ? "awaiting" : (live?.status || n.status);
        g.append(s("rect", { x: n.x, y: n.y, width: W, height: H, rx: 12, class: "mm-node", fill: n.sentinel ? "var(--cyan)" : dotColor(status) }));
      }
      mm.setAttribute("viewBox", `0 0 ${MW} ${MH}`);
      mm.replaceChildren(s("rect", { class: "mm-bg", width: MW, height: MH, rx: 6 }), g, s("rect", { id: "graph-mm-view", class: "mm-view", rx: 2 }));
      drawViewportBox();
    }
    function drawViewportBox() {
      const box = $("#graph-mm-view"), mm = $("#graph-minimap");
      if (!box || !st.mini || !st.layout) return;
      const { w, h } = size(), m = st.mini, v = st.view;
      // The pane's corners in content coordinates, then in the minimap's.
      const x0 = -v.x / v.k, y0 = -v.y / v.k, x1 = (w - v.x) / v.k, y1 = (h - v.y) / v.k;
      const b = st.layout.bounds;
      const whole = x0 <= b.x + 1 && y0 <= b.y + 1 && x1 >= b.x + b.w - 1 && y1 >= b.y + b.h - 1;
      mm.classList.toggle("hidden", whole);
      box.setAttribute("x", m.ox + x0 * m.k);
      box.setAttribute("y", m.oy + y0 * m.k);
      box.setAttribute("width", Math.max(2, (x1 - x0) * m.k));
      box.setAttribute("height", Math.max(2, (y1 - y0) * m.k));
    }
    function minimapTo(e) {
      const mm = $("#graph-minimap"), r = mm.getBoundingClientRect(), m = st.mini;
      const px = ((e.clientX - r.left) / r.width) * m.MW, py = ((e.clientY - r.top) / r.height) * m.MH;
      const cx = (px - m.ox) / m.k, cy = (py - m.oy) / m.k;
      const { w, h } = size();
      st.moved = true;
      st.view = { ...st.view, x: w / 2 - cx * st.view.k, y: h / 2 - cy * st.view.k };
      applyView();
    }

    function renderControls() {
      for (const b of document.querySelectorAll("#graph-orient button")) b.classList.toggle("active", b.dataset.o === st.orient);
      const used = $("#graph-orient-used");
      if (used) used.textContent = st.orient === "auto" ? (st.layout?.orient === "down" ? "↓" : "→") : "";
      const n = matches().length;
      const count = $("#graph-find-count");
      if (count) count.textContent = st.query ? `${n} match${n === 1 ? "" : "es"}` : "";
    }

    // ---------- events ----------
    function wire() {
      if (st.wired) return;
      st.wired = true;
      const wrap = $("#graph-wrap"), svg = $("#graph");
      for (const id of ["#edge-spawn", "#edge-message", "#edge-capability"]) $(id).addEventListener("change", () => render());
      for (const b of document.querySelectorAll("#graph-orient button")) {
        b.addEventListener("click", () => { st.orient = b.dataset.o; savePref("conjure-graph-orient", st.orient); st.moved = false; st.layout = null; render(); });
      }
      $("#graph-fit").addEventListener("click", () => fit());
      $("#graph-zoom-in").addEventListener("click", () => zoomCenter(1.25));
      $("#graph-zoom-out").addEventListener("click", () => zoomCenter(0.8));
      $("#graph-zoom-pct").addEventListener("click", () => { const { w, h } = size(); zoomAt(1 / st.view.k, w / 2, h / 2, true); });
      $("#graph-expand").addEventListener("click", () => { st.collapsed.clear(); st.moved = false; render(); });

      // Wheel: pinch or a mouse wheel zooms at the pointer; a trackpad's
      // two-finger scroll pans.
      wrap.addEventListener("wheel", (e) => {
        e.preventDefault();
        const r = wrap.getBoundingClientRect();
        const mouseWheel = e.deltaMode === 1 || (e.deltaX === 0 && Math.abs(e.deltaY) >= 40 && Number.isInteger(e.deltaY));
        if (e.ctrlKey || mouseWheel) {
          const step = e.ctrlKey ? 0.012 : 0.0018;
          zoomAt(Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : step)), e.clientX - r.left, e.clientY - r.top);
        } else {
          st.moved = true;
          st.view = { ...st.view, x: st.view.x - e.deltaX, y: st.view.y - e.deltaY };
          applyView();
        }
      }, { passive: false });

      // Drag the background to pan; click a card to select it.
      svg.addEventListener("pointerdown", (e) => {
        if (e.button !== 0 || e.target.closest(".g-node")) return;
        st.dragging = { x: e.clientX, y: e.clientY, vx: st.view.x, vy: st.view.y, moved: false };
        svg.setPointerCapture(e.pointerId);
        wrap.classList.add("dragging");
      });
      svg.addEventListener("pointermove", (e) => {
        const d = st.dragging;
        if (!d) return;
        const dx = e.clientX - d.x, dy = e.clientY - d.y;
        if (Math.abs(dx) + Math.abs(dy) > 2) d.moved = true;
        st.moved = true;
        st.view = { ...st.view, x: d.vx + dx, y: d.vy + dy };
        applyView();
      });
      const endDrag = () => {
        if (!st.dragging) return;
        st.dragging = null;
        wrap.classList.remove("dragging");
        if (st.pending) { st.pending = false; render(); }
      };
      svg.addEventListener("pointerup", endDrag);
      svg.addEventListener("pointercancel", endDrag);
      svg.addEventListener("click", (e) => {
        const fold = e.target.closest(".fold");
        if (fold) {
          const addr = fold.getAttribute("data-fold");
          if (!st.collapsed.delete(addr)) st.collapsed.add(addr);
          render();
          return;
        }
        const node = e.target.closest(".g-node");
        if (!node || node.classList.contains("sentinel")) return;
        st.selected = node.getAttribute("data-addr");
        onSelect(st.selected);
        panel(true);
      });
      svg.addEventListener("dblclick", (e) => {
        const node = e.target.closest(".g-node");
        if (node && !node.classList.contains("sentinel") && !e.target.closest(".fold")) onOpen(node.getAttribute("data-addr"));
        else if (!node) fit();
      });
      svg.addEventListener("keydown", (e) => {
        const node = e.target.closest?.(".g-node");
        if (node && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          e.stopPropagation();
          st.selected = node.getAttribute("data-addr");
          onSelect(st.selected);
          panel(true);
        }
      });
      svg.addEventListener("pointerover", (e) => {
        const node = e.target.closest(".g-node");
        const addr = node?.getAttribute("data-addr") || null;
        if (addr !== st.hover) { st.hover = addr; applyHighlight(); }
      });
      svg.addEventListener("pointerleave", () => { if (st.hover) { st.hover = null; applyHighlight(); } });

      const mm = $("#graph-minimap");
      let mmDrag = false;
      mm.addEventListener("pointerdown", (e) => { mmDrag = true; mm.setPointerCapture(e.pointerId); minimapTo(e); });
      mm.addEventListener("pointermove", (e) => { if (mmDrag) minimapTo(e); });
      mm.addEventListener("pointerup", () => { mmDrag = false; });

      const find = $("#graph-find");
      find.addEventListener("input", () => { st.query = find.value.trim(); applyHighlight(); renderControls(); });
      find.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Escape") { find.value = ""; st.query = ""; applyHighlight(); renderControls(); find.blur(); }
        if (e.key === "Enter") {
          const all = matches();
          if (!all.length) return;
          st.findIndex = ((st.findIndex ?? -1) + (e.shiftKey ? -1 : 1) + all.length) % all.length;
          const hit = all[st.findIndex];
          reveal(hit.addr);
          st.selected = hit.addr;
          onSelect(hit.addr);
        }
      });
      $("#graph-inspector-toggle").addEventListener("click", () => panel(!panelOpen()));
      $("#graph-inspector-close").addEventListener("click", () => panel(false));
      new ResizeObserver(() => { if (!st.layout) return; if (!st.moved) fit({ animate: false }); else drawViewportBox(); }).observe(wrap);
    }

    // ---------- the details drawer ----------
    const panelOpen = () => document.getElementById("view-graph").classList.contains("panel-open");
    const drawerWidth = () => (panelOpen() ? $("#graph-inspector").getBoundingClientRect().width : 0);
    /** Slide the agent's details over the graph's right edge (the graph
     *  keeps its full width), keeping the selected agent uncovered. */
    function panel(open) {
      document.getElementById("view-graph").classList.toggle("panel-open", open);
      $("#graph-inspector-toggle").classList.toggle("active", open);
      if (open && st.selected) setTimeout(() => center(st.selected, { onlyIfHidden: true }), 0);
    }

    /** Keys while the graph is showing (app.js routes them here). */
    function key(e) {
      switch (e.key) {
        case "Escape":
          if (!panelOpen()) return false;
          panel(false);
          return true;
        case "f": fit(); return true;
        case "+": case "=": zoomCenter(1.25); return true;
        case "-": case "_": zoomCenter(0.8); return true;
        case "0": { const { w, h } = size(); zoomAt(1 / st.view.k, w / 2, h / 2, true); return true; }
        case "/": $("#graph-find").focus(); return true;
        case "c":
          if (st.selected) { if (!st.collapsed.delete(st.selected)) st.collapsed.add(st.selected); render(); }
          return true;
        default: return false;
      }
    }

    return {
      refresh, render, fit, key,
      select(addr) {
        st.selected = addr;
        if (!st.layout) return;
        for (const [a, el] of st.nodeEls) {
          el.classList.toggle("selected", a === addr);
          el.querySelector(".card")?.setAttribute("filter", a === addr ? "url(#glow)" : "");
        }
        reveal(addr, { onlyIfHidden: true });
      },
      set selected(addr) { st.selected = addr; },
      get selected() { return st.selected; },
    };
  };
})();
