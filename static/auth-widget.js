/* Auth widget — shows "Signed in as foo@bar.com · Logout" in the topbar nav.
   Silently no-ops if auth is disabled or the user isn't authenticated. */
(() => {
  "use strict";
  document.addEventListener("DOMContentLoaded", async () => {
    let data;
    try {
      const res = await fetch("/auth/me", { credentials: "same-origin" });
      if (!res.ok) return;
      data = await res.json();
    } catch (_) { return; }
    if (!data || !data.authenticated) return;

    const nav = document.querySelector(".djg-nav");
    if (!nav) return;

    const wrap = document.createElement("span");
    wrap.className = "djg-auth-widget";
    wrap.innerHTML = `
      <span class="djg-auth-email" title="${data.email || ""}">${(data.email || "").split("@")[0]}</span>
      <a class="djg-auth-logout" href="/auth/logout" title="Sign out">↩</a>
    `;
    nav.appendChild(wrap);
  });
})();
