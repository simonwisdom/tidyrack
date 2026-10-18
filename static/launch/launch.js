// The feedback button (and optional PostHog analytics, off here) for a public copy; settings in config.js.
// Reads window.LAUNCH = { app, posthogKey, feedbackUrl } (set in public/launch/config.js).
// - Analytics: PostHog EU, cookieless, off if the visitor opted out on the privacy page.
// - window.launch.track(event, props): custom events; a no-op when analytics is off.
// - Feedback: any element with [data-launch-feedback] opens a small form that posts to the feedback service.
(function () {
  "use strict";
  var cfg = window.LAUNCH || {};
  var OPT_OUT = "launch-analytics-off";

  function optedOut() {
    try { return localStorage.getItem(OPT_OUT) === "1"; } catch (e) { return false; }
  }

  // --- Analytics -------------------------------------------------------------------------------
  // PostHog's own loader snippet (posthog.com/docs/libraries/js), kept in this file so the page needs no inline script.
  function loadPosthog() {
    /* eslint-disable */
    !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset identify get_distinct_id".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
    /* eslint-enable */
    window.posthog.init(cfg.posthogKey, {
      api_host: "https://eu.i.posthog.com",
      ui_host: "https://eu.posthog.com",
      cookieless_mode: "always",
      persistence: "memory",
      person_profiles: "never",
      capture_pageview: true,
      capture_pageleave: true,
      capture_exceptions: true,
      disable_session_recording: true,
      disable_surveys: true,
    });
    window.posthog.register({ app: cfg.app });
  }

  var analyticsOn = Boolean(cfg.posthogKey) && !optedOut();
  if (analyticsOn) loadPosthog();

  window.launch = {
    analyticsOn: analyticsOn,
    track: function (event, props) {
      if (analyticsOn && window.posthog) window.posthog.capture(event, props || {});
    },
    setOptOut: function (off) {
      try { off ? localStorage.setItem(OPT_OUT, "1") : localStorage.removeItem(OPT_OUT); } catch (e) {}
      if (window.posthog && window.posthog.opt_out_capturing) {
        off ? window.posthog.opt_out_capturing() : window.posthog.opt_in_capturing();
      }
    },
    optedOut: optedOut,
  };

  // --- Privacy page toggle ---------------------------------------------------------------------
  function wireToggle() {
    var box = document.getElementById("launch-analytics");
    if (!box) return;
    box.checked = !optedOut();
    box.addEventListener("change", function () {
      window.launch.setOptOut(!box.checked);
      var note = document.getElementById("launch-analytics-note");
      if (note) note.textContent = box.checked ? "Anonymous usage stats are on." : "Anonymous usage stats are off on this device.";
    });
  }

  // --- Feedback --------------------------------------------------------------------------------
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text) e.textContent = text;
    return e;
  }

  function buildDialog() {
    var d = el("dialog", { class: "lp-dialog", "aria-labelledby": "lp-fb-title" });
    var form = el("form", { method: "dialog" });
    form.appendChild(el("h2", { id: "lp-fb-title" }, "Send feedback"));
    form.appendChild(el("label", { for: "lp-fb-msg" }, "What's working, what isn't, what you'd want?"));
    var msg = el("textarea", { id: "lp-fb-msg", name: "message", rows: "5", required: "", maxlength: "4000" });
    form.appendChild(msg);
    form.appendChild(el("label", { for: "lp-fb-email" }, "Email, if you'd like a reply (optional)"));
    var email = el("input", { id: "lp-fb-email", name: "email", type: "email", maxlength: "200", autocomplete: "email" });
    form.appendChild(email);
    var hp = el("input", { class: "lp-hp", name: "website", tabindex: "-1", autocomplete: "off", "aria-hidden": "true" });
    form.appendChild(hp);
    var status = el("p", { class: "lp-status", role: "status" });
    form.appendChild(status);
    var row = el("div", { class: "lp-row" });
    var cancel = el("button", { type: "button", class: "lp-btn lp-quiet" }, "Cancel");
    var send = el("button", { type: "submit", class: "lp-btn" }, "Send");
    row.appendChild(cancel);
    row.appendChild(send);
    form.appendChild(row);
    d.appendChild(form);

    cancel.addEventListener("click", function () { d.close(); });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      status.className = "lp-status";
      status.textContent = "Sending…";
      send.disabled = true;
      fetch(cfg.feedbackUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app: cfg.app, message: msg.value, email: email.value || null, page: location.pathname, website: hp.value }),
      })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          if (!res.ok) throw new Error(res.body && res.body.error ? res.body.error : "the server refused it");
          status.textContent = "Thanks, sent.";
          window.launch.track("feedback_sent");
          msg.value = "";
          setTimeout(function () { d.close(); status.textContent = ""; }, 1200);
        })
        .catch(function (err) {
          status.className = "lp-status lp-error";
          status.textContent = "Not sent: " + err.message + ".";
        })
        .finally(function () { send.disabled = false; });
    });
    document.body.appendChild(d);
    return d;
  }

  function wireFeedback() {
    var triggers = document.querySelectorAll("[data-launch-feedback]");
    if (!triggers.length || !cfg.feedbackUrl) return;
    var dialog;
    triggers.forEach(function (t) {
      t.addEventListener("click", function (ev) {
        ev.preventDefault();
        dialog = dialog || buildDialog();
        dialog.showModal();
      });
    });
  }

  function ready() { wireToggle(); wireFeedback(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
  else ready();
})();
