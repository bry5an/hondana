// /login: the admin password form. The server sends a page here (303) when it needs the
// password; ?next= is where to go back to once signed in.
"use strict";

(function () {
  const form = document.getElementById("login-form");
  const status = document.getElementById("login-status");
  const next = new URLSearchParams(location.search).get("next") || "/admin";
  // Only ever go back to a page on this server.
  const target = next.startsWith("/") && !next.startsWith("//") ? next : "/admin";

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("login-submit");
    btn.disabled = true;
    btn.textContent = "Signing in…";
    status.className = "set-status";
    status.textContent = "";
    try {
      await postJson("/api/auth/login", { password: form.password.value });
      location.replace(target);
    } catch (err) {
      status.className = "set-status err";
      status.textContent = err.message || "That didn't work.";
      btn.disabled = false;
      btn.textContent = "Sign in";
      form.password.select();
    }
  });
  document.getElementById("login-password").focus();
})();
