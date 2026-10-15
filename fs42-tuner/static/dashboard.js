(function () {
  "use strict";

  const REFRESH_MS = 10 * 1000;
  // Thumbnails are cached in the browser for a minute, matching the server's
  // snapshot reuse, so a refresh doesn't re-request every frame.
  const THUMB_BUCKET_MS = 20 * 1000;          // a watched channel's picture is asked for again this often
  const GUIDE_BUCKET_MS = 10 * 1000;

  const statsEl = document.getElementById("stats");
  const channelsEl = document.getElementById("channels");
  const statusEl = document.getElementById("status-line");
  const consoleLink = document.getElementById("console-link");
  if (consoleLink) consoleLink.href = `${location.protocol}//${location.hostname}:4242/`;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtUptime(sec) {
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }

  function fmtClock(sec) {
    sec = Math.max(0, Math.round(sec));
    const m = Math.floor(sec / 60);
    const s = String(sec % 60).padStart(2, "0");
    return `${m}:${s}`;
  }

  function fmtAge(sec) {
    if (sec == null) return "never";
    if (sec < 90) return `${sec}s ago`;
    return `${Math.round(sec / 60)}m ago`;
  }

  function card(label, value, sub, warn) {
    return `<div class="stat${warn ? " warn" : ""}">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value">${esc(value)}</div>
      <div class="stat-sub">${esc(sub || "")}</div>
    </div>`;
  }

  function renderStats(sys) {
    const cpuPct = sys.cpu.count ? sys.cpu.load1 / sys.cpu.count : 0;
    const mem = sys.memory;
    const guide = sys.guide || {};
    const guideAge = guide.age_seconds;
    statsEl.innerHTML = [
      card("Channels on air", `${sys.schedule.channels_on_air}`,
           sys.schedule.db_modified ? `schedule written ${sys.schedule.db_modified.replace("T", " ").slice(0, 16)}` : "no schedule db",
           sys.schedule.channels_on_air === 0),
      card("Encoders", `${sys.encoders.in_use} / ${sys.encoders.max}`,
           `${sys.encoders.started_total} started since boot`,
           sys.encoders.in_use >= sys.encoders.max),
      card("Viewers", `${sys.viewers.in_use} / ${sys.viewers.max}`,
           `${sys.viewers.served_total} served, ${sys.viewers.refused_total} refused`,
           sys.viewers.refused_total > 0),
      card("CPU load", sys.cpu.load1.toFixed(2),
           `${sys.cpu.count} cores, 5m ${sys.cpu.load5.toFixed(2)}`,
           cpuPct > 0.9),
      card("Memory", mem ? `${mem.available_mb} MB free` : "unknown",
           mem ? `of ${mem.total_mb} MB` : "", mem && mem.available_mb < 512),
      card("Guide", fmtAge(guideAge),
           guide.fs42 ? `${guide.fs42.channels} channels, ${guide.fs42.programmes} programmes` : "",
           guideAge == null),
      readAheadCard(sys.prefetch),
      bigFilesCard(sys.prefetch && sys.prefetch.stream),
      joinsCard(sys.joins),
      card("Uptime", fmtUptime(sys.uptime_seconds), "", false),
    ].join("");
  }

  // the read-ahead: copies of the next break or episode made before they are due (prefetch.py)
  function readAheadCard(p) {
    if (!p) return "";
    if (!p.enabled) return card("Read-ahead", "off", p.why_not || "", false);
    const shares = Object.entries(p.shares || {}).map(([k, v]) => `${k.split("/").pop()}: ${v.mb_per_s != null ? v.mb_per_s + " MB/s" : "measuring"}, ${v.delay_s != null ? v.delay_s + "s to open" : ""}`).join("; ");
    return card("Read-ahead", `${p.cache.used_mb} / ${p.cache.max_mb} MB`,
                `${p.cache.files} files in ${p.ram ? "RAM" : "disk"}` + (p.copying ? `, copying ${p.copying}` : "") + (shares ? ` · ${shares}` : ""), false);
  }

  // the joins between pieces: how long a viewer waited at each, over the last hour (tuner.py: joins_report)
  function joinsCard(j) {
    if (!j || !j.summary || !j.summary.count) return card("Joins", "none yet", "how late each piece's picture comes at a cut is recorded here", false);
    const s = j.summary;
    const by = Object.entries(s.by || {}).map(([k, v]) => `${k} ${v.median_s}s (${v.count})`).join(", ");
    return card("Joins", `${s.median_s}s median`, `${s.count} in the last hour, slowest ${s.worst_s}s` + (by ? ` · ${by}` : ""),
                s.p90_s > 1.0);
  }

  function renderJoins(j) {
    const el = document.getElementById("joins");
    if (!el) return;
    const rows = (j && j.recent) || [];
    el.hidden = !rows.length;
    if (!rows.length) return;
    el.innerHTML = `<details><summary>Recent joins (${rows.length})</summary><table class="joins"><thead><tr>
      <th>Time</th><th>Channel</th><th>From</th><th>To</th><th>Late by</th><th>First picture</th><th>How</th><th>File</th></tr></thead><tbody>` +
      rows.map((r) => {
        const waited = r.late_s != null ? Math.max(0, r.late_s) : r.first_picture_s;
        return `<tr class="${waited > 1 ? "slow" : ""}"><td>${esc(r.at)}</td><td>${esc(r.channel)}</td><td>${esc(r.from)}</td><td>${esc(r.to)}</td>
          <td>${waited.toFixed(2)} s</td><td>${r.first_picture_s.toFixed(2)} s</td><td>${esc((r.flags || []).join(", ") || "plain")}</td><td>${esc(r.file)}</td></tr>`;
      }).join("") + "</tbody></table></details>";
  }

  // big files on a share, read through the local read-ahead server (streaming.py): what each is holding ahead of where it plays
  function bigFilesCard(s) {
    if (!s || !s.enabled) return "";
    const files = s.files || [];
    const short = files.filter((f) => f.short_mb > 0);
    const detail = files.slice(0, 4).map((f) => {
      const note = f.streams_live ? "streams live" : `${f.have_mb} of ${f.wanted_mb} MB held`;
      return `${esc(f.file)}: ${f.state}, ${note}` + (f.short_mb > 0 ? `, short by ${f.short_mb} MB` : "");
    }).join("; ");
    return card("Big files", files.length ? `${s.ram_mb} MB RAM + ${s.disk_mb} MB disk` : "nothing playing",
                files.length ? detail : `files of ${s.min_mb} MB or more on a share are read ahead`, short.length > 0);
  }

  function renderChannels(rows) {
    const bucket = Math.floor(Date.now() / THUMB_BUCKET_MS);
    // Keep the picture elements across redraws: replacing them blanks the picture while the new one loads, which flickers.
    const kept = new Map([...channelsEl.querySelectorAll("img.thumb-img[data-ch]")].map((i) => [i.dataset.ch, i]));
    channelsEl.innerHTML = rows.map((r) => {
      const n = esc(r.channel);
      const now = r.now;
      // What the tile shows. A channel that is broken, off air or has nothing scheduled shows the test pattern with its
      // reason. The guide channel is a picture drawn here (no video is read), refreshed every few seconds. Any other
      // channel gets a snapshot only while somebody is watching it: a snapshot opens a video, which on a network share
      // is real work, so a channel nobody is watching just says "idle".
      let thumb;
      if (r.pattern) {
        thumb = `<div class="thumb-pattern"><span>${esc(r.pattern)}</span></div>`;
      } else if (now && now.content_type === "guide") {
        thumb = `<img class="thumb-img" alt="" data-ch="${n}" src="/thumb/${n}.jpg?b=${Math.floor(Date.now() / GUIDE_BUCKET_MS)}">`;
      } else if (r.external && r.external.mode === "relay") {
        thumb = '<div class="thumb-off">passing on</div>';       // copied untouched: nothing here watches or acts on it
      } else if (!r.encoding) {
        thumb = '<div class="thumb-off">idle</div>';             // its encoder is not running: our own channels and converted sources alike
      } else if (r.external || (now && !now.is_stream)) {
        // a watched station, or a watched converted source (a still from the few seconds of it this container keeps)
        thumb = `<img class="thumb-img" alt="" data-ch="${n}" data-fallback="${r.external ? "\u25B6 live stream" : "\u25B6 on air"}" src="/thumb/${n}.jpg?b=${bucket}">`;
      } else {
        thumb = '<div class="thumb-off">\u25B6 live stream</div>';
      }
      const pct = now && now.duration ? Math.min(100, (now.elapsed / now.duration) * 100) : 0;
      const badges = [
        r.on_air ? '<span class="badge live">on air</span>' : '<span class="badge off">off air</span>',
        r.external ? `<span class="badge ext" title="${esc(r.external.source)}">IPTV external source</span>` : "",
        r.encoding ? '<span class="badge enc">encoding</span>' : "",
        r.viewers ? `<span class="badge view">${r.viewers} watching</span>` : "",
      ].join("");
      const body = now
        ? `<div class="now">${esc(now.title)}</div>
           <div class="file">${esc(now.file)} <span class="kind">${esc(now.content_type === "bump" ? "mid card" : now.content_type)}</span></div>` +
          (r.external ? `<div class="time">from ${esc(r.external.source)}</div>`
            : `<div class="bar"><span style="width:${pct.toFixed(1)}%"></span></div>
           <div class="time">${fmtClock(now.elapsed)} / ${fmtClock(now.duration)}</div>`)
        : `<div class="now dim">Nothing scheduled</div>`;
      // the channel's logo in the corner of its picture: our own channels draw one from their name (or use the one set on the
      // station); an external source's channel shows the one its source gives it, and none when it has none
      const logoSrc = r.external ? (r.external.logo || "") : `/logo/${n}.png`;
      const logo = logoSrc ? `<img class="chan-logo" alt="" loading="lazy" referrerpolicy="no-referrer" src="${esc(logoSrc)}">` : "";
      return `<a class="card-chan" href="/watch.html?ch=${n}">
        <div class="thumb">${thumb}${logo}</div>
        <div class="meta">
          <div class="head"><span class="num">${n}</span><span class="name">${esc(r.station)}</span></div>
          <div class="badges">${badges}</div>
          ${body}
        </div>
      </a>`;
    }).join("");

    channelsEl.querySelectorAll("img.thumb-img[data-ch]").forEach((fresh) => {
      const was = kept.get(fresh.dataset.ch);
      if (!was) return;                                                // first time: let it load as it is
      const src = fresh.getAttribute("src");
      fresh.replaceWith(was);                                          // the picture on screen stays until the next one is ready
      if (was.getAttribute("src") !== src) {
        const next = new Image();
        next.onload = () => { was.src = src; };
        next.onerror = () => { was.src = src; };                       // let its own error handling show "no preview"
        next.src = src;
      }
    });
    channelsEl.querySelectorAll(".chan-logo").forEach((img) => img.addEventListener("error", () => img.remove()));
    channelsEl.querySelectorAll(".thumb-img").forEach((img) => {
      if (img.dataset.bound) return;                                   // a kept picture already has its handler
      img.dataset.bound = "1";
      img.addEventListener("error", () => {
        const off = document.createElement("div");
        off.className = "thumb-off";
        off.textContent = img.dataset.fallback || "no preview";
        img.replaceWith(off);
      });
    });
  }

  async function refresh() {
    try {
      const [sys, chans] = await Promise.all([
        fetch("/system.json").then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
        fetch("/channels.json").then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      ]);
      renderStats(sys);
      renderJoins(sys.joins);
      renderChannels(chans);
      statusEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
      statusEl.classList.remove("error");
    } catch (e) {
      statusEl.textContent = "Lost contact with fs42-tuner, retrying";
      statusEl.classList.add("error");
    }
  }

  // ---- getting started: what is done and what is next, until the first channel is on air
  const setupEl = document.getElementById("setup");
  async function loadSetup() {
    if (setupEl.dataset.busy === "1") return;               // a channel is being made: leave its progress on screen
    let dismissed = false;
    try { dismissed = localStorage.getItem("setup-dismissed") === "1"; } catch (e) { /* not remembered */ }
    try {
      const [sys, lib, stations] = await Promise.all([
        fetch("/api/system/status").then((r) => r.json()),
        fetch("/library.json").then((r) => r.json()),
        fetch("/api/stations").then((r) => r.json()),
      ]);
      const rows = stations.stations || stations.rows || stations || [];
      const list = Array.isArray(rows) ? rows : [];
      const real = list.filter((s) => s.type !== "guide");
      const hasLists = (lib.tags || []).some((t) => t.videos > 0 && !["bump", "commercials"].includes(t.tag));
      const steps = [
        { done: !!(sys.media && sys.media.present), text: "Your media folder has videos in it.", href: "/files.html", go: "Lists & files" },
        { done: hasLists, text: "You have a list with videos in it (or a series, season or folder you can pick directly).", href: "/files.html", go: "Lists & files" },
        { done: real.length > 0, text: "You have a station.", href: "/station.html", go: "New station" },
        { done: real.some((s) => s.on_air), text: "A station is built and on air (Save and build on the station's page).", href: "/stations.html", go: "Stations" },
        { done: null, text: `Add it to Jellyfin or another IPTV app: a tuner of type HDHomeRun at ${location.host} and an XMLTV guide at http://${location.host}/xmltv.xml. The Status page checks everything else.`, href: "/status.html", go: "Status" },
      ];
      const complete = steps.slice(0, 4).every((s) => s.done);
      setupEl.hidden = complete || dismissed;
      if (setupEl.hidden) return;
      const next = steps.findIndex((s) => s.done === false);
      setupEl.replaceChildren();
      const head = document.createElement("div");
      head.className = "view-head";
      head.innerHTML = "<h2>Getting started</h2>";
      const hide = document.createElement("button");
      hide.type = "button";
      hide.className = "btn";
      hide.textContent = "Hide";
      hide.addEventListener("click", () => { try { localStorage.setItem("setup-dismissed", "1"); } catch (e) { /* not remembered */ } setupEl.hidden = true; });
      head.append(hide);
      setupEl.append(head);
      steps.forEach((s, i) => {
        const p = document.createElement("p");
        p.className = "state " + (s.done ? "ok" : i === next ? "" : "dim");
        p.textContent = `${s.done ? "\u2714" : i === next ? "\u25B6" : "\u25CB"} ${s.text} `;
        if (!s.done && (i === next || s.done === null)) {
          const a = document.createElement("a");
          a.className = "btn" + (i === next ? " primary" : "");
          a.href = s.href;
          a.textContent = s.go;
          p.append(a);
        }
        setupEl.append(p);
      });
      if (!steps[2].done) {                                   // no station yet: a way to get one without choosing anything
        const qs = W.quickStart();                          // its "on air: Watch it" message stays until the page is reloaded
        qs.button.addEventListener("click", () => { setupEl.dataset.busy = "1"; });
        const row = document.createElement("p");
        row.append(qs.button, " ", qs.note);
        setupEl.append(row);
      }
    } catch (e) {
      setupEl.hidden = true;
    }
  }
  loadSetup();
  setInterval(loadSetup, 30 * 1000);

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
