(function () {
  "use strict";

  const LAST_CHANNEL_KEY = "fs42.lastChannel";
  const GUIDE_REFRESH_MS = 5 * 60 * 1000;
  const BANNER_REFRESH_MS = 15 * 1000;
  const BANNER_SHOW_MS = 5000;
  const RETRY_MS = 3000;

  const video = document.getElementById("video");
  const bannerEl = document.getElementById("banner");
  const bannerNum = document.getElementById("banner-num");
  const bannerName = document.getElementById("banner-name");
  const bannerTitle = document.getElementById("banner-title");
  const toastEl = document.getElementById("toast");
  const chanDisplay = document.getElementById("chan-display");
  const chanInput = document.getElementById("chan-input");
  const chanEntry = document.getElementById("chan-entry");
  const guideEl = document.getElementById("guide");
  const guideGrid = document.getElementById("guide-grid");
  const muteBtn = document.getElementById("mute-btn");

  let channels = [];     // [{number, name}] sorted by channel number
  let programmes = {};   // number -> [{start, stop, title}] sorted by start
  let current = null;    // currently tuned channel number, as a string

  // ---- captions ------------------------------------------------------------------------------------------
  // The server says which file a channel is airing and where in it the channel is now, and hands over that file's
  // captions as WebVTT. The page works out which line belongs on screen from the clock, so nothing is added to the
  // stream itself (it is not selectable in other players: see the README). Off by default; the choice of language is
  // remembered, so captions stay on from one episode to the next.
  const CC_PREF_KEY = "fs42.captions";          // "off" or a language code ("" for a track with none)
  const CC_NUDGE_KEY = "fs42.captionsNudge";    // seconds: how far behind the clock the picture runs for this viewer
  const ccBtn = document.getElementById("cc-btn");
  const ccSync = document.getElementById("cc-sync");
  const ccBox = document.getElementById("captions");
  const cc = { eid: null, tracks: [], track: -1, cues: [], skew: 0, pos: 0, at: 0, nudge: 2, timer: null, poll: null, token: 0,
               inBreak: false, segEnd: 0, polling: false };
  try { cc.nudge = Number(localStorage.getItem(CC_NUDGE_KEY) ?? 2) || 0; } catch (e) { /* keep the default */ }

  function ccPref() {      // ?cc=on (or a language code, or off) in the address overrides what was remembered
    const asked = new URLSearchParams(location.search).get("cc");
    if (asked !== null && !cc.chosen) return asked === "on" ? "" : asked || "off";
    try { return localStorage.getItem(CC_PREF_KEY) || "off"; } catch (e) { return "off"; }
  }
  function ccSetPref(v) { try { localStorage.setItem(CC_PREF_KEY, v); } catch (e) { /* not remembered */ } }

  function parseVtt(text) {
    const secs = (s) => { const p = s.replace(",", ".").split(":").map(Number); return p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1]; };
    const cues = [];
    for (const block of text.replace(/\r/g, "").split(/\n\n+/)) {
      const lines = block.split("\n");
      const i = lines.findIndex((l) => l.includes("-->"));
      if (i < 0) continue;
      const m = lines[i].match(/([\d:.,]+)\s*-->\s*([\d:.,]+)/);
      if (!m) continue;
      const body = lines.slice(i + 1).join("\n").replace(/<[^>]+>/g, "").replace(/\{\\[^}]*\}/g, "").replace(/&(amp|lt|gt|nbsp);/g, (m, e) => ({ amp: "&", lt: "<", gt: ">", nbsp: " " })[e]).trim();   // one pass: no entity is decoded twice
      if (body) cues.push({ from: secs(m[1]), to: secs(m[2]), text: body });
    }
    return cues.sort((a, b) => a.from - b.from);
  }

  function ccPosition() {      // where in the file the picture is now
    return cc.pos + (Date.now() / 1000 + cc.skew - cc.at) - cc.nudge;
  }

  function ccShow() {
    if (cc.track < 0 || !cc.cues.length || cc.inBreak) { ccBox.classList.add("hidden"); return; }
    const t = ccPosition();
    const lines = cc.cues.filter((c) => t >= c.from && t < c.to).map((c) => c.text);
    ccBox.replaceChildren();
    for (const l of lines) { const s = document.createElement("span"); s.textContent = l; ccBox.append(s, document.createElement("br")); }
    ccBox.classList.toggle("hidden", !lines.length);
  }

  async function ccLoad(trackIndex, token) {
    const t = cc.tracks[trackIndex];
    cc.cues = [];
    if (!t) return;
    try {
      const res = await fetch(`/captions/${encodeURIComponent(current)}/${cc.eid}/${encodeURIComponent(t.id)}.vtt`);
      if (!res.ok) return;
      const cues = parseVtt(await res.text());
      if (token === cc.token) cc.cues = cues;
    } catch (e) { /* captions are a nicety: the picture carries on */ }
  }

  function ccRender() {
    ccBtn.classList.toggle("hidden", !cc.tracks.length);
    ccBtn.classList.toggle("on", cc.track >= 0);
    ccBtn.textContent = cc.track >= 0 ? "CC: " + cc.tracks[cc.track].label : "CC";
    ccSync.classList.toggle("hidden", cc.track < 0);
    document.getElementById("cc-offset").textContent = `${cc.nudge.toFixed(1)}s`;
    ccShow();
  }

  async function ccSelect(index) {          // -1 is off
    cc.chosen = true;                       // from now on the button decides, not the address
    cc.track = index;
    ccSetPref(index < 0 ? "off" : cc.tracks[index].lang);
    cc.token++;
    ccRender();
    if (index >= 0) { await ccLoad(index, cc.token); ccRender(); }
  }

  async function ccPollOnce() {
    if (!current) return;
    const num = current;
    let info;
    try { info = await (await fetch(`/captions/${encodeURIComponent(num)}.json`)).json(); } catch (e) { return; }
    if (num !== current) return;
    cc.skew = (info.now || Date.now() / 1000) - Date.now() / 1000;
    cc.pos = info.pos || 0;
    cc.at = info.now || Date.now() / 1000;
    cc.segEnd = (info.now || 0) + (info.remaining || 0);          // when this piece ends: look again just after
    cc.inBreak = info.kind === "bump" || info.kind === "commercial";
    if (cc.inBreak) { ccShow(); return; }                         // a break has no captions of its own: keep the show's choice, show nothing
    if (info.eid !== cc.eid) {              // a different file is on: look again at what it has
      cc.eid = info.eid;
      cc.tracks = info.tracks || [];
      cc.cues = [];
      cc.token++;
      const pref = ccPref();
      cc.track = pref === "off" ? -1 : cc.tracks.findIndex((t) => t.lang === pref);
      if (pref !== "off" && cc.track < 0 && cc.tracks.length) cc.track = 0;      // another language than before: better than none
      ccRender();
      if (cc.track >= 0) { await ccLoad(cc.track, cc.token); ccRender(); }
    }
  }

  function ccReset() {                      // a new channel: forget the old file
    cc.eid = null; cc.tracks = []; cc.track = -1; cc.cues = []; cc.token++;
    ccRender();
    ccPollOnce();
  }

  ccBtn.addEventListener("click", () => ccSelect(cc.track + 1 >= cc.tracks.length ? -1 : cc.track + 1));
  const nudge = (d) => { cc.nudge = Math.round((cc.nudge + d) * 10) / 10; try { localStorage.setItem(CC_NUDGE_KEY, String(cc.nudge)); } catch (e) { /* ok */ } ccRender(); };
  document.getElementById("cc-earlier").addEventListener("click", () => nudge(-0.5));
  document.getElementById("cc-later").addEventListener("click", () => nudge(0.5));
  setInterval(() => {
    ccShow();
    // the piece on air has ended (the picture runs a little behind the clock): look at what is on now, don't wait for the next poll
    if (cc.segEnd && !cc.polling && Date.now() / 1000 + cc.skew - cc.nudge > cc.segEnd + 0.3) {
      cc.polling = true;
      cc.segEnd = 0;
      ccPollOnce().finally(() => { cc.polling = false; });
    }
  }, 200);
  setInterval(ccPollOnce, 5000);
  let player = null;
  let bannerTimer = null;
  let toastTimer = null;
  let retryTimer = null;

  function sortKey(num) {
    const f = parseFloat(num);
    return Number.isNaN(f) ? Infinity : f;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.add("hidden"), 4000);
  }

  // -- data loading ---------------------------------------------------

  async function loadLineup() {
    const res = await fetch("/lineup.json");
    if (!res.ok) throw new Error(`lineup.json: ${res.status}`);
    const rows = await res.json();
    channels = rows
      .map((r) => ({ number: String(r.GuideNumber), name: r.GuideName || String(r.GuideNumber) }))
      .sort((a, b) => sortKey(a.number) - sortKey(b.number));
  }

  function parseXmltvDate(s) {
    // "YYYYMMDDHHMMSS [+-ZZZZ]" -- local-offset for fs42 blocks, +0000 for
    // anything sourced from SiliconDust. Both forms carry an explicit offset.
    const m = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\s*([+-]\d{4})?$/.exec((s || "").trim());
    if (!m) return null;
    const [, Y, Mo, D, H, Mi, S, off] = m;
    let ms = Date.UTC(+Y, +Mo - 1, +D, +H, +Mi, +S);
    if (off) {
      const sign = off[0] === "-" ? -1 : 1;
      const oh = +off.slice(1, 3);
      const om = +off.slice(3, 5);
      ms -= sign * (oh * 60 + om) * 60000;
    }
    return new Date(ms);
  }

  async function loadGuide() {
    const res = await fetch("/xmltv.xml");
    if (!res.ok) throw new Error(`xmltv.xml: ${res.status}`);
    const text = await res.text();
    const doc = new DOMParser().parseFromString(text, "application/xml");

    // Every <channel> carries its tuner channel number as a second
    // display-name (see guide.py) specifically so a consumer that only knows
    // the number, like this page, can key off it without touching ids.
    const idToNumber = {};
    doc.querySelectorAll("channel").forEach((ch) => {
      const id = ch.getAttribute("id");
      const names = Array.from(ch.querySelectorAll("display-name")).map((n) => n.textContent.trim());
      const num = names.find((n) => /^[0-9.]+$/.test(n));
      if (id && num) idToNumber[id] = num;
    });

    const byNumber = {};
    doc.querySelectorAll("programme").forEach((pr) => {
      const num = idToNumber[pr.getAttribute("channel")];
      if (!num) return;
      const start = parseXmltvDate(pr.getAttribute("start"));
      const stop = parseXmltvDate(pr.getAttribute("stop"));
      if (!start || !stop) return;
      const titleEl = pr.querySelector("title");
      (byNumber[num] = byNumber[num] || []).push({
        start, stop, title: titleEl ? titleEl.textContent : "",
      });
    });
    Object.values(byNumber).forEach((list) => list.sort((a, b) => a.start - b.start));
    programmes = byNumber;
  }

  function nowPlaying(num) {
    const list = programmes[num];
    if (!list) return null;
    const now = new Date();
    return list.find((p) => p.start <= now && now < p.stop) || null;
  }

  function upNext(num) {
    const list = programmes[num];
    if (!list) return null;
    const now = new Date();
    return list.find((p) => p.start > now) || null;
  }

  // -- guide grid -------------------------------------------------------

  function renderGuide() {
    guideGrid.innerHTML = "";
    for (const c of channels) {
      const now = nowPlaying(c.number);
      const next = upNext(c.number);
      const row = document.createElement("div");
      row.className = "guide-row" + (c.number === current ? " active" : "");
      row.innerHTML = `
        <div class="guide-chan">${escapeHtml(c.number)}<small>${escapeHtml(c.name)}</small></div>
        <div class="guide-now">${now ? escapeHtml(now.title) : "&mdash;"}</div>
        <div class="guide-next">${next ? "Next: " + escapeHtml(next.title) : ""}</div>
      `;
      row.addEventListener("click", () => {
        tune(c.number);
        setGuideOpen(false);
      });
      guideGrid.appendChild(row);
    }
  }

  function setGuideOpen(open) {
    guideEl.classList.toggle("hidden", !open);
    document.getElementById("guide-toggle").classList.toggle("active", open);
  }

  // -- banner -------------------------------------------------------------

  function updateBanner(showAndFade) {
    const chan = channels.find((c) => c.number === current);
    if (!chan) return;
    bannerNum.textContent = chan.number;
    bannerName.textContent = chan.name;
    const np = nowPlaying(chan.number);
    bannerTitle.textContent = np ? np.title : "";
    if (showAndFade) {
      bannerEl.classList.remove("hidden");
      clearTimeout(bannerTimer);
      bannerTimer = setTimeout(() => bannerEl.classList.add("hidden"), BANNER_SHOW_MS);
    }
  }

  // -- playback -------------------------------------------------------

  function destroyPlayer() {
    clearTimeout(retryTimer);
    if (player) {
      try {
        player.pause();
        player.unload();
        player.detachMediaElement();
        player.destroy();
      } catch (e) {
        // best-effort teardown -- a half-torn-down player is still better
        // than leaking the old one into the next tune()
      }
      player = null;
    }
  }

  function tune(num) {
    const chan = channels.find((c) => c.number === num);
    if (!chan) {
      showToast(`No channel ${num}`);
      return;
    }
    current = num;
    try { localStorage.setItem(LAST_CHANNEL_KEY, num); } catch (e) { /* private mode etc. */ }
    chanDisplay.textContent = num;
    chanInput.value = "";
    destroyPlayer();

    player = mpegts.createPlayer(
      { type: "mpegts", isLive: true, url: `/auto/v${encodeURIComponent(num)}` },
      {
        enableStashBuffer: false,
        liveBufferLatencyChasing: true,
        liveBufferLatencyMaxLatency: 3.0,
        liveBufferLatencyMinRemain: 1.0,
      }
    );
    player.attachMediaElement(video);
    player.on(mpegts.Events.ERROR, (type, detail) => {
      showToast("Stream error — retrying…");
      clearTimeout(retryTimer);
      retryTimer = setTimeout(() => { if (current === num) tune(num); }, RETRY_MS);
    });
    // the server closing the stream (a viewer too slow to keep up is dropped) is not an error to the player: it just
    // stops, and the picture freezes. Treat the end of a live stream as a reason to reconnect.
    if (mpegts.Events.LOADING_COMPLETE) {
      player.on(mpegts.Events.LOADING_COMPLETE, () => {
        showToast("Stream ended — reconnecting…");
        clearTimeout(retryTimer);
        retryTimer = setTimeout(() => { if (current === num) tune(num); }, RETRY_MS);
      });
    }
    stall.since = Date.now();                        // a fresh player gets time to start before the watchdog looks
    stall.last = -1;
    player.load();
    player.play().catch(() => {
      // Autoplay-with-sound can be refused before the mute button is
      // touched; the element is already muted by default so this is rare.
    });

    updateBanner(true);
    renderGuide();
    ccReset();
  }

  // The picture stopping without any error (a gap in the stream the player cannot get past) used to need a page
  // refresh. If a playing picture has not moved for STALL_MS, tune the channel again.
  const STALL_MS = 8000;
  const stall = { last: -1, since: Date.now(), tries: 0 };
  setInterval(() => {
    if (!current || !player || document.hidden) { stall.since = Date.now(); return; }
    if (video.paused) {                              // autoplay was refused: ask again rather than reconnect forever
      video.play().catch(() => {});
      stall.since = Date.now();
      return;
    }
    if (video.currentTime !== stall.last) {          // it moved
      stall.last = video.currentTime;
      stall.since = Date.now();
      stall.tries = 0;
      return;
    }
    if (Date.now() - stall.since > STALL_MS) {
      stall.tries += 1;
      showToast(stall.tries > 1 ? "Still no picture — trying again…" : "The picture stopped — reconnecting…");
      stall.since = Date.now() + Math.min(stall.tries, 4) * 2000;   // each failed try waits a little longer
      tune(current);
    }
  }, 2000);

  function channelStep(dir) {
    if (!channels.length) return;
    let idx = channels.findIndex((c) => c.number === current);
    if (idx === -1) idx = 0;
    idx = (idx + dir + channels.length) % channels.length;
    tune(channels[idx].number);
  }

  // -- wiring -------------------------------------------------------

  document.getElementById("chan-up").addEventListener("click", () => channelStep(1));
  document.getElementById("chan-down").addEventListener("click", () => channelStep(-1));
  document.getElementById("guide-toggle").addEventListener("click", () => {
    setGuideOpen(guideEl.classList.contains("hidden"));
  });
  // ---- volume: a slider, a mute button, and the arrow keys. The picture starts muted (browsers refuse sound
  // until you do something), then the first touch of any of these brings it up at the volume you left it at.
  const VOLUME_KEY = "fs42.volume";
  const volEl = document.getElementById("volume");
  function showVolume() {
    const silent = video.muted || video.volume === 0;
    muteBtn.innerHTML = silent ? "&#128263;" : video.volume < 0.34 ? "&#128264;" : video.volume < 0.67 ? "&#128265;" : "&#128266;";    // low, medium, loud: one icon each
    muteBtn.setAttribute("aria-label", silent ? "Unmute" : "Mute");
    volEl.value = String(Math.round((video.muted ? 0 : video.volume) * 100));
    volEl.style.opacity = silent ? "0.6" : "1";
  }
  function setVolume(v, remember = true) {
    video.volume = Math.min(1, Math.max(0, v));
    video.muted = video.volume === 0;
    if (remember) { try { localStorage.setItem(VOLUME_KEY, String(video.volume)); } catch (e) { /* not remembered */ } }
    showVolume();
  }
  function toggleMute() {
    if (video.muted || video.volume === 0) {
      if (video.volume === 0) video.volume = 0.5;       // it was turned all the way down: bring it back to something
      video.muted = false;
    } else {
      video.muted = true;
    }
    showVolume();
  }
  try {                                                  // the saved volume is ready for the first unmute
    const saved = Number(localStorage.getItem(VOLUME_KEY));
    if (localStorage.getItem(VOLUME_KEY) !== null && saved >= 0 && saved <= 1) video.volume = saved;
  } catch (e) { /* the default: full */ }
  showVolume();
  muteBtn.addEventListener("click", toggleMute);
  volEl.addEventListener("input", () => setVolume(Number(volEl.value) / 100));
  video.addEventListener("volumechange", showVolume);
  chanEntry.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = chanInput.value.trim();
    if (v) tune(v);
  });
  document.addEventListener("keydown", (e) => {
    if (e.target === chanInput || e.target === volEl) return;       // the slider has its own arrow keys
    if (e.key === "ArrowUp") channelStep(1);
    else if (e.key === "ArrowDown") channelStep(-1);
    else if (e.key === "ArrowRight") setVolume((video.muted ? 0 : video.volume) + 0.05);
    else if (e.key === "ArrowLeft") setVolume((video.muted ? 0 : video.volume) - 0.05);
    else if ((e.key === "m" || e.key === "M") && !e.ctrlKey && !e.metaKey) toggleMute();
    else if ((e.key === "c" || e.key === "C") && !e.ctrlKey && !e.metaKey && cc.tracks.length) ccBtn.click();
  });

  async function refreshGuide() {
    try {
      await loadGuide();
      renderGuide();
      updateBanner(false);
    } catch (e) {
      showToast("Guide unavailable");
    }
  }

  async function init() {
    if (!window.mpegts || !mpegts.isSupported()) {
      document.getElementById("screen").innerHTML =
        '<p style="padding:2em;color:#e5534b">This browser does not support MSE-based MPEG-TS playback. Try a recent desktop Chrome, Firefox or Edge.</p>';
      return;
    }
    try {
      await loadLineup();
      await loadGuide();
    } catch (e) {
      showToast("Could not load channel list");
      return;
    }
    renderGuide();

    let start = new URLSearchParams(location.search).get("ch");
    if (!start) {
      try { start = localStorage.getItem(LAST_CHANNEL_KEY); } catch (e) { /* ignore */ }
    }
    if (!start || !channels.some((c) => c.number === start)) {
      start = channels.length ? channels[0].number : null;
    }
    if (start) tune(start);

    setInterval(refreshGuide, GUIDE_REFRESH_MS);
    setInterval(() => updateBanner(false), BANNER_REFRESH_MS);
  }

  init();
})();
