(function () {
  "use strict";
  const { el, call, fmtWhen } = W;
  W.nav("status");
  const $ = (id) => document.getElementById(id);
  const MARK = { error: "✖", warn: "▲", ok: "✔" };
  const say = (text, kind) => { $("msg").textContent = text || ""; $("msg").className = "msg" + (kind ? " " + kind : ""); };

  const dur = (s) => (s >= 86400 ? `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h` : s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m`);

  function table(rows) {
    const t = el("table", { className: "chan-table" });
    for (const [k, v] of rows) t.append(el("tr", {}, el("th", { textContent: k }), el("td", { textContent: v == null || v === "" ? "—" : String(v) })));
    return t;
  }

  function render(r) {
    const checks = $("checks");
    checks.replaceChildren(el("h2", { textContent: "Checks" }));
    for (const c of r.checks) {
      const p = el("p", { className: "state " + (c.level === "error" ? "err" : c.level === "ok" ? "ok" : ""), textContent: `${MARK[c.level]} ${c.text} ` });
      if (c.href) p.append(el("a", { className: "btn", href: c.href, textContent: c.fix || "Fix it" }));
      checks.append(p);
    }
    const st = $("stations");
    st.replaceChildren(el("h2", { textContent: "Stations" }));
    const rows = r.stations.map((s) => [`#${s.number} ${s.name}`, s.type === "guide" ? "guide channel (drawn live)" :
      !s.scheduled_until ? "no schedule" : !s.on_air ? `off air: schedule ended ${fmtWhen(s.scheduled_until)}` :
      `on air, scheduled until ${fmtWhen(s.scheduled_until)}${s.low ? " (running low)" : ""}`]);
    st.append(rows.length ? table(rows) : el("p", { className: "dim", textContent: "No stations (or the console can't be reached)." }));
    const sys = $("system");
    const g = r.guide || {};
    sys.replaceChildren(el("h2", { textContent: "System" }), table([
      ["Version", `${r.version.wrapper || "?"} (${(r.version.commit || "?").slice(0, 7)}), built ${r.version.built || "?"}`],
      ["Inside it", `FieldStation42 ${r.version.fieldstation42 || "?"}, TA_NFOWriter ${r.version.ta_nfowriter || "?"}, ${r.ffmpeg.found ? r.ffmpeg.version : "ffmpeg NOT FOUND"}`],
      ["Running for", dur(r.uptime_seconds)],
      ["Console (:4242)", r.console.reachable ? `reachable, ${r.console.stations} stations` : `NOT reachable: ${r.console.error || ""}`],
      ["Media folder", `${r.media.path}: ${r.media.present ? "has files" : "EMPTY or missing"}`],
      ["Guide", `${g.age_seconds != null ? `built ${g.age_seconds}s ago` : "not built yet"}${g.last_build ? `, last build took ${g.last_build.seconds}s` : ""}; ` +
        Object.entries(g).filter(([k, v]) => v && typeof v === "object" && "channels" in v).map(([k, v]) => `${k}: ${v.channels} ch / ${v.programmes} programmes`).join(", ")],
      ["Sources", r.sources.length ? r.sources.map((s) => `${s.name} (${s.enabled ? s.channels + " ch" : "off"}${s.error ? ", ERROR" : ""})`).join(", ") : "none"],
      ["Streams", `${r.encoders.in_use} of ${r.encoders.max} encoders, ${r.viewers.in_use} of ${r.viewers.max} viewers`],
      ["CPU / memory", `load ${r.cpu.load1} on ${r.cpu.count} cores; ${r.memory ? r.memory.available_mb + " of " + r.memory.total_mb + " MB free" : "?"}`],
      ["Disk free", Object.entries(r.disk).map(([k, d]) => `${k} ${d.free_mb == null ? "?" : d.free_mb + " MB"}`).join(", ")],
    ]));
    const rec = $("recent");
    rec.replaceChildren(el("h2", { textContent: "Recent problems the container printed" }));
    rec.append(r.recent_problems.length ? el("pre", { className: "dim", textContent: r.recent_problems.map((x) => `${new Date(x.at * 1000).toLocaleTimeString()}  ${x.line}`).join("\n") })
      : el("p", { className: "dim", textContent: "None since it started." }));
  }

  async function load() { render(await call("/api/system/status")); }
  $("speed").addEventListener("click", async () => {
    $("speed").disabled = true;
    say("Reading part of a video from your media…", "ok");
    try {
      const r = await call("/api/system/speedtest", {});
      say(r.error ? r.error : `Read ${r.mb} MB of ${r.file} in ${r.seconds}s: ${r.mb_per_s} MB/s. ` +
        (r.mb_per_s < 20 ? "That is slow: catalog builds and seeking will be slow too (check the share's network and mount options)." : "That is fine."), r.error || r.mb_per_s < 20 ? "err" : "ok");
    } catch (e) { say(e.message, "err"); }
    $("speed").disabled = false;
  });
  load().catch((e) => say(e.message, "err"));
  setInterval(() => { if (document.visibilityState === "visible") load().catch(() => {}); }, 15000);
})();
