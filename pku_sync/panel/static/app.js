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
    quota: null,
    organize: { open: false, working: false, courseId: "", lectureId: "", result: null, blocked: "" },
    exerciseLaunch: null,
    grading: { exerciseId: null, title: "", status: "idle", jobId: null, result: null, blocked: "", unanswered: [] },
    connection: {
      state: "disconnected",
      connected: false,
      status_label: COPY.connection.disconnected,
      error: null
    },
    directory: null,
    directoryError: "",
    directoryLoading: false,
    syncCompleted: false,
    view: "dashboard",
    courseId: null,
    lectureId: null,
    materialView: "lecture",
    materials: null,
    materialsError: "",
    materialsLoading: false,
    overview: null,
    overviewError: "",
    overviewLoading: false,
    notice: "",
    noticeKind: "hint"
  };

  var GLYPH = {
    home: "⌂",
    courses: "▦",
    sync: "↻",
    notes: "✎",
    arrow: "→",
    launch: "↗",
    book: "▤",
    quiz: "✓"
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function template(text, values) {
    return String(text === null || text === undefined ? "" : text).replace(
      /\{(\w+)\}/g,
      function (whole, key) {
        return values && values[key] !== undefined ? String(values[key]) : "";
      }
    );
  }

  /* Multi-line approved copy keeps its line breaks without allowing markup. */
  function lines(text) {
    return String(text === null || text === undefined ? "" : text)
      .split("\n")
      .map(esc)
      .join("<br>");
  }

  function glyph(name) {
    return '<span aria-hidden="true">' + esc(GLYPH[name] || "·") + "</span>";
  }

  function button(label, action, options) {
    var opts = options || {};
    var classes = ["btn"];
    if (opts.primary) classes.push("btn-primary");
    if (opts.small) classes.push("btn-small");
    if (opts.quiet) classes.push("btn-quiet");
    var extra = "";
    if (opts.attrs) {
      for (var key in opts.attrs) {
        if (Object.prototype.hasOwnProperty.call(opts.attrs, key)) {
          extra += " " + key + '="' + esc(opts.attrs[key]) + '"';
        }
      }
    }
    return (
      '<button type="button" class="' +
      classes.join(" ") +
      '" data-action="' +
      esc(action) +
      '"' +
      extra +
      (opts.disabled ? " disabled" : "") +
      ">" +
      (opts.icon ? glyph(opts.icon) : "") +
      esc(label) +
      "</button>"
    );
  }

  /* -- time readouts (never a frozen timestamp in the markup) ------------- */

  function parseTime(value) {
    if (!value) return null;
    var when = new Date(value);
    return isNaN(when.getTime()) ? null : when;
  }

  function relativeParts(iso) {
    var copy = COPY.time;
    var when = parseTime(iso);
    if (!when) return { value: copy.never, unit: "" };
    var diff = Date.now() - when.getTime();
    if (diff < 0) diff = 0;
    var minutes = Math.floor(diff / 60000);
    if (minutes < 1) return { value: copy.just_now, unit: "" };
    if (minutes < 60) return { value: String(minutes), unit: copy.minutes };
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return { value: String(hours), unit: copy.hours };
    return { value: String(Math.floor(hours / 24)), unit: copy.days };
  }

  function relativeText(iso) {
    var parts = relativeParts(iso);
    return parts.unit ? parts.value + " " + parts.unit : parts.value;
  }

  function sameDay(left, right) {
    return (
      left.getFullYear() === right.getFullYear() &&
      left.getMonth() === right.getMonth() &&
      left.getDate() === right.getDate()
    );
  }

  function friendlyTime(iso) {
    var when = parseTime(iso);
    if (!when) return "";
    var pad = function (value) {
      return (value < 10 ? "0" : "") + value;
    };
    var stamp = pad(when.getHours()) + ":" + pad(when.getMinutes());
    var now = new Date();
    if (sameDay(when, now)) return COPY.time.today + " " + stamp;
    if (sameDay(when, new Date(now.getTime() - 86400000))) {
      return COPY.time.yesterday + " " + stamp;
    }
    return (
      template(COPY.time.date, { month: when.getMonth() + 1, day: when.getDate() }) +
      " " +
      stamp
    );
  }

  function dayLabel(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
    if (!match) return "";
    return template(COPY.time.full_date, {
      year: Number(match[1]),
      month: Number(match[2]),
      day: Number(match[3])
    });
  }

  /* The lecture hint's topic: the scheduled day/period when Notion's title
     carries them, otherwise the title's own scope tail — never invented. */
  function lectureTopic(lecture) {
    var bits = [];
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(lecture.date || ""));
    if (match) {
      bits.push(
        template(COPY.time.date, { month: Number(match[2]), day: Number(match[3]) })
      );
    }
    if (lecture.period) bits.push(lecture.period);
    if (bits.length) return bits.join(" ");
    var parts = String(lecture.title || "").split("·");
    var tail = parts.length > 1 ? parts.slice(1).join("·") : "";
    tail = tail
      .replace(/[（(][^）)]*[）)]/g, "")
      .replace(/[》」』]/g, "")
      .trim();
    return tail || lecture.scope || "";
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
      state.quota = result.ok && result.body ? result.body : null;
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

  function materialsUrl(courseId, view, lectureId) {
    var url =
      "/api/courses/" + encodeURIComponent(courseId) + "/materials?view=" +
      encodeURIComponent(view);
    if (view === "lecture") url += "&lecture_id=" + encodeURIComponent(lectureId || "");
    return url;
  }

  function loadMaterials() {
    if (!state.courseId) return Promise.resolve();
    if (state.materialView === "lecture" && !state.lectureId) return Promise.resolve();
    state.materialsLoading = true;
    state.materialsError = "";
    render();
    return requestJson(
      materialsUrl(state.courseId, state.materialView, state.lectureId)
    ).then(function (result) {
      state.materialsLoading = false;
      if (result.ok && result.body) {
        state.materials = result.body;
        state.materialsError = "";
      } else {
        state.materials = null;
        state.materialsError =
          (result.body && result.body.detail) || COPY.lecture.materials.error;
      }
      render();
    });
  }

  /* The course screen's 课程资料概览 rows ARE the 按资料类型 groups, so the
     overview and the type view can never disagree about a count. */
  function loadOverview() {
    if (!state.courseId) return Promise.resolve();
    state.overviewLoading = true;
    state.overviewError = "";
    render();
    return requestJson(materialsUrl(state.courseId, "type")).then(function (result) {
      state.overviewLoading = false;
      if (result.ok && result.body) {
        state.overview = result.body;
        state.overviewError = "";
      } else {
        state.overview = null;
        state.overviewError =
          (result.body && result.body.detail) || COPY.course.materials.error;
      }
      render();
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

  function quotaCard() {
    var quota = state.quota;
    if (quota === null) return '<div class="account-card" data-block="quota"><p>账户未激活</p></div>';
    if (quota.active === false) return '<div class="account-card" data-block="quota"><p>账户未激活</p></div>';
    if (quota.available === false) return '<div class="account-card" data-block="quota"><p>额度暂时无法读取</p></div>';
    return '<div class="account-card" data-block="quota"><p>转写 ' + esc(Math.floor((quota.transcribe_seconds_remaining === undefined ? 0 : quota.transcribe_seconds_remaining) / 60)) + ' 分钟</p><p>AI 点 ' + esc(quota.llm_points_remaining) + '</p></div>';
  }

  function shell(content) {
    return (
      '<div class="app-shell"><aside class="sidebar" aria-label="' +
      esc(COPY.nav.label) +
      '">' +
      brand(false) +
      '<p class="side-kicker">' +
      esc(COPY.nav.kicker) +
      '</p><nav class="nav"><button type="button" class="' +
      (state.view === "dashboard" ? "active" : "") +
      '" data-nav="dashboard" data-action="open-dashboard"' +
      (state.view === "dashboard" ? ' aria-current="page"' : "") +
      '><span class="nav-icon" aria-hidden="true">' +
      esc(GLYPH.home) +
      "</span>" +
      esc(COPY.nav.dashboard) +
      '</button></nav><div class="sidebar-spacer"></div>' +
      connectionCard() +
      quotaCard() +
      '</aside><header class="mobile-topbar"><div class="mobile-topbar-left">' +
      '<button type="button" class="mobile-overview" data-nav="dashboard" data-action="open-dashboard" aria-label="' +
      esc(COPY.nav.dashboard) +
      '"' +
      (state.view === "dashboard" ? ' aria-current="page"' : "") +
      '><span aria-hidden="true">' +
      esc(GLYPH.home) +
      '</span><span>' +
      esc(COPY.nav.dashboard) +
      '</span></button>' +
      brand(true) +
      '</div><div class="mobile-connection">' +
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
      "</p>" +
      button(COPY.directory.empty.action, "reload-directory", { primary: true, small: true }) +
      "</section>"
    );
  }

  function successDirectoryState() {
    return (
      '<section class="state-card wide" role="status" data-state="success"><div class="state-icon" aria-hidden="true">✓</div><h2>' +
      esc(COPY.directory.success.title) +
      "</h2>" +
      button(COPY.directory.success.action, "dismiss-sync-success", { primary: true, small: true }) +
      "</section>"
    );
  }

  /* -- dashboard --------------------------------------------------------- */

  function statCard(key, tone, symbol, label, value, unit, foot) {
    return (
      '<article class="stat-card ' +
      tone +
      '" data-stat="' +
      esc(key) +
      '"><div class="stat-top"><span>' +
      esc(label) +
      '</span><span class="stat-symbol" aria-hidden="true">' +
      esc(GLYPH[symbol]) +
      '</span></div><strong class="stat-value" data-field="value">' +
      esc(value) +
      (unit ? "<span>" + esc(unit) + "</span>" : "") +
      '</strong><span class="stat-foot">' +
      esc(foot) +
      "</span></article>"
    );
  }

  function statsSection() {
    var stats = state.directory.stats;
    var copy = COPY.dashboard.stats;
    var sync = relativeParts(state.directory.sync.last_sync_at);
    return (
      '<section aria-labelledby="overview-title">' +
      '<div class="section-label"><h2 id="overview-title">' +
      esc(COPY.dashboard.overview.title) +
      "</h2><p>" +
      esc(COPY.dashboard.overview.note) +
      '</p></div><div class="stats-grid">' +
      statCard(
        "lectures",
        "green",
        "book",
        copy.lectures.label,
        stats.lectures,
        copy.lectures.unit,
        template(copy.lectures.foot, { courses: stats.courses })
      ) +
      statCard(
        "materials",
        "blue",
        "courses",
        copy.materials.label,
        stats.materials,
        copy.materials.unit,
        copy.materials.foot
      ) +
      statCard(
        "sync",
        "gold",
        "sync",
        copy.sync.label,
        sync.value,
        sync.unit,
        copy.sync.foot
      ) +
      "</div></section>"
    );
  }

  function organizeCard() {
    var copy = COPY.directory.exercises.organize;
    var courses = state.directory.courses || [];
    var selectedCourse = courses.find(function (course) { return course.id === state.organize.courseId; });
    var lectures = selectedCourse ? (selectedCourse.lectures || []) : [];
    var balance = state.quota && state.quota.llm_points_remaining;
    var insufficient = typeof balance === "number" && balance < 5;
    var status = "";
    if (state.organize.working) {
      status = '<div class="organize-status" aria-live="polite" aria-busy="true"><span class="spinner" aria-hidden="true"></span><strong>' + esc(copy.working) + '</strong></div>';
    } else if (state.organize.blocked) {
      status = '<div class="organize-status error" role="alert"><strong>' + esc(copy.blocked) + '</strong><p>' + esc(state.organize.blocked) + '</p>' + button(copy.retry, "organize-retry", { small: true, primary: true }) + '</div>';
    } else if (state.organize.result) {
      status = '<div class="organize-status success" aria-live="polite"><strong>' + esc(copy.completed) + '</strong><span class="cost-pill">' + esc(template(copy.settlement, { points: state.organize.result.points_charged })) + '</span></div>';
    }
    if (!state.organize.open) {
      return '<section class="exercise-toolbar" aria-labelledby="organize-title"><div><h2 id="organize-title">' + esc(copy.title) + '</h2><p>' + esc(copy.body) + '</p></div><div class="toolbar-actions">' + button(copy.action, "organize-open", { primary: true, icon: "quiz" }) + '<span class="cost-pill">' + esc(copy.estimate) + '</span></div>' + status + '</section>';
    }
    var courseOptions = '<option value="">' + esc(copy.course_label) + '</option>' + courses.map(function (course) { return '<option value="' + esc(course.id) + '"' + (course.id === state.organize.courseId ? ' selected' : '') + '>' + esc(course.title) + '</option>'; }).join("");
    var lectureOptions = '<option value="">' + esc(copy.lecture_label) + '</option>' + lectures.map(function (lecture) { return '<option value="' + esc(lecture.id) + '"' + (lecture.id === state.organize.lectureId ? ' selected' : '') + '>' + esc(lecture.title) + '</option>'; }).join("");
    return '<section class="exercise-toolbar organize-confirm" aria-labelledby="organize-scope-title"><div><h2 id="organize-scope-title">' + esc(copy.scope_label) + '</h2><p>' + esc(copy.body) + '</p><div class="scope-fields"><label>' + esc(copy.course_label) + '<select data-organize-field="course">' + courseOptions + '</select></label><label>' + esc(copy.lecture_label) + '<select data-organize-field="lecture">' + lectureOptions + '</select></label></div></div><div class="toolbar-actions"><span class="cost-pill">' + esc(copy.estimate) + '</span>' + (insufficient ? '<p class="insufficient" role="alert">' + esc(copy.insufficient) + '</p>' : '') + button(copy.confirm, "organize-confirm", { primary: true, disabled: insufficient || !state.organize.courseId || !state.organize.lectureId || state.organize.working }) + button(copy.cancel, "organize-cancel", { quiet: true, disabled: state.organize.working }) + '</div>' + status + '</section>';
  }
  function exerciseAction(row) {
    var actions = COPY.directory.exercises.actions;
    return actions[row.status] ? actions[row.status] : actions.organized;
  }

  function exerciseLaunchFeedback(row) {
    var current = state.exerciseLaunch;
    var copy = COPY.directory.exercises.launch;
    if (!current || current.exerciseId !== row.id) return "";
    if (current.status === "opening") {
      return '<div class="exercise-launch-feedback" role="status" aria-live="polite">' + esc(copy.opening) + '</div>';
    }
    if (current.status === "failed") {
      return '<div class="exercise-launch-feedback error" role="alert" aria-live="polite"><strong>' + esc(copy.failed_title) + '</strong><span>' + esc(copy.failed_body) + '</span>' + button(copy.retry, "retry-exercise-launch", { small: true, primary: true, attrs: { "data-exercise": row.id, "data-purpose": current.purpose } }) + '</div>';
    }
    return "";
  }

  function missingExerciseIdentity(row) {
    var copy = COPY.directory.exercises.launch;
    return '<div class="exercise-mapping-missing" role="status" data-state="page_identity_missing"><strong>' + esc(copy.missing_title) + '</strong><span>' + esc(copy.missing_body) + '</span>' + button(copy.fallback, "launch-exercise-fallback", { small: true, attrs: { "data-exercise": row.id } }) + '</div>';
  }
  function gradingScreen() {
    return gradingCard();
  }

  function gradingCard() {
    var g = state.grading;
    if (!g || g.status === "idle") return "";
    var copy = COPY.directory.exercises.grading;
    if (g.status === "confirm") {
      return '<section class="exercise-toolbar grading-card" role="dialog" aria-live="polite"><strong>确认重新批改「' + esc(g.title) + '」？</strong><span>预计 1–5 AI 点 · 完成后按实际用量结算</span>' + button("确认重新批改", "start-regrade", { attrs: { "data-exercise": g.exerciseId } }) + button("取消", "grading-back", {}) + '</section>';
    }
    if (g.status === "running") {
      return '<section class="exercise-toolbar grading-card" aria-busy="true" aria-live="polite" role="status"><span class="spinner" aria-hidden="true"></span><strong>' + esc(template(copy.working, { title: g.title })) + '</strong><span>' + esc(copy.explanation) + '</span><span class="cost-pill">' + esc(copy.estimate) + '</span></section>';
    }
    if (g.status === "blocked") {
      return '<section class="exercise-toolbar grading-card error" role="alert" aria-live="polite"><strong>' + esc(copy.blocked) + '</strong><span>' + esc(g.blocked) + '</span>' + (g.unanswered.length ? '<span>未回答：' + esc(g.unanswered.join("、")) + '</span>' : '') + button(copy.back, "grading-back", { small: true, primary: true }) + '</section>';
    }
    if (g.status === "failed") {
      return '<section class="exercise-toolbar grading-card error" role="alert" aria-live="polite"><strong>' + esc(copy.failed) + '</strong><span>' + esc(g.blocked) + '</span>' + button(copy.retry, "grading-retry", { small: true, primary: true, attrs: { "data-exercise": g.exerciseId } }) + button(copy.back, "grading-back", { small: true, quiet: true }) + '</section>';
    }
    if (g.status === "completed" && g.result) {
      var r = g.result;
      return '<section class="exercise-toolbar grading-summary" aria-live="polite"><strong>' + esc(copy.completed) + '</strong><h2>' + esc(r.title) + '</h2><p>得分：' + esc(r.score) + '</p><p>批改时间：' + esc(r.graded_at) + '</p><span class="cost-pill">' + esc(template(copy.settlement, { points: r.points_charged })) + '</span><div class="toolbar-actions">' + button(copy.result, "grading-result", { primary: true, attrs: { "data-url": r.result_page_url } }) + button(copy.back, "grading-back", { quiet: true }) + '</div></section>';
    }
    return "";
  }

  function exerciseDirectorySection() {
    var rows = state.directory.exercises ? state.directory.exercises : [];
    var copy = COPY.directory.exercises;
    if (!rows.length) return '<section aria-labelledby="exercise-directory-title" data-state="empty-exercises"><div class="section-label"><h2 id="exercise-directory-title">' + esc(copy.title) + '</h2><p>' + esc(copy.note) + '</p></div><div class="exercise-empty">' + esc(copy.empty) + '</div></section>';
    var items = rows.map(function (row) {
      var purpose = row.status === "graded" ? "result" : "answer";
      var actionName = row.status === "pending-grade" ? "grade-exercise" : "launch-exercise";
      var identityMissing = !row.url;
      var action = button(exerciseAction(row), actionName, { primary: row.status === "pending-grade" || row.status === "graded", small: true, disabled: !state.connection.connected || identityMissing || state.grading.status === "running", attrs: { "data-exercise": row.id, "data-target": row.id, "data-purpose": purpose } });
      var estimate = row.status === "pending-grade" ? '<span class="cost-pill">预计 1–5 AI 点</span>' : '';
      var regradeDisabled = !state.connection.connected || identityMissing || state.grading.status === "running";
      var regrade = row.status === "graded" ? button(COPY.directory.exercises.regrade ? COPY.directory.exercises.regrade : "重新批改", "confirm-regrade", { small: true, quiet: true, disabled: regradeDisabled, attrs: { "data-exercise": row.id } }) : '';
      var mismatch = row.mismatch_notice ? '<p role="alert">本地批改记录与 Notion 标记不一致，请手动确认。</p>' : '';
      return '<article class="exercise-row" role="listitem" data-exercise-row="' + esc(row.id) + '"><div class="exercise-main"><div class="exercise-title-wrap"><h3>' + esc(row.title) + '</h3><span class="exercise-status ' + esc(row.status) + '" data-status="' + esc(row.status) + '">' + esc(row.status_label) + '</span></div><p class="exercise-meta">' + esc(row.course) + ' \u00b7 ' + esc(row.scope) + '</p>' + (identityMissing ? missingExerciseIdentity(row) : exerciseLaunchFeedback(row)) + mismatch + '</div><div class="exercise-actions">' + action + regrade + estimate + '</div></article>';
    }).join("");
    return '<section aria-labelledby="exercise-directory-title"><div class="section-label"><h2 id="exercise-directory-title">' + esc(copy.title) + '</h2><p>' + esc(copy.note) + '</p></div><div class="exercise-directory" role="list" aria-label="' + esc(copy.list_label) + '">' + items + '</div></section>';
  }
  function courseGrid() {
    var directory = state.directory;
    var synced = relativeText(directory.sync.last_sync_at);
    var cards = directory.courses
      .map(function (course) {
        var counts = template(COPY.directory.course_counts, {
          lectures: course.counts.lectures,
          materials: course.counts.materials,
          synced: synced
        });
        return (
          '<article class="course-card" data-course="' +
          esc(course.id) +
          '"><span class="course-icon" aria-hidden="true">' +
          esc(GLYPH.book) +
          '</span><div class="course-content"><span class="course-meta">' +
          esc(course.scope) +
          "</span><h3>" +
          esc(course.title) +
          '</h3><p data-field="course-counts">' +
          esc(counts) +
          "</p></div>" +
          button(COPY.dashboard.course_action, "open-course", {
            small: true,
            attrs: { "data-course": course.id }
          }) +
          "</article>"
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

  function activitySection() {
    var copy = COPY.dashboard.activity;
    var entries = state.directory.activity || [];
    if (!entries.length) return "";
    var rows = entries
      .map(function (entry) {
        return (
          '<div class="activity-item" data-activity="' +
          esc(entry.id) +
          '"><span class="activity-bullet" aria-hidden="true">' +
          esc(GLYPH.sync) +
          "</span><span><strong>" +
          esc(template(copy.item, { title: entry.title })) +
          "</strong><small>" +
          esc(template(copy.detail, { course: entry.course, type: entry.type })) +
          "</small></span><time>" +
          esc(friendlyTime(entry.updated)) +
          "</time></div>"
        );
      })
      .join("");
    return (
      '<section aria-labelledby="activity-title">' +
      '<div class="section-label"><h2 id="activity-title">' +
      esc(copy.title) +
      "</h2><p>" +
      esc(copy.note) +
      '</p></div><div class="activity-list">' +
      rows +
      "</div></section>"
    );
  }

  function topPill() {
    if (!state.connection.connected || !state.directory) {
      return (
        '<span class="sync-pill' +
        (state.connection.connected ? "" : " off") +
        '" data-field="connection-status"><span class="status-dot" aria-hidden="true"></span>' +
        esc(state.connection.status_label) +
        "</span>"
      );
    }
    return (
      '<span class="sync-pill" data-field="sync-pill">' +
      '<span class="status-dot" aria-hidden="true"></span>' +
      esc(
        template(COPY.dashboard.sync_pill, {
          relative: relativeText(state.directory.sync.last_sync_at)
        })
      ) +
      "</span>"
    );
  }

  function directoryBody() {
    if (!state.connection.connected) return disconnectedDirectoryState();
    if (state.directoryLoading) return loadingDirectoryState();
    if (state.directoryError) return errorDirectoryState();
    if (!state.directory || !state.directory.courses.length) {
      return emptyDirectoryState();
    }
    if (state.syncCompleted) return successDirectoryState();
    return null;
  }

  function dashboardScreen() {
    if (state.grading.status !== "idle") return gradingScreen();
    var fallback = directoryBody();
    var body = fallback
      ? fallback
      : statsSection() + organizeCard() + gradingCard() + exerciseDirectorySection() + courseGrid() + activitySection();
    return (
      '<div class="topline"><div><h1 class="page-title">' +
      esc(COPY.directory.title) +
      '</h1><p class="page-desc">' +
      esc(COPY.directory.desc) +
      '</p></div><div class="top-actions">' +
      topPill() +
      (fallback
        ? ""
        : button(COPY.dashboard.sync_action, "reload-directory", {
            primary: true,
            icon: "sync"
          })) +
      "</div></div>" +
      body
    );
  }

  /* -- course detail ----------------------------------------------------- */

  function currentCourse() {
    if (!state.directory || !state.courseId) return null;
    var courses = state.directory.courses;
    for (var i = 0; i < courses.length; i += 1) {
      if (courses[i].id === state.courseId) return courses[i];
    }
    return null;
  }

  function currentLecture() {
    var course = currentCourse();
    if (!course || !state.lectureId) return null;
    for (var i = 0; i < course.lectures.length; i += 1) {
      if (course.lectures[i].id === state.lectureId) return course.lectures[i];
    }
    return null;
  }

  function breadcrumb(trail) {
    var parts = trail.map(function (item) {
      if (!item.action) return "<span>" + esc(item.label) + "</span>";
      var attrs = "";
      if (item.course) attrs = ' data-course="' + esc(item.course) + '"';
      return (
        '<button type="button" data-action="' +
        esc(item.action) +
        '"' +
        attrs +
        ">" +
        esc(item.label) +
        "</button>"
      );
    });
    return (
      '<div class="breadcrumb">' +
      parts.join('<span aria-hidden="true">/</span>') +
      "</div>"
    );
  }

  function lectureHint(lecture) {
    var copy = COPY.course.lectures;
    if (lecture.mapping_state !== "mapped") return copy.hint_unmapped;
    return template(copy.hint_mapped, {
      topic: lectureTopic(lecture),
      count: lecture.counts.related_materials
    });
  }

  function lectureList(course) {
    var copy = COPY.course.lectures;
    if (!course.lectures.length) {
      return '<div class="missing-lecture-entry" data-state="missing-page"><strong>' + esc(copy.missing_page) + '</strong><small>' + esc(copy.missing_page_copy) + '</small>' + button(copy.missing_page_fallback, "launch", { small: true, primary: true, attrs: { "data-kind": "course", "data-target": course.id } }) + '</div>';
    }
    var items = course.lectures
      .map(function (lecture) {
        var number = String(lecture.number);
        if (number.length < 2) number = "0" + number;
        if (lecture.page_state === "missing") {
          return '<div class="missing-lecture-entry" data-state="missing-page" data-lecture="' + esc(lecture.id) + '"><strong>' + esc(copy.missing_page) + '</strong><small>' + esc(lecture.title) + '</small><small>' + esc(copy.missing_page_copy) + '</small>' + button(COPY.lecture.open_lecture, "launch", { small: true, disabled: true, attrs: { "data-kind": "lecture", "data-target": lecture.id } }) + button(copy.missing_page_fallback, "launch", { small: true, attrs: { "data-kind": "course", "data-target": course.id } }) + '</div>';
        }
        return (
          '<button type="button" class="lecture-item" data-action="select-lecture" data-course="' +
          esc(course.id) +
          '" data-lecture="' +
          esc(lecture.id) +
          '"><span class="lecture-number">' +
          esc(number) +
          "</span><span><strong>" +
          esc(lecture.title) +
          '</strong><small data-field="lecture-hint">' +
          esc(lectureHint(lecture)) +
          '</small></span><span class="item-arrow" aria-hidden="true">' +
          esc(GLYPH.arrow) +
          "</span></button>"
        );
      })
      .join("");
    return '<div class="lecture-list">' + items + "</div>";
  }

  function materialOverview(course) {
    var copy = COPY.course.materials;
    var body;
    if (state.overviewLoading) {
      body =
        '<div class="empty-materials" role="status" aria-busy="true"><strong>' +
        esc(copy.loading) +
        "</strong></div>";
    } else if (state.overviewError) {
      body =
        '<div class="empty-materials" role="alert"><strong>' +
        esc(state.overviewError) +
        "</strong></div>";
    } else {
      var groups = (state.overview && state.overview.groups) || [];
      body = groups.length
        ? '<div class="material-summary">' +
          groups
            .map(function (group) {
              return (
                '<div class="material-summary-row" data-type="' +
                esc(group.type) +
                '"><span>' +
                esc(group.type) +
                "</span><strong>" +
                esc(template(copy.row_unit, { count: group.count })) +
                "</strong></div>"
              );
            })
            .join("") +
          "</div>"
        : '<div class="empty-materials"><strong>' + esc(copy.empty) + "</strong></div>";
    }
    return (
      '<aside class="panel" aria-labelledby="course-materials-title">' +
      '<div class="section-label" style="margin-top:0"><h2 id="course-materials-title">' +
      esc(copy.title) +
      "</h2><p>" +
      esc(copy.note) +
      '</p></div><div class="material-summary-total"><strong data-field="materials-total">' +
      esc(course.counts.materials) +
      "</strong><span>" +
      esc(copy.total_unit) +
      "</span></div>" +
      body +
      '<p class="material-source-note">' +
      lines(copy.source_note) +
      "</p></aside>"
    );
  }

  function courseScreen() {
    var course = currentCourse();
    if (!course) return dashboardScreen();
    var copy = COPY.course;
    return (
      breadcrumb([
        { label: copy.breadcrumb_home, action: "open-dashboard" },
        { label: copy.breadcrumb_courses }
      ]) +
      '<section class="course-hero" aria-labelledby="course-title"><div>' +
      '<span class="course-kicker">' +
      esc(template(copy.kicker, { scope: course.scope })) +
      '</span><h1 id="course-title">' +
      esc(course.title) +
      "</h1><p>" +
      esc(copy.desc) +
      '</p></div><div class="hero-actions">' +
      button(copy.sync_action, "reload-directory", { primary: true, icon: "sync" }) +
      "</div></section>" +
      '<div class="course-layout"><section class="panel" aria-labelledby="lecture-list-title">' +
      '<div class="section-label" style="margin-top:0"><h2 id="lecture-list-title">' +
      esc(copy.lectures.title) +
      "</h2><p>" +
      esc(template(copy.lectures.note, { lectures: course.counts.lectures })) +
      "</p></div>" +
      lectureList(course) +
      "</section>" +
      materialOverview(course) +
      "</div>"
    );
  }

  /* -- lecture detail ---------------------------------------------------- */

  function infoRow(label, value) {
    return (
      '<div class="material-summary-row" data-row="' +
      esc(label) +
      '"><span>' +
      esc(label) +
      "</span><strong>" +
      esc(value) +
      "</strong></div>"
    );
  }

  function lectureInfo(lecture) {
    var copy = COPY.lecture.info;
    var mapped = lecture.mapping_state === "mapped";
    var related =
      template(copy.related_value, { count: lecture.counts.related_materials }) +
      (mapped ? "" : copy.related_pending_suffix);
    var page =
      lecture.page_state === "ready"
        ? copy.page_ready + (mapped ? "" : copy.page_pending_suffix)
        : copy.page_missing;
    return (
      '<article class="panel" aria-labelledby="lecture-info-title">' +
      '<h2 id="lecture-info-title">' +
      esc(copy.title) +
      '</h2><div class="material-summary">' +
      infoRow(copy.date, dayLabel(lecture.date) || copy.unknown) +
      infoRow(copy.duration, copy.unknown) +
      infoRow(copy.sync, lecture.sync_label) +
      infoRow(copy.related, related) +
      infoRow(copy.page, page) +
      '</div><p class="material-source-note">' +
      lines(copy.source_note) +
      "</p></article>"
    );
  }

  function launchPanel(lecture) {
    var copy = COPY.lecture.launch;
    var item = function (entry, kind) {
      return (
        '<div class="launch-item"><div><strong>' +
        esc(entry.title) +
        "</strong><small>" +
        esc(entry.detail) +
        "</small></div>" +
        button(entry.action, "launch", {
          small: true,
          icon: kind === "notes" ? "notes" : "launch",
          disabled: lecture.page_state === "missing",
          attrs: { "data-kind": kind, "data-target": lecture.id }
        }) +
        "</div>"
      );
    };
    return (
      '<aside class="panel" aria-labelledby="launch-title"><h2 id="launch-title">' +
      esc(copy.title) +
      '</h2><div class="launch-list">' +
      item(copy.notes, "notes") +
      item(copy.quiz, "quiz") +
      '</div><p class="launch-hint">' +
      esc(copy.hint) +
      "</p></aside>"
    );
  }

  function launchFeedback() {
    var current = state.launch;
    if (!current) return "";
    var copy = COPY.launch;
    if (current.status === "opening") return '<div class="launch-feedback" role="status" aria-live="polite">' + esc(copy.opening) + '</div>';
    if (current.status === "missing") return '<div class="launch-feedback error" role="alert" aria-live="polite"><strong>' + esc(copy.missing_title) + '</strong><span>' + esc(copy.missing_body) + '</span>' + (current.fallback && current.fallback.url ? button(copy.fallback, "launch-fallback", { small: true, primary: true, attrs: { "data-url": current.fallback.url } }) : "") + '</div>';
    return '<div class="launch-feedback" role="alert" aria-live="polite"><strong>打开没有成功</strong><span>' + esc(copy.failed) + '</span>' + button(copy.retry, "retry-launch", { small: true, primary: true, attrs: { "data-kind": current.kind, "data-target": current.targetId } }) + '</div>';
  }

  function materialControls() {
    var copy = COPY.lecture.materials;
    var keys = ["lecture", "type", "all"];
    return (
      '<div class="material-controls" role="group" aria-label="' +
      esc(copy.controls_label) +
      '">' +
      keys
        .map(function (key) {
          var active = state.materialView === key;
          return (
            '<button type="button" class="material-view' +
            (active ? " active" : "") +
            '" data-material-view="' +
            key +
            '" aria-pressed="' +
            (active ? "true" : "false") +
            '">' +
            esc(copy.views[key]) +
            "</button>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  /* The per-row association label. “已关联本讲” requires an explicit link to
     the SELECTED lecture — an explicit link to another lecture is
     course-level from this lecture's perspective (approved prototype
     semantics) — and an inferred association stays visibly 待确认, never
     已关联本讲. The two 待确认 semantics stay structurally distinct: this
     badge under the title vs the 处理状态 pill in its own column. */
  function associationLabel(item) {
    var copy = COPY.lecture.materials;
    if (
      item.linked_to_lecture &&
      item.lecture &&
      state.lectureId &&
      item.lecture.id === state.lectureId
    ) {
      return { kind: "linked", text: copy.linked };
    }
    if (item.association_state === "待确认") {
      return { kind: "pending", text: copy.pending };
    }
    return { kind: "course", text: copy.course_level };
  }

  function materialRow(item) {
    var copy = COPY.lecture.materials;
    var assoc = associationLabel(item);
    var statusClasses = {
      "待阅读": "reading",
      "已索引": "indexed",
      "重点": "focus",
      "待确认": "pending"
    };
    // Regression guard: the old "indexed" : "pending" fallback is intentionally gone.
    var badge = statusClasses[item.status] ? statusClasses[item.status] : "reading";
    return (
      '<div class="material-row" data-material="' +
      esc(item.id) +
      '"><div class="material-title"><strong>' +
      esc(item.title) +
      "</strong>" +
      '<small class="assoc ' +
      assoc.kind +
      '" data-assoc="' +
      assoc.kind +
      '">' +
      esc(assoc.text) +
      '</small></div><span class="material-type">' +
      esc(item.type) +
      "</span>" +
      (item.status
        ? '<span class="status-badge ' + badge + '">' + esc(item.status) + "</span>"
        : "<span></span>") +
      '<span class="material-source">' +
      esc(template(copy.source, { source: item.source })) +
      "</span>" +
      button(copy.open, "launch", {
        small: true,
        icon: "launch",
        attrs: { "data-kind": "material", "data-target": item.id }
      }) +
      "</div>"
    );
  }

  function groupLabel(text, key) {
    return (
      '<p class="material-group-label' +
      (key === "pending" ? " pending" : "") +
      '" data-group="' +
      esc(key) +
      '">' +
      esc(text) +
      "</p>"
    );
  }

  function materialsBody() {
    var copy = COPY.lecture.materials;
    if (state.materialsLoading) {
      return (
        '<div class="empty-materials" role="status" aria-busy="true" data-state="loading"><strong>' +
        esc(copy.loading) +
        "</strong></div>"
      );
    }
    if (state.materialsError) {
      return (
        '<div class="empty-materials" role="alert" data-state="error"><strong>' +
        esc(state.materialsError) +
        "</strong></div>"
      );
    }
    var payload = state.materials;
    if (!payload) return "";
    if (payload.view === "lecture") {
      var confirmed = payload.confirmed || [];
      var inferred = payload.inferred || [];
      // three-way classification: a lecture with zero materials at all shows
      // the approved empty copy; an inferred-only (unmapped) lecture shows the
      // mapping-unconfirmed panel; a mapped lecture lists its confirmed items
      // with any inferred ones in a visually separated 待确认 subsection.
      if (!confirmed.length && !inferred.length) {
        return (
          '<div class="empty-materials" data-state="empty"><strong>' +
          esc(copy.empty.title) +
          "</strong>" +
          esc(copy.empty.body) +
          "</div>"
        );
      }
      if (!payload.mapped) {
        var unmapped = copy.unmapped;
        return (
          '<div class="empty-materials" data-state="unmapped"><strong>' +
          esc(unmapped.title) +
          "</strong>" +
          esc(unmapped.body) +
          '<div style="margin-top:12px">' +
          button(unmapped.action, "open-course", {
            small: true,
            attrs: { "data-course": state.courseId }
          }) +
          "</div></div>"
        );
      }
      return (
        '<div class="material-list" aria-live="polite">' +
        confirmed.map(materialRow).join("") +
        (inferred.length
          ? '<div class="pending-subsection" data-state="pending">' +
            groupLabel(
              template(copy.pending_group, { count: inferred.length }),
              "pending"
            ) +
            inferred.map(materialRow).join("") +
            "</div>"
          : "") +
        "</div>"
      );
    }
    if (payload.view === "type") {
      var groups = payload.groups || [];
      if (!groups.length) {
        return (
          '<div class="empty-materials" data-state="empty"><strong>' +
          esc(copy.empty.title) +
          "</strong>" +
          esc(copy.empty.body) +
          "</div>"
        );
      }
      return (
        '<div class="material-list" aria-live="polite">' +
        groups
          .map(function (group) {
            return (
              groupLabel(
                template(copy.group, { type: group.type, count: group.count }),
                group.type
              ) + group.items.map(materialRow).join("")
            );
          })
          .join("") +
        "</div>"
      );
    }
    var items = payload.items || [];
    if (!items.length) {
      return (
        '<div class="empty-materials" data-state="empty"><strong>' +
        esc(copy.empty.title) +
        "</strong>" +
        esc(copy.empty.body) +
        "</div>"
      );
    }
    return (
      '<div class="material-list" aria-live="polite">' +
      items.map(materialRow).join("") +
      "</div>"
    );
  }

  function materialsPanel() {
    var copy = COPY.lecture.materials;
    return (
      '<section class="materials-panel" aria-labelledby="related-materials-title">' +
      '<div class="materials-heading"><div><h2 id="related-materials-title">' +
      esc(copy.title) +
      "</h2><p>" +
      esc(copy.note) +
      "</p></div>" +
      materialControls() +
      "</div>" +
      materialsBody() +
      '<p class="materials-hint">' +
      esc(copy.hint) +
      "</p></section>"
    );
  }

  function lectureScreen() {
    var course = currentCourse();
    var lecture = currentLecture();
    if (!course) return dashboardScreen();
    if (!lecture) return courseScreen();
    var copy = COPY.lecture;
    return (
      breadcrumb([
        { label: COPY.course.breadcrumb_home, action: "open-dashboard" },
        { label: course.title, action: "open-course", course: course.id },
        { label: lecture.title }
      ]) +
      '<section class="detail-hero" aria-labelledby="lecture-title"><div>' +
      '<span class="lecture-status" data-field="lecture-sync">' +
      '<span class="status-dot" aria-hidden="true"></span>' +
      esc(lecture.sync_label) +
      '</span><h1 id="lecture-title">' +
      esc(lecture.title) +
      "</h1><p>" +
      lines(template(copy.desc, { course: course.title, scope: course.scope })) +
      '</p></div><div class="hero-actions">' +
      launchFeedback() +
      button(copy.open_lecture, "launch", {
        primary: true,
        icon: "launch",
        disabled: lecture.page_state === "missing",
        attrs: { "data-kind": "lecture", "data-target": lecture.id }
      }) +
      "</div></section>" +
      '<div class="detail-grid">' +
      lectureInfo(lecture) +
      launchPanel(lecture) +
      "</div>" +
      materialsPanel()
    );
  }

  function render() {
    if (!state.activated) {
      app.innerHTML = onboarding();
      return;
    }
    var screen;
    if (state.grading.status !== "idle") screen = gradingScreen();
    else if (directoryBody()) screen = dashboardScreen();
    else if (state.view === "course") screen = courseScreen();
    else if (state.view === "lecture") screen = lectureScreen();
    else screen = dashboardScreen();
    app.innerHTML = shell(screen);
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

  /* -- navigation -------------------------------------------------------- */

  function openDashboard() {
    state.launch = null;
    state.view = "dashboard";
    state.lectureId = null;
    state.materials = null;
    state.materialsError = "";
    render();
  }

  function openCourse(courseId) {
    state.launch = null;
    state.view = "course";
    state.courseId = courseId || state.courseId;
    state.overview = null;
    state.overviewError = "";
    render();
    return loadOverview();
  }

  function selectLecture(courseId, lectureId) {
    state.launch = null;
    state.view = "lecture";
    state.courseId = courseId || state.courseId;
    state.lectureId = lectureId;
    // selecting a lecture ALWAYS lands on 按讲次 — including re-selection
    // after the material view was switched
    state.materialView = "lecture";
    state.materials = null;
    state.materialsError = "";
    render();
    return loadMaterials();
  }

  function setMaterialView(view) {
    if (state.materialView === view) return Promise.resolve();
    state.materialView = view;
    state.materials = null;
    render();
    return loadMaterials();
  }

  function syncNow() {
    state.syncCompleted = false;
    return loadDirectory().then(function () {
      state.syncCompleted = Boolean(state.directory && state.directory.sync && state.directory.sync.state === "done");
      render();
      if (state.view === "course") return loadOverview();
      if (state.view === "lecture") return loadMaterials();
      return null;
    });
  }

  function organizeOpen() {
    state.organize.open = true;
    state.organize.blocked = "";
    if (!state.organize.courseId && state.directory.courses.length) state.organize.courseId = state.directory.courses[0].id;
    var course = state.directory.courses.find(function (item) { return item.id === state.organize.courseId; });
    if (course && !state.organize.lectureId && course.lectures.length) state.organize.lectureId = course.lectures[0].id;
    render();
  }

  function blockOrganize(reason) {
    state.organize.working = false;
    state.organize.blocked = reason || COPY.directory.exercises.organize.blocked;
    render();
  }

  function finishOrganize(result, jobId, attempts) {
    var copy = COPY.directory.exercises.organize;
    var pollAttempts = attempts || 0;
    if (!result.ok || !result.body || result.body.status === "blocked") {
      blockOrganize(result.body && (result.body.reason || result.body.detail) || copy.blocked);
      return Promise.resolve();
    }
    if (result.body.status === "running") {
      var activeJobId = jobId || result.body.job_id;
      if (!activeJobId || pollAttempts >= POLL_MAX_ATTEMPTS) {
        blockOrganize(copy.blocked);
        return Promise.resolve();
      }
      return new Promise(function (resolve) {
        window.setTimeout(function () {
          resolve(requestJson("/api/exercises/organize/status?job_id=" + encodeURIComponent(activeJobId))
            .then(function (next) { return finishOrganize(next, activeJobId, pollAttempts + 1); })
            .catch(function () { blockOrganize(copy.blocked); }));
        }, 250);
      });
    }
    if (result.body.status !== "completed") {
      blockOrganize(copy.blocked);
      return Promise.resolve();
    }
    state.organize.working = false;
    state.organize.result = result.body;
    if (state.quota && typeof result.body.points_remaining === "number") {
      state.quota.llm_points_remaining = result.body.points_remaining;
    }
    state.organize.open = false;
    return loadDirectory().then(render);
  }

  function organizeConfirm() {
    var copy = COPY.directory.exercises.organize;
    var balance = state.quota && state.quota.llm_points_remaining;
    if (typeof balance === "number" && balance < 5) {
      state.organize.blocked = copy.insufficient;
      render();
      return Promise.resolve();
    }
    state.organize.working = true;
    state.organize.blocked = "";
    state.organize.result = null;
    render();
    return requestJson("/api/exercises/organize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ course_id: state.organize.courseId, lecture_ids: [state.organize.lectureId] })
    }).then(function (result) { return finishOrganize(result, result.body && result.body.job_id); })
      .catch(function () { blockOrganize(copy.blocked); });
  }
  function confirmRegrade(targetId) {
    var rows = state.directory ? state.directory.exercises : [];
    var row = rows.find(function (item) { return item.id === targetId; });
    if (!state.connection.connected || !row || row.status !== "graded" || !row.url) return;
    state.grading = { exerciseId: targetId, title: row.title, status: "confirm", jobId: null, result: null, blocked: "", unanswered: [] };
    render();
  }
  function startGrading(targetId, regrade) {
    var row = (state.directory && state.directory.exercises || []).find(function (item) { return item.id === targetId; });
    if (!row || !row.url) return;
    state.grading = { exerciseId: targetId, title: row.title, status: "running", jobId: null, result: null, blocked: "", unanswered: [] };
    render();
    requestJson("/api/exercises/" + encodeURIComponent(targetId) + "/grade", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ regrade: Boolean(regrade), confirm: Boolean(regrade) }) }).then(function (result) {
      if (!result.ok || !result.body || result.body.status !== "running") {
        state.grading.status = result.body && result.body.status === "failed" ? "failed" : "blocked";
        state.grading.blocked = result.body && (result.body.reason || result.body.detail) || COPY.directory.exercises.grading.blocked;
        state.grading.unanswered = result.body && result.body.unanswered || []; render(); return;
      }
      state.grading.jobId = result.body.job_id; render();
      var attempts = 0;
      function pollGrade() {
        attempts += 1;
        requestJson("/api/exercises/grade/status?job_id=" + encodeURIComponent(state.grading.jobId)).then(function (next) {
          if (next.body && next.body.status === "running" && attempts < POLL_MAX_ATTEMPTS) { window.setTimeout(pollGrade, 250); return; }
          if (next.body && next.body.status === "completed") { state.grading.status = "completed"; state.grading.result = next.body; }
          else { state.grading.status = next.body && next.body.status === "failed" ? "failed" : "blocked"; state.grading.blocked = next.body && next.body.reason || COPY.directory.exercises.grading.blocked; state.grading.unanswered = next.body && next.body.unanswered || []; }
          if (next.body && typeof next.body.points_remaining === "number" && state.quota) {
            state.quota.llm_points_remaining = next.body.points_remaining;
          }
          render();
        }).catch(function () { state.grading.status = "blocked"; state.grading.blocked = COPY.directory.exercises.grading.blocked; render(); });
      }
      pollGrade();
    }).catch(function () { state.grading.status = "blocked"; state.grading.blocked = COPY.directory.exercises.grading.blocked; render(); });
  }

  function launchExerciseFallback(targetId) {
    return requestJson("/api/exercises/" + encodeURIComponent(targetId) + "/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purpose: "answer" })
    }).then(function (result) {
      if (result.body && result.body.fallback && result.body.fallback.url) {
        window.location.assign(result.body.fallback.url);
      }
    });
  }
  function launchExercise(targetId, purpose) {
    state.exerciseLaunch = { exerciseId: targetId, purpose: purpose, status: "opening" };
    render();
    return requestJson("/api/exercises/" + encodeURIComponent(targetId) + "/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purpose: purpose })
    }).then(function (result) {
      if (result.ok && result.body && result.body.url) {
        var navigate = function () {
          showToast(COPY.launch.opening);
          window.location.assign(result.body.url);
        };
        if (purpose === "answer") {
          // The launch endpoint records the answer-start event. Re-read the
          // metadata directory before navigating so the current row changes
          // to pending-answer without a manual reload when the panel remains visible.
          return loadDirectory().then(function () {
            render();
            navigate();
          }, navigate);
        }
        navigate();
        return;
      }
      state.exerciseLaunch = {
        exerciseId: targetId,
        purpose: purpose,
        status: result.body && result.body.status === "page_identity_missing" ? "missing" : "failed",
        fallback: result.body && result.body.fallback
      };
      render();
    }).catch(function () {
      state.exerciseLaunch = { exerciseId: targetId, purpose: purpose, status: "failed" };
      render();
    });
  }
  function launch(kind, targetId) {
    state.launch = { kind: kind, targetId: targetId, status: "opening" };
    render();
    return requestJson("/api/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_id: targetId,
        course_id: state.courseId ? state.courseId : null
      })
    }).then(function (result) {
      if (result.ok) {
        if (result.body) {
          if (result.body.url) {
            showToast(COPY.launch[kind] ? COPY.launch[kind] : COPY.launch.lecture);
            window.location.assign(result.body.url);
            return;
          }
        }
      }
      if (result.body) {
        if (result.body.status === "missing_mapping") {
          state.launch = { kind: kind, targetId: targetId, status: "missing", fallback: result.body.fallback };
        } else {
          state.launch = { kind: kind, targetId: targetId, status: "failed" };
        }
      } else {
        state.launch = { kind: kind, targetId: targetId, status: "failed" };
      }
      render();
    }).catch(function () {
      state.launch = { kind: kind, targetId: targetId, status: "failed" };
      render();
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
    else if (action === "reload-directory") syncNow();
    else if (action === "dismiss-sync-success") {
      state.syncCompleted = false;
      openDashboard();
    }
    else if (action === "open-dashboard") openDashboard();
    else if (action === "organize-open") organizeOpen();
    else if (action === "organize-confirm") organizeConfirm();
    else if (action === "organize-cancel") { state.organize.open = false; state.organize.blocked = ""; render(); }
    else if (action === "organize-retry") { state.organize.blocked = ""; state.organize.open = true; render(); }
    else if (action === "open-course") openCourse(target.dataset.course);
    else if (action === "select-lecture") {
      selectLecture(target.dataset.course, target.dataset.lecture);
    } else if (action === "launch-exercise") {
      launchExercise(target.dataset.target, target.dataset.purpose || "answer");
    } else if (action === "retry-exercise-launch") {
      launchExercise(target.dataset.exercise, target.dataset.purpose || "answer");
    } else if (action === "launch-exercise-fallback") {
      launchExerciseFallback(target.dataset.exercise);
    } else if (action === "grade-exercise") {
      startGrading(target.dataset.target, false);
    } else if (action === "confirm-regrade") {
      confirmRegrade(target.dataset.exercise);
    } else if (action === "start-regrade") {
      startGrading(target.dataset.exercise, true);
    } else if (action === "grading-retry") {
      startGrading(target.dataset.exercise || state.grading.exerciseId);
    } else if (action === "grading-back") {
      state.grading = { exerciseId: null, title: "", status: "idle", jobId: null, result: null, blocked: "", unanswered: [] }; render();
    } else if (action === "grading-result") {
      if (target.dataset.url) window.location.assign(target.dataset.url);
    } else if (action === "launch") {
      launch(target.dataset.kind, target.dataset.target);
    } else if (action === "retry-launch") {
      launch(target.dataset.kind, target.dataset.target);
    } else if (action === "launch-fallback") {
      if (target.dataset.url) window.location.assign(target.dataset.url);
    }
  });

  document.addEventListener("click", function (event) {
    var view = event.target.closest("[data-material-view]");
    if (view) setMaterialView(view.dataset.materialView);
  });

  document.addEventListener("change", function (event) {
    var field = event.target.closest("[data-organize-field]");
    if (!field) return;
    if (field.dataset.organizeField === "course") {
      state.organize.courseId = field.value;
      var course = state.directory.courses.find(function (item) { return item.id === field.value; });
      state.organize.lectureId = course && course.lectures.length ? course.lectures[0].id : "";
    } else {
      state.organize.lectureId = field.value;
    }
    render();
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
