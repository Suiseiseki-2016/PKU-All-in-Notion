"use strict";
/* Local course workspace and account setup. */

(function () {
  var COPY = window.PANEL_COPY;
  var app = document.getElementById("app");
  var toastEl = document.getElementById("toast");
  var toastTimer = null;
  var pollTimer = null;
  var notionParentsRead = null;
  var notionParentsEpoch = 0;
  var accountQuotaEpoch = 0;
  var renderedAccountEpoch = -1;
  var renderedAccountEmail = "";
  var settledQuotaRead = null;
  var POLL_INTERVAL_MS = 1500;
  var POLL_MAX_ATTEMPTS = 240;
  var welcomeKey = "pku-panel-welcome-v1";
  var welcomeOpen = true;
  try { welcomeOpen = window.localStorage.getItem(welcomeKey) !== "seen"; } catch (_) {}

  function welcomeScreen() {
    var copy = COPY.welcome;
    return '<main class="welcome-screen" aria-labelledby="welcome-heading">' + brand(false) +
      '<section class="welcome-intro"><p class="eyebrow">' + esc(copy.eyebrow) + '</p><h1 id="welcome-heading" tabindex="-1">' +
      esc(copy.title) + '</h1><p>' + esc(copy.description) + '</p></section><ol class="welcome-steps">' +
      copy.steps.map(function (step, index) {
        return '<li><span class="welcome-number" aria-hidden="true">' + (index + 1) + '</span><h2>' +
          esc(step.title) + '</h2><p>' + esc(step.body) + '</p></li>';
      }).join('') + '</ol><footer class="welcome-footer"><p>' + esc(copy.note) + '</p><div>' +
      button(copy.start, "welcome-start", { primary: true, icon: "arrow" }) +
      button(copy.skip, "welcome-skip", { quiet: true }) + '</div></footer></main>';
  }

  function closeWelcome(start) {
    welcomeOpen = false;
    try { window.localStorage.setItem(welcomeKey, "seen"); } catch (_) {}
    if (start && state.activated) state.view = "campus-account";
    render();
    var heading = document.querySelector('main h1, #auth-email');
    if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); }
    if (start && state.activated) {
      var settings = document.querySelector('.campus-settings');
      if (settings) settings.open = true;
      loadNotionParents();
    }
  }

  async function confirmAction(message) {
    // Tauri replaces window.confirm with a Promise. A Promise is truthy
    // before the user decides, so native actions must await a real choice.
    if (!window.__TAURI_INTERNALS__) return (await window.confirm(message)) === true;
    if (document.querySelector('[data-product-confirmation]')) return false;
    return new Promise(function (resolve) {
      var dialog = document.createElement('dialog');
      dialog.className = 'product-confirmation';
      dialog.setAttribute('data-product-confirmation', '');
      dialog.setAttribute('role', 'alertdialog');
      dialog.setAttribute('aria-labelledby', 'product-confirmation-title');
      dialog.setAttribute('aria-describedby', 'product-confirmation-message');
      var title = document.createElement('h2');
      title.id = 'product-confirmation-title';
      title.textContent = '请确认此操作';
      var text = document.createElement('p');
      text.id = 'product-confirmation-message';
      text.textContent = message;
      var controls = document.createElement('div');
      controls.className = 'product-confirmation-actions';
      var settled = false;
      function finish(accepted) {
        if (settled) return;
        settled = true;
        if (dialog.open) dialog.close();
        dialog.remove();
        resolve(accepted === true);
      }
      ['cancel', 'confirm'].forEach(function (choice) {
        var control = document.createElement('button');
        control.type = 'button';
        control.className = choice === 'confirm' ? 'btn primary' : 'btn';
        control.setAttribute('data-confirmation-decision', choice);
        control.textContent = choice === 'confirm' ? '确认' : '取消';
        control.addEventListener('click', function () { finish(choice === 'confirm'); });
        controls.appendChild(control);
      });
      dialog.addEventListener('cancel', function (event) { event.preventDefault(); finish(false); });
      dialog.addEventListener('close', function () { finish(false); });
      dialog.append(title, text, controls);
      document.body.appendChild(dialog);
      try {
        dialog.showModal();
        controls.querySelector('[data-confirmation-decision="cancel"]').focus();
      } catch (_) { finish(false); }
    });
  }

  var state = {
    activated: false,
    quota: null,
    quotaStale: false,
    authMode: "login",
    authPending: false,
    update: null,
    connection: {
      state: "disconnected",
      connected: false,
      status_label: COPY.connection.disconnected,
      error: null
    },
    view: "dashboard",
    campus: { courses: [], course: null, courseCache: {}, courseId: "", loading: false, error: "", configured: false, home: null, parents: [], parentId: "", catalogAutoStarted: false, parentError: "", query: "", category: "all", dateQuery: "", demoConnection: false, submission: { selectedId: "", draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } } },
    recordings: { configured: false, editingCredentials: false, loading: false, syncPending: false, backgroundPending: false, background: { state: "idle" }, error: "", task: { state: "idle" }, taskCourseId: "", noteStrategyByKey: {}, directOss: false },
    termReview: { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" },
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
      '<button type="' + (opts.type === "submit" ? "submit" : "button") + '" class="' +
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

  var accountRefreshPending = false;
  var redeemPending = false;

  function loadQuota() {
    var epoch = ++accountQuotaEpoch;
    state.campus.courseCache = {};
    state.campus.course = null;
    return requestJson("/api/platform/quota").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      state.activated = Boolean(result.ok && result.body && result.body.active);
      state.quota = result.ok && result.body ? result.body : null;
      state.quotaStale = false;
    });
  }

  function refreshSettledQuota() {
    if (!state.activated || !state.quota) return Promise.resolve();
    var epoch = accountQuotaEpoch;
    var email = accountEmail();
    if (settledQuotaRead && settledQuotaRead.epoch === epoch) return settledQuotaRead.promise;
    var read = { epoch: epoch, promise: null };
    function currentAccount() {
      return epoch === accountQuotaEpoch && state.activated && accountEmail() === email;
    }
    function stale() {
      if (!currentAccount()) return;
      state.quotaStale = true;
      render();
    }
    read.promise = requestJson("/api/platform/quota").then(function (result) {
      if (!currentAccount()) return;
      var quota = result.body;
      if (!result.ok || !quota || !quota.active || quota.available === false ||
          String(quota.email || "") !== email ||
          typeof quota.llm_points_remaining !== "number" || !Number.isFinite(quota.llm_points_remaining) ||
          typeof quota.transcribe_seconds_remaining !== "number" || !Number.isFinite(quota.transcribe_seconds_remaining)) {
        stale();
        return;
      }
      state.quota = quota;
      state.quotaStale = false;
      render();
    }).catch(stale).then(function () {
      if (settledQuotaRead === read) settledQuotaRead = null;
    });
    settledQuotaRead = read;
    return read.promise;
  }

  function refreshAccount() {
    if (state.quotaStale) return refreshSettledQuota();
    if (!state.activated || !state.quota || !state.quota.email || accountVerified() || accountRefreshPending) {
      return Promise.resolve();
    }
    accountRefreshPending = true;
    return requestJson("/api/auth/me").then(function (result) {
      if (!result.ok || !result.body || !result.body.available) return;
      if (result.body.email !== state.quota.email) return;
      var wasVerified = accountVerified();
      state.quota.email_verified = Boolean(result.body.email_verified);
      if (!wasVerified && accountVerified()) {
        render();
        showToast(COPY.account.verified_toast);
      }
    }).catch(function () {
      // A temporary relay failure leaves the last known state visible.
    }).then(function () {
      accountRefreshPending = false;
    });
  }

  function accountEmail() {
    return state.quota && state.quota.email ? String(state.quota.email) : "";
  }

  function accountVerified() {
    if (!state.quota) return false;
    if (state.quota.legacy_activate) return true;
    if (!state.quota.email) return true;
    return Boolean(state.quota.email_verified);
  }

  function canRedeem() {
    return state.activated && accountVerified();
  }

  function loadUpdate() {
    return requestJson("/api/update").then(function (result) {
      state.update = result.ok && result.body ? result.body : null;
    });
  }

  function loadConnection() {
    return requestJson("/api/connection").then(function (result) {
      if (result.ok && result.body) state.connection = result.body;
    });
  }

  /* The course screen's 课程资料概览 rows ARE the 按资料类型 groups, so the
     overview and the type view can never disagree about a count. */

  var recordingTaskRead = null;
  function pollRecordingTask(force) {
    if (!state.activated || (!force && state.recordings.task.state !== "running" &&
        state.recordings.background.state !== "running")) return;
    var epoch = accountQuotaEpoch;
    if (recordingTaskRead && recordingTaskRead.epoch === epoch) return recordingTaskRead.promise;
    var read = { epoch: epoch, promise: null,
      foreground: state.recordings.task, background: state.recordings.background };
    recordingTaskRead = read;
    read.promise = requestJson("/api/recordings/task").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      // A start/cancel response owns newer state than this earlier observation.
      // Applying an old idle snapshot can otherwise hide a newly running task
      // and stop its scheduled polling altogether.
      if (state.recordings.task !== read.foreground || state.recordings.background !== read.background) return;
      if (!result.ok || !result.body) return;
      var previous = state.recordings.task;
      var previousBackground = state.recordings.background;
      var foreground = Object.assign({}, result.body);
      delete foreground.background_sync;
      state.recordings.task = foreground;
      if (foreground.course_id) state.recordings.taskCourseId = foreground.course_id;
      state.recordings.background = result.body.background_sync || { state: "idle" };
      var backgroundChanged = JSON.stringify(previousBackground) !== JSON.stringify(state.recordings.background);
      var completedCourse = backgroundChanged && (state.recordings.background.course_results || []).some(function (row) {
        var old = (previousBackground.course_results || []).find(function (entry) { return entry.course_id === row.course_id; });
        return row.state === "done" && (!old || old.state !== "done");
      });
      // Terminal feedback must be visible before ancillary cache/quota reads.
      if (backgroundChanged || previous.state !== foreground.state) render();
      if (previous.state === "running" && foreground.state !== "running" || completedCourse ||
          previousBackground.state === "running" && state.recordings.background.state !== "running") {
        return Promise.all([loadCampusCourses(), refreshSettledQuota()]).then(function () {
          if (epoch !== accountQuotaEpoch) return;
          if (state.view === "campus-course") return loadCampusCourse(state.campus.courseId);
          render();
        });
      }
      if (previous.stage !== result.body.stage || previous.state !== result.body.state ||
          previous.stage_index !== result.body.stage_index || previous.current_item !== result.body.current_item ||
          previous.progress_done !== result.body.progress_done || previous.progress_total !== result.body.progress_total ||
          previous.progress_unit !== result.body.progress_unit || previous.bytes_done !== result.body.bytes_done) {
        if (previous.state !== "running" || result.body.state !== "running" || !refreshRunningTaskStatus()) render();
      }
    }).catch(function () {}).finally(function () {
      if (recordingTaskRead === read) recordingTaskRead = null;
    });
    return read.promise;
  }

  function refreshRunningTaskStatus() {
    var boxes = document.querySelectorAll(".recording-task");
    var markup = recordingTaskStatus();
    if (!boxes.length || !markup) return false;
    var template = document.createElement("template");
    template.innerHTML = markup;
    var next = template.content.firstElementChild;
    var nextText = next && next.querySelector(".recording-task-text");
    var nextProgress = next && next.querySelector(".recording-progress");
    if (!nextText || !nextText.firstChild || !nextProgress) return false;
    for (var i = 0; i < boxes.length; i += 1) {
      var currentText = boxes[i].querySelector(".recording-task-text");
      var currentProgress = boxes[i].querySelector(".recording-progress");
      if (!currentText || !currentText.firstChild || !currentProgress) return false;
      currentText.firstChild.nodeValue = nextText.firstChild.nodeValue;
      currentProgress.replaceWith(nextProgress.cloneNode(true));
    }
    return true;
  }


  function loadCampusCourses() {
    var epoch = accountQuotaEpoch;
    return requestJson("/api/campus/courses").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) return;
      state.campus.courses = result.body.courses || [];
      if (Object.prototype.hasOwnProperty.call(result.body, 'notion_home')) {
        state.campus.home = result.body.notion_home && result.body.notion_home.id && result.body.notion_home.schema === 2 ? result.body.notion_home : null;
        state.campus.workspace = state.campus.home && state.campus.home.schema === 2 ? state.campus.home : null;
      }
      state.campus.configured = Boolean(result.body.campus_configured);
      state.campus.demoConnection = Boolean(result.body.demo_connection);
      render();
    }).catch(function () {});
  }

  function courseTaskBusy(courseId) {
    var background = state.recordings.background || {};
    return state.recordings.syncPending || state.recordings.task.state === "running" ||
      background.state === "running" && background.current_course_id === courseId;
  }

  function campusReadError(message) {
    var value = String(message || "");
    if (/课程准备读取已过期|课程快照或准备顺序已变化|课序不一致/.test(value))
      return "课程信息需要重新核对，Notion 同步暂未完成。登录状态不受影响，可返回「我的课程」更新目录后重试。";
    if (/account_unconfirmed|revision|快照.*未确认|快照.*已更新/i.test(value))
      return "账号目录版本核对未完成或已变化；已有内容保留，请更新这门课后重试。";
    return value || "教学网连接中断；保留已有内容，请检查网络后重试。";
  }

  function rememberCampusCourse(course) {
    if (!course || !course.id) return;
    state.campus.courseCache[course.id] = { epoch: accountQuotaEpoch, body: JSON.parse(JSON.stringify(course)) };
  }

  function loadCampusCourse(courseId) {
    var epoch = accountQuotaEpoch;
    var readId = (state.campus.courseReadId || 0) + 1;
    state.campus.courseReadId = readId;
    state.campus.loading = true;
    state.campus.error = "";
    render();
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId)).then(function (result) {
      if (epoch !== accountQuotaEpoch || state.campus.courseId !== courseId || state.campus.courseReadId !== readId) return;
      state.campus.loading = false;
      if (result.ok && result.body && result.body.id === courseId) {
        state.campus.course = result.body;
        rememberCampusCourse(result.body);
        state.campus.error = "";
      } else {
        state.campus.error = campusReadError(result.body && result.body.detail);
        if (state.campus.course) state.campus.course.read_only = true;
      }
      render();
      if (result.ok) return loadCampusReceipts(courseId);
    }).catch(function () {
      if (epoch !== accountQuotaEpoch || state.campus.courseId !== courseId || state.campus.courseReadId !== readId) return;
      state.campus.loading = false;
      state.campus.error = campusReadError();
      if (state.campus.course) state.campus.course.read_only = true;
      render();
    });
  }

  function loadNotionParents(options) {
    if (state.campus.demoConnection) return Promise.resolve();
    if (!state.connection.connected) { state.campus.parentError = "请先连接 Notion。"; render(); return Promise.resolve(); }
    var epoch = notionParentsEpoch;
    var email = accountEmail();
    var selectedOnly = Boolean(options && options.selectedOnly);
    if (notionParentsRead && notionParentsRead.epoch === epoch && notionParentsRead.email === email &&
        notionParentsRead.selectedOnly === selectedOnly)
      return notionParentsRead.promise;
    state.campus.parentError = "";
    state.campus.parentsLoading = true;
    render();
    var pending = { epoch: epoch, email: email, selectedOnly: selectedOnly, promise: null };
    notionParentsRead = pending;
    pending.promise = requestJson("/api/campus/notion/parents" + (selectedOnly ? "?selected_only=true" : "")).then(function (result) {
      if (epoch !== notionParentsEpoch || email !== accountEmail() || !state.activated) return;
      if (!result.ok || !result.body) { state.campus.parentError = (result.body && result.body.detail) || "Notion 页面暂时无法读取。"; render(); return; }
      state.campus.parents = result.body.pages || [];
      state.campus.homeSync = result.body.home_sync || null;
      state.campus.home = result.body.home && result.body.home.id && result.body.home.schema === 2 ? result.body.home : null;
      state.campus.workspace = result.body.workspace && result.body.workspace.id ? result.body.workspace : null;
      var destinations = state.campus.parents.filter(function (page) { return !state.campus.home || page.id !== state.campus.home.id; });
      if (state.campus.home && destinations.some(function (page) { return page.id === state.campus.home.parent_id; }))
        state.campus.parentId = state.campus.home.parent_id;
      else if (!destinations.some(function (page) { return page.id === state.campus.parentId; }))
        state.campus.parentId = destinations.length ? destinations[0].id : "";
      render();
    }).catch(function () {
      if (epoch !== notionParentsEpoch || email !== accountEmail() || !state.activated) return;
      state.campus.parentError = "Notion 页面暂时无法读取。"; render();
    }).finally(function () {
      if (notionParentsRead === pending) {
        notionParentsRead = null;
        state.campus.parentsLoading = false;
        render();
      }
    });
    return pending.promise;
  }

  function campusCatalogReady() {
    return state.campus.courses.some(function (course) { return Boolean(course.synced_at) && !course.read_only; });
  }

  function campusSetupGuide(includeSyncAction) {
    var catalogReady = campusCatalogReady();
    var syncBusy = state.recordings.syncPending || state.recordings.backgroundPending ||
      state.recordings.task.state === "running" || state.recordings.background.state === "running";
    var steps = [
      ["产品账号", state.activated && accountVerified()],
      ["教学网账号", state.campus.configured],
      ["课程目录", catalogReady],
      ["Notion 学习主页", Boolean(state.campus.home)]
    ];
    var next = steps.find(function (step) { return !step[1]; });
    var nextAction = state.campus.demoConnection ? '' : !accountVerified() ? button("我已验证邮箱，刷新状态", "refresh-verification", { small: true }) :
      !state.campus.configured ? button("设置教学网账号", "campus-account-settings", { small: true, primary: true }) :
      !catalogReady ? (includeSyncAction === false ? '' : button(syncBusy ? "正在更新课程目录…" : "更新课程目录", "recording-sync", { small: true, primary: true, disabled: syncBusy })) :
      !state.campus.home ? button("设置 Notion 学习主页", "campus-account-settings", { small: true, primary: true }) : '';
    return '<section class="panel campus-setup"><h2>开始使用</h2><ul class="setup-steps">' +
      steps.map(function (step) { return '<li><span class="setup-mark" aria-hidden="true">' + (step[1] ? '✓' : '○') + '</span><span>' + esc(step[0]) + '</span></li>'; }).join("") +
      '</ul><p>' + (state.campus.demoConnection ? '演示模式：此处不会创建真实页面。' :
        next ? '下一步：' + esc(next[0]) : '已就绪，可以从课程中选择录像按需整理。') + '</p>' + nextAction + '</section>';
  }

  function campusDashboardScreen() {
    var query = state.campus.query.trim().toLowerCase();
    var visible = state.campus.courses.filter(function (course) { return !query || (course.title + " " + course.code + " " + course.term + " " + course.id).toLowerCase().includes(query); });
    var titleCounts = {};
    state.campus.courses.forEach(function (course) { titleCounts[course.title] = (titleCounts[course.title] || 0) + 1; });
    var items = visible.map(function (course) {
      var classLabel = titleCounts[course.title] > 1 && course.id
        ? ' · 教学班 ' + esc(course.id.replace(/^_/, '').replace(/_\d+$/, '')) : '';
      return '<article class="course-card"><span class="course-icon" aria-hidden="true">' + esc(GLYPH.book) +
        '</span><div class="course-content"><span class="course-meta">' + esc(course.term || '本学期') + classLabel + '</span><h3>' + esc(course.title) +
        '</h3><p>' + esc(course.recordings) + ' 节课 · ' + esc(course.assignments || 0) + ' 项正式作业 · ' + esc(course.announcements || 0) + ' 则通知' +
        (course.calendar_items ? ' · ' + esc(course.calendar_items) + ' 项日历事项' : '') + '</p><details class="course-meta-details" data-persistent-disclosure="course-meta-' + esc(course.id) + '"><summary>课程信息</summary><p>' +
        esc(course.code ? '课程号 ' + (course.code.length > 14 ? course.code.slice(0, 8) + '…' + course.code.slice(-4) : course.code) : '课程号未提供') + (course.term ? ' · ' + esc(course.term) : '') +
        (course.id ? ' · 教学班 …' + esc(course.id.slice(-6)) : '') + '</p><p>目录更新：' +
        esc(course.synced_at ? course.synced_at.slice(0, 10) : "尚未更新") + '</p></details>' +
        (course.read_only ? '<p role="status">' + esc(course.read_only_message || "教学网读取尚未完成；已有内容保留为只读，请更新这门课。") + '</p>' : '') + '</div>' +
        button("查看课程", "open-campus-course", { small: true, attrs: { "data-course": course.id } }) + '</article>';
    }).join("");
    var credentials = state.campus.configured && !state.recordings.editingCredentials
      ? '<p>教学网账号已保存。' + button("更换账号", "recording-edit-credentials", { small: true, quiet: true }) + '</p>'
      : recordingCredentialsForm();
    if (state.view === "campus-account") {
      return breadcrumb([{label:"我的课程", action:"open-dashboard"}, {label:"连接设置"}]) +
        '<div class="topline"><div><h1 class="page-title">连接设置</h1><p class="page-desc">连接教学网获取课程，连接 Notion 保存笔记。</p></div></div>' +
        campusSetupGuide() + recordingTaskStatus() + backgroundSyncStatus() +
        (state.recordings.error ? '<p role="alert">' + esc(state.recordings.error) + '</p>' : '') +
        '<section class="panel"><h2>教学网账号</h2>' + credentials + '</section>' + campusHomeControls();
    }
    var empty = state.campus.courses.length ? (visible.length ? '<div class="course-grid">' + items + '</div>' : '<div class="empty-materials">没有找到匹配的课程。</div>') :
      '<div class="empty-materials">还没有课程。连接教学网账号后，点击「更新课程目录」。</div>';
    return '<div class="topline"><div><h1 class="page-title">我的课程</h1><p class="page-desc">查看作业与资料，按需整理课堂笔记。</p></div></div>' +
      (state.view === "campus-account" || !state.campus.configured || !state.campus.home || !accountVerified() ? campusSetupGuide(false) : '') +
      '<section class="panel" aria-label="课程列表"><div class="course-list-toolbar"><span class="course-count">' + (query ? '找到 ' + visible.length : state.campus.courses.length) + ' 门课程</span>' +
      button("更新课程目录", "recording-sync", { small: true, primary: true,
        disabled: !state.campus.configured,
        attrs: { "aria-disabled": state.recordings.backgroundPending || state.recordings.syncPending || state.recordings.task.state === "running" || state.recordings.background.state === "running",
          "style": state.recordings.backgroundPending || state.recordings.syncPending || state.recordings.task.state === "running" || state.recordings.background.state === "running" ? "opacity:.55;cursor:not-allowed;transform:none" : "" }
      }) +
      '</div>' + recordingTaskStatus() + backgroundSyncStatus() +
       (state.campus.courses.some(function (course) { return !course.synced_at; }) ?
         '<p class="form-note hint">部分课程尚未更新，请先更新目录。</p>' : '') +
       (state.campus.courses.length ? '<label class="campus-search-label">查找课程<input class="text-input" type="search" data-campus-search placeholder="输入课程名或课程号" value="' + esc(state.campus.query) + '"></label>' : '') + (state.recordings.error ? '<p role="alert">' + esc(state.recordings.error) + '</p>' : '') +
      empty + '</section>';
  }

  function campusHomeControls() {
    if (state.campus.demoConnection) return '';
    var busy = Boolean(state.campus.workspaceBusy);
    var loading = Boolean(state.campus.parentsLoading);
    var homeSync = state.campus.homeSync;
    var blocked = homeSync && ['pulled', 'current', 'absent'].indexOf(homeSync.status) === -1;
    var header = '<div class="home-setup-header"><span class="home-notion-mark" aria-hidden="true">N</span><div><p class="eyebrow">NOTION 学习空间</p><h2>' +
      (blocked ? '恢复学习主页' : state.campus.home ? '你的学习主页' : '创建学习主页') + '</h2><p>让课程、讲次与学习记录都有自己的位置。</p></div></div>';
    var error = state.campus.parentError ? '<p role="alert" class="form-note home-setup-error">' + esc(state.campus.parentError) + '</p>' : '';
    if (!state.connection.connected) return '<section class="panel home-setup">' + header +
      '<div class="home-connect"><p>连接 Notion 后，选择一个页面作为学习主页的保存位置。</p>' +
      button("连接 Notion", "connect-notion", { primary: true }) + '</div></section>';
    if (blocked) {
      var recoveryMessages = {
        notion_reconnect_required: '账号已保存学习主页，但当前 Notion 授权无法访问。请重新连接 Notion，并授权原主页及其父页面；授权完成后会自动恢复。',
        notion_home_archived: '账号已有学习主页，但页面已归档或在回收站。请在 Notion 恢复页面后重试，软件会自动恢复主页配置。',
        notion_home_changed: '账号已有学习主页，但它在 Notion 中的位置或页面状态已变化。请核对原主页和父页面后重试。',
        notion_unavailable: '暂时无法连接 Notion，已有主页配置保留。连接恢复后重试，软件会自动恢复主页。',
        local_write_failed: '已找到账号主页，但本机配置保存失败。请检查本机存储后重试。',
        account_unavailable: '暂时无法读取账号主页配置。请检查账号登录与连接后重试。'
      };
      var recoveryOptions = state.campus.parents.map(function (page) {
        return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
      }).join('');
      return '<section class="panel home-setup" data-home-recovery><div>' + header +
        '<p role="status" class="form-note">' + esc(recoveryMessages[homeSync.status] || '账号主页配置未能确认，请重新读取。') + '</p><div class="home-setup-actions">' +
        (homeSync.status === 'notion_reconnect_required' ? button('重新连接 Notion', 'connect-notion', {primary:true, disabled:loading || busy}) : '') +
        button(loading ? '正在自动恢复…' : '重试恢复主页', 'campus-load-parents', {small:true, disabled:loading || busy}) + '</div>' +
        (recoveryOptions ? '<details class="home-setup-advanced"><summary>使用新的学习主页</summary><p>原 Notion 页面保留，账号将使用新主页。</p><label>新主页保存位置<select data-campus-parent aria-label="新主页保存位置"' + (busy ? ' disabled' : '') + '>' + recoveryOptions + '</select></label>' +
          button(busy ? '正在创建并验证…' : '在所选位置新建主页', 'campus-replace-workspace', {small:true, disabled:loading || busy || state.recordings.task.state === 'running'}) + '</details>' : '') + '</div></section>';
    }
    if (loading && !state.campus.home) return '<section class="panel home-setup">' + header +
      '<p role="status" class="form-note">正在读取账号配置并自动恢复已有学习主页…</p></section>';
    var refresh = button(loading ? "正在读取页面…" : "刷新可用页面", "campus-load-parents", { small: true, quiet: true, disabled: loading || busy });
    if (state.campus.home) {
      var destinations = state.campus.parents.filter(function (page) { return page.id !== state.campus.home.id; });
      var moveOptions = destinations.map(function (page) {
        return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
      }).join("");
      return '<section class="panel home-setup home-setup-ready">' + header +
        '<p class="home-ready-badge">✓ 学习主页已就绪</p>' +
        (state.campus.workspace && state.campus.workspace.active === false ? '<p role="status" class="form-note hint">新主页仍待验证，当前继续使用原主页。</p>' : '') + '<div class="home-setup-actions">' +
        button("打开主页", "campus-open-home", { primary: true, attrs: { "data-url": state.campus.home.url || "" } }) +
        (state.campus.workspace ? button(busy ? "正在更新…" : "更新目录", "campus-create-workspace", { small: true, disabled: busy || state.recordings.task.state === "running", attrs: { "aria-busy": String(busy) } }) : '') +
        button("生成全部课程总结", "campus-summaries", {
          small: true, quiet: true, disabled: busy || state.recordings.task.state === "running",
          attrs: { "data-url": state.campus.home.url || "" }
        }) +
        '</div><details class="home-setup-advanced" data-persistent-disclosure="home-location"><summary>管理保存位置</summary><div class="home-location-controls">' +
        (moveOptions ? '<label>主页所在位置<select data-campus-parent aria-label="学习主页所在位置"' + (busy ? ' disabled' : '') + '>' + moveOptions + '</select></label>' +
          button("移动现有主页", "campus-move-home", { small: true, disabled: busy || state.campus.parentId === state.campus.home.parent_id }) :
          '') + refresh + '</div>' + '</details>' + error + '</section>';
    }
    var options = state.campus.parents.map(function (page) {
      return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
    }).join("");
    var selected = state.campus.parents.find(function (page) { return page.id === state.campus.parentId; });
    return '<section class="panel home-setup">' + header + '<div class="home-setup-body"><div class="home-destination">' +
      '<label for="home-parent">1 · 选择保存位置</label><p>选择已授权给应用的 Notion 页面。</p>' +
      (options ? '<select id="home-parent" data-campus-parent aria-label="Notion 父页面"' + (busy || loading ? ' disabled' : '') + '>' + options + '</select>' :
        '<div class="home-pages-empty">' + (loading ? '正在读取可用页面…' : '还没有可选择的页面，请读取页面列表或检查 Notion 授权。') + '</div>') + refresh +
      '</div><div class="home-preview"><small>主页结构预览</small><h3>我的学习空间</h3><ul><li>课程与课堂讲次</li><li>作业、通知与课程资料</li><li>课堂笔记与课程总结</li></ul></div></div>' +
      '<div class="home-setup-footer"><div><strong>2 · 创建学习主页</strong><p>' +
      (!campusCatalogReady() ? '先更新课程目录，至少一门课程读取成功后即可创建。Notion 保存位置会保留。' :
        selected ? '保存到「' + esc(selected.title) + '」下面' : '选好保存位置后即可创建') + '</p></div>' +
      button(busy ? "正在创建并验证…" : "创建学习主页", "campus-create-workspace", {
        primary: true, disabled: busy || loading || !state.campus.parentId || !campusCatalogReady() || state.recordings.task.state === "running", attrs: { "aria-busy": String(busy) }
      }) + '</div>' + error + '</section>';
  }

  function directOssConsent() {
    return '<label class="recording-oss-consent"><input type="checkbox" data-recording-direct-oss' +
      (state.recordings.directOss ? ' checked' : '') +
      '> 本次整理将音频经 15 分钟短时授权直传阿里云 OSS，以加快云端转写；教学网账号留在本机，临时音频处理后删除。</label>';
  }

  function createWorkspace(replaceExisting) {
    if (state.campus.workspaceBusy) return;
    var parentId = replaceExisting ? state.campus.parentId : state.campus.workspace && state.campus.workspace.parent_id || state.campus.parentId;
    if (!parentId) { state.campus.parentError = "请先选择 Notion 保存位置。"; render(); return; }
    var epoch = accountQuotaEpoch;
    var updating = Boolean(state.campus.workspace);
    state.campus.workspaceBusy = true;
    state.campus.parentError = "";
    render();
    var payload = { parent_page_id: parentId };
    if (replaceExisting) payload.replace_existing = true;
    return requestJson("/api/campus/notion/workspace", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body || !result.body.id || result.body.active !== true) throw new Error(result.body && result.body.detail || "新版目录未完成验证。");
      state.campus.workspace = result.body;
      state.campus.home = result.body;
      state.campus.homeSync = {status:'pulled'};
      state.campus.parentId = result.body.parent_id;
      showToast(updating ? "学习主页目录已更新。" : "学习主页已创建并验证，可以开始整理课程。");
    }).catch(function (error) {
      if (epoch !== accountQuotaEpoch) return;
      state.campus.parentError = campusReadError(error.message || "新版目录暂时无法同步。");
    }).finally(function () { if (epoch === accountQuotaEpoch) { state.campus.workspaceBusy = false; render(); } });
  }

  function campusMaterial(item, courseId, lessons) {
    var category = item.category === "assignment" ? "作业或测验" : item.category === "information" ? "课程信息" : "资料";
    var links = (item.files || []).map(function (name, index) {
      return '<a class="material-file-link" href="/api/campus/courses/' + encodeURIComponent(courseId) +
        '/materials/' + item.index + '/files/' + index + '" download>下载 ' + esc(name) + '</a>';
    }).join(" ");
    var control = "";
    if (lessons && lessons.length) {
      var options = lessons.map(function (lesson) {
        return '<option value="' + esc(lesson.id) + '">' + esc(lesson.title) + '</option>';
      }).join("");
      control = item.match_state === "ignored"
        ? button("恢复待确认", "campus-material-restore", { small: true, attrs: { "data-material": item.index } })
        : (options ? '<select data-material-select="' + esc(item.index) + '" aria-label="选择录像讲次">' +
            '<option value="">选择录像讲次</option>' + options + '</select>' +
            button("关联到讲次", "campus-material-assign", { small: true, attrs: { "data-material": item.index } }) : "") +
          button("忽略匹配", "campus-material-ignore", { small: true, quiet: true, attrs: { "data-material": item.index } });
    }
    return '<li><strong>' + esc(item.title) + '</strong> <span class="course-meta">' + category +
      '</span>' + (links ? '<div>' + links + '</div>' : '<small>教学网未提供可下载附件；可在教学网查看。</small>') +
      (control ? '<div class="material-match-control">' + control + '</div>' : '') + '</li>';
  }

  function campusOfficialSections(course, section) {
    var updated = course.synced_at_label || "尚未更新";
    var noticeState = course.section_states && course.section_states.announcements;
    var noticeStatus = course.announcements_status === "unavailable" ? "教学网未开放通知功能，仍需查看教学网。" :
      course.announcements_status === "error" ? "通知暂时读取失败，请到教学网核对。" :
      course.announcements_status === "not_synced" ? "通知尚未同步，请先更新目录。" : "教学网当前没有正式通知。";
    var noticeItems = (course.announcements || []).map(function (item) {
      return '<li><strong>' + esc(item.title) + '</strong><small>发布：' + esc(item.posted_at_label || item.posted_at || "未提供") +
        ' · 来源：教学网正式通知 · 更新：' + esc(updated) + '</small><p>' + esc(item.body_text || "正文未提供，请查看教学网。") +
        '</p><a href="' + esc(item.source_url || '#') + '" target="_blank" rel="noopener noreferrer">查看这条通知</a></li>';
    }).join("");
    var noticeWarning = course.announcements_status === "error" || course.announcements_status === "unavailable" ||
      course.announcements_status === "not_synced";
    if (noticeState) {
      noticeWarning = ["not_synced", "error", "cached"].indexOf(noticeState.state) >= 0;
      if (noticeWarning) noticeStatus = noticeState.message;
    }
    var notices = '<section class="panel" id="course-notices"><h2>通知中心</h2>' +
      (noticeWarning ? '<p class="form-note hint">' + esc(noticeStatus) + '</p>' : '') +
      (noticeItems ? '<ul class="official-list">' + noticeItems + '</ul>' :
        (noticeWarning ? '' : '<p>' + esc(noticeStatus) + '</p>')) + '</section>';
    var assignmentItems = (course.assignments || []).map(function (item) {
      var status = (state.campus.assignmentStatuses || {})[item.content_id];
      var source = item.content_id && item.source !== "calendar"
        ? '/api/campus/courses/' + encodeURIComponent(course.id) +
          '/assignments/' + encodeURIComponent(item.content_id) + '/source'
        : '';
      var due = item.due_at_label || item.due_at || (item.teacher_deadline_quote
        ? '老师原文写 ' + item.teacher_deadline_quote + '（系统无独立截止字段，请打开原要求核对）'
        : '教学网未公布');
      var sourceLinks = (item.source_links || []).filter(function (link) {
        return link && /^https:\/\/[^/]+/i.test(link.url || '') && link.label;
      }).map(function (link) {
        return '<a href="' + esc(link.url) + '" target="_blank" rel="noopener noreferrer">' +
          esc(link.label) + '</a>';
      }).join(' · ');
      return '<li><strong>' + esc(item.title) + '</strong><small>' + (item.source === 'calendar' ? '日历时间：' : '截止：') + esc(due) +
        ' · 来源：教学网' + (item.source === "calendar" ? "日历" : "作业") +
        ' · 更新：' + esc(updated) + '</small><p>' + esc(item.instructions || "要求未提供，请打开教学网原页面核对。") +
        '</p>' + (item.source === 'calendar' ? '<p>尚未关联到正式作业；日历时间不能据此认定为作业截止时间。</p>' : '') +
        ((item.files || []).length ? '<p>附件：' + esc(item.files.join("、")) + '</p>' : '') +
        (sourceLinks ? '<p>老师原页面链接：' + sourceLinks + '</p>' : '') +
        (source ? '<a href="' + esc(source) + '" target="_blank" rel="noopener noreferrer">查看教学网原要求</a>' :
          '<small>请到教学网课程中核对原要求</small>') +
        (source ? button(status && status.loading ? '正在读取提交状态…' : '读取教学网提交状态', 'campus-assignment-status', {
          small: true, disabled: Boolean(status && status.loading), attrs: { 'data-assignment': item.content_id } }) : '') +
        (status ? '<p role="status">' + esc(status.label || '教学网当前提交状态未确认') +
          (status.checked_at ? ' · 核对时间：' + esc(status.checked_at) : '') + '</p>' +
          (status.error ? '<p role="alert">' + esc(status.error) + '</p>' : '') : '') +
        (item.content_id && item.source !== "calendar" ? button("准备作业文件", "campus-prepare-assignment", { small: true,
          attrs: { "data-assignment": item.content_id } }) : '<small>尚无可验证的提交目标</small>') + '</li>';
    }).join("");
    var announcementTaskItems = (course.announcement_tasks || []).map(function (item) {
      return '<li><strong>' + esc(item.notice_title) + '</strong><small>公告待办 · 截止：' +
        esc(item.due_at_label) + '</small><p>' + esc(item.instructions) +
        '</p><a href="' + esc(item.source_url) + '" target="_blank" rel="noopener noreferrer">查看老师公告原文</a>' +
        '<small>老师公告中的交付要求；请按原文的方式完成，不代表教学网有独立提交入口。</small></li>';
    }).join("");
    var handbookItems = (course.handbook_reviews || []).map(function (item) {
      var source = '/api/campus/courses/' + encodeURIComponent(course.id) +
        '/materials/' + encodeURIComponent(item.material_index) + '/files/' + encodeURIComponent(item.file_index);
      var pages = (item.pages || []).map(function (page) {
        return '<details><summary>PDF 第 ' + esc(page.page) + ' 页 · 手册原文摘录</summary><p>' +
          esc(page.excerpt) + (page.truncated ? '…（请打开原 PDF 阅读余下内容）' : '') + '</p></details>';
      }).join('');
      var preview = (item.time_preview || []).map(function (cue) {
        return '<li><strong>PDF 第 ' + esc(cue.page) + ' 页</strong> · ' + esc(cue.text) + '</li>';
      }).join('');
      var presence = (item.presence_preview || []).map(function (cue) {
        return '<li><strong>' + esc(cue.label) + ' · PDF 第 ' + esc(cue.page) + ' 页</strong> · ' +
          esc(cue.text) + '<small>' + esc(cue.note) + '</small></li>';
      }).join('');
      return '<li><strong>' + esc(item.title) + '</strong><small>课程手册要求，需向老师或助教核对；' +
        '不是教学网正式作业，也没有已验证的提交入口或系统截止。</small><p>' + esc(item.status) +
        '</p>' + (preview ? '<p>手册原文中的时间／提交线索（未核对是否仍有效）：</p><ul>' + preview + '</ul>' : '') +
        (presence ? '<p>笔记无法代替的参与事项（到场方式待核对）：</p><ul>' + presence + '</ul>' : '') +
        pages + (item.pages && item.pages.length ? '' :
          button('读取手册并显示要求', 'campus-review-handbook', { small: true, attrs: {
            'data-material': item.material_index, 'data-file': item.file_index } })) +
        '<a href="' + esc(source) + '" download>下载课程手册 PDF（核对原页）</a></li>';
    }).join("");
    var selected = state.campus.submission.selectedId;
    var selectedItem = (course.assignments || []).find(function (item) { return item.content_id === selected; });
    var draft = state.campus.submission.draft;
    var formValues = state.campus.submission.form;
    var preparation = "";
    if (selectedItem) {
      var extracted = state.campus.submission.analysis;
      var analysisView = extracted ? '<div data-assignment-requirements><h4>已核对原文的交付要求</h4><ul>' +
        extracted.analysis.requirements.concat(extracted.analysis.filename_rules).map(function (row) {
          return '<li>' + esc(row.quote) + '</li>';
        }).join('') + '</ul><p>原文指定格式：' +
        esc(extracted.analysis.file_formats.map(function (row) { return row.value; }).join('、') || '未明确，请核对') +
        '</p>' + extracted.analysis.uncertainties.map(function (item) { return '<p class="form-note hint">待核对：' + esc(item) + '</p>'; }).join('') +
        '<p data-assignment-analysis-cost="' + esc(extracted.points_charged) + '">本次用量：' +
        esc(extracted.points_charged) + ' AI 点' + (extracted.reused ? '（复用已保存结果）' : '') + '</p>' +
        (extracted.reused ? '<p>原任务用量：' + esc(extracted.receipt_points_charged) + ' AI 点</p>' : '') + '</div>' : '';
      var rarHint = /\.rar\b/i.test(selectedItem.instructions || "") ?
        '<p class="form-note hint">老师要求中出现 .rar；这里生成的是 ZIP。请核对老师是否接受 ZIP，需要 RAR 时在本机按原要求制作。</p>' : '';
      preparation = '<div class="assignment-preparation"><h3>准备作业文件 · ' + esc(selectedItem.title) +
        '</h3><p>按老师要求选择文件形式。ZIP 模式会把 Notion 正文保存为 Markdown 文本“答案.md”，不会自动生成 PDF 或 Word。若老师要求文档，请先从 Notion 导出后用单文件模式上传。在线作答或随堂考勤请使用教学网原入口。草稿只保存在本机，准备阶段不会提交。</p>' + rarHint +
        button(state.campus.submission.analysisBusy ? '正在识别要求…' : 'AI 识别作业要求', 'campus-analyze-requirements',
          { small: true, disabled: !!state.campus.submission.analysisBusy }) + analysisView +
        (draft ? '<div role="status"><p>正式提交目标：' + esc(course.title) + ' · ' + esc(draft.assignment_title) +
          '</p><p>截止：' + esc(draft.due_at_label || draft.due_at || '教学网未公布，请核对原要求') +
          '</p><p>本地草稿：' + esc(draft.archive_name) + '</p><ul>' +
          draft.files.map(function (file) { return '<li>' + esc(file.name) + ' · ' + esc(formatTransfer(file.bytes)) + '</li>'; }).join("") +
          '</ul>' + (draft.warnings || []).map(function (warning) { return '<p class="form-note hint">' + esc(warning) + '</p>'; }).join("") +
          '<a href="' + esc(draft.download_url) + '" download data-action="campus-download-draft">下载并检查' + (draft.submission_mode === "single_file" ? '文件' : '压缩包') + '</a>' +
          (state.campus.submission.saveStatus ? '<p role="status" class="assignment-download-status" data-native-save="' + esc(state.campus.submission.saveStatus.state) + '" data-saved-sha256="' + esc(state.campus.submission.saveStatus.sha256 || '') + '">' + esc(state.campus.submission.saveStatus.message) + '</p>' : '') +
          '<label class="assignment-check"><input type="checkbox" id="assignment-reviewed"> 我已对照老师要求核对提交目标、文件名、文件内容和截止时间</label>' +
          button("确认提交到教学网", "campus-submit-assignment", { primary: true, small: true,
            disabled: draft.state === "submitted" }) + (draft.state === "submitted" ? '<p>已发送提交请求，请在教学网查看回执。</p>' : '') +
          (draft.receipt_download_url ? '<a href="' + esc(draft.receipt_download_url) + '" download>下载教学网正式回执</a>' : '') +
          '</div>' :
          '<form id="assignment-prepare-form"><label>提交文件形式<select name="submission_mode"><option value="zip"' + (formValues.submission_mode === "zip" ? ' selected' : '') + '>ZIP 压缩包</option><option value="single_file"' + (formValues.submission_mode === "single_file" ? ' selected' : '') + '>单个原文件（PDF、Word 等）</option></select></label>' +
          (formValues.submission_mode === "zip" ?
            '<label>Notion 答案页链接<input name="notion_url" type="url" placeholder="粘贴 Notion 页面链接" value="' + esc(formValues.notion_url) + '"></label>' +
            '<label>压缩包文件名（按老师要求）<input name="archive_name" required placeholder="学号姓名第1次作业.zip" value="' + esc(formValues.archive_name) + '"></label>' +
            '<label>单独导出页面中的 Python 代码块为（可选）<input name="code_filename" placeholder="hill_cipher.py" value="' + esc(formValues.code_filename) + '"></label>' +
            '<label>补充文件（如代码、图片、已导出的 PDF）<input name="attachments" type="file" multiple></label>' :
            '<label>已导出的答案文件<input name="attachments" type="file"></label>') +
          '<p data-assignment-attachments>' + (formValues.attachments.length ? '已选择：' + esc(formValues.attachments.map(function (file) { return file.name; }).join('、')) : '尚未选择文件') + '</p>' +
          (formValues.submission_mode === "zip" ? '<p class="form-note">Notion 嵌入文件不会自动加入压缩包；请在这里补充。另一套 Notion 页面需要另行授权给本应用；也可先导出答案文件并上传。</p>' :
            '<p class="form-note">请先将 Notion 作业导出为老师要求的格式，再选择导出的文件；此处不会转换页面格式或改变原文件名。</p>') +
          button("生成本地草稿并预检", "campus-build-draft", { primary: true, small: true }) + '</form>') +
        (state.campus.submission.error ? '<p role="alert">' + esc(state.campus.submission.error) + '</p>' : '') +
        '</div>';
    }
    var assignmentWarning = course.assignments_status === "partial" || course.assignments_status === "error" ||
      course.assignments_status === "not_synced";
    var assignmentState = course.section_states && course.section_states.assignments;
    if (assignmentState) assignmentWarning = ["not_synced", "error", "cached"].indexOf(assignmentState.state) >= 0;
    var assignments = '<section class="panel" id="course-assignments"><h2>作业中心</h2>' +
      (assignmentWarning ? '<p class="form-note hint">' + esc(assignmentState && assignmentState.message || '作业目录尚未完整读取；请到教学网核对正式要求和截止时间。') + '</p>' : '') +
      (assignmentItems ? '<h3>教学网作业与日历事项</h3><ul class="official-list">' + assignmentItems + '</ul>' :
        (assignmentWarning ? '' : '<p>教学网正式作业索引当前为 0 项；课程手册、通知或课堂要求仍可能包含课业。</p>')) +
      (announcementTaskItems ? '<h3>老师公告中的交付要求</h3><ul class="official-list">' + announcementTaskItems + '</ul>' : '') +
      (handbookItems ? '<h3>课程手册中的待核对要求</h3><ul class="official-list">' + handbookItems + '</ul>' : '') +
      preparation + '<details class="submission-receipts"><summary>本机教学网回执记录</summary><p>保存的是教学网当时返回的提交历史；当前提交状态请到教学网核对。</p>' +
      (state.campus.receiptError ? '<p role="alert">' + esc(state.campus.receiptError) + '</p>' : '') +
      '<ul>' + (state.campus.receipts || []).map(function (item) {
        return '<li>' + esc(item.assignment_title) + ' · ' + esc(item.archive_name) + ' · ' + esc(item.verified_at) +
          (item.state === 'receipt_verified' ? ' · 回执已保存在本机，账号记录待核验 ' +
            button('恢复回执保存', 'campus-recover-submission', { small: true, attrs: { 'data-draft': item.draft_id } }) :
            (item.download_url ? ' <a href="' + esc(item.download_url) + '" download>下载教学网正式回执</a>' : '')) + '</li>';
      }).join('') + '</ul>' + (state.campus.pendingSubmissions || []).map(function (item) {
        return '<p>' + esc(item.assignment_title) + ' · ' + esc(item.archive_name) + ' · 提交结果待核验。 ' +
          button('核对未完成记录', 'campus-recover-submission', { small: true, attrs: { 'data-draft': item.draft_id } }) + '</p>';
      }).join('') + button('刷新回执记录', 'campus-load-receipts', { small: true }) + '</details></section>';
    return section === "notices" ? notices : section === "assignments" ? assignments : notices + assignments;
  }

  function selectCampusAssignment(contentId) {
    state.campus.submission = { selectedId: contentId, draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } };
    render();
    var panel = document.querySelector(".assignment-preparation");
    if (panel) panel.scrollIntoView({ block: "nearest" });
  }

  function loadAssignmentStatus(contentId) {
    var courseId = state.campus.courseId;
    var statuses = state.campus.assignmentStatuses || (state.campus.assignmentStatuses = {});
    if (statuses[contentId] && statuses[contentId].loading) return;
    statuses[contentId] = { loading: true, label: '正在从教学网读取当前状态…' }; render();
    return requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + '/assignments/' +
      encodeURIComponent(contentId) + '/status').then(function (result) {
      if (state.campus.courseId !== courseId || state.campus.assignmentStatuses !== statuses) return;
      statuses[contentId] = result.ok && result.body ? result.body : {
        state: 'unknown', label: '教学网当前提交状态未确认',
        error: result.body && result.body.detail || '请到教学网核对原页面。本地回执不会代替当前状态。' };
      render();
    }).catch(function () {
      if (state.campus.courseId !== courseId || state.campus.assignmentStatuses !== statuses) return;
      statuses[contentId] = { state: 'unknown', label: '教学网当前提交状态未确认',
        error: '读取失败，请到教学网核对原页面。本地回执不会代替当前状态。' }; render();
    });
  }

  function loadCampusReceipts(courseId) {
    return requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + '/submission-receipts').then(function (result) {
      if (state.campus.courseId !== courseId) return;
      state.campus.receipts = result.ok && result.body ? result.body.items || [] : [];
      state.campus.pendingSubmissions = result.ok && result.body ? result.body.pending || [] : [];
      state.campus.receiptError = result.ok ? '' : '本机回执记录暂时无法读取。';
      render();
    }).catch(function () {
      if (state.campus.courseId !== courseId) return;
      state.campus.receipts = []; state.campus.pendingSubmissions = []; state.campus.receiptError = '本机回执记录暂时无法读取。'; render();
    });
  }

  function recoverCampusSubmission(draftId) {
    var courseId = state.campus.courseId;
    if (!draftId) return;
    return requestJson('/api/campus/submission-drafts/' + encodeURIComponent(draftId) + '/recover', { method: 'POST' }).then(function (result) {
      if (state.campus.courseId !== courseId) return;
      if (!result.ok || !result.body) {
        state.campus.receiptError = result.body && result.body.detail || '原提交记录未确认，禁止重复发送。';
        render(); return;
      }
      state.campus.receiptError = '';
      showToast(result.body.message);
      loadCampusReceipts(courseId);
    }).catch(function () {
      if (state.campus.courseId !== courseId) return;
      state.campus.receiptError = '核对失败，原记录已保留，请到教学网核对，避免重复提交。'; render();
    });
  }

  function prepareCampusAssignment() {
    var form = document.querySelector("#assignment-prepare-form");
    var selected = state.campus.submission.selectedId;
    if (!form || !selected || !form.reportValidity()) return;
    if (state.campus.submission.form.submission_mode === "single_file" &&
        state.campus.submission.form.attachments.length !== 1) {
      state.campus.submission.error = "请选择恰好一个已导出的答案文件。";
      render();
      return;
    }
    var courseId = state.campus.courseId;
    var data = new FormData(form);
    data.delete("attachments");
    if (state.campus.submission.form.submission_mode === "single_file") {
      data.delete("archive_name");
      data.delete("notion_url");
      data.delete("code_filename");
    }
    state.campus.submission.form.attachments.forEach(function (file) { data.append("attachments", file); });
    state.campus.submission.error = "";
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) +
      "/assignments/" + encodeURIComponent(selected) + "/prepare",
      { method: "POST", body: data }).then(function (result) {
        if (state.campus.courseId !== courseId) return;
        if (!result.ok || !result.body) {
          state.campus.submission.error = (result.body && result.body.detail) || "草稿准备失败。";
        } else {
          state.campus.submission.draft = result.body;
        }
        render();
      }).catch(function () {
        state.campus.submission.error = "草稿准备失败，请稍后重试。";
        render();
      });
  }

  async function analyzeCampusAssignmentRequirements() {
    var selected = state.campus.submission.selectedId;
    var courseId = state.campus.courseId;
    if (!selected || state.campus.submission.analysisBusy) return;
    if (!await confirmAction('确认识别这项教学网作业的要求？\n已有相同要求的结果会直接复用，不再扣费；需要新识别时预计 1–5 AI 点。\n请核对原文、截止时间和文件格式。')) return;
    state.campus.submission.analysisBusy = true;
    state.campus.submission.error = '';
    render();
    return requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + '/assignments/' +
      encodeURIComponent(selected) + '/requirements', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true }) }).then(function (result) {
      if (state.campus.courseId !== courseId || state.campus.submission.selectedId !== selected) return;
      state.campus.submission.analysisBusy = false;
      if (!result.ok || !result.body) state.campus.submission.error = result.body && result.body.detail || '要求识别未完成，请核对原任务。';
      else state.campus.submission.analysis = result.body;
      render();
    }).catch(function () {
      if (state.campus.courseId !== courseId || state.campus.submission.selectedId !== selected) return;
      state.campus.submission.analysisBusy = false;
      state.campus.submission.error = '要求识别连接中断，原调用记录已保留；再次打开可恢复同一请求。';
      render();
    });
  }

  async function submitCampusAssignment() {
    var draft = state.campus.submission.draft;
    var checked = document.querySelector("#assignment-reviewed");
    if (!draft || !checked || !checked.checked) {
      state.campus.submission.error = "请先下载检查草稿文件，并勾选确认。";
      render();
      return;
    }
    if (!await confirmAction("确认向教学网正式提交「" + (state.campus.course && state.campus.course.title || '') + ' · ' +
      draft.assignment_title + "」？\n文件：" + draft.archive_name + '\n截止：' +
      (draft.due_at_label || draft.due_at || '教学网未公布，请核对原要求') + "\n提交后可能无法撤回。")) return;
    var courseId = state.campus.courseId;
    var contentId = state.campus.submission.selectedId;
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) +
      "/assignments/" + encodeURIComponent(contentId) + "/submit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_id: draft.draft_id, sha256: draft.sha256, confirmed: true })
      }).then(function (result) {
        if (state.campus.courseId !== courseId) return;
        if (!result.ok || !result.body) {
          state.campus.submission.error = (result.body && result.body.detail) || "提交状态未确认，请到教学网核对。";
        } else {
          draft.state = result.body.state;
          draft.receipt_download_url = result.body.receipt_download_url;
          state.campus.submission.error = "";
          showToast(result.body.message);
          loadCampusReceipts(courseId);
        }
        render();
      }).catch(function () {
        state.campus.submission.error = "提交状态未确认，请到教学网核对历史，避免重复提交。";
        render();
      });
  }

  function campusExistingNotionControl(course) {
    return '<section class="panel campus-existing-notion"><h2>已有课程笔记</h2>' +
      (course.existing_notion_url ? '<p><a href="' + esc(course.existing_notion_url) +
        '" target="_blank" rel="noopener noreferrer">打开之前整理的课程页</a></p>' :
        '<p>如果你之前在 Notion 另建过这门课，可在这里关联；原页面内容不会被改动。</p>') +
      '<label>已有 Notion 课程页链接<input id="campus-existing-notion-url" type="url" value="' +
      esc(course.existing_notion_url || "") + '" placeholder="粘贴 Notion 页面链接"></label>' +
      button("保存关联", "campus-save-existing-notion", { small: true }) + '</section>';
  }

  function saveExistingNotionLink() {
    var input = document.querySelector("#campus-existing-notion-url");
    if (!input || !input.value.trim() || !input.reportValidity()) return;
    var courseId = state.campus.courseId;
    var epoch = accountQuotaEpoch;
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) + "/existing-notion", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: input.value.trim() })
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "关联未保存。");
      if (state.campus.courseId !== courseId) return;
      state.campus.course = result.body;
      render();
      if (state.campus.home && !state.campus.demoConnection) {
        return requestJson("/api/campus/notion/sync", { method: "POST" }).then(function (started) {
          if (epoch !== accountQuotaEpoch) return;
          if (!started.ok || !started.body) {
            return showToast((started.body && started.body.detail) || "关联已保存，但目录更新未确认；请稍后重试。");
          }
          if (started.body.scope === "background") state.recordings.background = started.body;
          else {
            state.recordings.task = started.body;
            state.recordings.taskCourseId = "";
          }
          render();
        }).catch(function () {
          if (epoch === accountQuotaEpoch) showToast("关联已保存，但目录更新未确认；已有课程可继续查看，请稍后重试。");
        });
      }
    }).catch(function () { showToast("关联未保存。"); });
  }

  function campusCourseScreen() {
    var course = state.campus.course;
    var section = state.campus.section === "quizzes" ? "home" : state.campus.section || "home";
    var sectionLabels = { assignments: "作业中心", notices: "通知中心", quizzes: "小测中心", materials: "课程资料", settings: "课程设置", lecture: "讲次" };
    var trail = [{ label: "我的课程", action: "open-dashboard" }, { label: course ? course.title : "教学网课程", action: section !== "home" ? "campus-course-home" : undefined }];
    if (section !== "home") trail.push({ label: sectionLabels[section] || "课程" });
    var header = breadcrumb(trail);
    // Keep the current course visible while a background refresh replaces it.
    // Replacing a long page with the short loading panel makes the browser
    // clamp the scroll container to zero, which feels like a random jump.
    if (state.campus.loading && !course) return header + '<section class="panel" role="status">正在读取课程目录…</section>';
    if (!course) return header + '<section class="panel" role="alert">' + esc(state.campus.error || "课程尚未同步。") + '</section>';
    var needsTranscription = false;
    var lessons = course.lessons.filter(function (lesson) {
      return (section !== "lecture" || lesson.id === state.campus.lessonId) &&
        (!state.campus.dateQuery || (lesson.date || "").includes(state.campus.dateQuery.trim()));
    }).map(function (lesson, index) {
      if (section !== "lecture") {
        return '<article class="campus-lesson-page"><div><small>第 ' + esc(course.lessons.indexOf(lesson) + 1) + ' 节课 · ' + esc(lesson.date || "日期未提供") + '</small>' +
          '<h3>' + esc(lesson.title) + '</h3>' + (lesson.notion_note_url ? '<span class="recording-published">已有课堂总结</span>' : '') + '</div>' +
          button("进入讲次", "campus-open-lesson", { small: true, attrs: { "data-recording": lesson.id } }) + '</article>';
      }
      var materials = lesson.materials.filter(function (item) {
        return state.campus.category === "all" || item.category === state.campus.category;
      }).map(function (item) {
        return campusMaterial(item, course.id);
      }).join("");
      var publishExisting = Boolean(lesson.publish_ready && !lesson.video_available);
      var canRegenerate = Boolean(lesson.notion_lecture_url && lesson.transcript_available && lesson.notes_available);
      var publishSaved = publishExisting && !canRegenerate;
      needsTranscription = needsTranscription || (!lesson.transcript_available && !publishSaved);
      var details = lesson.duration_seconds ? ' · 约 ' + Math.ceil(lesson.duration_seconds / 60) +
        ' 分钟' : ' · 时长未提供，可先读取时长';
      if (publishSaved) details += ' · 发布已有笔记，不重复消耗转写或 AI 额度';
      else if (lesson.transcript_available) details += ' · 复用已有转写，笔记生成可能消耗 AI 点';
      else if (lesson.duration_seconds) details += ' · 预计转写用量约 ' + Math.ceil(lesson.duration_seconds / 60) + ' 分钟';
      if (lesson.video_size) details += ' · 本地文件 ' + formatTransfer(lesson.video_size);
      var blocked = lesson.unavailable_reason || (course.read_only ? course.read_only_message || "教学网尚未完整读取，旧目录只读。" : courseTaskBusy(course.id) ? "这门课正在更新或处理，请等待当前任务完成。" : state.campus.demoConnection ? "演示模式不会实际整理，请在正式程序中连接 Notion。" : !state.connection.connected ? "请先连接 Notion。" : !state.campus.home ? "请先创建 Notion 学习主页。" : "");
      var review = state.termReview.courseId === course.id && state.termReview.recordingId === lesson.id ? termReviewPanel(lesson) : "";
      return '<article class="recording-row"><div><span class="course-meta">第 ' + esc(course.lessons.indexOf(lesson) + 1) + ' 节课</span><h3>' + esc(lesson.title) +
        '</h3><p>' + esc(lesson.date || "日期未记录") + esc(details) + (lesson.unavailable_reason ? ' · ' + esc(lesson.unavailable_reason) : '') +
        '</p>' + (!lesson.duration_seconds && state.campus.configured ? button('读取时长', 'campus-estimate-duration', { small: true, quiet: true, attrs: { 'data-recording': lesson.id } }) : '') +
        (materials ? '<ul>' + materials + '</ul>' : '<p>暂无明确对应的课件</p>') + '</div><div class="recording-row-actions">' +
        (lesson.notion_lecture_url
          ? '<span class="recording-published">已写入 Notion</span>' + button(lesson.notion_note_url ? "查看总结" : "查看讲次", "campus-open-lecture", {
              small: true, primary: true, attrs: { "data-url": lesson.notion_note_url || lesson.notion_lecture_url }
            }) : '') +
        (!publishExisting || canRegenerate ? recordingNoteStyle(lesson.id, Boolean(blocked)) : "") +
        (canRegenerate
          ? button("重新整理笔记", "campus-regenerate", {
              small: true, quiet: true, disabled: Boolean(blocked),
              attrs: { "data-recording": lesson.id }
            }) + (blocked ? '<small class="action-reason">' + esc(blocked) + '</small>' : '')
          : button(publishExisting ? "发布已有笔记" : "整理这节录像", "campus-process", { small: true, primary: true,
              disabled: Boolean(blocked),
              attrs: { "data-recording": lesson.id } }) + (blocked ? '<small class="action-reason">' + esc(blocked) + '</small>' : '')) +
        (lesson.video_available
          ? '<a class="btn btn-small btn-quiet" href="/api/recordings/' + encodeURIComponent(lesson.id) + '/video" download>下载原始录像</a>'
          : button("下载原始录像", "recording-download", { small: true, quiet: true,
              disabled: !state.campus.configured || courseTaskBusy(course.id) || Boolean(lesson.unavailable_reason),
              attrs: { "data-recording": lesson.id } })) +
        (lesson.transcript_available || lesson.notes_ready ? button("核对术语", "campus-review-term", {
          small: true, quiet: true, attrs: { "data-recording": lesson.id, "aria-expanded": review ? "true" : "false" }
        }) : "") +
        (lesson.transcript_reviewed_pending ? '<small class="recording-published">转写已修正，请重新整理笔记。</small>' : "") +
        '</div>' + review + '</article>';
    }).join("");
    var pendingItems = course.unassigned_materials.filter(function (item) {
      return state.campus.category === "all" || item.category === state.campus.category;
    });
    var hasLessons = course.lessons.length > 0;
    var pending = '<section class="panel"><h2>' + (hasLessons ? '尚未对应讲次的内容' : '课程内容') + '</h2><p>' + (hasLessons ? '这些内容暂时无法可靠对应到某节录像；你可以关联到讲次或忽略匹配。' : '教学网提供的资料、作业和课程信息。可直接下载有附件的内容。') + '</p><ul>' +
      (pendingItems.length ? pendingItems.map(function (item) { return campusMaterial(item, course.id, course.lessons); }).join("") : '<li>' + esc(course.section_states && course.section_states.materials && ['not_synced', 'error', 'cached'].indexOf(course.section_states.materials.state) >= 0 ? course.section_states.materials.message : '当前分类暂无内容。') + '</li>') + '</ul></section>';
    var summary = course.summary && course.summary.url ? course.summary : null;
    var summaryControl = '<div class="course-summary-control">' +
      (summary ? button("打开课程总结", "campus-open-summary", {
        small: true, attrs: { "data-url": summary.url }
      }) + '<small>' + esc(summary.title || "课程总结快照") + '</small>' : "") +
      button(summary ? "重新生成课程总结" : "生成课程总结", "campus-course-summary", {
        small: true, quiet: Boolean(summary),
        disabled: courseTaskBusy(course.id) || state.campus.demoConnection || !state.campus.home,
        attrs: { "data-course": course.id }
      }) + '</div>';
    var hero = '<section class="course-hero' + (section !== 'home' ? ' course-hero-compact' : '') + '" data-course-id="' + esc(course.id) + '"><div><span class="course-kicker">教学网课程</span><h1>' + esc(course.title) +
      '</h1><small>' + esc(course.term || '') + '</small></div><div class="hero-actions">' +
      button("更新这门课", "recording-sync-course", { small: true,
        // Preserve real double-click input; the task guard rejects repeats.
        disabled: !state.campus.configured,
        attrs: { "data-course": course.id,
          "aria-disabled": courseTaskBusy(course.id),
          "style": courseTaskBusy(course.id) ? "opacity:.55;cursor:not-allowed;transform:none" : "" }
      }) + button("课程设置", "campus-course-settings", { small: true, quiet: true }) + '</div></section>';
    var notice = course.read_only ? '<section class="panel" role="status">' + esc(course.read_only_message || "教学网尚未完整读取；旧目录保留为只读，请更新这门课或打开教学网核对。") + '</section>' : '';
    if (state.campus.loading) notice += '<p role="status">正在读取本课最新缓存；已有内容可继续浏览。</p>';
    if (state.campus.error) notice += '<p role="alert">' + esc(state.campus.error) + '</p>';
    if (state.recordings.error && (!state.recordings.errorCourseId || state.recordings.errorCourseId === course.id))
      notice += '<p role="alert">' + esc(state.recordings.error) + '</p>';
    notice += backgroundSyncStatus();
    if (section === "assignments" || section === "notices") return header + hero + notice + recordingTaskStatus() + campusOfficialSections(course, section);
    if (section === "materials") {
      var linkedMaterials = course.lessons.map(function (lesson) {
        var items = lesson.materials.filter(function (item) {
          return state.campus.category === "all" || item.category === state.campus.category;
        });
        return items.length ? '<section class="panel"><h2>' + esc(lesson.title) + '</h2><ul>' +
          items.map(function (item) { return campusMaterial(item, course.id); }).join('') + '</ul></section>' : '';
      }).join('');
      return header + hero + notice + recordingTaskStatus() + summaryControl + linkedMaterials + pending;
    }
    if (section === "settings") return header + hero + notice + recordingTaskStatus() + '<details class="panel campus-settings" open><summary>课程笔记与 Notion 主页设置</summary>' + campusHomeControls() + campusExistingNotionControl(course) + '</details>';
    if (section === "lecture") return header + notice + '<section class="panel" id="course-lessons">' + (needsTranscription ? directOssConsent() : "") + recordingTaskStatus() + lessons + '</section>';
    var centers = [['assignments', '作业中心'], ['notices', '通知中心'], ['materials', '课程资料']].map(function (entry) {
      return '<a href="#course-' + entry[0] + '" data-action="campus-open-center" data-center="' + entry[0] + '">' + esc(entry[1]) + '<span aria-hidden="true">↗</span></a>';
    }).join('');
    var activeTaskNotice = state.recordings.task.state === "running" || state.recordings.task.state === "failed" ||
      ["done", "partial", "cancelled"].indexOf(state.recordings.task.state) >= 0 && state.recordings.task.kind === "sync"
      ? recordingTaskStatus() : "";
    var historical = (course.historical_lessons || []).filter(function (lesson) {
      return lesson.historical === true && lesson.read_only === true && lesson.processing_allowed === false &&
        lesson.course_id === course.id && lesson.history_current_revision === course.catalog_revision;
    }).map(function (lesson) {
      return '<article class="campus-lesson-page" data-history-source="' + esc(lesson.source_id) + '"><div><small>历史讲次 · 只读</small>' +
        '<h3>' + esc(lesson.title) + '</h3><p>原记录时间：' + esc(lesson.recorded_at) + '</p></div>' +
        (lesson.notion_lecture_url && !course.read_only ? button('查看保留讲次', 'campus-open-lecture', {
          small: true, quiet: true, attrs: { 'data-url': lesson.notion_lecture_url }
        }) : '<small>原讲次链接尚未核验；已有内容会保留。</small>') + '</article>';
    }).join('');
    var historicalDirectory = historical ? '<section id="course-history" class="campus-lesson-directory"><div class="section-label"><h2>历史讲次</h2><small>只读</small></div>' +
      '<p>这些讲次已不在教学网现行目录中，原页面和学习内容会保留。</p><div class="campus-lesson-pages">' + historical + '</div></section>' : '';
    return header + hero + notice + activeTaskNotice + '<nav class="campus-centers" aria-label="课程内容">' + centers + '</nav>' +
      '<section id="course-lessons" class="campus-lesson-directory"><div class="section-label"><h2>每节课</h2><small>' + esc(course.lessons.length) + ' 节课</small></div>' +
      (hasLessons ? '<div class="campus-lesson-pages">' + lessons + '</div>' : '<p>' + esc(course.section_states && course.section_states.recordings &&
        course.section_states.recordings.state !== "empty" ? course.section_states.recordings.message : "这门课程暂时没有录像，可进入课程资料查看文件。") + '</p>') + '</section>' + historicalDirectory;
  }

  var campusReviewsRead = null;

  function saveCampusMaterialMatch(index, action) {
    var lectureId = "";
    if (action === "assign") {
      var select = document.querySelector('[data-material-select="' + index + '"]');
      lectureId = select && select.value || "";
      if (!lectureId) return showToast("请先选择录像讲次。");
    }
    return requestJson("/api/campus/courses/" + encodeURIComponent(state.campus.courseId) +
      "/materials/" + encodeURIComponent(index) + "/match", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lecture_id: lectureId, ignore: action === "ignore" })
      }).then(function (result) {
        if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "关联没有保存成功。");
        state.campus.course = result.body;
        render();
      }).catch(function () { showToast("关联没有保存成功。"); });
  }

  function openCampusCourse(courseId) {
    state.view = "campus-course";
    state.campus.section = "home";
    state.campus.lessonId = "";
    state.campus.dateQuery = "";
    state.campus.courseId = courseId;
    rememberCampusCourse(state.campus.course);
    var cached = state.campus.courseCache[courseId];
    state.campus.course = cached && cached.epoch === accountQuotaEpoch ? JSON.parse(JSON.stringify(cached.body)) : null;
    var listed = state.campus.courses.find(function (course) { return course.id === courseId; });
    if (state.campus.course && listed && listed.catalog_revision &&
        listed.catalog_revision !== state.campus.course.catalog_revision) {
      state.campus.course.read_only = true;
      state.campus.course.read_only_message = "课程目录已有更新；先显示旧内容，请等待本课缓存读取完成。";
    }
    state.campus.receipts = []; state.campus.pendingSubmissions = []; state.campus.receiptError = '';
    state.campus.assignmentStatuses = {};
    state.termReview = { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
    state.campus.submission = { selectedId: "", draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } };
    loadCampusCourse(courseId);
  }

  function termReviewPanel(lesson) {
    var review = state.termReview;
    var content = "";
    if (review.status === "loading") content = '<p role="status">正在查找需要核对的片段…</p>';
    else if (review.status === "none") content = lesson.transcript_reviewed_pending
      ? '<p>这节录像的转写已修正。请使用「重新整理笔记」生成新版笔记。</p>'
      : '<p>这节录像目前没有待核对的术语。</p>';
    else if (review.status === "corrected") content = '<p role="status">已修正转写中的术语。请使用「重新整理笔记」生成新版笔记，并对照录像检查结果。</p>';
    else if (review.target) {
      var target = review.target;
      content = '<p>自动转写对这一处术语不确定，可能把 RSA 识别成 RAC。请听原声并阅读上下文；只有确认老师说的是 RSA，才勾选并提交。</p>' +
        '<blockquote class="term-review-excerpt">' + esc(target.excerpt) + '</blockquote>' +
        '<p class="term-review-time">建议核听 ' + esc(target.start_label || "") + '–' + esc(target.end_label || "") + '</p>' +
        (review.videoAvailable && target.video_url
          ? '<video controls preload="metadata" playsinline data-term-review-video data-seek="' + esc(Math.max(0, Number(target.start) - 8) || 0) + '" src="' + esc(target.video_url) + '">浏览器无法播放这节录像。</video>'
          : '<p>这节录像尚未保存在电脑上。请先使用上方的录像下载操作，再回来核听。</p>') +
        (review.videoAvailable && target.video_url ? '<label class="term-review-confirm"><input type="checkbox" data-term-review-confirm' + (review.confirmed ? ' checked' : '') + (review.status === "submitting" ? ' disabled' : '') + '>我已听原声，确认老师说的是 RSA。</label>' +
          button("确认修正为 RSA", "campus-submit-term-review", { small: true, primary: true, disabled: !review.confirmed || review.status === "submitting" }) : "");
    }
    return '<section class="term-review-panel" aria-label="' + esc(lesson.title) + '术语核对"><div class="term-review-heading"><h4>核对术语</h4>' +
      button("收起", "campus-close-term-review", { small: true, quiet: true }) + '</div>' + content +
      (review.error ? '<p class="term-review-error" role="alert">' + esc(review.error) + '</p>' : '') + '</section>';
  }

  function reviewCampusHandbook(target) {
    var courseId = state.campus.courseId;
    var url = '/api/campus/courses/' + encodeURIComponent(courseId) +
      '/materials/' + encodeURIComponent(target.dataset.material) +
      '/files/' + encodeURIComponent(target.dataset.file);
    target.disabled = true;
    target.textContent = '正在读取手册…';
    return fetch(url, { cache: 'no-store' }).then(function (response) {
      if (!response.ok) return response.json().catch(function () { return {}; }).then(function (body) {
        throw new Error(body.detail || '手册暂时无法读取，请稍后再试。');
      });
      var cached = response.headers.get('X-Handbook-Cached') === 'true';
      return response.blob().then(function () {
        if (!cached) throw new Error('已取得附件，但它不是可读取的 PDF；请下载原文件人工核对。');
        return loadCampusCourse(courseId).then(function () {
          if (state.campus.courseId !== courseId) return;
          if (state.campus.error || !state.campus.course) {
            throw new Error(state.campus.error || '课程页暂时无法刷新，请稍后重试。');
          }
          var card = (state.campus.course.handbook_reviews || []).find(function (item) {
            return String(item.material_index) === String(target.dataset.material) &&
              String(item.file_index) === String(target.dataset.file);
          });
          showToast(card && card.pages && card.pages.length ?
            '已读取手册；请对照原 PDF 和老师最新通知核对。' :
            'PDF 已保存，但未提取出考核正文；请打开原文件人工核对。');
        });
      });
    }).catch(function (error) {
      showToast(error.message || '手册暂时无法读取，请稍后再试。');
    }).finally(function () {
      if (document.contains(target)) { target.disabled = false; target.textContent = '读取手册并显示要求'; }
    });
  }

  async function moveCampusHome() {
    if (!state.campus.home || !state.campus.parentId) return showToast("请先选择新的 Notion 父页面。");
    if (state.campus.parentId === state.campus.home.parent_id) return;
    if (!await confirmAction("将现有学习主页及其中的课程内容一起移动到所选页面？")) return;
    return requestJson("/api/campus/notion/workspace/move", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_page_id: state.campus.parentId }) }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "学习主页没有移动成功。");
      state.campus.home = result.body;
      render();
      showToast("学习主页已移动，原有内容保留。");
    });
  }

  async function downloadRecording(recordingId) {
    if (!await confirmAction("确认只下载这节录像到本机？不会转写，也不会消耗转写或 AI 额度。")) return;
    return requestJson("/api/recordings/" + encodeURIComponent(recordingId) + "/download", { method: "POST" }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "录像下载没有开始。");
      state.recordings.task = result.body;
      state.recordings.taskCourseId = state.campus.courseId;
      render();
    });
  }

  function estimateCampusDuration(recordingId) {
    var courseId = state.campus.courseId;
    var target = document.querySelector('[data-action="campus-estimate-duration"][data-recording="' + recordingId + '"]');
    if (target) { target.disabled = true; target.textContent = "正在读取…"; }
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) + "/recordings/" +
      encodeURIComponent(recordingId) + "/duration", { method: "POST" }).then(function (result) {
      if (!result.ok || !result.body) { showToast((result.body && result.body.detail) || "暂时无法读取时长。"); return; }
      if (state.campus.courseId !== courseId) return;
      var lesson = state.campus.course && state.campus.course.lessons.find(function (item) { return item.id === recordingId; });
      if (lesson) lesson.duration_seconds = result.body.duration_seconds;
      if (!result.body.duration_seconds) showToast("教学网仍未提供可用时长，请在确认整理前留意剩余额度。");
      render();
    }).catch(function () { showToast("暂时无法读取时长。"); }).finally(function () {
      if (target && target.isConnected) { target.disabled = false; target.textContent = "读取时长"; }
    });
  }

  async function startCourseSummary(courseId) {
    if (courseTaskBusy(courseId)) {
      return showToast("请等待当前任务完成后再生成课程总结。");
    }
    var course = state.campus.course;
    var scope = courseId && course && course.id === courseId
      ? "「" + course.title + "」" : "全部已同步课程";
    if (!await confirmAction("生成" + scope + "的《课程总结》快照？" +
        "应用会汇总本机讲次页、老师口头线索和教学网正式作业，只新建今天的快照页，" +
        "不覆盖已有页面，也不新造知识点；此操作不消耗转写或 AI 笔记额度。")) return;
    var path = courseId
      ? "/api/campus/courses/" + encodeURIComponent(courseId) + "/summary"
      : "/api/campus/notion/summaries";
    return requestJson(path, { method: "POST" }).then(function (result) {
      if (!result.ok || !result.body) {
        return showToast((result.body && result.body.detail) || "课程总结没有开始。");
      }
      state.recordings.task = result.body;
      state.recordings.taskCourseId = courseId || "";
      render();
    }).catch(function () { showToast("课程总结没有开始。"); });
  }

  async function processCampusRecording(recordingId, regenerate) {
    var course = state.campus.course;
    var lesson = course && course.lessons.find(function (item) { return item.id === recordingId; });
    if (!lesson || !state.campus.home) return;
    var directOss = state.recordings.directOss && !lesson.transcript_available && !regenerate;
    var notesStrategy = state.recordings.noteStrategyByKey[recordingId] || "transcript";
    var hasMatchedSlides = (lesson.materials || []).some(function (item) {
      return item.category === "material" && (item.files || []).some(function (name) {
        return /\.(pdf|pptx|docx)$/i.test(name);
      });
    });
    var sourceWarning = hasMatchedSlides || notesStrategy !== "topics" ? "" :
      "当前没有明确关联的课件，笔记可能主要依据自动转写；建议先在下方把对应课件关联到这节录像，再整理。";
    var reuseExisting = Boolean(lesson.publish_ready && !lesson.video_available && !regenerate);
    var confirmation = regenerate
      ? "确认重新整理「" + lesson.title + "」？将使用本机已有转写，重新生成笔记并发布一个新版 Notion 页面。旧笔记和旧页面会保留；AI 笔记会再次消耗额度。"
      : reuseExisting
        ? "确认发布「" + lesson.title + "」的已有笔记？将复用本机转写、笔记和课堂画面，不重新下载录像，也不重复消耗转写或 AI 笔记额度。"
        : lesson.transcript_available
          ? "确认整理「" + lesson.title + "」？将复用本机已有转写，生成笔记并写入对应 Notion 讲次。不会重新转写，AI 笔记会消耗额度。"
          : "确认整理「" + lesson.title + "」？将下载录像、转写并将笔记写入对应 Notion 讲次。转写与 AI 笔记会消耗额度。";
    if (!reuseExisting) confirmation += notesStrategy === "topics"
      ? "本次按知识点分段整理，再按课堂顺序合并，保留解释与例子；明确关联的课件文字会与原转写一同发送到笔记服务，辅助辨认术语。"
      : "本次保留讲述顺序，依据原转写整理。";
    if (!await confirmAction(sourceWarning + confirmation +
        (directOss && !regenerate && !reuseExisting ? "本次音频会经短时授权直传阿里云 OSS。" : ""))) return;
    return requestJson("/api/campus/courses/" + encodeURIComponent(course.id) + "/recordings/" + encodeURIComponent(recordingId) + "/process",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ direct_oss: reuseExisting ? false : directOss, regenerate: Boolean(regenerate), reuse_existing: reuseExisting }, reuseExisting ? {} : { notes_strategy: notesStrategy })) }).then(function (result) {
        if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "整理没有开始。");
        state.recordings.task = result.body;
        state.recordings.directOss = false;
        state.recordings.taskCourseId = course.id;
        render();
      });
  }

  /* -- auth (product email+password) ------------------------------------ */

  function authModeCopy() {
    var modes = COPY.auth;
    if (state.authMode === "register") return modes.register;
    if (state.authMode === "forgot") return modes.forgot;
    return modes.login;
  }

  function authScreen() {
    var copy = COPY.auth;
    var mode = authModeCopy();
    var note = state.notice || "";
    var noteClass = state.notice && state.noticeKind === "error" ? "form-note" : "form-note hint";
    var passwordField =
      state.authMode === "forgot"
        ? ""
        : '<label class="field-label" for="auth-password">' +
          esc(copy.password_label) +
          '</label><input id="auth-password" type="password" class="text-input" autocomplete="' +
          (state.authMode === "register" ? "new-password" : "current-password") +
          '" placeholder="' +
          esc(copy.password_placeholder) +
          '" required>';
    var confirmField = state.authMode === "register"
      ? '<label class="field-label" for="auth-password-confirm">' +
        esc(copy.confirm_password_label) +
        '</label><input id="auth-password-confirm" type="password" class="text-input" autocomplete="new-password" placeholder="' +
        esc(copy.confirm_password_placeholder) +
        '" required>'
      : "";
    var switches = "";
    if (state.authMode === "login") {
      switches =
        '<div class="auth-switches">' +
        button(mode.switch_register, "auth-mode-register", { quiet: true, small: true }) +
        button(mode.switch_forgot, "auth-mode-forgot", { quiet: true, small: true }) +
        "</div>";
    } else if (state.authMode === "register") {
      switches =
        '<div class="auth-switches">' +
        button(mode.switch_login, "auth-mode-login", { quiet: true, small: true }) +
        "</div>";
    } else {
      switches =
        '<div class="auth-switches">' +
        button(mode.switch_login, "auth-mode-login", { quiet: true, small: true }) +
        "</div>";
    }
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
      '<section class="onboarding-main"><div class="onboarding-card" aria-labelledby="auth-title">' +
      '<header><span class="mini-status"><span aria-hidden="true">●</span> ' +
      esc(copy.mini_status) +
      '</span><span class="brand-name" style="font-size:12px">' +
      esc(copy.role) +
      "<small>" +
      esc(copy.modes[state.authMode] || copy.modes.login) +
      "</small></span></header>" +
      '<p class="eyebrow">' +
      esc(mode.eyebrow) +
      '</p><h2 class="card-title" id="auth-title">' +
      esc(mode.title) +
      '</h2><p class="card-subtitle">' +
      esc(mode.subtitle) +
      "</p>" +
      '<form id="auth-form" data-auth-mode="' + esc(state.authMode) + '" aria-busy="' + state.authPending + '"><label class="field-label" for="auth-email">' +
      esc(copy.email_label) +
      '</label><input id="auth-email" type="email" class="text-input" autocomplete="username" placeholder="' +
      esc(copy.email_placeholder) +
      '" required>' +
      passwordField +
      confirmField +
      '<div class="auth-actions">' +
      button(state.authPending ? mode.submitting : mode.submit, "auth-submit", { primary: true, type: "submit", disabled: state.authPending }) +
      "</div>" +
      '<p id="auth-help" role="' + (state.noticeKind === "error" ? "alert" : "status") + '" class="' + noteClass + '">' + esc(note) + "</p>" +
      "</form>" +
      switches + button("使用引导", "open-welcome", { quiet: true, small: true }) +
      "</div></section></main>"
    );
  }

  /* -- account onboarding --------------------------------------------- */

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

  function connectionCard(location) {
    var copy = COPY.connection;
    return (
      '<div class="account-card" data-block="connection"><div class="account-name">' +
      '<span class="avatar" aria-hidden="true">N</span>' +
      esc(copy.manage_label) +
      '</div><p data-field="connection-status">' +
      esc(state.connection.status_label) +
      "</p>" +
      (state.connection.error ? '<p class="form-note" role="alert">' + esc(state.connection.error) + "</p>" : "") +
      (location !== 'mobile' && state.connection.connected && !state.connection.error
        ? '<details class="account-actions" data-persistent-disclosure="desktop-connection"><summary>管理连接</summary>' + connectionControls(true) + '</details>'
        : connectionControls(true)) +
      "</div>"
    );
  }

  function quotaCard(location) {
    var suffix = location === "mobile" ? "-mobile" : "";
    var copy = COPY.account;
    var quota = state.quota;
    if (quota === null || quota.active === false) {
      return (
        '<div class="account-card" data-block="quota"><div class="account-name">' +
        esc(copy.title) +
        "</div><p>" +
        esc(copy.signed_out) +
        "</p></div>"
      );
    }
    var email = accountEmail();
    var verified = accountVerified();
    var statusLine = email
      ? '<p class="account-email">' +
        esc(email) +
        "</p><p class=\"account-verify\">" +
        esc(verified ? copy.verified : copy.unverified) +
        "</p>"
      : "<p>" + esc(copy.verified) + "</p>";
    var balance =
      quota.available === false
        ? "<p>额度暂时无法读取</p>"
        : "<p>转写 " +
          esc(
            Math.floor(
              (quota.transcribe_seconds_remaining === undefined
                ? 0
                : quota.transcribe_seconds_remaining) / 60
            )
          ) +
          " 分钟</p><p>AI 点 " +
          esc(quota.llm_points_remaining) +
          "</p>";
    if (state.quotaStale) {
      balance += '<p class="form-note hint" role="status">' + esc(copy.quota_stale) + '</p>';
    }
    var redeemBlock = "";
    if (!verified && email) {
      redeemBlock =
        '<p class="form-note hint">' +
        esc(copy.unverified_help) +
        "</p>" +
        button(copy.resend, "resend-verification", { small: true, quiet: true }) +
        button(copy.refresh_verification, "refresh-verification", { small: true, quiet: true }) +
        '<p class="form-note hint">' +
        esc(copy.redeem_gated) +
        "</p>";
    } else {
      redeemBlock =
        '<form id="redeem-form' + suffix + '" class="redeem-form" data-redeem-form><label class="field-label" for="code' + suffix + '">' +
        esc(copy.redeem_label) +
        '</label><div class="input-row">' +
        '<input id="code' + suffix + '" type="text" class="text-input" autocomplete="off" placeholder="' +
        esc(copy.redeem_placeholder) +
        '" required>' +
        button(copy.redeem, "redeem", { primary: true, small: true, disabled: redeemPending }) +
        "</div><p class=\"form-note hint\">" +
        esc(copy.redeem_help) +
        "</p></form>";
    }
    return (
      '<div class="account-card" data-block="quota"><div class="account-name">' +
      esc(copy.title) +
      "</div>" +
      balance +
      (location === 'mobile'
        ? statusLine + redeemBlock + button(copy.logout, "logout", { small: true, quiet: true })
        : '<details class="account-actions" data-persistent-disclosure="desktop-account"' + (!verified && email ? ' open' : '') +
          '><summary>账户与额度设置</summary>' + statusLine + redeemBlock + button(copy.logout, "logout", { small: true, quiet: true }) + '</details>') +
      "</div>"
    );
  }

  function updateCard() {
    var update = state.update;
    if (!update) return "";
    var copy = COPY.update;
    var body = "<p>" + esc(template(copy.version, { version: update.version })) + "</p>";
    if (update.status === "desktop_managed") {
      body += "<p>" + esc(copy.desktop_managed) + "</p>";
    } else if (update.status === "available") {
      body += "<p>" + esc(template(copy.available, { version: update.available_version })) + "</p>";
      body += button(copy.action, "request-update", {
        small: true,
        attrs: { "data-version": update.available_version }
      });
    } else if (update.status === "up_to_date") {
      body += "<p>" + esc(copy.up_to_date) + "</p>";
    } else if (update.status === "requested") {
      body += "<p>" + esc(copy.requested) + "</p>";
    } else if (update.status === "deferred") {
      body += "<p>" + esc(copy.deferred) + "</p>";
      body += button(copy.retry, "request-update", {
        small: true,
        attrs: { "data-version": update.available_version }
      });
    }
    return '<div class="account-card" data-block="update">' + body + "</div>";
  }

  function requestUpdate(version) {
    return requestJson("/api/update/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: version })
    }).then(function (result) {
      if (result.ok && result.body) {
        state.update = result.body;
      } else {
        showToast(COPY.update.request_failed);
      }
      render();
    }).catch(function () {
      showToast(COPY.update.request_failed);
    });
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
      '</button>' + button('使用引导', 'open-welcome', { quiet: true }) + '</nav><div class="sidebar-spacer"></div>' +
      '<div class="account-card campus-account"><div class="account-name">教学网账号</div><p>' + (state.campus.configured ? '已保存' : '尚未连接') + '</p>' + button("连接设置", "campus-account-settings", { small: true, quiet: true }) + '</div>' +
      connectionCard() +
      quotaCard() +
      updateCard() +
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
      '</div><details class="mobile-account-menu" data-persistent-disclosure="mobile-account"><summary>账户与额度</summary><div class="mobile-account-content">' + button('使用引导', 'open-welcome', { small: true, quiet: true }) + button("连接设置", "campus-account-settings", { small: true, quiet: true }) + connectionCard("mobile") + quotaCard("mobile") + updateCard() + '</div></details></header>' +
      '<main class="main"><div class="main-inner">' +
      content +
      "</div></main></div>"
    );
  }

  /* -- dashboard --------------------------------------------------------- */

  function dashboardScreen() {
    return campusDashboardScreen();
  }

  /* -- course detail ----------------------------------------------------- */

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

  function formatTransfer(bytes) {
    if (!bytes) return "0 KB";
    if (bytes < 1048576) return (bytes / 1024).toFixed(bytes >= 102400 ? 0 : 1) + " KB";
    return (bytes / 1048576).toFixed(bytes >= 104857600 ? 0 : 1) + " MB";
  }

  function termReviewUrl(courseId, recordingId) {
    return "/api/campus/courses/" + encodeURIComponent(courseId) + "/recordings/" +
      encodeURIComponent(recordingId) + "/term-review";
  }

  function openTermReview(recordingId) {
    var courseId = state.campus.courseId;
    var current = state.termReview;
    if (current.courseId === courseId && current.recordingId === recordingId) {
      state.termReview = { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
      render();
      return;
    }
    var review = { courseId: courseId, recordingId: recordingId, status: "loading", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
    state.termReview = review;
    render();
    return requestJson(termReviewUrl(courseId, recordingId)).then(function (result) {
      if (state.termReview !== review || state.campus.courseId !== courseId) return;
      if (!result.ok || !result.body) {
        review.status = "error";
        review.error = (result.body && result.body.detail) || "暂时无法读取待核对片段，请稍后再试。";
      } else {
        review.status = result.body.status === "needs_review" && result.body.target ? "needs_review" : "none";
        review.target = result.body.target || null;
        review.transcriptSha256 = result.body.transcript_sha256 || "";
        review.videoAvailable = Boolean(result.body.video_available);
      }
      render();
    }).catch(function () {
      if (state.termReview !== review || state.campus.courseId !== courseId) return;
      review.status = "error";
      review.error = "暂时无法读取待核对片段，请稍后再试。";
      render();
    });
  }

  function submitTermReview() {
    var review = state.termReview;
    if (review.status !== "needs_review" || !review.confirmed || !review.target || !review.transcriptSha256) return;
    review.status = "submitting";
    review.error = "";
    render();
    return requestJson(termReviewUrl(review.courseId, review.recordingId), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segment_index: review.target.segment_index,
        transcript_sha256: review.transcriptSha256, confirmed_spoken_term: "RSA", confirmed: true })
    }).then(function (result) {
      if (state.termReview !== review || state.campus.courseId !== review.courseId) return;
      if (!result.ok || !result.body || result.body.status !== "corrected") {
        review.status = "needs_review";
        review.error = (result.body && result.body.detail) || "修正没有保存，请重新核听后重试。";
        review.confirmed = false;
        render();
        return;
      }
      review.status = "corrected";
      review.confirmed = false;
      return loadCampusCourse(review.courseId);
    }).catch(function () {
      if (state.termReview !== review || state.campus.courseId !== review.courseId) return;
      review.status = "needs_review";
      review.confirmed = false;
      review.error = "修正没有保存，请稍后重试。";
      render();
    });
  }

  function formatAudioPosition(samples) {
    var seconds = Math.floor(Math.max(0, Number(samples) || 0) / 16000);
    var minutes = Math.floor(seconds / 60);
    return minutes + ":" + String(seconds % 60).padStart(2, "0");
  }

  function taskUnitName(task) {
    var unit = task.progress_unit;
    if (unit === "bytes") return "未传输";
    if (unit === "segments") return "段录像";
    if (unit === "audio_extract") return "音频";
    if (unit === "audio_parts") return "段音频";
    if (unit === "笔记段") return "部分笔记";
    if (unit === "courses") return "门课程";
    return "项";
  }

  function taskRemainingText(task, remaining) {
    if (task.progress_unit === "bytes") return formatTransfer(remaining);
    if (task.progress_unit === "audio_extract") return formatAudioPosition(remaining);
    return String(remaining);
  }

  function recordingTaskProgress(task) {
    var done = Math.max(0, Number(task.progress_done) || 0);
    var total = Math.max(0, Number(task.progress_total) || 0);
    var remaining = Math.max(0, Number(task.progress_remaining) || (total ? total - done : 0));
    var percent = total ? Math.min(100, Math.floor(done / total * 100)) : 0;
    var detail = task.progress_unit === "bytes"
      ? (total ? formatTransfer(done) + " / " + formatTransfer(total) : formatTransfer(task.bytes_done))
      : task.progress_unit === "segments" ? done + " / " + total + " 段录像"
      : task.progress_unit === "audio_extract" ? "已处理 " + formatAudioPosition(done) +
        (total ? " / " + formatAudioPosition(total) : "") + " 音频"
      : task.progress_unit === "audio_parts" ? done + " / " + total + " 段音频"
      : task.progress_unit === "笔记段" ? "已完成 " + done + " / " + total + " 部分笔记"
      : task.progress_unit === "courses" ? "已完成 " + done + " / " + total + " 门课程"
      : task.progress_unit === "items" ? "已完成 " + done + " / " + total + " 项"
      : task.progress_unit === "remux" ? "正在封装录像文件" : "请稍候";
    if (total && task.progress_unit !== "remux") {
      detail += " · 剩余 " + taskRemainingText(task, remaining) + " " + taskUnitName(task);
    }
    if (task.speed_bps && (task.progress_unit === "bytes" || task.progress_unit === "segments")) {
      detail += " · " + formatTransfer(task.speed_bps) + "/秒";
    }
    if (task.current_item) detail += " · " + task.current_item;
    return '<div class="recording-progress"><progress aria-label="' + esc(task.stage) + '进度" ' +
      (total ? 'value="' + esc(done) + '" max="' + esc(total) + '"' : '') + '></progress>' +
      '<span>' + esc(total ? percent + "% · " + detail : detail) + '</span></div>';
  }

  function recordingTaskStatus() {
    var task = state.recordings.task;
    if (!task || task.state === "idle") return "";
    var activeCourseId = state.campus.courseId;
    if (state.recordings.taskCourseId && state.recordings.taskCourseId !== activeCourseId && task.kind === "process") return "";
    if (state.recordings.taskCourseId && state.recordings.taskCourseId !== activeCourseId &&
        task.kind === "sync" && state.view === "campus-course") return "";
    var activeStage = task.progress_unit === "audio_extract" ? "提取音轨" :
      task.progress_unit === "audio_parts" ? "上传音频并转写" : task.stage;
    var stepPrefix = task.stage_total
      ? "第 " + Math.max(1, Number(task.stage_index) || 1) + "/" + task.stage_total + " 步 · " : "";
    var message = task.state === "running" ? stepPrefix + "正在" + activeStage + "…" :
      task.state === "done" || task.state === "partial" || task.state === "cancelled" ? task.stage : task.error || "处理未完成，请稍后重试。";
    if ((task.kind === "sync" || task.kind === "summary") && task.state === "done" && Number(task.summary_created) === 0 && Number(task.summary_existing) > 0)
      message = (task.kind === "sync" ? "课程目录更新完成；" : "") + "复用已有课程总结 " + task.summary_existing + " 页，本轮未生成新总结。";
    message = campusReadError(message);
    var progress = task.state === "running" ? recordingTaskProgress(task) : "";
    var warnings = (task.warnings || []).length
      ? '<small class="recording-warning">' + esc((task.warnings || []).slice(0, 2).map(campusReadError).join("；")) +
        ((task.warnings || []).length > 2 ? "…" : "") + '</small>' : "";
    var failure = task.state === "failed" && task.failure_id
      ? '<small class="recording-warning">故障编号：' + esc(task.failure_id) + ' · 再次失败时请附上此编号</small>'
      : "";
    return '<div class="recording-task" role="status" aria-live="polite"><div class="recording-task-text">' + esc(message) +
      (task.state === "running" && task.kind === "sync" ? button("取消目录更新", "recording-cancel-sync", { small: true, quiet: true }) : "") +
      (task.state === "done" && task.lecture_url ? button("打开这节课", "recording-open-result", { small: true, attrs: { "data-url": task.lecture_url } }) : "") +
      (task.state === "done" && task.result_url ? (task.kind === "download"
        ? '<a class="btn btn-small btn-quiet" href="' + esc(task.result_url) + '" download>保存录像到电脑</a>'
        : button(task.kind === "summary" ? "打开课程总结" : task.kind === "sync" ? "查看更新结果" : "查看笔记", "recording-open-result", { small: true, quiet: true, attrs: { "data-url": task.result_url } })) : "") +
      warnings + failure +
      '</div>' + progress + '</div>';
  }

  function backgroundSyncStatus() {
    var task = state.recordings.background;
    if (!task || task.state === "idle") return "";
    var labels = { pending: "等待更新", running: "正在更新", done: "完成", confirmed: "已核对", not_read: "尚未读取", unconfirmed: "尚未核对", failed: "失败", partial: "部分完成", cancelled: "已取消", interrupted: "中断待恢复", unknown: "状态未确认" };
    var rows = (task.course_results || []).map(function (row) {
      var course = state.campus.courses.find(function (item) { return item.id === row.course_id; });
      return '<li data-sync-course="' + esc(row.course_id) + '" data-sync-state="' + esc(row.state) + '"><strong>' + esc(row.title || course && course.title || row.course_id) + '</strong> · ' + esc(labels[row.state] || "状态未确认") +
        '<small>教学网：' + esc(labels[row.metadata_state] || "状态未确认") + ' · Notion：' + esc(labels[row.projection_state] || "状态未确认") + '</small>' +
        (row.summary_created ? '<small>本轮新建总结 ' + esc(row.summary_created) + ' 页</small>' : '') +
        (row.summary_existing ? '<small>复用已有总结 ' + esc(row.summary_existing) + ' 页</small>' : '') +
        (row.error ? '<p role="alert">' + esc(campusReadError(row.error)) + '</p>' : '') +
        (row.failure_id ? '<small>故障编号：' + esc(row.failure_id) + '</small>' : '') + '</li>';
    }).join('');
    return '<section class="panel" data-background-sync data-background-state="' + esc(task.state) + '" role="status" aria-live="polite"><h2>后台课程更新</h2><p data-background-stage>' +
      esc(campusReadError(task.stage || labels[task.state])) + '</p><p>已完成课程可继续使用；未完整课程保留已有内容。</p>' +
      (task.state === "running" ? recordingTaskProgress(task) + button("取消后台更新", "recording-cancel-background", { small: true, quiet: true, disabled: state.recordings.backgroundPending }) : '') +
      (['failed', 'partial', 'cancelled', 'interrupted'].indexOf(task.state) >= 0 ? button("恢复未完成课程", "recording-resume-background", { small: true, disabled: state.recordings.backgroundPending }) : '') +
      (task.error ? '<p role="alert">' + esc(campusReadError(task.error)) + '</p>' : '') + '<ul>' + rows + '</ul></section>';
  }

  function updateBackgroundSync(resume) {
    if (state.recordings.backgroundPending) return Promise.resolve();
    var epoch = accountQuotaEpoch;
    state.recordings.backgroundPending = true;
    render();
    return requestJson(resume ? "/api/recordings/sync" : "/api/recordings/cancel-sync", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(resume ? { scope: "background", resume: true } : { scope: "background" })
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body || result.body.scope !== "background") return recordingError(campusReadError(result.body && result.body.detail));
      state.recordings.background = result.body;
    }).catch(function () {
      if (epoch === accountQuotaEpoch) recordingError("后台更新结果未确认；已有内容保留，请读取任务状态后再恢复。");
    }).finally(function () {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.backgroundPending = false;
      render();
    });
  }

  /* -- lecture detail ---------------------------------------------------- */

  /* The per-row association label. “已关联本讲” requires an explicit link to
     the SELECTED lecture — an explicit link to another lecture is
     course-level from this lecture's perspective (approved prototype
     semantics) — and an inferred association stays visibly 待确认, never
     已关联本讲. The two 待确认 semantics stay structurally distinct: this
     badge under the title vs the 处理状态 pill in its own column. */

  function captureCredentialFields(ids) {
    var draft = { fields: {}, focus: null };
    ids.forEach(function (id) {
      var field = document.getElementById(id);
      if (!field) return;
      draft.fields[id] = field.value;
      if (document.activeElement === field) draft.focus = { id: id, start: field.selectionStart, end: field.selectionEnd };
    });
    return draft;
  }

  function restoreCredentialFields(draft) {
    if (!draft) return;
    Object.keys(draft.fields).forEach(function (id) {
      var field = document.getElementById(id);
      if (field) field.value = draft.fields[id];
    });
    if (draft.focus) {
      var field = document.getElementById(draft.focus.id);
      if (field) {
        field.focus({ preventScroll: true });
        if (draft.focus.start !== null) field.setSelectionRange(draft.focus.start, draft.focus.end);
      }
    }
  }

  function updateAuthControls() {
    var form = document.getElementById("auth-form");
    if (!form) return;
    form.setAttribute("aria-busy", String(state.authPending));
    form.querySelectorAll("input").forEach(function (field) { field.readOnly = state.authPending; });
    document.querySelectorAll('[data-action^="auth-mode-"]').forEach(function (control) { control.disabled = state.authPending; });
    var submit = form.querySelector('[data-action="auth-submit"]');
    submit.disabled = state.authPending;
    submit.setAttribute("aria-busy", String(state.authPending));
    submit.textContent = state.authPending ? authModeCopy().submitting : authModeCopy().submit;
    var help = document.getElementById("auth-help");
    help.textContent = state.notice;
    help.className = state.noticeKind === "error" ? "form-note" : "form-note hint";
    help.setAttribute("role", state.noticeKind === "error" ? "alert" : "status");
  }

  function render() {
    if (welcomeOpen) {
      if (!document.querySelector('.welcome-screen')) app.innerHTML = welcomeScreen();
      return;
    }
    // Keep an in-progress code only in this account's current DOM. Never
    // carry it through logout/login or persist it to browser storage.
    var sameAccount = state.activated && renderedAccountEpoch === accountQuotaEpoch &&
      renderedAccountEmail === accountEmail();
    var authForm = document.getElementById("auth-form");
    var credentialDraft = (!state.activated && authForm && authForm.dataset.authMode === state.authMode)
      ? captureCredentialFields(["auth-email", "auth-password", "auth-password-confirm"])
      : sameAccount ? captureCredentialFields(["recording-username", "recording-password"]) : null;
    var codeDrafts = {};
    var focusedCode = null;
    if (sameAccount) {
      document.querySelectorAll('[data-redeem-form] input[type="text"]').forEach(function (field) {
        codeDrafts[field.id] = field.value;
        if (document.activeElement === field) {
          focusedCode = { id: field.id, start: field.selectionStart, end: field.selectionEnd,
            direction: field.selectionDirection };
        }
      });
    }
    var disclosures = {};
    document.querySelectorAll('[data-persistent-disclosure]').forEach(function (element) {
      disclosures[element.dataset.persistentDisclosure] = element.open;
    });
    var openSettings = document.querySelector(".campus-settings[open] > summary");
    var settingsTitle = openSettings ? openSettings.textContent : "";
    var sidebar = document.querySelector(".sidebar");
    var sidebarScroll = sidebar ? sidebar.scrollTop : 0;
    var main = document.querySelector(".main");
    var mainScroll = main ? main.scrollTop : 0;
    var windowScroll = window.scrollY || window.pageYOffset || 0;
    var activeField = document.activeElement && document.activeElement.closest &&
      document.activeElement.closest("#assignment-prepare-form") ? document.activeElement.name : "";
    var caret = activeField && typeof document.activeElement.selectionStart === "number" ?
      document.activeElement.selectionStart : null;
    if (!state.activated) {
      // Background reads must not replace a field while it is being typed into.
      if (!authForm || authForm.dataset.authMode !== state.authMode) app.innerHTML = authScreen();
      renderedAccountEpoch = -1;
      renderedAccountEmail = "";
      restoreCredentialFields(credentialDraft);
      updateAuthControls();
      return;
    }
    var screen;
    if (state.view === "campus-course") screen = campusCourseScreen();
    else if (state.view === "campus-account") screen = campusDashboardScreen();
    else if (state.view === "dashboard") screen = dashboardScreen();
    else screen = dashboardScreen();
    app.innerHTML = shell(screen);
    renderedAccountEpoch = accountQuotaEpoch;
    renderedAccountEmail = accountEmail();
    restoreCredentialFields(credentialDraft);
    Object.keys(codeDrafts).forEach(function (id) {
      var field = document.getElementById(id);
      if (field) field.value = codeDrafts[id];
    });
    document.querySelectorAll('[data-persistent-disclosure]').forEach(function (element) {
      var key = element.dataset.persistentDisclosure;
      if (Object.prototype.hasOwnProperty.call(disclosures, key)) element.open = disclosures[key];
    });
    var settings = document.querySelector(".campus-settings");
    if (settings && settingsTitle && settings.querySelector("summary").textContent === settingsTitle) settings.open = true;
    sidebar = document.querySelector(".sidebar");
    if (sidebar && sameAccount) sidebar.scrollTop = sidebarScroll;
    main = document.querySelector(".main");
    if (main) main.scrollTop = mainScroll;
    if (windowScroll) window.scrollTo(0, windowScroll);
    if (focusedCode) {
      var codeField = document.getElementById(focusedCode.id);
      if (codeField) {
        codeField.focus({ preventScroll: true });
        codeField.setSelectionRange(focusedCode.start, focusedCode.end, focusedCode.direction);
      }
    }
    if (activeField) {
      var replacement = document.querySelector('#assignment-prepare-form [name="' + activeField + '"]');
      if (replacement) {
        replacement.focus({ preventScroll: true });
        if (caret !== null && replacement.setSelectionRange) replacement.setSelectionRange(caret, caret);
      }
    }
  }

  function showToast(message) {
    toastEl.textContent = message;
    toastEl.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastEl.classList.remove("show");
    }, 2800);
  }

  function focusAuthEmail() {
    var input = document.getElementById("auth-email");
    if (input) input.focus();
  }

  function focusCode(form) {
    var input = form ? form.querySelector('input[type="text"]') : document.getElementById("code");
    if (input) input.focus();
  }

  /* -- actions ----------------------------------------------------------- */

  function setAuthMode(mode) {
    if (state.authPending) return;
    var email = document.getElementById("auth-email");
    var emailDraft = email ? email.value : "";
    state.authMode = mode;
    state.notice = "";
    state.noticeKind = "hint";
    render();
    document.getElementById("auth-email").value = emailDraft;
    focusAuthEmail();
  }

  function submitAuth() {
    if (state.authPending) return Promise.resolve();
    var form = document.getElementById("auth-form");
    if (!form) return Promise.resolve();
    var emailInput = document.getElementById("auth-email");
    var passwordInput = document.getElementById("auth-password");
    var confirmInput = document.getElementById("auth-password-confirm");
    var email = emailInput ? emailInput.value.trim() : "";
    var password = passwordInput ? passwordInput.value : "";
    if (!email) {
      state.notice = COPY.auth.empty_email;
      state.noticeKind = "error";
      updateAuthControls();
      if (emailInput) emailInput.focus();
      return Promise.resolve();
    }
    if (state.authMode !== "forgot" && !password) {
      state.notice = COPY.auth.empty_password;
      state.noticeKind = "error";
      updateAuthControls();
      if (passwordInput) passwordInput.focus();
      return Promise.resolve();
    }
    if (state.authMode === "register" && password !== (confirmInput ? confirmInput.value : "")) {
      state.notice = COPY.auth.password_mismatch;
      state.noticeKind = "error";
      updateAuthControls();
      if (confirmInput) confirmInput.focus();
      return Promise.resolve();
    }
    if (!form.reportValidity()) return Promise.resolve();
    var authenticated = false;
    state.authPending = true;
    state.notice = "";
    updateAuthControls();
    var url =
      state.authMode === "register"
        ? "/api/auth/register"
        : state.authMode === "forgot"
          ? "/api/auth/forgot"
          : "/api/auth/login";
    var body =
      state.authMode === "forgot"
        ? { email: email }
        : { email: email, password: password };
    return requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (result) {
      if (!result.ok) {
        state.notice =
          (result.body && result.body.detail) || COPY.auth.failed;
        state.noticeKind = "error";
        updateAuthControls();
        return;
      }
      if (state.authMode === "forgot") {
        state.notice = COPY.auth.forgot_toast;
        state.noticeKind = "hint";
        state.authMode = "login";
        render();
        showToast(COPY.auth.forgot_toast);
        return;
      }
      state.activated = true;
      authenticated = true;
      if (passwordInput) passwordInput.value = "";
      if (confirmInput) confirmInput.value = "";
      state.notice = "";
      state.noticeKind = "hint";
      var toast =
        state.authMode === "register"
          ? COPY.auth.registered_toast
          : COPY.auth.logged_in_toast;
      return loadQuota()
        .then(loadCampusCourses)
        .then(function () { return pollRecordingTask(true); })
        .then(loadConnection)
        .then(function () {
          render();
          showToast(toast);
        });
    }).catch(function () {
      if (authenticated) { render(); showToast(COPY.auth.loading_failed); return; }
      state.notice = COPY.auth.network_failed;
      state.noticeKind = "error";
      updateAuthControls();
    }).finally(function () {
      state.authPending = false;
      updateAuthControls();
    });
  }

  function logoutAccount() {
    accountQuotaEpoch += 1;
    state.recordings.credentialsPending = false;
    state.recordings.credentialsError = "";
    state.campus.workspaceBusy = false;
    state.campus.courseCache = {};
    state.campus.course = null;
    state.recordings.syncPending = false;
    state.recordings.task = { state: "idle" };
    state.recordings.taskCourseId = "";
    state.recordings.error = "";
    state.recordings.errorCourseId = "";
    state.recordings.background = { state: "idle" };
    state.recordings.backgroundPending = false;
    settledQuotaRead = null;
    notionParentsEpoch += 1;
    notionParentsRead = null;
    return requestJson("/api/auth/logout", { method: "POST" }).then(function () {
      state.activated = false;
      state.quota = null;
      state.quotaStale = false;

      state.campus.home = null;
      state.campus.workspace = null;
      state.campus.homeSync = null;
      state.campus.parents = [];
      state.campus.course = null;
      state.campus.courseCache = {};
      state.campus.courses = [];
      state.authMode = "login";
      state.notice = "";
      render();
    });
  }

  function resendVerification() {
    var email = accountEmail();
    if (!email) return Promise.resolve();
    return requestJson("/api/auth/resend-verification", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email })
    }).then(function (result) {
      if (!result.ok) {
        showToast((result.body && result.body.detail) || COPY.auth.failed);
        return;
      }
      showToast(COPY.account.resend_toast);
    });
  }

  function redeem(form) {
    if (redeemPending) return Promise.resolve();
    form = form || document.getElementById("redeem-form");
    var input = form && form.querySelector('input[type="text"]');
    var code = input ? input.value.trim() : "";
    if (!code) {
      showToast(COPY.account.empty_code_notice);
      focusCode(form);
      return Promise.resolve();
    }
    if (!canRedeem()) {
      showToast(COPY.account.redeem_gated);
      return Promise.resolve();
    }
    var epoch = accountQuotaEpoch;
    var email = accountEmail();
    function currentAccount() {
      return epoch === accountQuotaEpoch && state.activated && accountEmail() === email;
    }
    function currentForm() {
      return form && document.getElementById(form.id);
    }
    redeemPending = true;
    var submit = form && form.querySelector('[data-action="redeem"]');
    if (submit) submit.disabled = true;
    return requestJson("/api/platform/redeem", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code })
    }).then(function (result) {
      if (!currentAccount()) return;
      if (!result.ok) {
        showToast((result.body && result.body.detail) || COPY.account.redeem_failed);
        focusCode(currentForm());
        return;
      }
      var currentInput = currentForm() && currentForm().querySelector('input[type="text"]');
      if (currentInput && currentInput.value.trim() === code) currentInput.value = "";
      return requestJson("/api/platform/quota").then(function (quota) {
        if (!currentAccount()) return;
        if (quota.ok && quota.body) {
          state.activated = Boolean(quota.body.active);
          state.quota = quota.body;
          render();
        }
        showToast(COPY.account.redeemed_toast);
      }, function () {
        if (currentAccount()) showToast(COPY.account.redeemed_toast);
      });
    }).catch(function () {
      if (!currentAccount()) return;
      showToast(COPY.account.redeem_failed);
      focusCode(currentForm());
    }).finally(function () {
      redeemPending = false;
      document.querySelectorAll('[data-redeem-form] [data-action="redeem"]').forEach(function (button) {
        button.disabled = false;
      });
    });
  }

  /* -- navigation -------------------------------------------------------- */

  function openDashboard() {

    state.view = "dashboard";

    render();
  }

  function recordingError(message, courseId) {
    state.recordings.error = campusReadError(message || "操作没有完成，请稍后重试。");
    state.recordings.errorCourseId = courseId === undefined ?
      (state.campus.courseId || "") : courseId;
    render();
  }

  function recordingCredentialsForm() {
    var copy = COPY.course.recordings;
    var pending = Boolean(state.recordings.credentialsPending);
    var readonly = pending ? " readonly" : "";
    return '<form id="recording-credentials-form" class="recording-credentials" aria-busy="' + pending + '">' +
      '<label for="recording-username">' + esc(copy.username) + '<input id="recording-username" autocomplete="username" placeholder="学号" required' + readonly + '></label>' +
      '<label for="recording-password">' + esc(copy.password) + '<input id="recording-password" type="password" autocomplete="current-password" placeholder="密码" required' + readonly + '></label>' +
      '<div class="credentials-actions">' + button(pending ? copy.saving : copy.save, "recording-save-credentials", {
        primary: true, small: true, type: "submit", disabled: pending, attrs: { "aria-busy": String(pending) }
      }) + (state.campus.configured ? button("取消", "recording-cancel-credentials", { small: true, quiet: true, disabled: pending }) : "") + '</div>' +
      '<p class="form-note hint">' + esc(copy.credentials_hint) + '</p>' +
      (state.recordings.credentialsError ? '<p role="alert" class="form-note">' + esc(state.recordings.credentialsError) + '</p>' : "") + '</form>';
  }

  function saveRecordingCredentials() {
    if (state.recordings.credentialsPending) return Promise.resolve();
    var user = document.getElementById("recording-username");
    var password = document.getElementById("recording-password");
    var form = document.getElementById("recording-credentials-form");
    if (!form || !form.reportValidity()) return Promise.resolve();
    if (!user.value.trim()) {
      state.recordings.credentialsError = "请输入教学网账号。";
      render();
      document.getElementById("recording-username").focus();
      return Promise.resolve();
    }
    var epoch = accountQuotaEpoch;
    var body = JSON.stringify({ username: user.value.trim(), password: password.value });
    state.recordings.credentialsPending = true;
    state.recordings.credentialsError = "";
    render();
    return requestJson("/api/recordings/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body || result.body.verified !== true) {
        state.recordings.credentialsError = (result.body && result.body.detail) || "教学网登录尚未验证，请稍后重试。";
        return;
      }
      state.recordings.configured = true;
      state.campus.configured = true;
      state.recordings.editingCredentials = false;
      state.recordings.error = "";
      password.value = "";
      showToast(COPY.course.recordings.verified);
    }).catch(function () {
      if (epoch === accountQuotaEpoch) state.recordings.credentialsError = "验证连接中断，请检查网络后重试。";
    }).finally(function () {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.credentialsPending = false;
      render();
    });
  }

  function syncRecordingIndex(courseId) {
    if (courseId ? courseTaskBusy(courseId) : state.recordings.backgroundPending ||
        state.recordings.background.state === "running" || state.recordings.syncPending || state.recordings.task.state === "running") return Promise.resolve();
    var epoch = accountQuotaEpoch;
    var taskCourseId = courseId ? (courseId) : "";
    var options = { method: "POST" };
    if (courseId) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ course_id: courseId });
    }
    state.recordings.syncPending = true;
    render();
    return requestJson("/api/recordings/sync", options).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.syncPending = false;
      if (!result.ok) return recordingError((result.body && result.body.detail) || "录像目录同步失败。", taskCourseId);
      if (result.body.scope === "background") state.recordings.background = result.body;
      else {
        state.recordings.task = result.body;
        state.recordings.taskCourseId = taskCourseId;
      }
      state.recordings.error = "";
      render();
    }).catch(function () {
      if (epoch !== accountQuotaEpoch) return;
      state.recordings.syncPending = false;
      recordingError("录像目录同步失败。", taskCourseId);
    });
  }

  function cancelRecordingSync() {
    return requestJson("/api/recordings/cancel-sync", { method: "POST" }).then(function (result) {
      if (!result.ok) return showToast((result.body && result.body.detail) || "无法取消目录更新。");
      state.recordings.task = result.body;
      render();
    }).catch(function () { showToast("无法取消目录更新。"); });
  }

  function recordingNoteStyle(recordingId, disabled) {
    var selected = state.recordings.noteStrategyByKey[recordingId] || "transcript";
    return '<label>笔记方式<select data-recording-note-strategy="' + esc(recordingId) + '" aria-label="笔记方式"' + (disabled ? ' disabled' : '') + '>' +
      '<option value="transcript"' + (selected === "transcript" ? ' selected' : '') + '>保留讲述顺序</option>' +
      '<option value="topics"' + (selected === "topics" ? ' selected' : '') + '>知识点图文笔记（试用）</option></select></label>';
  }

  function openNotionPreview(url, onFailure) {
    return requestJson("/api/open-notion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url })
    }).then(function (result) {
      if (!result.ok || !result.body || !result.body.opened) throw new Error("browser open failed");
      if (["library", "course", "lecture"].indexOf(state.view) >= 0) openDashboard();
      else openDashboard();
      showToast(COPY.launch.opened_external);
      return true;
    }).catch(function () {
      if (onFailure) onFailure();
      else showToast(COPY.launch.failed);
      return false;
    });
  }

  function disconnectNotion() {
    return requestJson("/api/connection/disconnect", { method: "POST" }).then(
      function (result) {
        if (result.ok && result.body) state.connection = result.body;

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
      return loadNotionParents().then(function () {
        render();
        showToast(COPY.connection.reconnect_toast);
      });
    }
    render();
    if (state.connection.error) showToast(state.connection.error);
    return Promise.resolve();
  }

  function pollConnection() {
    stopPolling();
    var attempts = 0;
    pollTimer = window.setInterval(function () {
      attempts += 1;
      requestJson("/api/connection").then(function (result) {
        if (result.ok && result.body) state.connection = result.body;
        if (state.connection.state !== "connecting") {
          stopPolling();
          finishConnect();
        } else if (attempts >= POLL_MAX_ATTEMPTS) {
          stopPolling();
          showToast(COPY.connection.timeout);
        }
      }).catch(function () {
        if (attempts >= POLL_MAX_ATTEMPTS) {
          stopPolling();
          showToast(COPY.connection.failed);
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
        if (!result.ok) {
          showToast((result.body && result.body.detail) || COPY.connection.failed);
          return;
        }
        if (result.body) state.connection = result.body;
        if (state.connection.connected || state.connection.state !== "connecting") return finishConnect();
        render();
        pollConnection();
      }
    ).catch(function () {
      showToast(COPY.connection.failed);
    });
  }

  function downloadAssignmentDraft() {
    var submission = state.campus.submission;
    var draft = submission.draft;
    var courseId = state.campus.courseId;
    var contentId = state.campus.submission.selectedId;
    if (!draft || state.campus.submission.downloadBusy) return;
    state.campus.submission.downloadBusy = true;
    var query = '?sha256=' + encodeURIComponent(draft.sha256) + '&course_id=' + encodeURIComponent(courseId) +
      '&content_id=' + encodeURIComponent(contentId);
    return requestJson('/api/campus/submission-drafts/' + encodeURIComponent(draft.draft_id) + '/check' + query).then(function (result) {
      if (state.campus.submission !== submission || state.campus.courseId !== courseId ||
          submission.selectedId !== contentId || submission.draft !== draft) return;
      if (!result.ok || !result.body || result.body.valid !== true || result.body.draft_id !== draft.draft_id ||
          result.body.sha256 !== draft.sha256) {
        state.campus.submission.error = (result.body && result.body.detail) || '草稿未通过当前预览核验，请重新准备。';
        render(); return;
      }
      if (window.__TAURI_INTERNALS__ && typeof window.__TAURI_INTERNALS__.invoke === 'function') {
        submission.saveStatus = { state: 'choosing', message: '请选择文件保存位置…' };
        render();
        return window.__TAURI_INTERNALS__.invoke('save_reviewed_draft', {
          draftId: draft.draft_id, sha256: draft.sha256, courseId: courseId,
          contentId: contentId, archiveName: draft.archive_name
        }).then(function (saved) {
          if (state.campus.submission !== submission || submission.draft !== draft) return;
          if (saved && saved.state === 'cancelled') {
            submission.saveStatus = { state: 'cancelled', message: '已取消保存，草稿仍可查看。' };
          } else if (saved && saved.state === 'saved' && saved.sha256 === draft.sha256 && saved.filename) {
            submission.saveStatus = { state: 'saved', sha256: saved.sha256, message: '文件已保存：' + saved.filename };
          } else {
            submission.saveStatus = null;
            submission.error = '保存结果未通过核验，请检查文件后重试。';
          }
          render();
        }).catch(function (error) {
          if (state.campus.submission !== submission || submission.draft !== draft) return;
          submission.error = typeof error === 'string' ? error : '文件未能保存，请检查磁盘空间或目录权限后重试。';
          submission.saveStatus = null;
          render();
        });
      }
      var link = document.createElement('a');
      link.href = draft.download_url + '?sha256=' + encodeURIComponent(draft.sha256);
      link.download = draft.archive_name;
      document.body.appendChild(link); link.click(); link.remove();
    }).catch(function () {
      if (state.campus.submission !== submission) return;
      submission.error = '草稿暂时无法核验，请稍后重试或重新准备。'; render();
    }).finally(function () { submission.downloadBusy = false; });
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target || target.disabled) return;
    var action = target.dataset.action;
    var mobileMenu = target.closest('.mobile-account-menu');
    if (mobileMenu && ["open-dashboard", "campus-account-settings", "open-welcome"].indexOf(action) >= 0) {
      mobileMenu.open = false;
    }
    if (action === "open-welcome") {
      welcomeOpen = true; render(); document.getElementById('welcome-heading').focus(); return;
    }
    if (action === "welcome-start" || action === "welcome-skip") {
      closeWelcome(action === "welcome-start"); return;
    }
    if (action === 'campus-download-draft') { event.preventDefault(); downloadAssignmentDraft(); return; }
    if (action === "auth-submit" || action === "recording-save-credentials") return;
    else if (action === "auth-mode-login") setAuthMode("login");
    else if (action === "auth-mode-register") setAuthMode("register");
    else if (action === "auth-mode-forgot") setAuthMode("forgot");
    else if (action === "logout") logoutAccount();
    else if (action === "resend-verification") resendVerification();
    else if (action === "refresh-verification") refreshAccount();
    else if (action === "redeem") redeem(target.closest("[data-redeem-form]"));
    else if (action === "request-update") requestUpdate(target.dataset.version);
    else if (action === "revoke-notion") disconnectNotion();
    else if (action === "connect-notion") connectNotion();
    else if (action === "open-dashboard") openDashboard();
    else if (action === "campus-account-settings") {
      state.view = "campus-account";
      render();
      var settingsPanel = document.querySelector(state.campus.configured ? ".home-setup" : "#recording-credentials-form");
      if (settingsPanel) settingsPanel.scrollIntoView({ block: "start" });
      loadNotionParents();
    }
    else if (action === "open-campus-course") openCampusCourse(target.dataset.course);
    else if (action === "campus-course-home") { state.campus.section = "home"; render(); }
    else if (action === "campus-course-settings") { state.campus.section = "settings"; render(); loadNotionParents(); }
    else if (action === "campus-open-center") {
      event.preventDefault();
      if (["assignments", "notices", "materials"].indexOf(target.dataset.center) < 0) return;
      state.campus.section = target.dataset.center; render();
    }
    else if (action === "campus-open-lesson") { state.campus.section = "lecture"; state.campus.lessonId = target.dataset.recording; render(); }
    else if (action === "campus-load-parents") loadNotionParents();
    else if (action === "campus-create-workspace") createWorkspace();
    else if (action === 'campus-replace-workspace') {
      var selected = state.campus.parents.find(function (page) {return page.id === state.campus.parentId;});
      var epoch = accountQuotaEpoch;
      confirmAction('将在「' + (selected ? selected.title : '所选页面') + '」下面新建并使用学习主页，原 Notion 页面保留。是否继续？').then(function (accepted) {
        if (accepted && epoch === accountQuotaEpoch) createWorkspace(true);
      });
    }
    else if (action === "campus-move-home") moveCampusHome();
    else if (action === "campus-open-home" && target.dataset.url) openNotionPreview(target.dataset.url);
    else if (action === "campus-save-existing-notion") saveExistingNotionLink();
    else if (action === "campus-prepare-assignment") selectCampusAssignment(target.dataset.assignment);
    else if (action === "campus-build-draft") prepareCampusAssignment();
    else if (action === "campus-analyze-requirements") analyzeCampusAssignmentRequirements();
    else if (action === "campus-submit-assignment") submitCampusAssignment();
    else if (action === "campus-assignment-status") loadAssignmentStatus(target.dataset.assignment);
    else if (action === "campus-load-receipts") loadCampusReceipts(state.campus.courseId);
    else if (action === "campus-recover-submission") recoverCampusSubmission(target.dataset.draft);
    else if (action === "campus-process") processCampusRecording(target.dataset.recording);
    else if (action === "campus-regenerate") processCampusRecording(target.dataset.recording, true);
    else if (action === "campus-course-summary") startCourseSummary(target.dataset.course);
    else if (action === "campus-summaries") startCourseSummary("");
    else if (action === "campus-open-summary" && target.dataset.url) openNotionPreview(target.dataset.url);
    else if (action === "campus-review-term") openTermReview(target.dataset.recording);
    else if (action === "campus-close-term-review") {
      state.termReview = { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
      render();
    }
    else if (action === "campus-submit-term-review") submitTermReview();
    else if (action === "campus-estimate-duration") estimateCampusDuration(target.dataset.recording);
    else if (action === "campus-material-assign") saveCampusMaterialMatch(target.dataset.material, "assign");
    else if (action === "campus-material-ignore") saveCampusMaterialMatch(target.dataset.material, "ignore");
    else if (action === "campus-material-restore") saveCampusMaterialMatch(target.dataset.material, "restore");
    else if (action === "campus-review-handbook") reviewCampusHandbook(target);
    else if (action === "campus-open-lecture" && target.dataset.url) openNotionPreview(target.dataset.url);
    else if (action === "recording-edit-credentials") { state.recordings.editingCredentials = true; render(); }
    else if (action === "recording-cancel-credentials") { if (state.recordings.credentialsPending) return; state.recordings.editingCredentials = false; state.recordings.credentialsError = ""; render(); }
    else if (action === "recording-sync") syncRecordingIndex();
    else if (action === "recording-sync-course") {
      if (target.dataset.course) syncRecordingIndex(target.dataset.course);
      else recordingError("请先选择这门课程对应的教学网课程。");
    }
    else if (action === "recording-cancel-sync") cancelRecordingSync();
    else if (action === "recording-cancel-background") updateBackgroundSync(false);
    else if (action === "recording-resume-background") updateBackgroundSync(true);
    else if (action === "recording-download") downloadRecording(target.dataset.recording);
    else if (action === "recording-open-result" && target.dataset.url) openNotionPreview(target.dataset.url);
  });

  document.addEventListener("input", function (event) {
    if (event.target.closest("#assignment-prepare-form") &&
        ["notion_url", "archive_name", "code_filename"].includes(event.target.name)) {
      state.campus.submission.form[event.target.name] = event.target.value;
      return;
    }
    if (event.target.matches("[data-campus-search], [data-campus-date]")) {
      var selector = event.target.matches("[data-campus-date]") ? "[data-campus-date]" : "[data-campus-search]";
      if (selector === "[data-campus-date]") state.campus.dateQuery = event.target.value;
      else state.campus.query = event.target.value;
      var start = event.target.selectionStart;
      render();
      var input = document.querySelector(selector);
      if (input) { input.focus(); input.setSelectionRange(start, start); }
    }
  });
  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-term-review-confirm]")) {
      state.termReview.confirmed = event.target.checked;
      var submit = document.querySelector('[data-action="campus-submit-term-review"]');
      if (submit) submit.disabled = !event.target.checked;
      return;
    }
    if (event.target.matches('#assignment-prepare-form [name="submission_mode"]')) {
      state.campus.submission.form = { submission_mode: event.target.value, notion_url: "", archive_name: "", code_filename: "", attachments: [] };
      state.campus.submission.error = "";
      render();
      return;
    }
    if (event.target.matches('#assignment-prepare-form [name="attachments"]')) {
      state.campus.submission.form.attachments = Array.from(event.target.files || []);
      var summary = document.querySelector("[data-assignment-attachments]");
      if (summary) summary.textContent = state.campus.submission.form.attachments.length ?
        "已选择：" + state.campus.submission.form.attachments.map(function (file) { return file.name; }).join("、") :
        "尚未选择文件";
      return;
    }
    if (event.target.matches("[data-campus-category]")) { state.campus.category = event.target.value; render(); return; }
    if (event.target.matches("[data-campus-parent]")) { state.campus.parentId = event.target.value; render(); return; }
    if (event.target.matches("[data-recording-direct-oss]")) { state.recordings.directOss = event.target.checked; return; }
    if (event.target.matches("[data-recording-note-strategy]")) {
      state.recordings.noteStrategyByKey[event.target.dataset.recordingNoteStrategy] = event.target.value;
      return;
    }
  });

  document.addEventListener("loadedmetadata", function (event) {
    if (!event.target.matches || !event.target.matches("[data-term-review-video]")) return;
    var seek = Number(event.target.dataset.seek);
    if (Number.isFinite(seek)) event.target.currentTime = Math.min(seek, event.target.duration || seek);
  }, true);
  document.addEventListener("submit", function (event) {
    event.preventDefault();
    if (event.target.id === "auth-form") submitAuth();
    else if (event.target.id === "recording-credentials-form") saveRecordingCredentials();
    else if (event.target.matches("[data-redeem-form]")) redeem(event.target);
  });

  window.setInterval(refreshAccount, 8000);
  window.setInterval(pollRecordingTask, 1500);
  window.addEventListener("focus", refreshAccount);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshAccount();
  });

  render();
  loadUpdate().then(render).catch(function () {
    // A release-check failure never blocks or replaces the local panel.
  });
  loadQuota()
    .then(loadCampusCourses)
    .then(function () { return pollRecordingTask(true); })
    .then(loadConnection)
    .then(refreshAccount)
    .then(function () { return loadNotionParents({ selectedOnly: true }); })
    .then(render)
    .catch(function () {
      // an unreachable loopback API must not leave a blank surface
      render();
    });
})();
