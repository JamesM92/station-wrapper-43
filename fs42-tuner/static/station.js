(function () {
  "use strict";
  const { el, call, hourLabel } = W;
  W.nav("stations");

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const original = params.get("name");          // null: a new station
  const copyOf = params.get("copy");            // a new station that starts as a copy of this one

  // "bump" holds the interstitial clips and "commercials" the ad breaks: neither is something to schedule as a program
  const NOT_PROGRAMS = new Set(["bump", "commercials"]);
  const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  const SEASONS = ["spring", "summer", "fall", "winter"];
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const cap = (d) => d[0].toUpperCase() + d.slice(1);
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const emptyDay = () => ({ videos: [], commercials: [], cards: [] });
  const state = {
    schedule: null,            // the definition being edited (see schedules.py): default, seasons, events
    tab: "default",            // "default", a season, or "special"
    members: {},               // what each source list is made of (from /api/files/shows), for the choosers
    pendingLists: new Set(),   // lists a template named, made when the station is saved (they may stay empty for now)
    layer: "videos",           // "videos", "commercials" or "cards"
    eventId: null,             // the special day being edited
    lastLists: [],             // what the last slot drawn played, to start the next one the same
    lastAds: "",
    lists: [],                 // names of the lists (tag folders)
    playable: [],              // the ones a station can play as programs
    shows: [],                 // shows already used as sources (made into hidden lists)
    showKinds: {},             // list name -> series / season / episode / folder
    linking: new Set(),        // sources picked a moment ago whose videos are still being linked in
    edit: true,
  };
  let cal = null;

  function say(text, kind) {
    $("msg").textContent = text || "";
    $("msg").className = "msg" + (kind ? " " + kind : "");
  }
  const run = (fn) => fn().catch((e) => say(e.message, "err"));

  // ---- the schedule editor
  const newDefinition = () => ({
    default: Object.fromEntries(DAYS.map((d) => [d, { videos: [{ from: 0, to: 24, lists: [] }], commercials: [], cards: [] }])),
    seasons: { dates: { spring: "03-01", summer: "06-01", fall: "09-01", winter: "12-01" }, spring: null, summer: null, fall: null, winter: null },
    events: [],
  });
  const eventById = () => state.schedule.events.find((e) => e.id === state.eventId) || null;
  const newId = () => Math.random().toString(16).slice(2, 10);
  const mdParts = (s) => s.split("-").map(Number);
  const pad = (n) => String(n).padStart(2, "0");

  // the week (or single day) the calendar is showing, and what is true of it
  function view() {
    const s = state.schedule;
    if (state.tab === "default") return { columns: DAYS.map((d) => ({ key: d, label: cap(d).slice(0, 3) })), day: (k) => s.default[k], full: false };
    if (SEASONS.includes(state.tab)) {
      const w = s.seasons[state.tab];
      if (!w) return null;
      return { columns: DAYS.map((d) => ({ key: d, label: cap(d).slice(0, 3) })), day: (k) => w[k], full: false };
    }
    const ev = eventById();
    if (!ev) return null;
    if (ev.mode === "same") return { columns: [{ key: "every", label: "Every day" }], day: () => ev.schedule, full: ev.replace };
    return { columns: DAYS.map((d) => ({ key: d, label: cap(d).slice(0, 3) })), day: (k) => ev.schedule[k], full: ev.replace };
  }

  function tabs(box, items, current, onPick) {
    box.replaceChildren();
    for (const [id, label] of items) {
      const b = el("button", { type: "button", className: "tab", textContent: label });
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(id === current));
      b.addEventListener("click", () => onPick(id));
      box.append(b);
    }
  }

  function seasonRange(s) {
    const order = [...SEASONS].sort((a, b) => state.schedule.seasons.dates[a].localeCompare(state.schedule.seasons.dates[b]));
    const next = state.schedule.seasons.dates[order[(order.indexOf(s) + 1) % 4]];
    const [m, d] = mdParts(next);
    const end = new Date(2001, m - 1, d - 1);
    return `${MONTHS[end.getMonth()]} ${end.getDate()}`;
  }

  function renderTabs() {
    tabs($("season-tabs"), [["default", "Default"], ...SEASONS.map((s) => [s, cap(s)]), ["special", "Special days"]], state.tab, (id) => {
      state.tab = id;
      render();
    });
    tabs($("layer-tabs"), [["videos", "Videos"], ["commercials", "Commercials"], ["cards", "Mid cards"]], state.layer, (id) => { state.layer = id; render(); });
  }

  function renderSeasonOptions() {
    const box = $("season-opts");
    box.replaceChildren();
    if (state.tab === "default") {
      box.append(el("span", { className: "dim", textContent: "The normal week. It plays whenever no season or special day says otherwise." }));
      return;
    }
    if (!SEASONS.includes(state.tab)) return;
    const s = state.tab, seasons = state.schedule.seasons;
    const inherit = el("input", { type: "checkbox", checked: seasons[s] === null });
    inherit.addEventListener("change", () => {
      seasons[s] = inherit.checked ? null : clone(state.schedule.default);
      render();
    });
    const [m, d] = mdParts(seasons.dates[s]);
    const month = el("select", { ariaLabel: `${cap(s)} starts in` });
    MONTHS.forEach((name, i) => month.append(el("option", { value: i + 1, textContent: name })));
    month.value = m;
    const day = el("input", { type: "number", min: 1, max: 31, value: d, ariaLabel: `${cap(s)} starts on day` });
    const set = () => {
      const mm = Number(month.value), dd = Math.min(Number(day.value) || 1, new Date(2001, mm, 0).getDate());
      seasons.dates[s] = `${pad(mm)}-${pad(dd)}`;
      render();
    };
    month.addEventListener("change", set);
    day.addEventListener("change", set);
    box.append(el("label", { className: "check" }, inherit, el("span", { textContent: "Same as the default schedule" })),
      el("label", {}, el("span", { textContent: `${cap(s)} starts` }), el("span", { className: "row" }, month, day)),
      el("span", { className: "dim", textContent: `runs until ${seasonRange(s)}` }));
  }

  // ---- special days
  const WEEKDAY_NAMES = DAYS.map(cap);
  function whenText(ev) {
    const w = ev.when;
    if (w.type === "nth") {
      return `${["", "1st", "2nd", "3rd", "4th", "5th"][w.nth] || "last"} ${cap(w.weekday)} of ${MONTHS[w.month - 1]}` + (w.days > 1 ? `, ${w.days} days` : "") + ", every year";
    }
    const [sm, sd] = mdParts(w.start), [em, ed] = mdParts(w.end);
    const span = w.start === w.end ? `${MONTHS[sm - 1]} ${sd}` : `${MONTHS[sm - 1]} ${sd} – ${MONTHS[em - 1]} ${ed}`;
    return span + (w.year ? `, ${w.year}` : ", every year");
  }

  function renderEvents() {
    const box = $("event-list");
    box.replaceChildren();
    const evs = state.schedule.events;
    if (!evs.length) box.append(el("p", { className: "dim", textContent: "No special days yet. Add one for something like Shark Week or a Thanksgiving lineup." }));
    for (const ev of evs) {
      const row = el("div", { className: "event" });
      row.setAttribute("aria-current", String(ev.id === state.eventId));
      row.append(el("strong", { textContent: ev.name || "(unnamed)" }), el("span", { className: "when", textContent: whenText(ev) + (ev.replace ? " · replaces the whole day" : " · only the slots drawn") }));
      const rm = el("button", { type: "button", className: "btn danger", textContent: "Delete" });
      rm.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!confirm(`Delete “${ev.name}”?`)) return;
        state.schedule.events = evs.filter((x) => x.id !== ev.id);
        if (state.eventId === ev.id) state.eventId = null;
        render();
      });
      row.append(rm);
      row.addEventListener("click", () => { state.eventId = ev.id; render(); });
      box.append(row);
    }
  }

  function renderEventForm() {
    const box = $("event-form"), ev = eventById();
    box.hidden = !ev;
    box.replaceChildren();
    if (!ev) return;
    const field = (label, ...kids) => el("label", {}, el("span", { textContent: label }), ...kids);
    const name = el("input", { type: "text", value: ev.name, maxLength: 60 });
    name.addEventListener("input", () => { ev.name = name.value; renderEvents(); schedulePreview(); });
    const kindSel = el("select");
    kindSel.append(el("option", { value: "range", textContent: "Certain dates" }), el("option", { value: "nth", textContent: "A day like “the 4th Thursday of November”" }));
    kindSel.value = ev.when.type;
    kindSel.addEventListener("change", () => {
      const now = new Date();
      ev.when = kindSel.value === "nth" ? { type: "nth", month: 11, weekday: "thursday", nth: 4, days: 1 }
        : { type: "range", start: `${pad(now.getMonth() + 1)}-${pad(now.getDate())}`, end: `${pad(now.getMonth() + 1)}-${pad(now.getDate())}`, year: now.getFullYear() };
      render();
    });
    const monthSel = (value, onChange) => {
      const s = el("select");
      MONTHS.forEach((m, i) => s.append(el("option", { value: i + 1, textContent: m })));
      s.value = value;
      s.addEventListener("change", () => onChange(Number(s.value)));
      return s;
    };
    const dayNum = (value, onChange) => {
      const i = el("input", { type: "number", min: 1, max: 31, value });
      i.addEventListener("change", () => onChange(Math.max(1, Math.min(31, Number(i.value) || 1))));
      return i;
    };
    const whenBox = el("fieldset");
    if (ev.when.type === "range") {
      const [sm, sd] = mdParts(ev.when.start), [em, ed] = mdParts(ev.when.end);
      const setStart = (m, d) => { ev.when.start = `${pad(m)}-${pad(d)}`; if (ev.when.year && ev.when.end < ev.when.start) ev.when.end = ev.when.start; render(); };
      const setEnd = (m, d) => { ev.when.end = `${pad(m)}-${pad(d)}`; render(); };
      const every = el("input", { type: "checkbox", checked: !ev.when.year });
      every.addEventListener("change", () => { ev.when.year = every.checked ? null : new Date().getFullYear(); render(); });
      whenBox.append(field("From", monthSel(sm, (m) => setStart(m, sd)), dayNum(sd, (d) => setStart(sm, d))),
        field("To", monthSel(em, (m) => setEnd(m, ed)), dayNum(ed, (d) => setEnd(em, d))),
        el("label", { className: "check" }, every, el("span", { textContent: "Every year" })));
      if (ev.when.year) {
        const y = el("input", { type: "number", min: 2000, max: 2100, value: ev.when.year });
        y.addEventListener("change", () => { ev.when.year = Number(y.value) || new Date().getFullYear(); render(); });
        whenBox.append(field("Year", y));
      }
    } else {
      const nth = el("select");
      [[1, "1st"], [2, "2nd"], [3, "3rd"], [4, "4th"], [5, "5th"], [-1, "last"]].forEach(([v, l]) => nth.append(el("option", { value: v, textContent: l })));
      nth.value = ev.when.nth;
      nth.addEventListener("change", () => { ev.when.nth = Number(nth.value); render(); });
      const wd = el("select");
      DAYS.forEach((d) => wd.append(el("option", { value: d, textContent: cap(d) })));
      wd.value = ev.when.weekday;
      wd.addEventListener("change", () => { ev.when.weekday = wd.value; render(); });
      whenBox.append(field("The", nth, wd), field("of", monthSel(ev.when.month, (m) => { ev.when.month = m; render(); })),
        field("for how many days", dayNum(ev.when.days, (d) => { ev.when.days = d; render(); })));
    }
    const replace = el("input", { type: "checkbox", checked: ev.replace });
    replace.addEventListener("change", () => {
      ev.replace = replace.checked;
      if (ev.replace) fillGaps(ev);
      render();
    });
    const mode = el("select");
    mode.append(el("option", { value: "same", textContent: "The same lineup every day" }), el("option", { value: "weekday", textContent: "A different lineup for each weekday" }));
    mode.value = ev.mode;
    mode.addEventListener("change", () => {
      if (mode.value === ev.mode) return;
      if (mode.value === "weekday") ev.schedule = Object.fromEntries(DAYS.map((d) => [d, clone(ev.schedule)]));
      else {
        const differ = DAYS.some((d) => JSON.stringify(ev.schedule[d]) !== JSON.stringify(ev.schedule.monday));
        if (differ && !confirm("Use Monday's lineup for every day? The other days' lineups will be lost.")) { mode.value = ev.mode; return; }
        ev.schedule = clone(ev.schedule.monday);
      }
      ev.mode = mode.value;
      render();
    });
    box.append(field("Name", name), field("Dates", kindSel), whenBox, field("Lineup", mode),
      el("label", { className: "check" }, replace, el("span", { textContent: "Replace the whole day (otherwise only the slots you draw change, and the rest of the day plays as normal)" })));
  }

  // a replaced day must be filled: give the empty stretches the first list so nothing is left bare
  function fillGaps(ev) {
    const days = ev.mode === "same" ? [ev.schedule] : DAYS.map((d) => ev.schedule[d]);
    for (const day of days) {
      let blocks = day.videos;
      if (!blocks.length) blocks = [{ from: 0, to: 24, lists: state.lastLists.length ? [...state.lastLists] : [] }];
      else {
        blocks = clone(blocks).sort((a, b) => a.from - b.from);
        const out = [];
        let mark = 0;
        for (const b of blocks) {
          if (b.from > mark) out.push({ from: mark, to: b.from, lists: [...blocks[0].lists] });
          out.push(b);
          mark = b.to;
        }
        if (mark < 24) out.push({ from: mark, to: 24, lists: [...blocks[blocks.length - 1].lists] });
        blocks = CalendarOps.tidy(out);
      }
      day.videos = blocks;
    }
  }

  $("add-event").addEventListener("click", () => {
    const now = new Date();
    const md = `${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    const ev = { id: newId(), name: "New special day", when: { type: "range", start: md, end: md, year: now.getFullYear() }, replace: false, mode: "same", schedule: emptyDay() };
    state.schedule.events.push(ev);
    state.eventId = ev.id;
    render();
  });

  // ---- the calendar, and the slot under the pointer
  // ---- the Shows choice: folder -> series -> season -> episode, each narrowing the one before. A folder alone is
  // not a choice; a series alone is the whole series (played in order); a season plays that season; an episode (or a
  // movie) plays that one video. Nothing needs a list made first.
  const mediaList = (() => {
    const cache = new Map();
    return (path) => {
      if (!cache.has(path)) cache.set(path, call(`/api/files/list?root=media&path=${encodeURIComponent(path)}`).catch((e) => { cache.delete(path); throw e; }));
      return cache.get(path);
    };
  })();
  const joinPath = (a, b) => (a ? `${a}/${b}` : b);

  // a big series on a slow share takes a while to link: the page carries on, and says so until it is done
  let linkTimer = null;
  function trackLinking(tag) {
    state.linking.add(tag);
    if (linkTimer) return;
    linkTimer = setInterval(async () => {
      const got = await call("/api/files/shows").catch(() => null);
      if (!got) return;
      state.linking = new Set(got.linking || []);
      state.members = got.members || state.members;
      if (cal && !document.querySelector(".chooser-panel:not([hidden])")) renderSlotEditor();      // not while someone is browsing
      if (!state.linking.size) { clearInterval(linkTimer); linkTimer = null; }
    }, 2000);
  }

  // ---- the chooser: lists are folders like any other. Open "Lists" or "Media", go down into series, seasons, folders and
  // lists, and use any of them, one video, a YouTube playlist, or all of a channel's shorts, streams or standard videos.
  // Lists are for files you want to reuse; anything from the media (or a video inside a list) becomes a source of its own the
  // first time it is chosen, so nothing needs a list made first. A field that takes one list (a mid card, a commercial)
  // can still have several: they are put together as one source ("combine").
  const TYPE_NAMES = { videos: "All standard videos", shorts: "All shorts", streams: "All streams" };
  const seasonKind = (name) => {
    const m = /^Season (\d{4,5})$/.exec(name.trim());
    if (!m) return null;
    const n = Number(m[1]);
    return n >= 20000 ? "streams" : n >= 10000 ? "shorts" : "videos";
  };
  const memberText = (m) => `${m.path.split("/").pop()}${m.types ? ` (${m.types.join(", ")})` : m.root === "tags" ? " (list)" : ""}`;

  function chooser({ selected, multi, combine, onChange, none, label, pool }) {
    const box = el("div", { className: "chooser" });
    const chips = el("div", { className: "chips" });
    const toggle = el("button", { type: "button", className: "btn", textContent: multi || combine ? "Add…" : "Change…" });
    const panel = el("div", { className: "chooser-panel", hidden: true });
    const crumbs = el("div", { className: "crumbs" });
    const rows = el("div", { className: "chooser-rows" });
    const note = el("span", { className: "dim" });
    const kind = (n) => (state.shows.includes(n) ? (state.showKinds[n] || "series") + (state.linking.has(n) ? ", linking…" : "") : "list");
    const current = () => (combine ? (selected ? state.members[selected] || [{ root: "tags", path: selected }] : []) : []);
    const chip = (text, remove) => {
      const c = el("span", { className: "chip" }, el("span", { textContent: text }));
      if (remove) {
        const x = el("button", { type: "button", className: "chip-x", textContent: "✕", ariaLabel: `Remove ${text}` });
        x.addEventListener("click", remove);
        c.append(x);
      }
      chips.append(c);
    };

    async function setMembers(members) {
      if (!members.length) return onChange("");
      if (members.length === 1 && members[0].root === "tags") return onChange(members[0].path);
      note.textContent = "Adding…";
      try {
        const body = members.length === 1 ? { path: members[0].path, types: members[0].types } : { members };
        const out = await call("/api/files/add-show", body);
        if (!state.shows.includes(out.tag)) state.shows.push(out.tag);
        state.showKinds[out.tag] = out.kind || "series";
        state.members[out.tag] = members;
        if (out.linking) trackLinking(out.tag);
        onChange(out.tag);
      } catch (e) {
        note.textContent = String(e.message || e);           // where you are looking: the message at the top is off screen
        say(String(e.message || e), "err");
      }
    }
    const sameMember = (a, b) => a.root === b.root && a.path === b.path && JSON.stringify(a.types || null) === JSON.stringify(b.types || null);
    const addMember = (m) => setMembers([...current().filter((x) => !sameMember(x, m)), m]);

    if (combine) {
      const have = current();
      for (const m of have) chip(memberText(m), () => setMembers(have.filter((x) => !sameMember(x, m))));
      if (selected && state.linking.has(selected)) chips.append(el("span", { className: "dim", textContent: "linking its videos…" }));
    } else {
      const names = multi ? selected : selected ? [selected] : [];
      for (const n of names) chip(`${n} (${kind(n)})`, multi || none ? () => onChange(multi ? selected.filter((s) => s !== n) : "") : null);
    }
    if (!chips.children.length) chips.append(el("span", { className: "dim", textContent: none || (multi || combine ? "Nothing chosen yet." : "Nothing chosen.") }));
    let loc = { root: "", path: "" };

    const chooseList = (name) => (combine ? addMember({ root: "tags", path: name }) : onChange(multi ? [...selected.filter((n) => n !== name), name] : name));
    async function useSource(body) {                                       // body: {path, types?} or {list_path}
      note.textContent = "Adding…";
      try {
        if (combine) {
          let path = body.path;
          if (body.list_path) path = (await call(`/api/files/media-path?list_path=${encodeURIComponent(body.list_path)}`)).path;
          note.textContent = "";
          return addMember({ root: "media", path, ...(body.types ? { types: body.types } : {}) });
        }
        const out = await call("/api/files/add-show", body);               // it becomes a source the first time it is chosen
        if (!state.shows.includes(out.tag)) state.shows.push(out.tag);
        state.showKinds[out.tag] = out.kind || "series";
        if (out.linking) trackLinking(out.tag);
        chooseList(out.tag);
      } catch (e) {
        note.textContent = String(e.message || e);
        say(String(e.message || e), "err");
      }
    }
    const row = (text, { open, use, why }) => {
      const r = el("div", { className: "chooser-row" });
      if (open) {
        const o = el("button", { type: "button", className: "link", textContent: text });
        o.addEventListener("click", open);
        r.append(o);
      } else r.append(el("span", { className: "chooser-name", textContent: text }));
      if (use || why) {
        const b = el("button", { type: "button", className: "btn", textContent: multi || combine ? "Add" : "Use", disabled: !use || !state.edit, title: why || "" });
        if (use) b.addEventListener("click", use);
        r.append(b);
      }
      rows.append(r);
    };
    const go = (root, path) => { loc = { root, path }; draw(); };
    async function draw() {
      crumbs.replaceChildren();
      rows.replaceChildren();
      const crumb = (text, root, path) => {
        const b = el("button", { type: "button", className: "link", textContent: text });
        b.addEventListener("click", () => go(root, path));
        crumbs.append(b, el("span", { className: "dim", textContent: " / " }));
      };
      crumb("Browse", "", "");
      if (loc.root) crumb(loc.root === "lists" ? "Lists" : "Media", loc.root, "");
      const parts = loc.path ? loc.path.split("/") : [];
      parts.forEach((p, i) => crumb(p, loc.root, parts.slice(0, i + 1).join("/")));
      if (!loc.root) {
        row("Lists (files you re-use)", { open: () => go("lists", "") });
        row("Media (series, seasons, folders, videos, playlists)", { open: () => go("media", "") });
        return;
      }
      let entries = [];
      try {
        if (loc.root === "lists" && !loc.path) {
          entries = (pool || state.lists).map((n) => ({ name: n, kind: "dir", list: true }));
        } else if (loc.root === "lists") {
          entries = (await call(`/api/files/list?root=tags&path=${encodeURIComponent(loc.path)}`)).entries;
        } else {
          entries = (await mediaList(loc.path)).entries;
        }
      } catch (e) {
        rows.append(el("span", { className: "dim", textContent: String(e.message || e) }));
        return;
      }
      const here = (n) => (loc.path ? `${loc.path}/${n}` : n);
      if (loc.root === "lists" && loc.path && !loc.path.includes("/")) row("This whole list", { use: () => chooseList(loc.path) });
      if (loc.root === "media" && loc.path.includes("/")) {
        // a YouTube channel keeps shorts, streams and standard videos in seasons of their own: choose a kind in one go
        const kinds = [...new Set(entries.filter((e) => e.kind === "dir").map((e) => seasonKind(e.name)).filter(Boolean))];
        if (kinds.length && entries.some((e) => /^Season \d{4,5}$/.test(e.name) && Number(e.name.split(" ")[1]) >= 1000)) {
          for (const k of ["videos", "shorts", "streams"].filter((x) => kinds.includes(x))) {
            row(`${TYPE_NAMES[k]} (every season, kept up to date)`, { use: () => useSource({ path: loc.path, types: [k] }) });
          }
        }
      }
      if (!entries.length) rows.append(el("span", { className: "dim", textContent: "Nothing here." }));
      for (const e of entries) {
        const isDir = e.kind === "dir";
        const text = e.label || e.name;
        if (!isDir && !e.video && e.kind !== "playlist") continue;          // videos and playlists are choosable
        if (loc.root === "lists" && !loc.path) {
          row(e.name, { open: () => go("lists", e.name), use: () => chooseList(e.name) });
        } else if (loc.root === "lists") {
          row(text, { open: isDir ? () => go("lists", here(e.name)) : null, use: e.kind === "playlist" ? null : () => useSource({ list_path: here(e.name) }) });
        } else {
          const top = !loc.path;                                           // a whole top folder of the media is not one choice
          const holder = isDir && (top || text === "_Playlists");           // nor is the folder the playlists are kept in
          row(e.kind === "playlist" ? `${text} (YouTube playlist)` : text,
            { open: isDir ? () => go("media", here(e.name)) : null,
              use: holder ? null : () => useSource({ path: here(e.name) }),
              why: holder ? (text === "_Playlists" ? "Open it and choose a playlist" : "Open it and choose a series, a season, or a video inside") : "" });
        }
      }
    }
    toggle.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) draw();
    });
    panel.append(crumbs, rows, note);
    box.append(chips, toggle, panel);
    return box;
  }

  function renderSlotEditor() {
    const box = $("slot-editor"), s = cal ? cal.selected() : null;
    box.hidden = !s;
    box.replaceChildren();
    if (!s) return;
    const b = s.block;
    const reach = cal.selectedSpan();
    if (reach) {
      box.append(el("strong", { textContent: `${cap(reach.from.key)} ${CalendarOps.clock(reach.from.h)} – ${cap(reach.to.key)} ${CalendarOps.clock(reach.to.h)}` }),
        el("span", { className: "dim", textContent: `One slot across ${reach.days} days: what you choose here is the same on every day of it. Drag its first or last edge to change when it starts or ends.` }));
    } else {
      box.append(el("strong", { textContent: `${CalendarOps.clock(b.from)} – ${CalendarOps.clock(b.to)}` + (s.key === "every" ? "" : `, ${cap(s.key)}`) }));
    }
    if (state.layer === "videos") {
      box.append(el("span", { className: "dim", textContent: "Plays from these (one is picked at random each time). Lists are folders like any other: open one, or a series, a season or a folder of your media, and add it or one video:" }),
        chooser({ selected: b.lists, multi: true, pool: state.playable, onChange: (lists) => { state.lastLists = lists; cal.update((x) => { x.lists = lists; }); } }));
      const cardPick = (label, key) => chooser({
        selected: b[key] || "", multi: false, combine: true, none: "(the time period's cards, or the channel's)", label,
        onChange: (v) => cal.update((x) => {
          x[key] = v;
          if (!x.start && !x.end) { delete x.start; delete x.end; }
          else { x.start = x.start || x.end; x.end = x.end || x.start; }       // one chosen: the other is the same
        }) });
      box.append(el("span", { className: "dim", textContent: "Mid cards for this show here, at the start of a break:" }), cardPick("Start-of-break cards for this slot", "start"),
        el("span", { className: "dim", textContent: "and at its end:" }), cardPick("End-of-break cards for this slot", "end"));
      const burn = el("input", { type: "checkbox", checked: b.burn === true });
      burn.addEventListener("change", () => cal.update((x) => { if (burn.checked) x.burn = true; else delete x.burn; }));
      const burnLabel = el("label", { className: "check" });
      burnLabel.append(burn, document.createTextNode(" Draw the captions on the picture while this plays"));
      box.append(burnLabel, el("span", { className: "dim", textContent: "Always visible, in the language chosen on the Stations page; a video without that language is left alone. Off, they are only the caption track a player can switch on." }));
      if (b.lists.some((n) => state.shows.includes(n))) {
        box.append(el("span", { className: "dim", textContent: "A show plays in order: each airing is the next episode, picking up after the last one scheduled, and starting over after the final one. A new show needs Save and build." }));
      }
    } else if (state.layer === "cards") {
      // a new block starts with nothing chosen (it is not a copy of the station's defaults or of the block before it)
      const pick = (label, key) => chooser({ selected: b[key] || "", multi: false, combine: true, none: "Nothing chosen yet: choose what plays at this end of the break.", label,
        onChange: (v) => cal.update((x) => { x[key] = v; if (key === "start" && !x.end) x.end = v; }) });
      box.append(el("span", { className: "dim", textContent: "At the start of a break during these hours:" }), pick("Start-of-break mid cards list", "start"),
        el("span", { className: "dim", textContent: "and at its end:" }), pick("End-of-break mid cards list", "end"));
      if (b.start && !b.end) cal.update((x) => { x.end = x.start; });
    } else {
      // a new block starts with nothing chosen: it is not a copy of the block before it
      box.append(el("span", { className: "dim", textContent: "Commercials from this during these hours (a list, a series, a season, a folder, a video or a playlist; add as many as you like):" }),
        chooser({ selected: b.list || "", multi: false, combine: true, none: "Nothing chosen yet: choose what plays in these hours.", label: "Commercials",
          onChange: (v) => { state.lastAds = v; cal.update((x) => { x.list = v; }); } }));
    }
    const row = el("div", { className: "row" });
    const btn = (label, cls, fn) => { const x = el("button", { type: "button", className: "btn " + cls, textContent: label }); x.addEventListener("click", fn); return x; };
    row.append(btn(reach ? "Delete all days" : "Delete", "danger", () => cal.removeSelected()), btn("Copy", "", () => cal.copySelected()));
    if (reach) row.append(btn("Split into separate days", "", () => cal.unlink()));
    const v = view();
    if (v && v.columns.length > 1) {
      row.append(el("span", { className: "dim", textContent: "Same time on:" }));
      const others = (pick) => v.columns.map((c) => c.key).filter((k) => k !== s.key && pick(k));
      const paste = (pick) => () => { const keys = others(pick); if (keys.length) cal.pasteBlockOn(keys, { ...clone(cal.selected().block) }); };
      row.append(btn("Weekdays", "", paste((k) => !["saturday", "sunday"].includes(k))), btn("Weekend", "", paste((k) => ["saturday", "sunday"].includes(k))), btn("All days", "", paste(() => true)));
    }
    box.append(row);
  }

  function renderProblem() {
    const v = view();
    let text = "";
    if (v) {
      for (const c of v.columns) {
        const p = CalendarOps.problem(v.day(c.key).videos, v.full, true);
        if (p) { text = `${c.key === "every" ? "" : cap(c.key) + ": "}${p}.`; break; }
      }
    }
    $("cal-problem").textContent = text;
  }

  function renderLayerNote() {
    const note = $("layer-note");
    if (state.layer === "videos") note.textContent = "Times snap to half hours. A slot that starts or ends at half past can hold only one list. Drag from one day to another to make one slot that runs across days, with the same lists, cards and captions on each.";
    else if (state.layer === "cards") note.textContent = "Mid cards for a time period, by the hour: the list played at the start of a break and the one at its end (they can be the same). A video slot with cards of its own (choose it on the Videos tab) uses those; hours you leave empty use the station's own two lists (below)" +
      ($("f-cards").checked ? "." : ", which are switched off: those hours have no cards, and a block you draw here turns cards on for its hours.");
    else note.textContent = "Commercials are chosen by the hour, for the hours that have videos. Hours you leave empty use the station's own commercial list (below)" +
      ($("f-com").checked ? "." : ", which is switched off: those hours have no breaks, and a block you draw here turns commercials on for its hours.");
  }

  function render() {
    renderTabs();
    renderSeasonOptions();
    $("special").hidden = state.tab !== "special";
    if (state.tab === "special") { renderEvents(); renderEventForm(); }
    const v = view();
    $("editor").hidden = !v;
    if (v) {
      drawCalendar(v);
      renderLayerNote();
      renderProblem();
      renderSlotEditor();
    } else {
      cal = null;
    }
    schedulePreview();
  }
  // a fresh calendar for each view, so no selection is left over from another tab
  function drawCalendar(v) {
    cal = Calendar.create({
      host: $("calendar"),
      columns: v.columns,
      getDay: (k) => view().day(k),
      layer: () => state.layer,
      full: () => view().full,
      // a slot drawn on a day that is already filled starts with no list (so it shows, and you choose); on a day with
      // gaps it starts like the last one you made
      defaults: () => (state.layer === "videos" ? { lists: view().full ? [] : [...state.lastLists] }
        : state.layer === "cards" ? { start: "", end: "" }
        : { list: "" }),
      onChange: () => { renderProblem(); schedulePreview(); renderSlotEditor(); },
      onSelect: () => renderSlotEditor(),
    });
    cal.render();
  }

  // which layer wins each of the next days, for the draft being edited
  let previewTimer = null;
  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => run(showPreview), 500);
  }
  async function showPreview() {
    const box = $("preview");
    if (!state.schedule) return;
    let out;
    try {
      out = await call("/api/stations/schedule-preview", { schedule: state.schedule });
    } catch (e) {
      box.replaceChildren(el("span", { className: "dim", textContent: `Next days: ${e.message}` }));
      return;
    }
    box.replaceChildren(el("span", { className: "dim", textContent: "Next 14 days:" }));
    for (const d of out.days) {
      const label = d.layer === "event" ? d.name + (d.replace ? "" : " (some slots)") : d.layer === "season" ? cap(d.name) : d.name === "default" ? "Default" : `${cap(d.name)} (default)`;
      const date = new Date(d.date + "T00:00:00");
      box.append(el("div", { className: "day " + d.layer, title: `${cap(d.weekday)} ${d.date}` }, el("b", { textContent: `${cap(d.weekday).slice(0, 3)} ${date.getDate()}` }), el("span", { textContent: label })));
    }
  }

  // ---- the rest of the form
  // a form field that holds one list name, chosen with the browser: a hidden input keeps the value, so the rest of the form
  // reads it as it always did. Several sources chosen for it are put together as one.
  function bindChooser(id, pool) {
    const input = $(id), host = $(id + "-box");
    const draw = () => host.replaceChildren(chooser({ selected: input.value, multi: false, combine: true, pool,
      onChange: (v) => { input.value = v; draw(); input.dispatchEvent(new Event("change")); } }));
    return { set: (v) => { input.value = v || ""; draw(); } };
  }
  let fields = null;                                   // made once the lists are known (apply)

  function fillSelect(sel, names, current, withNone) {
    sel.replaceChildren();
    if (withNone) sel.append(el("option", { value: "", textContent: "(none)" }));
    for (const n of names) sel.append(el("option", { value: n, textContent: n }));
    if (current && !names.includes(current)) sel.append(el("option", { value: current, textContent: `${current} (missing)` }));
    sel.value = current || (withNone ? "" : names[0] || "");
  }

  function kind() {
    return document.querySelector("input[name=kind]:checked").value;
  }

  // ---- start from a template: the slot length and a week laid out for a kind of channel (the lists are yours to choose)
  const WEEKEND = ["saturday", "sunday"];
  const TEMPLATES = [
    { id: "", label: "A blank week" },
    { id: "movies", label: "A movie channel: one list all day, 5 minute slots", increment: 5, hint: "A movie is rounded up to the next 5 minutes, not the next half hour.",
      week: () => [[0, 24, "movies"]] },
    { id: "allday", label: "One list, all day, in 30 minute slots", increment: 30, week: () => [[0, 24, "everything"]] },
    { id: "cartoons", prefix: "Cartoons", label: "A cartoon channel (like Cartoon Network): young kids early, teens mid-day, adult cartoons after 10 pm", increment: 30,
      week: (d) => (WEEKEND.includes(d)
        ? [[0, 6, "adult cartoons"], [6, 12, "young kids"], [12, 20, "teens"], [20, 22, "family"], [22, 24, "adult cartoons"]]
        : [[0, 6, "adult cartoons"], [6, 11, "young kids"], [11, 17, "teens"], [17, 22, "family"], [22, 24, "adult cartoons"]]) },
    { id: "mainstream", prefix: "Mainstream", label: "A mainstream channel: a full week with a morning show, mid-day talk, a cartoon block, prime time and late night", increment: 30,
      week: (d) => (WEEKEND.includes(d)
        ? [[0, 7, "overnight"], [7, 12, "weekend cartoons"], [12, 19, "movies and sports"], [19, 23, "prime time"], [23, 24, "late night"]]
        : [[0, 5, "overnight"], [5, 9, "morning show"], [9, 11, "daytime"], [11, 14, "mid-day talk shows"], [14, 15.5, "afternoon"],
           [15.5, 17.5, "cartoon block"], [17.5, 19, "early evening"], [19, 23, "prime time"], [23, 24, "late night"]]) },
    { id: "loop", label: "A loop: one list repeating all day (shuffled)", loop: true },
  ];
  const clockText = (h) => `${String(Math.floor(h)).padStart(2, "0")}:${h % 1 ? "30" : "00"}`;
  function layoutText(t) {
    const line = (d) => t.week(d).map(([a, b, what]) => `${clockText(a)}–${clockText(b)} ${what}`).join(", ");
    return t.week("monday").length > 1 ? `Weekdays: ${line("monday")}. Weekends: ${line("saturday")}.` : "";
  }
  function applyTemplate(id) {
    const t = TEMPLATES.find((x) => x.id === id);
    if (!t || !t.id) return;
    const lists = state.playable;
    const name = $("f-name").value, number = $("f-number").value ? Number($("f-number").value) : null;
    const bump = state.lists.includes("bump") ? "bump" : state.lists[0];
    if (t.loop) {
      apply({ name, number, kind: "loop", loop: { list: lists[0] || "", shuffle: true } });
    } else {
      const defn = newDefinition();
      // each part of the day starts with a different list of yours when you have several (round and round), so the week has
      // shape from the start; click a slot to choose what really plays
      // A template with a name for each part of the day (kids, mainstream) uses a list of its own for each, made when the
      // station is saved (a list may be empty at first: it is there to be filled), so the week has its shape and its names.
      // A template without one starts every part with your own lists in turn.
      const roles = [...new Set(DAYS.flatMap((d) => t.week(d).map((b) => b[2])))];
      const cap1 = (s) => s.charAt(0).toUpperCase() + s.slice(1);
      const listFor = (role) => {
        if (t.prefix) {
          const name = `${t.prefix} - ${cap1(role)}`;
          if (!state.lists.includes(name)) state.pendingLists.add(name);
          return name;
        }
        return lists.length ? lists[roles.indexOf(role) % lists.length] : "";
      };
      for (const d of DAYS) defn.default[d].videos = t.week(d).map(([from, to, role]) => ({ from, to, lists: listFor(role) ? [listFor(role)] : [] }));
      apply({ name, number, kind: "standard", schedule: defn, increment: t.increment, breaks: "end", bump, commercials: "", midcards: false });
    }
    $("template-hint").textContent = [t.hint, t.week ? layoutText(t) : "",
      t.prefix ? `Each part of the day has a list of its own (“${t.prefix} - …”), made when you save, which can be empty for now: put videos in it on the Lists & files page, or click a slot to choose something else.`
        : lists.length ? `The parts of the day start with your lists in turn; click a slot to choose what really plays.`
        : "You have no lists yet: make one on the Lists & files page, or click a slot to pick a series or folder directly."].filter(Boolean).join(" ");
  }
  $("f-template").append(...TEMPLATES.map((t) => el("option", { value: t.id, textContent: t.label })));
  $("f-template").addEventListener("change", () => { applyTemplate($("f-template").value); state.dirty = true; });

  // ---- what saving and building would do, in words, and a warning before leaving with changes that were not saved
  let libCounts = null;
  async function summarise() {
    const box = $("plan-summary");
    if (kind() === "guide") { box.textContent = ""; return; }
    if (!libCounts) {
      try {
        const lib = await (await fetch("/library.json")).json();
        libCounts = lib.never ? {} : Object.fromEntries((lib.tags || []).map((t) => [t.tag, t.videos]));
      } catch (e) { libCounts = {}; }
    }
    let names = [];
    if (kind() === "loop") names = [$("f-looplist").value];
    else if (state.schedule) {
      const days = (w) => DAYS.map((d) => w[d]);
      const weeks = [state.schedule.default, ...Object.values(state.schedule.seasons).filter((x) => x && x.monday)];
      for (const w of weeks) for (const d of days(w)) for (const b of d.videos) names.push(...b.lists);
      for (const ev of state.schedule.events) {
        const ds = ev.mode === "same" ? [ev.schedule] : DAYS.map((d) => ev.schedule[d]);
        for (const d of ds) for (const b of d.videos) names.push(...b.lists);
      }
    }
    names = [...new Set(names.filter(Boolean))];
    if (!names.length) { box.textContent = "Nothing is chosen to play yet: click a slot on the calendar and choose a list, a series, a season or a video."; return; }
    const fresh = names.filter((n) => state.pendingLists.has(n));
    const parts = names.filter((n) => !state.pendingLists.has(n)).map((n) => (n in libCounts ? `${n} (${libCounts[n]} video${libCounts[n] === 1 ? "" : "s"})`
      : state.lists.includes(n) ? `${n} (a list)` : `${n} (a source)`));
    if (fresh.length) parts.push(`${fresh.length} new list${fresh.length === 1 ? "" : "s"} named for the parts of the day (made when you save, empty until you put videos in)`);
    const empty = names.filter((n) => libCounts[n] === 0);
    const breaks = $("f-com").checked ? "Breaks use the default commercials" : "No breaks except in hours where you chose commercials";
    box.textContent = `Plays from ${parts.join(", ")}. ${breaks}.` + (empty.length ? ` ${empty.join(", ")} ${empty.length === 1 ? "has" : "have"} no videos yet, so ${empty.length === 1 ? "it" : "they"} will play nothing.` : "");
  }
  let summaryTimer = null;
  state.dirty = false;
  const touched = () => {
    clearTimeout(summaryTimer);
    summaryTimer = setTimeout(() => { summarise().catch(() => {}); }, 300);
  };
  for (const ev of ["input", "change", "click", "pointerup", "keyup"]) {
    document.getElementById("form").addEventListener(ev, (e) => {
      touched();
      if (ev !== "click" && ev !== "pointerup" && ev !== "keyup") state.dirty = true;
      else if (e.target && e.target.closest && e.target.closest("#calendar, #slot-editor, .chooser")) state.dirty = true;
    });
  }
  window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
  function showKind() {
    $("standard").hidden = kind() !== "standard";
    $("loop").hidden = kind() !== "loop";
    $("guide-panel").hidden = kind() !== "guide";
    $("f-captions-field").hidden = kind() === "guide";                     // a guide channel has no videos to caption
    $("f-level-field").hidden = kind() === "guide";                        // nor to level
    $("save-apply").hidden = kind() !== "guide";
    $("save-build").hidden = $("build-note").hidden = kind() === "guide";      // a guide channel is drawn live: nothing to build
  }
  document.querySelectorAll("input[name=kind]").forEach((r) => r.addEventListener("change", showKind));
  function breaksChanged() {
    const on = $("f-com").checked;
    $("f-comlist-box").hidden = !on;
    // the defaults are opt in: their choices are only shown while they are on. Times of day and slots that choose commercials
    // or cards of their own are opted in already, so the break settings below stay.
    for (const id of ["f-bump", "f-bumpend"]) $(id).closest(".field").hidden = !$("f-cards").checked;
    if (state.schedule) renderLayerNote();
  }
  $("f-com").addEventListener("change", breaksChanged);
  $("f-cards").addEventListener("change", breaksChanged);

  function collect() {
    const form = { name: $("f-name").value, number: $("f-number").value === "" ? null : Number($("f-number").value), kind: kind(),
      logo: $("f-logo").value.trim(), caption_track: $("f-captrack").checked, level_audio: $("f-level").checked };
    if (form.kind === "standard") {
      form.schedule = clone(state.schedule);
      form.increment = Number($("f-inc").value);
      form.breaks = $("f-breaks").value;
      form.snap = $("f-snap").checked;
      form.pack = $("f-pack").checked;
      form.bump = $("f-bump").value;
      form.bump_end = $("f-bumpend").value;
      form.midcards = $("f-cards").checked;
      form.commercials = $("f-com").checked ? $("f-comlist").value : "";
    } else if (form.kind === "guide") {
      form.guide = {
        title: $("g-title").value, hours: Number($("g-hours").value), theme: $("g-theme").value, scroll_speed: Number($("g-speed").value),
        messages: $("g-messages").value.split("\n").map((m) => m.trim()).filter(Boolean), music: $("g-music").value.trim(),
      };
    } else {
      form.loop = { list: $("f-looplist").value, shuffle: $("f-shuffle").checked };
    }
    return form;
  }

  function apply(form) {
    if (!fields) fields = { bump: bindChooser("f-bump", state.lists), bumpEnd: bindChooser("f-bumpend", state.lists),
      commercials: bindChooser("f-comlist", state.lists), loop: bindChooser("f-looplist", state.playable) };
    fields.loop.set(form.kind === "loop" ? form.loop.list : state.playable[0]);
    $("f-name").value = form.name;
    $("f-number").value = form.number;
    $("f-logo").value = form.logo || "";
    $("f-captrack").checked = form.caption_track !== false;
    $("f-level").checked = form.level_audio !== false;
    document.querySelector(`input[name=kind][value=${form.kind}]`).checked = true;
    if (form.kind === "standard") {
      state.schedule = form.schedule ? clone(form.schedule) : newDefinition();
      state.tab = "default";
      state.eventId = null;
      const first = state.schedule.default.monday.videos[0];
      state.lastLists = first ? [...first.lists] : [];
      $("f-inc").value = String(form.increment);
      $("f-breaks").value = form.breaks || "end";
      $("f-snap").checked = !!form.snap;
      $("f-pack").checked = form.pack !== false;
      fields.bump.set(form.bump);
      fields.bumpEnd.set(form.bump_end || form.bump);
      $("f-com").checked = !!form.commercials;
      $("f-cards").checked = form.midcards !== false;
      fields.commercials.set(form.commercials || (state.lists.includes("commercials") ? "commercials" : state.lists[0]));
      breaksChanged();
    } else if (form.kind === "guide") {
      const g = form.guide;
      $("g-title").value = g.title;
      $("g-theme").value = g.theme || "modern-blue";
      $("g-hours").value = String(g.hours);
      const speeds = [0.6, 1, 1.8];
      const near = speeds.reduce((a, b) => (Math.abs(b - g.scroll_speed) < Math.abs(a - g.scroll_speed) ? b : a));
      $("g-speed").value = String(near);
      $("g-messages").value = g.messages.join("\n");
      $("g-music").value = g.music;
    } else {
      fields.loop.set(form.loop.list);
      $("f-shuffle").checked = form.loop.shuffle;
    }
    showKind();
    if (form.kind === "standard") render();
    touched();
  }

  // ---- saving
  async function save() {
    const form = collect();
    if (state.pendingLists.size) {                        // the lists a template named are made now, if the station uses them
      const used = new Set(JSON.stringify(form).match(/"[^"]+"/g)?.map((s) => JSON.parse(s)) || []);
      for (const name of [...state.pendingLists]) {
        if (!used.has(name)) continue;
        try { await call("/api/files/create-tag", { name }); } catch (e) { if (!/already/i.test(e.message)) throw e; }
        if (!state.lists.includes(name)) state.lists.push(name);
        if (!state.playable.includes(name)) state.playable.push(name);
        state.pendingLists.delete(name);
      }
    }
    const out = await call("/api/stations/save", { original, form });
    state.dirty = false;
    if (kind() === "guide") await call("/api/titles/settings", { episode_names: $("g-episodes").checked });      // the episode names setting lives on this page
    return out;
  }

  async function build(name) {
    const job = await call("/api/stations/build", { name, days: 7, rebuild: true });
    for (;;) {
      say(job.message, "ok");
      await new Promise((r) => setTimeout(r, 1500));
      const j = await call(`/api/stations/job?id=${encodeURIComponent(job.id)}`);
      say(j.message, j.state === "error" ? "err" : "ok");
      if (j.state === "running") continue;
      return j;
    }
  }

  $("form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      const out = await save();
      say(`Saved “${out.name}”.` + (out.warnings.length ? " Note: " + out.warnings.join("; ") + "." : "") +
        " Use Save and build when you want the changes to take effect.", out.warnings.length ? "err" : "ok");
      if (!original) location.replace(`/station.html?name=${encodeURIComponent(out.name)}`);
    });
  });

  $("save-apply").addEventListener("click", () => run(async () => {
    const out = await save();
    const r = await call("/api/stations/guide-apply", {});
    say(`Saved “${out.name}”. ` + (r.restarted ? "The guide restarted with your changes." : "Nobody is watching it, so it will start with your changes."), "ok");
    if (!original) setTimeout(() => location.replace(`/station.html?name=${encodeURIComponent(out.name)}`), 1200);
  }));

  $("save-build").addEventListener("click", () => run(async () => {
    $("save-build").disabled = $("save").disabled = true;
    try {
      const out = await save();
      const j = await build(out.name);
      if (j.state === "done") {
        say(`“${out.name}” is built and scheduled. See it on the Schedule page.`, "ok");
        setTimeout(() => location.assign(`/schedule.html?name=${encodeURIComponent(out.name)}`), 1200);
      }
    } finally {
      $("save-build").disabled = $("save").disabled = !state.edit;
    }
  }));

  // ---- start
  run(async () => {
    const [tags, lib, shows] = await Promise.all([call("/api/files/list?root=tags&path="), call("/api/stations"),
      call("/api/files/shows").catch(() => ({ shows: [] }))]);
    state.shows = shows.shows || [];
    state.showKinds = shows.kinds || {};
    state.members = shows.members || {};
    call("/api/files/list?root=media&path=").then((m) => {         // suggestions for the music folder: the folders inside your media
      $("g-music-hints").replaceChildren(...m.entries.filter((e) => e.kind === "dir").map((e) => el("option", { value: `/mnt/media/${e.name}` })));
    }).catch(() => {});
    state.edit = lib.edit;
    call("/api/titles").then((n) => { $("g-episodes").checked = n.settings.episode_names; }).catch(() => {});
    state.lists = tags.entries.filter((e) => e.kind === "dir").map((e) => e.name);
    state.playable = state.lists.filter((n) => !NOT_PROGRAMS.has(n));
    if (original) {
      $("title").textContent = original;
      const got = await call(`/api/stations/get?name=${encodeURIComponent(original)}`);
      $("f-name").disabled = true;
      $("name-hint").textContent = "To rename a station, use Rename on the Stations page: it also rebuilds the schedule under the new name.";
      if (!got.form) {
        $("form").hidden = true;
        const box = $("locked");
        box.hidden = false;
        box.append(el("h2", { textContent: "This station can't be edited here" }),
          el("p", { textContent: `It ${got.why}.` }),
          el("p", { className: "dim", textContent: "Edit it in the FieldStation42 console, which understands every setting. Nothing here will change it." }),
          el("a", { className: "btn", href: `${location.protocol}//${location.hostname}:4242/static/stations.html`, target: "_blank", rel: "noopener", textContent: "Open the FieldStation42 console ↗" }),
          el("a", { className: "btn", href: "/stations.html", textContent: "Back to stations" }));
        return;
      }
      apply(got.form);
    } else if (copyOf) {
      const got = await call(`/api/stations/get?name=${encodeURIComponent(copyOf)}`);
      if (!got.form) throw new Error(`“${copyOf}” can't be copied here: ${got.why || "it uses settings this form can't hold"}.`);
      apply({ ...got.form, name: `${copyOf} copy`, number: null });
      $("template-field").hidden = true;
      $("title").textContent = "New station (a copy)";
      state.dirty = false;
      $("f-name").focus();
      $("f-name").select();
    } else {
      const bump = state.lists.includes("bump") ? "bump" : state.lists[0];
      apply({ name: "", number: null, kind: "standard", schedule: null, increment: 30, breaks: "end", bump, commercials: "", midcards: false });
      $("template-field").hidden = false;                  // a new station can start from a template
      state.dirty = false;
      $("f-name").focus();
    }
    if (!state.edit) {
      say("Changing stations is switched off on this server (DASHBOARD_EDIT=0).", "err");
      $("save").disabled = $("save-build").disabled = $("save-apply").disabled = true;
    }
  });
})();
