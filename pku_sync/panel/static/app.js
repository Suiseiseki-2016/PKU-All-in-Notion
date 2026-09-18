"use strict";
/* Student panel renderer (VAL-META-019/020).
 *
 * Every user-facing string comes from window.PANEL_COPY (served by
 * pku_sync/panel/student_ui.py) so the approved prototype copy has exactly
 * one source of truth. The renderer is metadata-only: it draws index counts,
 * titles, and states — never page bodies — and it never receives or renders a
 * credential. */

(function () {
  var COPY = window.PANEL_COPY;
  var app = document.getElementById("app");
  var toastEl = document.getElementById("toast");
  var toastTimer = null;
  var pollTimer = null;
  var POLL_INTERVAL_MS = 1500;
  var POLL_MAX_ATTEMPTS = 240;

  var state = {
    activated: false,
    connection: {
      state: "disconnected",
      connected: false,
      status_label: COPY.connection.disconnected,
      error: null
    },
    directory: null,
    directoryError: "",
    directoryLoading: false,
    notice: "",
    noticeKind: "hint"
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function button(label, action, options) {
    var opts = options || {};
    var classes = ["btn"];
    if (opts.primary) classes.push("btn-primary");
    if (opts.small) classes.push("btn-small");
    if (opts.quiet) classes.push("btn-quiet");
    return (
      '<button type="button" class="' +
      classes.join(" ") +
      '" data-action="' +
      esc(action) +
      '"' +
      (opts.disabled ? " disabled" : "") +
      ">" +
      esc(label) +
      "</button>"
    );
  }

  function brand(compact) {
    return (
      '<div class="' +
      (compact ? "mobile-brand" : "sidebar-brand") +
      '"><span class="brand-mark" aria-hidden="true">' +
      esc(COPY.brand.mark) +
      '</span><span class="brand-name">' +
      esc(COPY.brand.name) +
      "<small>" +
      esc(COPY.brand.note) +
      "</small></span></div>"
    );
  }

  /* -- network ------------------------------------------------------------ */

  function requestJson(url, options) {
    return fetch(url, options).then(function (response) {
      return response
        .json()
        .catch(function () {
          return null;
        })
        .then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        });
    });
  }

  function loadQuota() {
    return requestJson("/api/platform/quota").then(function (result) {
      state.activated = Boolean(result.ok && result.body && result.body.active);
    });
  }

  function loadConnection() {
    return requestJson("/api/connection").then(function (result) {
      if (result.ok && result.body) state.connection = result.body;
    });
  }

  function loadDirectory() {
    if (!state.connection.connected) {
      // a disconnected panel never reads (or shows) the index
      state.directory = null;
      state.directoryError = "";
      return Promise.resolve();
    }
    state.directoryLoading = true;
    render();
    return requestJson("/api/directory").then(function (result) {
      state.directoryLoading = false;
      if (result.ok && result.body) {
        state.directory = result.body;
        state.directoryError = "";
      } else {
        state.directory = null;
        state.directoryError =
          (result.body && result.body.detail) || COPY.directory.error.title;
      }
    });
  }

  /* -- onboarding -------------------------------------------------------- */

  function notionConnectionRow() {
    var copy = COPY.onboarding.notion;
    var connection = state.connection;
    var connecting = connection.state === "connecting";
    var right = connection.connected
      ? '<span class="check" aria-label="' +
        esc(copy.check_label) +
        '">✓</span>' +
        button(copy.revoke, "revoke-notion", { small: true, quiet: true })
      : button(copy.reconnect, "connect-notion", {
          small: true,
          quiet: true,
          disabled: connecting
        });
    return (
      '<div class="connection-item" data-row="notion"><span class="item-left">' +
      '<span class="connection-icon" aria-hidden="true">' +
      esc(copy.badge) +
      "</span><span><strong>" +
      esc(copy.title) +
      "</strong><small data-field=\"connection-status\">" +
      esc(connection.status_label) +
      '</small></span></span><span class="item-right">' +
      right +
      "</span></div>"
    );
  }

  function onboarding() {
    var copy = COPY.onboarding;
    var trust = copy.trust_items
      .map(function (item) {
        return (
          '<div class="trust-item"><span class="trust-icon" aria-hidden="true">' +
          esc(item.icon) +
          "</span><span><strong>" +
          esc(item.title) +
          "</strong><small>" +
          esc(item.detail) +
          "</small></span></div>"
        );
      })
      .join("");
    var note = state.notice || copy.code_help;
    var noteClass = state.notice && state.noticeKind === "error" ? "form-note" : "form-note hint";
    return (
      '<main class="onboarding">' +
      '<section class="onboarding-aside" aria-labelledby="welcome-title">' +
      brand(false) +
      '<div class="aside-copy"><p class="eyebrow">' +
      esc(copy.aside.eyebrow) +
      '</p><h1 id="welcome-title">' +
      esc(copy.aside.title_lines[0]) +
      "<br>" +
      esc(copy.aside.title_lines[1]) +
      '</h1><p class="lead">' +
      esc(copy.aside.lead) +
      "</p></div>" +
      '<p class="aside-note"><span aria-hidden="true">⌁</span><span>' +
      esc(copy.aside.note) +
      "</span></p></section>" +
      '<section class="onboarding-main"><div class="onboarding-card" aria-labelledby="activation-title">' +
      '<header><span class="mini-status"><span aria-hidden="true">●</span> ' +
      esc(copy.mini_status) +
      '</span><span class="brand-name" style="font-size:12px">' +
      esc(copy.role) +
      "<small>" +
      esc(copy.role_note) +
      "</small></span></header>" +
      '<div class="progress-wrap" aria-label="' +
      esc(copy.progress.aria_label) +
      '"><div class="progress-label"><span>' +
      esc(copy.progress.label) +
      "</span><strong>" +
      esc(copy.progress.value) +
      '</strong></div><div class="progress-track" aria-hidden="true">' +
      '<span class="progress-step done"></span><span class="progress-step done"></span>' +
      '<span class="progress-step current"></span></div></div>' +
      '<p class="eyebrow">' +
      esc(copy.eyebrow) +
      '</p><h2 class="card-title" id="activation-title">' +
      esc(copy.title) +
      '</h2><p class="card-subtitle">' +
      esc(copy.subtitle) +
      "</p>" +
      '<div class="connection-list" aria-label="' +
      esc(copy.connection_list_label) +
      '"><div class="connection-item"><span class="item-left">' +
      '<span class="connection-icon" aria-hidden="true">' +
      esc(copy.campus.badge) +
      "</span><span><strong>" +
      esc(copy.campus.title) +
      "</strong><small>" +
      esc(copy.campus.detail) +
      '</small></span></span><span class="check" aria-label="' +
      esc(copy.campus.check_label) +
      '">✓</span></div>' +
      notionConnectionRow() +
      "</div>" +
      '<div class="trust-list" aria-label="' +
      esc(copy.trust_label) +
      '">' +
      trust +
      "</div>" +
      '<form id="activation-form"><label class="field-label" for="code">' +
      esc(copy.code_label) +
      '</label><div class="input-row">' +
      '<input id="code" type="text" class="text-input" autocomplete="off" placeholder="' +
      esc(copy.code_placeholder) +
      '" aria-describedby="code-help" required>' +
      button(copy.activate, "activate", { primary: true }) +
      '</div><p id="code-help" class="' +
      noteClass +
      '">' +
      esc(note) +
      "</p></form>" +
      '<p class="card-footnote">' +
      esc(copy.footer) +
      "</p></div></section></main>"
    );
  }

  /* -- activated shell --------------------------------------------------- */

  function connectionControls(small) {
    var copy = COPY.connection;
    var connecting = state.connection.state === "connecting";
    return state.connection.connected
      ? button(copy.revoke, "revoke-notion", { small: small, quiet: true })
      : button(copy.reconnect, "connect-notion", {
          small: small,
          quiet: true,
          disabled: connecting
        });
  }

  function connectionCard() {
    var copy = COPY.connection;
    return (
      '<div class="account-card" data-block="connection"><div class="account-name">' +
      '<span class="avatar" aria-hidden="true">N</span>' +
      esc(copy.manage_label) +
      '</div><p data-field="connection-status">' +
      esc(state.connection.status_label) +
      "</p>" +
      connectionControls(true) +
      "</div>"
    );
  }

  function shell(content) {
    return (
      '<div class="app-shell"><aside class="sidebar" aria-label="' +
      esc(COPY.nav.label) +
      '">' +
      brand(false) +
      '<p class="side-kicker">' +
      esc(COPY.nav.kicker) +
      '</p><nav class="nav"><button type="button" class="active" data-nav="dashboard" aria-current="page">' +
      '<span class="nav-icon" aria-hidden="true">⌂</span>' +
      esc(COPY.nav.dashboard) +
      '</button></nav><div class="sidebar-spacer"></div>' +
      connectionCard() +
      '</aside><header class="mobile-topbar">' +
      brand(true) +
      '<div class="mobile-connection">' +
      connectionControls(true) +
      "</div></header>" +
      '<main class="main"><div class="main-inner">' +
      content +
      "</div></main></div>"
    );
  }

  function disconnectedDirectoryState() {
    var copy = COPY.directory.disconnected;
    return (
      '<section class="state-card wide disconnected" role="status" data-state="disconnected">' +
      '<div class="state-icon" aria-hidden="true">↺</div><h2>' +
      esc(copy.title) +
      "</h2><p>" +
      esc(copy.body) +
      "</p>" +
      button(copy.action, "connect-notion", {
        primary: true,
        small: true,
        disabled: state.connection.state === "connecting"
      }) +
      "</section>"
    );
  }

  function loadingDirectoryState() {
    return (
      '<section class="state-card wide loading" role="status" aria-busy="true" data-state="loading">' +
      '<div class="state-icon" aria-hidden="true"><span class="spinner"></span></div><h2>' +
      esc(COPY.directory.loading) +
      "</h2></section>"
    );
  }

  function errorDirectoryState() {
    return (
      '<section class="state-card wide error" role="alert" data-state="error">' +
      '<div class="state-icon" aria-hidden="true">!</div><h2>' +
      esc(COPY.directory.error.title) +
      "</h2><p>" +
      esc(state.directoryError) +
      "</p>" +
      button(COPY.directory.error.action, "reload-directory", {
        primary: true,
        small: true
      }) +
      "</section>"
    );
  }

  function emptyDirectoryState() {
    return (
      '<section class="state-card wide" data-state="empty"><div class="state-icon" aria-hidden="true">○</div><h2>' +
      esc(COPY.directory.empty.title) +
      "</h2><p>" +
      esc(COPY.directory.empty.body) +
      "</p></section>"
    );
  }

  function courseGrid() {
    var directory = state.directory;
    var cards = directory.courses
      .map(function (course) {
        var counts = COPY.directory.course_counts
          .replace("{lectures}", String(course.counts.lectures))
          .replace("{materials}", String(course.counts.materials));
        return (
          '<article class="course-card"><span class="course-meta">' +
          esc(course.scope) +
          "</span><h3>" +
          esc(course.title) +
          "</h3><p>" +
          esc(counts) +
          "</p></article>"
        );
      })
      .join("");
    return (
      '<section aria-labelledby="courses-title" data-state="ready">' +
      '<div class="section-label"><h2 id="courses-title">' +
      esc(COPY.directory.section) +
      "</h2><p>" +
      esc(directory.semester) +
      '</p></div><div class="course-grid">' +
      cards +
      "</div></section>"
    );
  }

  function dashboard() {
    var connected = state.connection.connected;
    var pill =
      '<span class="sync-pill' +
      (connected ? "" : " off") +
      '" data-field="connection-status"><span class="status-dot" aria-hidden="true"></span>' +
      esc(state.connection.status_label) +
      "</span>";
    var body;
    if (!connected) body = disconnectedDirectoryState();
    else if (state.directoryLoading) body = loadingDirectoryState();
    else if (state.directoryError) body = errorDirectoryState();
    else if (!state.directory || !state.directory.courses.length) body = emptyDirectoryState();
    else body = courseGrid();
    return shell(
      '<div class="topline"><div><h1 class="page-title">' +
        esc(COPY.directory.title) +
        '</h1><p class="page-desc">' +
        esc(COPY.directory.desc) +
        '</p></div><div class="top-actions">' +
        pill +
        "</div></div>" +
        body
    );
  }

  function render() {
    app.innerHTML = state.activated ? dashboard() : onboarding();
  }

  function showToast(message) {
    toastEl.textContent = message;
    toastEl.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastEl.classList.remove("show");
    }, 2800);
  }

  function focusCode() {
    var input = document.getElementById("code");
    if (input) input.focus();
  }

  /* -- actions ----------------------------------------------------------- */

  function activate() {
    var input = document.getElementById("code");
    var code = input ? input.value.trim() : "";
    if (!code) {
      // no request and no navigation for an empty code: the inline notice is
      // the whole outcome
      state.notice = COPY.onboarding.empty_code_notice;
      state.noticeKind = "error";
      render();
      focusCode();
      return Promise.resolve();
    }
    return requestJson("/api/platform/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code })
    }).then(function (result) {
      if (!result.ok) {
        state.notice =
          (result.body && result.body.detail) || COPY.onboarding.activate_failed;
        state.noticeKind = "error";
        render();
        focusCode();
        return;
      }
      state.activated = true;
      state.notice = "";
      state.noticeKind = "hint";
      return loadDirectory().then(function () {
        render();
        showToast(COPY.onboarding.activated_toast);
      });
    });
  }

  function disconnectNotion() {
    return requestJson("/api/connection/disconnect", { method: "POST" }).then(
      function (result) {
        if (result.ok && result.body) state.connection = result.body;
        state.directory = null;
        state.directoryError = "";
        render();
        if (!state.connection.connected) showToast(COPY.connection.disconnect_toast);
      }
    );
  }

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function finishConnect() {
    if (state.connection.connected) {
      return loadDirectory().then(function () {
        render();
        showToast(COPY.connection.reconnect_toast);
      });
    }
    render();
    return Promise.resolve();
  }

  function pollConnection() {
    stopPolling();
    var attempts = 0;
    pollTimer = window.setInterval(function () {
      attempts += 1;
      requestJson("/api/connection").then(function (result) {
        if (result.ok && result.body) state.connection = result.body;
        if (state.connection.state !== "connecting" || attempts >= POLL_MAX_ATTEMPTS) {
          stopPolling();
          finishConnect();
        }
      });
    }, POLL_INTERVAL_MS);
  }

  function connectNotion() {
    return requestJson("/api/connection/connect", { method: "POST" }).then(
      function (result) {
        if (result.status === 409) {
          showToast(COPY.connection.busy);
          return;
        }
        if (result.ok && result.body) state.connection = result.body;
        if (state.connection.connected) return finishConnect();
        render();
        pollConnection();
      }
    );
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target || target.disabled) return;
    var action = target.dataset.action;
    if (action === "activate") activate();
    else if (action === "revoke-notion") disconnectNotion();
    else if (action === "connect-notion") connectNotion();
    else if (action === "reload-directory") loadDirectory().then(render);
  });

  document.addEventListener("submit", function (event) {
    event.preventDefault();
    if (event.target.id === "activation-form") activate();
  });

  loadQuota()
    .then(loadConnection)
    .then(function () {
      if (state.activated) return loadDirectory();
      return null;
    })
    .then(render)
    .catch(function () {
      // an unreachable loopback API must not leave a blank surface
      render();
    });
})();
