(function () {
  "use strict";

  // The Files page: build the lists (tag folders) a station plays from. Names come from your disk, so every
  // one is put on the page with textContent, never as HTML.

  // one view of the media: the pool of links (or the media folders when there is no pool), with the converted
  // YouTube library showing up inside it as a folder
  const ROOT = "media", LABEL = "Media";
  const $ = (id) => document.getElementById(id);

  const state = {
    edit: true,
    roots: {},
    src: { root: "media", path: "" },
    srcPick: new Set(),
    tag: null,
    tagPath: "",
    tagPick: new Set(),
    counts: {},
  };


  // ---- talking to the server
  async function call(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    let res, data;
    const tries = body === undefined ? 8 : 1;          // looking things up is safe to repeat through a short restart
    for (let attempt = 1; ; attempt++) {
      try {
        res = await fetch(path, opts);
        data = await res.json();
        break;
      } catch (e) {
        if (attempt >= tries) throw new Error("could not reach the dashboard");
        await new Promise((r) => setTimeout(r, 1500));
      }
    }
    if (res.status === 401) {                      // not logged in (or the session ended): log in, then come back
      location.assign("/login.html?next=" + encodeURIComponent(location.pathname + location.search));
      throw new Error("you need to log in");
    }
    if (!res.ok) throw new Error(data.error || `that failed (${res.status})`);
    return data;
  }
  const list = (root, path) => call(`/api/files/list?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);

  function say(text, kind) {
    const el = $("msg");
    el.textContent = text || "";
    el.className = "files-msg" + (kind ? " " + kind : "");
    if (text && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });          // the answer is at the top: bring it into view
  }
  async function run(fn) {
    try {
      await fn();
    } catch (e) {
      say(e.message, "err");
    }
  }

  // ---- small helpers
  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const k of kids) node.append(k);
    return node;
  }
  const join = (a, b) => (a ? `${a}/${b}` : b);
  function fmtSize(n) {
    if (n == null) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
  }

  function crumbs(nav, rootLabel, path, go) {
    nav.replaceChildren();
    const parts = path ? path.split("/") : [];
    const add = (label, to) => {
      if (nav.childNodes.length) nav.append(el("span", { className: "sep", textContent: "/" }));
      const b = el("button", { type: "button", textContent: label });
      b.addEventListener("click", () => go(to));
      nav.append(b);
    };
    add(rootLabel, "");
    parts.forEach((p, i) => add(p, parts.slice(0, i + 1).join("/")));
  }

  // ---- the sources pane
  async function openSrc(root, path) {
    const data = await list(root, path);
    state.src = { root, path: data.path };
    state.srcPick.clear();
    crumbs($("src-crumbs"), LABEL, data.path, (to) => run(() => openSrc(root, to)));
    const ul = $("src-list");
    ul.replaceChildren();
    if (!data.entries.length) ul.append(el("li", { className: "empty", textContent: "Nothing here." }));
    for (const e of data.entries) ul.append(srcRow(e));
    if (data.truncated) ul.append(el("li", { className: "empty", textContent: "Only the first part of this folder is shown. Open a sub-folder." }));
    drawKinds(data);
    updateButtons();
  }

  // ---- a YouTube channel keeps its shorts, streams and standard videos in seasons of their own (TA_NFOWriter: Season 2026,
  // Season 12026, Season 22026): choose a kind in one go instead of every season
  const KIND_NAMES = { videos: "standard videos", shorts: "shorts", streams: "streams" };
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const seasonKind = (name) => {
    const m = /^Season (\d{4,5})$/.exec(name.trim());
    if (!m || Number(m[1]) < 1000) return null;
    const n = Number(m[1]);
    return n >= 20000 ? "streams" : n >= 10000 ? "shorts" : "videos";
  };
  function drawKinds(data) {
    const box = $("src-kinds");
    box.replaceChildren();
    const here = state.src.path;
    const kinds = ["videos", "shorts", "streams"].filter((k) => data.entries.some((e) => e.kind === "dir" && seasonKind(e.name) === k));
    box.hidden = !(here.includes("/") && kinds.length);
    if (box.hidden) return;
    box.append(el("span", { className: "dim", textContent: "This channel, by kind:" }));
    for (const k of kinds) {
      const add = el("button", { type: "button", className: "btn", textContent: "Add all", disabled: !(state.edit && state.tag) });
      add.title = state.tag ? `Every season of ${KIND_NAMES[k]}, kept up to date as new ones appear (if “Keep folders up to date” is ticked)` : "Pick a list first";
      add.dataset.kind = k;
      add.addEventListener("click", () => run(() => addSources([{ root: state.src.root, path: here, types: [k] }])));
      const src = el("button", { type: "button", className: "btn", textContent: "Use as a source", disabled: !state.edit });
      src.title = "A source of its own that a station can play without a list being made first";
      src.addEventListener("click", () => run(async () => {
        const out = await call("/api/files/add-show", { path: here, types: [k] });
        say(`“${out.tag}” is a source now${out.linking ? " (its videos are being linked)" : ""}. Choose it in a station's slot.`, "ok");
        await loadSources().catch(() => {});
        await openSrc(state.src.root, state.src.path);
      }));
      box.append(el("div", { className: "kind-row" }, el("span", { className: "kind-name", textContent: cap(KIND_NAMES[k]) }), add, src));      // one line per kind: its two buttons stay together
    }
  }

  function srcRow(e) {
    const selectable = e.kind === "dir" || e.video || e.kind === "playlist";      // a show can go in a list (a pool of shows) or be a source of its own
    const li = el("li", { className: e.kind === "dir" ? "dir" : selectable ? "" : "dim" });
    const box = el("input", { type: "checkbox", disabled: !selectable });
    box.setAttribute("aria-label", `Select ${e.name}`);
    box.addEventListener("change", () => {
      box.checked ? state.srcPick.add(e.name) : state.srcPick.delete(e.name);
      updateButtons();
    });
    const name = el("span", { className: "name" });
    if (e.kind === "dir") {
      const b = el("button", { type: "button", textContent: e.name + "/" });
      b.addEventListener("click", () => run(() => openSrc(state.src.root, join(state.src.path, e.name))));
      name.append(b);
    } else {
      name.textContent = (e.kind === "playlist" ? "♫ " : "") + (e.label || e.name);   // a movie in its own folder shows as just the movie
      if (e.label) name.title = e.name;
    }
    const meta = e.kind === "dir" ? "" : e.kind === "playlist" ? `playlist, ${e.videos} video${e.videos === 1 ? "" : "s"}` : fmtSize(e.size);
    li.append(box, name, el("span", { className: "meta", textContent: meta }));
    if (e.show) {            // a show can be a source of its own: a station picks it from its Shows menu
      li.append(el("span", { className: "meta", textContent: "show" }));
      const b = el("button", { type: "button", className: "btn", textContent: e.source ? "Stop using as a source" : "Use as a source" });
      b.title = e.source ? "Stations stop being able to pick this show" : "Stations can pick this show directly, apart from your lists. New episodes are added on their own.";
      b.addEventListener("click", () => run(async () => {
        const path = join(state.src.path, e.name);
        const out = await call(e.source ? "/api/files/remove-show" : "/api/files/add-show", e.source ? { tag: e.source } : { path });
        say(e.source ? `“${e.name}” is no longer a source.` : `“${out.tag}” is now a source: pick it from the Shows menu when you edit a station.`, "ok");
        await openSrc(state.src.root, state.src.path);
      }));
      li.append(b);
    }
    li.dataset.name = e.name;
    return li;
  }

  function setAll(ul, pick, on) {
    pick.clear();
    for (const li of ul.children) {
      const box = li.querySelector("input[type=checkbox]");
      if (!box || box.disabled) continue;
      box.checked = on;
      if (on) pick.add(li.dataset.name);
    }
    updateButtons();
  }

  // ---- the lists pane
  // The video counts come from a scan of every list that can take minutes on a big library, so the lists are drawn
  // first and the counts fill in as the scan reports them (the server never makes this page wait for it).
  let countsTimer = null;
  function paintCounts() {
    for (const li of $("tag-list").children) {
      const n = li.dataset.tag != null ? state.counts[li.dataset.tag] : null;
      const c = li.querySelector(".count");
      if (c) c.textContent = n != null ? `${n} videos` : (state.counting ? "not counted yet" : "");
      const w = (state.warns || {})[li.dataset.tag];
      let badge = li.querySelector(".badge");
      const text = w ? [w.dead ? `${w.dead} dead` : "", w.duplicates ? `${w.duplicates} duplicate` : ""].filter(Boolean).join(", ") : "";
      if (text) {
        if (!badge) { badge = el("span", { className: "badge warn" }); li.append(badge); }
        badge.textContent = text;
        badge.title = "Dead: a link whose video is gone (judged from inside the container). Duplicate: the same video linked more than once in this list, so it is scheduled more often.";
      } else if (badge) {
        badge.remove();
      }
    }
  }
  function paintSummary(lib) {
    const box = $("lib-summary");
    if (!box) return;
    if (lib.never) { box.textContent = "Counting the videos in your lists\u2026"; box.classList.remove("warn"); return; }
    if (lib.found === false) { box.textContent = ""; return; }
    const t = lib.totals || { videos: 0, dead: 0, duplicates: 0 };
    const warns = [];
    if (t.dead) warns.push(`${t.dead} dead link${t.dead === 1 ? "" : "s"}`);
    if (t.duplicates) warns.push(`${t.duplicates} duplicate${t.duplicates === 1 ? "" : "s"}`);
    box.textContent = (lib.scanning ? "Scanning\u2026 " : "") + `${t.videos} videos in ${(lib.tags || []).length} lists` + (warns.length ? ` \u2014 ${warns.join(", ")}` : "");
    box.classList.toggle("warn", warns.length > 0);
  }
  // Rescan: ask for a fresh count, then look every few seconds until it has finished
  async function rescan() {
    say("Counting the videos in your lists again\u2026");
    await call("/library.json?refresh");
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 5000));
      const lib = await call("/library.json");
      paintSummary(lib);
      if (!lib.scanning) break;
    }
    await loadCounts(false);
    say("Counted again.", "ok");
  }
  $("lib-rescan").addEventListener("click", () => run(rescan));

  async function loadCounts(fresh, tries = 0) {
    clearTimeout(countsTimer);
    try {
      const lib = await call("/library.json");           // the last scheduled scan: opening this page never starts one
      state.counts = {};
      state.warns = {};
      for (const t of lib.tags || []) {
        state.counts[t.tag] = t.videos;
        if (t.dead || t.duplicates) state.warns[t.tag] = { dead: t.dead || 0, duplicates: t.duplicates || 0 };
      }
      state.counting = !!lib.never;                      // no scan has finished yet since the server started
      state.scanAge = lib.age_seconds;
      paintSummary(lib);
      paintCounts();
      if (lib.never && tries < 90) countsTimer = setTimeout(() => loadCounts(false, tries + 1).catch(() => {}), 10000);
    } catch (e) { state.counting = false; paintCounts(); }        // counts are a nicety
  }

  async function loadTags(keepOpen, fresh = true) {
    const data = await list("tags", "");
    loadCounts(fresh);
    const ul = $("tag-list");
    ul.replaceChildren();
    const tags = data.entries.filter((e) => e.kind === "dir");
    if (!tags.length) ul.append(el("li", { className: "empty", textContent: "No lists yet. Create one above." }));
    for (const t of tags) {
      const li = el("li");
      li.dataset.tag = t.name;
      li.setAttribute("aria-current", String(t.name === state.tag));
      li.append(el("span", { textContent: t.name }),
        el("span", { className: "count", textContent: state.counts[t.name] != null ? `${state.counts[t.name]} videos` : "" }));
      li.addEventListener("click", () => run(() => openTag(t.name, "")));
      ul.append(li);
    }
    if (state.tag && !tags.some((t) => t.name === state.tag)) {
      state.tag = null;
      $("tag-view").hidden = true;
    } else if (state.tag && keepOpen) {
      await openTag(state.tag, state.tagPath);
    }
    updateButtons();
  }

  async function loadFollows(tag) {
    const data = await call(`/api/files/follows?tag=${encodeURIComponent(tag)}`);
    const ul = $("follow-list");
    ul.replaceChildren();
    $("follows").hidden = !data.follows.length;
    for (const f of data.follows) {
      const li = el("li", { className: f.error ? "dead" : "" });
      li.append(el("span", { className: "name", textContent: f.root === "tags" ? `the list “${f.path}”` : f.path }));
      li.append(el("span", { className: "meta", textContent: f.error ? f.error
        : f.last_checked ? `checked ${fmtWhen(f.last_checked)}${f.last_added ? `, ${f.last_added} new` : ""}` : "not checked yet" }));
      const b = el("button", { type: "button", className: "btn", textContent: "Stop", disabled: !state.edit });
      b.addEventListener("click", () => run(async () => {
        await call("/api/files/unfollow", { tag, root: f.root, path: f.path });
        await loadFollows(tag);
        say(`No longer keeping “${f.path}” up to date. What is already in the list stays.`, "ok");
      }));
      li.append(b);
      ul.append(li);
    }
  }

  async function fillRefs(tag) {
    const all = (await list("tags", "")).entries.filter((e) => e.kind === "dir" && e.name !== tag);
    const sel = $("ref-list");
    sel.replaceChildren(...all.map((e) => el("option", { value: e.name, textContent: e.name })));
    $("ref-row").hidden = !all.length;
  }
  $("ref-btn").addEventListener("click", () => run(async () => {
    const name = $("ref-list").value;
    if (!name) return;
    const out = await call("/api/files/add", { tag: state.tag, sources: [{ root: "tags", path: name }], follow: true });
    await openTag(state.tag, state.tagPath);
    say(`“${name}” is now inside “${state.tag}” and stays in step with it (${out.added} link${out.added === 1 ? "" : "s"} added). ${REBUILD}`, "ok");
  }));

  async function openTag(tag, path) {
    loadFollows(tag).catch(() => {});
    fillRefs(tag).catch(() => {});
    const data = await list("tags", join(tag, path));
    state.tag = tag;
    state.tagPath = path;
    state.tagPick.clear();
    for (const li of $("tag-list").children) {
      li.setAttribute("aria-current", String(li.firstChild && li.firstChild.textContent === tag));
    }
    $("tag-view").hidden = false;
    crumbs($("tag-crumbs"), tag, path, (to) => run(() => openTag(tag, to)));
    const ul = $("tag-contents");
    ul.replaceChildren();
    if (!data.entries.length) ul.append(el("li", { className: "empty", textContent: "This list is empty. Pick videos on the left and add them." }));
    for (const e of data.entries) {
      const li = el("li", { className: e.kind === "dir" ? "dir" : e.link && !e.ok ? "dead" : "" });
      const box = el("input", { type: "checkbox" });
      box.setAttribute("aria-label", `Select ${e.name}`);
      box.addEventListener("change", () => {
        box.checked ? state.tagPick.add(e.name) : state.tagPick.delete(e.name);
        updateButtons();
      });
      const name = el("span", { className: "name" });
      if (e.kind === "dir" && !e.link) {
        const b = el("button", { type: "button", textContent: e.name + "/" });
        b.addEventListener("click", () => run(() => openTag(tag, join(path, e.name))));
        name.append(b);
      } else {
        name.textContent = e.name;
      }
      const meta = e.link ? (e.ok ? "link" : "dead link") : e.kind === "file" ? "real file" : "";
      li.append(box, name, el("span", { className: "meta", textContent: meta }));
      li.dataset.name = e.name;
      ul.append(li);
    }
    updateButtons();
  }

  // ---- buttons
  function updateButtons() {
    const edit = state.edit;
    const add = $("add-btn");
    add.disabled = !(edit && state.tag && state.srcPick.size);
    const n = state.srcPick.size;
    add.textContent = !state.tag ? "Pick a list to add to" : n ? `Add ${n} to “${state.tag}”` : `Add to “${state.tag}”`;
    for (const b of $("src-kinds").querySelectorAll("button[data-kind]")) b.disabled = !(edit && state.tag);       // a channel's kinds of video
    $("remove-btn").disabled = !(edit && state.tag && state.tagPick.size);
    $("rename-btn").disabled = !(edit && state.tag);
    $("delete-btn").disabled = !(edit && state.tag);
    $("new-form").querySelector("button").disabled = !edit;
  }

  $("src-all").addEventListener("click", () => setAll($("src-list"), state.srcPick, true));
  $("src-none").addEventListener("click", () => setAll($("src-list"), state.srcPick, false));
  $("tag-all").addEventListener("click", () => setAll($("tag-contents"), state.tagPick, true));
  $("tag-none").addEventListener("click", () => setAll($("tag-contents"), state.tagPick, false));

  const REBUILD = "A station that uses this list needs its catalog rebuilt in the schedule console to see the change.";

  $("new-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      const name = $("new-name").value;
      const out = await call("/api/files/create-tag", { name });
      $("new-name").value = "";
      state.tag = out.tag;
      state.tagPath = "";
      await loadTags(true);
      say(`Created the list “${out.tag}”. Pick videos on the left and add them.`, "ok");
    });
  });

  $("add-btn").addEventListener("click", () => run(() => addSources(
    [...state.srcPick].map((name) => ({ root: state.src.root, path: join(state.src.path, name) })))));

  async function addSources(sources) {
    // many channels can be tens of thousands of links: it runs on the server in the background and the page shows how far it is
    $("add-btn").disabled = true;
    let out;
    try {
      const started = await call("/api/files/add", { tag: state.tag, sources, follow: $("follow-on").checked, background: true });
      say("Adding… looking through what you picked", "ok");
      for (;;) {
        await new Promise((r) => setTimeout(r, 800));
        const j = await call(`/api/files/add-status?id=${encodeURIComponent(started.id)}`);
        if (j.state === "running") {
          say(`Adding to “${started.tag}”… ${j.message} (${j.elapsed}s)`, "ok");
          continue;
        }
        if (j.state === "error") throw new Error(j.message);
        out = j.result;
        break;
      }
    } finally {
      updateButtons();
    }
    let text = `Added ${out.added} link${out.added === 1 ? "" : "s"} to “${out.tag}”`;
    if (out.already_there) text += ` (${out.already_there} already there)`;
    if (out.following && out.following.length) text += `. Kept up to date: ${out.following.join(", ")}`;
    text += `. ${REBUILD}`;
    if (out.notes.length) text += ` Skipped: ${out.notes.slice(0, 3).join("; ")}${out.notes.length > 3 ? "; ..." : ""}`;
    state.srcPick.clear();
    for (const box of $("src-list").querySelectorAll("input[type=checkbox]")) box.checked = false;
    await loadTags(true);
    say(text, out.added || out.already_there ? "ok" : "err");
  }

  $("remove-btn").addEventListener("click", () => run(async () => {
    const n = state.tagPick.size;
    if (!confirm(`Remove ${n} item${n === 1 ? "" : "s"} from “${state.tag}”? Only the links go; your media is not touched.`)) return;
    const paths = [...state.tagPick].map((name) => join(state.tagPath, name));
    const out = await call("/api/files/remove", { tag: state.tag, paths });
    await loadTags(true);
    say(`Removed ${out.removed} link${out.removed === 1 ? "" : "s"}.` +
      (out.refused.length ? ` Left alone, because they are real files: ${out.refused.slice(0, 3).join(", ")}.` : "") +
      ` ${REBUILD}`, out.refused.length ? "err" : "ok");
  }));

  $("rename-btn").addEventListener("click", () => run(async () => {
    const to = prompt(`New name for “${state.tag}”? A station that uses the old name must be changed to the new one.`, state.tag);
    if (!to || to === state.tag) return;
    const out = await call("/api/files/rename-tag", { tag: state.tag, to });
    state.tag = out.tag;
    await loadTags(true);
    say(`Renamed to “${out.tag}”. ${out.note}.`, "ok");
  }));

  $("delete-btn").addEventListener("click", () => run(async () => {
    if (!confirm(`Delete the list “${state.tag}”? Only its links are removed; your media is not touched.`)) return;
    const out = await call("/api/files/delete-tag", { tag: state.tag });
    state.tag = null;
    $("tag-view").hidden = true;
    await loadTags(false);
    say(`Deleted “${out.tag}” (${out.removed} link${out.removed === 1 ? "" : "s"} removed).`, "ok");
  }));

  // ---- the YouTube (TubeArchivist) conversion: settings and what it is doing
  const OTHER = "__other__";
  const ta = { cfg: null, lastState: null };

  function fmtWhen(sec) {
    if (!sec) return "";
    return new Date(sec * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  // the library folders being converted: each shows as <its folder name>-nfo in Media
  function renderLibraries(st, det, first) {
    ta.libraries = st.libraries;
    const box = $("ta-libs");
    box.replaceChildren();
    for (const l of st.libraries) {
      const row = el("li", { className: l.exists ? "" : "dim" });
      row.append(el("span", { className: "lib-path", textContent: l.path }));
      row.append(el("span", { className: "lib-note", textContent: !l.exists ? "not found: is its volume mounted?"
        : `${l.channels === null ? "counting channels…" : l.channels + " channel" + (l.channels === 1 ? "" : "s")} → shows as ${l.name}` + (l.converted ? ` (${l.converted} converted)` : "") }));
      const rm = el("button", { type: "button", className: "btn", textContent: "Remove", disabled: !state.edit || st.libraries.length < 2 });
      rm.addEventListener("click", () => run(() => saveLibraries(st.libraries.map((x) => x.path).filter((p) => p !== l.path))));
      row.append(rm);
      box.append(row);
    }
    if (!first) return;               // polls must not reset a choice being made in the add row
    fillAdd(st, det || { libraries: [] });
  }

  function fillAdd(st, det) {
    const sel = $("ta-add");
    sel.replaceChildren();
    const have = new Set(st.libraries.map((l) => l.path));
    for (const l of det.libraries.filter((x) => !have.has(x.path))) {
      sel.append(el("option", { value: l.path, textContent: `${l.path}  (${l.channels} channel${l.channels === 1 ? "" : "s"} found)` }));
    }
    sel.append(el("option", { value: OTHER, textContent: "Another folder…" }));
    $("ta-add-other").hidden = sel.value !== OTHER;
  }

  async function saveLibraries(paths) {
    await call("/api/ta/settings", { libraries: paths });
    await loadTa(true);
    say("Saved. Each library shows in Media as its folder name plus -nfo.", "ok");
  }

  async function loadTa(first) {
    const st = await call("/api/ta/status");
    // looking for library folders walks the media, which is slow on a network share: it is asked for once, after the
    // panel is up, and never by the five-second refresh
    const det = null;
    if (first) call("/api/ta/detect").then((d) => fillAdd(st, d)).catch(() => fillAdd(st, { libraries: [] }));
    ta.cfg = st.settings;
    renderLibraries(st, det, first);
    if (first) {
      $("ta-enabled").checked = st.settings.enabled;
      $("ta-interval").value = String(st.settings.interval_minutes);
    }
    renderTaStatus(st);
    if (first) restoreOpen(st.settings.enabled);
    if (ta.lastState === "running" && st.job.state !== "running") await refreshAfterConversion();
    ta.lastState = st.job.state;
    $("ta-add-btn").disabled = !state.edit;
    $("ta-run").disabled = !state.edit || !st.settings.enabled;
    $("ta-save").disabled = !state.edit;
    $("ta-enabled").disabled = !state.edit;
  }

  function renderTaStatus(st) {
    const job = st.job || {}, el_ = $("ta-status");
    let text = "", kind = "";
    if (!st.settings.enabled) {
      text = "Off. Nothing is converted. Switch it on above when you are ready.";
    } else if (job.state === "running") {
      const since = job.last_start ? ` Started ${fmtWhen(job.last_start)}.` : "";
      const total = st.libraries.reduce((n, l) => n + (l.channels || 0), 0);
      const progress = total ? ` ${st.channels_in_view} of ${total} channels converted so far.` : "";
      const now = job.channel
        ? ` Now on ${job.channel}${job.channel_pos ? ` (channel ${job.channel_pos} of ${job.channel_total}` + (job.channels_done != null ? `, ${job.channels_done} finished` : "") + ")" : ""}` +
          (job.channel_videos_total ? `, ${job.channel_videos_done.toLocaleString()} of ${job.channel_videos_total.toLocaleString()} of its videos` : "") +
          `, last seen working ${fmtWhen(job.channel_seen)}.`
        : job.channel_total ? ` ${job.channels_done || 0} of ${job.channel_total} channels finished${job.videos_total ? `, ${job.videos_total.toLocaleString()} videos to look at` : ""}; finding the one it is on…`
        : " Looking for the channel it is on…";
      text = `Converting ${job.current || "your library"}… this can take a while the first time.${since}${now}${progress}` +
        (st.channels_in_view && st.channels_in_view < total ? " If it was interrupted, it may go back over the channels already done before reaching the rest, so the count can sit still for a while." : "");
    } else if (job.state === "error") {
      text = `That did not work: ${job.message || "unknown problem"}`;
      kind = "err";
    } else if (job.state === "idle") {
      text = `Done${job.last_end ? " " + fmtWhen(job.last_end) : ""}. ${st.channels_in_view} channel${st.channels_in_view === 1 ? "" : "s"} converted.` +
        (job.next_run ? ` Next check ${fmtWhen(job.next_run)}.` : "");
      kind = "ok";
    } else {
      text = "Starting…";
    }
    const missing = st.libraries.filter((l) => !l.exists).map((l) => l.path);
    if (st.settings.enabled && missing.length && job.state !== "error") {
      text += ` Not found: ${missing.join(", ")}.`;
      kind = "err";
    }
    el_.textContent = text;
    el_.className = "ta-status" + (kind ? " " + kind : "");
    // the one-line version, shown when the panel is folded away
    let short = "Off";
    if (st.settings.enabled) {
      if (job.state === "running") short = `● Converting ${job.current || ""}`.trim() + (job.channel ? `: ${job.channel}` : "") + "…";
      else if (job.state === "error") short = "⚠ Problem: open to see it";
      else if (missing.length) short = "⚠ A folder was not found";
      else short = `On, ${st.channels_in_view} channel${st.channels_in_view === 1 ? "" : "s"}` + (job.next_run ? `, next check ${fmtWhen(job.next_run)}` : "");
    }
    $("ta-summary").textContent = short;
  }

  // the panel can be folded away; what you chose is remembered. Until you choose, it is open only while it is off.
  let restoring = false;
  function restoreOpen(enabled) {
    let saved = null;
    try { saved = localStorage.getItem("ta-open"); } catch (e) { /* private window: no memory */ }
    restoring = true;
    $("ta-details").open = saved === null ? !enabled : saved === "1";
    setTimeout(() => { restoring = false; }, 0);
  }
  $("ta-details").addEventListener("toggle", () => {
    if (restoring) return;
    try { localStorage.setItem("ta-open", $("ta-details").open ? "1" : "0"); } catch (e) { /* not remembered */ }
  });

  // ---- sources made for stations, and which stations use them
  async function loadSources() {
    const card = $("sources-card");
    let data;
    try { data = await call("/api/files/sources-in-use"); } catch (e) { card.hidden = true; return; }
    const idle = data.empty_unused || [];
    card.hidden = !data.sources.length && !idle.length;
    $("sources-count").textContent = (data.sources.length ? `(${data.sources.length}, ${data.sources.filter((s) => !s.used_by.length).length} not used` : "(") +
      (idle.length ? `${data.sources.length ? ", " : ""}${idle.length} empty list${idle.length === 1 ? "" : "s"} no station uses` : "") + ")";
    const box = $("sources-list");
    box.replaceChildren();
    for (const s of data.sources) {
      const row = el("div", { className: "chooser-row" });
      const what = (s.members || []).map((m) => `${m.path.split("/").pop()}${m.types ? ` (${m.types.join(", ")})` : ""}`).join(", ");
      row.append(el("span", { textContent: `${s.tag} — ${s.kind || "source"}${what && s.kind === "set" ? `: ${what}` : ""} — ` +
        (s.used_by.length ? `used by ${s.used_by.join(", ")}` : "not used by any station") }));
      if (!s.used_by.length && state.edit && data.known_stations !== undefined) {
        const b = el("button", { type: "button", className: "btn", textContent: "Remove" });
        b.addEventListener("click", () => run(async () => {
          await call("/api/files/remove-show", { tag: s.tag });
          say(`Removed “${s.tag}”.`, "ok");
          await loadSources();
          await loadTags(true);
        }));
        row.append(b);
      }
      box.append(row);
    }
    if (idle.length) {
      box.append(el("p", { className: "dim", textContent: "Lists with no videos that no station uses (a template makes one for each part of the day):" }));
      for (const name of idle) {
        const row = el("div", { className: "chooser-row" }, el("span", { textContent: name }));
        if (state.edit) {
          const b = el("button", { type: "button", className: "btn", textContent: "Delete" });
          b.addEventListener("click", () => run(async () => {
            if (!confirm(`Delete the empty list “${name}”? Nothing is in it and no station uses it.`)) return;
            await call("/api/files/delete-tag", { tag: name });
            say(`Deleted “${name}”.`, "ok");
            await loadSources();
            await loadTags(true);
          }));
          row.append(b);
        }
        box.append(row);
      }
    }
  }
  $("sources-card").addEventListener("toggle", () => { if ($("sources-card").open) run(loadSources); });

  async function refreshAfterConversion() {
    try {
      const info = await call("/api/files/info");
      state.roots = info.roots;
      await openSrc(ROOT, state.src.path);
    } catch (e) { /* the panel is still right; the lists refresh on the next click */ }
  }

  $("ta-add").addEventListener("change", () => {
    $("ta-add-other").hidden = $("ta-add").value !== OTHER;
    if (!$("ta-add-other").hidden) $("ta-add-other").focus();
  });
  $("ta-add-btn").addEventListener("click", () => run(async () => {
    const v = $("ta-add").value;
    const path = v === OTHER ? $("ta-add-other").value.trim() : v;
    if (!path) throw new Error("choose a folder first");
    await saveLibraries([...ta.libraries.map((l) => l.path), path]);
    $("ta-add-other").value = "";
  }));

  async function saveTa(extra) {
    const body = { enabled: $("ta-enabled").checked, interval_minutes: Number($("ta-interval").value), ...extra };
    await call("/api/ta/settings", body);
    await loadTa(true);
    return body;
  }

  $("ta-enabled").addEventListener("change", () => run(async () => {
    try {
      const body = await saveTa();
      say(body.enabled ? "Switched on. The conversion starts within a few seconds." : "Switched off.", "ok");
    } catch (e) {
      $("ta-enabled").checked = !$("ta-enabled").checked;      // it did not take: show the real state
      throw e;
    }
  }));
  $("ta-save").addEventListener("click", () => run(async () => {
    await saveTa();
    say("Saved.", "ok");
  }));
  $("ta-run").addEventListener("click", () => run(async () => {
    await call("/api/ta/run", {});
    say("Asked it to convert now.", "ok");
    setTimeout(() => run(() => loadTa(false)), 1500);
  }));

  // ---- start
  run(async () => {
    const info = await call("/api/files/info");
    state.edit = info.edit;
    state.roots = info.roots;
    if (!info.edit) say("Changing lists is switched off on this server (DASHBOARD_EDIT=0). You can still browse.", "err");
    if (!info.tags) say("There is no catalog/by-genre folder yet. It is created when the container starts.", "err");
    if (info.roots[ROOT]) await openSrc(ROOT, "");
    else say("No media is mounted in the container, so there is nothing to browse.", "err");
    if (info.tags) await loadTags(false, false);
    updateButtons();
    await loadTa(true);
    loadSources().catch(() => {});
    setInterval(() => loadTa(false).catch(() => {}), 5000);        // quiet: a missed poll is not worth a red message
  });
})();
