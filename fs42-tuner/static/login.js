(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);

  // only ever go back to a page on this server: the address is resolved against this origin and used only when it stays on it
  function nextUrl() {
    let u;
    try { u = new URL(params.get("next") || "/", location.origin); } catch (e) { return "/"; }
    if (u.origin !== location.origin || u.pathname.startsWith("/login")) return "/";
    return u.pathname + u.search + u.hash;
  }

  function say(text, kind) {
    $("msg").textContent = text || "";
    $("msg").className = "msg" + (kind ? " " + kind : "");
  }

  async function post(path, body) {
    let res, data;
    try {
      res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      data = await res.json();
    } catch (e) {
      throw new Error("could not reach the dashboard");
    }
    if (!res.ok) throw new Error(data.error || `that failed (${res.status})`);
    return data;
  }
  const run = (fn) => fn().catch((e) => say(e.message, "err"));

  $("login-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      await post("/api/auth/login", { password: $("l-pw").value });
      location.assign(nextUrl());
    });
  });

  $("setup-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      if ($("s-pw").value !== $("s-pw2").value) throw new Error("the two passwords are not the same");
      await post("/api/auth/setup", { password: $("s-pw").value });
      location.assign(nextUrl());
    });
  });

  $("change-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run(async () => {
      if ($("c-new").value !== $("c-new2").value) throw new Error("the two new passwords are not the same");
      await post("/api/auth/password", { current: $("c-old").value, new: $("c-new").value });
      say("Password changed. Everyone else has been logged out.", "ok");
      $("change-form").reset();
    });
  });

  run(async () => {
    const res = await fetch("/api/auth/status");
    const st = await res.json();
    if (!st.login) { $("off").hidden = false; return; }
    if (st.setup_needed) { $("setup-form").hidden = false; $("s-pw").focus(); return; }
    if (st.authenticated && params.get("change")) { $("change-form").hidden = false; $("c-old").focus(); return; }
    if (st.authenticated) { location.replace(nextUrl()); return; }
    $("login-form").hidden = false;
    $("l-pw").focus();
  });
})();
