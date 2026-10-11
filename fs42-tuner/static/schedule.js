(function () {
  "use strict";
  const { el, call, fmtWhen, fmtTime } = W;
  W.nav("schedule");

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const state = { stations: [], edit: true, busy: false };

  function say(text, kind) {
    $("msg").textContent = text || "";
    $("msg").className = "msg" + (kind ? " " + kind : "");
  }
  const run = (fn) => fn().catch((e) => say(e.message, "err"));

  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  function shift(days) {
    const d = new Date($("date").value + "T00:00:00");
    d.setDate(d.getDate() + days);
    $("date").value = iso(d);
    run(load);
  }

  async function load() {
    const name = $("station").value;
    if (!name) return;
    const s = await call(`/api/schedule?name=${encodeURIComponent(name)}&date=${encodeURIComponent($("date").value)}`);
    const note = $("note");
    if (!s.scheduled_until) {
      note.textContent = "Nothing is scheduled for this station yet. Build it from the Stations page.";
      note.className = "sched-note bad";
    } else if (!s.on_air) {
      note.textContent = `Off air: the schedule ended ${fmtWhen(s.scheduled_until)}.`;
      note.className = "sched-note bad";
    } else if (s.low) {
      note.textContent = `Scheduled until ${fmtWhen(s.scheduled_until)}: that is less than a day away, so add days soon.`;
      note.className = "sched-note warn";
    } else {
      note.textContent = `Scheduled until ${fmtWhen(s.scheduled_until)}.`;
      note.className = "sched-note";
    }
    if (s.notes && s.notes.length) {                    // hours that play the test pattern because their lists are empty
      note.textContent += " " + s.notes.join(" ");
      if (note.className === "sched-note") note.className = "sched-note warn";
    }
    const rows = $("rows");
    rows.replaceChildren();
    $("table").hidden = !s.blocks.length;
    $("empty").hidden = !!s.blocks.length;
    $("empty").textContent = s.beyond ? "That is further ahead than the schedule is shown." : "Nothing is scheduled on this day.";
    for (const b of s.blocks) {
      const tr = el("tr", { className: "block-row" + (b.now ? " now" : "") });
      const hours = (new Date(b.end) - new Date(b.start)) / 3600000;
      const toggle = el("button", { type: "button", className: "twisty", textContent: "▸", title: "Show the files", ariaExpanded: "false" });
      const title = el("td", {}, toggle, el("span", { textContent: " " + b.title + (b.now ? "  (on now)" : "") }));
      tr.append(el("td", { textContent: hours >= 23 ? "All day (repeats)" : `${fmtTime(b.start)} – ${fmtTime(b.end)}` }), title);
      rows.append(tr);
      let open = null;                                    // the detail row, once it has been opened
      const flip = () => run(async () => {
        if (open) {
          open.hidden = !open.hidden;
          toggle.textContent = open.hidden ? "▸" : "▾";
          toggle.setAttribute("aria-expanded", String(!open.hidden));
          return;
        }
        open = el("tr", { className: "detail" }, el("td", { colSpan: 2 }));
        tr.after(open);
        toggle.textContent = "▾";
        toggle.setAttribute("aria-expanded", "true");
        await showFiles(s.name, b, open.firstChild, 0);
      });
      toggle.addEventListener("click", flip);
    }
    if (s.truncated) {
      rows.append(el("tr", {}, el("td", { colSpan: 2, className: "dim", textContent: "Only the first part of this day is shown." })));
    }
    $("extend").disabled = !state.edit || state.busy;
  }

  const TYPE = { feature: "program", commercial: "commercial", bump: "mid card", stream: "stream" };

  // the files inside one block, a page at a time: a looping station's day holds thousands of repeats
  async function showFiles(name, block, cell, offset) {
    const url = `/api/schedule/block?name=${encodeURIComponent(name)}&start=${encodeURIComponent(block.start)}&offset=${offset}&limit=200`;
    const data = await call(url);
    let list = cell.querySelector("ul.files");
    if (!list) {
      cell.replaceChildren();
      list = el("ul", { className: "files" });
      cell.append(list);
    }
    cell.querySelector(".more")?.remove();
    for (const e of data.entries) {
      list.append(el("li", { className: "kind-" + e.type }, el("span", { className: "when", textContent: fmtTime(e.at) }),
        el("span", { className: "kind", textContent: TYPE[e.type] || e.type }),
        titleOf(e),
        el("span", { className: "len", textContent: fmtLen(e.duration) })));
    }
    const shown = offset + data.entries.length;
    if (!data.total) cell.append(el("p", { className: "dim", textContent: "This block lists no files." }));
    if (shown < data.total) {
      const more = el("button", { type: "button", className: "btn more", textContent: `Show more (${shown} of ${data.total})` });
      more.addEventListener("click", () => run(() => showFiles(name, block, cell, shown)));
      cell.append(more);
    }
  }

  // FieldStation42's own title for the file when its catalog has one; the file name goes beside it, dimmer
  function titleOf(e) {
    const stem = e.file.replace(/\.[^.]+$/, "");
    const span = el("span", { className: "file", title: e.file });
    if (e.title && e.title !== stem) {
      span.append(e.title, el("span", { className: "fname", textContent: "  " + e.file }));
    } else {
      span.textContent = e.file;
    }
    return span;
  }

  function fmtLen(sec) {
    sec = Math.round(sec);
    const m = Math.floor(sec / 60), s = sec % 60;
    return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
  }

  async function extend() {
    const name = $("station").value;
    state.busy = true;
    $("extend").disabled = true;
    try {
      const job = await call("/api/stations/build", { name, days: 7, rebuild: false });
      for (;;) {
        say(job.message, "ok");
        await new Promise((r) => setTimeout(r, 1500));
        const j = await call(`/api/stations/job?id=${encodeURIComponent(job.id)}`);
        say(j.message, j.state === "error" ? "err" : "ok");
        if (j.state !== "running") break;
      }
    } finally {
      state.busy = false;
    }
    await load();
  }

  $("station").addEventListener("change", () => {
    history.replaceState(null, "", `?name=${encodeURIComponent($("station").value)}`);
    run(load);
  });
  $("date").addEventListener("change", () => run(load));
  $("prev").addEventListener("click", () => shift(-1));
  $("next").addEventListener("click", () => shift(1));
  $("today").addEventListener("click", () => { $("date").value = iso(new Date()); run(load); });
  $("extend").addEventListener("click", () => run(extend));

  run(async () => {
    const data = await call("/api/stations");
    state.stations = data.stations;
    state.edit = data.edit;
    $("date").value = iso(new Date());
    const sel = $("station");
    for (const s of data.stations) sel.append(el("option", { value: s.name, textContent: `#${s.number}  ${s.name}` }));
    if (!data.stations.length) {
      $("empty").hidden = false;
      $("empty").textContent = "There are no stations yet. Make one on the Stations page.";
      $("extend").disabled = true;
      return;
    }
    const wanted = params.get("name");
    if (wanted && data.stations.some((s) => s.name === wanted)) sel.value = wanted;
    await load();
  });
})();
