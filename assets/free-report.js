/* "Get the free 7-day report" form on the token pages (scripts/build_token_pages.py).
   The form stays hidden unless GET <app>/api/public/free-reports/options answers (the server's FREE_REPORTS_ENABLED switch).
   The request is sent as text/plain JSON (no CORS preflight); the report link is emailed, never shown here.
   Cloudflare Turnstile is loaded only when the server returns a site key. No cookies, no tracking. */
(function () {
  "use strict";
  var box = document.getElementById("free-report");
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
    window.__watamFreeReportTs = function () {
      widget = window.turnstile.render(box.querySelector(".frq-ts"), { sitekey: key, theme: "auto", action: "free-report" });
    };
    var s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__watamFreeReportTs&render=explicit";
    s.async = true;
    document.head.appendChild(s);
  }

  fetch(api + "/api/public/free-reports/options", { credentials: "omit" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (o) {
      if (!o || typeof o.consent_text !== "string") return;
      consent(o.consent_text, o.privacy_url);
      if (o.turnstile_site_key) loadTurnstile(o.turnstile_site_key);
      box.hidden = false;
    })
    .catch(function () { /* stays hidden */ });

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var email = form.elements.email.value.trim();
    if (!email || !form.elements.email.checkValidity()) { show("Enter a valid work email address.", "err"); return; }
    if (!form.elements.consent.checked) { show("Please tick the consent box.", "err"); return; }
    var token = "";
    if (siteKey) {
      token = window.turnstile && widget !== null ? window.turnstile.getResponse(widget) : "";
      if (!token) { show("Please complete the check above the button.", "err"); return; }
    }
    var canonical = document.querySelector('link[rel="canonical"]');
    var body = {
      email: email,
      company: form.elements.company.value.trim(),
      consent: true,
      chain_id: parseInt(box.getAttribute("data-chain-id"), 10),
      token: box.getAttribute("data-token"),
      page: canonical ? canonical.href : location.href,
      website: form.elements.website.value,
      turnstile_token: token
    };
    btn.disabled = true;
    show("Sending…");
    fetch(api + "/api/public/free-reports", {
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
