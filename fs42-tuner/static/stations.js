(function () {
  "use strict";
  const { el, call, fmtWhen } = W;
  W.nav("stations");

  const $ = (id) => document.getElementById(id);
  const jobs = new Map();            // station name -> the message of a build in progress
  let editable = true;
  let loadedKeeper = false;

  function say(text, kind) {
    $("msg").textContent = text || "";
    $("msg").className = "msg" + (kind ? " " + kind : "");
  }
  const run = (fn) => fn().catch((e) => say(e.message, "err"));

  function status(s) {
    if (s.type === "guide") return ["On air. The guide is drawn live: nothing needs scheduling.", "ok"];
    if (!s.scheduled_until) return ["Off air: nothing is scheduled yet. Use Build to make a schedule.", "bad"];
    if (!s.on_air) return [`Off air: the schedule ended ${fmtWhen(s.scheduled_until)}. Add days to put it back on.`, "bad"];
    if (s.low) return [`On air, but the schedule ends ${fmtWhen(s.scheduled_until)}. Add days soon.`, "warn"];
    return [`On air. Scheduled until ${fmtWhen(s.scheduled_until)}.`, "ok"];
  }

  function card(s) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, el("span", { className: "num", textContent: `#${s.number}` }), el("span", { textContent: s.name }),
      el("span", { className: "type", textContent: s.type })));
    if (s.lists.length) {
      const chips = el("div", { className: "chips" });
      for (const l of s.lists) chips.append(el("span", { className: "chip", textContent: l }));
      c.append(chips);
    }
    if (s.number === 0) c.append(el("p", { className: "why", textContent: "Channel 0 doesn't work in Jellyfin and some other IPTV clients: Edit the station and give it a number from 1 up." }));
    const [text, kind] = status(s);
    c.append(el("p", { className: "status " + kind, textContent: text }));
    if (!s.editable) c.append(el("p", { className: "why", textContent: `Not editable here: ${s.why}.` }));
    const busy = jobs.get(s.name) || (s.job && s.job.message);
    const actions = el("div", { className: "actions" });
    const btn = (label, cls, fn, disabled) => {
      const b = el("button", { type: "button", className: "btn " + (cls || ""), textContent: label, disabled: !!disabled });
      b.addEventListener("click", fn);
      return b;
    };
    if (s.editable) actions.append(el("a", { className: "btn", href: `/station.html?name=${encodeURIComponent(s.name)}`, textContent: "Edit" }));
    if (s.editable && s.type !== "guide") actions.append(el("a", { className: "btn", href: `/station.html?copy=${encodeURIComponent(s.name)}`, textContent: "Duplicate", title: "A new station that starts as a copy of this one" }));
    if (s.type !== "guide") actions.append(el("a", { className: "btn", href: `/schedule.html?name=${encodeURIComponent(s.name)}`, textContent: "Schedule" }));
    if (s.type !== "guide") {                  // a guide channel is drawn live: nothing to schedule or build
      actions.append(btn("Add 7 days", "", () => run(() => build(s.name, 7, false)), !editable || busy));
      actions.append(btn("Update videos", "", () => run(() => refresh(s)), !editable || busy));
      actions.append(btn("Rebuild schedule", "", () => run(() => rebuild(s)), !editable || busy));
    }
    actions.append(btn("Rename", "", () => run(() => rename(s)), !editable || busy));
    actions.append(btn("Delete", "danger", () => run(() => remove(s)), !editable || busy));
    c.append(actions);
    if (busy) c.append(el("p", { className: "job", textContent: busy }));
    if (s.old_schedule && s.old_schedule.interrupted) {      // started, then the server restarted (or it failed): nothing is building
      c.append(el("p", { className: "job err", textContent: `A rebuild of this schedule was started but is not running now (the server was restarted, or it stopped with a problem). The old schedule is still playing, until ${fmtWhen(s.old_schedule.cut)}.` }));
      c.append(btn("Resume the rebuild", "primary", () => run(() => build(s.name, 7, true, false, true)), !editable));
    } else if (s.old_schedule) {                // a rebuild is in progress or has just finished: the channel stays on air
      c.append(el("p", { className: "job", textContent: s.old_schedule.finished
        ? `Playing the old schedule until ${fmtWhen(s.old_schedule.cut)}, then the new one starts.`
        : "Playing the old schedule while the new one is built." }));
    }
    return c;
  }

  // ---- the schedule keeper
  async function loadKeeper(first) {
    const k = await call("/api/keeper");
    if (first) {
      $("k-on").checked = k.settings.enabled;
      const opts = [...$("k-days").options].map((o) => Number(o.value));
      if (!opts.includes(k.settings.days_ahead)) {
        $("k-days").append(el("option", { value: k.settings.days_ahead, textContent: `${k.settings.days_ahead} days` }));
      }
      $("k-days").value = String(k.settings.days_ahead);
    }
    $("k-on").disabled = $("k-days").disabled = $("k-save").disabled = !editable;
    const st = k.status, box = $("k-status");
    let text = "", cls = "dim";
    if (!k.settings.enabled) {
      text = "Off. Stations will go off air when their schedule runs out.";
      cls = "dim";
    } else if (st.error) {
      text = `Could not check the stations: ${st.error}`;
      cls = "err";
    } else if (!st.last_pass) {
      text = "On. It checks a little after the dashboard starts, then every 30 minutes.";
    } else {
      text = `On. Last checked ${fmtWhen(st.last_pass)}.`;
      if (st.last_action) {
        text += " Last topped up: " + st.last_action.added.map((a) => `${a.station} (+${a.days} day${a.days === 1 ? "" : "s"})`).join(", ") + ` at ${fmtWhen(st.last_action.at)}.`;
      } else {
        text += " Nothing has needed topping up yet.";
      }
      if (st.more) text += " Still catching up on a long gap.";
      cls = "ok";
    }
    box.textContent = text;
    box.className = cls;
  }

  async function saveKeeper() {
    await call("/api/keeper/settings", { enabled: $("k-on").checked, days_ahead: Number($("k-days").value) });
    await loadKeeper(true);
  }
  $("k-on").addEventListener("change", () => run(async () => {
    try {
      await saveKeeper();
      say($("k-on").checked ? "Schedules will be kept topped up." : "Switched off: stations will go off air when their schedule runs out.", "ok");
    } catch (e) {
      $("k-on").checked = !$("k-on").checked;
      throw e;
    }
  }));
  $("k-save").addEventListener("click", () => run(async () => { await saveKeeper(); say("Saved.", "ok"); }));

  // ---- the volume target
  async function loadVolume() {
    const v = await call("/api/loudness");
    const sel = $("v-target");
    const t = String(Math.round(v.settings.target));
    if (![...sel.options].some((o) => o.value === t)) sel.append(el("option", { value: t, textContent: `${t} LUFS` }));
    sel.value = t;
    sel.disabled = $("v-save").disabled = !editable;
    const st = v.status || {};
    const parts = [`${st.measured || 0} videos measured`];
    if (st.queued) parts.push(`${st.queued} waiting`);
    if (st.measuring) parts.push(`measuring ${st.measuring}`);
    if (st.failed) parts.push(`${st.failed} could not be read`);
    $("v-status").textContent = parts.join(", ") + ".";
  }
  $("v-save").addEventListener("click", () => run(async () => {
    await call("/api/loudness/settings", { target: Number($("v-target").value) });
    await loadVolume();
    say("Saved. The new target is used from the next programme.", "ok");
  }));

  // ---- the caption language
  async function loadCaptions() {
    const c = await call("/api/captions");
    const sel = $("c-lang");
    if (![...sel.options].some((o) => o.value === c.settings.language)) sel.append(el("option", { value: c.settings.language, textContent: c.settings.language }));
    sel.value = c.settings.language;
    sel.disabled = $("c-save").disabled = !editable;
  }
  $("c-save").addEventListener("click", () => run(async () => {
    await call("/api/captions/settings", { language: $("c-lang").value });
    await loadCaptions();
    say("Saved. The caption language is used from the next programme.", "ok");
  }));

  async function load() {
    const data = await call("/api/stations");
    editable = data.edit;
    $("new-btn").hidden = !editable;
    const box = $("cards");
    box.replaceChildren();
    if (!data.stations.length) {
      const qs = W.quickStart(() => run(load));
      box.append(el("p", { className: "empty-state", textContent: "No stations yet. Make one with New station, or let it pick something to play:" }),
        el("p", {}, qs.button, " ", qs.note));
    }
    for (const s of data.stations) box.append(card(s));
    loadKeeper(!loadedKeeper).then(() => { loadedKeeper = true; }).catch(() => {});
    loadCaptions().catch(() => {});
    loadVolume().catch(() => {});
    if (!editable) say("Changing stations is switched off on this server (DASHBOARD_EDIT=0). You can still look.", "err");
  }

  async function build(name, days, rebuild, refresh = false, resume = false) {
    say("");
    const job = await call("/api/stations/build", { name, days, rebuild, refresh, resume });
    await follow(name, job);
  }

  // watch a background job until it ends, keeping the card's progress line current
  async function follow(name, job) {
    jobs.set(name, job.message);
    try {
      await load();
      for (;;) {
        await new Promise((r) => setTimeout(r, 1500));
        const j = await call(`/api/stations/job?id=${encodeURIComponent(job.id)}`);
        if (j.state === "running") {
          jobs.set(name, j.message);
          await load();
          continue;
        }
        jobs.delete(name);
        await load();
        say(`${name}: ${j.message}`, j.state === "done" ? "ok" : "err");
        return;
      }
    } finally {
      jobs.delete(name);          // never leave a station looking busy after an error
    }
  }

  // new videos only: they are used the next time days are added, and the schedule is not touched
  async function refresh(s) {
    await build(s.name, 1, false, true);
  }

  async function rebuild(s) {
    if (!confirm(`Rebuild the whole schedule of “${s.name}”? This throws its schedule away and makes a new one from scratch (7 days). The channel keeps playing the old schedule until the new one is ready.\n\nTo just bring in videos you added, use Update videos instead: it leaves the schedule alone.`)) return;
    await build(s.name, 7, true);
  }

  async function rename(s) {
    const to = prompt(`New name for “${s.name}”?`, s.name);
    if (!to || to.trim() === s.name) return;
    if (!confirm(`Rename “${s.name}” to “${to.trim()}”? FieldStation42 keeps a station's schedule under its name, so the old schedule is cleared and a new catalog and 7 days are built under the new name. It is off air for a moment.`)) return;
    say("");
    const out = await call("/api/stations/rename", { name: s.name, to: to.trim() });
    await load();
    if (out.job) {
      await follow(out.name, out.job);
    } else {
      say(`Renamed to “${out.name}”.`, "ok");
    }
  }

  async function remove(s) {
    if (!confirm(`Delete the station “${s.name}”? Its lists and your media are not touched.`)) return;
    await call("/api/stations/delete", { name: s.name });
    await load();
    say(`Deleted “${s.name}”.`, "ok");
  }

  run(load);
  // a build started elsewhere, or before this page was reloaded, shows up too: look often while one runs
  setInterval(() => { if (!jobs.size) load().catch(() => {}); }, 4000);
})();
