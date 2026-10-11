// Shared by the Stations, Station and Schedule pages: a navigation bar, a fetch helper and DOM helpers.
// Names and titles come from your disk and from FieldStation42, so they are only ever put on a page as text.
(function () {
  "use strict";

  const LINKS = [
    ["dashboard", "Dashboard", "/"],
    ["stations", "Stations", "/stations.html"],
    ["iptv", "Sources", "/iptv.html"],
    ["schedule", "Schedule", "/schedule.html"],
    ["files", "Lists & files", "/files.html"],
    ["watch", "Watch", "/watch.html"],
    ["status", "Status", "/status.html"],
  ];

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const k of kids) if (k != null) node.append(k);
    return node;
  }

  // The same bar on every page. The FieldStation42 console opens in its own tab, so this tab stays where it is.
  function nav(active) {
    const bar = el("nav", { className: "topnav", ariaLabel: "Sections" });
    bar.append(el("a", { href: "/", className: "brand", textContent: "Station Wrapper 43" }));
    for (const [key, label, href] of LINKS) {
      const a = el("a", { href, textContent: label });
      if (key === active) a.setAttribute("aria-current", "page");
      bar.append(a);
    }
    const adv = el("a", {
      href: `${location.protocol}//${location.hostname}:4242/`, target: "_blank", rel: "noopener",
      textContent: "FieldStation42 console (advanced) ↗", className: "adv",
    });
    bar.append(adv);
    const host = document.getElementById("dash") || document.body;
    host.prepend(bar);
    account(bar);
  }

  // The login's state in the bar: a warning while no password is set, and Log out / Password once logged in.
  async function account(bar) {
    try {
      const st = await (await fetch("/api/auth/status")).json();
      if (!st.login) return;
      if (st.setup_needed) {
        bar.insertBefore(el("a", { href: "/login.html", className: "warn", textContent: "⚠ No password set: set one" }), bar.querySelector(".adv"));
      } else if (st.authenticated) {
        const out = el("a", { href: "#", textContent: "Log out" });
        out.addEventListener("click", async (ev) => {
          ev.preventDefault();
          await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
          location.assign("/login.html");
        });
        bar.insertBefore(el("a", { href: "/login.html?change=1", textContent: "Password" }), bar.querySelector(".adv"));
        bar.insertBefore(out, bar.querySelector(".adv"));
      }
    } catch (e) { /* the bar still works without it */ }
  }

  async function call(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    let res, data;
    // The dashboard restarts for a few seconds when a station is added or removed. Looking things up (GET) is
    // safe to repeat, so wait it out instead of showing an error; a change (POST) is never repeated blindly.
    const tries = body === undefined ? 8 : 1;
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
    if (res.status === 401) {                      // not logged in (or the session ended): go and log in, then come back
      location.assign("/login.html?next=" + encodeURIComponent(location.pathname + location.search));
      throw new Error("you need to log in");
    }
    if (!res.ok) throw new Error(data.error || `that failed (${res.status})`);
    return data;
  }

  function fmtWhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function fmtTime(iso) {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const hourLabel = (h) => `${String(h % 24).padStart(2, "0")}:00`;

  // One click: a working channel from what there is (the biggest list, or the first series in your media), built for a week.
  // Returns {button, note}: put them where they should go. onDone() runs when the build has finished.
  function quickStart(onDone) {
    const button = el("button", { type: "button", className: "btn primary", textContent: "Make me a channel now" });
    const note = el("span", { className: "dim", role: "status" });
    button.addEventListener("click", async () => {
      button.disabled = true;
      note.textContent = "Making a channel…";
      try {
        const made = await call("/api/stations/quickstart", {});
        note.textContent = `Making “${made.name}” (channel ${made.number}) from ${made.describe}, and building a week of it…`;
        let job = made.job;
        for (let i = 0; i < 1800 && job && job.state === "running"; i++) {
          await new Promise((r) => setTimeout(r, 1500));
          job = await call(`/api/stations/job?id=${encodeURIComponent(job.id)}`);
          note.textContent = job.message || note.textContent;
        }
        if (job && job.state === "error") throw new Error(job.message);
        note.replaceChildren(el("span", { textContent: `“${made.name}” is on air as channel ${made.number}. ` }),
          el("a", { className: "btn", href: `/watch.html?ch=${made.number}`, textContent: "Watch it" }), " ",
          el("a", { className: "btn", href: `/station.html?name=${encodeURIComponent(made.name)}`, textContent: "Change it" }));
        if (onDone) onDone(made);
      } catch (e) {
        note.textContent = e.message;
        button.disabled = false;
      }
    });
    return { button, note };
  }

  window.W = { el, nav, call, fmtWhen, fmtTime, hourLabel, quickStart };
})();
