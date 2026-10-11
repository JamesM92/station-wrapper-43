(function () {
  "use strict";
  const { el, call, fmtWhen } = W;
  W.nav("iptv");

  const $ = (id) => document.getElementById(id);
  const state = { sources: [], edit: true, editing: null, open: new Set() };
  const HINTS = {
    stream: "A camera or one stream: rtsp://, http://, rtmp://, udp:// or srt://. A password in the address is kept private and never shown again.",
    m3u: "The address of the playlist (an .m3u or .m3u8 file). Its channels are read now and again every few hours.",
    hdhomerun: "The tuner's address, such as http://192.168.1.20. Its channels keep their own numbers.",
  };
  const KINDS = { stream: "stream", m3u: "playlist", hdhomerun: "tuner" };
  const MODES = { convert: "converted for browsers", relay: "passed on as it is", guide: "guide only (not played here)" };

  function say(text, kind) {
    $("msg").textContent = text || "";
    $("msg").className = "msg" + (kind ? " " + kind : "");
    if (text && $("msg").scrollIntoView) $("msg").scrollIntoView({ block: "nearest" });
  }
  const run = (fn) => fn().catch((e) => say(e.message, "err"));

  async function load() {
    const data = await call("/api/iptv/sources");
    state.sources = data.sources;
    state.edit = data.edit;
    state.watching = data.watching || {};
    $("new-btn").disabled = !state.edit;
    const legacy = $("legacy");
    legacy.hidden = !data.legacy;
    legacy.replaceChildren();
    if (data.legacy) {
      legacy.append(el("h2", { textContent: "An older setting: one HDHomeRun in the config file" }),
        el("p", { className: "dim", textContent: `The config file still has a single passthrough device (${data.legacy.url}, ${data.legacy.mode} mode). Sources on this page replace it: they feed the guide and the lineup the same way, with the numbers kept stable.` }),
        el("button", { type: "button", className: "btn primary", textContent: "Move it onto this page", onclick: () => run(async () => {
          await call("/api/iptv/import-legacy", {});
          say("Moved. Restart the container once so the old setting is gone for good.", "ok");
          await load();
        }) }));
    }
    const clashes = $("clashes");
    clashes.hidden = !(data.clashes && data.clashes.length);
    clashes.replaceChildren();
    if (!clashes.hidden) {
      clashes.append(el("h2", { textContent: "Numbers that were taken" }),
        el("p", { className: "dim", textContent: "These channels asked for a number another channel already has, so they were given the next free one. Change a number below if you want a particular one." }),
        el("ul", {}, ...data.clashes.map((c) => el("li", { textContent: `${c.channel} (${c.source}) wanted ${c.wanted}` }))));
    }
    render();
  }

  function render() {
    const box = $("sources");
    box.replaceChildren();
    if (!state.sources.length) {
      box.append(el("p", { className: "dim", textContent: "No sources yet. Add one: a camera, a playlist, or another tuner." }));
      return;
    }
    for (const s of state.sources) box.append(card(s));
  }

  function card(s) {
    const c = el("div", { className: "source" + (s.enabled ? "" : " off") });
    const on = s.channels.filter((ch) => !(ch.override && ch.override.enabled === false)).length;
    c.append(el("h2", {}, el("span", { textContent: s.name }), el("span", { className: "kind", textContent: KINDS[s.type] || s.type }),
      el("span", { className: "kind", textContent: MODES[s.mode] || s.mode })));
    c.append(el("div", { className: "addr", textContent: s.url }));
    const st = s.error ? el("p", { className: "state err", textContent: `Could not read it: ${s.error}` })
      : el("p", { className: "state ok", textContent: !s.enabled ? "Switched off: its channels are not on air."
        : s.mode === "guide" ? `Guide only: its ${s.channels.length} channels' listings are in our guide; they are not in the lineup, so Jellyfin plays them from the tuner itself.`
        : `${on} of ${s.channels.length} channel${s.channels.length === 1 ? "" : "s"} on air` + (s.refreshed ? `, read ${fmtWhen(new Date(s.refreshed * 1000).toISOString())}` : "") + "." });
    c.append(st);
    const row = el("div", { className: "row" });
    const btn = (label, cls, fn, disabled) => {
      const b = el("button", { type: "button", className: "btn " + (cls || ""), textContent: label, disabled: !!disabled });
      b.addEventListener("click", fn);
      return b;
    };
    row.append(btn(s.enabled ? "Switch off" : "Switch on", "", () => run(async () => {
      await call("/api/iptv/save", { id: s.id, name: s.name, enabled: !s.enabled });
      await load();
    }), !state.edit));
    if (s.type !== "stream") row.append(btn("Read the channels again", "", () => run(async () => {
      say(`Reading ${s.name}…`, "ok");
      await call("/api/iptv/refresh", { id: s.id });
      await load();
      say(`Read ${s.name}.`, "ok");
    }), !state.edit));
    row.append(btn("Edit", "", () => openForm(s), !state.edit));
    row.append(btn("Delete", "danger", () => run(async () => {
      if (!confirm(`Remove “${s.name}”? Its channels leave the lineup, the guide and the player.`)) return;
      await call("/api/iptv/delete", { id: s.id });
      await load();
    }), !state.edit));
    c.append(row);
    if (s.channels.length) c.append(channels(s));
    return c;
  }

  function channels(s) {
    const d = el("details", {});
    d.open = state.open.has(s.id);
    d.addEventListener("toggle", () => { d.open ? state.open.add(s.id) : state.open.delete(s.id); });
    d.append(el("summary", { textContent: `Channels (${s.channels.length})` }));
    const table = el("table", { className: "chan-table" });
    table.append(el("thead", {}, el("tr", {}, ...["On", "Number", "Name", "Group", ""].map((h) => el("th", { textContent: h })))));
    const body = el("tbody", {});
    for (const ch of s.channels) {
      const ov = ch.override || {};
      const on = ov.enabled !== false;
      const tr = el("tr", { className: on ? "" : "off" });
      const save = (fields) => run(async () => { await call("/api/iptv/channel", { id: s.id, key: ch.key, ...fields }); await load(); });
      const box = el("input", { type: "checkbox", checked: on, disabled: !state.edit });
      box.setAttribute("aria-label", `${ch.name} on air`);
      box.addEventListener("change", () => save({ enabled: box.checked }));
      const num = el("input", { type: "text", className: "num", value: ov.number || ch.number || "", placeholder: "auto", disabled: !state.edit });
      num.setAttribute("aria-label", `${ch.name} number`);
      num.addEventListener("change", () => save({ number: num.value }));
      const name = el("input", { type: "text", value: ov.name || ch.name, disabled: !state.edit });
      name.setAttribute("aria-label", `${ch.name} name`);
      name.addEventListener("change", () => save({ name: name.value === ch.name ? "" : name.value }));
      const watch = ch.number && on && s.enabled && s.mode !== "guide" ? el("a", { className: "btn", href: `/watch.html?ch=${encodeURIComponent(ch.number)}`, textContent: "Watch" }) : el("span", {});
      tr.append(el("td", {}, box), el("td", {}, num), el("td", {}, name), el("td", { textContent: ch.group || "" }), el("td", {}, watch));
      body.append(tr);
    }
    table.append(body);
    d.append(table);
    return d;
  }

  // ---- the form
  function openForm(s) {
    state.editing = s ? s.id : null;
    $("form").hidden = false;
    $("form-title").textContent = s ? `Change ${s.name}` : "Add a source";
    $("f-type").value = s ? s.type : "stream";
    $("f-type").disabled = !!s;                       // what kind of source it is can't change: make a new one
    $("f-name").value = s ? s.name : "";
    $("f-url").value = s ? s.url : "";
    $("f-mode").value = s ? s.mode : "convert";
    $("f-base").value = s && s.base ? s.base : "";
    $("f-result").textContent = "";
    typeChanged();
    $("f-name").focus();
    $("form").scrollIntoView({ block: "nearest" });
  }
  function typeChanged() {
    const t = $("f-type").value;
    $("f-url-hint").textContent = HINTS[t];
    $("f-url").placeholder = t === "stream" ? "rtsp://user:password@192.168.1.60:554/stream1" : t === "m3u" ? "http://example.com/channels.m3u" : "http://192.168.1.20";
    $("f-base-field").hidden = t === "hdhomerun";
    $("f-mode-guide").hidden = $("f-mode-guide").disabled = t !== "hdhomerun";
    if (t !== "hdhomerun" && $("f-mode").value === "guide") $("f-mode").value = "convert";
    if (!state.editing) $("f-mode").value = t === "stream" ? "convert" : "convert";
  }
  $("f-type").addEventListener("change", typeChanged);
  $("new-btn").addEventListener("click", () => openForm(null));
  $("f-cancel").addEventListener("click", () => { $("form").hidden = true; });
  $("f-test").addEventListener("click", () => run(async () => {
    $("f-result").textContent = "Looking at it for a few seconds…";
    const url = $("f-url").value.trim();
    const t = $("f-type").value;
    const probeUrl = t === "stream" ? url : null;
    if (!probeUrl) { $("f-result").textContent = "A playlist or tuner is checked when it is saved: its channels are read then."; return; }
    const r = await call("/api/iptv/test", { url: probeUrl });
    $("f-result").textContent = r.ok ? `It works: ${[r.video, r.audio].filter(Boolean).join(", ") || "a stream"}.` : `It did not answer: ${r.error}`;
  }));
  $("form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      $("f-save").disabled = true;
      $("f-result").textContent = "Saving…";
      try {
        const base = $("f-base").value === "" ? null : Number($("f-base").value);
        const body = { name: $("f-name").value, url: $("f-url").value, mode: $("f-mode").value, base };
        if (state.editing) body.id = state.editing; else body.type = $("f-type").value;
        await call("/api/iptv/save", body);
        $("form").hidden = true;
        await load();
        say("Saved. Its channels are in the lineup, the guide and the player.", "ok");
      } finally {
        $("f-save").disabled = false;
        $("f-result").textContent = "";
      }
    });
  });

  run(load);
  setInterval(() => {
    const typing = document.activeElement && document.activeElement.closest && document.activeElement.closest("#sources, #form");
    if (document.visibilityState === "visible" && !typing) load().catch(() => {});       // never while something is being typed
  }, 30000);
})();
