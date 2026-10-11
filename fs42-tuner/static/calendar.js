// A week as a calendar: columns are days, rows are half hours. Drag on a column to draw a slot, drag a slot's top or
// bottom edge to resize it, drag a slot itself to move it (to another time, or another day), click it to select it,
// press Delete to remove the selected slot, and Ctrl+C / Ctrl+X / Ctrl+V copy, cut and paste it (the paste goes where
// the pointer is, or at the same time on the selected day). The editing rules (paint / remove / resize) are plain functions
// in `CalendarOps`, so they can be tested without a browser.
(function (root) {
  "use strict";

  // ---- the rules. A day is a list of blocks {from, to, ...payload}, in hours (multiples of 0.5), kept sorted.
  // `full` days must stay covered from 0 to 24 with no gaps (the default and the seasons); the others may have gaps.
  const same = (a, b) => JSON.stringify(rest(a)) === JSON.stringify(rest(b));
  const rest = (b) => { const { from, to, ...r } = b; return r; };
  const clone = (x) => JSON.parse(JSON.stringify(x));

  function tidy(blocks) {                      // sorted, and neighbours that say the same thing become one
    const out = [];
    for (const b of clone(blocks).sort((x, y) => x.from - y.from)) {
      const last = out[out.length - 1];
      if (last && last.to === b.from && same(last, b)) last.to = b.to;
      else out.push(b);
    }
    return out;
  }

  // Put `payload` on from..to, writing over whatever was there.
  function paint(blocks, from, to, payload) {
    if (!(from < to)) return clone(blocks);
    const out = [];
    for (const b of blocks) {
      if (b.to <= from || b.from >= to) { out.push(clone(b)); continue; }
      if (b.from < from) out.push({ ...clone(b), to: from });          // the part before
      if (b.to > to) out.push({ ...clone(b), from: to });              // the part after
    }
    out.push({ from, to, ...clone(payload) });
    return tidy(out);
  }

  // Take block i out. In a full day the hole is closed by the block before it (or the one after, for the first).
  function remove(blocks, i, full) {
    const out = clone(blocks).sort((x, y) => x.from - y.from);
    const [gone] = out.splice(i, 1);
    if (full && out.length) {
      if (i > 0) out[i - 1].to = gone.to;
      else out[0].from = gone.from;
    }
    return tidy(out);
  }

  // Move one edge of block i to `value`. Making a block smaller only shortens it: the neighbour does not grow into the
  // space (a full day then shows "nothing is scheduled" there until it is filled). Making it larger takes the time from
  // the neighbour on a full day, and stops at the neighbour on the others. Never smaller than `unit`.
  function resize(blocks, i, edge, value, full, unit) {
    const out = clone(blocks).sort((x, y) => x.from - y.from);
    const b = out[i], prev = out[i - 1], next = out[i + 1];
    if (edge === "to") {
      const max = next ? (full ? next.to - unit : next.from) : 24;
      value = Math.min(Math.max(value, b.from + unit), max);
      b.to = value;
      if (full && next && value > next.from) next.from = value;       // growing into the neighbour: it gives the time
    } else {
      const min = prev ? (full ? prev.from + unit : prev.to) : 0;
      value = Math.max(Math.min(value, b.to - unit), min);
      b.from = value;
      if (full && prev && value < prev.to) prev.to = value;
    }
    return out;
  }

  // Put a block (its payload, and how long it is) down starting at `from`, on this or another day: snapped to `unit`,
  // kept inside the day, writing over what is there. Take it out of where it was first (remove) when moving.
  function place(blocks, block, from, unit) {
    const dur = block.to - block.from;
    from = Math.min(Math.max(Math.round(from / unit) * unit, 0), 24 - dur);
    return paint(blocks, from, from + dur, rest(block));
  }

  // What is wrong with a day's blocks, as text for the person editing (or null).
  function problem(blocks, full, videos) {
    const out = clone(blocks).sort((x, y) => x.from - y.from);
    if (full) {
      let mark = 0;
      for (const b of out) { if (b.from !== mark) return `Nothing is scheduled from ${clock(mark)}`; mark = b.to; }
      if (mark !== 24) return `Nothing is scheduled from ${clock(mark)}`;
    }
    if (videos) {
      for (const b of out) if (!b.lists || !b.lists.length) return `The slot at ${clock(b.from)} has no list yet`;
      for (let h = 0; h < 24; h++) {
        const a = out.find((b) => b.from <= h && h < b.to), c = out.find((b) => b.from <= h + 0.5 && h + 0.5 < b.to);
        if (a !== c) {
          if (!a || !c) return `The hour at ${clock(h)} is only half filled`;
          if (a.lists.length !== 1 || c.lists.length !== 1) return `A slot that starts or ends at half past (${clock(h + 0.5)}) can hold only one list`;
        }
      }
    }
    return null;
  }

  function clock(h) { return `${String(Math.floor(h)).padStart(2, "0")}:${h % 1 ? "30" : "00"}`; }

  root.CalendarOps = { paint, remove, resize, place, tidy, problem, clock };

  // ---- the grid (browser only)
  if (typeof document === "undefined") return;

  const ROW = 18;                              // pixels per half hour
  const clip = { value: null };                // what Copy last took: {kind: "block", layer, block} or {kind: "day", day}
  const hue = (name) => { let h = 0; for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
  const colour = (b) => {
    const name = b.lists ? b.lists[0] : b.start || b.list;
    return name ? `hsl(${hue(name)} 45% 34%)` : "#7a4a2b";
  };
  const text = (b) => (b.lists ? (b.lists.length ? b.lists.join(", ") : "(choose a list)")
    : b.start ? (b.end && b.end !== b.start ? `${b.start} / ${b.end}` : b.start) : b.list || "(choose a list)");

  function el(tag, props, ...kids) {
    const e = Object.assign(document.createElement(tag), props || {});
    for (const k of kids) e.append(k);
    return e;
  }

  // opts: {host, columns:[{key,label}], getDay(key)->{videos,commercials}, layer()->"videos"|"commercials",
  //        full()->bool, defaults()->payload, onChange(), onSelect(sel)}
  function create(opts) {
    const cal = { sel: null };
    const unit = () => (opts.layer() === "videos" ? 0.5 : 1);
    const blocksOf = (key) => { const d = opts.getDay(key); return d[opts.layer()] || (d[opts.layer()] = []); };   // a saved schedule from before mid cards has no "cards"
    const setBlocks = (key, blocks) => { opts.getDay(key)[opts.layer()] = blocks; };
    const isFull = () => opts.layer() === "videos" && opts.full();
    let drag = null;

    function render() {
      const host = opts.host;
      host.replaceChildren();
      host.classList.add("cal");
      const head = el("div", { className: "cal-head" }, el("div", { className: "cal-corner" }));
      const body = el("div", { className: "cal-body" });
      const hours = el("div", { className: "cal-hours" });
      for (let h = 0; h < 24; h++) hours.append(el("div", { className: "cal-hour", textContent: clock(h), style: `height:${ROW * 2}px` }));
      body.append(hours);
      for (const col of opts.columns) {
        const bar = el("div", { className: "cal-colhead" }, el("span", { textContent: col.label }));
        const copy = el("button", { type: "button", className: "cal-mini", textContent: "⧉", title: `Copy ${col.label}'s whole day` });
        copy.addEventListener("click", () => { clip.value = { kind: "day", day: clone(opts.getDay(col.key)) }; render(); opts.onSelect && opts.onSelect(cal.sel, true); });
        bar.append(copy);
        if (clip.value) {
          const paste = el("button", { type: "button", className: "cal-mini", textContent: "⎘",
            title: clip.value.kind === "day" ? `Paste the copied day over ${col.label}` : `Paste the copied slot onto ${col.label}` });
          paste.addEventListener("click", () => pasteInto(col.key));
          bar.append(paste);
        }
        head.append(bar);
        const c = el("div", { className: "cal-col", style: `height:${ROW * 48}px` });
        c.dataset.key = col.key;
        c.addEventListener("pointerdown", (ev) => down(ev, col.key, c));
        // the other layer, faint, for context
        const other = opts.layer() === "videos" ? "commercials" : "videos";
        for (const b of opts.getDay(col.key)[other] || []) c.append(blockEl(b, true));
        blocksOf(col.key).forEach((b, i) => c.append(blockEl(b, false, col.key, i)));
        body.append(c);
      }
      host.append(head, body);
      head.style.gridTemplateColumns = body.style.gridTemplateColumns = `3.2em repeat(${opts.columns.length}, minmax(0, 1fr))`;
    }

    function blockEl(b, ghost, key, i) {
      const top = b.from * 2 * ROW, height = (b.to - b.from) * 2 * ROW;
      const e = el("div", { className: "cal-block" + (ghost ? " ghost" : "") + (!ghost && cal.sel && cal.sel.key === key && cal.sel.i === i ? " selected" : ""),
        style: `top:${top}px;height:${height}px;background:${colour(b)}` });
      if (ghost) return e;
      e.append(el("span", { className: "cal-text", textContent: text(b) }), el("span", { className: "cal-time", textContent: `${clock(b.from)}–${clock(b.to)}` }));
      e.title = `${text(b)}  ${clock(b.from)}–${clock(b.to)}`;
      if (b.to - b.from <= 0.5) e.classList.add("short");          // a half hour is one line: the name, then the time
      for (const edge of ["from", "to"]) {
        const h = el("div", { className: `cal-handle ${edge}` });
        h.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); startResize(ev, key, i, edge); });
        e.append(h);
      }
      e.addEventListener("pointerdown", (ev) => {              // the body of a slot: a click selects it, a drag moves it
        if (ev.button !== 0 || ev.target.classList.contains("cal-handle")) return;
        ev.stopPropagation();
        startMove(ev, key, i, e);
      });
      e.dataset.i = i;
      return e;
    }

    function cellAt(colEl, ev) {
      const y = ev.clientY - colEl.getBoundingClientRect().top;
      return Math.min(47, Math.max(0, Math.floor(y / ROW)));
    }
    const hoursOf = (c0, c1) => {                // two cells (either order) -> from, to in hours on this layer's unit
      const u = unit();
      let a = Math.min(c0, c1) / 2, b = (Math.max(c0, c1) + 1) / 2;
      a = Math.floor(a / u) * u; b = Math.max(a + u, Math.ceil(b / u) * u);
      return [a, Math.min(24, b)];
    };

    function down(ev, key, colEl) {
      if (ev.button !== 0 || ev.target.classList.contains("cal-handle")) return;
      drag = { type: "create", key, colEl, c0: cellAt(colEl, ev), c1: cellAt(colEl, ev), moved: false };
      const ghost = el("div", { className: "cal-block drawing" });
      colEl.append(ghost);
      drag.ghost = ghost;
      showGhost();
      listen();
    }

    // the drag follows the pointer anywhere on the page (no pointer capture: it works the same with a mouse, a finger or a test)
    function listen() {
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    }
    function unlisten() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    }

    function startResize(ev, key, i, edge) {
      const colEl = opts.host.querySelector(`.cal-col[data-key="${key}"]`);
      cal.sel = { key, i };
      drag = { type: "resize", key, i, edge, colEl, blocks: clone(blocksOf(key)) };
      listen();
    }

    function startMove(ev, key, i, blockEl_) {
      const colEl = opts.host.querySelector(`.cal-col[data-key="${key}"]`);
      const b = blocksOf(key)[i];
      const top = colEl.getBoundingClientRect().top;
      drag = { type: "move", key, i, block: clone(b), colEl, targetKey: key, from: b.from, moved: false,
               grab: (ev.clientY - top) / ROW / 2 - b.from, x: ev.clientX, y: ev.clientY };
      cal.sel = { key, i };
      blockEl_.classList.add("selected");
      listen();
    }

    function columnAt(ev) {                    // the day column under the pointer, if any
      for (const c of opts.host.querySelectorAll(".cal-col")) {
        const r = c.getBoundingClientRect();
        if (ev.clientX >= r.left && ev.clientX < r.right) return c;
      }
      return null;
    }

    function showMove() {
      for (const g of opts.host.querySelectorAll(".cal-block.drawing")) g.remove();
      const col = opts.host.querySelector(`.cal-col[data-key="${drag.targetKey}"]`);
      if (!col) return;
      const dur = drag.block.to - drag.block.from;
      const ghost = el("div", { className: "cal-block drawing", textContent: `${clock(drag.from)}–${clock(drag.from + dur)}` });
      ghost.style.top = `${drag.from * 2 * ROW}px`;
      ghost.style.height = `${dur * 2 * ROW}px`;
      col.append(ghost);
    }

    function showGhost() {
      const [a, b] = hoursOf(drag.c0, drag.c1);
      drag.ghost.style.top = `${a * 2 * ROW}px`;
      drag.ghost.style.height = `${(b - a) * 2 * ROW}px`;
      drag.ghost.textContent = `${clock(a)}–${clock(b)}`;
    }

    function move(ev) {
      if (!drag) return;
      if (drag.type === "move") {
        if (!drag.moved && Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) < 4) return;       // still a click
        drag.moved = true;
        const col = columnAt(ev) || opts.host.querySelector(`.cal-col[data-key="${drag.targetKey}"]`);
        drag.targetKey = col.dataset.key;
        const dur = drag.block.to - drag.block.from;
        const raw = (ev.clientY - col.getBoundingClientRect().top) / ROW / 2 - drag.grab;
        drag.from = Math.min(Math.max(Math.round(raw / unit()) * unit(), 0), 24 - dur);
        showMove();
        return;
      }
      const c = cellAt(drag.colEl, ev);
      if (drag.type === "create") {
        if (c !== drag.c1) { drag.c1 = c; drag.moved = drag.moved || c !== drag.c0; showGhost(); }
      } else {
        const t = Math.round(((ev.clientY - drag.colEl.getBoundingClientRect().top) / ROW / 2) / unit()) * unit();
        const out = CalendarOps.resize(drag.blocks, drag.i, drag.edge, Math.min(24, Math.max(0, t)), isFull(), unit());
        setBlocks(drag.key, out);
        render();
        drag.colEl = opts.host.querySelector(`.cal-col[data-key="${drag.key}"]`);
      }
    }

    function up() {
      if (!drag) return;
      const d = drag;
      drag = null;
      unlisten();
      if (d.type === "resize") {
        select(d.key, d.i);
        opts.onChange();
        return;
      }
      if (d.type === "move") {
        for (const g of opts.host.querySelectorAll(".cal-block.drawing")) g.remove();
        if (!d.moved || (d.targetKey === d.key && d.from === d.block.from)) { select(d.key, d.i); return; }        // a click, or put back
        const without = CalendarOps.remove(blocksOf(d.key), d.i, isFull());
        setBlocks(d.key, without);
        const out = CalendarOps.place(blocksOf(d.targetKey), d.block, d.from, unit());
        setBlocks(d.targetKey, out);
        const i = out.findIndex((x) => x.from <= d.from && d.from < x.to);
        cal.sel = { key: d.targetKey, i };
        render();
        opts.onChange();
        opts.onSelect && opts.onSelect(cal.sel);
        return;
      }
      d.ghost.remove();
      const blocks = blocksOf(d.key);
      if (!d.moved) {                            // a click: select the slot under it, or make one on empty space
        const h = d.c0 / 2;
        const hit = blocks.findIndex((b) => b.from <= h && h < b.to);
        if (hit >= 0) { select(d.key, hit); return; }
      }
      const start = Math.floor(d.c0 / 2 / unit()) * unit();
      const [a, b] = d.moved ? hoursOf(d.c0, d.c1) : [start, start + 1];       // a click makes a one hour slot
      const out = CalendarOps.paint(blocks, a, Math.min(24, b), opts.defaults());
      setBlocks(d.key, out);
      const i = out.findIndex((x) => x.from <= a && a < x.to);
      cal.sel = { key: d.key, i };
      render();
      opts.onChange();
      opts.onSelect && opts.onSelect(cal.sel);
    }

    function select(key, i) {
      cal.sel = key == null ? null : { key, i };
      render();
      opts.onSelect && opts.onSelect(cal.sel);
    }

    function selected() {
      if (!cal.sel) return null;
      const b = blocksOf(cal.sel.key)[cal.sel.i];
      return b ? { key: cal.sel.key, i: cal.sel.i, block: b } : null;
    }

    function removeSelected() {
      const s = selected();
      if (!s) return;
      setBlocks(s.key, CalendarOps.remove(blocksOf(s.key), s.i, isFull()));
      cal.sel = null;
      render();
      opts.onChange();
      opts.onSelect && opts.onSelect(null);
    }

    function copySelected() {
      const s = selected();
      if (!s) return;
      clip.value = { kind: "block", layer: opts.layer(), block: clone(s.block) };
      render();
      opts.onSelect && opts.onSelect(cal.sel, true);
    }

    // put the copied block at the same time on these days (key list), writing over what is there
    function pasteBlockOn(keys, block) {
      for (const k of keys) setBlocks(k, CalendarOps.paint(blocksOf(k), block.from, block.to, rest(block)));
      render();
      opts.onChange();
    }

    function pasteInto(key) {
      const v = clip.value;
      if (!v) return;
      if (v.kind === "day") {
        const d = opts.getDay(key);
        d.videos = clone(v.day.videos);
        d.commercials = clone(v.day.commercials);
        d.cards = clone(v.day.cards || []);
        cal.sel = null;
        render();
        opts.onChange();
        opts.onSelect && opts.onSelect(null);
      } else if (v.layer === opts.layer()) {
        pasteBlockOn([key], v.block);
      }
    }

    // the selected block's settings changed (its lists, say)
    function update(fn) {
      const s = selected();
      if (!s) return;
      fn(s.block);
      setBlocks(s.key, blocksOf(s.key));
      render();
      opts.onChange();
    }

    // where the pointer is over the calendar: a paste with the keyboard goes there
    const hover = { key: null, h: 0 };
    opts.host.addEventListener("pointermove", (ev) => {
      const col = [...opts.host.querySelectorAll(".cal-col")].find((c) => { const r = c.getBoundingClientRect(); return ev.clientX >= r.left && ev.clientX < r.right; });
      if (!col) { hover.key = null; return; }
      hover.key = col.dataset.key;
      hover.h = (ev.clientY - col.getBoundingClientRect().top) / ROW / 2;
    });
    opts.host.addEventListener("pointerleave", () => { hover.key = null; });

    // Ctrl+V: the copied slot goes down with its top at the pointer (or, with the pointer elsewhere, at the same time on
    // the selected day); a copied day goes onto the day under the pointer or the selected one
    function pasteWithKeyboard() {
      const v = clip.value;
      if (!v) return;
      const key = hover.key || (cal.sel && cal.sel.key);
      if (!key) return;
      if (v.kind === "day") { pasteInto(key); return; }
      if (v.layer !== opts.layer()) return;
      const at = hover.key ? hover.h : v.block.from;
      const out = CalendarOps.place(blocksOf(key), v.block, at, unit());
      setBlocks(key, out);
      const start = Math.min(Math.max(Math.round(at / unit()) * unit(), 0), 24 - (v.block.to - v.block.from));
      cal.sel = { key, i: out.findIndex((x) => x.from <= start && start < x.to) };
      render();
      opts.onChange();
      opts.onSelect && opts.onSelect(cal.sel);
    }

    // Delete (or Backspace) removes the selected slot, Escape lets go of it, Ctrl+C / X / V copy, cut and paste;
    // not while typing in a field, and Ctrl+C leaves alone a piece of text the person has selected
    const onKey = (ev) => {
      if (!opts.host.isConnected) { document.removeEventListener("keydown", onKey); return; }
      const t = ev.target, typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
      if (typing || ev.altKey) return;
      if (ev.ctrlKey || ev.metaKey) {
        const k = ev.key.toLowerCase();
        if (k === "v") { ev.preventDefault(); pasteWithKeyboard(); }
        else if ((k === "c" || k === "x") && cal.sel && !(window.getSelection && String(window.getSelection()))) {
          ev.preventDefault();
          copySelected();
          if (k === "x") removeSelected();
        }
        return;
      }
      if (!cal.sel) return;
      if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); removeSelected(); }
      else if (ev.key === "Escape") select(null);
    };
    document.addEventListener("keydown", onKey);

    Object.assign(cal, { render, selected, removeSelected, copySelected, pasteBlockOn, update, clearSelection: () => select(null),
      hasClip: () => !!clip.value, clipKind: () => (clip.value ? clip.value.kind : null) });
    return cal;
  }

  root.Calendar = { create, ROW };
})(typeof window !== "undefined" ? window : globalThis);
