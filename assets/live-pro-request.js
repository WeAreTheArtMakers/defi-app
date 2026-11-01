/* "Request Live Pro" form on /live-pro/ (#request). The form stays hidden, and the mailto line below it stays, unless
   GET <app>/api/public/live-pro-requests/options answers (the server's LIVE_PRO_REQUESTS_ENABLED switch).
   Sent as text/plain JSON (no CORS preflight). Cloudflare Turnstile is loaded only when the server returns a site key.
   No cookies, no tracking. Same pattern as assets/free-report.js. */
(function () {
  "use strict";
  var box = document.getElementById("live-pro-request");
  if (!box || !window.fetch || !window.JSON) return;
  var api = box.getAttribute("data-api");
  var form = box.querySelector("form");
  var msg = box.querySelector(".frq-msg");
  var btn = form.querySelector("button[type=submit]");
  var siteKey = null;
  var widget = null;
  var SUPPORT = "admin@wearetheartmakers.com";

  function show(text, kind) {
    msg.textContent = text || "";
    msg.className = "frq-msg" + (kind ? " " + kind : "");
  }

  function consent(text, privacyUrl) {
    var span = box.querySelector(".frq-consent");
    var cut = text.indexOf("Privacy policy:");
    span.textContent = (cut >= 0 ? text.slice(0, cut) : text + " ");
    if (privacyUrl && /^https:\/\//.test(privacyUrl)) {
      var a = document.createElement("a");
      a.href = privacyUrl;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "Privacy policy";
      span.appendChild(a);
    }
  }

  function loadTurnstile(key) {
    siteKey = key;
    window.__watamLiveProTs = function () {
      widget = window.turnstile.render(box.querySelector(".frq-ts"), { sitekey: key, theme: "auto", action: "live-pro-request" });
    };
    var s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__watamLiveProTs&render=explicit";
    s.async = true;
    document.head.appendChild(s);
  }

  function invalid(field, text) {
    field.setAttribute("aria-invalid", "true");
    field.focus();
    show(text, "err");
  }

  fetch(api + "/api/public/live-pro-requests/options", { credentials: "omit" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (o) {
      if (!o || typeof o.consent_text !== "string") return;
      consent(o.consent_text, o.privacy_url);
      if (o.turnstile_site_key) loadTurnstile(o.turnstile_site_key);
      form.hidden = false;
    })
    .catch(function () { /* the form stays hidden; the mailto line remains */ });

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var f = form.elements;
    Array.prototype.forEach.call(f, function (el) { if (el.removeAttribute) el.removeAttribute("aria-invalid"); });
    var email = f.email.value.trim();
    if (!email || !f.email.checkValidity()) { invalid(f.email, "Enter a valid work email address."); return; }
    if (f.tokens.value.length > 500) { invalid(f.tokens, "Tokens to watch: at most 500 characters."); return; }
    if (f.message.value.length > 1000) { invalid(f.message, "Message: at most 1,000 characters."); return; }
    if (!f.consent.checked) { invalid(f.consent, "Please tick the consent box."); return; }
    var token = "";
    if (siteKey) {
      token = window.turnstile && widget !== null ? window.turnstile.getResponse(widget) : "";
      if (!token) { show("Please complete the check above the button.", "err"); return; }
    }
    var body = {
      email: email,
      name: f.name.value.trim(),
      company: f.company.value.trim(),
      role: f.role.value,
      tokens: f.tokens.value.trim(),
      message: f.message.value.trim(),
      consent: true,
      website: f.website.value,
      turnstile_token: token
    };
    btn.disabled = true;
    show("Sending\u2026");
    fetch(api + "/api/public/live-pro-requests", {
      method: "POST", credentials: "omit", headers: { "Content-Type": "text/plain;charset=UTF-8" }, body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, j: j }; }, function () { return { ok: r.ok, j: null }; });
      })
      .then(function (x) {
        if (x.ok && x.j && x.j.received) {
          form.reset();
          show(x.j.message, "ok");
        } else {
          var m = x.j && x.j.error && x.j.error.message;
          show(m || ("Something went wrong. Please write to " + SUPPORT + "."), "err");
        }
      })
      .catch(function () { show("The request could not be sent. Please try again in a minute or write to " + SUPPORT + ".", "err"); })
      .then(function () {
        btn.disabled = false;
        if (siteKey && window.turnstile && widget !== null) window.turnstile.reset(widget);
      });
  });
})();
