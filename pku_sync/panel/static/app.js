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
    batchOrganize: null,
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
    campus: { courses: [], course: null, courseCache: {}, courseId: "", loading: false, error: "", configured: false, home: null, parents: [], parentId: "", catalogAutoStarted: false, parentError: "", query: "", category: "all", dateQuery: "", materialQuery: "", demoConnection: false, submission: { selectedId: "", draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } } },
    recordings: { configured: false, editingCredentials: false, loading: false, syncPending: false, backgroundPending: false, background: { state: "idle" }, batch: { state: "idle" }, error: "", task: { state: "idle" }, taskCourseId: "", noteStrategyByKey: {}, directOss: false },
    localRecordings: { open: false, loading: false, saveLocally: false, downloadDir: "", effectiveDir: "", count: 0, totalBytes: 0, items: [] },
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
    readingPreviews = {};
    mcpHub = {status:null, host:'codex', error:''};
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
        openCampusSetupIfNeeded();
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

  function openCampusSetupIfNeeded() {
    if (state.activated && accountVerified() && !state.campus.configured) {
      state.view = "campus-account";
    }
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
        state.recordings.background.state !== "running" && state.recordings.batch.state !== "running")) return;
    var epoch = accountQuotaEpoch;
    if (recordingTaskRead && recordingTaskRead.epoch === epoch) return recordingTaskRead.promise;
    var read = { epoch: epoch, promise: null,
      foreground: state.recordings.task, background: state.recordings.background, batch: state.recordings.batch };
    recordingTaskRead = read;
    read.promise = requestJson("/api/recordings/task").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      // A start/cancel response owns newer state than this earlier observation.
      // Applying an old idle snapshot can otherwise hide a newly running task
      // and stop its scheduled polling altogether.
      if (state.recordings.task !== read.foreground || state.recordings.background !== read.background || state.recordings.batch !== read.batch) return;
      if (!result.ok || !result.body) return;
      var previous = state.recordings.task;
      var previousBackground = state.recordings.background;
      var previousBatch = state.recordings.batch;
      var foreground = Object.assign({}, result.body);
      delete foreground.background_sync;
      delete foreground.batch_organize;
      state.recordings.task = foreground;
      if (foreground.course_id) state.recordings.taskCourseId = foreground.course_id;
      state.recordings.background = result.body.background_sync || { state: "idle" };
      state.recordings.batch = result.body.batch_organize || { state: "idle" };
      var batchChanged = JSON.stringify(previousBatch) !== JSON.stringify(state.recordings.batch);
      var backgroundChanged = JSON.stringify(previousBackground) !== JSON.stringify(state.recordings.background);
      var completedCourse = backgroundChanged && (state.recordings.background.course_results || []).some(function (row) {
        var old = (previousBackground.course_results || []).find(function (entry) { return entry.course_id === row.course_id; });
        return row.state === "done" && (!old || old.state !== "done");
      });
      if (previous.state === 'running' && foreground.kind === 'sync' && foreground.state === 'done' ||
          previousBackground.state === 'running' && state.recordings.background.state === 'done') showToast('课程目录已更新');
      // Terminal feedback must be visible before ancillary cache/quota reads.
      if (batchChanged || backgroundChanged || previous.state !== foreground.state) render();
      if (previous.state === "running" && foreground.state !== "running" || completedCourse ||
          previousBackground.state === "running" && state.recordings.background.state !== "running" ||
          previousBatch.state === "running" && state.recordings.batch.state !== "running") {
        if (previousBatch.state === "running" && state.recordings.batch.state !== "running") state.batchOrganize = null;
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


  function applyLocalRecordingsPayload(data) {
    if (!data) return;
    state.localRecordings.saveLocally = Boolean(data.save_recordings_locally);
    state.localRecordings.downloadDir = data.recordings_download_dir || "";
    state.localRecordings.effectiveDir = data.effective_download_dir || "";
    state.localRecordings.count = Number(data.count) || 0;
    state.localRecordings.totalBytes = Number(data.total_bytes) || 0;
    state.localRecordings.items = Array.isArray(data.items) ? data.items : [];
  }

  function loadLocalRecordings() {
    var epoch = accountQuotaEpoch;
    state.localRecordings.loading = true;
    render();
    return requestJson("/api/recordings/local-storage").then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (result.ok && result.body) applyLocalRecordingsPayload(result.body);
    }).catch(function () {}).finally(function () {
      if (epoch === accountQuotaEpoch) {
        state.localRecordings.loading = false;
        render();
      }
    });
  }

  function updateLocalStorageSettings(payload, toastMessage) {
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "本地存储设置未保存，请检查路径权限。");
        render();
        return;
      }
      applyLocalRecordingsPayload(result.body);
      render();
      if (toastMessage) showToast(toastMessage);
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("本地存储设置保存失败，请稍后重试。");
    });
  }

  function pickLocalStorageDirectory() {
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage/pick-directory", { method: "POST" }).then(function (result) {
      if (epoch !== accountQuotaEpoch || !result.ok || !result.body) return;
      if (!result.body.cancelled && result.body.path) {
        return updateLocalStorageSettings({ recordings_download_dir: result.body.path }, "已更新录像下载目录。");
      }
    }).catch(function () {});
  }

  function openLocalStoragePath(recordingId) {
    return requestJson("/api/recordings/local-storage/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_id: recordingId || "" })
    }).then(function (result) {
      if (!result.ok) showToast((result.body && result.body.detail) || "无法打开本地目录。");
    }).catch(function () {
      showToast("无法打开本地目录。");
    });
  }

  function deleteOneLocalRecording(recordingId) {
    if (!recordingId) return Promise.resolve();
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage/" + encodeURIComponent(recordingId), {
      method: "DELETE"
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "未能删除本地录像文件。");
        return;
      }
      applyLocalRecordingsPayload(result.body);
      showToast("已删除本地录像文件。");
      if (state.view === "campus-course" && state.campus.courseId) {
        return loadCampusCourse(state.campus.courseId);
      }
      render();
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("未能删除本地录像文件。");
    });
  }

  async function clearAllLocalRecordings() {
    if (!await confirmAction("确认清理本机保存的全部原始录像文件？\n（已生成的课堂笔记、转写与课件不受影响）")) return;
    var epoch = accountQuotaEpoch;
    return requestJson("/api/recordings/local-storage/clear", { method: "POST" }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) {
        showToast((result.body && result.body.detail) || "清理本地录像失败。");
        return;
      }
      applyLocalRecordingsPayload(result.body);
      showToast("已清理全部本地原始录像。");
      if (state.view === "campus-course" && state.campus.courseId) {
        return loadCampusCourse(state.campus.courseId);
      }
      render();
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast("清理本地录像失败。");
    });
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
      if (result.body.local_recordings) {
        applyLocalRecordingsPayload(result.body.local_recordings);
      }
      state.campus.configured = Boolean(result.body.campus_configured);
      state.campus.demoConnection = Boolean(result.body.demo_connection);
      render();
    }).catch(function () {});
  }

  function courseTaskBusy(courseId) {
    var background = state.recordings.background || {};
    var batch = state.recordings.batch || {};
    return state.recordings.syncPending || state.recordings.task.state === "running" ||
      batch.state === "running" && (batch.items || []).some(function (item) {
        return item.course_id === courseId && (item.state === "pending" || item.state === "running");
      }) ||
      background.state === "running" && background.current_course_id === courseId;
  }

  function campusReadError(message) {
    var value = String(message || "");
    if (/课程准备读取已过期|课程快照或准备顺序已变化|课序不一致/.test(value))
      return "课程信息需要重新核对，同步暂未完成。登录状态不受影响，可返回「我的课程」更新目录后重试。";
    if (/account_unconfirmed|revision|快照.*未确认|快照.*已更新/i.test(value))
      return "账号目录版本核对未完成或已变化；已有内容保留，请更新这门课后重试。";
    // Legacy publishing tasks can still finish in the backend. Keep their
    // diagnostics readable without restoring service branding to the UI.
    return value.replace(/Notion/gi, '笔记同步服务') || "教学网连接中断；保留已有内容，请检查网络后重试。";
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
      ["我的账号", state.activated && accountVerified()],
      ["教学网账号", state.campus.configured],
      ["课程目录", catalogReady]
    ];
    var next = steps.find(function (step) { return !step[1]; });
    var nextAction = state.campus.demoConnection ? '' : !accountVerified() ? button("我已验证邮箱，刷新状态", "refresh-verification", { small: true }) :
      !state.campus.configured ? (state.view === 'campus-account' ? '' : button("设置教学网账号", "campus-account-settings", { small: true, primary: true })) :
      !catalogReady ? (includeSyncAction === false ? '' : button(syncBusy ? "正在更新课程目录…" : "更新课程目录", "recording-sync", { small: true, primary: true, disabled: syncBusy })) :
      '';
    if (!next && state.view === 'campus-account') {
      nextAction = button('进入我的课程', 'open-dashboard', { small: true, primary: true });
    }
    return '<section class="panel campus-setup"><h2>开始使用</h2>' +
      (!(state.view === 'campus-account' && (!state.campus.configured || state.recordings.editingCredentials)) ? pendingInviteRewardHint() : '') + '<ul class="setup-steps">' +
      steps.map(function (step) { return '<li><span class="setup-mark" aria-hidden="true">' + (step[1] ? '✓' : '○') + '</span><span>' + esc(step[0]) + '</span></li>'; }).join("") +
      '</ul><p>' + (state.campus.demoConnection ? '演示模式：此处不会创建真实页面。' :
        next ? '下一步：' + esc(next[0]) : '已就绪，可以从课程中选择录像按需整理。') + '</p>' + nextAction + '</section>';
  }

  function formatCourseTitleMarkup(title) {
    var match = String(title || '').match(/^(.*?)(\((?:\d{2}-\d{2}学年.*|.*学期.*)\))$/);
    return match ? esc(match[1]) + '<span class="course-title-term">' + esc(match[2]) + '</span>' : esc(title);
  }

  function formatLessonTitleMarkup(title, date) {
    title = String(title || '');
    return date && title.startsWith(date) && title.slice(date.length).trim()
      ? '<span class="lesson-title-date">' + esc(date) + '</span><span>' + esc(title.slice(date.length)) + '</span>'
      : esc(title);
  }

  function batchLessonKey(courseId, lessonId) { return JSON.stringify([courseId, lessonId]); }

  function batchCourses() {
    var panel = state.batchOrganize;
    return panel && panel.preview ? panel.preview.courses.filter(function (course) {
      return !panel.courseId || panel.courseId === course.course_id;
    }) : [];
  }

  function selectBatchLessons(mode) {
    var panel = state.batchOrganize;
    panel.selected = {};
    batchCourses().forEach(function (course) {
      course.lessons.forEach(function (lesson) {
        if (lesson.eligible && (mode === 'all' || mode === 'pending' && lesson.needs_organize))
          panel.selected[batchLessonKey(course.course_id, lesson.id)] = true;
      });
    });
  }

  function openBatchOrganize(courseId) {
    var epoch = accountQuotaEpoch;
    var panel = { open: true, loading: true, courseId: courseId || '', selected: {}, notesStrategy: isLiteTier() ? 'transcript' : 'topics', error: '' };
    state.batchOrganize = panel;
    render();
    refreshSettledQuota();
    return requestJson('/api/campus/batch-organize/preview').then(function (result) {
      if (epoch !== accountQuotaEpoch || state.batchOrganize !== panel) return;
      if (!result.ok || !result.body) throw new Error(result.body && result.body.detail || '暂时无法读取录像，请重试。');
      panel.preview = result.body;
      selectBatchLessons('pending');
    }).catch(function (error) {
      if (epoch === accountQuotaEpoch && state.batchOrganize === panel) panel.error = error.message;
    }).finally(function () {
      if (epoch === accountQuotaEpoch && state.batchOrganize === panel) { panel.loading = false; render(); }
    });
  }

  function selectedBatchItems() {
    var selected = state.batchOrganize && state.batchOrganize.selected || {};
    var items = [];
    batchCourses().forEach(function (course) {
      course.lessons.forEach(function (lesson) {
        if (lesson.eligible && selected[batchLessonKey(course.course_id, lesson.id)])
          items.push({ course: course, lesson: lesson });
      });
    });
    return items;
  }

  function batchOrganizePanel() {
    var panel = state.batchOrganize;
    if (!panel || !panel.open) return '';
    var header = '<section class="batch-organize-panel" aria-label="批量整理选择"><h2>一键并行整理课堂笔记</h2>' +
      '<p class="form-note">支持多门课程同时并行处理；开始前自动预估转写时长与 AI 点数。</p>';
    if (panel.loading) return header + '<p role="status">正在读取录像与成本预估…</p></section>';
    if (!panel.preview) return header + '<p role="alert">' + esc(panel.error) + '</p>' +
      button('重新读取', 'batch-preview-retry', {small:true}) + '</section>';
    var bundledQuota = state.quota || {};
    var bundledNotes = Boolean(bundledQuota.tier) || Number(bundledQuota.tier_expires_at) * 1000 > Date.now() || Number(bundledQuota.weekly_rollover_seconds) > 0;
    var useFlashForTranscript = panel.notesStrategy === 'transcript' && panel.preview.notes_model === 'qwen3.7-plus';
    var activeModel = useFlashForTranscript ? 'qwen3.5-flash' : panel.preview.notes_model;
    var items = selectedBatchItems();
    var coursesCount = new Set(items.map(function (item) { return item.course.course_id; })).size;
    var minutes = 0, minPoints = 0, maxPoints = 0, reuse = 0, estimated = false;
    items.forEach(function (item) {
      var lesson = item.lesson;
      var lessonMinPoints = bundledNotes ? 0 : useFlashForTranscript ? 1 : lesson.estimated_points_min;
      var lessonMaxPoints = bundledNotes ? 0 : useFlashForTranscript ? 2 : lesson.estimated_points_max;
      minutes += lesson.estimated_transcribe_minutes;
      minPoints += lessonMinPoints; maxPoints += lessonMaxPoints;
      if (lesson.transcript_available) reuse += 1;
      if (!lesson.transcript_available && lesson.duration_is_estimated) estimated = true;
    });
    var quota = state.quota || {};
    var remainingMinutes = typeof quota.transcribe_seconds_remaining === 'number' ? Math.floor(quota.transcribe_seconds_remaining / 60) : null;
    var remainingPoints = typeof quota.llm_points_remaining === 'number' ? quota.llm_points_remaining : null;
    var warning = (remainingMinutes !== null && minutes > quota.transcribe_seconds_remaining / 60) ||
      (remainingPoints !== null && minPoints > remainingPoints);
    var summary = '<div class="batch-cost-summary" data-batch-cost-estimate aria-live="polite">' +
      '<div><small>所选范围</small><strong>' + coursesCount + ' 门课程 · ' + items.length + ' 节录像</strong><span>' +
      (coursesCount > 1 ? '最多 ' + Math.min(3, coursesCount) + ' 门课并行' : '单课顺序处理') + '</span></div>' +
      '<div><small>预计转写时长</small><strong>约 ' + minutes + ' 分钟</strong><span>' +
      (reuse ? '含 ' + reuse + ' 讲复用已有转写；' : '') + (estimated ? '未读时长讲次按标准课时估算' : '按录像实际时长计算') +
      '</span><span>当前剩余 ' + (remainingMinutes === null ? '暂不可用' : remainingMinutes + ' 分钟') + '</span></div>' +
      '<div><small>预计 AI 点数</small><strong>约 ' + minPoints + '–' + maxPoints + ' 点</strong><span>' + esc(activeModel) +
      '</span><span>当前剩余 ' + (remainingPoints === null ? '暂不可用' : remainingPoints + ' 点') + '</span></div></div>' +
      '<p class="form-note">以上为估算，实际消耗以处理结果为准。已有笔记会优先复用。</p>' +
      (warning ? '<p class="form-note hint" role="status">提示：预估消耗高于当前剩余额度，整理会在额度用尽处安全停止，已完成的转写与笔记均会保留。</p>' : '') +
      (state.quotaStale ? '<p class="form-note hint">剩余额度正在刷新，请以最新余额为准。</p>' : '');
    var courses = batchCourses().map(function (course) {
      var eligible = course.lessons.filter(function (lesson) { return lesson.eligible; });
      var count = eligible.filter(function (lesson) { return panel.selected[batchLessonKey(course.course_id, lesson.id)]; }).length;
      var attrs = ' data-course="' + esc(course.course_id) + '"';
      return '<article class="batch-course"><label class="batch-course-heading"><input type="checkbox" data-action="batch-toggle-course"' + attrs +
        (count && count < eligible.length ? ' data-batch-indeterminate' : '') +
        (count && count === eligible.length ? ' checked' : '') + (!eligible.length ? ' disabled' : '') +
        '><span><strong>' + esc(course.title) + '</strong><small>' + esc(course.term) + ' · 已选 ' + count + ' / ' + course.lessons.length +
        ' 讲（待整理 ' + course.lessons.filter(function (lesson) { return lesson.needs_organize; }).length + ' 讲）</small></span></label>' +
        (course.read_only ? '<p class="form-note">' + esc(course.read_only_message) + '</p>' : '') +
        '<details class="batch-course-lessons" data-persistent-disclosure="batch-course-' + esc(course.course_id) + '"><summary>展开选择具体讲次</summary>' +
        course.lessons.map(function (lesson) {
          var lessonMinPoints = bundledNotes ? 0 : useFlashForTranscript ? 1 : lesson.estimated_points_min;
          var lessonMaxPoints = bundledNotes ? 0 : useFlashForTranscript ? 2 : lesson.estimated_points_max;
          return '<label class="batch-lesson"><input type="checkbox" data-action="batch-toggle-lesson"' + attrs + ' data-recording="' + esc(lesson.id) + '"' +
            (panel.selected[batchLessonKey(course.course_id, lesson.id)] ? ' checked' : '') + (!lesson.eligible ? ' disabled' : '') +
            '><span><strong>' + esc(lesson.date) + ' ' + esc(lesson.title) + '</strong><small>' +
            esc(lesson.unavailable_reason || (course.read_only ? '课程只读' : lesson.notes_available ? '已生成笔记' : lesson.transcript_available ? '已有转写' : '待整理')) +
            ' · ' + (lesson.transcript_available ? '转写 0 分钟' : '约 ' + lesson.estimated_transcribe_minutes + ' 分钟') +
            ' · ' + esc(activeModel) + ' · AI 约 ' + lessonMinPoints + '–' + lessonMaxPoints + ' 点</small></span></label>';
        }).join('') + '</details></article>';
    }).join('');
    var running = state.recordings.batch.state === 'running' || panel.starting;
    return header + '<div class="batch-selection-actions">' + button('全选待整理', 'batch-select-pending', {small:true, quiet:true}) +
      button('全选全部讲次', 'batch-select-all', {small:true, quiet:true}) + button('清空选择', 'batch-select-none', {small:true, quiet:true}) +
      '<label>笔记样式 <select data-batch-note-style><option value="topics"' + (panel.notesStrategy === 'topics' ? ' selected' : '') + (isLiteTier() ? ' disabled' : '') +
      '>知识点图文笔记</option><option value="transcript"' + (panel.notesStrategy === 'transcript' ? ' selected' : '') + '>保留讲述顺序</option></select></label></div>' +
      summary + '<div class="batch-course-list">' + (courses || '<p>当前没有可整理的录像。</p>') + '</div>' +
      (panel.error ? '<p role="alert">' + esc(panel.error) + '</p>' : '') +
      button(panel.starting ? '正在启动…' : '开始并行整理（' + items.length + ' 讲）', 'batch-organize-start', {primary:true,
        disabled: !items.length || running || state.campus.demoConnection}) + '</section>';
  }

  function startBatchOrganize() {
    var panel = state.batchOrganize;
    if (!panel || panel.starting || state.recordings.batch.state === 'running') return;
    var items = selectedBatchItems().map(function (item) { return {course_id:item.course.course_id, recording_id:item.lesson.id}; });
    if (!items.length) return;
    var epoch = accountQuotaEpoch;
    panel.starting = true; panel.error = ''; render();
    return requestJson('/api/campus/batch-organize', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items:items, notes_strategy:panel.notesStrategy, direct_oss:true, max_parallel_courses:3})
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) throw new Error(result.body && result.body.detail || '批量整理未能开始，请重试。');
      state.recordings.batch = result.body;
      panel.open = false;
    }).catch(function (error) {
      if (epoch === accountQuotaEpoch) panel.error = error.message;
    }).finally(function () {
      if (epoch === accountQuotaEpoch) { panel.starting = false; render(); }
    });
  }

  function cancelBatchOrganize() {
    var epoch = accountQuotaEpoch;
    return requestJson('/api/campus/batch-organize/cancel', {method:'POST'}).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) return showToast(result.body && result.body.detail || '取消请求未能送达，请重试。');
      state.recordings.batch = result.body; render();
    }).catch(function () { if (epoch === accountQuotaEpoch) showToast('取消请求未能送达，请重试。'); });
  }

  function batchOrganizeStatus() {
    var batch = state.recordings.batch;
    if (!batch || batch.state === 'idle') return '';
    var done = (batch.items || []).filter(function (item) { return item.state === 'done'; }).length;
    return '<section class="batch-organize-banner" data-batch-organize data-batch-state="' + esc(batch.state) + '" aria-label="批量整理进度">' +
      '<div class="batch-status-heading"><strong>' + (batch.state === 'running' ? '正在并行整理' : batch.state === 'done' ? '✓ 批量整理完成' : '批量整理') +
      '</strong>' + (batch.state === 'running' ? button('取消批量整理', 'batch-organize-cancel', {small:true, quiet:true}) : '') +
      '</div><p role="status">' + esc(batch.stage) + ' · 已完成 ' + done + ' / ' + batch.progress_total + ' 讲 · ' + (batch.active_courses || 0) +
      ' 门课正在处理</p><progress aria-label="批量整理进度" value="' + (batch.progress_done || 0) + '" max="' + (batch.progress_total || 1) + '"></progress>' +
      '<details data-persistent-disclosure="batch-progress"><summary>查看各讲进度</summary><ul class="batch-progress-items">' +
      (batch.items || []).map(function (item) {
        // Running rows carry the lecture-wide stage label; keep a stage that
        // already reads as an activity untouched instead of double-prefixing.
        var runningStage = String(item.stage || "").indexOf('正在') === 0
          ? item.stage : '正在' + (item.stage || '转写并生成笔记');
        return '<li><div><strong>' + esc(item.course_title) + ' · ' + esc(item.title) + '</strong><small>' +
          esc(item.state === 'done' ? '✓ 已完成' : item.state === 'pending' ? '等待中' : runningStage) +
          (item.state === 'running' ? ' · ' + Math.round(item.progress_percent || 0) + '%' : '') + '</small>' +
          (item.error ? '<small class="batch-item-error">' + esc(item.error) + '</small>' : '') + '</div>' +
          (item.state === 'done' ? button('查看笔记', 'batch-open-note', {small:true, quiet:true, attrs:{'data-course':item.course_id, 'data-recording':item.recording_id}}) : '') + '</li>';
      }).join('') + '</ul></details></section>';
  }

  function lastCampusSync() {
    var times = state.campus.courses.map(function (course) { return course.synced_at; }).filter(function (value) { return parseTime(value); });
    times.sort(function (a, b) { return parseTime(b) - parseTime(a); });
    return times.length ? (Date.now() - parseTime(times[0]).getTime() < 60000 ? '刚刚' : friendlyTime(times[0])) : '尚未更新';
  }

  function localRecordingsNoticeBar() {
    var lr = state.localRecordings;
    if (!lr || lr.count <= 0 || lr.open) return "";
    return '<div class="local-recordings-banner" data-local-recordings-banner role="status">' +
      '<div><strong>本机已保存 ' + esc(lr.count) + ' 节课程录像</strong><span> · 共占用 ' + esc(formatTransfer(lr.totalBytes)) +
      (lr.effectiveDir ? ' · 目录：' + esc(lr.effectiveDir) : '') + '</span></div>' +
      '<div class="local-recordings-banner-actions">' +
      button("查看已下载录像", "toggle-local-recordings", { small: true }) +
      button("打开下载目录", "local-storage-open-dir", { small: true, quiet: true }) +
      button("全部清理", "local-storage-clear-all", { small: true, quiet: true }) +
      '</div></div>';
  }

  function localRecordingsPanel(alwaysVisible) {
    var lr = state.localRecordings;
    if (!alwaysVisible && (!lr || !lr.open)) return "";
    var rows = (lr.items || []).map(function (item) {
      return '<li class="local-recording-item" data-local-recording-item="' + esc(item.recording_id) + '">' +
        '<div class="local-recording-info">' +
        '<strong>' + esc(item.course_title) + ' · ' + esc(item.title) + '</strong>' +
        '<small>' + esc(item.date || "日期未记录") + ' · ' + esc(formatTransfer(item.size_bytes)) + ' · ' + esc(item.path) + '</small>' +
        '</div>' +
        '<div class="local-recording-actions">' +
        button("在文件夹中显示", "local-storage-reveal", { small: true, quiet: true, attrs: { "data-recording": item.recording_id } }) +
        '<a class="btn btn-small btn-quiet" href="/api/recordings/' + encodeURIComponent(item.recording_id) + '/video" download>导出 MP4</a>' +
        button("删除本地文件", "local-storage-delete-one", { small: true, quiet: true, attrs: { "data-recording": item.recording_id } }) +
        '</div></li>';
    }).join("");
    return '<section class="panel local-recordings-panel" data-local-recordings-panel aria-label="已下载录像与本地存储设置">' +
      '<div class="local-recordings-header"><div><h2>已下载录像与本地存储设置</h2>' +
      '<p class="form-note">默认不保留原始录像与音频：整理笔记时仅临时提取音轨与课堂画面，整理完成后立即自动删除本地视频文件。</p></div>' +
      (!alwaysVisible ? button("收起", "toggle-local-recordings", { small: true, quiet: true }) : "") + '</div>' +
      '<div class="local-recordings-settings">' +
      '<label class="local-recordings-toggle"><input type="checkbox" data-local-storage-save-toggle' + (lr.saveLocally ? " checked" : "") + '>' +
      '<span>整理笔记后也在本地保留原始录像（默认关闭，开启后每节课约占用 500–800 MB）</span></label>' +
      '<div class="local-recordings-dir-row">' +
      '<label for="local-recordings-dir-input">录像下载保存目录</label>' +
      '<div class="local-recordings-dir-controls">' +
      '<input id="local-recordings-dir-input" type="text" class="text-input" data-local-storage-dir-input value="' + esc(lr.downloadDir) + '" placeholder="' + esc(lr.effectiveDir || "默认：数据目录下的 recordings-downloads") + '">' +
      button("选择文件夹…", "local-storage-pick-dir", { small: true, quiet: true }) +
      button("保存目录", "local-storage-save-dir", { small: true }) +
      button("打开目录", "local-storage-open-dir", { small: true, quiet: true }) +
      '</div><small class="form-note hint">当前生效目录：' + esc(lr.effectiveDir || "默认目录") + '</small></div></div>' +
      '<div class="local-recordings-list-section">' +
      '<div class="local-recordings-list-header"><h3>本机已保存录像（' + esc(lr.count) + ' 节 · 共 ' + esc(formatTransfer(lr.totalBytes)) + '）</h3>' +
      (lr.count > 0 ? button("清空全部本地录像", "local-storage-clear-all", { small: true, quiet: true }) : "") + '</div>' +
      (lr.count > 0 ? '<ul class="local-recordings-list">' + rows + '</ul>' : '<p class="form-note hint">当前本机没有保存任何原始录像文件，不占用额外磁盘空间。</p>') +
      '</div></section>';
  }

  function campusDashboardScreen() {
    var query = state.campus.query.trim().toLowerCase();
    var visible = state.campus.courses.filter(function (course) { return !query || (course.title + " " + course.code + " " + course.term + " " + course.id).toLowerCase().includes(query); });
    var titleCounts = {};
    state.campus.courses.forEach(function (course) { titleCounts[course.title] = (titleCounts[course.title] || 0) + 1; });
    var items = visible.map(function (course) {
      var titleTerm = String(course.title || '').match(/\(([^()]*学期[^()]*)\)$/);
      var classLabel = titleCounts[course.title] > 1 && course.id
        ? ' · 教学班 ' + esc(course.id.replace(/^_/, '').replace(/_\d+$/, '')) : '';
      return '<article class="course-card" data-course-card="' + esc(course.id) + '"><span class="course-icon" aria-hidden="true">' + esc(GLYPH.book) +
        '</span><div class="course-content"><span class="course-meta">' + esc(titleTerm ? titleTerm[1] : course.term || '本学期') + classLabel + '</span><h3>' + formatCourseTitleMarkup(course.title) +
        '</h3><p>' + esc(course.recordings) + ' 节课 · ' + esc(course.assignments || 0) + ' 项正式作业 · ' + esc(course.announcements || 0) + ' 则通知' +
        (course.calendar_items ? ' · ' + esc(course.calendar_items) + ' 项日历事项' : '') + '</p>' +
        (course.read_only ? '<p class="course-readonly-note" role="status">' + cachedCourseNotice(course) + '</p>' : '') + '</div><div class="course-card-footer"><details class="course-meta-details" data-persistent-disclosure="course-meta-' + esc(course.id) + '"><summary>课程信息</summary><p>' +
        esc(course.code ? '课程号 ' + (course.code.length > 14 ? course.code.slice(0, 8) + '…' + course.code.slice(-4) : course.code) : '课程号未提供') + (course.term ? ' · ' + esc(course.term) : '') +
        (course.id ? ' · 教学班 …' + esc(course.id.slice(-6)) : '') + '</p><p>目录更新：' +
        esc(course.synced_at ? course.synced_at.slice(0, 10) : "尚未更新") + '</p></details>' +
        button("查看课程", "open-campus-course", { small: true, quiet: true, attrs: { "data-course": course.id } }) + '</div></article>';
    }).join("");
    var credentials = state.campus.configured && !state.recordings.editingCredentials
      ? '<p>教学网账号已保存。' + button("更换账号", "recording-edit-credentials", { small: true, quiet: true }) + '</p>'
      : recordingCredentialsForm();
    if (state.view === "campus-account") {
      return breadcrumb([{label:"我的课程", action:"open-dashboard"}, {label:"连接设置"}]) +
        '<div class="topline"><div><h1 class="page-title">连接设置</h1><p class="page-desc">连接教学网获取课程，资料与笔记保存在本机。</p></div></div>' +
        campusSetupGuide() + recordingTaskStatus() + backgroundSyncStatus() +
        (state.recordings.error ? '<p role="alert">' + esc(state.recordings.error) + '</p>' : '') +
        '<section class="panel"><h2>教学网账号</h2>' + credentials + '</section>' +
        (state.campus.home || state.campus.homeSync ? campusHomeControls() : '') +
        localRecordingsPanel(true);
    }
    var empty = state.campus.courses.length ? (visible.length ? '<div class="course-grid">' + items + '</div>' : '<div class="empty-materials">没有找到匹配的课程。</div>') :
      '<div class="empty-materials">还没有课程。连接教学网账号后，点击「更新课程目录」。</div>';
    return '<div class="topline"><div><h1 class="page-title">我的课程</h1><p class="page-desc">查看作业与资料，按需整理课堂笔记。</p></div></div>' +
      (!(state.campus.configured && state.campus.courses.length) ? campusSetupGuide(false) : '') +
      '<section class="panel" aria-label="课程列表"><div class="course-list-toolbar"><div class="course-list-meta"><span class="course-count">' + (query ? '找到 ' + visible.length : state.campus.courses.length) + ' 门课程</span><span class="course-last-sync"> · 上次更新：' + esc(lastCampusSync()) + '</span></div><div class="course-list-actions">' +
      button("更新课程目录", "recording-sync", { small: true, primary: true,
        disabled: !state.campus.configured,
        attrs: { "aria-disabled": state.recordings.backgroundPending || state.recordings.syncPending || state.recordings.task.state === "running" || state.recordings.background.state === "running",
          "style": state.recordings.backgroundPending || state.recordings.syncPending || state.recordings.task.state === "running" || state.recordings.background.state === "running" ? "opacity:.55;cursor:not-allowed;transform:none" : "" }
      }) +
      (state.campus.courses.some(function (course) { return course.recordings > 0; }) ?
        button(state.batchOrganize && state.batchOrganize.open ? '收起一键整理' : '一键整理笔记', 'toggle-batch-organize',
          {small:true, quiet:Boolean(state.batchOrganize && state.batchOrganize.open)}) : '') +
      '<span class="local-recordings-action-wrap">' + button(
        state.localRecordings.open
          ? "收起已下载录像"
          : state.localRecordings.count > 0
            ? "已下载录像 (" + state.localRecordings.count + " · " + formatTransfer(state.localRecordings.totalBytes) + ")"
            : "已下载录像",
        "toggle-local-recordings",
        { small: true, quiet: true }
      ) + '</span>' +
      '</div></div>' + localRecordingsNoticeBar() + localRecordingsPanel(false) + recordingTaskStatus() + backgroundSyncStatus() + batchOrganizeStatus() + batchOrganizePanel() +
       (state.campus.courses.some(function (course) { return !course.synced_at; }) ?
         '<p class="form-note hint">部分课程尚未更新，请先更新目录。</p>' : '') +
       (state.campus.courses.length ? '<label class="campus-search-label">查找课程<input class="text-input" type="search" data-campus-search placeholder="输入课程名或课程号" value="' + esc(state.campus.query) + '"></label>' : '') + (state.recordings.error ? '<p role="alert">' + esc(state.recordings.error) + '</p>' : '') +
      empty + '</section>';
  }

  function campusHomeControls() {
    var syncBusy = state.recordings.syncPending || state.recordings.backgroundPending || state.recordings.task.state === "running";
    if (state.campus.demoConnection) return '';
    var busy = Boolean(state.campus.workspaceBusy);
    var waitHint = busy ? '<p role="status" class="form-note">' + esc(COPY.notion.workspace_wait_hint) + '</p>' : '';
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
        selected ? '保存到「' + esc(selected.title) + '」下面' : '选好保存位置后即可创建') + '</p>' +
      (!campusCatalogReady() ? button(syncBusy ? '正在更新课程目录…' : '先更新课程目录', 'recording-sync', {small:true, disabled:syncBusy || !state.campus.configured}) : '') + '</div>' +
      button(busy ? "正在创建并验证…" : "创建学习主页", "campus-create-workspace", {
        primary: true, disabled: busy || loading || !state.campus.parentId || !campusCatalogReady() || state.recordings.task.state === "running", attrs: { "aria-busy": String(busy) }
      }) + '</div>' + waitHint + error + '</section>';
  }

  function directOssConsent(lectureId) {
    return '<label class="recording-oss-consent"><input type="checkbox" data-recording-direct-oss data-action="toggle-direct-oss-consent" data-testid="direct-oss-consent-' + esc(lectureId || '') + '"' +
      (state.recordings.directOss ? ' checked' : '') +
      '><span>同意上传本讲音频至云端进行 AI 转写与笔记整理（完成后自动清理云端临时音频）</span></label>';
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
      '</span>' + (item.match_source === 'keyframe' ? '<span class="status-badge indexed">关键帧匹配</span>' : '') + (links ? '<div>' + links + '</div>' : item.body_text ? '<p class="material-body-text">' + esc(item.body_text) + '</p>' : '<small>教学网未提供可下载附件；可在教学网查看。</small>') +
      (control ? '<details class="material-match-disclosure"><summary>关联讲次或忽略</summary><div class="material-match-control">' + control + '</div></details>' : '') + '</li>';
  }

  function assignmentUrgency(item) {
    if (!item || item.source === 'calendar') return '';
    var dueDate = parseTime(item.due_at);
    if (!dueDate) return '';
    var hoursLeft = (dueDate.getTime() - Date.now()) / 3600000;
    if (hoursLeft > 0 && hoursLeft <= 24) return 'today';
    if (hoursLeft > 24 && hoursLeft <= 72) return 'soon';
    return '';
  }

  var lectureMaterialPicker = { recording: "", query: "", filter: "recommended", busy: false };

  function lectureMaterialTerms(value) {
    var text = String(value || '').normalize('NFKC').toLowerCase();
    var digits = {零:0, 〇:0, 一:1, 二:2, 两:2, 三:3, 四:4, 五:5, 六:6, 七:7, 八:8, 九:9};
    text = text.replace(/[零〇一二两三四五六七八九十]+/g, function (part) {
      var number;
      if (part.indexOf('十') >= 0) {
        var halves = part.split('十');
        if (halves.length !== 2 || halves.some(function (s) { return s.length > 1; })) return part;
        number = (halves[0] ? digits[halves[0]] : 1) * 10 + (halves[1] ? digits[halves[1]] : 0);
      } else if (part.length === 1) number = digits[part];
      return number > 0 && number <= 20 ? String(number) : part;
    });
    var dates = [];
    text = text.replace(/(?:20\d{2}[-/.年]\s*)?(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?:日)?|\b20\d{2}(\d{2})(\d{2})\b/g,
      function (whole, month, day, compactMonth, compactDay) {
        var m = Number(month || compactMonth), d = Number(day || compactDay);
        if (m < 1 || m > 12 || d < 1 || d > 31) return whole;
        dates.push(m + '-' + d); return ' ';
      });
    text = text.replace(/(?:chapter|lecture|week|lec|ppt|ch|第)\s*[-_ ]*0*(\d+)\s*(?:讲|章|周|课|节)?/g, ' $1 ')
      .replace(/(\d+)\s*(?:讲|章|周|课|节)/g, ' $1 ')
      .replace(/\d+/g, function (n) { return ' ' + Number(n) + ' '; });
    var terms = text.split(/[\s\p{P}\p{S}]+/u).filter(Boolean);
    return { dates: dates, numbers: terms.filter(function (t) { return /^\d+$/.test(t); }), terms: terms, text: text };
  }

  function lectureMaterialRows(course, lesson) {
    var query = lectureMaterialTerms(lectureMaterialPicker.query);
    var lessonTerms = lectureMaterialTerms((lesson.date || '') + ' ' + (lesson.title || ''));
    var number = String(lesson.number || lessonTerms.numbers[0] || course.lessons.indexOf(lesson) + 1);
    var items = course.all_materials || (course.unassigned_materials || []).concat.apply(course.unassigned_materials || [],
      course.lessons.map(function (row) { return row.materials || []; }));
    var seen = {};
    return items.filter(function (item) {
      if (seen[item.index]) return false;
      seen[item.index] = true; return true;
    }).map(function (item) {
      var terms = lectureMaterialTerms([item.title, item.path, item.body_text, (item.files || []).join(' '),
        (item.attachments || []).map(function (asset) { return asset.filename; }).join(' ')].join(' '));
      var ids = item.assigned_lecture_ids || course.lessons.filter(function (row) {
        return (row.materials || []).some(function (material) { return material.index === item.index; });
      }).map(function (row) { return row.id; });
      return { item: item, linked: ids.indexOf(lesson.id) >= 0,
        unassigned: !ids.length && item.match_state !== 'ignored',
        recommended: terms.numbers.indexOf(number) >= 0 || terms.dates.some(function (d) { return lessonTerms.dates.indexOf(d) >= 0; }),
        matches: query.dates.every(function (d) { return terms.dates.indexOf(d) >= 0; }) &&
          query.terms.every(function (term) { return /^\d+$/.test(term) ? terms.numbers.indexOf(term) >= 0 : terms.text.indexOf(term) >= 0; }) };
    }).filter(function (row) {
      if (!row.matches) return false;
      if (lectureMaterialPicker.filter === 'unassigned') return row.unassigned;
      if (lectureMaterialPicker.filter === 'all') return true;
      return row.linked || row.recommended && row.item.match_state !== 'ignored' || row.unassigned;
    }).sort(function (a, b) { return Number(b.recommended) - Number(a.recommended) || Number(b.linked) - Number(a.linked) || a.item.index - b.item.index; });
  }

  function lectureMaterialsBox(course, lesson) {
    var busy = lectureMaterialPicker.busy;
    var attrs = { 'data-recording': lesson.id };
    var linked = (lesson.materials || []).map(function (item) {
      var source = item.match_source === 'keyframe' ? '关键帧匹配' : item.match_source === 'auto' ? '自动匹配' : '手动选择';
      return '<li class="lecture-material-row"><div>' + campusMaterial(item, course.id).replace(/^<li>|<\/li>$/g, '') +
        (item.match_source === 'keyframe' ? '' : '<span class="status-badge indexed">' + source + '</span>') + '</div>' +
        button('取消关联', 'lecture-unassign-material', {small:true, quiet:true, disabled:busy,
          attrs:{'data-material':item.index, 'data-recording':lesson.id}}) + '</li>';
    }).join('');
    var local = (lesson.local_materials || []).map(function (item) {
      return '<li class="lecture-material-row"><div><strong>' + esc(item.filename) + '</strong> <small>' +
        esc(formatTransfer(item.size)) + '</small> <span class="status-badge indexed">本地上传</span></div>' +
        button('移除', 'lecture-remove-local-material', {small:true, quiet:true, disabled:busy,
          attrs:{'data-local-material':item.id, 'data-recording':lesson.id}}) + '</li>';
    }).join('');
    var picker = '';
    if (lectureMaterialPicker.recording === lesson.id) {
      var rows = lectureMaterialRows(course, lesson);
      picker = '<section class="lecture-material-picker" data-lecture-material-picker="' + esc(lesson.id) + '">' +
        '<input type="search" class="lesson-filter-input" data-lecture-material-search="' + esc(lesson.id) +
        '" aria-label="搜索教学网课件" placeholder="搜索标题、章节或日期，如第1讲 / ch1 / 第一讲" value="' + esc(lectureMaterialPicker.query) + '">' +
        '<div class="lecture-material-filters">' + [['recommended','本讲推荐'],['unassigned','尚未关联'],['all','全部课件']].map(function (filter) {
          return button(filter[1], 'lecture-material-filter', {small:true, quiet:lectureMaterialPicker.filter !== filter[0],
            attrs:{'data-filter':filter[0], 'data-recording':lesson.id, 'aria-pressed':String(lectureMaterialPicker.filter === filter[0])}});
        }).join('') + '</div><ul class="lecture-material-options">' + rows.map(function (row) {
          return '<li class="lecture-material-row" data-lecture-material-option="' + esc(row.item.index) + '"><div><strong>' + esc(row.item.title) +
            '</strong>' + (row.recommended ? ' <span class="status-badge indexed">本讲推荐</span>' : '') +
            '<small>' + esc((row.item.files || []).join(' · ')) + '</small></div>' +
            button(row.linked ? '已选 · 取消' : '关联到本讲', row.linked ? 'lecture-unassign-material' : 'lecture-assign-material',
              {small:true, quiet:row.linked, disabled:busy, attrs:{'data-material':row.item.index, 'data-recording':lesson.id}}) + '</li>';
        }).join('') + '</ul>' + (rows.length ? '' : '<p class="empty-materials">没有找到匹配的教学网课件，可切换筛选或上传本地文件。</p>') + '</section>';
    }
    return '<section class="lecture-materials-box"><h4>本讲课件</h4>' +
      (linked || local ? '<ul class="lecture-linked-materials">' + linked + local + '</ul>' :
        '<div class="empty-materials lecture-empty-materials"><p>本讲尚未关联课件</p><small>可从教学网选择课件、从电脑上传，或按视频关键帧匹配。</small></div>') +
      '<div class="lecture-materials-toolbar">' + button('从教学网选择课件', 'lecture-toggle-material-picker',
        {small:true, quiet:true, attrs:Object.assign({'aria-expanded':String(lectureMaterialPicker.recording === lesson.id)}, attrs)}) +
      '<label class="btn btn-small btn-quiet lecture-upload-label"' + (Boolean((state.quota || {}).tier) && (state.quota || {}).tier !== 'max' ? ' role="button" tabindex="0" data-action="membership-upgrade" data-tier="Max" data-feature="上传补充材料"' : '') + '>Max · 上传补充材料 / 课堂笔记 (.pdf/.pptx/.docx/.md/.txt)<input type="file" data-lecture-local-upload="' +
        esc(lesson.id) + '" accept=".pdf,.pptx,.docx,.md,.txt" multiple hidden' + (busy || (Boolean((state.quota || {}).tier) && (state.quota || {}).tier !== 'max') ? ' disabled' : '') + '></label></div>' + picker + '</section>';
  }

  function updateLectureMaterials(url, options) {
    if (lectureMaterialPicker.busy) return;
    lectureMaterialPicker.busy = true;
    var courseId = state.campus.courseId, epoch = accountQuotaEpoch;
    render();
    return requestJson(url, options).then(function (result) {
      if (!result.ok || !result.body || !result.body.lessons) throw new Error(result.body && result.body.detail || '课件更新失败，请重试。');
      if (epoch === accountQuotaEpoch && courseId === state.campus.courseId) {
        state.campus.course = result.body;
        rememberCampusCourse(result.body);
      }
    }).catch(function (error) { showToast(error.message || '课件更新失败，请重试。'); })
      .finally(function () { lectureMaterialPicker.busy = false; render(); });
  }

  function changeLectureMaterial(target, unassign) {
    return updateLectureMaterials('/api/campus/courses/' + encodeURIComponent(state.campus.courseId) +
      '/materials/' + encodeURIComponent(target.dataset.material) + '/match', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(unassign ?
          {unassign_lecture_id:target.dataset.recording} : {lecture_id:target.dataset.recording, append:true})
      });
  }

  var proCenterNotice = "当前为 Lite 等级（仅含课堂普通转写），升级 Pro / Max 可完整使用作业管理、DDL 与通知中心功能";

  function proCenterBadge() {
    return isLiteTier() ? ' <span class="status-badge tier-pro">Pro</span>' : '';
  }

  function campusOfficialSections(course, section) {
    var lite = isLiteTier();
    var tierNotice = lite ? '<p class="tier-feature-notice" role="status">' + proCenterNotice + '</p>' : '';
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
    var notices = '<section class="panel" id="course-notices"><h2>通知中心' + proCenterBadge() + '</h2>' + tierNotice +
      (noticeWarning ? '<p class="form-note hint">' + esc(noticeStatus) + '</p>' : '') +
      (noticeItems ? '<ul class="official-list">' + noticeItems + '</ul>' :
        (noticeWarning ? '' : '<p>' + esc(noticeStatus) + '</p>')) + '</section>';
    var assignmentItems = (course.assignments || []).map(function (item) {
      var status = (state.campus.assignmentStatuses || {})[item.content_id];
      var source = item.content_id && item.source !== "calendar"
        ? '/api/campus/courses/' + encodeURIComponent(course.id) +
          '/assignments/' + encodeURIComponent(item.content_id) + '/source'
        : '';
      var deadlineMention = !item.due_at_label && !item.due_at && !item.teacher_deadline_quote && String(item.instructions || '').match(/((?:\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})[^\n。；;]{0,18}(?:前|截止|提交))/);
      var due = item.due_at_label || item.due_at || (item.teacher_deadline_quote
        ? '老师原文写 ' + item.teacher_deadline_quote + '（系统无独立截止字段，请打开原要求核对）'
        : deadlineMention ? deadlineMention[1] + '（正文提及）' : '教学网未公布');
      var urgencyKind = assignmentUrgency(item);
      var urgency = urgencyKind ? '<span class="status-badge urgent" data-deadline-urgency="' + urgencyKind + '">' +
        (urgencyKind === 'today' ? '今天截止' : '即将截止') + '</span>' : '';
      var caveats = [];
      if (item.source === 'calendar') caveats.push('来自教学网日历提醒，尚未关联到正式作业；具体要求与截止时间请到教学网课程中核对');
      else {
        if (!source) caveats.push('请到教学网课程中核对原要求');
        if (!item.content_id) caveats.push('尚无可验证的提交目标');
      }
      var sourceLinks = (item.source_links || []).filter(function (link) {
        return link && /^https:\/\/[^/]+/i.test(link.url || '') && link.label;
      }).map(function (link) {
        return '<a href="' + esc(link.url) + '" target="_blank" rel="noopener noreferrer">' +
          esc(link.label) + '</a>';
      }).join(' · ');
      return '<li><div class="official-item-header"><strong>' + esc(item.title) + '</strong>' + urgency + '<span class="status-badge ' +
        (item.source === 'calendar' ? 'pending">日历提醒' : 'indexed">正式作业') + '</span></div><small>' + (item.source === 'calendar' ? '日历时间：' : '截止：') + esc(due) +
        ' · 来源：教学网' + (item.source === "calendar" ? "日历" : "作业") +
        ' · 更新：' + esc(updated) + '</small><p>' + esc(item.instructions || "要求未提供，请打开教学网原页面核对。") +
        '</p>' +
        ((item.files || []).length ? '<p>附件：' + esc(item.files.join("、")) + '</p>' : '') +
        (sourceLinks ? '<p>老师原页面链接：' + sourceLinks + '</p>' : '') +
        '<div class="official-item-actions">' + (source ? '<a href="' + esc(source) + '" target="_blank" rel="noopener noreferrer">查看教学网原要求</a>' :
          '') +
        (source ? button(status && status.loading ? '正在读取提交状态…' : '读取教学网提交状态', 'campus-assignment-status', {
          small: true, disabled: Boolean(status && status.loading), attrs: { 'data-assignment': item.content_id } }) : '') +
        (item.content_id && item.source !== "calendar" ? button("准备作业文件", "campus-prepare-assignment", { small: true, disabled: lite,
          attrs: { "data-assignment": item.content_id } }) : '') + '</div>' +
        (status ? '<p role="status">' + esc(status.label || '教学网当前提交状态未确认') +
          (status.checked_at ? ' · 核对时间：' + esc(status.checked_at) : '') + '</p>' +
          (status.error ? '<p role="alert">' + esc(status.error) + '</p>' : '') : '') +
        (caveats.length ? '<div class="official-item-footer">' + caveats.map(function (text) {
          return '<span class="official-caveat">' + esc(text) + '</span>';
        }).join('') + '</div>' : '') + '</li>';
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
      var filenameSuggestion = '';
      if (extracted && extracted.analysis && formValues.submission_mode === 'zip') {
        var filenameRule = (extracted.analysis.filename_rules || []).find(function (row) {
          return String(row.quote || '').trim();
        });
        if (filenameRule) {
          var suggestedName = String(filenameRule.quote).trim();
          var quotedName = suggestedName.match(/[“「『"'`]([^”」』"'`]+)[”」』"'`]/);
          suggestedName = quotedName ? quotedName[1] : suggestedName.replace(/^.*?(?:命名为|文件名[为是：:\s]*|命名[：:\s]+)/, '');
          suggestedName = suggestedName.replace(/^[\s“”「」『』"'`（(：:]+|[\s“”「」『』"'`）)。，,；;！!：:]+$/g, '');
          var zipEnding = suggestedName.match(/\.zip(?=$|[\s”」』"'`。，,；;！!）)])/i);
          if (zipEnding) suggestedName = suggestedName.slice(0, zipEnding.index + 4);
          if (suggestedName) {
            if (!/\.zip$/i.test(suggestedName)) suggestedName += '.zip';
            filenameSuggestion = button('套用命名规则', 'campus-apply-filename-rule', {
              small: true, quiet: true, disabled: lite, attrs: { 'data-filename-template': suggestedName }
            });
          }
        }
      }
      var analysisView = extracted && extracted.analysis ? '<div data-assignment-requirements><h4>已核对原文的交付要求</h4><ul>' +
        extracted.analysis.requirements.concat(extracted.analysis.filename_rules).map(function (row) {
          return '<li>' + esc(row.quote) + '</li>';
        }).join('') + '</ul>' + filenameSuggestion + '<p>原文指定格式：' +
        esc(extracted.analysis.file_formats.map(function (row) { return row.value; }).join('、') || '未明确，请核对') +
        '</p>' + extracted.analysis.uncertainties.map(function (item) { return '<p class="form-note hint">待核对：' + esc(item) + '</p>'; }).join('') +
        '<p data-assignment-analysis-cost="' + esc(extracted.points_charged) + '">本次用量：' +
        esc(extracted.points_charged) + ' AI 点' + (extracted.reused ? '（复用已保存结果）' : '') + '</p>' +
        (extracted.reused ? '<p>原任务用量：' + esc(extracted.receipt_points_charged) + ' AI 点</p>' : '') + '</div>' : '';
      var rarHint = /\.rar\b/i.test(selectedItem.instructions || "") ?
        '<p class="form-note hint">老师要求中出现 .rar；这里生成的是 ZIP。请核对老师是否接受 ZIP，需要 RAR 时在本机按原要求制作。</p>' : '';
      preparation = '<div class="assignment-preparation"><h3>准备作业文件 · ' + esc(selectedItem.title) +
        '</h3><p>按老师要求选择本地答案文件。ZIP 模式会将所选文件打包；单文件模式保留原文件格式。在线作答或随堂考勤请使用教学网原入口。草稿只保存在本机，准备阶段不会提交。</p>' + rarHint +
        button(state.campus.submission.analysisBusy ? '正在识别要求…' : 'AI 识别作业要求', 'campus-analyze-requirements',
          { small: true, disabled: lite || !!state.campus.submission.analysisBusy }) + analysisView +
        (draft ? '<div role="status"><p>正式提交目标：' + esc(course.title) + ' · ' + esc(draft.assignment_title) +
          '</p><p>截止：' + esc(draft.due_at_label || draft.due_at || '教学网未公布，请核对原要求') +
          '</p><p>本地草稿：' + esc(draft.archive_name) + '</p><ul>' +
          draft.files.map(function (file) { return '<li>' + esc(file.name) + ' · ' + esc(formatTransfer(file.bytes)) + '</li>'; }).join("") +
          '</ul>' + (draft.warnings || []).map(function (warning) { return '<p class="form-note hint">' + esc(warning) + '</p>'; }).join("") +
          '<a href="' + esc(draft.download_url) + '" download data-action="campus-download-draft">下载并检查' + (draft.submission_mode === "single_file" ? '文件' : '压缩包') + '</a>' +
          (state.campus.submission.saveStatus ? '<p role="status" class="assignment-download-status" data-native-save="' + esc(state.campus.submission.saveStatus.state) + '" data-saved-sha256="' + esc(state.campus.submission.saveStatus.sha256 || '') + '">' + esc(state.campus.submission.saveStatus.message) + '</p>' : '') +
          '<label class="assignment-check"><input type="checkbox" id="assignment-reviewed"> 我已对照老师要求核对提交目标、文件名、文件内容和截止时间</label>' +
          button("确认提交到教学网", "campus-submit-assignment", { primary: true, small: true,
            disabled: lite || draft.state === "submitted" }) + (draft.state === "submitted" ? '<p>已发送提交请求，请在教学网查看回执。</p>' : '') +
          (draft.receipt_download_url ? '<a href="' + esc(draft.receipt_download_url) + '" download>下载教学网正式回执</a>' : '') +
          '</div>' :
          '<form id="assignment-prepare-form"><label>提交文件形式<select name="submission_mode"><option value="zip"' + (formValues.submission_mode === "zip" ? ' selected' : '') + '>ZIP 压缩包</option><option value="single_file"' + (formValues.submission_mode === "single_file" ? ' selected' : '') + '>单个原文件（PDF、Word 等）</option></select></label>' +
          (formValues.submission_mode === "zip" ?
            '<label>压缩包文件名（按老师要求）<input name="archive_name" required placeholder="学号姓名第1次作业.zip" value="' + esc(formValues.archive_name) + '"></label>' +
            '<label>补充文件（如代码、图片、已导出的 PDF）<input name="attachments" type="file" multiple></label>' :
            '<label>已导出的答案文件<input name="attachments" type="file"></label>') +
          '<p data-assignment-attachments>' + (formValues.attachments.length ? '已选择：' + esc(formValues.attachments.map(function (file) { return file.name; }).join('、')) : '尚未选择文件') + '</p>' +
          (formValues.submission_mode === "zip" ? '<p class="form-note">请加入老师要求的全部答案文件，再生成压缩包检查。</p>' :
            '<p class="form-note">请选择老师要求格式的答案文件；此处不会转换格式或改变原文件名。</p>') +
          button("生成本地草稿并预检", "campus-build-draft", { primary: true, small: true, disabled: lite }) + '</form>') +
        (state.campus.submission.error ? '<p role="alert">' + esc(state.campus.submission.error) + '</p>' : '') +
        '</div>';
    }
    var assignmentWarning = course.assignments_status === "partial" || course.assignments_status === "error" ||
      course.assignments_status === "not_synced";
    var assignmentState = course.section_states && course.section_states.assignments;
    if (assignmentState) assignmentWarning = ["not_synced", "error", "cached"].indexOf(assignmentState.state) >= 0;
    var assignments = '<section class="panel" id="course-assignments"><h2>作业中心' + proCenterBadge() + '</h2>' + tierNotice +
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
            button('恢复回执保存', 'campus-recover-submission', { small: true, disabled: lite, attrs: { 'data-draft': item.draft_id } }) :
            (item.download_url ? ' <a href="' + esc(item.download_url) + '" download>下载教学网正式回执</a>' : '')) + '</li>';
      }).join('') + '</ul>' + (state.campus.pendingSubmissions || []).map(function (item) {
        return '<p>' + esc(item.assignment_title) + ' · ' + esc(item.archive_name) + ' · 提交结果待核验。 ' +
          button('核对未完成记录', 'campus-recover-submission', { small: true, disabled: lite, attrs: { 'data-draft': item.draft_id } }) + '</p>';
      }).join('') + button('刷新回执记录', 'campus-load-receipts', { small: true }) + '</details></section>';
    return section === "notices" ? notices : section === "assignments" ? assignments : notices + assignments;
  }

  function selectCampusAssignment(contentId) {
    if (isLiteTier()) return guideMembership("Pro / Max", "作业与通知中心");
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
    if (isLiteTier()) return guideMembership("Pro / Max", "作业与通知中心");
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
    if (isLiteTier()) return guideMembership("Pro / Max", "作业与通知中心");
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
    if (isLiteTier()) return guideMembership("Pro / Max", "作业与通知中心");
    var selected = state.campus.submission.selectedId;
    var courseId = state.campus.courseId;
    if (!selected || state.campus.submission.analysisBusy) return;
    state.campus.submission.analysisBusy = true;
    state.campus.submission.error = '';
    render();
    return requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + '/assignments/' +
      encodeURIComponent(selected) + '/requirements', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true }) }).then(function (result) {
      if (state.campus.courseId !== courseId || state.campus.submission.selectedId !== selected) return;
      state.campus.submission.analysisBusy = false;
      if (!result.ok || !result.body) state.campus.submission.error = result.body && result.body.detail || '要求识别未完成，请核对原任务。';
      else {
        state.campus.submission.analysis = result.body;
        if (result.body.analysis && !state.campus.submission.form.archive_name.trim() &&
            state.campus.submission.form.attachments.length === 0) {
          var formats = (result.body.analysis.file_formats || []).map(function (f) { return String(f.value || '').toLowerCase(); });
          if (formats.some(function (f) { return /zip|rar|7z|压缩包/.test(f); })) {
            state.campus.submission.form.submission_mode = 'zip';
          } else if (formats.some(function (f) { return /pdf|doc|docx|txt|md|py|cpp|ipynb|单文件/.test(f); })) {
            state.campus.submission.form.submission_mode = 'single_file';
          }
        }
      }
      render();
    }).catch(function () {
      if (state.campus.courseId !== courseId || state.campus.submission.selectedId !== selected) return;
      state.campus.submission.analysisBusy = false;
      state.campus.submission.error = '要求识别连接中断，原调用记录已保留；再次打开可恢复同一请求。';
      render();
    });
  }

  async function submitCampusAssignment() {
    if (isLiteTier()) return guideMembership("Pro / Max", "作业与通知中心");
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

  var readingPreviews = {};
  var mcpHub = { status: null, host: 'codex', error: '' };

  function copyStudyText(text, message) {
    function fallback() {
      var field = document.createElement('textarea');
      field.value = text; field.style.position = 'fixed'; field.style.opacity = '0';
      document.body.appendChild(field); field.select();
      var copied = document.execCommand('copy'); field.remove();
      showToast(copied ? message : '复制失败，请选择文本后手动复制。');
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { showToast(message); }).catch(fallback);
    }
    fallback();
  }

  // Escape all source text first; local notes can contain arbitrary HTML.
  function formatMath(tex) {
    var s = esc(String(tex || '').trim());
    var symbols = {
      alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ε',
      zeta: 'ζ', eta: 'η', theta: 'θ', iota: 'ι', kappa: 'κ', lambda: 'λ',
      mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ',
      phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
      Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
      Sigma: 'Σ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
      le: '≤', leq: '≤', ge: '≥', geq: '≥', ne: '≠', neq: '≠', approx: '≈',
      equiv: '≡', sim: '∼', propto: '∝', times: '×', cdot: '·', div: '÷', pm: '±', mp: '∓',
      to: '→', rightarrow: '→', leftarrow: '←', Rightarrow: '⇒', Leftarrow: '⇐',
      leftrightarrow: '↔', Leftrightarrow: '⇔', iff: '⇔', maps: '↦', mapsto: '↦',
      'in': '∈', notin: '∉', subset: '⊂', subseteq: '⊆', supset: '⊃', supseteq: '⊇',
      cup: '∪', cap: '∩', emptyset: '∅', forall: '∀', exists: '∃', neg: '¬',
      land: '∧', lor: '∨', oplus: '⊕', otimes: '⊗', perp: '⊥', parallel: '∥',
      infty: '∞', partial: '∂', nabla: '∇', sum: '∑', prod: '∏', int: '∫',
      dots: '…', cdots: '⋯', ldots: '…', mid: '∣'
    };
    s = s.replace(/\\(?:text|mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}/g, '$1')
      .replace(/\\pmod\{([^{}]*)\}/g, ' (mod $1)')
      .replace(/\\sqrt\{([^{}]*)\}/g, '√($1)')
      .replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, '<span class="math-frac"><span class="math-num">$1</span><span class="math-den">$2</span></span>')
      .replace(/\\([a-zA-Z]+)/g, function (m, name) { return symbols[name] || name; })
      .replace(/\^\{([^{}]+)\}/g, '<sup>$1</sup>')
      .replace(/\^([a-zA-Z0-9+\-])/g, '<sup>$1</sup>')
      .replace(/_\{([^{}]+)\}/g, '<sub>$1</sub>')
      .replace(/_([a-zA-Z0-9+\-])/g, '<sub>$1</sub>')
      .replace(/\\([{}])/g, '$1');
    return s;
  }

  function studyMarkdown(markdown, visuals) {
    function imageUrl(value) {
      var url = String(value || '').trim();
      if (!url || /[\s\\]/.test(url) || /^\/\//.test(url)) return '';
      try { return /^https?:$/.test(new URL(url, window.location.href).protocol) ? url : ''; }
      catch (_) { return ''; }
    }
    function card(url, caption, note) {
      var source = imageUrl(url);
      return source ? '<figure class="note-visual-card"><img src="' + esc(source) + '" alt="' + esc(caption) + '" loading="lazy" data-zoomable-image><figcaption><span>' + esc(caption) + (note ? '<br>' + esc(note) : '') + '</span></figcaption></figure>' : '';
    }
    function sectionVisuals(heading) {
      var items = (Array.isArray(visuals) ? visuals : []).filter(function (v) { return String(v.heading || '').trim() === heading.trim(); });
      if (!items.length) return '';
      return '<div class="note-visual-grid">' + items.map(function (v) {
        return card(v.frame_url, v.frame_caption || '课堂板书/投影 · ' + (v.time || ''), v.board_note) +
          (imageUrl(v.slide_url) ? card(v.slide_url, v.slide_caption || '对应课件页') : v.slide_caption ? '<div class="note-slide-reference">📄 ' + esc(v.slide_caption) + '</div>' : '');
      }).join('') + '</div>';
    }
    function inline(value) {
      var codes = [];
      var maths = [];
      var safe = String(value || '').replace(/`([^`]+)`/g, function (_, c) {
        codes.push('<code>' + esc(c) + '</code>');
        return '\u0000CODE' + (codes.length - 1) + '\u0000';
      }).replace(/\$\$([^$]+)\$\$/g, function (_, m) {
        maths.push('<span class="math-inline">' + formatMath(m) + '</span>');
        return '\u0000MATH' + (maths.length - 1) + '\u0000';
      }).replace(/(^|[^\\])\$([^\s$][^$]*?[^\s$]|[^\s$])\$/g, function (_, pre, m) {
        maths.push('<span class="math-inline">' + formatMath(m) + '</span>');
        return pre + '\u0000MATH' + (maths.length - 1) + '\u0000';
      });
      return esc(safe)
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\u0000MATH(\d+)\u0000/g, function (_, idx) { return maths[Number(idx)] || ''; })
        .replace(/\u0000CODE(\d+)\u0000/g, function (_, idx) { return codes[Number(idx)] || ''; });
    }
    function splitTableCells(row) {
      return row.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); });
    }
    function isTableSeparator(row) {
      return /^\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)+\s*\|?$/.test(String(row || '').trim());
    }
    var rawLines = String(markdown || '').split(/\r?\n/);
    var out = [];
    var fenced = false;
    var mathBlock = null;
    for (var i = 0; i < rawLines.length; i += 1) {
      var line = rawLines[i];
      if (/^\s*```/.test(line)) {
        fenced = !fenced;
        out.push(fenced ? '<div class="study-code-wrap"><button type="button" class="study-code-copy" data-action="campus-copy-code">复制代码</button><pre><code>' : '</code></pre></div>');
        continue;
      }
      if (fenced) {
        out.push(esc(line) + '\n');
        continue;
      }
      if (mathBlock !== null) {
        if (/^\s*\$\$\s*$/.test(line)) {
          out.push('<div class="math-block">' + formatMath(mathBlock.join(' ')) + '</div>');
          mathBlock = null;
        } else {
          mathBlock.push(line);
        }
        continue;
      }
      if (/^\s*\$\$\s*$/.test(line)) {
        mathBlock = [];
        continue;
      }
      var singleMath = /^\s*\$\$(.+)\$\$\s*$/.exec(line);
      if (singleMath) {
        out.push('<div class="math-block">' + formatMath(singleMath[1]) + '</div>');
        continue;
      }
      if (line.indexOf('|') >= 0 && i + 1 < rawLines.length && isTableSeparator(rawLines[i + 1])) {
        var headers = splitTableCells(line);
        i += 2;
        var rowsHtml = '';
        while (i < rawLines.length && rawLines[i].trim().indexOf('|') >= 0) {
          var cells = splitTableCells(rawLines[i]);
          rowsHtml += '<tr>' + headers.map(function (_, col) { return '<td>' + inline(cells[col] || '') + '</td>'; }).join('') + '</tr>';
          i += 1;
        }
        i -= 1;
        out.push('<div class="study-table-wrap"><table class="study-table"><thead><tr>' +
          headers.map(function (h) { return '<th>' + inline(h) + '</th>'; }).join('') +
          '</tr></thead><tbody>' + rowsHtml + '</tbody></table></div>');
        continue;
      }
      var heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) {
        var level = Math.max(heading[1].length, 2);
        var headingHtml = inline(heading[2]);
        out.push('<h' + level + '>' + headingHtml + '</h' + level + '>' + (/^#{2,3}$/.test(heading[1]) ? sectionVisuals(heading[2]) : ''));
        continue;
      }
      var image = /^\s*!\[([^\]]*)\]\(([^\s]+)\)\s*$/.exec(line);
      if (image && imageUrl(image[2])) {
        out.push(card(image[2], image[1]));
        continue;
      }
      if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        var sub = /^\s{2,}(?:[-*+]|\d+[.)])\s+/.test(line);
        out.push('<li' + (sub ? ' class="study-subitem"' : '') + '>' + inline(line.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, '')) + '</li>');
        continue;
      }
      if (/^>\s?/.test(line)) {
        out.push('<blockquote>' + inline(line.replace(/^>\s?/, '')) + '</blockquote>');
        continue;
      }
      if (line.trim()) out.push('<p>' + inline(line) + '</p>');
    }
    if (mathBlock !== null) out.push('<div class="math-block">' + formatMath(mathBlock.join(' ')) + '</div>');
    return out.join('') + (fenced ? '</code></pre></div>' : '');
  }

  function highlightStudyText(html, query, hitIndex) {
    if (!query) return { html: html, count: 0 };
    var template = document.createElement('template');
    template.innerHTML = html;
    var walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    var pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    var count = 0;
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var fragment = document.createDocumentFragment();
      var offset = 0;
      var match;
      pattern.lastIndex = 0;
      while ((match = pattern.exec(text))) {
        fragment.appendChild(document.createTextNode(text.slice(offset, match.index)));
        var mark = document.createElement('mark');
        mark.className = 'study-search-hit';
        mark.textContent = match[0];
        fragment.appendChild(mark);
        offset = match.index + match[0].length;
        count += 1;
      }
      if (offset) {
        fragment.appendChild(document.createTextNode(text.slice(offset)));
        node.replaceWith(fragment);
      }
    });
    if (count > 0) {
      var activeMark = template.content.querySelectorAll('mark.study-search-hit')[(((hitIndex || 0) % count) + count) % count];
      activeMark.classList.add('study-search-hit-active');
      activeMark.dataset.activeSearchHit = 'true';
    }
    return { html: template.innerHTML, count: count };
  }

  function previewReader(key, lecture) {
    var item = readingPreviews[key];
    if (!item || !item.open) return '';
    var query = (item.query || '').trim();
    var content = highlightStudyText(studyMarkdown(item.markdown || '本讲尚无笔记，整理完成后即可阅读。', item.visuals), query, item.hitIndex);
    return '<section class="lecture-note-reader" aria-label="' + (lecture ? '本讲课堂笔记' : '课程总结预览') + '">' +
      '<div class="reader-toolbar">' +
      (item.markdown ? button(lecture ? '复制笔记全文' : '复制 Markdown', 'campus-copy-markdown', {small:true, attrs:{'data-preview':key}}) +
        button('下载 .md', 'campus-download-markdown', {small:true, quiet:true, attrs:{'data-preview':key}}) +
        '<input type="search" class="reader-search-input" data-reader-search="' + esc(key) + '" aria-label="搜索笔记内容" placeholder="搜索笔记内容" value="' + esc(item.query || '') + '">' +
        (query ? '<small class="reader-search-count" data-reader-search-count>' + (content.count ? '找到 ' + content.count + ' 处' : '无匹配') + '</small>' : '') : '') + '</div>' +
      (item.loading ? '<p role="status">正在读取本地笔记…</p>' : item.error ? '<p role="alert">' + esc(item.error) + '</p>' :
        '<div class="study-markdown">' + content.html + '</div>') + '</section>';
  }

  function openStudyPreview(recording, forceOpen) {
    var epoch = accountQuotaEpoch;
    var courseId = state.campus.courseId;
    var key = courseId + ':' + (recording || 'summary');
    if (!forceOpen && readingPreviews[key] && readingPreviews[key].open && !readingPreviews[key].loading && !readingPreviews[key].error) {
      readingPreviews[key].open = false; render(); return;
    }
    var item = readingPreviews[key] = {open:true, loading:true, markdown:'', lecture:Boolean(recording), hitIndex:0};
    render();
    requestJson('/api/campus/courses/' + encodeURIComponent(courseId) + (recording ? '/recordings/' + encodeURIComponent(recording) + '/note-preview' : '/summary-preview')).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok || !result.body) throw new Error(result.body && result.body.detail || '暂时无法读取笔记，请重试。');
      item.markdown = result.body.markdown; item.visuals = result.body.visuals || []; item.loading = false; render();
    }).catch(function (error) {
      if (epoch !== accountQuotaEpoch) return;
      item.loading = false;
      if (forceOpen) item.open = false;
      else item.error = error.message;
      render();
    });
  }

  function mcpHubScreen() {
    var data = mcpHub.status;
    if (!data) return '<section class="panel"><h1>MCP AI 学习工作台</h1><p role="status">' + esc(mcpHub.error || '正在读取本地知识库…') + '</p>' +
      (mcpHub.error ? button('重试', 'open-mcp-hub', {small:true}) : '') + '</section>';
    var host = data.hosts.find(function (row) { return row.id === mcpHub.host; }) || data.hosts[0];
    return '<section class="panel mcp-hub"><p class="eyebrow">MCP 接入</p><h1>AI 工具连接</h1><p>已就绪课程 ' + esc(data.courses_count) + ' 门 · 可供 AI 调阅的课堂总结/笔记 ' + esc(data.notes_count) + ' 篇</p>' +
      '<div class="mcp-hosts" aria-label="选择 AI 工具">' + data.hosts.map(function (row) {return button(row.name, 'select-mcp-host', {small:true, primary:row.id === host.id, attrs:{'data-host':row.id, 'aria-pressed':String(row.id === host.id)}});}).join('') + '</div>' +
      '<h2>' + esc(host.name) + '</h2><p>' + esc(host.description) + '</p>' +
      (host.cli_command ? '<h3>运行一次即可接入</h3><pre><code>' + esc(host.cli_command) + '</code></pre><div class="reader-toolbar">' + button('复制接入命令', 'copy-mcp-command', {small:true}) + '</div>' : '') +
      '<h3>配置文件</h3><p class="mcp-config-path">' + esc(host.config_path) + '</p><pre><code>' + esc(host.config_snippet) + '</code></pre>' +
      '<div class="reader-toolbar">' + button('一键复制配置', 'copy-mcp-snippet', {small:true, primary:true}) + button('复制 AI 提问示例', 'copy-mcp-prompt', 'small:true' ? {small:true} : {}) + '</div>' +
      '<blockquote>' + esc(host.sample_prompt) + '</blockquote><p>接入后可查询课程总结、阅读讲次笔记、搜索知识与作业线索。</p></section>';
  }

  function openCampusLesson(recordingId) {
    state.campus.section = 'lecture';
    state.campus.lessonId = recordingId;
    var lesson = state.campus.course && state.campus.course.lessons.find(function (item) { return item.id === recordingId; });
    if (lesson && lesson.notes_available) openStudyPreview(recordingId, true);
    else render();
  }

  function campusCourseScreen() {
    var course = state.campus.course;
    var section = state.campus.section === "quizzes" ? "home" : state.campus.section || "home";
    var sectionLabels = { assignments: "作业中心", notices: "通知中心", quizzes: "小测中心", materials: "课程资料", settings: "课程设置", lecture: "讲次" };
    var trail = [{ label: "我的课程", action: "open-dashboard" }, { label: course ? course.title : "教学网课程", action: section !== "home" ? "campus-course-home" : undefined }];
    if (section !== "home") trail.push({ label: sectionLabels[section] || "课程" });
    var pager = '';
    if (section === "lecture" && course && course.lessons.length > 1) {
      var activeIndex = course.lessons.findIndex(function (lesson) { return lesson.id === state.campus.lessonId; });
      if (activeIndex >= 0) {
        pager = '<div class="breadcrumb-pager">' +
          button("上一讲", "campus-open-lesson", { small: true, quiet: true, disabled: activeIndex <= 0, attrs: { "data-recording": activeIndex > 0 ? course.lessons[activeIndex - 1].id : "", "data-lesson-nav": "prev", "title": "上一讲 ([)" } }) +
          button("下一讲", "campus-open-lesson", { small: true, quiet: true, disabled: activeIndex >= course.lessons.length - 1, attrs: { "data-recording": activeIndex < course.lessons.length - 1 ? course.lessons[activeIndex + 1].id : "", "data-lesson-nav": "next", "title": "下一讲 (])" } }) + '</div>';
      }
    }
    var header = breadcrumb(trail, pager);
    // Keep the current course visible while a background refresh replaces it.
    // Replacing a long page with the short loading panel makes the browser
    // clamp the scroll container to zero, which feels like a random jump.
    if (state.campus.loading && !course) return header + '<section class="panel" role="status">正在读取课程目录…</section>';
    if (!course) return header + '<section class="panel" role="alert">' + esc(state.campus.error || "课程尚未同步。") + '</section>';
    var needsTranscription = false;
    var lessonQuery = state.campus.dateQuery.trim().toLowerCase();
    var showLessonFilter = section === "home" && course.lessons.length >= 4;
    var lessons = course.lessons.filter(function (lesson) {
      if (section === "lecture") return lesson.id === state.campus.lessonId;
      return !lessonQuery || [lesson.date || "", lesson.title || "", lesson.note_topic || "",
        (lesson.materials || []).map(function (m) { return m.title || ""; }).join(" ")
      ].join(" ").toLowerCase().includes(lessonQuery);
    }).map(function (lesson, index) {
      var topicLabel = lesson.note_topic || ((lesson.materials && lesson.materials.length) ? '课件：' + lesson.materials.map(function (m) { return m.title; }).join('、') : '');
      var topicLine = topicLabel ? '<p class="lesson-topic-line">' + esc(topicLabel) + '</p>' : '';
      if (section !== "lecture") {
        return '<article class="campus-lesson-page" data-lesson-card="' + esc(lesson.id) + '"><div><small>第 ' + esc(course.lessons.indexOf(lesson) + 1) + ' 节课 · ' + esc(lesson.date || "日期未提供") + '</small>' +
          '<h3>' + formatLessonTitleMarkup(lesson.title, lesson.date) + '</h3>' + topicLine + '<div class="lesson-status">' + (lesson.notes_available ? '<span class="recording-published">已有课堂笔记</span>' :
            lesson.transcript_available ? '<span class="status-badge indexed">已生成转写</span>' : '<span class="status-badge reading">待整理笔记</span>') +
          ((lesson.materials || []).length ? '<small class="lesson-material-count">已关联 ' + esc(lesson.materials.length) + ' 份课件</small>' : '') + '</div></div>' +
          button("进入讲次", "campus-open-lesson", { small: true, attrs: { "data-recording": lesson.id } }) + '</article>';
      }
      var publishExisting = Boolean(lesson.publish_ready && !lesson.video_available);
      var canRegenerate = Boolean(lesson.transcript_available && lesson.notes_available);
      var publishSaved = publishExisting && !canRegenerate;
      needsTranscription = needsTranscription || (!lesson.transcript_available && !publishSaved);
      var details = lesson.duration_seconds ? ' · 约 ' + Math.ceil(lesson.duration_seconds / 60) +
        ' 分钟' : '';
      if (publishSaved) details += ' · 本机已有笔记与课堂画面';
      else if (lesson.transcript_available && !lesson.notes_available) details += ' · 已生成转写';
      if (lesson.video_available && Number(lesson.video_size) > 0) details += ' · 本地视频 ' + formatTransfer(lesson.video_size);
      else if (!lesson.duration_seconds && !lesson.unavailable_reason) details += ' · 在线回放';
      var metaText = lesson.title && lesson.date && String(lesson.title).indexOf(lesson.date) === 0
        ? details.replace(/^ · /, '') : (lesson.date || "日期未记录") + details;
      if (lesson.unavailable_reason) metaText += (metaText ? ' · ' : '') + lesson.unavailable_reason;
      var lessonPreview = readingPreviews[course.id + ':' + lesson.id];
      var lessonPreviewOpen = Boolean(lessonPreview && lessonPreview.open && !lessonPreview.loading && !lessonPreview.error);
      var blocked = lesson.unavailable_reason || (course.read_only ? course.read_only_message || "教学网尚未完整读取，旧目录只读。" : courseTaskBusy(course.id) ? "这门课正在更新或处理，请等待当前任务完成。" : state.campus.demoConnection ? "演示模式不会实际整理，请在正式程序中登录。" : "");
      var review = state.termReview.courseId === course.id && state.termReview.recordingId === lesson.id ? termReviewPanel(lesson) : "";
      return '<article class="recording-row"><div><span class="course-meta">第 ' + esc(course.lessons.indexOf(lesson) + 1) + ' 节课</span><h3>' + formatLessonTitleMarkup(lesson.title, lesson.date) +
        '</h3>' + topicLine + (metaText ? '<p>' + esc(metaText) + '</p>' : '') +
        lectureMaterialsBox(course, lesson) + '</div><div class="recording-row-actions">' +
        '<div class="recording-primary-bar">' + (lesson.notes_available ? button(lessonPreviewOpen ? '收起本讲笔记' : '阅读本讲笔记', 'campus-preview-lecture-note', {small:true, primary:!lessonPreviewOpen, quiet:lessonPreviewOpen, attrs:{'data-recording':lesson.id}}) : '') + (!publishExisting || canRegenerate ? recordingNoteStyle(lesson.id, Boolean(blocked)) : "") +
        (canRegenerate ? button("重新整理笔记", "campus-regenerate", { small: true, quiet: true, disabled: Boolean(blocked), attrs: { "data-recording": lesson.id } }) : button(lesson.notes_available ? "重新整理笔记" : "生成本讲笔记", "campus-process", { small: true, primary: !lesson.notes_available,
              disabled: Boolean(blocked),
              attrs: { "data-recording": lesson.id } })) + (blocked ? '<small class="action-reason">' + esc(blocked === (course.read_only_message || '教学网尚未完整读取，旧目录只读。') ? '请先更新这门课' : blocked) + '</small>' : '') + '</div><details class="lecture-more-actions" data-persistent-disclosure="lecture-tools-' + esc(lesson.id) + '"><summary>更多工具</summary><div class="recording-secondary-bar">' +
        (lesson.video_available || lesson['keyframe' + 's_available'] ? button('按关键帧匹配课件', 'campus-match-slides-keyframe' + 's', {small:true, quiet:true, disabled:courseTaskBusy(course.id), attrs:{'data-recording':lesson.id}}) : '') +
        (!lesson.duration_seconds && state.campus.configured ? button('读取时长', 'campus-estimate-duration', { small: true, quiet: true, attrs: { 'data-recording': lesson.id } }) : '') +
        (lesson.video_available
          ? '<a class="btn btn-small btn-quiet" href="/api/recordings/' + encodeURIComponent(lesson.id) + '/video" download>下载原始录像</a>' +
            button("在文件夹中显示", "local-storage-reveal", { small: true, quiet: true, attrs: { "data-recording": lesson.id } }) +
            button("删除本地录像", "local-storage-delete-one", { small: true, quiet: true, attrs: { "data-recording": lesson.id } })
          : button("下载原始录像", "recording-download", { small: true, quiet: true,
              disabled: !state.campus.configured || courseTaskBusy(course.id) || Boolean(lesson.unavailable_reason),
              attrs: { "data-recording": lesson.id } })) +
        (lesson.transcript_available || lesson.notes_ready ? button("核对术语", "campus-review-term", {
          small: true, quiet: true, attrs: { "data-recording": lesson.id, "aria-expanded": review ? "true" : "false" }
        }) : "") +
        '</div></details>' +
        (lesson.transcript_reviewed_pending ? '<small class="recording-published">转写已修正，请重新整理笔记。</small>' : "") +
        '</div>' + previewReader(course.id + ':' + lesson.id, true) + review + '</article>';
    }).join("");
    var materialQuery = section === "materials" ? (state.campus.materialQuery || '').trim().toLowerCase() : '';
    function matchesMaterial(item, lesson) {
      return (state.campus.category === "all" || item.category === state.campus.category) &&
        (!materialQuery || [item.title || '', item.body_text || '', lesson && lesson.title || '',
          (item.files || []).join(' ')].join(' ').toLowerCase().includes(materialQuery));
    }
    var pendingItems = course.unassigned_materials.filter(function (item) {
      return matchesMaterial(item);
    });
    var infoItems = pendingItems.filter(function (item) { return item.category === "information"; });
    var slideItems = pendingItems.filter(function (item) { return item.category !== "information"; });
    var infoSection = infoItems.length ? '<section class="panel" data-materials-info><h2>课程大纲与基本信息</h2><p>教学网提供的课程介绍、教学大纲与考核说明。</p><ul>' +
      infoItems.map(function (item) { return campusMaterial(item, course.id); }).join("") + '</ul></section>' : '';
    var hasLessons = course.lessons.length > 0;
    var pending = (slideItems.length || !materialQuery && !infoItems.length) ? '<section class="panel" data-materials-unassigned><h2>' + (hasLessons ? '待关联讲次的课件与资料' : '课程内容') + '</h2><p>' + (hasLessons ? '下载或整理录像后将自动按视频关键帧匹配课件，也可在此手动关联到对应讲次。' : '教学网提供的资料、作业和课程信息。可直接下载有附件的内容。') + '</p><ul>' +
      (slideItems.length ? slideItems.map(function (item) { return campusMaterial(item, course.id, course.lessons); }).join("") : '<li>' + esc(course.section_states && course.section_states.materials && ['not_synced', 'error', 'cached'].indexOf(course.section_states.materials.state) >= 0 ? course.section_states.materials.message : '当前分类暂无内容。') + '</li>') + '</ul></section>' : '';
    var summary = course.summary && course.summary.title ? course.summary : null;
    var summaryControl = '<div class="course-summary-control">' +
      (summary && summary.title ? '<small>' + esc(summary.title) + '</small>' : "") +
      button(isLiteTier() ? "Pro · 生成课程总结" : summary ? "重新生成课程总结" : "生成课程总结", "campus-course-summary", {
        small: true, quiet: Boolean(summary),
        disabled: courseTaskBusy(course.id) || state.campus.demoConnection,
        attrs: { "data-course": course.id }
      }) + '</div>';
    var hero = '<section class="course-hero' + (section !== 'home' ? ' course-hero-compact' : '') + '" data-course-id="' + esc(course.id) + '"><div><span class="course-kicker">教学网课程</span><h1>' + formatCourseTitleMarkup(course.title) +
      '</h1><small>' + esc(course.term || '') + '</small></div><div class="hero-actions">' +
      button("更新这门课", "recording-sync-course", { small: true,
        // Preserve real double-click input; the task guard rejects repeats.
        disabled: !state.campus.configured,
        attrs: { "data-course": course.id,
          "aria-disabled": courseTaskBusy(course.id),
          "style": courseTaskBusy(course.id) ? "opacity:.55;cursor:not-allowed;transform:none" : "" }
      }) + (course.lessons.length > 1 ? button('批量整理本课', 'open-course-batch-organize',
        {small:true, quiet:true, attrs:{'data-course':course.id}}) : '') + '</div></section>';
    var notice = course.read_only ? '<section class="panel" role="status">' + cachedCourseNotice(course) + '</section>' : '';
    if (state.campus.loading) notice += '<p role="status">正在读取本课最新缓存；已有内容可继续浏览。</p>';
    if (state.campus.error) notice += '<p role="alert">' + esc(state.campus.error) + '</p>';
    if (state.recordings.error && (!state.recordings.errorCourseId || state.recordings.errorCourseId === course.id))
      notice += '<p role="alert">' + esc(state.recordings.error) + '</p>';
    notice += backgroundSyncStatus() + batchOrganizeStatus() + batchOrganizePanel();
    if (section === "assignments" || section === "notices") return header + hero + notice + recordingTaskStatus() + campusOfficialSections(course, section);
    var cachedSummary = readingPreviews[course.id + ':summary'];
    var hasSummary = Boolean(summary || course.has_summary || (course.has_summary === undefined && (course.lessons || []).some(function (lesson) { return lesson.notes_available; })) || (cachedSummary && cachedSummary.markdown && !cachedSummary.error));
    var summaryPreviewOpen = Boolean(cachedSummary && cachedSummary.open);
    var summaryReadControl = hasSummary ? button(summaryPreviewOpen ? '收起课程总结' : '阅读课程总结',
      'campus-preview-course-summary', {small:true, primary:!summaryPreviewOpen, quiet:summaryPreviewOpen,
        attrs:{'data-course':course.id}}) : '<small class="form-note hint">暂未生成课程总结</small>';
    if (section === "materials") {
      var materialCount = course.lessons.reduce(function (count, lesson) {
        return count + (lesson.materials || []).length;
      }, course.unassigned_materials.length);
      var materialFilter = materialCount >= 4 ? '<div class="section-label materials-filter-bar"><h2>课程资料</h2><input type="search" class="lesson-filter-input" data-campus-material-filter aria-label="筛选课程资料" placeholder="搜索标题、内容或附件名" value="' + esc(state.campus.materialQuery || '') + '"></div>' : '';
      var linkedMaterials = course.lessons.map(function (lesson) {
        var items = (lesson.materials || []).filter(function (item) {
          return matchesMaterial(item, lesson);
        });
        return items.length ? '<section class="panel" data-materials-linked><h2>' + esc(lesson.title) + '</h2><ul>' +
          items.map(function (item) { return campusMaterial(item, course.id); }).join('') + '</ul></section>' : '';
      }).join('');
      var emptyMaterials = materialQuery && !linkedMaterials && !infoItems.length && !slideItems.length ?
        '<div class="empty-materials">没有找到匹配的课程资料。</div>' : '';
      return header + hero + notice + recordingTaskStatus() + '<section class="panel materials-summary-panel"><div class="materials-summary-copy"><strong>课程总结</strong><span>汇总已关联课件、课堂笔记与作业要求</span></div>' + summaryControl +
        summaryReadControl +
        previewReader(course.id + ':summary', false) + '</section>' + materialFilter + linkedMaterials + infoSection + pending + emptyMaterials;
    }
    if (section === "settings") return header + hero + notice + recordingTaskStatus() + '<details class="panel campus-settings" open><summary>课程笔记与 Notion 主页设置</summary>' + campusHomeControls() + campusExistingNotionControl(course) + '</details>';
    if (section === "lecture") return header + hero + notice + '<section class="panel" id="course-lessons">' + recordingTaskStatus() + lessons + (needsTranscription ? directOssConsent(state.campus.lessonId) : "") + '</section>';
    var centers = [['assignments', '作业中心'], ['notices', '通知中心'], ['materials', '课程资料']].map(function (entry) {
      var count = entry[0] === 'assignments' ? (course.assignments || []).length + ' 项记录' :
        entry[0] === 'notices' ? (course.announcements || []).length + ' 则通知' :
          ((course.all_materials || []).length || (course.unassigned_materials || []).length) + ' 份资料';
      var urgentCount = entry[0] === 'assignments' ? (course.assignments || []).filter(function (item) {
        return assignmentUrgency(item) !== '';
      }).length : 0;
      if (urgentCount > 0) count += ' · ' + urgentCount + ' 项即将截止';
      return '<a href="#course-' + entry[0] + '" data-action="campus-open-center" data-center="' + entry[0] + '"' +
        (urgentCount > 0 ? ' data-urgent-assignments="' + urgentCount + '"' : '') + '><span class="center-title">' + esc(entry[1]) + (entry[0] !== 'materials' ? proCenterBadge() : '') + '</span><small class="center-count">' + esc(count) + '</small><span class="center-arrow" aria-hidden="true">↗</span></a>';
    }).join('');
    var activeTaskNotice = state.recordings.task.state === "running" || state.recordings.task.state === "failed" ||
      ["done", "partial", "cancelled"].indexOf(state.recordings.task.state) >= 0 && ["sync", "summary"].indexOf(state.recordings.task.kind) >= 0
      ? recordingTaskStatus() : "";
    var historical = (course.historical_lessons || []).filter(function (lesson) {
      return lesson.historical === true && lesson.read_only === true && lesson.processing_allowed === false &&
        lesson.course_id === course.id && lesson.history_current_revision === course.catalog_revision;
    }).map(function (lesson) {
      return '<article class="campus-lesson-page" data-history-source="' + esc(lesson.source_id) + '"><div><small>历史讲次 · 只读</small>' +
        '<h3>' + esc(lesson.title) + '</h3><p>原记录时间：' + esc(lesson.recorded_at) + '</p></div>' +
        '<small>历史学习内容保存在本机。</small>' + '</article>';
    }).join('');
    var historicalDirectory = historical ? '<section id="course-history" class="campus-lesson-directory"><div class="section-label"><h2>历史讲次</h2><small>只读</small></div>' +
      '<p>这些讲次已不在教学网现行目录中，原页面和学习内容会保留。</p><div class="campus-lesson-pages">' + historical + '</div></section>' : '';
    var spotlight = '<section class="panel course-summary-spotlight" aria-label="课程总结"><div class="summary-spotlight-header"><h2>课程总结</h2><span class="summary-progress">已整理 ' +
      course.lessons.filter(function (lesson) {return lesson.notes_available;}).length + ' / ' + course.lessons.length + ' 讲课堂笔记</span></div><p>汇总各讲核心概念、重点脉络与口头提醒，方便复习串讲与随时查阅。</p>' + summaryControl +
      summaryReadControl + previewReader(course.id + ':summary', false) + '</section>';
    return header + hero + spotlight + notice + activeTaskNotice + '<nav class="campus-centers" aria-label="课程内容">' + centers + '</nav>' +
      '<section id="course-lessons" class="campus-lesson-directory"><div class="section-label"><h2>每节课</h2><small>' + esc(course.lessons.length) + ' 节课</small>' +
      (showLessonFilter ? '<input type="search" class="lesson-filter-input" data-campus-lesson-filter aria-label="筛选讲次" placeholder="搜索日期、主题或课件" value="' + esc(state.campus.dateQuery || "") + '">' : '') + '</div>' +
      (showLessonFilter && lessonQuery && !lessons ? '<div class="empty-materials">没有找到匹配的讲次。</div>' : hasLessons ? '<div class="campus-lesson-pages">' + lessons + '</div>' : '<p>' + esc(course.section_states && course.section_states.recordings &&
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
    state.campus.materialQuery = "";
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
    return loadCampusCourse(courseId);
  }

  function termReviewPanel(lesson) {
    var review = state.termReview;
    var termTarget = (lesson && lesson.transcript_review && lesson.transcript_review.target) || review.target || {};
    var candidateTerm = termTarget.candidate_term || lesson.candidate_term || "RSA";
    var ambiguousTerm = termTarget.ambiguous_term || lesson.ambiguous_term || "RAC";
    var content = "";
    if (review.status === "loading") content = '<p role="status">正在查找需要核对的片段…</p>';
    else if (review.status === "none") content = lesson.transcript_reviewed_pending
      ? '<p>这节录像的转写已修正。请使用「重新整理笔记」生成新版笔记。</p>'
      : '<p>这节录像目前没有待核对的术语。</p>';
    else if (review.status === "corrected") content = '<p role="status">已修正转写中的术语。请使用「重新整理笔记」生成新版笔记，并对照录像检查结果。</p>';
    else if (review.target) {
      var target = review.target;
      content = '<p>自动转写对这一处术语不确定，可能把 ' + esc(candidateTerm) + ' 识别成 ' + esc(ambiguousTerm) + '。请听原声并阅读上下文；只有确认老师说的是 ' + esc(candidateTerm) + '，才勾选并提交。</p>' +
        '<blockquote class="term-review-excerpt">' + esc(target.excerpt) + '</blockquote>' +
        '<p class="term-review-time">建议核听 ' + esc(target.start_label || "") + '–' + esc(target.end_label || "") + '</p>' +
        (review.videoAvailable && target.video_url
          ? '<video controls preload="metadata" playsinline data-term-review-video data-seek="' + esc(Math.max(0, Number(target.start) - 8) || 0) + '" src="' + esc(target.video_url) + '">浏览器无法播放这节录像。</video>'
          : '<p>这节录像尚未保存在电脑上。请先使用上方的录像下载操作，再回来核听。</p>') +
        (review.videoAvailable && target.video_url ? '<label class="term-review-confirm"><input type="checkbox" data-term-review-confirm' + (review.confirmed ? ' checked' : '') + (review.status === "submitting" ? ' disabled' : '') + '>我已听原声，确认老师说的是 ' + esc(candidateTerm) + '。</label>' +
          button("确认修正为 " + candidateTerm, "campus-submit-term-review", { small: true, primary: true, disabled: !review.confirmed || review.status === "submitting" }) : "");
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
    if (isLiteTier()) return guideMembership("Pro / Max", "课程总结");
    if (courseTaskBusy(courseId)) {
      return showToast("请等待当前任务完成后再生成课程总结。");
    }
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
    if (!lesson) return;
    var canRegenerate = Boolean(lesson.transcript_available && lesson.notes_available);
    var reuseExisting = Boolean(lesson.publish_ready && !lesson.video_available && !canRegenerate && !regenerate);
    var directOss = Boolean(state.recordings.directOss !== false && !lesson.transcript_available && !regenerate && !reuseExisting);
    var notesStrategy = isLiteTier() ? "transcript" : (state.recordings.noteStrategyByKey[recordingId] || "topics");
    return requestJson("/api/campus/courses/" + encodeURIComponent(course.id) + "/recordings/" + encodeURIComponent(recordingId) + "/process",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ direct_oss: reuseExisting ? false : directOss, regenerate: Boolean(regenerate), reuse_existing: reuseExisting }, reuseExisting ? {} : { notes_strategy: notesStrategy })) }).then(function (result) {
        if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "整理没有开始。");
        state.recordings.task = result.body;
        state.recordings.directOss = true;
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
    var modeTabs = state.authMode === "login" || state.authMode === "register"
      ? '<div class="auth-mode-tabs">' +
        '<button type="button" class="auth-mode-tab' + (state.authMode === "login" ? " active" : "") +
        '" data-action="auth-mode-login" aria-pressed="' + (state.authMode === "login") + '">' +
        esc(copy.modes.login) + '</button>' +
        '<button type="button" class="auth-mode-tab' + (state.authMode === "register" ? " active" : "") +
        '" data-action="auth-mode-register" aria-pressed="' + (state.authMode === "register") + '">' +
        esc(copy.modes.register) + '</button></div>'
      : "";
    var note = state.notice || "";
    var noteClass = state.notice && state.noticeKind === "error" ? "form-note" : "form-note hint";
    var passwordField =
      state.authMode === "forgot"
        ? ""
        : '<label class="field-label" for="auth-password">' +
          esc(state.authMode === "register" ? (copy.password_register_label || copy.password_label) : copy.password_label) +
          '</label><input id="auth-password" type="password" class="text-input" autocomplete="' +
          (state.authMode === "register" ? "new-password" : "current-password") +
          '"' + (state.authMode === "register" ? ' minlength="8"' : '') + ' placeholder="' +
          esc(copy.password_placeholder) +
          '" required>' +
          (state.authMode === "register"
            ? '<p class="form-note hint" data-password-hint>' + esc(copy.password_hint || "密码长度至少 8 位，建议包含字母与数字。") + "</p>"
            : "");
    var confirmField = state.authMode === "register"
      ? '<label class="field-label" for="auth-password-confirm">' +
        esc(copy.confirm_password_label) +
        '</label><input id="auth-password-confirm" type="password" class="text-input" autocomplete="new-password" minlength="8" placeholder="' +
        esc(copy.confirm_password_placeholder) +
        '" required>'
      : "";
    var inviteField = state.authMode === "register"
      ? '<label class="field-label" for="auth-invite">' +
        esc(copy.invite_label || "邀请码或邀请链接（选填）") +
        '</label><input id="auth-invite" type="text" class="text-input" autocomplete="off" placeholder="' +
        esc(copy.invite_placeholder || "填入同学分享的邀请码或链接，绑定新学号后双方各得 1 个月额度") +
        '">'
      : "";
    var switches = "";
    if (state.authMode === "login") {
      switches =
        '<div class="auth-switches">' +
        button(mode.switch_forgot, "auth-mode-forgot", { quiet: true, small: true }) +
        "</div>";
    } else if (state.authMode !== "register") {
      switches =
        '<div class="auth-switches">' +
        button(mode.switch_login, "auth-mode-login", { quiet: true, small: true }) +
        "</div>";
    }
    return (
      '<main class="onboarding">' +
      '<section class="onboarding-aside" aria-labelledby="auth-aside-title">' +
      brand(false) +
      '<div class="aside-copy"><p class="eyebrow">' +
      esc(copy.aside.eyebrow) +
      '</p><h1 id="auth-aside-title">' +
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
      '</p>' + modeTabs + '<h2 class="card-title" id="auth-title">' +
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
      inviteField +
      '<div class="auth-actions">' +
      button(state.authPending ? mode.submitting : mode.submit, "auth-submit", { primary: true, type: "submit", disabled: state.authPending }) +
      "</div>" +
      '<p id="auth-help" role="' + (state.noticeKind === "error" ? "alert" : "status") + '" class="' + noteClass + '">' + esc(note) + "</p>" +
      "</form>" +
      switches +
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

  function isLiteTier() {
    var tier = String((state.quota || {}).tier || '').toLowerCase();
    return Boolean(tier) && ['pro', 'max'].indexOf(tier) < 0;
  }

  function membershipValidity(quota) {
    var value = quota.tier_expires_at || quota.membership_expires_at || quota.subscription_expires_at || quota.expires_at || quota.active_until;
    var numeric = Number(value);
    var date = value ? new Date(Number.isFinite(numeric) ? (numeric < 1e12 ? numeric * 1000 : numeric) : value) : null;
    if (!date || isNaN(date.getTime())) return Number(quota.weekly_rollover_seconds) > 0 ? '按周充能' : '长期有效';
    var stamp = date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
    return (date.getTime() < Date.now() ? '会员已到期 ' : '有效期至 ') + stamp;
  }

  function guideMembership(tier, feature) {
    var mobile = document.querySelector('.mobile-account-menu');
    var mobileVisible = mobile && mobile.getClientRects().length > 0;
    if (mobileVisible) mobile.open = true;
    var drawer = document.querySelector('.sidebar .account-drawer');
    if (!mobileVisible && drawer) drawer.open = true;
    var field = document.getElementById(mobileVisible ? 'code-mobile' : 'code');
    if (field) { field.scrollIntoView({block:'center'}); field.focus({preventScroll:true}); }
    else {
      var verify = document.querySelector((mobileVisible ? '.mobile-account-content' : '.sidebar') + ' [data-action="refresh-verification"]');
      if (verify) { verify.scrollIntoView({block:'center'}); verify.focus({preventScroll:true}); }
    }
    showToast(feature + '需 ' + tier + ' 会员。' + (accountVerified() ?
      '请在「会员续费 / 兑换码」输入续费码或礼包链接，即可升级。' : '请先验证邮箱，再在「会员续费 / 兑换码」兑换升级。'));
  }

  function cachedCourseNotice(course) {
    if (!course.read_only) return '';
    return '<span>' + esc(!state.campus.configured ? '当前展示本机已缓存的课程内容；如需同步最新课件与录像，请先连接教学网。' :
      course.read_only_message || '当前展示本机已缓存的课程内容；如需同步最新课件与录像，请先连接教学网。') + '</span>' +
      (!state.campus.configured ? button('连接教学网', 'open-campus-setup', {small:true, quiet:true}) : '');
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
    var tierKey = String(quota.tier || "").toLowerCase();
    var tierLabel = tierKey && copy.tier_labels && copy.tier_labels[tierKey]
      ? copy.tier_labels[tierKey]
      : tierKey ? tierKey.toUpperCase() : "";
    var tierBadge = tierLabel
      ? '<span class="tier-badge tier-' + esc(tierKey || "pro") + '" data-account-tier="' + esc(tierKey) + '">' + esc(tierLabel) + '</span>'
      : "";
    var statusLine = '<p class="account-email">' + esc(email ? email + ' · ' : '') +
      esc(verified ? copy.verified : copy.unverified) + '</p>';
    var aiPoints = Math.max(0, Number(quota.llm_points_remaining !== undefined ? quota.llm_points_remaining : quota.ai_credits) || 0);
    var totalMins = Math.floor(Math.max(0, Number(quota.transcribe_seconds_remaining) || 0) / 60);
    var hasBreakdown = quota.basic_seconds_remaining !== undefined || quota.advanced_seconds_remaining !== undefined;
    var quotaDetails = quota.available === false ? '' :
      '<p class="quota-dual-line" data-dual-quota>' +
      (hasBreakdown ? esc(copy.basic_quota_label) + ' ' + Math.floor(Math.max(0, Number(quota.basic_seconds_remaining) || 0) / 60) +
        ' 分钟 · ' + esc(copy.advanced_quota_label) + ' ' + Math.floor(Math.max(0, Number(quota.advanced_seconds_remaining) || 0) / 60) + ' 分钟 · ' : '') +
      'AI 点 ' + esc(aiPoints) + '</p>' +
      (hasBreakdown && (Number(quota.advanced_seconds_remaining) > 0 || !isLiteTier()) ?
        '<p class="form-note hint">' + esc(copy.advanced_fallback_hint) + '</p>' : '') +
      (Number(quota.weekly_rollover_seconds) > 0 ? '<p class="form-note hint">每周充能 25 小时自动滚存，有效期内未用完时长保留。</p>' : '');
    var balance = (quota.available === false ? '<p>额度暂时无法读取</p>' :
      '<p class="quota-available">可用转写 ' + esc(totalMins) + ' 分钟</p>') +
      '<p class="form-note hint membership-validity" data-membership-expiry>' + esc(membershipValidity(quota)) + '</p>';
    if (state.quotaStale) {
      balance += '<p class="form-note hint" role="status">' + esc(copy.quota_stale) + '</p>';
    }
    var inviteBlock = quota.invite_code
      ? '<div id="invite-box' + suffix + '" class="quota-invite-box" data-invite-box>' +
        '<p class="field-label">' + esc(copy.invite_title || "邀请同学得 1 个月额度") +
        (quota.invited_count ? ' · 已邀 ' + esc(quota.invited_count) + ' 人' : '') + '</p>' +
        '<p class="account-invite-code">邀请码：<code id="account-invite-code' + suffix + '">' + esc(quota.invite_code) + '</code></p>' +
        '<div class="invite-copy-actions">' +
        button(copy.invite_copy_code || "复制邀请码", "copy-invite-code", { small: true, quiet: true, attrs: { id: "copy-invite-code-btn" + suffix } }) +
        button(copy.invite_copy || "复制邀请链接", "copy-invite-link", { small: true, quiet: true, attrs: { id: "copy-invite-btn" + suffix } }) +
        '</div><p class="form-note hint">' + esc(copy.invite_help) + '</p></div>'
      : "";
    var redeemBlock = inviteBlock;
    if (!verified && email) {
      redeemBlock +=
        '<p class="form-note hint">' +
        esc(copy.unverified_help) +
        "</p>" +
        button(copy.resend, "resend-verification", { small: true, quiet: true }) +
        button(copy.refresh_verification, "refresh-verification", { small: true, quiet: true }) +
        '<p class="form-note hint">' +
        esc(copy.redeem_gated) +
        "</p>";
    } else {
      var canExchange = aiPoints >= 10;
      var exchangeBlock = isLiteTier() && aiPoints <= 0 ? '' :
        '<div class="quota-exchange-box" data-quota-exchange>' +
        button(copy.exchange_button || "10 AI 点兑换 1 小时转写时长", "exchange-quota", {
          small: true,
          quiet: true,
          disabled: !canExchange
        }) +
        '<p class="form-note hint">' + esc(copy.exchange_help || "10 AI 点可兑换 1 小时高级转写时长（可 1:1 自动抵扣普通转写）。") + "</p></div>";
      redeemBlock =
        inviteBlock +
        exchangeBlock +
        '<form id="redeem-form' + suffix + '" class="redeem-form" data-redeem-form><label class="field-label" for="code' + suffix + '">' +
        esc(copy.gift_redeem_label || copy.redeem_label) +
        '</label><div class="input-row redeem-input-row">' +
        '<input id="code' + suffix + '" type="text" class="text-input" autocomplete="off" placeholder="' +
        esc(copy.gift_redeem_placeholder || copy.redeem_placeholder) +
        '" required>' +
        button(copy.redeem, "redeem", { primary: true, small: true, disabled: redeemPending }) +
        "</div><p class=\"form-note hint\">" +
        esc(copy.gift_redeem_help || copy.redeem_help) +
        "</p></form>";
    }
    return (
      '<div class="account-card" data-block="quota"><div class="account-name">' +
      esc(copy.title) +
      tierBadge +
      "</div>" +
      balance +
      (location === 'mobile'
        ? statusLine + quotaDetails + redeemBlock + button(copy.logout, "logout", { small: true, quiet: true })
        : '<details class="account-actions account-drawer" data-persistent-disclosure="desktop-account"' + (!verified && email ? ' open' : '') +
          '><summary>账户与额度设置</summary>' + statusLine + quotaDetails + redeemBlock + button(copy.logout, "logout", { small: true, quiet: true }) + '</details>') +
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
      '</button><button type="button" class="btn btn-quiet' + (state.view === "mcp-hub" ? " active" : "") + '" data-action="open-mcp-hub"' + (state.view === "mcp-hub" ? ' aria-current="page"' : "") + '><span class="nav-icon" aria-hidden="true">✦</span>AI 接入 (MCP)</button>' +
      '</nav><div class="sidebar-spacer"></div>' +
      '<div class="account-card campus-account"><div class="account-name">教学网账号</div><p>' + (state.campus.configured ? '已保存' : '尚未连接') + '</p>' + button("连接设置", "campus-account-settings", { small: true, quiet: true }) + '</div>' +

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

      '</div><details class="mobile-account-menu" data-persistent-disclosure="mobile-account"><summary>账户与额度</summary><div class="mobile-account-content">' + button('AI 接入 (MCP)', 'open-mcp-hub', {small:true, quiet:true}) + button("连接设置", "campus-account-settings", { small: true, quiet: true }) + quotaCard("mobile") + updateCard() + '</div></details></header>' +
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

  function breadcrumb(trail, extraHtml) {
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
      (extraHtml || '') +
      "</div>"
    );
  }

  function formatTransfer(bytes) {
    if (!bytes) return "0 B";
    if (bytes < 1024) return Math.round(bytes) + " B";
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
    var lecture = (state.recordings.items || []).concat((state.campus.course && state.campus.course.lessons) || [])
      .find(function (item) { return item.id === review.recordingId; }) || {};
    var target = (lecture.transcript_review && lecture.transcript_review.target) || review.target || {};
    var candidateTerm = target.candidate_term || lecture.candidate_term || "RSA";
    review.status = "submitting";
    review.error = "";
    render();
    return requestJson(termReviewUrl(review.courseId, review.recordingId), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segment_index: review.target.segment_index,
        transcript_sha256: review.transcriptSha256, confirmed_spoken_term: candidateTerm, confirmed: true })
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

  function isNoteProgressUnit(unit) {
    return unit === "划分知识点" || unit === "整理知识点" || unit === "知识点分段" ||
      unit === "段" || unit === "章节" || unit === "笔记段" || unit === "整合笔记";
  }

  function taskUnitName(task) {
    var unit = task.progress_unit;
    if (unit === "bytes") return "未传输";
    if (unit === "segments") return "段录像";
    if (unit === "audio_extract") return "音频";
    if (unit === "audio_parts") return "段音频";
    if (unit === "划分知识点") return "个知识模块";
    if (unit === "整合笔记") return "步整合";
    if (isNoteProgressUnit(unit)) return "个知识点";
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
    var isNoteUnit = isNoteProgressUnit(task.progress_unit);
    // Lecture organization reports one overall percent; other kinds still
    // derive it from the raw sub-stage counts.
    var overallPercent = task.kind === "process" && typeof task.progress_percent === "number" &&
      task.progress_percent > 0;
    var percent = overallPercent
      ? (task.state === "done" ? 100 : Math.max(1, Math.min(99, Math.round(task.progress_percent))))
      : (total ? Math.min(100, Math.round(done / total * 100)) : 0);
    var detail = task.progress_unit === "bytes"
      ? (total ? formatTransfer(done) + " / " + formatTransfer(total) : formatTransfer(task.bytes_done))
      : task.progress_unit === "segments" ? done + " / " + total + " 段录像"
      : task.progress_unit === "audio_extract" ? "已处理 " + formatAudioPosition(done) +
        (total ? " / " + formatAudioPosition(total) : "") + " 音频"
      : task.progress_unit === "audio_parts" ? done + " / " + total + " 段音频"
      : task.progress_unit === "整合笔记" ? "正在整合课堂笔记"
      : isNoteUnit ? "已完成 " + done + " / " + total + " 个知识点"
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
    return '<div class="recording-progress"><progress class="recording-progress-bar" aria-label="' + esc(campusReadError(task.stage)) + '进度" ' +
      (overallPercent ? 'value="' + esc(percent) + '" max="100"'
        : total ? 'value="' + esc(done) + '" max="' + esc(total) + '"' : '') + '></progress>' +
      '<span>' + esc(total ? percent + "% · " + detail : detail) + '</span></div>';
  }

  function recordingTaskStatus() {
    var task = state.recordings.task;
    if (!task || task.state === "idle") return "";
    if (task.kind === 'sync' && task.state === 'done' && !task.error && !(task.warnings || []).length) return '';
    var activeCourseId = state.campus.courseId;
    if (state.recordings.taskCourseId && state.recordings.taskCourseId !== activeCourseId && task.kind === "process") return "";
    if (state.recordings.taskCourseId && state.recordings.taskCourseId !== activeCourseId &&
        task.kind === "sync" && state.view === "campus-course") return "";
    var activeStage = task.progress_unit === "audio_extract" ? "提取音轨" :
      task.progress_unit === "audio_parts" ? "上传音频并转写" : (task.stage || "转写并生成笔记");
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
      (task.state === "done" && /^\/api\//.test(task.result_url || '') ? (task.kind === "download"
        ? '<a class="btn btn-small btn-quiet" href="' + esc(task.result_url) + '" download>保存录像到电脑</a>'
        : button(task.kind === "summary" ? "打开课程总结" : task.kind === "sync" ? "查看更新结果" : "查看笔记", "recording-open-result", { small: true, quiet: true, attrs: { "data-url": task.result_url } })) : "") +
      warnings + failure +
      '</div>' + progress + '</div>';
  }

  function backgroundSyncStatus() {
    var task = state.recordings.background;
    if (!task || task.state === "idle") return "";
    if (task.state === 'done' && !task.error && !(task.warnings || []).length &&
        !(task.course_results || []).some(function (row) { return row.error || (row.state && row.state !== 'done'); })) return '';
    var labels = { pending: "等待更新", running: "正在更新", done: "完成", confirmed: "已核对", not_read: "尚未读取", unconfirmed: "尚未核对", failed: "失败", partial: "部分完成", cancelled: "已取消", interrupted: "中断待恢复", unknown: "状态未确认" };
    var list = task.course_results || [];
    var doneCount = list.filter(function (r) { return r.state === "done"; }).length;
    var failedCount = list.filter(function (r) { return r.state && r.state !== "done" && r.state !== "pending" && r.state !== "running"; }).length;
    var rows = list.slice().sort(function (a, b) {
      return Number(a.state === "done") - Number(b.state === "done");
    }).map(function (row) {
      var course = state.campus.courses.find(function (item) { return item.id === row.course_id; });
      return '<li data-sync-course="' + esc(row.course_id) + '" data-sync-state="' + esc(row.state) + '"><strong>' + esc(row.title || course && course.title || row.course_id) + '</strong> · <span class="sync-state-tag">' + esc(labels[row.state] || "状态未确认") + '</span> ' +
        '<small> · 教学网：' + esc(labels[row.metadata_state] || "状态未确认") + '</small>' +
        (row.summary_created ? '<small>本轮新建总结 ' + esc(row.summary_created) + ' 页</small>' : '') +
        (row.summary_existing ? '<small>复用已有总结 ' + esc(row.summary_existing) + ' 页</small>' : '') +
        (row.error ? '<p role="alert">' + esc(campusReadError(row.error)) + '</p>' : '') +
        (row.failure_id ? '<small>故障编号：' + esc(row.failure_id) + '</small>' : '') + '</li>';
    }).join('');
    return '<section class="panel background-sync-banner" data-background-sync data-background-state="' + esc(task.state) + '" role="status" aria-live="polite">' +
      '<div class="background-sync-bar"><div class="background-sync-main"><span class="background-sync-badge">' +
      (task.state === "done" ? "✓ 已更新" : task.state === "running" ? "正在更新" : "后台更新") + '</span>' +
      '<p class="background-sync-stage" data-background-stage>' + esc(campusReadError(task.stage || labels[task.state])) + '</p>' +
      (list.length > 0 && task.state !== "running" ? '<span class="background-sync-counts">已完成 ' + doneCount + ' / ' + list.length + ' 门' + (failedCount ? ' · ' + failedCount + ' 门未完成' : '') + '</span>' : '') +
      '</div><div class="background-sync-actions">' +
      (task.state === "running" ? button("取消后台更新", "recording-cancel-background", { small: true, quiet: true, disabled: state.recordings.backgroundPending }) : '') +
      (['failed', 'partial', 'cancelled', 'interrupted'].indexOf(task.state) >= 0 ? button("恢复未完成课程", "recording-resume-background", { small: true, disabled: state.recordings.backgroundPending }) : '') +
      '</div></div>' + (task.state === "running" ? recordingTaskProgress(task) : '') +
      (task.error ? '<p role="alert" class="background-sync-error">' + esc(campusReadError(task.error)) + '</p>' : '') +
      (list.length ? '<details class="background-sync-details" data-sync-details data-persistent-disclosure="background-sync-details"><summary>' +
        (failedCount ? '查看未完成与各课明细 (' + failedCount + ' 门待处理 / 共 ' + list.length + ' 门)' : '查看各课更新明细 (' + list.length + ' 门)') +
        '</summary><ul class="background-sync-list">' + rows + '</ul></details>' : '') + '</section>';
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
    var focusedLessonFilter = document.activeElement && document.activeElement.matches('[data-campus-lesson-filter]') ?
      { start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd,
        direction: document.activeElement.selectionDirection } : null;
    var focusedCampusSearch = document.activeElement && document.activeElement.matches('[data-campus-search]') ?
      { start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd,
        direction: document.activeElement.selectionDirection } : null;
    var focusedReadingSearch = document.activeElement && document.activeElement.matches('[data-reader-search], [data-campus-material-filter]') ?
      { key: document.activeElement.dataset.readerSearch, start: document.activeElement.selectionStart,
        end: document.activeElement.selectionEnd, direction: document.activeElement.selectionDirection } : null;
    var focusedMaterialSearch = document.activeElement && document.activeElement.matches('[data-lecture-material-search]') ?
      { key:document.activeElement.dataset.lectureMaterialSearch, start:document.activeElement.selectionStart,
        end:document.activeElement.selectionEnd, direction:document.activeElement.selectionDirection } : null;
    // Keep an in-progress code only in this account's current DOM. Never
    // carry it through logout/login or persist it to browser storage.
    var sameAccount = state.activated && renderedAccountEpoch === accountQuotaEpoch &&
      renderedAccountEmail === accountEmail();
    var authForm = document.getElementById("auth-form");
    var credentialDraft = (!state.activated && authForm && authForm.dataset.authMode === state.authMode)
      ? captureCredentialFields(["auth-email", "auth-password", "auth-password-confirm", "auth-invite"])
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
    if (state.view === "mcp-hub") screen = mcpHubScreen();
    else if (state.view === "campus-course") screen = campusCourseScreen();
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
    document.querySelectorAll('[data-batch-indeterminate]').forEach(function (element) { element.indeterminate = true; });
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
    if (focusedLessonFilter) {
      var lessonFilter = document.querySelector('[data-campus-lesson-filter]');
      if (lessonFilter) {
        lessonFilter.focus({ preventScroll: true });
        lessonFilter.setSelectionRange(focusedLessonFilter.start, focusedLessonFilter.end, focusedLessonFilter.direction);
      }
    }
    if (focusedCampusSearch) {
      var campusSearch = document.querySelector('[data-campus-search]');
      if (campusSearch) {
        campusSearch.focus({ preventScroll: true });
        campusSearch.setSelectionRange(focusedCampusSearch.start, focusedCampusSearch.end, focusedCampusSearch.direction);
      }
    }
    if (focusedReadingSearch) {
      var readingSearch = Array.from(document.querySelectorAll('[data-reader-search], [data-campus-material-filter]')).find(function (input) {
        return input.dataset.readerSearch === focusedReadingSearch.key;
      });
      if (readingSearch) {
        readingSearch.focus({ preventScroll: true });
        readingSearch.setSelectionRange(focusedReadingSearch.start, focusedReadingSearch.end, focusedReadingSearch.direction);
      }
    }
    if (focusedMaterialSearch) {
      var materialSearch = Array.from(document.querySelectorAll('[data-lecture-material-search]')).find(function (input) {
        return input.dataset.lectureMaterialSearch === focusedMaterialSearch.key;
      });
      if (materialSearch) {
        materialSearch.focus({preventScroll:true});
        materialSearch.setSelectionRange(focusedMaterialSearch.start, focusedMaterialSearch.end, focusedMaterialSearch.direction);
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
    var inviteInput = document.getElementById("auth-invite");
    var email = emailInput ? emailInput.value.trim() : "";
    var password = passwordInput ? passwordInput.value : "";
    var inviteCode = inviteInput ? inviteInput.value.trim() : "";
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
    if (state.authMode === "register" && password.length < 8) {
      state.notice = COPY.auth.password_too_short || "密码至少需要 8 位字符，请补充后再注册。";
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
        : state.authMode === "register" && inviteCode
          ? { email: email, password: password, invite_code: inviteCode }
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
      if (inviteInput) inviteInput.value = "";
      state.notice = "";
      state.noticeKind = "hint";
      var toast =
        state.authMode === "register"
          ? COPY.auth.registered_toast
          : COPY.auth.logged_in_toast;
      return loadQuota()
        .then(loadCampusCourses)
        .then(function () { return pollRecordingTask(true); })
        .then(function () {
          openCampusSetupIfNeeded();
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
    state.recordings.batch = { state: "idle" };
    state.batchOrganize = null;
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

  function exchangeQuota() {
    if (!canRedeem()) {
      showToast(COPY.account.redeem_gated);
      return Promise.resolve();
    }
    var epoch = accountQuotaEpoch;
    return requestJson("/api/platform/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hours: 1, target: "advanced" })
    }).then(function (result) {
      if (epoch !== accountQuotaEpoch) return;
      if (!result.ok) {
        showToast((result.body && result.body.detail) || COPY.account.exchange_failed || "AI 点不足，暂时无法兑换转写时长。");
        return;
      }
      return loadQuota().then(function () {
        if (epoch !== accountQuotaEpoch) return;
        render();
        showToast(COPY.account.exchanged_toast || "已用 10 AI 点兑换 1 小时转写时长。");
      });
    }).catch(function () {
      if (epoch === accountQuotaEpoch) showToast(COPY.account.exchange_failed || "AI 点不足，暂时无法兑换转写时长。");
    });
  }

  function copyInviteLink() {
    var quota = state.quota || {};
    var link = quota.invite_url || quota.invite_code || "";
    if (!link) return;
    copyStudyText(link, COPY.account.invite_copied_toast || "邀请链接已复制，发给同学绑定新教学网账号即可生效。");
  }

  function copyInviteCode() {
    var code = (state.quota || {}).invite_code || "";
    if (code) copyStudyText(code, "邀请码已复制，可发给同学注册或绑定邀请。");
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
      var toastMsg = result.body && result.body.invite_bound
        ? "邀请码已绑定！验证新教学网学号后双方各得 1 个月额度。"
        : COPY.account.redeemed_toast;
      return requestJson("/api/platform/quota").then(function (quota) {
        if (!currentAccount()) return;
        if (quota.ok && quota.body) {
          state.activated = Boolean(quota.body.active);
          state.quota = quota.body;
          render();
        }
        showToast(toastMsg);
      }, function () {
        if (currentAccount()) showToast(toastMsg);
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

  function pendingInviteRewardHint() {
    return state.quota && state.quota.invite_pending_campus_reward
      ? '<p class="invite-reward-hint" role="status">🎁 已绑定同学邀请：验证并保存新的北大教学网账号（学号）后，你与邀请人将立即各得 1 个月 Lite 普通转写额度（每周 25 小时自动滚存）。</p>' : '';
  }

  function recordingCredentialsForm() {
    var copy = COPY.course.recordings;
    var pending = Boolean(state.recordings.credentialsPending);
    var readonly = pending ? " readonly" : "";
    return '<form id="recording-credentials-form" class="recording-credentials" aria-busy="' + pending + '">' + pendingInviteRewardHint() +
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
      if (result.body.invite_reward_granted) {
        showToast(COPY.account.campus_reward_toast || COPY.course.recordings.verified);
        return refreshSettledQuota();
      } else {
        showToast(COPY.course.recordings.verified);
      }
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
      if (result.body.state === 'done') {
        showToast('课程目录已更新');
        return loadCampusCourses().then(function () {
          if (epoch !== accountQuotaEpoch) return;
          if (state.view === 'campus-course') return loadCampusCourse(state.campus.courseId);
          render();
        });
      }
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
    var lite = isLiteTier();
    var hasTier = Boolean((state.quota || {}).tier);
    var selected = lite ? "transcript" : (state.recordings.noteStrategyByKey[recordingId] || "topics");
    var topicsLabel = hasTier ? "按知识点图文整理（高级转写 · 笔记不扣 AI 点）" : "按知识点图文整理（Plus · 约 2–4 点）";
    var transcriptLabel = hasTier ? "按讲课时间线整理（普通转写 · 笔记不扣 AI 点）" : "按讲课时间线整理（Flash · 约 1–2 点）";
    return '<label class="recording-note-style">笔记方式<select data-recording-note-strategy="' + esc(recordingId) + '" aria-label="笔记方式"' + (disabled ? ' disabled' : '') + '>' +
      '<option value="topics"' + (selected === "topics" ? ' selected' : '') + (lite ? ' disabled' : '') + '>' + topicsLabel + '</option>' +
      '<option value="transcript"' + (selected === "transcript" ? ' selected' : '') + '>' + transcriptLabel + '</option></select>' + (lite ? '<small class="recording-note-tier-hint">当前为 Lite 等级，默认使用普通转写；升级 Pro / Max 可解锁按知识点图文整理。</small>' : '') + '</label>';
  }

  function openNotionPreview(url, onFailure) {
    return requestJson("/api/open-notion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url })
    }).then(function (result) {
      if (!result.ok || !result.body || !result.body.opened) throw new Error("browser open failed");
      if (["library", "course", "lecture"].indexOf(state.view) >= 0) openDashboard();
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

  function openImageLightbox(img) {
    var existing = document.querySelector('.note-lightbox');
    if (existing) existing.remove();
    var dialog = document.createElement('dialog');
    dialog.className = 'note-lightbox';
    dialog.setAttribute('aria-label', img.alt || '课堂图片预览');
    var picture = document.createElement('img');
    picture.src = img.src;
    picture.alt = img.alt || '';
    var caption = document.createElement('p');
    caption.textContent = img.alt || '';
    dialog.append(picture, caption);
    function close() {
      if (dialog.open) dialog.close();
      dialog.remove();
    }
    dialog.addEventListener('click', close);
    dialog.addEventListener('close', close);
    document.body.appendChild(dialog);
    try { dialog.showModal(); } catch (_) { close(); }
  }

  document.addEventListener("click", function (event) {
    var zoomImg = event.target.closest && event.target.closest('img[data-zoomable-image]');
    if (zoomImg) {
      openImageLightbox(zoomImg);
      return;
    }
    if (!event.target.closest('details, summary, a, button, input, select, textarea, label')) {
      var courseCard = event.target.closest('[data-course-card]');
      var lessonCard = event.target.closest('[data-lesson-card]');
      if (courseCard) { openCampusCourse(courseCard.dataset.courseCard); return; }
      if (lessonCard) { openCampusLesson(lessonCard.dataset.lessonCard); return; }
    }
    var target = event.target.closest("[data-action]");
    if (!target || target.disabled) return;
    var action = target.dataset.action;
    var mobileMenu = target.closest('.mobile-account-menu');
    if (mobileMenu && ["open-dashboard", "campus-account-settings", "open-campus-setup"].indexOf(action) >= 0) {
      mobileMenu.open = false;
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
    else if (action === "exchange-quota") exchangeQuota();
    else if (action === "copy-invite-link") copyInviteLink();
    else if (action === "copy-invite-code") copyInviteCode();
    else if (action === "request-update") requestUpdate(target.dataset.version);
    else if (action === "revoke-notion") disconnectNotion();
    else if (action === "connect-notion") connectNotion();
    else if (action === "membership-upgrade") { event.preventDefault(); guideMembership(target.dataset.tier, target.dataset.feature); }
    else if (action === "open-dashboard") openDashboard();
    else if (action === 'toggle-batch-organize') {
      if (state.batchOrganize && state.batchOrganize.open) { state.batchOrganize.open = false; render(); }
      else openBatchOrganize('');
    }
    else if (action === 'open-course-batch-organize') openBatchOrganize(target.dataset.course);
    else if (action === 'batch-preview-retry') openBatchOrganize(state.batchOrganize.courseId);
    else if (action === 'batch-select-pending' || action === 'batch-select-all' || action === 'batch-select-none') {
      selectBatchLessons(action.replace('batch-select-', '')); render();
    }
    else if (action === 'batch-organize-start') startBatchOrganize();
    else if (action === 'batch-organize-cancel') cancelBatchOrganize();
    else if (action === 'batch-open-note') {
      var noteEpoch = accountQuotaEpoch;
      openCampusCourse(target.dataset.course).then(function () {
        if (noteEpoch === accountQuotaEpoch && state.campus.courseId === target.dataset.course && state.campus.course)
          openCampusLesson(target.dataset.recording);
      });
    }
    else if (action === "campus-account-settings" || action === "open-campus-setup") {
      state.view = "campus-account";
      render();
      if (state.campus.configured && state.connection.connected && !state.campus.home) loadNotionParents();
      var settingsPanel = document.querySelector(state.campus.configured ? ".home-setup" : "#recording-credentials-form");
      if (settingsPanel) settingsPanel.scrollIntoView({ block: "start" });
      if (!state.campus.configured) {
        var username = document.getElementById("recording-username");
        if (username) username.focus();
      }
    }
    else if (action === "open-campus-course") openCampusCourse(target.dataset.course);
    else if (action === "campus-course-home") { state.campus.section = "home"; render(); }
    else if (action === "campus-course-settings") { state.campus.section = "settings"; render(); loadNotionParents(); }
    else if (action === "campus-open-center") {
      event.preventDefault();
      if (["assignments", "notices", "materials"].indexOf(target.dataset.center) < 0) return;
      if (isLiteTier() && target.dataset.center !== 'materials') return guideMembership('Pro / Max', target.dataset.center === 'assignments' ? '作业中心' : '通知中心');
      state.campus.section = target.dataset.center; render();
    }
    else if (action === "campus-open-lesson") { openCampusLesson(target.dataset.recording); }
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
    else if (action === "campus-apply-filename-rule") {
      state.campus.submission.form.archive_name = target.dataset.filenameTemplate;
      render();
      var archiveInput = document.querySelector('#assignment-prepare-form [name="archive_name"]');
      if (archiveInput) archiveInput.focus();
    }
    else if (action === "campus-submit-assignment") submitCampusAssignment();
    else if (action === "campus-assignment-status") loadAssignmentStatus(target.dataset.assignment);
    else if (action === "campus-load-receipts") loadCampusReceipts(state.campus.courseId);
    else if (action === "campus-recover-submission") recoverCampusSubmission(target.dataset.draft);
    else if (action === "campus-process") processCampusRecording(target.dataset.recording);
    else if (action === "campus-regenerate") processCampusRecording(target.dataset.recording, true);
    else if (action === 'open-mcp-hub') {
      var mcpEpoch = accountQuotaEpoch;
      state.view = 'mcp-hub'; mcpHub.error = ''; render();
      requestJson('/api/mcp/status').then(function (result) {
        if (mcpEpoch !== accountQuotaEpoch) return;
        if (!result.ok || !result.body) throw new Error('本地知识库暂时无法读取，请重试。');
        mcpHub.status = result.body; render();
      }).catch(function (error) {if (mcpEpoch !== accountQuotaEpoch) return; mcpHub.error = error.message; render();});
    }
    else if (action === 'select-mcp-host') {mcpHub.host = target.dataset.host; render();}
    else if (['copy-mcp-snippet', 'copy-mcp-prompt', 'copy-mcp-command'].indexOf(action) >= 0 && mcpHub.status) {
      var host = mcpHub.status.hosts.find(function (row) {return row.id === mcpHub.host;});
      copyStudyText(action === 'copy-mcp-prompt' ? host.sample_prompt : action === 'copy-mcp-command' ? host.cli_command : host.config_snippet, '已复制，可直接粘贴使用');
    }
    else if (action === 'campus-match-slides-keyframe' + 's') {
      var matchCourseId = state.campus.courseId, matchEpoch = accountQuotaEpoch;
      target.disabled = true;
      requestJson('/api/campus/courses/' + encodeURIComponent(matchCourseId) + '/recordings/' + encodeURIComponent(target.dataset.recording) + '/match-slides', {method:'POST'}).then(function (result) {
        if (matchEpoch !== accountQuotaEpoch || state.campus.courseId !== matchCourseId) return;
        if (!result.ok || !result.body) throw new Error(result.body && result.body.detail || '课件匹配失败，请重试。');
        state.campus.course = result.body;
        render();
        showToast(result.body.keyframe_matched_count ? '已根据视频关键帧匹配 ' + result.body.keyframe_matched_count + ' 份课件。' : '未在候选课件中找到与本讲画面吻合的投影页。');
      }).catch(function (error) { if (matchEpoch === accountQuotaEpoch) showToast(error.message); }).finally(function () {target.disabled = false;});
    }
    else if (action === 'campus-preview-course-summary') openStudyPreview('');
    else if (action === 'campus-preview-lecture-note') openStudyPreview(target.dataset.recording);
    else if (action === 'campus-toggle-preview') {readingPreviews[target.dataset.preview].open = false; render();}
    else if (action === 'campus-copy-markdown') {
      var preview = readingPreviews[target.dataset.preview];
      if (preview) copyStudyText(preview.markdown, preview.lecture ? '已复制笔记 Markdown，可直接粘贴或发送给 AI' : '已复制课程总结 Markdown');
    }
    else if (action === 'campus-download-markdown') {
      var downloadPreview = readingPreviews[target.dataset.preview];
      if (!downloadPreview || !downloadPreview.markdown) return;
      var courseTitle = (state.campus.course && state.campus.course.title || '课程').replace(/\([^()]*\)$/, '').trim();
      var filename = courseTitle + '-课程总结.md';
      if (downloadPreview.lecture) {
        var lessonId = target.dataset.preview.split(':').slice(1).join(':');
        var lesson = (state.campus.course.lessons || []).find(function (item) {return item.id === lessonId;});
        filename = courseTitle + '-' + (lesson && lesson.title || '课堂笔记') + '.md';
      }
      filename = filename.replace(/[/\\:*?"<>|\r\n]+/g, '-');
      var blob = new Blob([downloadPreview.markdown], {type:'text/markdown;charset=utf-8'});
      var downloadUrl = URL.createObjectURL(blob);
      var link = document.createElement('a');
      link.href = downloadUrl;
      link.download = filename;
      document.body.appendChild(link);
      try {link.click();}
      finally {
        link.remove();
        setTimeout(function () {URL.revokeObjectURL(downloadUrl);}, 0);
      }
      showToast('已下载 Markdown 文件');
    }
    else if (action === 'campus-copy-code') {
      var codeWrap = target.closest('.study-code-wrap');
      var codeEl = codeWrap && codeWrap.querySelector('code');
      if (codeEl) copyStudyText(codeEl.textContent, '已复制代码，可直接粘贴');
    }
    else if (action === "campus-course-summary") startCourseSummary(target.dataset.course);
    else if (action === "campus-summaries") startCourseSummary("");
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
    else if (action === 'lecture-toggle-material-picker') {
      lectureMaterialPicker.recording = lectureMaterialPicker.recording === target.dataset.recording ? '' : target.dataset.recording;
      lectureMaterialPicker.query = ''; lectureMaterialPicker.filter = 'recommended'; render();
    }
    else if (action === 'lecture-material-filter') { lectureMaterialPicker.filter = target.dataset.filter; render(); }
    else if (action === 'lecture-assign-material') changeLectureMaterial(target, false);
    else if (action === 'lecture-unassign-material') changeLectureMaterial(target, true);
    else if (action === 'lecture-remove-local-material') updateLectureMaterials('/api/campus/courses/' +
      encodeURIComponent(state.campus.courseId) + '/recordings/' + encodeURIComponent(target.dataset.recording) +
      '/local-materials/' + encodeURIComponent(target.dataset.localMaterial) + '/remove', {method:'POST'});
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
    else if (action === "toggle-local-recordings") {
      state.localRecordings.open = !state.localRecordings.open;
      if (state.localRecordings.open) loadLocalRecordings();
      else render();
    }
    else if (action === "local-storage-pick-dir") pickLocalStorageDirectory();
    else if (action === "local-storage-save-dir") {
      var dirInput = document.querySelector("[data-local-storage-dir-input]");
      updateLocalStorageSettings({ recordings_download_dir: dirInput ? dirInput.value.trim() : "" }, "已保存录像下载目录。");
    }
    else if (action === "local-storage-open-dir") openLocalStoragePath("");
    else if (action === "local-storage-reveal") openLocalStoragePath(target.dataset.recording || "");
    else if (action === "local-storage-delete-one") deleteOneLocalRecording(target.dataset.recording || "");
    else if (action === "local-storage-clear-all") clearAllLocalRecordings();
    else if (action === "recording-open-result" && target.dataset.url) {
      var localResult = target.dataset.url.match(/^\/api\/campus\/courses\/([^/]+)\/(?:recordings\/([^/]+)\/note-preview|summary-preview)$/);
      if (localResult) {
        var resultCourse = decodeURIComponent(localResult[1]);
        var resultRecording = localResult[2] ? decodeURIComponent(localResult[2]) : '';
        var resultEpoch = accountQuotaEpoch;
        state.view = 'campus-course';
        state.campus.courseId = resultCourse;
        state.campus.section = resultRecording ? 'lecture' : 'home';
        state.campus.lessonId = resultRecording;
        loadCampusCourse(resultCourse).then(function () {
          if (resultEpoch !== accountQuotaEpoch || state.campus.courseId !== resultCourse) return;
          var key = resultCourse + ':' + (resultRecording || 'summary');
          if (readingPreviews[key]) readingPreviews[key].open = false;
          openStudyPreview(resultRecording, true);
        });
      } else openNotionPreview(target.dataset.url);
    }
  });

  document.addEventListener("keydown", function (event) {
    var upgrade = event.target.closest('[data-action="membership-upgrade"]');
    if (upgrade && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); upgrade.click(); return; }
    if (event.target && event.target.matches && event.target.matches('[data-reader-search]')) {
      var key = event.target.dataset.readerSearch;
      var preview = readingPreviews[key];
      if (event.key === 'Escape' && preview && preview.query) {
        event.preventDefault();
        preview.query = ''; preview.hitIndex = 0;
        render();
        return;
      }
      if (event.key === 'Enter' && preview && preview.query && preview.query.trim()) {
        event.preventDefault();
        preview.hitIndex = (preview.hitIndex || 0) + (event.shiftKey ? -1 : 1);
        render();
        var search = Array.from(document.querySelectorAll('[data-reader-search]')).find(function (input) {
          return input.dataset.readerSearch === key;
        });
        if (search) {
          search.focus({ preventScroll: true });
          var activeMark = search.closest('.lecture-note-reader').querySelector('mark.study-search-hit-active');
          if (activeMark) activeMark.scrollIntoView({ block: 'center' });
        }
        return;
      }
    }
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey ||
        document.querySelector('dialog[open]') ||
        (event.target.closest && event.target.closest('input, textarea, select, [content' + 'editable]'))) return;
    if (state.view !== 'campus-course' || state.campus.section !== 'lecture' ||
        (event.key !== '[' && event.key !== ']')) return;
    var btn = document.querySelector('.breadcrumb-pager [data-lesson-nav="' + (event.key === '[' ? 'prev' : 'next') + '"]');
    if (btn && !btn.disabled) {
      event.preventDefault();
      openCampusLesson(btn.dataset.recording);
    }
  });

  document.addEventListener("input", function (event) {
    if (event.target.matches('[data-lecture-material-search]')) {
      lectureMaterialPicker.query = event.target.value; render(); return;
    }
    if (event.target.matches('[data-reader-search]')) {
      var previewKey = event.target.dataset.readerSearch;
      if (readingPreviews[previewKey]) {
        readingPreviews[previewKey].query = event.target.value;
        readingPreviews[previewKey].hitIndex = 0;
        render();
      }
      return;
    }
    if (event.target.matches('[data-campus-material-filter]')) {
      state.campus.materialQuery = event.target.value;
      render();
      return;
    }
    if (event.target.matches('[data-campus-lesson-filter]')) {
      state.campus.dateQuery = event.target.value;
      render();
      return;
    }
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
    if (event.target.matches('[data-local-storage-save-toggle]')) {
      var enabled = Boolean(event.target.checked);
      updateLocalStorageSettings(
        { save_recordings_locally: enabled },
        enabled ? "已开启：整理笔记后在本地保留原始录像。" : "已关闭：整理笔记后自动删除本地临时录像。"
      );
      return;
    }
    if (event.target.matches('[data-lecture-local-upload]')) {
      var files = Array.from(event.target.files || []);
      if (!files.length) return;
      if (files.some(function (file) { return !/\.(pdf|pptx|docx|md|txt)$/i.test(file.name); })) {
        showToast('仅支持 .pdf、.pptx、.docx、.md、.txt 课件或笔记。');
        event.target.value = '';
        return;
      }
      var form = new FormData();
      files.forEach(function (file) { form.append('files', file); });
      updateLectureMaterials('/api/campus/courses/' + encodeURIComponent(state.campus.courseId) + '/recordings/' +
        encodeURIComponent(event.target.dataset.lectureLocalUpload) + '/local-materials', {method:'POST', body:form});
      return;
    }
    if (event.target.matches('[data-batch-note-style]')) {
      state.batchOrganize.notesStrategy = event.target.value; render(); return;
    }
    if (event.target.matches('[data-action="batch-toggle-course"], [data-action="batch-toggle-lesson"]')) {
      var control = event.target;
      var batchCourse = batchCourses().find(function (course) { return course.course_id === control.dataset.course; });
      if (!batchCourse) return;
      batchCourse.lessons.forEach(function (lesson) {
        if (lesson.eligible && (!control.dataset.recording || lesson.id === control.dataset.recording))
          state.batchOrganize.selected[batchLessonKey(batchCourse.course_id, lesson.id)] = control.checked;
      });
      render(); return;
    }
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
    .then(refreshAccount)
    .then(render)
    .catch(function () {
      // an unreachable loopback API must not leave a blank surface
      render();
    });
})();
