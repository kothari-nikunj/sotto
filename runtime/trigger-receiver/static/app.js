/* Sotto — The Window (M3: live views on top of M2's read + careful writes).
   Vanilla JS, no build step, no dependencies.
   XSS rule: ALL data rendering goes through textContent / createElement.
   innerHTML is never used with data (not used at all, in fact).
   Write rule: NEVER optimistic — every edit re-renders from the server's
   response. The dashboard is a window, not a second brain.

   Design system: the LEDGER. Every list is ruled rows under a heavy top
   rule — serif entries, mono folio meta right-aligned. Views open with a
   thesis (Today composes an editorial status sentence from live data),
   never a stat-tile row. Motion is gated: view switches render instantly;
   the entrance stagger runs once per view per session; row exits and toasts
   are the only other movers.

   M3 surfaces: Today's docket (GET /api/calendar) + research cards
   (GET /api/research) fold into the morning memo; The Record (#record,
   GET /api/ledger) is the date-grouped action timeline. All three parse
   defensively — unknown shapes render what exists, never crash. */
(function () {
  "use strict";

  var MEMORY_TYPES = ["context", "interest", "milestone", "working_style", "relationship_change"];

  var state = {
    csrf: null,          // from /api/session; refreshed on 403 once per write
    renderSeq: 0,        // guards against stale async renders after navigation
    peopleQuery: "",     // remembered so back-nav keeps the search
    peopleFilter: "all", // all | person | company
    entered: {}          // view name -> true once its entrance stagger has run
  };

  var main = document.getElementById("view");
  var navlinks = document.getElementById("navlinks");

  var reducedMotion = false;
  try {
    reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (e) { /* older engines: assume motion is fine */ }

  /* ---------------- DOM helpers (safe by construction) ---------------- */

  function el(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function append(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined) continue;
      parent.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return parent;
  }

  function setView() {
    main.replaceChildren();
    for (var i = 0; i < arguments.length; i++) {
      if (arguments[i]) main.appendChild(arguments[i]);
    }
  }

  function button(className, label, onClick) {
    var b = el("button", className, label);
    b.type = "button";
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }

  /* The ledger: cap (mono label + right slot) over a ruled row list. */
  function ledgerCap(label, rightNode) {
    var cap = el("div", "ledger-cap");
    cap.appendChild(el("span", null, label));
    if (rightNode) cap.appendChild(rightNode);
    return cap;
  }

  function capCount(text) {
    return el("span", "cap-count", text);
  }

  /* Marks a ledger for the once-per-session entrance stagger. */
  function enterOnce(viewName, ledgerEl) {
    if (!state.entered[viewName] && !reducedMotion) {
      ledgerEl.classList.add("ledger-enter");
    }
    return ledgerEl;
  }

  /* ---------------- Formatting helpers ---------------- */

  function parseWhen(value) {
    if (!value) return null;
    var d = new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }

  function timeAgo(value) {
    var d = parseWhen(value);
    if (!d) return null;
    var s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 0) s = 0;
    if (s < 45) return "just now";
    var m = Math.floor(s / 60);
    if (m < 60) return m + " min ago";
    var h = Math.floor(m / 60);
    if (h < 24) return h + (h === 1 ? " hour ago" : " hours ago");
    var days = Math.floor(h / 24);
    if (days === 1) return "yesterday";
    if (days < 30) return days + " days ago";
    var months = Math.floor(days / 30);
    if (months < 12) return months + (months === 1 ? " month ago" : " months ago");
    var years = Math.floor(days / 365);
    return years + (years === 1 ? " year ago" : " years ago");
  }

  function friendlyDate(value) {
    // Accepts "2026-08-06" or ISO datetime; returns e.g. "Wednesday, August 6".
    var d = parseWhen(/^\d{4}-\d{2}-\d{2}$/.test(String(value)) ? value + "T12:00:00" : value);
    if (!d) return String(value || "");
    try {
      return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    } catch (e) {
      return d.toDateString();
    }
  }

  function shortDate(value) {
    var d = parseWhen(/^\d{4}-\d{2}-\d{2}$/.test(String(value)) ? value + "T12:00:00" : value);
    if (!d) return String(value || "");
    try {
      return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    } catch (e) {
      return d.toDateString();
    }
  }

  function prettyKey(key) {
    var s = String(key).replace(/[_-]+/g, " ").trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function briefName(kind) {
    var k = String(kind || "brief");
    return k.charAt(0).toUpperCase() + k.slice(1) + (k === "brief" ? "" : " brief");
  }

  function isUrl(value) {
    return typeof value === "string" && /^https?:\/\//i.test(value);
  }

  /* Defensive string read — the M3 endpoints pass fields through verbatim. */
  function str(v) {
    return typeof v === "string" ? v.trim() : "";
  }

  function isDateOnly(v) {
    return /^\d{4}-\d{2}-\d{2}$/.test(String(v || ""));
  }

  /* Local YYYY-MM-DD for a Date — the docket's today/tomorrow split key. */
  function localDateKey(d) {
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function fmtClock(d) {
    try {
      return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    } catch (e) {
      return d.getHours() + ":" + String(d.getMinutes()).padStart(2, "0");
    }
  }

  /* "9:00 AM" + "9:45 AM" → "9:00–9:45 AM"; date-only starts are all-day;
     anything unparseable degrades to the raw string, never throws. */
  function fmtRange(startStr, endStr) {
    if (isDateOnly(startStr)) return "all day";
    var s = parseWhen(startStr);
    if (!s) return str(startStr) || "—";
    var a = fmtClock(s);
    var e = isDateOnly(endStr) ? null : parseWhen(endStr);
    if (!e) return a;
    var b = fmtClock(e);
    var ma = a.match(/\s?[AP]M$/i);
    var mb = b.match(/\s?[AP]M$/i);
    if (ma && mb && ma[0].toUpperCase() === mb[0].toUpperCase()) {
      a = a.slice(0, a.length - ma[0].length);
    }
    return a + "–" + b;
  }

  /* "sarah.chen@meridianlabs.io" → "Sarah Chen" — research entries carry only
     an email; attendees sometimes arrive nameless too. */
  function nameFromEmail(email) {
    var local = String(email).split("@")[0] || "";
    var words = local.split(/[._-]+/).filter(Boolean);
    if (!words.length) return String(email);
    for (var i = 0; i < words.length; i++) {
      words[i] = words[i].charAt(0).toUpperCase() + words[i].slice(1);
    }
    return words.join(" ");
  }

  /* "Bob|reply" → {main: "Bob", tag: "reply"} — the learner's rule encoding. */
  function splitRule(value) {
    var s = String(value);
    var i = s.indexOf("|");
    if (i === -1) return { main: s, tag: "" };
    return { main: s.slice(0, i), tag: s.slice(i + 1) };
  }

  /* ---------------- Toast (503 / network failures only) ---------------- */

  function toast(message) {
    var root = document.querySelector(".toast-root");
    if (!root) {
      root = el("div", "toast-root");
      root.setAttribute("role", "status");
      root.setAttribute("aria-live", "polite");
      document.body.appendChild(root);
    }
    var t = el("div", "toast", message);
    root.appendChild(t);
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { t.classList.add("show"); });
    });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, reducedMotion ? 0 : 250);
    }, 4200);
  }

  /* ---------------- API layer ---------------- */

  function api(path) {
    return fetch(path, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (res) {
        if (res.status === 401) {
          showSessionExpired();
          return handled("unauthorized");
        }
        if (!res.ok) throw new Error("Request failed (" + res.status + ")");
        return res.json();
      });
  }

  /* Errors already surfaced to the user (toast / overlay) are marked handled
     so inline error paths stay quiet for them. */
  function handled(reason) {
    var e = new Error(reason);
    e.handled = true;
    throw e;
  }

  function ensureCsrf() {
    if (state.csrf) return Promise.resolve(state.csrf);
    return api("/api/session").then(function (data) {
      if (data && typeof data.csrf === "string") state.csrf = data.csrf;
      return state.csrf;
    });
  }

  /* All writes: POST JSON + X-Sotto-CSRF. 403 → refresh session once, retry once.
     503 → toast "editing unavailable". Network failure → toast. Everything else
     throws for the caller's inline error text. Never optimistic. */
  function apiPost(path, body, retried) {
    return ensureCsrf().then(function (token) {
      return fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          "X-Sotto-CSRF": token || ""
        },
        body: JSON.stringify(body || {})
      }).catch(function () {
        toast("No connection — nothing was changed");
        return handled("network");
      });
    }).then(function (res) {
      if (res.status === 401) {
        showSessionExpired();
        return handled("unauthorized");
      }
      if (res.status === 403 && !retried) {
        state.csrf = null;
        return apiPost(path, body, true);
      }
      if (res.status === 503) {
        toast("This deploy can't take edits yet");
        return handled("unavailable");
      }
      if (!res.ok) throw new Error("Request failed (" + res.status + ")");
      return res.json().catch(function () { return {}; });
    });
  }

  function showInlineError(node, message) {
    node.textContent = message;
    node.hidden = false;
  }

  function showSessionExpired() {
    if (document.querySelector(".session-expired")) return;
    var overlay = el("div", "session-expired");
    var card = el("div", "card");
    append(card,
      el("h2", null, "Signed out"),
      el("p", null, "Your session timed out. Your data is safe — sign back in with your setup code."));
    card.appendChild(button("btn btn-primary", "Sign in", function () { location.reload(); }));
    overlay.appendChild(card);
    document.body.appendChild(overlay);
  }

  /* ---------------- Shared view chrome ---------------- */

  function skeletonView(rows) {
    var wrap = el("div");
    wrap.appendChild(el("div", "skeleton skeleton-line w-30"));
    for (var i = 0; i < (rows || 3); i++) {
      var c = el("div", "skeleton-card");
      append(c,
        el("div", "skeleton skeleton-line w-50"),
        el("div", "skeleton skeleton-line w-90"),
        el("div", "skeleton skeleton-line w-70"));
      wrap.appendChild(c);
    }
    return wrap;
  }

  function errorView(message, retry) {
    var card = el("div", "card error-state");
    card.appendChild(el("p", null, message));
    card.appendChild(button("btn", "Try again", retry));
    return card;
  }

  function emptyState(title, body) {
    var wrap = el("div", "empty-state");
    if (title) wrap.appendChild(el("div", "empty-title", title));
    if (body) wrap.appendChild(el("div", null, body));
    return wrap;
  }

  function statusDot(status) {
    var map = { open: "on", waiting: "warn", failed: "bad", blocked: "off" };
    var s = String(status || "open").toLowerCase();
    return el("span", "row-dot " + (map[s] || "off"));
  }

  /* ---------------- WhatsApp-style emphasis (safe tokenization) ----------------
     Splits each line on *bold* / _italic_ runs via a capturing split, then builds
     strong/em/text NODES. No string of data ever becomes markup. */

  function appendEmphasized(parent, line) {
    var tokens = String(line).split(/(\*[^*\n]+\*|_[^_\n]+_)/g);
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (!t) continue;
      if (t.length > 2 && t.charAt(0) === "*" && t.charAt(t.length - 1) === "*") {
        parent.appendChild(el("strong", null, t.slice(1, -1)));
      } else if (t.length > 2 && t.charAt(0) === "_" && t.charAt(t.length - 1) === "_") {
        parent.appendChild(el("em", null, t.slice(1, -1)));
      } else {
        parent.appendChild(document.createTextNode(t));
      }
    }
  }

  /* A paragraph that is one short fully-*bold* line is a section title in the
     chat rendering (compose_brief emits them that way) — surface that structure. */
  function isSectionLine(para) {
    return /^\*[^*\n]{1,64}\*$/.test(para.trim());
  }

  function renderChatText(text) {
    var article = el("article", "brief-body");
    var paragraphs = String(text).split(/\n{2,}/);
    for (var p = 0; p < paragraphs.length; p++) {
      var raw = paragraphs[p];
      if (isSectionLine(raw)) {
        article.appendChild(el("div", "brief-section", raw.trim().slice(1, -1)));
        continue;
      }
      var para = el("p");
      var lines = raw.split("\n");
      for (var i = 0; i < lines.length; i++) {
        if (i > 0) para.appendChild(document.createElement("br"));
        appendEmphasized(para, lines[i]);
      }
      article.appendChild(para);
    }
    return article;
  }

  /* Fallback: readable key/value walk of arbitrary JSON (all textContent). */
  function renderDataWalk(value, depth) {
    depth = depth || 0;
    if (value === null || value === undefined) return el("span", null, "—");
    if (typeof value !== "object") {
      if (isUrl(value)) {
        var a = el("a", null, value);
        a.href = value;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        return a;
      }
      return el("span", null, String(value));
    }
    if (Array.isArray(value)) {
      if (!value.length) return el("span", null, "—");
      var ul = el("ul");
      for (var i = 0; i < value.length; i++) {
        ul.appendChild(append(el("li"), renderDataWalk(value[i], depth + 1)));
      }
      return ul;
    }
    var keys = Object.keys(value);
    if (!keys.length) return el("span", null, "—");
    var dl = el("dl", depth > 0 ? "nested" : null);
    for (var k = 0; k < keys.length; k++) {
      dl.appendChild(el("dt", null, prettyKey(keys[k])));
      dl.appendChild(append(el("dd"), renderDataWalk(value[keys[k]], depth + 1)));
    }
    return dl;
  }

  /* ---------------- View: Today ---------------- */

  function viewToday(anchor) {
    var seq = ++state.renderSeq;
    setView(skeletonView(3));
    Promise.all([
      api("/api/overview"),
      api("/api/loops").catch(function () { return null; }),
      api("/api/calendar").catch(function () { return null; }),
      api("/api/research").catch(function () { return null; })
    ])
      .then(function (results) {
        if (seq !== state.renderSeq) return;
        var data = results[0] || {};
        var loops = results[1] && Array.isArray(results[1].loops) ? results[1].loops : null;
        var cal = results[2];
        var research = results[3];
        var frag = document.createDocumentFragment();

        // Masthead — dateline + folio number
        var head = el("div", "masthead");
        var folio = el("div", "masthead-folio");
        folio.appendChild(el("span", null, "Today"));
        folio.appendChild(el("span", "folio-no", "№ " + String(data.date || "").replace(/-/g, "·")));
        head.appendChild(folio);
        head.appendChild(el("h1", "masthead-date", friendlyDate(data.date || new Date().toISOString())));
        head.appendChild(buildThesis(data, loops, cal));
        frag.appendChild(head);
        frag.appendChild(el("hr", "head-rule"));

        // Today's docket — the day's meetings, each carrying its attendees' research
        var googleOk = !!(data.services && data.services.google === true);
        frag.appendChild(buildDocket(cal, research, googleOk));

        // Waiting on you — open loops that deserve attention today
        var loopRows = loops || [];
        var docket = el("div");
        var capRight;
        if (loopRows.length > 5) {
          capRight = el("a", "cap-count", "all " + loopRows.length + " →");
          capRight.href = "#loops";
        } else {
          capRight = capCount(loopRows.length ? String(loopRows.length) + " open" : "clear");
        }
        docket.appendChild(ledgerCap("Waiting on you", capRight));
        var dl = el("div", "ledger");
        if (!loopRows.length) {
          dl.appendChild(emptyState("Nothing is waiting on you",
            "When a thread needs a reply or a promise needs keeping, it lands here."));
        } else {
          for (var i = 0; i < Math.min(loopRows.length, 5); i++) {
            dl.appendChild(docketRow(loopRows[i] || {}));
          }
        }
        docket.appendChild(enterOnce("today", dl));
        frag.appendChild(docket);

        // Today's briefs, when any have landed
        var todays = Array.isArray(data.briefs_today) ? data.briefs_today : [];
        if (todays.length) {
          frag.appendChild(ledgerCap("Delivered today", capCount(String(todays.length))));
          var bl = el("div", "ledger");
          for (var b = 0; b < todays.length; b++) {
            bl.appendChild(briefRow({ date: data.date, kind: todays[b] && todays[b].kind }, "today"));
          }
          frag.appendChild(bl);
        }

        state.entered.today = true;
        setView();
        main.appendChild(frag);
        if (anchor === "docket") {
          var target = document.getElementById("docket");
          if (target) target.scrollIntoView();
        }
      }).catch(function (err) {
        if (seq !== state.renderSeq || err.handled) return;
        setView(errorView("The Today page didn't load.", function () { viewToday(); }));
      });
  }

  /* One editorial sentence, composed from live data — every clause a link. */
  function buildThesis(data, loops, cal) {
    var p = el("p", "thesis");

    function clause(text, href, isDown) {
      var a = el("a", isDown ? "down" : null, text);
      a.href = href;
      return a;
    }
    function sep() { return el("span", "sep", "·"); }

    var parts = [];

    // 1 · The Bridge (the Mac feeding Sotto)
    var ago = timeAgo(data.last_event_at);
    if (data.bridge_connected === true) {
      parts.push(clause("Bridge live" + (ago ? ", last event " + ago : ""), "/setup"));
    } else {
      var since = ago && ago !== "just now"
        ? " since " + ago.replace(" ago", "") + " ago" : (ago ? "" : " yet");
      parts.push(clause("your Mac hasn't checked in" + since, "/setup", true));
    }

    // 2 · Briefs today
    var todays = Array.isArray(data.briefs_today) ? data.briefs_today : [];
    if (todays.length === 1 && todays[0] && todays[0].kind) {
      parts.push(clause(String(todays[0].kind) + " brief delivered — read it",
        "#briefs/" + encodeURIComponent(data.date || "") + "/" + encodeURIComponent(todays[0].kind)));
    } else if (todays.length > 1) {
      parts.push(clause(todays.length + " briefs delivered today", "#briefs"));
    } else {
      parts.push(clause("no brief has landed yet today", "#briefs"));
    }

    // 3 · The docket — one clause; once today is behind you it looks at tomorrow
    var docketSplit = splitDocketEvents(cal);
    var todaysMeetings = docketSplit.today;
    if (todaysMeetings.length) {
      var mtext = todaysMeetings.length === 1 ? "1 meeting today"
        : todaysMeetings.length + " meetings today";
      var next = null;
      for (var mi = 0; mi < todaysMeetings.length; mi++) {
        var st = parseWhen(todaysMeetings[mi].start);
        if (st && st.getTime() > Date.now()) { next = st; break; }
      }
      if (next) mtext += " — next at " + fmtClock(next);
      parts.push(clause(mtext, "#today/docket"));
    } else if (docketSplit.tomorrow.length) {
      var tn = docketSplit.tomorrow.length;
      var ttext = tn === 1 ? "1 meeting tomorrow" : tn + " meetings tomorrow";
      var firstStart = null;
      for (var ti = 0; ti < docketSplit.tomorrow.length; ti++) {
        var ts2 = parseWhen(docketSplit.tomorrow[ti].start);
        if (ts2) { firstStart = ts2; break; }
      }
      if (firstStart) ttext += " — first at " + fmtClock(firstStart);
      parts.push(clause(ttext, "#today/docket"));
    }

    // 4 · Open loops
    var n = loops ? loops.length : (typeof data.loops_active === "number" ? data.loops_active : 0);
    parts.push(clause(n === 0 ? "no loops open" : n === 1 ? "1 loop open" : n + " loops open", "#loops"));

    // 5 · Services — name only what needs a hand; quiet when all is well
    var svc = (data.services && typeof data.services === "object") ? data.services : {};
    var trouble = [];
    if (svc.google !== true) trouble.push("Google needs connecting");
    if (svc.whatsapp !== true) trouble.push("WhatsApp isn't linked");
    if (svc.granola === "reconnect") trouble.push("Granola needs a reconnect");
    if (trouble.length) {
      parts.push(clause(trouble.join(", "), "/setup", true));
    } else {
      parts.push(clause("all services connected", "/setup"));
    }

    for (var i = 0; i < parts.length; i++) {
      if (i > 0) p.appendChild(sep());
      p.appendChild(parts[i]);
    }
    p.appendChild(document.createTextNode("."));

    // Sentence case for the first clause only.
    var first = p.querySelector("a");
    if (first && first.textContent) {
      first.textContent = first.textContent.charAt(0).toUpperCase() + first.textContent.slice(1);
    }
    return p;
  }

  /* Compact loop row for Today — links to #loops where the actions live. */
  function docketRow(loop) {
    var a = el("a", "ledger-row has-dot");
    a.href = "#loops";
    a.appendChild(statusDot(loop.status));
    var mainCol = el("div", "row-main");
    var name = typeof loop.contact_name === "string" ? loop.contact_name.trim() : "";
    var summary = typeof loop.summary === "string" ? loop.summary.trim() : "";
    mainCol.appendChild(el("div", "row-title", name || summary || "Open loop"));
    if (name && summary) mainCol.appendChild(el("div", "row-sub", summary));
    a.appendChild(mainCol);
    var folio = el("div", "row-folio");
    var opened = timeAgo(loop.created_at);
    if (opened) folio.appendChild(el("span", null, opened.replace(" ago", "")));
    a.appendChild(folio);
    return a;
  }

  /* ---------------- Today's docket (GET /api/calendar) ---------------- */

  /* today/tomorrow split on the event's own wall-clock date (the server already
     filtered to that window), made TIME-aware: today's list keeps only meetings
     still ahead or in progress — a 4:30 call is off the docket at 6pm. In-progress
     meetings are flagged so the folio can say "now". Non-date starts stay in
     today — tolerant. */
  function splitDocketEvents(cal) {
    var out = { today: [], tomorrow: [], selfEmail: null };
    var events = (cal && Array.isArray(cal.events)) ? cal.events : [];
    var todayKey = localDateKey(new Date());
    var now = Date.now();
    out.selfEmail = inferSelfEmail(events);
    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      if (!ev || typeof ev !== "object") continue;
      // Solo blocks (commutes, focus time) aren't meetings — the docket shows only
      // events with at least one other human on them.
      if (!attendeeInfo(ev, out.selfEmail).length) continue;
      var prefix = str(ev.start).slice(0, 10);
      if (isDateOnly(prefix) && prefix > todayKey) { out.tomorrow.push(ev); continue; }
      var s = isDateOnly(ev.start) ? null : parseWhen(ev.start);
      var e = isDateOnly(ev.end) ? null : parseWhen(ev.end);
      if (e && e.getTime() < now) continue;                   // ended — off the docket
      ev._inProgress = !!(s && e && s.getTime() <= now);      // started, not ended
      out.today.push(ev);
    }
    return out;
  }

  /* The dashboard never learns who the user is, so the docket infers it: the
     one address present in EVERY peopled event (2+ of them) is the user's own.
     Two such addresses (a recurring pair) → inconclusive, skip no one. */
  function inferSelfEmail(events) {
    var counts = {};
    var peopled = 0;
    for (var i = 0; i < events.length; i++) {
      var att = Array.isArray(events[i].attendees) ? events[i].attendees : [];
      var seen = {};
      for (var j = 0; j < att.length; j++) {
        var em = att[j] && typeof att[j] === "object" ? str(att[j].email).toLowerCase() : "";
        if (em && !seen[em]) { seen[em] = true; counts[em] = (counts[em] || 0) + 1; }
      }
      if (Object.keys(seen).length) peopled++;
    }
    if (peopled < 2) return null;
    var everywhere = [];
    for (var k in counts) {
      if (counts[k] === peopled) everywhere.push(k);
    }
    return everywhere.length === 1 ? everywhere[0] : null;
  }

  /* The non-self human attendees of an event, with whatever the server's
     knowledge-graph join added (title/company/slug — see /api/calendar). */
  function attendeeInfo(ev, selfEmail) {
    var att = Array.isArray(ev.attendees) ? ev.attendees : [];
    var out = [];
    for (var i = 0; i < att.length; i++) {
      var a = att[i];
      if (!a || typeof a !== "object") continue;
      var em = str(a.email).toLowerCase();
      if (em && em === selfEmail) continue;                       // skip the user
      if (em.indexOf("resource.calendar.google") !== -1) continue; // rooms aren't people
      var name = str(a.name) || (em ? nameFromEmail(em) : "");
      if (!name) continue;
      out.push({ name: name, email: em, title: str(a.title),
                 company: str(a.company), slug: str(a.slug) });
    }
    return out;
  }

  /* The knowledge-graph fallback line: "Sarah Chen — CTO, Meridian Labs",
     linking to the dossier when the server matched a slug. */
  function graphLine(att) {
    var whoBits = [];
    if (att.title) whoBits.push(att.title);
    if (att.company) whoBits.push(att.company);
    if (!whoBits.length) return null;
    var text = att.name + " — " + whoBits.join(", ");
    var line = el("div", "research-line");
    if (att.slug) {
      var a = el("a", "research-who", text);
      a.href = "#people/" + encodeURIComponent(att.slug);
      line.appendChild(a);
    } else {
      line.appendChild(el("span", "research-who", text));
    }
    return line;
  }

  function meetingRow(ev, selfEmail, researchByEmail) {
    var row = el("div", "ledger-row");
    var mainCol = el("div", "row-main");
    mainCol.appendChild(el("div", "row-title", str(ev.summary) || "Untitled meeting"));

    // Every KNOWN attendee gets one quiet context line — the research card
    // when the cache has it, else the knowledge-graph join ("Name — Title,
    // Company" → their dossier). Only attendees Sotto knows nothing about
    // stay on the plain mono names line, so a meeting is never context-bare
    // when anyone on it is on file.
    var people = attendeeInfo(ev, selfEmail);
    var plain = [];
    var known = [];
    var seen = {};
    for (var i = 0; i < people.length; i++) {
      var att = people[i];
      if (att.email && seen[att.email]) continue;
      if (att.email) seen[att.email] = true;
      var entry = att.email && researchByEmail ? researchByEmail[att.email] : null;
      if (entry) known.push({ entry: entry });
      else if (att.title || att.company) known.push({ att: att });
      else plain.push(att.name);
    }
    if (plain.length > 5) {
      var extra = plain.length - 5;
      plain = plain.slice(0, 5);
      plain.push("+" + extra);
    }
    if (plain.length) mainCol.appendChild(el("div", "attendee-line", plain.join(" · ")));
    for (var c = 0; c < known.length; c++) {
      if (known[c].entry) {
        appendResearchLines(mainCol, known[c].entry);
      } else {
        var g = graphLine(known[c].att);
        if (g) mainCol.appendChild(g);
      }
    }
    row.appendChild(mainCol);

    // ONE mono folio line: the time (or "now" while it runs) with the place
    // folded in; a real conference link keeps its own "join →" affordance.
    var folio = el("div", "row-folio");
    var bits = [ev._inProgress ? "now" : fmtRange(ev.start, ev.end)];
    var link = str(ev.meeting_link);
    var loc = str(ev.location);
    // Only a real conference link earns "join →"; a URL-ish location (maps links on
    // commute-type events) is still just a place. A long address folds to its
    // first segment (the venue name) — never a mid-word ellipsis.
    var hasPlace = !isUrl(link) && !!loc;
    if (hasPlace) {
      var place = loc;
      if (place.length > 26) {
        var ci = place.indexOf(",");
        place = ci > 0 && ci <= 26 ? place.slice(0, ci) : place.slice(0, 25) + "…";
      }
      bits.push(place);
    }
    // With a place folded in the line may run long — let it wrap in its own
    // column instead of squeezing the title on a phone.
    folio.appendChild(el("span", hasPlace ? "folio-wrap" : null, bits.filter(Boolean).join(" · ")));
    if (isUrl(link)) {
      var a2 = el("a", "go", "join →");
      a2.href = link;
      a2.target = "_blank";
      a2.rel = "noopener noreferrer";
      folio.appendChild(a2);
    }
    row.appendChild(folio);
    return row;
  }

  function docketLine() {
    var p = el("p", "docket-line");
    for (var i = 0; i < arguments.length; i++) p.appendChild(
      typeof arguments[i] === "string" ? document.createTextNode(arguments[i]) : arguments[i]);
    return p;
  }

  function buildDocket(cal, research, googleOk) {
    var section = el("div");
    section.id = "docket";
    var split = splitDocketEvents(cal);
    var todays = split.today;
    var tomorrows = split.tomorrow;
    var researchByEmail = researchMap(research);
    if (research && research.stale === true && Object.keys(researchByEmail).length) {
      section.appendChild(el("p", "docket-line research-when", "attendee research is from yesterday"));
    }

    // Time-aware framing: once today's meetings are behind you (or there were
    // none) and tomorrow has some, the docket looks forward — "Tomorrow's
    // docket", tomorrow's meetings as the primary list, no sub-label.
    var evening = !todays.length && tomorrows.length > 0;
    var primary = evening ? tomorrows : todays;

    var capRight = null;
    if (cal && !cal.unavailable &&
        !(googleOk === false && !todays.length && !tomorrows.length)) {
      capRight = capCount(primary.length === 0 ? "clear"
        : primary.length === 1 ? "1 meeting" : primary.length + " meetings");
    }
    section.appendChild(ledgerCap(evening ? "Tomorrow's docket" : "Today's docket", capRight));
    var ledger = el("div", "ledger");

    if (!cal) {
      ledger.appendChild(docketLine("The docket didn't load."));
    } else if (cal.unavailable === true ||
               (googleOk === false && !todays.length && !tomorrows.length)) {
      // An empty docket with Google unconnected isn't "a clear day" — it's a
      // calendar Sotto can't see. Say so, honestly, with the fix one tap away.
      var connect = el("a", null, "Connect Google");
      connect.href = "/setup";
      ledger.appendChild(docketLine(connect, " to see your docket."));
    } else if (!todays.length && !tomorrows.length) {
      ledger.appendChild(docketLine("A clear day."));
    } else {
      var selfEmail = split.selfEmail;
      var i;
      for (i = 0; i < primary.length; i++) {
        ledger.appendChild(meetingRow(primary[i], selfEmail, researchByEmail));
      }
      if (!evening && tomorrows.length) {
        var sub = el("div", "ledger-sub");
        sub.appendChild(el("span", null, "Tomorrow"));
        sub.appendChild(el("span", "cap-count", String(tomorrows.length)));
        ledger.appendChild(sub);
        var hiddenRows = [];
        for (i = 0; i < tomorrows.length; i++) {
          var row = meetingRow(tomorrows[i], selfEmail, researchByEmail);
          if (i >= 3) { row.hidden = true; hiddenRows.push(row); }
          ledger.appendChild(row);
        }
        if (hiddenRows.length) {
          var moreWrap = el("div", "ledger-more");
          moreWrap.appendChild(button("text-action", "+" + hiddenRows.length + " more", function () {
            for (var r = 0; r < hiddenRows.length; r++) hiddenRows[r].hidden = false;
            if (moreWrap.parentNode) moreWrap.parentNode.removeChild(moreWrap);
          }));
          ledger.appendChild(moreWrap);
        }
      }
    }
    section.appendChild(enterOnce("today", ledger));
    return section;
  }

  /* ---------------- Attendee research (GET /api/research) ----------------
     Research renders INSIDE the docket, under the meeting it belongs to. No research
     on file → nothing renders (absence isn't a gap). */

  function researchMap(research) {
    var map = {};
    var raw = research && Array.isArray(research.attendees) ? research.attendees : [];
    for (var i = 0; i < raw.length; i++) {
      var entry = raw[i];
      if (!entry || typeof entry !== "object") continue;
      var em = str(entry.email).toLowerCase();
      if (em && !map[em]) map[em] = entry;
    }
    return map;
  }

  /* The lines worth knowing about one attendee, appended under their meeting:
     who they are, what's new (Recently), and the opener (Hook). The who-line
     always leads with the person's NAME — "Sarah Chen — CTO, Meridian Labs",
     the same shape as the knowledge-graph fallback — because the plain names
     line no longer lists attendees that carry a context line. */
  function appendResearchLines(mainCol, entry) {
    var whoBits = [];
    if (str(entry.title)) whoBits.push(str(entry.title));
    if (str(entry.company)) whoBits.push(str(entry.company));
    var who = whoBits.join(", ") || str(entry.company_summary);
    var name = str(entry.name) || (str(entry.email) ? nameFromEmail(str(entry.email).toLowerCase()) : "");
    var text = name && who ? name + " — " + who : (name || who);
    if (text) {
      var whoLine = el("div", "research-line");
      whoLine.appendChild(el("span", "research-who", text));
      mainCol.appendChild(whoLine);
    }

    var recent = Array.isArray(entry.recent_activity) ? entry.recent_activity : [];
    for (var i = 0; i < recent.length; i++) {
      var it = recent[i];
      if (!it || typeof it !== "object" || !str(it.what)) continue;
      var line = el("div", "research-line");
      line.appendChild(el("span", "mini-label", "Recently"));
      line.appendChild(document.createTextNode(str(it.what)));
      if (str(it.when)) line.appendChild(el("span", "research-when", " · " + str(it.when)));
      if (isUrl(it.source_url)) {
        var src = el("a", "research-src", "↗");
        src.href = it.source_url;
        src.target = "_blank";
        src.rel = "noopener noreferrer";
        line.appendChild(src);
      }
      mainCol.appendChild(line);
      break;
    }

    var hooks = Array.isArray(entry.conversation_hooks) ? entry.conversation_hooks : [];
    for (var h = 0; h < hooks.length; h++) {
      if (!str(hooks[h])) continue;
      var hookLine = el("div", "research-line");
      hookLine.appendChild(el("span", "mini-label", "Hook"));
      hookLine.appendChild(el("em", null, str(hooks[h])));
      mainCol.appendChild(hookLine);
      break;
    }
  }

  /* ---------------- View: Open Loops ---------------- */

  function viewLoops() {
    var seq = ++state.renderSeq;
    setView(skeletonView(3));
    api("/api/loops").then(function (data) {
      if (seq !== state.renderSeq) return;
      var loops = (data && Array.isArray(data.loops)) ? data.loops : [];
      var frag = document.createDocumentFragment();
      frag.appendChild(el("p", "eyebrow", "Loops"));
      frag.appendChild(el("h1", "view-title", "Open loops"));
      frag.appendChild(el("p", "view-sub", "What Sotto is holding open until you close it."));

      var counter = {
        count: loops.length,
        cap: capCount(""),
        list: null
      };

      frag.appendChild(ledgerCap("Waiting on you", counter.cap));
      var list = el("div", "ledger");
      counter.list = list;

      if (!loops.length) {
        list.appendChild(emptyState("Nothing is waiting on you",
          "When a thread needs a reply or a promise needs keeping, it lands here."));
      } else {
        for (var i = 0; i < loops.length; i++) {
          list.appendChild(loopRow(loops[i] || {}, counter));
        }
      }
      // Count set AFTER the list is built: on an already-empty page the empty
      // state above stands alone — "All closed" is only for the last row closing.
      updateLoopCount(counter);
      frag.appendChild(enterOnce("loops", list));
      state.entered.loops = true;
      setView();
      main.appendChild(frag);
    }).catch(function (err) {
      if (seq !== state.renderSeq || err.handled) return;
      setView(errorView("The loops didn't load.", viewLoops));
    });
  }

  function updateLoopCount(counter) {
    counter.cap.textContent = counter.count === 1 ? "1 open" : counter.count + " open";
    if (counter.count === 0 && counter.list && !counter.list.querySelector(".empty-state")) {
      counter.list.appendChild(emptyState("All closed",
        "That was the last one. New loops appear as briefs find them."));
    }
  }

  function loopRow(loop, counter) {
    var row = el("div", "ledger-row has-dot");
    row.appendChild(statusDot(loop.status));

    // No "Someone" fabrication: group-chat asks and meeting-title actions have
    // no contact — there the summary IS the title and gets no separate line.
    var name = typeof loop.contact_name === "string" ? loop.contact_name.trim() : "";
    var summary = typeof loop.summary === "string" ? loop.summary.trim() : "";
    var mainCol = el("div", "row-main");
    mainCol.appendChild(el("div", "row-title", name || summary || "Open loop"));
    if (name && summary) mainCol.appendChild(el("div", "row-sub", summary));

    var meetingTime = loop.meeting_time === null || loop.meeting_time === undefined
      ? "" : String(loop.meeting_time).trim();
    // Skip the Meeting line when the summary already states it verbatim.
    if (meetingTime && summary.indexOf(meetingTime) === -1) {
      mainCol.appendChild(el("div", "row-detail",
        "Meeting: " + (timeAgo(meetingTime) ? shortDate(meetingTime) : meetingTime)));
    }
    row.appendChild(mainCol);

    // ONE mono folio line: the age alone (the dot carries status, the summary
    // carries what it is). SURFACED appears only when it says something (>1).
    var folio = el("div", "row-folio");
    var bits = [];
    var opened = timeAgo(loop.created_at);
    if (opened) bits.push(opened.replace(" ago", ""));
    if (typeof loop.times_surfaced === "number" && loop.times_surfaced > 1) {
      bits.push("surfaced " + loop.times_surfaced + "x");
    }
    if (bits.length) folio.appendChild(el("span", null, bits.join(" · ")));
    row.appendChild(folio);

    // M2: terminal transitions — mark resolved / dismiss. No undo (matches chat).
    var anchor = typeof loop.anchor_key === "string" ? loop.anchor_key : "";
    if (anchor) {
      var errEl = el("p", "inline-error");
      errEl.hidden = true;
      var actions = el("div", "row-actions");
      var resolveBtn, dismissBtn;
      var post = function (op) {
        errEl.hidden = true;
        resolveBtn.disabled = dismissBtn.disabled = true;
        apiPost("/api/loops", { anchor_key: anchor, op: op }).then(function () {
          removeLoopRow(row, counter);
        }).catch(function (err) {
          resolveBtn.disabled = dismissBtn.disabled = false;
          if (!err || !err.handled) showInlineError(errEl, "That didn't save — try again");
        });
      };
      resolveBtn = button("text-action", "✓ mark resolved", function () { post("resolve"); });
      dismissBtn = button("text-action", "dismiss", function () { post("dismiss"); });
      append(actions, resolveBtn, dismissBtn);
      row.appendChild(actions);
      mainCol.appendChild(errEl);
    }
    return row;
  }

  function removeLoopRow(row, counter) {
    var finished = false;
    var done = function () {
      if (finished) return;
      finished = true;
      if (row.parentNode) row.parentNode.removeChild(row);
      counter.count = Math.max(0, counter.count - 1);
      updateLoopCount(counter);
    };
    if (reducedMotion) return done();
    row.classList.add("row-leaving");
    row.addEventListener("animationend", done, { once: true });
    setTimeout(done, 350); // fallback if the animation never fires
  }

  /* ---------------- View: Briefs (ledger + reading page) ---------------- */

  /* mode "archive": the date is the entry, the kind is the folio.
     mode "today": the masthead already carries the date, so the kind is the
     entry and the folio is the read affordance. Never both — no date echo. */
  function briefRow(b, mode) {
    var a = el("a", "ledger-row");
    a.href = "#briefs/" + encodeURIComponent(b.date || "") + "/" + encodeURIComponent(b.kind || "");
    var mainCol = el("div", "row-main");
    var folio = el("div", "row-folio");
    if (mode === "today") {
      mainCol.appendChild(el("div", "row-title", briefName(b.kind)));
      folio.appendChild(el("span", "go", "read →"));
    } else {
      mainCol.appendChild(el("div", "row-title", friendlyDate(b.date)));
      folio.appendChild(el("span", null, String(b.kind || "brief")));
    }
    a.appendChild(mainCol);
    a.appendChild(folio);
    return a;
  }

  function viewBriefs(date, kind) {
    if (date && kind) return viewBriefDetail(date, kind);
    var seq = ++state.renderSeq;
    setView(skeletonView(4));
    api("/api/briefs").then(function (data) {
      if (seq !== state.renderSeq) return;
      var briefs = (data && Array.isArray(data.briefs)) ? data.briefs : [];
      var frag = document.createDocumentFragment();
      frag.appendChild(el("p", "eyebrow", "Briefs"));
      frag.appendChild(el("h1", "view-title", "What Sotto has delivered"));
      frag.appendChild(el("p", "view-sub", "Every morning and evening brief, kept on file."));

      frag.appendChild(ledgerCap("On file",
        capCount(briefs.length === 1 ? "1 brief" : briefs.length + " briefs")));
      var list = el("div", "ledger");
      if (!briefs.length) {
        list.appendChild(emptyState("No briefs on file yet",
          "Your next brief lands here the moment it's delivered. Earlier ones live in your chat history."));
      } else {
        for (var i = 0; i < briefs.length; i++) {
          list.appendChild(briefRow(briefs[i] || {}, "archive"));
        }
      }
      frag.appendChild(enterOnce("briefs", list));
      state.entered.briefs = true;
      setView();
      main.appendChild(frag);
    }).catch(function (err) {
      if (seq !== state.renderSeq || err.handled) return;
      setView(errorView("The briefs didn't load.", function () { viewBriefs(); }));
    });
  }

  function viewBriefDetail(date, kind) {
    var seq = ++state.renderSeq;
    setView(skeletonView(2));
    api("/api/briefs?date=" + encodeURIComponent(date) + "&kind=" + encodeURIComponent(kind))
      .then(function (data) {
        if (seq !== state.renderSeq) return;
        data = data || {};
        var frag = document.createDocumentFragment();
        var page = el("div", "reading");

        var back = el("a", "back-link", "← All briefs");
        back.href = "#briefs";
        page.appendChild(back);

        var head = el("div", "masthead");
        var folio = el("div", "masthead-folio");
        folio.appendChild(el("span", null, briefName(data.kind || kind)));
        folio.appendChild(el("span", "folio-no", "№ " + String(data.date || date || "").replace(/-/g, "·")));
        head.appendChild(folio);
        head.appendChild(el("h1", "masthead-date", friendlyDate(data.date || date)));
        page.appendChild(head);
        page.appendChild(el("hr", "head-rule"));

        if (typeof data.chat_text === "string" && data.chat_text.trim()) {
          page.appendChild(renderChatText(data.chat_text));
        } else if (data.data && typeof data.data === "object") {
          var card = el("div", "card");
          card.appendChild(append(el("div", "data-walk"), renderDataWalk(data.data)));
          page.appendChild(card);
        } else {
          page.appendChild(el("p", "view-sub", "This brief was archived without readable text."));
        }
        frag.appendChild(page);
        setView();
        main.appendChild(frag);
      }).catch(function (err) {
        if (seq !== state.renderSeq || err.handled) return;
        setView(errorView("That brief didn't load.", function () { viewBriefDetail(date, kind); }));
      });
  }

  /* ---------------- View: People (one memory graph, legible halves) ---------------- */

  function viewPeople(slug) {
    if (slug) return viewPerson(slug);
    var seq = ++state.renderSeq;

    var frag = document.createDocumentFragment();
    frag.appendChild(el("p", "eyebrow", "People"));
    frag.appendChild(el("h1", "view-title", "Who Sotto knows"));
    frag.appendChild(el("p", "view-sub",
      "One memory graph — the people you talk to and the companies around them. Open anyone to correct what's on file."));

    var wrap = el("div", "search-wrap");
    var label = el("label", "visually-hidden", "Search people and companies");
    label.htmlFor = "people-q";
    var input = el("input", "search-input");
    input.type = "search";
    input.id = "people-q";
    input.placeholder = "Search names and companies…";
    input.autocomplete = "off";
    input.value = state.peopleQuery;
    append(wrap, label, input);
    frag.appendChild(wrap);

    // Segmented filter — All / People / Companies (counts fill in after load)
    var seg = el("div", "seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Filter the index");
    var segBtns = {};
    var results = el("div");
    results.id = "people-results";
    ["all", "person", "company"].forEach(function (key) {
      var names = { all: "All", person: "People", company: "Companies" };
      var b = button(null, names[key], function () {
        state.peopleFilter = key;
        for (var k in segBtns) segBtns[k].setAttribute("aria-pressed", String(k === key));
        loadPeople(seq, results, state.peopleQuery, segBtns);
      });
      b.setAttribute("aria-pressed", String(state.peopleFilter === key));
      segBtns[key] = b;
      seg.appendChild(b);
    });
    frag.appendChild(seg);
    frag.appendChild(results);

    setView();
    main.appendChild(frag);

    var debounceTimer = null;
    input.addEventListener("input", function () {
      state.peopleQuery = input.value;
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { loadPeople(seq, results, input.value, segBtns); }, 250);
    });

    loadPeople(seq, results, state.peopleQuery, segBtns);
  }

  function segLabel(btn, name, count) {
    btn.replaceChildren();
    append(btn, name + " ", el("span", "seg-count", String(count)));
  }

  function loadPeople(seq, container, query, segBtns) {
    container.replaceChildren(skeletonView(2));
    api("/api/people?q=" + encodeURIComponent(query || "")).then(function (data) {
      if (seq !== state.renderSeq || state.peopleQuery !== (query || "")) return;
      var all = (data && Array.isArray(data.people)) ? data.people : [];
      var persons = [], companies = [];
      for (var i = 0; i < all.length; i++) {
        ((all[i] && all[i].type) === "company" ? companies : persons).push(all[i] || {});
      }
      if (segBtns) {
        segLabel(segBtns.all, "All", all.length);
        segLabel(segBtns.person, "People", persons.length);
        segLabel(segBtns.company, "Companies", companies.length);
      }
      container.replaceChildren();

      if (!all.length) {
        container.appendChild(emptyState(
          query ? "Nothing matches “" + query + "”" : "No one on file yet",
          query ? "Search by first name or company — either works."
                : "Sotto meets people through your briefs and files them here, one dossier at a time."));
        return;
      }

      var filter = state.peopleFilter;
      var first = !state.entered.people;
      function section(label, rows) {
        if (!rows.length) {
          container.appendChild(ledgerCap(label, capCount("none")));
          var lg = el("div", "ledger");
          lg.appendChild(emptyState(null,
            label === "People" ? "No people match." : "No companies match."));
          container.appendChild(lg);
          return;
        }
        container.appendChild(ledgerCap(label, capCount(String(rows.length))));
        var ledger = el("div", "ledger");
        for (var i = 0; i < rows.length; i++) ledger.appendChild(personRow(rows[i]));
        if (first && !reducedMotion) ledger.classList.add("ledger-enter");
        container.appendChild(ledger);
      }

      if (filter === "person") {
        section("People", persons);
      } else if (filter === "company") {
        section("Companies", companies);
      } else {
        section("People", persons);
        if (companies.length) section("Companies", companies);
      }
      state.entered.people = true;
    }).catch(function (err) {
      if (seq !== state.renderSeq || err.handled) return;
      container.replaceChildren(errorView("The index didn't load.", function () {
        loadPeople(seq, container, query, segBtns);
      }));
    });
  }

  function personRow(p) {
    var link = el("a", "ledger-row");
    link.href = "#people/" + encodeURIComponent(p.slug || "");

    var mainCol = el("div", "row-main");
    mainCol.appendChild(el("div", "row-title", p.name || p.slug || "Unnamed"));
    var detailBits = [];
    if (p.title) detailBits.push(p.title);
    if (p.company) detailBits.push(p.company);
    if (detailBits.length) mainCol.appendChild(el("div", "row-detail", detailBits.join(" — ")));
    link.appendChild(mainCol);

    // ONE mono folio line — the section cap already says person vs company.
    var folio = el("div", "row-folio");
    var bits = [];
    if (p.type !== "company" && typeof p.fact_count === "number") {
      bits.push(p.fact_count + (p.fact_count === 1 ? " fact" : " facts"));
    }
    var upd = timeAgo(p.updated_at);
    if (upd) bits.push(upd.replace(" ago", ""));
    if (bits.length) folio.appendChild(el("span", null, bits.join(" · ")));
    link.appendChild(folio);
    return link;
  }

  function viewPerson(slug) {
    var seq = ++state.renderSeq;
    setView(skeletonView(3));
    api("/api/people/" + encodeURIComponent(slug)).then(function (data) {
      renderPerson(seq, slug, data || {}, null);
    }).catch(function (err) {
      if (seq !== state.renderSeq || err.handled) return;
      setView(errorView("This dossier didn't load.", function () { viewPerson(slug); }));
    });
  }

  /* Renders the dossier from a server payload — both on load and after every
     write (writes re-render from the POST response; never optimistic).
     Companies read differently: research builds them, so their record is
     read-only here — no correct/archive/add affordances. */
  function renderPerson(seq, slug, data, flashText) {
    if (seq !== state.renderSeq) return;

    var isCompany = data.type === "company";
    var ctx = isCompany ? null : {
      flash: flashText,
      flashDone: false,
      // POST a facts op; on success the server returns the full person → re-render.
      post: function (body, nextFlash) {
        return apiPost("/api/people/" + encodeURIComponent(slug) + "/facts", body)
          .then(function (resp) {
            renderPerson(seq, slug, resp || {}, nextFlash || null);
          });
      }
    };

    var frag = document.createDocumentFragment();

    var back = el("a", "back-link", "← People");
    back.href = "#people";
    frag.appendChild(back);

    var head = el("div", "dossier-head");
    head.appendChild(el("h1", null, data.name || slug));
    var meta = el("div", "dossier-meta");
    var metaBits = [];
    if (isCompany) metaBits.push("company");
    if (data.title) metaBits.push(String(data.title));
    if (data.company) metaBits.push(String(data.company));
    var upd = timeAgo(data.updated_at);
    if (upd) metaBits.push("updated " + upd);
    if (metaBits.length) meta.appendChild(document.createTextNode(metaBits.join(" · ")));
    if (meta.childNodes.length) head.appendChild(meta);
    frag.appendChild(head);
    frag.appendChild(el("hr", "head-rule"));

    // Companies keep their substance as prose sections (About / News / Context),
    // not facts — render those and stop. No facts ledger, no fact count.
    if (isCompany) {
      renderCompanySections(frag, data);
      frag.appendChild(el("p", "note",
        "Company pages are written by research and the briefs' Learn step. To correct one, tell Sotto in chat."));
      setView();
      main.appendChild(frag);
      return;
    }

    var facts = Array.isArray(data.facts) ? data.facts : [];
    var active = [];
    var archived = [];
    for (var i = 0; i < facts.length; i++) {
      var f = facts[i] || {};
      (String(f.status || "").toLowerCase() === "archived" ? archived : active).push(f);
    }

    frag.appendChild(ledgerCap("What Sotto knows",
      capCount(active.length === 1 ? "1 fact" : active.length + " facts")));
    var list = el("div", "ledger");
    if (!active.length && !archived.length) {
      list.appendChild(emptyState(null,
        "Nothing recorded yet — facts appear as briefs and research mention this person. Or add the first one below."));
    }
    for (var a = 0; a < active.length; a++) {
      list.appendChild(factNode(active[a], ctx));
    }
    frag.appendChild(list);
    if (ctx) frag.appendChild(addFactControl(ctx));
    if (archived.length) {
      var details = el("details", "archived-facts");
      details.appendChild(el("summary", null, archived.length + " archived — superseded, kept on file"));
      for (var z = 0; z < archived.length; z++) {
        details.appendChild(factNode(archived[z], null));
      }
      frag.appendChild(details);
    }

    setView();
    main.appendChild(frag);
  }

  /* Company dossier body: the server's `sections` ([{heading, text}], parsed from
     the markdown body's About/News/Context prose) as serif paragraphs under mono
     section labels. The dossier meta line already carries "updated N ago", so the
     caps stay clean — no second date in a second format. */
  function renderCompanySections(frag, data) {
    var sections = Array.isArray(data.sections) ? data.sections : [];
    if (!sections.length) {
      frag.appendChild(ledgerCap("On file", null));
      frag.appendChild(el("p", "company-empty",
        "Nothing written yet — research fills this page as briefs mention this company."));
      return;
    }
    for (var i = 0; i < sections.length; i++) {
      var s = sections[i] || {};
      var text = str(s.text);
      if (!text) continue;
      var heading = str(s.heading) || "On file";
      frag.appendChild(ledgerCap(heading, null));
      var prose = el("div", "company-prose");
      var paragraphs = text.split(/\n{2,}/);
      for (var p = 0; p < paragraphs.length; p++) {
        var para = el("p");
        var lines = paragraphs[p].split("\n");
        for (var ln = 0; ln < lines.length; ln++) {
          if (ln > 0) para.appendChild(document.createElement("br"));
          para.appendChild(document.createTextNode(lines[ln]));
        }
        prose.appendChild(para);
      }
      frag.appendChild(prose);
    }
  }

  /* A fact row in the ledger. ctx enables the quiet edit affordances
     (null for archived facts and for company records). */
  function factNode(fact, ctx) {
    var node = el("div", "ledger-row");
    if (ctx && ctx.flash && !ctx.flashDone && fact.text === ctx.flash) {
      ctx.flashDone = true;
      node.classList.add("fact-flash");
    }
    renderFactDisplay(node, fact, ctx);
    return node;
  }

  function factMetaRow(fact) {
    var meta = el("div", "fact-meta");
    var source = String(fact.source || "");
    if (source === "web_research" && isUrl(fact.source_ref)) {
      var a = el("a", "chip", "web research ↗");
      a.href = fact.source_ref;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      meta.appendChild(a);
    } else if (source) {
      meta.appendChild(el("span", "chip" + (source === "user_edit" ? " user-edit" : ""),
        source.replace(/[_-]+/g, " ")));
    }
    if (fact.type) meta.appendChild(el("span", "chip", String(fact.type).replace(/[_-]+/g, " ")));

    var bits = [];
    if (typeof fact.seen === "number" && fact.seen > 0) bits.push("seen " + fact.seen + "x");
    var last = timeAgo(fact.last);
    if (last) bits.push(last.replace(" ago", ""));   // bare age — the folio convention
    if (bits.length) meta.appendChild(el("span", null, bits.join(" · ")));
    return meta;
  }

  function renderFactDisplay(node, fact, ctx) {
    node.replaceChildren();
    var mainCol = el("div", "row-main");
    mainCol.appendChild(el("p", "fact-text", fact.text || "(empty fact)"));

    var meta = factMetaRow(fact);
    if (meta.childNodes.length) mainCol.appendChild(meta);
    node.appendChild(mainCol);

    if (ctx && fact.id) {
      var actions = el("div", "row-actions");
      actions.appendChild(button("text-action", "correct", function () {
        renderFactCorrect(node, fact, ctx);
      }));
      actions.appendChild(button("text-action", "archive", function () {
        renderFactArchive(node, fact, ctx);
      }));
      node.appendChild(actions);
    }
  }

  function renderFactCorrect(node, fact, ctx) {
    node.replaceChildren();
    var block = el("div", "edit-block row-main");
    var ta = document.createElement("textarea");
    ta.className = "edit-textarea";
    ta.value = fact.text || "";
    ta.rows = 3;
    var taLabel = el("label", "visually-hidden", "Corrected fact");
    var errEl = el("p", "inline-error");
    errEl.hidden = true;
    var row = el("div", "edit-row");
    var saveBtn, cancelBtn;
    saveBtn = button("btn btn-primary", "Save", function () {
      var text = ta.value.trim();
      if (!text) { showInlineError(errEl, "Write the corrected fact first"); return; }
      errEl.hidden = true;
      saveBtn.disabled = cancelBtn.disabled = true;
      ctx.post({ op: "correct", fact_id: fact.id, text: text }, text)
        .catch(function (err) {
          saveBtn.disabled = cancelBtn.disabled = false;
          if (!err || !err.handled) showInlineError(errEl, "That didn't save — try again");
        });
    });
    cancelBtn = button("btn", "Cancel", function () { renderFactDisplay(node, fact, ctx); });
    append(row, saveBtn, cancelBtn);
    append(block, taLabel, ta, row, errEl);
    node.appendChild(block);
    ta.focus();
  }

  function renderFactArchive(node, fact, ctx) {
    node.replaceChildren();
    var block = el("div", "row-main");
    block.appendChild(el("p", "fact-text", fact.text || "(empty fact)"));
    block.appendChild(el("p", "confirm-text", "Archive this fact? It stays on file, out of the way."));
    var errEl = el("p", "inline-error");
    errEl.hidden = true;
    var row = el("div", "edit-row");
    var yesBtn, noBtn;
    yesBtn = button("btn btn-primary", "Archive", function () {
      errEl.hidden = true;
      yesBtn.disabled = noBtn.disabled = true;
      ctx.post({ op: "archive", fact_id: fact.id })
        .catch(function (err) {
          yesBtn.disabled = noBtn.disabled = false;
          if (!err || !err.handled) showInlineError(errEl, "That didn't save — try again");
        });
    });
    noBtn = button("btn", "Cancel", function () { renderFactDisplay(node, fact, ctx); });
    append(row, yesBtn, noBtn);
    block.appendChild(row);
    block.appendChild(errEl);
    node.appendChild(block);
    block.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.stopPropagation(); renderFactDisplay(node, fact, ctx); }
    });
    yesBtn.focus();
  }

  function addFactControl(ctx) {
    var wrap = el("div", "add-fact");
    var openBtn = button("text-action", "+ add a fact", function () {
      wrap.replaceChildren();
      var block = el("div", "edit-block");
      var ta = document.createElement("textarea");
      ta.className = "edit-textarea";
      ta.rows = 3;
      ta.placeholder = "Something Sotto should hold onto…";
      var taLabel = el("label", "visually-hidden", "New fact");

      var select = document.createElement("select");
      select.className = "edit-select";
      for (var i = 0; i < MEMORY_TYPES.length; i++) {
        var opt = document.createElement("option");
        opt.value = MEMORY_TYPES[i];
        opt.textContent = MEMORY_TYPES[i].replace(/_/g, " ");
        select.appendChild(opt);
      }
      var selLabel = el("label", "visually-hidden", "Memory type");

      var errEl = el("p", "inline-error");
      errEl.hidden = true;
      var row = el("div", "edit-row");
      var saveBtn, cancelBtn;
      saveBtn = button("btn btn-primary", "Save", function () {
        var text = ta.value.trim();
        if (!text) { showInlineError(errEl, "Write the fact first"); return; }
        errEl.hidden = true;
        saveBtn.disabled = cancelBtn.disabled = true;
        ctx.post({ op: "add", text: text, memory_type: select.value }, text)
          .catch(function (err) {
            saveBtn.disabled = cancelBtn.disabled = false;
            if (!err || !err.handled) showInlineError(errEl, "That didn't save — try again");
          });
      });
      cancelBtn = button("btn", "Cancel", function () {
        wrap.replaceChildren(openBtn);
      });
      append(row, select, saveBtn, cancelBtn);
      append(block, taLabel, ta, selLabel, row, errEl);
      wrap.appendChild(block);
      ta.focus();
    });
    wrap.appendChild(openBtn);
    return wrap;
  }

  /* ---------------- View: Learned ---------------- */

  function viewLearned() {
    var seq = ++state.renderSeq;
    setView(skeletonView(2));
    api("/api/learned").then(function (data) {
      if (seq !== state.renderSeq) return;
      data = data || {};
      var frag = document.createDocumentFragment();
      frag.appendChild(el("p", "eyebrow", "Learned"));
      frag.appendChild(el("h1", "view-title", "What Sotto has learned"));
      frag.appendChild(el("p", "view-sub", "How Sotto writes as you, and the rules you've taught it."));

      // Your voice — an editorial summary, not a stat grid.
      frag.appendChild(ledgerCap("Your voice", null));
      frag.appendChild(el("hr", "head-rule single"));
      var style = (data.style && typeof data.style === "object") ? data.style : {};
      // The server sends the real registers (the fingerprint's canonical keys:
      // work_email / work_message / personal_message) — render them as-is.
      var buckets = Array.isArray(style.buckets) ? style.buckets : [];
      var perPerson = typeof style.per_person === "number" ? style.per_person : 0;
      if (buckets.length || perPerson) {
        var prose = el("p", "voice-prose");
        append(prose, "Sotto writes in your voice across ",
          el("strong", null, buckets.length === 1 ? "1 register" : buckets.length + " registers"));
        if (perPerson > 0) {
          append(prose, ", with a distinct feel for ",
            el("strong", null, perPerson === 1 ? "1 person" : perPerson + " people"),
            " you write to often");
        }
        var updAgo = timeAgo(style.updated_at);
        append(prose, updAgo ? " — last tuned " + updAgo + "." : ".");
        frag.appendChild(prose);
        if (buckets.length) {
          var chips = el("div", "voice-chips");
          for (var i = 0; i < buckets.length; i++) {
            chips.appendChild(el("span", "chip", String(buckets[i]).replace(/[_-]+/g, " ")));
          }
          frag.appendChild(chips);
        }
      } else {
        frag.appendChild(el("p", "voice-prose",
          "No fingerprint yet. Text “set up Sotto” and it reads your recent messages to learn how you sound."));
      }
      frag.appendChild(el("p", "note",
        "The fingerprint tunes itself from what you actually send — to steer it, just tell Sotto in chat."));

      // Preferences — the real rule ledgers, each row deletable (M2).
      var prefWrap = el("div");
      frag.appendChild(prefWrap);
      paintPrefs(seq, prefWrap, data.preferences);

      setView();
      main.appendChild(frag);
    }).catch(function (err) {
      if (seq !== state.renderSeq || err.handled) return;
      setView(errorView("The Learned page didn't load.", viewLearned));
    });
  }

  /* The learner's real shape: top-level rule lists, the approval_defaults
     dict, and the user-stated explicit block. Delete sends the server's
     contract — {op: "delete", list, value} — and re-renders from the
     response. Anything shaped differently degrades to read-only. */
  var PREF_SECTIONS = [
    { list: "deprioritization_hints", label: "Deprioritized",
      sub: "Kept out of the spotlight in your briefs." },
    { list: "edit_heavy", label: "Edit-heavy",
      sub: "You usually rewrite these drafts, so Sotto drafts them more carefully." }
  ];
  var PREF_EXPLICIT = [
    { list: "mute_senders", label: "Muted senders" },
    { list: "mute_people", label: "Muted people" },
    { list: "mute_sections", label: "Muted sections" },
    { list: "tone_notes", label: "Tone notes" }
  ];

  function paintPrefs(seq, container, prefs) {
    container.replaceChildren();
    var isObj = prefs && typeof prefs === "object" && !Array.isArray(prefs);

    var deleteRule = function (listName, value) {
      return apiPost("/api/prefs", { op: "delete", list: listName, value: value })
        .then(function (resp) {
          if (seq !== state.renderSeq) return;
          paintPrefs(seq, container, resp);
        });
    };

    var sections = [];  // {label, sub, rows: [{main, tag, extra, del}]}

    if (isObj) {
      var i, k, entry;
      for (i = 0; i < PREF_SECTIONS.length; i++) {
        entry = PREF_SECTIONS[i];
        var vals = prefs[entry.list];
        if (Array.isArray(vals) && vals.length) {
          sections.push({ label: entry.label, sub: entry.sub,
            rows: vals.map(ruleRowSpec(entry.list)) });
        }
      }
      var approvals = prefs.approval_defaults;
      if (approvals && typeof approvals === "object" && !Array.isArray(approvals) &&
          Object.keys(approvals).length) {
        var appRows = [];
        for (k in approvals) {
          if (!Object.prototype.hasOwnProperty.call(approvals, k)) continue;
          var parts = splitRule(k);
          appRows.push({ main: parts.main, tag: parts.tag,
            extra: String(approvals[k]), list: "approval_defaults", value: k });
        }
        sections.push({ label: "Approval defaults",
          sub: "How much of a green light each kind of action gets.", rows: appRows });
      }
      var explicit = prefs.explicit;
      if (explicit && typeof explicit === "object") {
        for (i = 0; i < PREF_EXPLICIT.length; i++) {
          entry = PREF_EXPLICIT[i];
          var ev = explicit[entry.list];
          if (Array.isArray(ev) && ev.length) {
            sections.push({ label: entry.label, sub: null,
              rows: ev.map(ruleRowSpec(entry.list)) });
          }
        }
      }
    }

    // The umbrella cap takes the same single hairline "Your voice" does (added
    // per branch below — the legacy raw ledger brings its own top rule).
    container.appendChild(ledgerCap("House rules",
      capCount(sections.length ? countRules(sections) + " on file" : "none yet")));

    if (!sections.length) {
      // Legacy/unknown shapes stay visible read-only rather than vanishing.
      var hasAny = prefs && typeof prefs === "object" &&
        (Array.isArray(prefs) ? prefs.length : Object.keys(prefs).length);
      if (hasAny && Array.isArray(prefs)) {
        var lg = el("div", "ledger");
        for (var j = 0; j < prefs.length; j++) {
          var r = el("div", "ledger-row");
          var m = el("div", "row-main");
          m.appendChild(el("div", "row-sub", typeof prefs[j] === "object"
            ? JSON.stringify(prefs[j]) : String(prefs[j])));
          r.appendChild(m);
          lg.appendChild(r);
        }
        container.appendChild(lg);
      } else {
        container.appendChild(el("hr", "head-rule single"));
        container.appendChild(emptyState("No rules yet",
          "Tell Sotto how you like things — “stop surfacing newsletters” — and the rule appears here."));
      }
      return;
    }

    // The umbrella cap takes the same single hairline "Your voice" does; each
    // rule group below then reads cap-over-ledger, the page's normal rhythm.
    container.appendChild(el("hr", "head-rule single"));
    for (var s = 0; s < sections.length; s++) {
      var sec = sections[s];
      container.appendChild(ledgerCap(sec.label, capCount(String(sec.rows.length))));
      if (sec.sub) container.appendChild(el("p", "cap-sub", sec.sub));
      var ledger = el("div", "ledger");
      for (var rI = 0; rI < sec.rows.length; rI++) {
        ledger.appendChild(prefRow(sec.rows[rI], deleteRule));
      }
      container.appendChild(ledger);
    }
  }

  function ruleRowSpec(listName) {
    return function (v) {
      var parts = splitRule(v);
      return { main: parts.main, tag: parts.tag, extra: "", list: listName, value: String(v) };
    };
  }

  function countRules(sections) {
    var n = 0;
    for (var i = 0; i < sections.length; i++) n += sections[i].rows.length;
    return n;
  }

  function prefRow(spec, deleteRule) {
    var row = el("div", "ledger-row");
    var mainCol = el("div", "row-main");
    mainCol.appendChild(el("div", "row-sub", spec.main));
    row.appendChild(mainCol);

    var folio = el("div", "row-folio");
    // The rule encoding's snake_case ("follow_up", "auto_draft") never reaches
    // the page — mono caps read as words.
    var tagBits = [];
    if (spec.tag) tagBits.push(spec.tag.replace(/[_-]+/g, " "));
    if (spec.extra) tagBits.push("→ " + spec.extra.replace(/[_-]+/g, " "));
    if (tagBits.length) folio.appendChild(el("span", null, tagBits.join(" ")));

    var errEl = el("p", "inline-error");
    errEl.hidden = true;
    var slot = el("span", "pref-confirm reveal");
    var showX = function (refocus) {
      slot.replaceChildren();
      var x = button("text-action", "remove", function () {
        slot.replaceChildren();
        var yes = button("text-action", "confirm", function () {
          errEl.hidden = true;
          yes.disabled = keep.disabled = true;
          deleteRule(spec.list, spec.value).catch(function (err) {
            yes.disabled = keep.disabled = false;
            if (!err || !err.handled) showInlineError(errEl, "That didn't save — try again");
          });
        });
        var keep = button("text-action", "keep", function () { showX(true); });
        append(slot, yes, keep);
        yes.focus();
      });
      x.setAttribute("aria-label", "Remove this rule: " + spec.main);
      slot.appendChild(x);
      if (refocus) x.focus();
    };
    // Escape backs out of the confirm and returns focus to the remove control.
    slot.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && slot.childNodes.length > 1) {
        e.stopPropagation();
        showX(true);
      }
    });
    showX();
    folio.appendChild(slot);
    row.appendChild(folio);
    mainCol.appendChild(errEl);
    return row;
  }

  /* ---------------- View: The Record (GET /api/ledger) ----------------
     The action timeline: outcomes.jsonl + dashboard audit rows + the triage
     funnel's surfaced.jsonl, merged newest first by the server, grouped here
     by local day. Every row becomes a serif sentence; unknown shapes render
     whatever fields exist — never crash. The triage rows make this view the
     answer to "why didn't I get nudged?" — hence the Nudges/Held filter. */

  var RECORD_WINDOWS = [7, 14, 30];
  var RECORD_FILTERS = [["all", "All"], ["nudges", "Nudges"],
                        ["held", "Held"], ["actions", "Actions"]];

  function recordFilterMatch(filter, entry) {
    if (filter === "all") return true;
    if (entry.source === "triage") {
      var v = str(entry.verdict);
      if (filter === "nudges") return v === "agent" || v === "promoted";
      if (filter === "held") return v === "queue" || v === "drop";
      return false;
    }
    return filter === "actions";
  }

  function viewRecord(daysPart) {
    var seq = ++state.renderSeq;
    var days = RECORD_WINDOWS.indexOf(parseInt(daysPart, 10)) !== -1
      ? parseInt(daysPart, 10) : 7;
    setView(skeletonView(3));
    api("/api/ledger?days=" + days).then(function (data) {
      if (seq !== state.renderSeq) return;
      var entries = [];
      var raw = data && Array.isArray(data.entries) ? data.entries : [];
      for (var i = 0; i < raw.length; i++) {
        if (raw[i] && typeof raw[i] === "object") entries.push(raw[i]);
      }
      var frag = document.createDocumentFragment();
      frag.appendChild(el("p", "eyebrow", "Record"));
      frag.appendChild(el("h1", "view-title", "What Sotto has done"));
      frag.appendChild(el("p", "view-sub",
        "Every action taken, every nudge surfaced or held, every correction made — kept on file."));

      // Window selector — mono links; the served window carries the mark.
      var range = el("div", "range-row");
      range.appendChild(el("span", "range-label", "last"));
      for (var w = 0; w < RECORD_WINDOWS.length; w++) {
        if (w > 0) range.appendChild(el("span", "sep", "·"));
        var a = el("a", null, String(RECORD_WINDOWS[w]));
        a.href = "#record/" + RECORD_WINDOWS[w];
        if (RECORD_WINDOWS[w] === days) a.setAttribute("aria-current", "true");
        range.appendChild(a);
      }
      range.appendChild(el("span", "range-label", "days"));
      frag.appendChild(range);

      // Filter row — same mono structure as the window selector; client-side
      // only (the filter re-renders the already-fetched entries).
      var filter = "all";
      var frow = el("div", "range-row");
      frow.appendChild(el("span", "range-label", "show"));
      var filterLinks = [];
      RECORD_FILTERS.forEach(function (pair, idx) {
        if (idx > 0) frow.appendChild(el("span", "sep", "·"));
        var fa = el("a", null, pair[1]);
        fa.href = "#record/" + days;
        if (pair[0] === filter) fa.setAttribute("aria-current", "true");
        fa.addEventListener("click", function (ev) {
          ev.preventDefault();
          filter = pair[0];
          for (var fi = 0; fi < filterLinks.length; fi++) {
            filterLinks[fi].removeAttribute("aria-current");
          }
          fa.setAttribute("aria-current", "true");
          renderList();
        });
        filterLinks.push(fa);
        frow.appendChild(fa);
      });
      frag.appendChild(frow);

      var list = el("div", "ledger");

      function renderList() {
        list.replaceChildren();
        var rows = [];
        for (var e = 0; e < entries.length; e++) {
          if (recordFilterMatch(filter, entries[e])) rows.push(entries[e]);
        }
        if (!rows.length) {
          list.appendChild(filter === "all"
            ? emptyState("Nothing on the record yet",
              "Approve a draft or close a loop — every action Sotto takes lands here.")
            : emptyState("Nothing here for this filter",
              "Try All — every nudge, hold, and action in the window is under it."));
          return;
        }
        // Group by local day, preserving the server's newest-first order.
        var groups = [];
        var byKey = {};
        for (var j = 0; j < rows.length; j++) {
          var d = parseWhen(rows[j].ts);
          var key = d ? localDateKey(d) : "undated";
          if (!byKey[key]) {
            byKey[key] = { key: key, when: d, rows: [] };
            groups.push(byKey[key]);
          }
          byKey[key].rows.push(rows[j]);
        }
        for (var g = 0; g < groups.length; g++) {
          var sub = el("div", "ledger-sub");
          sub.appendChild(el("span", null,
            groups[g].when ? shortDate(groups[g].key) : "Undated"));
          sub.appendChild(el("span", "cap-count", String(groups[g].rows.length)));
          list.appendChild(sub);
          for (var r = 0; r < groups[g].rows.length; r++) {
            list.appendChild(recordRow(groups[g].rows[r]));
          }
        }
      }

      renderList();
      frag.appendChild(enterOnce("record", list));
      state.entered.record = true;
      setView();
      main.appendChild(frag);
    }).catch(function (err) {
      if (seq !== state.renderSeq || err.handled) return;
      setView(errorView("The record didn't load.", function () { viewRecord(days); }));
    });
  }

  function recordRow(entry) {
    var row = el("div", "ledger-row");
    var mainCol = el("div", "row-main");
    var sentence = recordSentence(entry);
    if (!/[.!?]$/.test(sentence)) sentence += ".";
    mainCol.appendChild(el("div", "row-sub", sentence));
    row.appendChild(mainCol);

    // ONE mono folio line: the clock, plus the channel when it's known.
    var folio = el("div", "row-folio");
    var d = parseWhen(entry.ts);
    var bits = [d ? fmtClock(d) : "—"];
    if ((entry.source === "outcome" || entry.source === "triage") && str(entry.channel)) {
      bits.push(humanChannel(entry.channel));
    }
    folio.appendChild(el("span", null, bits.join(" · ")));
    row.appendChild(folio);
    return row;
  }

  function humanChannel(v) {
    var map = { imessage: "iMessage", whatsapp: "WhatsApp", gmail: "Gmail",
                email: "email", sms: "SMS", calendar: "calendar" };
    var k = str(v).toLowerCase();
    return map[k] || str(v);
  }

  /* "reply" → "a reply" — the noun phrase for outcome sentences. */
  function actionThing(t) {
    var map = { reply: "a reply", follow_up: "a follow-up", send: "a message",
                schedule: "a calendar hold", meeting_prep: "meeting prep",
                email: "an email" };
    var key = str(t).toLowerCase();
    if (map[key]) return map[key];
    if (!key) return "a message";
    var words = key.replace(/[_-]+/g, " ");
    return (/^[aeiou]/.test(words) ? "an " : "a ") + words;
  }

  function recordSentence(entry) {
    if (entry.source === "triage" || str(entry.verdict)) return triageSentence(entry);
    return entry.source === "outcome" || str(entry.outcome)
      ? outcomeSentence(entry)
      : dashboardSentence(entry);
  }

  /* triage_event.py surfaced.jsonl vocabulary — verdict: agent | queue | drop |
     promoted; class carries the funnel's demotion/Tier-1 class. Unknown values
     degrade to their prettified name, never crash. */
  var TRIAGE_CLASS_LABELS = {
    quiet: "quiet hours", cooldown: "cooldown", stale: "catch-up backlog",
    group: "group chatter", unknown: "unknown sender", ambient: "ambient",
    signal: "your own message", urgent: "urgent", actionable: "real ask",
    scheduling_ask: "scheduling ask", missed_call: "missed call",
    post_meeting: "post-meeting tap", meeting_hold: "in a meeting",
    automated: "automated", muted: "muted", system: "system message",
    ignore: "no signal", call: "call", error: "triage error"
  };

  function triageSentence(e) {
    var who = str(e.sender);
    var why = str(e.reason);
    var cls = str(e["class"]).toLowerCase();
    var label = TRIAGE_CLASS_LABELS[cls] || cls.replace(/[_-]+/g, " ");
    // "Sarah Chen — direct ask about Thursday"; skip the name when the reason
    // already carries it (missed-call reasons do), never render an empty dash.
    var detail = (why && who && why.toLowerCase().indexOf(who.toLowerCase()) !== -1)
      ? why
      : [who, why].filter(Boolean).join(" — ");
    if (!detail) detail = "an event";
    var tail = label ? " (" + label + ")" : "";
    switch (str(e.verdict)) {
      case "agent": return "Nudged you — " + detail + tail;
      case "promoted": return "Promoted from the queue — " + detail;
      case "queue": return "Held — " + detail + tail;
      case "drop": return "Filtered out — " + detail + tail;
      default: return "Triaged — " + detail + tail;
    }
  }

  /* log_outcome.py vocabulary: draft_created | opened | copied | dismissed |
     executed | viewed | edited_and_sent. Anything else renders tolerantly. */
  function outcomeSentence(e) {
    var thing = actionThing(e.action_type);
    var contact = str(e.contact);
    var to = contact ? " to " + contact : "";
    var forC = contact ? " for " + contact : "";
    switch (str(e.outcome)) {
      case "edited_and_sent":
        return "Drafted " + thing + to + " — edited before sending";
      case "executed":
        return str(e.action_type).toLowerCase() === "schedule"
          ? "Put a hold on the calendar" + forC
          : "Sent " + thing + to;
      case "draft_created":
        return "Drafted " + thing + to;
      case "copied":
        return "Drafted " + thing + to + " — copied to send";
      case "opened":
        return "Opened " + thing + to +
          (str(e.channel) ? " in " + humanChannel(e.channel) : "");
      case "dismissed":
        return "You dismissed " + thing + forC;
      case "viewed":
        return "You viewed " + thing + forC;
      default: {
        var o = str(e.outcome);
        return (o ? prettyKey(o) : "Logged an action") + " — " + thing +
          (contact ? ", " + contact : "");
      }
    }
  }

  /* "sarah-chen" → "Sarah Chen" — audit rows carry slugs; sentences carry names. */
  function nameFromSlug(slug) {
    var words = String(slug).split(/[_-]+/).filter(Boolean);
    for (var i = 0; i < words.length; i++) {
      words[i] = words[i].charAt(0).toUpperCase() + words[i].slice(1);
    }
    return words.join(" ") || String(slug);
  }

  /* Dashboard audit rows: {event: "write", endpoint, target, op} plus the
     login trail. Unknown events degrade to their prettified name. */
  function dashboardSentence(e) {
    var ev = str(e.event);
    if (ev === "write") {
      var ep = str(e.endpoint);
      var op = str(e.op);
      var target = str(e.target);
      var m = ep.match(/^\/api\/people\/([a-z0-9_-]+)\/facts$/);
      if (m) {
        var who = nameFromSlug(m[1]);
        if (op === "correct") return "You corrected a fact on " + who;
        if (op === "archive") return "You archived a fact on " + who;
        if (op === "add") return "You added a fact on " + who;
        return "You edited the file on " + who;
      }
      if (ep === "/api/loops") {
        if (op === "resolve") return "You marked a loop resolved";
        if (op === "dismiss") return "You dismissed a loop";
        return "You updated a loop";
      }
      if (ep === "/api/prefs") {
        var value = target.indexOf(":") !== -1
          ? target.slice(target.indexOf(":") + 1) : target;
        return "You removed a house rule" + (value ? " — " + value : "");
      }
      return "You made an edit" + (op ? " (" + op.replace(/[_-]+/g, " ") + ")" : "");
    }
    if (ev === "login_ok") return "You signed in";
    if (ev === "login_fail") return "A sign-in attempt failed";
    if (ev === "lockout") return "Sign-in locked for a minute after repeated failures";
    return ev ? prettyKey(ev) : "An event was recorded";
  }

  /* ---------------- Router ---------------- */

  var routes = {
    today: function (parts) { viewToday(decodePart(parts[1])); },
    loops: function () { viewLoops(); },
    briefs: function (parts) { viewBriefs(decodePart(parts[1]), decodePart(parts[2])); },
    people: function (parts) { viewPeople(decodePart(parts[1])); },
    learned: function () { viewLearned(); },
    record: function (parts) { viewRecord(decodePart(parts[1])); }
  };

  function decodePart(part) {
    if (!part) return null;
    try { return decodeURIComponent(part); } catch (e) { return part; }
  }

  function route() {
    var hash = location.hash.replace(/^#/, "") || "today";
    var parts = hash.split("/");
    var name = routes[parts[0]] ? parts[0] : "today";

    // Nav highlight
    var links = navlinks.querySelectorAll("a[data-nav]");
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("data-nav") === name) {
        links[i].setAttribute("aria-current", "page");
      } else {
        links[i].removeAttribute("aria-current");
      }
    }

    routes[name](parts);
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  /* ---------------- Boot ---------------- */

  function boot() {
    api("/api/session").then(function (data) {
      if (data && typeof data.csrf === "string") state.csrf = data.csrf;
    }).catch(function () {
      /* 401 already showed the session screen; other errors surface per-view */
    }).then(route);
    window.addEventListener("hashchange", route);
  }

  boot();
})();
