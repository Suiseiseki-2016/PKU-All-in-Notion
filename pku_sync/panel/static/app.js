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
    authMode: "login",
    update: null,
    organize: { open: false, working: false, courseId: "", lectureId: "", result: null, blocked: "", output: "" },
    exerciseLaunch: null,
    answerLaunches: {},
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
    courseTab: "materials",
    courseExercises: [],
    courseExercisesLoading: false,
    courseExercisesError: "",
    campus: { courses: [], course: null, courseId: "", loading: false, error: "", configured: false, home: null, parents: [], parentId: "", catalogAutoStarted: false, parentError: "", query: "", category: "all", dateQuery: "", demoConnection: false, submission: { selectedId: "", draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } } },
    recordings: { items: [], campusCourses: [], sourceId: "", configured: false, editingCredentials: false, loading: false, error: "", task: { state: "idle" }, taskCourseId: "", lectureByKey: {}, directOss: false },
    termReview: { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" },
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

  var accountRefreshPending = false;
  var redeemPending = false;

  function loadQuota() {
    return requestJson("/api/platform/quota").then(function (result) {
      state.activated = Boolean(result.ok && result.body && result.body.active);
      state.quota = result.ok && result.body ? result.body : null;
    });
  }

  function refreshAccount() {
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

  function applyAnswerLaunches(directory) {
    if (!directory || !Array.isArray(directory.exercises)) return;
    directory.exercises.forEach(function (row) {
      if (!state.answerLaunches[row.id]) return;
      if (row.status === "organized") {
        row.status = "pending-answer";
        row.status_label = COPY.directory.exercises.status_labels["pending-answer"];
      }
    });
  }

  function reconcileExerciseRow(exerciseId) {
    if (!exerciseId) return;
    state.answerLaunches[exerciseId] = true;
    applyAnswerLaunches(state.directory);
  }

  function loadDirectory(options) {
    var opts = options || {};
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
        applyAnswerLaunches(state.directory);
      } else if (opts.preserveOnError && state.directory) {
        state.directoryError = "";
        applyAnswerLaunches(state.directory);
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

  function loadCourseExercises() {
    var courseId = state.courseId;
    if (!courseId) return Promise.resolve();
    state.courseExercisesLoading = true;
    state.courseExercisesError = "";
    render();
    return requestJson("/api/courses/" + encodeURIComponent(courseId) + "/exercises").then(function (result) {
      if (state.courseId !== courseId) return;
      state.courseExercisesLoading = false;
      state.courseExercises = result.ok && result.body ? result.body.items || [] : [];
      state.courseExercisesError = result.ok ? "" : (result.body && result.body.detail) || "练习暂时无法读取。";
      render();
    }).catch(function () {
      state.courseExercisesLoading = false;
      state.courseExercisesError = "练习暂时无法读取。";
      render();
    });
  }

  function loadCourseRecordings() {
    var courseId = state.courseId;
    if (!courseId) return Promise.resolve();
    state.recordings.loading = true;
    state.recordings.error = "";
    render();
    return requestJson("/api/courses/" + encodeURIComponent(courseId) + "/recordings").then(function (result) {
      if (state.courseId !== courseId) return;
      state.recordings.loading = false;
      if (result.ok && result.body) {
        state.recordings.items = result.body.items || [];
        state.recordings.campusCourses = result.body.campus_courses || [];
        state.recordings.sourceId = result.body.selected_campus_course_id || "";
        state.recordings.configured = Boolean(result.body.campus_configured);
      } else {
        state.recordings.error = (result.body && result.body.detail) || "录像索引暂时无法读取。";
      }
      render();
    }).catch(function () {
      state.recordings.loading = false;
      state.recordings.error = "录像索引暂时无法读取。";
      render();
    });
  }

  function pollRecordingTask() {
    if (state.recordings.task.state !== "running") return;
    return requestJson("/api/recordings/task").then(function (result) {
      if (!result.ok || !result.body) return;
      var previous = state.recordings.task;
      state.recordings.task = result.body;
      if (previous.state === "running" && result.body.state !== "running") {
        return loadCampusCourses().then(function () {
          if (state.view === "campus-course") return loadCampusCourse(state.campus.courseId);
          if (state.view === "course") return loadCourseRecordings();
          render();
        });
      }
      if (previous.stage !== result.body.stage || previous.state !== result.body.state ||
          previous.stage_index !== result.body.stage_index || previous.current_item !== result.body.current_item ||
          previous.progress_done !== result.body.progress_done || previous.progress_total !== result.body.progress_total ||
          previous.progress_unit !== result.body.progress_unit || previous.bytes_done !== result.body.bytes_done) {
        if (previous.state !== "running" || result.body.state !== "running" || !refreshRunningTaskStatus()) render();
      }
    }).catch(function () {});
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
    return requestJson("/api/campus/courses").then(function (result) {
      if (!result.ok || !result.body) return;
      state.campus.courses = result.body.courses || [];
      state.campus.configured = Boolean(result.body.campus_configured);
      state.campus.demoConnection = Boolean(result.body.demo_connection);
      render();
    }).catch(function () {});
  }

  function loadCampusCourse(courseId) {
    state.campus.loading = true;
    state.campus.error = "";
    render();
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId)).then(function (result) {
      if (state.campus.courseId !== courseId) return;
      state.campus.loading = false;
      state.campus.course = result.ok ? result.body : null;
      state.campus.error = result.ok ? "" : (result.body && result.body.detail) || "教学网课程暂时无法读取。";
      render();
    }).catch(function () {
      state.campus.loading = false;
      state.campus.error = "教学网课程暂时无法读取。";
      render();
    });
  }

  function loadNotionParents() {
    if (state.campus.demoConnection) return Promise.resolve();
    if (!state.connection.connected) { state.campus.parentError = "请先连接 Notion。"; render(); return Promise.resolve(); }
    state.campus.parentError = "";
    return requestJson("/api/campus/notion/parents").then(function (result) {
      if (!result.ok || !result.body) { state.campus.parentError = (result.body && result.body.detail) || "Notion 页面暂时无法读取。"; render(); return; }
      state.campus.parents = result.body.pages || [];
      state.campus.home = result.body.home && result.body.home.id ? result.body.home : null;
      var destinations = state.campus.parents.filter(function (page) { return !state.campus.home || page.id !== state.campus.home.id; });
      if (state.campus.home && destinations.some(function (page) { return page.id === state.campus.home.parent_id; }))
        state.campus.parentId = state.campus.home.parent_id;
      else if (!destinations.some(function (page) { return page.id === state.campus.parentId; }))
        state.campus.parentId = destinations.length ? destinations[0].id : "";
      render();
      if (state.campus.home && state.campus.courses.length && !state.campus.catalogAutoStarted &&
          state.recordings.task.state !== "running") {
        state.campus.catalogAutoStarted = true;
        return requestJson("/api/campus/notion/sync", { method: "POST" }).then(function (started) {
          if (started.ok && started.body) {
            state.recordings.task = started.body;
            render();
          } else if (started.status !== 409) {
            showToast((started.body && started.body.detail) || "Notion 课程页暂时无法创建。");
          }
        });
      }
    }).catch(function () { state.campus.parentError = "Notion 页面暂时无法读取。"; render(); });
  }

  function campusSetupGuide() {
    var steps = [
      ["产品账号", state.activated && accountVerified()],
      ["教学网账号", state.campus.configured],
      ["Notion 学习主页", Boolean(state.campus.home)],
      ["课程目录", state.campus.courses.length > 0]
    ];
    var next = steps.find(function (step) { return !step[1]; });
    return '<section class="panel campus-setup"><h2>开始使用</h2><ul class="setup-steps">' +
      steps.map(function (step) { return '<li><span class="setup-mark" aria-hidden="true">' + (step[1] ? '✓' : '○') + '</span><span>' + esc(step[0]) + '</span></li>'; }).join("") +
      '</ul><p>' + (state.campus.demoConnection ? '演示模式：此处不会创建真实页面。' :
        next ? '下一步：' + esc(next[0]) : '已就绪，可以从课程中选择录像按需整理。') + '</p></section>';
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
        '</span><div class="course-content"><span class="course-meta">教学网课程' + classLabel + '</span><h3>' + esc(course.title) +
        '</h3><p>' + esc(course.recordings) + ' 节录像 · ' + esc(course.materials) + ' 份资料 · ' + esc(course.assignments || 0) + ' 项教学网正式作业索引 · ' + esc(course.handbook_reviews || 0) + ' 份课程手册待核对 · ' + esc(course.announcement_tasks || 0) + ' 项公告待办 · ' + esc(course.information || 0) + ' 项课程信息 · ' + esc(course.announcements || 0) + ' 则通知</p><p>' +
        esc(course.code ? '课程号 ' + (course.code.length > 14 ? course.code.slice(0, 8) + '…' + course.code.slice(-4) : course.code) : '课程号未提供') + (course.term ? ' · ' + esc(course.term) : '') +
        (course.id ? ' · 教学班 …' + esc(course.id.slice(-6)) : '') + '</p><p>目录更新：' +
        esc(course.synced_at ? course.synced_at.slice(0, 10) : "尚未更新") + '</p></div>' +
        button("查看课程", "open-campus-course", { small: true, attrs: { "data-course": course.id } }) + '</article>';
    }).join("");
    var credentials = state.campus.configured && !state.recordings.editingCredentials
      ? '<p>教学网账号已保存。' + button("更换账号", "recording-edit-credentials", { small: true, quiet: true }) + '</p>'
      : '<div class="recording-credentials"><label>教学网账号<input id="recording-username" autocomplete="username" placeholder="学号"></label>' +
        '<label>教学网密码<input id="recording-password" type="password" autocomplete="current-password" placeholder="密码"></label>' +
        button("保存账号", "recording-save-credentials", { small: true }) +
        (state.campus.configured ? button("取消", "recording-cancel-credentials", { small: true, quiet: true }) : "") + '</div>';
    var empty = state.campus.courses.length ? (visible.length ? '<div class="course-grid">' + items + '</div>' : '<div class="empty-materials">没有找到匹配的课程。</div>') :
      '<div class="empty-materials">还没有同步课程。保存教学网账号后读取目录；若已选定 Notion 学习主页，也会建立空白课程与讲次页，不下载录像或附件。</div>';
    return '<div class="topline"><div><h1 class="page-title">我的课程</h1><p class="page-desc">以教学网为准，先看录像讲次，再看课件。</p></div></div>' +
      campusSetupGuide() + campusHomeControls() +
      '<section class="panel"><div class="section-label"><h2>教学网课程</h2><p>目录更新读取标题、日期和附件名称；已选定主页时同步建立 Notion 页面。</p></div>' +
      credentials + button("更新课程目录", "recording-sync", { small: true, primary: true, disabled: !state.campus.configured || state.recordings.task.state === "running" }) +
      recordingTaskStatus() +
       (state.campus.courses.some(function (course) { return !course.synced_at; }) ?
         '<p class="form-note hint">部分课程目录尚未更新；显示 0 项资料可能只是旧索引。请更新课程目录。</p>' : '') +
       (state.campus.courses.length ? '<label class="campus-search-label">查找课程<input class="text-input" type="search" data-campus-search placeholder="输入课程名或课程号" value="' + esc(state.campus.query) + '"></label>' : '') + (state.recordings.error ? '<p role="alert">' + esc(state.recordings.error) + '</p>' : '') +
      empty + '</section>';
  }

  function campusHomeControls() {
    if (state.campus.demoConnection) return '';
    if (!state.connection.connected) return '<section class="panel"><h2>保存到 Notion</h2><p>整理前连接 Notion，应用会在你选定的父页面下创建学习主页。</p>' +
      button("连接 Notion", "connect-notion", { small: true, primary: true }) + '</section>';
    if (state.campus.home) {
      var destinations = state.campus.parents.filter(function (page) { return page.id !== state.campus.home.id; });
      var moveOptions = destinations.map(function (page) {
        return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
      }).join("");
      return '<section class="panel campus-home-location"><h2>Notion 学习主页已就绪</h2><p>整理结果会按课程和讲次保存在这个主页中。</p>' +
        button("打开主页", "campus-open-home", { small: true, attrs: { "data-url": state.campus.home.url || "" } }) +
        button("生成全部课程总结", "campus-summaries", {
          small: true, disabled: state.recordings.task.state === "running",
          attrs: { "data-url": state.campus.home.url || "" }
        }) +
        (moveOptions ? '<label>主页所在位置<select data-campus-parent aria-label="学习主页所在位置">' + moveOptions + '</select></label>' +
          button("移动现有主页", "campus-move-home", { small: true, disabled: state.campus.parentId === state.campus.home.parent_id }) :
          button("读取可用页面", "campus-load-parents", { small: true })) +
        (state.campus.parentError ? '<p role="alert" class="form-note">' + esc(state.campus.parentError) + '</p>' : '') + '</section>';
    }
    var options = state.campus.parents.map(function (page) {
      return '<option value="' + esc(page.id) + '"' + (page.id === state.campus.parentId ? ' selected' : '') + '>' + esc(page.title) + '</option>';
    }).join("");
    return '<section class="panel"><h2>创建 Notion 学习主页</h2><p>选择一个已授权的父页面，应用会在其中创建空白学习主页。创建目录不会下载录像。</p>' +
      (options ? '<select data-campus-parent aria-label="Notion 父页面">' + options + '</select>' + button("创建学习主页", "campus-create-home", { small: true, primary: true }) :
        button("读取可用页面", "campus-load-parents", { small: true })) + (state.campus.parentError ? '<p role="alert" class="form-note">' + esc(state.campus.parentError) + '</p>' : '') + '</section>';
  }

  function directOssConsent() {
    return '<label class="recording-oss-consent"><input type="checkbox" data-recording-direct-oss' +
      (state.recordings.directOss ? ' checked' : '') +
      '> 本次整理将音频经 15 分钟短时授权直传阿里云 OSS，以加快云端转写；教学网账号留在本机，临时音频处理后删除。</label>';
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

  function campusOfficialSections(course) {
    var updated = course.synced_at_label || "尚未更新";
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
    var notices = '<section class="panel" id="course-notices"><h2>通知</h2>' +
      (noticeWarning ? '<p class="form-note hint">' + esc(noticeStatus) + '</p>' : '') +
      (noticeItems ? '<ul class="official-list">' + noticeItems + '</ul>' :
        (noticeWarning ? '' : '<p>' + esc(noticeStatus) + '</p>')) + '</section>';
    var assignmentItems = (course.assignments || []).map(function (item) {
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
      return '<li><strong>' + esc(item.title) + '</strong><small>截止：' + esc(due) +
        ' · 来源：教学网' + (item.source === "calendar" ? "日历" : "作业") +
        ' · 更新：' + esc(updated) + '</small><p>' + esc(item.instructions || "要求未提供，请打开教学网原页面核对。") +
        '</p>' + ((item.files || []).length ? '<p>附件：' + esc(item.files.join("、")) + '</p>' : '') +
        (sourceLinks ? '<p>老师原页面链接：' + sourceLinks + '</p>' : '') +
        (source ? '<a href="' + esc(source) + '" target="_blank" rel="noopener noreferrer">查看教学网原要求</a>' :
          '<small>请到教学网课程中核对原要求</small>') +
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
      var rarHint = /\.rar\b/i.test(selectedItem.instructions || "") ?
        '<p class="form-note hint">老师要求中出现 .rar；这里生成的是 ZIP。请核对老师是否接受 ZIP，需要 RAR 时在本机按原要求制作。</p>' : '';
      preparation = '<div class="assignment-preparation"><h3>准备作业文件 · ' + esc(selectedItem.title) +
        '</h3><p>按老师要求选择文件形式。ZIP 模式会把 Notion 正文保存为 Markdown 文本“答案.md”，不会自动生成 PDF 或 Word。若老师要求文档，请先从 Notion 导出后用单文件模式上传。在线作答或随堂考勤请使用教学网原入口。草稿只保存在本机，准备阶段不会提交。</p>' + rarHint +
        (draft ? '<div role="status"><p>本地草稿：' + esc(draft.archive_name) + '</p><ul>' +
          draft.files.map(function (file) { return '<li>' + esc(file.name) + ' · ' + esc(formatTransfer(file.bytes)) + '</li>'; }).join("") +
          '</ul>' + (draft.warnings || []).map(function (warning) { return '<p class="form-note hint">' + esc(warning) + '</p>'; }).join("") +
          '<a href="' + esc(draft.download_url) + '" download>下载并检查' + (draft.submission_mode === "single_file" ? '文件' : '压缩包') + '</a>' +
          '<label class="assignment-check"><input type="checkbox" id="assignment-reviewed"> 我已对照老师要求核对提交目标、文件名、文件内容和截止时间</label>' +
          button("确认提交到教学网", "campus-submit-assignment", { primary: true, small: true,
            disabled: draft.state === "submitted" }) + (draft.state === "submitted" ? '<p>已发送提交请求，请在教学网查看回执。</p>' : '') +
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
    var assignments = '<section class="panel" id="course-assignments"><h2>作业</h2>' +
      (assignmentWarning ? '<p class="form-note hint">作业目录尚未完整读取；请到教学网核对正式要求和截止时间。</p>' : '') +
      (assignmentItems ? '<h3>教学网正式作业</h3><ul class="official-list">' + assignmentItems + '</ul>' :
        '<p>教学网正式作业索引当前为 0 项；课程手册、通知或课堂要求仍可能包含课业。</p>') +
      (announcementTaskItems ? '<h3>老师公告中的交付要求</h3><ul class="official-list">' + announcementTaskItems + '</ul>' : '') +
      (handbookItems ? '<h3>课程手册中的待核对要求</h3><ul class="official-list">' + handbookItems + '</ul>' : '') +
      preparation + '</section>';
    return notices + assignments;
  }

  function selectCampusAssignment(contentId) {
    state.campus.submission = { selectedId: contentId, draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } };
    render();
    var panel = document.querySelector(".assignment-preparation");
    if (panel) panel.scrollIntoView({ block: "nearest" });
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

  function submitCampusAssignment() {
    var draft = state.campus.submission.draft;
    var checked = document.querySelector("#assignment-reviewed");
    if (!draft || !checked || !checked.checked) {
      state.campus.submission.error = "请先下载检查草稿文件，并勾选确认。";
      render();
      return;
    }
    if (!window.confirm("确认向教学网正式提交「" + draft.assignment_title + "」？提交后可能无法撤回。")) return;
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
          state.campus.submission.error = "";
          showToast(result.body.message);
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
    return requestJson("/api/campus/courses/" + encodeURIComponent(courseId) + "/existing-notion", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: input.value.trim() })
    }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "关联未保存。");
      if (state.campus.courseId !== courseId) return;
      state.campus.course = result.body;
      render();
      if (state.campus.home && !state.campus.demoConnection) {
        requestJson("/api/campus/notion/sync", { method: "POST" }).then(function (started) {
          if (!started.ok) showToast((started.body && started.body.detail) || "Notion 导航暂时未更新。");
        });
      }
    }).catch(function () { showToast("关联未保存。"); });
  }

  function campusCourseScreen() {
    var course = state.campus.course;
    var header = breadcrumb([{ label: "我的课程", action: "open-dashboard" }, { label: course ? course.title : "教学网课程" }]);
    // Keep the current course visible while a background refresh replaces it.
    // Replacing a long page with the short loading panel makes the browser
    // clamp the scroll container to zero, which feels like a random jump.
    if (state.campus.loading && !course) return header + '<section class="panel" role="status">正在读取课程目录…</section>';
    if (!course) return header + '<section class="panel" role="alert">' + esc(state.campus.error || "课程尚未同步。") + '</section>';
    var lessons = course.lessons.filter(function (lesson) {
      return !state.campus.dateQuery || (lesson.date || "").includes(state.campus.dateQuery.trim());
    }).map(function (lesson, index) {
      var materials = lesson.materials.filter(function (item) {
        return state.campus.category === "all" || item.category === state.campus.category;
      }).map(function (item) {
        return campusMaterial(item, course.id);
      }).join("");
      var details = lesson.duration_seconds ? ' · 约 ' + Math.ceil(lesson.duration_seconds / 60) +
        ' 分钟，预计转写用量约 ' + Math.ceil(lesson.duration_seconds / 60) + ' 分钟' : ' · 时长未提供，可先读取时长';
      if (lesson.video_size) details += ' · 本地文件 ' + formatTransfer(lesson.video_size);
      var publishExisting = Boolean(lesson.publish_ready && !lesson.video_available);
      var blocked = lesson.unavailable_reason || (state.recordings.task.state === "running" ? "请等待当前任务完成。" : state.campus.demoConnection ? "演示模式不会实际整理，请在正式程序中连接 Notion。" : !state.connection.connected ? "请先连接 Notion。" : !state.campus.home ? "请先创建 Notion 学习主页。" : "");
      var review = state.termReview.courseId === course.id && state.termReview.recordingId === lesson.id ? termReviewPanel(lesson) : "";
      return '<article class="recording-row"><div><span class="course-meta">第 ' + esc(index + 1) + ' 节 · 录像优先</span><h3>' + esc(lesson.title) +
        '</h3><p>' + esc(lesson.date || "日期未记录") + esc(details) + (lesson.unavailable_reason ? ' · ' + esc(lesson.unavailable_reason) : '') +
        '</p>' + (!lesson.duration_seconds && state.campus.configured ? button('读取时长', 'campus-estimate-duration', { small: true, quiet: true, attrs: { 'data-recording': lesson.id } }) : '') +
        (materials ? '<ul>' + materials + '</ul>' : '<p>暂无明确对应的课件</p>') + '</div><div class="recording-row-actions">' +
        (lesson.notion_lecture_url
          ? '<span class="recording-published">已写入 Notion</span>' + button("打开最新笔记", "campus-open-lecture", {
              small: true, primary: true, attrs: { "data-url": lesson.notion_note_url || lesson.notion_lecture_url }
            }) + button("重新整理笔记", "campus-regenerate", {
              small: true, quiet: true, disabled: Boolean(blocked),
              attrs: { "data-recording": lesson.id }
            }) + (blocked ? '<small class="action-reason">' + esc(blocked) + '</small>' : '')
          : button(publishExisting ? "发布已有笔记" : "整理这节录像", "campus-process", { small: true, primary: true,
              disabled: Boolean(blocked),
              attrs: { "data-recording": lesson.id } }) + (blocked ? '<small class="action-reason">' + esc(blocked) + '</small>' : '')) +
        (lesson.video_available
          ? '<a class="btn btn-small btn-quiet" href="/api/recordings/' + encodeURIComponent(lesson.id) + '/video" download>保存录像到电脑</a>'
          : button("仅下载录像", "recording-download", { small: true, quiet: true,
              disabled: !state.campus.configured || state.recordings.task.state === "running" || Boolean(lesson.unavailable_reason),
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
      (pendingItems.length ? pendingItems.map(function (item) { return campusMaterial(item, course.id, course.lessons); }).join("") : '<li>当前分类暂无内容。</li>') + '</ul></section>';
    var summary = course.summary && course.summary.url ? course.summary : null;
    var summaryControl = '<div class="course-summary-control">' +
      (summary ? button("打开课程总结", "campus-open-summary", {
        small: true, attrs: { "data-url": summary.url }
      }) + '<small>' + esc(summary.title || "课程总结快照") + '</small>' : "") +
      button(summary ? "重新生成课程总结" : "生成课程总结", "campus-course-summary", {
        small: true, quiet: Boolean(summary),
        disabled: state.recordings.task.state === "running" || state.campus.demoConnection || !state.campus.home,
        attrs: { "data-course": course.id }
      }) + '</div>';
    return header + '<section class="course-hero"><div><span class="course-kicker">教学网课程</span><h1>' + esc(course.title) +
      '</h1><p>' + (hasLessons ? '先查看录像与课件目录。只有确认整理某节录像后，才会下载、转写并写入笔记。' : '这门课程暂时没有录像；可先查看和下载教学网资料。') + '</p><small>课程号 ' + esc(course.code || '未提供') + ' · 教学班 ' + esc(course.id) + '</small></div>' + summaryControl + '</section>' +
      campusOfficialSections(course) + campusHomeControls() + campusExistingNotionControl(course) + (hasLessons ? '<section class="panel"><div class="section-label"><h2>课程讲次</h2><p>以录像为主线，课件只在匹配明确时附到讲次。</p></div>' : '<section class="panel"><h2>按类型查找</h2>') +
      '<div class="campus-filters">' + (hasLessons ? '<label>按日期查找<input type="search" data-campus-date placeholder="例如 2026-09" value="' + esc(state.campus.dateQuery) + '"></label>' : '') +
      '<label>内容类型<select data-campus-category><option value="all">全部</option><option value="material"' + (state.campus.category === "material" ? " selected" : "") + '>资料</option>' +
      '<option value="assignment"' + (state.campus.category === "assignment" ? " selected" : "") + '>作业或测验</option><option value="information"' +
      (state.campus.category === "information" ? " selected" : "") + '>课程信息</option></select></label></div>' +
      (hasLessons ? directOssConsent() + recordingTaskStatus() + lessons : '') + '</section>' + pending;
  }

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
    state.campus.courseId = courseId;
    state.campus.course = null;
    state.termReview = { courseId: "", recordingId: "", status: "idle", target: null, transcriptSha256: "", videoAvailable: false, confirmed: false, error: "" };
    state.campus.submission = { selectedId: "", draft: null, error: "", form: { submission_mode: "zip", notion_url: "", archive_name: "", code_filename: "", attachments: [] } };
    loadCampusCourse(courseId);
    if (state.connection.connected && !state.campus.demoConnection) loadNotionParents();
  }

  function createCampusHome() {
    if (!state.campus.parentId) return showToast("请先选择 Notion 父页面。");
    return requestJson("/api/campus/notion/home", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_page_id: state.campus.parentId }) }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "学习主页没有创建成功。");
      state.campus.home = result.body;
      state.campus.catalogAutoStarted = true;
      if (result.body.task) state.recordings.task = result.body.task;
      render();
    });
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

  function moveCampusHome() {
    if (!state.campus.home || !state.campus.parentId) return showToast("请先选择新的 Notion 父页面。");
    if (state.campus.parentId === state.campus.home.parent_id) return;
    if (!window.confirm("将现有学习主页及其中的课程内容一起移动到所选页面？")) return;
    return requestJson("/api/campus/notion/home/move", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_page_id: state.campus.parentId }) }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "学习主页没有移动成功。");
      state.campus.home = result.body;
      render();
      showToast("学习主页已移动，原有内容保留。");
    });
  }

  function downloadRecording(recordingId) {
    if (!window.confirm("确认只下载这节录像到本机？不会转写，也不会消耗转写或 AI 额度。")) return;
    return requestJson("/api/recordings/" + encodeURIComponent(recordingId) + "/download", { method: "POST" }).then(function (result) {
      if (!result.ok || !result.body) return showToast((result.body && result.body.detail) || "录像下载没有开始。");
      state.recordings.task = result.body;
      state.recordings.taskCourseId = state.view === "campus-course" ? state.campus.courseId : state.courseId;
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

  function startCourseSummary(courseId) {
    if (state.recordings.task.state === "running") {
      return showToast("请等待当前任务完成后再生成课程总结。");
    }
    var course = state.campus.course;
    var scope = courseId && course && course.id === courseId
      ? "「" + course.title + "」" : "全部已同步课程";
    if (!window.confirm("生成" + scope + "的《课程总结》快照？" +
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

  function processCampusRecording(recordingId, regenerate) {
    var course = state.campus.course;
    var lesson = course && course.lessons.find(function (item) { return item.id === recordingId; });
    if (!lesson || !state.campus.home) return;
    var directOss = state.recordings.directOss;
    var hasMatchedSlides = (lesson.materials || []).some(function (item) {
      return item.category === "material" && (item.files || []).some(function (name) {
        return /\.(pdf|pptx|docx)$/i.test(name);
      });
    });
    var sourceWarning = hasMatchedSlides ? "" :
      "当前没有明确关联的课件，笔记可能主要依据自动转写；建议先在下方把对应课件关联到这节录像，再整理。";
    var reuseExisting = Boolean(lesson.publish_ready && !lesson.video_available && !regenerate);
    var confirmation = regenerate
      ? "确认重新整理「" + lesson.title + "」？将使用本机已有转写和明确匹配的课件，重新生成笔记并发布一个新版 Notion 页面。旧笔记和旧页面会保留；AI 笔记会再次消耗额度。完成后请对照来源校对术语、数字和老师要求。"
      : reuseExisting
        ? "确认发布「" + lesson.title + "」的已有笔记？将复用本机转写、笔记和课堂画面，不重新下载录像，也不重复消耗转写或 AI 笔记额度。"
        : "确认整理「" + lesson.title + "」？将下载录像、转写并将笔记写入对应 Notion 讲次。若有明确对应的教学网课件，会提取所选文字片段并与转写一同发送到 PKU All in Notion AI 笔记服务。转写与 AI 笔记会消耗额度；完成后请对照来源校对术语和数字。";
    if (!window.confirm(sourceWarning + confirmation +
        (directOss && !regenerate && !reuseExisting ? "本次音频会经短时授权直传阿里云 OSS。" : ""))) return;
    return requestJson("/api/campus/courses/" + encodeURIComponent(course.id) + "/recordings/" + encodeURIComponent(recordingId) + "/process",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ direct_oss: reuseExisting ? false : directOss, regenerate: Boolean(regenerate), reuse_existing: reuseExisting }) }).then(function (result) {
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
      '<form id="auth-form"><label class="field-label" for="auth-email">' +
      esc(copy.email_label) +
      '</label><input id="auth-email" type="email" class="text-input" autocomplete="username" placeholder="' +
      esc(copy.email_placeholder) +
      '" required>' +
      passwordField +
      confirmField +
      '<div class="auth-actions">' +
      button(mode.submit, "auth-submit", { primary: true }) +
      "</div>" +
      (note
        ? '<p id="auth-help" class="' + noteClass + '">' + esc(note) + "</p>"
        : "") +
      "</form>" +
      switches +
      "</div></section></main>"
    );
  }

  /* -- onboarding (legacy activate copy retained for panel_copy pins) ---- */

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
    // Retained for prototype parity / copy pins; the live gate uses authScreen.
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
      (state.connection.error ? '<p class="form-note" role="alert">' + esc(state.connection.error) + "</p>" : "") +
      connectionControls(true) +
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
      statusLine +
      balance +
      redeemBlock +
      button(copy.logout, "logout", { small: true, quiet: true }) +
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
      '</button></nav><div class="sidebar-spacer"></div>' +
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
      '</div><details class="mobile-account-menu"><summary>账户与额度</summary><div class="mobile-account-content">' + connectionCard() + quotaCard("mobile") + updateCard() + '</div></details></header>' +
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
      (state.connection.error ? '<p class="form-note" role="alert">' + esc(state.connection.error) + "</p>" : "") +
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
    var lectures = selectedCourse ? (selectedCourse.lectures || []).filter(function (item) { return item.page_state === "ready"; }) : [];
    var balance = state.quota && state.quota.llm_points_remaining;
    var insufficient = typeof balance === "number" && balance < 5;
    var status = "";
    if (state.organize.working) {
      status = '<div class="organize-status" aria-live="polite" aria-busy="true"><span class="spinner" aria-hidden="true"></span><strong>' + esc(copy.working) + '</strong></div>';
    } else if (state.organize.blocked) {
      status = '<div class="organize-status error" role="alert" aria-live="polite"><strong>' + esc(copy.blocked) + '</strong><p>' + esc(state.organize.blocked) + '</p>' + (state.organize.output ? '<pre class="organize-output">' + esc(state.organize.output) + '</pre>' : '') + button(copy.retry, "organize-retry", { small: true, primary: true }) + '</div>';
    } else if (state.organize.result) {
      status = '<div class="organize-status success" aria-live="polite"><strong>' + esc(copy.completed) + '</strong><span class="cost-pill">' + esc(template(copy.settlement, { points: state.organize.result.points_charged })) + '</span></div>';
    }
    if (!state.organize.open) {
      return '<section class="exercise-toolbar" aria-labelledby="organize-title"><div><h2 id="organize-title">' + esc(copy.title) + '</h2><p>' + esc(copy.body) + '</p></div><div class="toolbar-actions">' + button(copy.action, "organize-open", { primary: true, icon: "quiz" }) + '<span class="cost-pill">' + esc(copy.estimate) + '</span></div>' + status + '</section>';
    }
    var lectureOptions = '<option value="">' + esc(copy.lecture_label) + '</option>' + lectures.map(function (lecture) { return '<option value="' + esc(lecture.id) + '"' + (lecture.id === state.organize.lectureId ? ' selected' : '') + '>' + esc(lecture.title) + '</option>'; }).join("");
    return '<section class="exercise-toolbar organize-confirm" aria-labelledby="organize-scope-title"><div><h2 id="organize-scope-title">' + esc(copy.scope_label) + '</h2><p>' + esc(copy.body) + '</p><div class="scope-fields"><label>' + esc(copy.course_label) + '<strong>' + esc(selectedCourse ? selectedCourse.title : "") + '</strong></label><label>' + esc(copy.lecture_label) + '<select data-organize-field="lecture">' + lectureOptions + '</select></label></div></div><div class="toolbar-actions"><span class="cost-pill">' + esc(copy.estimate) + '</span>' + (insufficient ? '<p class="insufficient" role="alert">' + esc(copy.insufficient) + '</p>' : '') + button(copy.confirm, "organize-confirm", { primary: true, disabled: insufficient || !state.organize.courseId || !state.organize.lectureId || state.organize.working }) + button(copy.cancel, "organize-cancel", { quiet: true, disabled: state.organize.working }) + '</div>' + status + '</section>';
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
      var settlement = "";
      if (typeof g.pointsCharged === "number") {
        settlement = '<span class="cost-pill">' + esc(template(copy.settlement, { points: g.pointsCharged })) + '</span>';
        if (typeof g.pointsRemaining === "number") settlement += '<span>剩余 AI 点：' + esc(g.pointsRemaining) + '</span>';
      }
      return '<section class="exercise-toolbar grading-card error" role="alert" aria-live="polite"><strong>' + esc(copy.failed) + '</strong><span>' + esc(g.blocked) + '</span>' + settlement + button(copy.retry, "grading-retry", { small: true, primary: true, attrs: { "data-exercise": g.exerciseId } }) + button(copy.back, "grading-back", { small: true, quiet: true }) + '</section>';
    }
    if (g.status === "completed" && g.result) {
      var r = g.result;
      var settlement = typeof r.points_charged === "number"
        ? template(copy.settlement, { points: r.points_charged })
        : COPY.directory.exercises.settlement_unknown;
      return '<section class="exercise-toolbar grading-summary" aria-live="polite"><strong>' + esc(copy.completed) + '</strong><h2>' + esc(r.title) + '</h2><p>\u5f97\u5206\uff1a' + esc(r.score) + '</p><p>\u6279\u6539\u65f6\u95f4\uff1a' + esc(r.graded_at) + '</p><span class="cost-pill">' + esc(settlement) + '</span><div class="toolbar-actions">' + button(copy.result, "grading-result", { primary: true, attrs: { "data-url": r.result_page_url } }) + button(copy.back, "grading-back", { quiet: true }) + '</div></section>';
    }
    return "";
  }

  function exerciseDirectorySection(rows) {
    rows = rows || [];
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
    if (state.campus.courses.length || !state.directory || !state.directory.courses || !state.directory.courses.length) return campusDashboardScreen();
    var fallback = directoryBody();
    var body = fallback
      ? fallback
      : statsSection() + courseGrid() + activitySection();
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

  function courseTabs() {
    return '<div class="course-tabs" role="tablist" aria-label="课程内容">' +
      ["materials", "recordings", "exercises"].map(function (tab) {
        return '<button type="button" role="tab" data-action="course-tab" data-tab="' + tab +
          '" aria-selected="' + (state.courseTab === tab ? "true" : "false") +
          '" class="' + (state.courseTab === tab ? "active" : "") + '">' +
          esc(COPY.course.tabs[tab]) + '</button>';
      }).join("") + '</div>';
  }

  function suggestedLecture(item, course) {
    var number = /第\s*(\d+)\s*[讲次]/.exec(item.title || "");
    if (!number) return "";
    var matches = (course.lectures || []).filter(function (lecture) {
      return lecture.page_state === "ready" && Number(lecture.number) === Number(number[1]);
    });
    return matches.length === 1 ? matches[0].id : "";
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
    var activeCourseId = state.view === "campus-course" ? state.campus.courseId : state.courseId;
    if (state.recordings.taskCourseId && state.recordings.taskCourseId !== activeCourseId && task.kind === "process") return "";
    var activeStage = task.progress_unit === "audio_extract" ? "提取音轨" :
      task.progress_unit === "audio_parts" ? "上传音频并转写" : task.stage;
    var stepPrefix = task.stage_total
      ? "第 " + Math.max(1, Number(task.stage_index) || 1) + "/" + task.stage_total + " 步 · " : "";
    var message = task.state === "running" ? stepPrefix + "正在" + activeStage + "…" :
      task.state === "done" || task.state === "cancelled" ? task.stage : task.error || "处理未完成，请稍后重试。";
    var progress = task.state === "running" ? recordingTaskProgress(task) : "";
    var warnings = (task.warnings || []).length
      ? '<small class="recording-warning">' + esc((task.warnings || []).slice(0, 2).join("；")) +
        ((task.warnings || []).length > 2 ? "…" : "") + '</small>' : "";
    var failure = task.state === "failed" && task.failure_id
      ? '<small class="recording-warning">故障编号：' + esc(task.failure_id) + ' · 再次失败时请附上此编号</small>'
      : "";
    return '<div class="recording-task" role="status" aria-live="polite"><div class="recording-task-text">' + esc(message) +
      (task.state === "running" && task.kind === "sync" ? button("取消目录更新", "recording-cancel-sync", { small: true, quiet: true }) : "") +
      (task.state === "done" && task.lecture_url ? button("打开这节课", "recording-open-result", { small: true, attrs: { "data-url": task.lecture_url } }) : "") +
      (task.state === "done" && task.result_url ? (task.kind === "download"
        ? '<a class="btn btn-small btn-quiet" href="' + esc(task.result_url) + '" download>保存录像到电脑</a>'
        : button(task.kind === "summary" ? "打开课程总结" : "查看笔记", "recording-open-result", { small: true, quiet: true, attrs: { "data-url": task.result_url } })) : "") +
      warnings + failure +
      '</div>' + progress + '</div>';
  }

  function courseRecordingsScreen(course) {
    var copy = COPY.course.recordings;
    var records = state.recordings;
    var sourceOptions = '<option value="">' + esc(copy.source_missing) + '</option>' +
      records.campusCourses.map(function (item) {
        return '<option value="' + esc(item.id) + '"' + (item.id === records.sourceId ? ' selected' : '') + '>' + esc(item.title) + '</option>';
      }).join("");
    var credentials = records.configured && !records.editingCredentials ? '<span class="recording-connected">教学网账号已保存</span>' + button("更换账号", "recording-edit-credentials", { small: true, quiet: true }) :
      '<div class="recording-credentials"><input id="recording-username" autocomplete="username" aria-label="' + esc(copy.username) + '" placeholder="' + esc(copy.username) + '">' +
      '<input id="recording-password" type="password" autocomplete="current-password" aria-label="' + esc(copy.password) + '" placeholder="' + esc(copy.password) + '">' +
      button(copy.save, "recording-save-credentials", { small: true }) + '</div>';
    var controls = '<div class="recording-controls">' + credentials +
      button(copy.sync, "recording-sync", { small: true, disabled: records.task.state === "running" || !records.configured }) +
      (records.campusCourses.length ? '<label>' + esc(copy.source) + '<select data-recording-source aria-label="' + esc(copy.source) + '">' + sourceOptions + '</select></label>' : "") +
      '</div>';
    var rows = records.items.map(function (item) {
      var lectures = (course.lectures || []).filter(function (lecture) { return lecture.page_state === "ready"; });
      if (!Object.prototype.hasOwnProperty.call(records.lectureByKey, item.id)) {
        records.lectureByKey[item.id] = suggestedLecture(item, course);
      }
      var selected = records.lectureByKey[item.id];
      var options = '<option value="">' + esc(copy.choose_lecture) + '</option>' +
        lectures.map(function (lecture) {
          return '<option value="' + esc(lecture.id) + '"' + (lecture.id === selected ? ' selected' : '') + '>' + esc(lecture.title) + '</option>';
        }).join("");
      var status = item.unavailable_reason || copy.status[item.stage] || item.stage;
      return '<article class="recording-row" data-recording="' + esc(item.id) + '"><div><h3>' +
        esc(item.title) + '</h3><p>' + esc(item.date || "日期未记录") + ' · ' + esc(status) +
        '</p></div><div class="recording-row-actions"><select data-recording-lecture aria-label="' + esc(copy.choose_lecture) + '">' +
        options + '</select>' + button(copy.organize, "recording-process", { small: true, primary: true, disabled: records.task.state === "running" || Boolean(item.unavailable_reason), attrs: { "data-recording": item.id } }) +
        (item.video_available
          ? '<a class="btn btn-small btn-quiet" href="/api/recordings/' + encodeURIComponent(item.id) + '/video" download>保存录像到电脑</a>'
          : button("仅下载录像", "recording-download", { small: true, quiet: true,
              disabled: records.task.state === "running" || Boolean(item.unavailable_reason), attrs: { "data-recording": item.id } })) +
        '</div></article>';
    }).join("");
    var content = records.loading ? '<div class="empty-materials" role="status">正在读取录像目录…</div>' :
      records.error ? '<div class="empty-materials" role="alert">' + esc(records.error) + '</div>' :
      !records.sourceId ? '<div class="empty-materials">请选择这门课程对应的教学网课程；同步目录不会下载录像。</div>' :
      rows || '<div class="empty-materials">' + esc(copy.empty) + '</div>';
    return '<section class="panel course-recordings" aria-labelledby="course-recordings-title"><div class="section-label"><h2 id="course-recordings-title">' +
      esc(copy.title) + '</h2><p>' + esc(copy.intro) + '</p></div>' + controls +
      directOssConsent() + recordingTaskStatus() + content + '</section>';
  }

  function courseExercisesScreen() {
    var contents = state.courseExercisesLoading ? '<div class="empty-materials" role="status">正在读取本课程练习…</div>' :
      state.courseExercisesError ? '<div class="empty-materials" role="alert">' + esc(state.courseExercisesError) + '</div>' :
      exerciseDirectorySection(state.courseExercises);
    return organizeCard() + gradingCard() + contents;
  }

  function courseScreen() {
    var course = currentCourse();
    if (!course) return dashboardScreen();
    var copy = COPY.course;
    var content = state.courseTab === "recordings" ? courseRecordingsScreen(course) :
      state.courseTab === "exercises" ? courseExercisesScreen() :
      '<div class="course-layout"><section class="panel" aria-labelledby="lecture-list-title">' +
      '<div class="section-label" style="margin-top:0"><h2 id="lecture-list-title">' +
      esc(copy.lectures.title) + '</h2><p>' +
      esc(template(copy.lectures.note, { lectures: course.counts.lectures })) +
      '</p></div>' + lectureList(course) + '</section>' + materialOverview(course) + '</div>';
    return breadcrumb([
        { label: copy.breadcrumb_home, action: "open-dashboard" },
        { label: copy.breadcrumb_courses }
      ]) +
      '<section class="course-hero" aria-labelledby="course-title"><div>' +
      '<span class="course-kicker">' + esc(template(copy.kicker, { scope: course.scope })) +
      '</span><h1 id="course-title">' + esc(course.title) +
      '</h1><p>' + esc(copy.desc) +
      '</p></div><div class="hero-actions">' +
      button(copy.sync_action, "reload-directory", { primary: true, icon: "sync" }) +
      '</div></section>' + courseTabs() + content;
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

  function lectureLaunchDisabled(lecture) {
    // Legacy predicate retained in this comment for compatibility with the
    // static contract; the helper also covers a server missing_mapping reply.
    // disabled: lecture.page_state === "missing"
    var current = state.launch;
    return lecture.page_state === "missing" || (current && current.targetId === lecture.id && current.status === "missing");
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
          disabled: lectureLaunchDisabled(lecture),
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
        disabled: lectureLaunchDisabled(lecture),
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
    var main = document.querySelector(".main");
    var mainScroll = main ? main.scrollTop : 0;
    var windowScroll = window.scrollY || window.pageYOffset || 0;
    var activeField = document.activeElement && document.activeElement.closest &&
      document.activeElement.closest("#assignment-prepare-form") ? document.activeElement.name : "";
    var caret = activeField && typeof document.activeElement.selectionStart === "number" ?
      document.activeElement.selectionStart : null;
    if (!state.activated) {
      app.innerHTML = authScreen();
      return;
    }
    var screen;
    if (state.view === "campus-course") screen = campusCourseScreen();
    else if (state.view === "dashboard") screen = dashboardScreen();
    else if (directoryBody()) screen = dashboardScreen();
    else if (state.view === "course") screen = courseScreen();
    else if (state.view === "lecture") screen = lectureScreen();
    else screen = dashboardScreen();
    app.innerHTML = shell(screen);
    main = document.querySelector(".main");
    if (main) main.scrollTop = mainScroll;
    if (windowScroll) window.scrollTo(0, windowScroll);
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
    state.authMode = mode;
    state.notice = "";
    state.noticeKind = "hint";
    render();
    focusAuthEmail();
  }

  function submitAuth() {
    var emailInput = document.getElementById("auth-email");
    var passwordInput = document.getElementById("auth-password");
    var confirmInput = document.getElementById("auth-password-confirm");
    var email = emailInput ? emailInput.value.trim() : "";
    var password = passwordInput ? passwordInput.value : "";
    if (!email) {
      state.notice = COPY.auth.empty_email;
      state.noticeKind = "error";
      render();
      focusAuthEmail();
      return Promise.resolve();
    }
    if (state.authMode !== "forgot" && !password) {
      state.notice = COPY.auth.empty_password;
      state.noticeKind = "error";
      render();
      focusAuthEmail();
      return Promise.resolve();
    }
    if (state.authMode === "register" && password !== (confirmInput ? confirmInput.value : "")) {
      state.notice = COPY.auth.password_mismatch;
      state.noticeKind = "error";
      var help = document.getElementById("auth-help");
      if (!help) {
        help = document.createElement("p");
        help.id = "auth-help";
        document.getElementById("auth-form").appendChild(help);
      }
      help.className = "form-note";
      help.textContent = state.notice;
      if (confirmInput) confirmInput.focus();
      return Promise.resolve();
    }
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
        render();
        focusAuthEmail();
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
      state.notice = "";
      state.noticeKind = "hint";
      var toast =
        state.authMode === "register"
          ? COPY.auth.registered_toast
          : COPY.auth.logged_in_toast;
      return loadQuota()
        .then(loadCampusCourses)
        .then(loadConnection)
        .then(function () {
          if (state.connection.connected) return loadDirectory();
          return null;
        })
        .then(function () {
          render();
          showToast(toast);
        });
    });
  }

  function logoutAccount() {
    return requestJson("/api/auth/logout", { method: "POST" }).then(function () {
      state.activated = false;
      state.quota = null;
      state.directory = null;
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
    redeemPending = true;
    var submit = form && form.querySelector('[data-action="redeem"]');
    if (submit) submit.disabled = true;
    return requestJson("/api/platform/redeem", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code })
    }).then(function (result) {
      if (!result.ok) {
        showToast((result.body && result.body.detail) || COPY.account.redeem_failed);
        focusCode(form);
        return;
      }
      return requestJson("/api/platform/quota").then(function (quota) {
        if (quota.ok && quota.body) {
          state.activated = Boolean(quota.body.active);
          state.quota = quota.body;
          render();
        }
        showToast(COPY.account.redeemed_toast);
      }, function () {
        showToast(COPY.account.redeemed_toast);
      });
    }).catch(function () {
      showToast(COPY.account.redeem_failed);
      focusCode(form);
    }).finally(function () {
      redeemPending = false;
      if (form && !document.contains(form)) {
        render();
      } else {
        var currentSubmit = form && form.querySelector('[data-action="redeem"]');
        if (currentSubmit) currentSubmit.disabled = false;
      }
    });
  }

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
      // the sidebar quota card must show the freshly granted quota without a
      // reload: refetch it as part of the activation success path
      return loadQuota().then(function () {
        return loadDirectory();
      }).then(function () {
        render();
        showToast(COPY.onboarding.activated_toast);
      });
    });
  }

  /* -- navigation -------------------------------------------------------- */

  function openDashboard() {
    state.launch = null;
    state.exerciseLaunch = null;
    state.grading = { exerciseId: null, title: "", status: "idle", jobId: null, result: null, blocked: "", unanswered: [] };
    state.view = "dashboard";
    state.lectureId = null;
    state.materials = null;
    state.materialsError = "";
    render();
  }

  function openCourse(courseId) {
    state.launch = null;
    var changed = Boolean(courseId && courseId !== state.courseId);
    state.view = "course";
    state.courseId = courseId || state.courseId;
    if (changed) {
      state.courseTab = "materials";
      state.courseExercises = [];
      state.recordings.items = [];
      state.recordings.sourceId = "";
      state.recordings.lectureByKey = {};
      state.organize = { open: false, working: false, courseId: state.courseId, lectureId: "", result: null, blocked: "", output: "" };
    }
    state.overview = null;
    state.overviewError = "";
    render();
    return loadOverview();
  }

  function switchCourseTab(tab) {
    if (["materials", "recordings", "exercises"].indexOf(tab) < 0) return;
    state.courseTab = tab;
    state.organize.courseId = state.courseId;
    render();
    if (tab === "recordings") return loadCourseRecordings();
    if (tab === "exercises") return loadCourseExercises();
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

  function recordingError(message) {
    state.recordings.error = message || "操作没有完成，请稍后重试。";
    render();
  }

  function saveRecordingCredentials() {
    var user = document.getElementById("recording-username");
    var password = document.getElementById("recording-password");
    if (!user || !password || !user.value.trim() || !password.value) {
      recordingError("请填写教学网账号和密码。");
      return Promise.resolve();
    }
    return requestJson("/api/recordings/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: user.value.trim(), password: password.value })
    }).then(function (result) {
      if (!result.ok) return recordingError((result.body && result.body.detail) || "账号保存失败。");
      state.recordings.configured = true;
      state.campus.configured = true;
      state.recordings.editingCredentials = false;
      state.recordings.error = "";
      render();
    }).catch(function () { recordingError("账号保存失败。"); });
  }

  function syncRecordingIndex() {
    return requestJson("/api/recordings/sync", { method: "POST" }).then(function (result) {
      if (!result.ok) return recordingError((result.body && result.body.detail) || "录像目录同步失败。");
      state.recordings.task = result.body;
      state.recordings.taskCourseId = state.courseId;
      state.recordings.error = "";
      render();
    }).catch(function () { recordingError("录像目录同步失败。"); });
  }

  function cancelRecordingSync() {
    return requestJson("/api/recordings/cancel-sync", { method: "POST" }).then(function (result) {
      if (!result.ok) return showToast((result.body && result.body.detail) || "无法取消目录更新。");
      state.recordings.task = result.body;
      render();
    }).catch(function () { showToast("无法取消目录更新。"); });
  }

  function chooseRecordingSource(campusCourseId) {
    if (!campusCourseId) {
      state.recordings.sourceId = "";
      state.recordings.items = [];
      render();
      return Promise.resolve();
    }
    var courseId = state.courseId;
    return requestJson("/api/courses/" + encodeURIComponent(courseId) + "/recording-source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ campus_course_id: campusCourseId })
    }).then(function (result) {
      if (!result.ok || !result.body) return recordingError((result.body && result.body.detail) || "课程对应关系保存失败。");
      if (state.courseId !== courseId) return;
      state.recordings.sourceId = result.body.selected_campus_course_id;
      state.recordings.items = result.body.items || [];
      state.recordings.lectureByKey = {};
      state.recordings.error = "";
      render();
    }).catch(function () { recordingError("课程对应关系保存失败。"); });
  }

  function processRecording(recordingId) {
    var item = state.recordings.items.find(function (entry) { return entry.id === recordingId; });
    var course = currentCourse();
    var lectureId = state.recordings.lectureByKey[recordingId];
    var lecture = course && (course.lectures || []).find(function (entry) { return entry.id === lectureId && entry.page_state === "ready"; });
    if (!item || !lecture) {
      recordingError("请先选择要写入的讲次。");
      return Promise.resolve();
    }
    var directOss = state.recordings.directOss;
    if (!window.confirm("确认下载并转写「" + item.title + "」，生成笔记后发布到「" + lecture.title + "」？转写与 AI 笔记会消耗额度。" +
        (directOss ? "本次音频会经短时授权直传阿里云 OSS。" : ""))) return Promise.resolve();
    var courseId = state.courseId;
    return requestJson("/api/recordings/" + encodeURIComponent(recordingId) + "/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ course_id: courseId, lecture_id: lectureId, direct_oss: directOss })
    }).then(function (result) {
      if (!result.ok || !result.body) return recordingError((result.body && result.body.detail) || "录像整理没有开始。");
      state.recordings.task = result.body;
      state.recordings.directOss = false;
      state.recordings.taskCourseId = courseId;
      state.recordings.error = "";
      render();
    }).catch(function () { recordingError("录像整理没有开始。"); });
  }

  function organizeOpen() {
    state.organize.open = true;
    state.organize.blocked = "";
    state.organize.courseId = state.courseId;
    var course = state.directory.courses.find(function (item) { return item.id === state.organize.courseId; });
    if (course && !state.organize.lectureId) { var ready = course.lectures.filter(function (item) { return item.page_state === "ready"; }); if (ready.length) state.organize.lectureId = ready[0].id; }
    render();
  }

  function blockOrganize(reason, output) {
    state.organize.working = false;
    state.organize.blocked = reason || COPY.directory.exercises.organize.blocked;
    state.organize.output = output || "";
    render();
  }

  function finishOrganize(result, jobId, attempts) {
    var copy = COPY.directory.exercises.organize;
    var pollAttempts = attempts || 0;
    if (!result.ok || !result.body || result.body.status === "blocked") {
      blockOrganize(result.body && (result.body.reason || result.body.detail) || copy.blocked, result.body && result.body.output);
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
    state.organize.output = "";
    state.organize.result = result.body;
    if (state.quota && typeof result.body.points_remaining === "number") {
      state.quota.llm_points_remaining = result.body.points_remaining;
    }
    state.organize.open = false;
    return loadDirectory().then(function () { return loadCourseExercises(); }).then(render);
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
    state.organize.output = "";
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
        state.grading.pointsCharged = result.body && result.body.points_charged;
        state.grading.pointsRemaining = result.body && result.body.points_remaining;
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
          else { state.grading.status = next.body && next.body.status === "failed" ? "failed" : "blocked"; state.grading.blocked = next.body && next.body.reason || COPY.directory.exercises.grading.blocked; state.grading.pointsCharged = next.body && next.body.points_charged; state.grading.pointsRemaining = next.body && next.body.points_remaining; state.grading.unanswered = next.body && next.body.unanswered || []; }
          if (next.body && typeof next.body.points_remaining === "number" && state.quota) {
            state.quota.llm_points_remaining = next.body.points_remaining;
          }
          render();
        }).catch(function () { state.grading.status = "blocked"; state.grading.blocked = COPY.directory.exercises.grading.blocked; render(); });
      }
      pollGrade();
    }).catch(function () { state.grading.status = "blocked"; state.grading.blocked = COPY.directory.exercises.grading.blocked; render(); });
  }

  function openNotionPreview(url, onFailure) {
    return requestJson("/api/open-notion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url })
    }).then(function (result) {
      if (!result.ok || !result.body || !result.body.opened) throw new Error("browser open failed");
      openDashboard();
      showToast(COPY.launch.opened_external);
      return true;
    }).catch(function () {
      if (onFailure) onFailure();
      else showToast(COPY.launch.failed);
      return false;
    });
  }
  function launchExerciseFallback(targetId) {
    return requestJson("/api/exercises/" + encodeURIComponent(targetId) + "/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purpose: "answer" })
    }).then(function (result) {
      if (result.body && result.body.fallback && result.body.fallback.url) {
        return openNotionPreview(result.body.fallback.url);
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
          return openNotionPreview(result.body.url, function () {
            state.exerciseLaunch = { exerciseId: targetId, purpose: purpose, status: "failed" };
            render();
          });
        };
        if (purpose === "answer") {
          // The launch endpoint records the answer-start event. Re-read the
          // metadata directory before navigating so the current row changes
          // to pending-answer without a manual reload when the panel remains visible.
          state.answerLaunches[targetId] = true;
          reconcileExerciseRow(targetId);
          return loadDirectory({ preserveOnError: true }).then(function () {
            applyAnswerLaunches(state.directory);
            render();
            return navigate();
          }, function () {
            state.directoryLoading = false;
            applyAnswerLaunches(state.directory);
            render();
            return navigate();
          });
        }
        return navigate();
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
            return openNotionPreview(result.body.url, function () {
              state.launch = { kind: kind, targetId: targetId, status: "failed" };
              render();
            });
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
      return loadDirectory().then(loadNotionParents).then(function () {
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

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target || target.disabled) return;
    var action = target.dataset.action;
    if (action === "auth-submit") submitAuth();
    else if (action === "auth-mode-login") setAuthMode("login");
    else if (action === "auth-mode-register") setAuthMode("register");
    else if (action === "auth-mode-forgot") setAuthMode("forgot");
    else if (action === "logout") logoutAccount();
    else if (action === "resend-verification") resendVerification();
    else if (action === "refresh-verification") refreshAccount();
    else if (action === "redeem") redeem(target.closest("[data-redeem-form]"));
    else if (action === "activate") activate();
    else if (action === "request-update") requestUpdate(target.dataset.version);
    else if (action === "revoke-notion") disconnectNotion();
    else if (action === "connect-notion") connectNotion();
    else if (action === "reload-directory") syncNow();
    else if (action === "dismiss-sync-success") {
      state.syncCompleted = false;
      openDashboard();
    }
    else if (action === "open-dashboard") openDashboard();
    else if (action === "open-campus-course") openCampusCourse(target.dataset.course);
    else if (action === "campus-load-parents") loadNotionParents();
    else if (action === "campus-create-home") createCampusHome();
    else if (action === "campus-move-home") moveCampusHome();
    else if (action === "campus-open-home" && target.dataset.url) openNotionPreview(target.dataset.url);
    else if (action === "campus-save-existing-notion") saveExistingNotionLink();
    else if (action === "campus-prepare-assignment") selectCampusAssignment(target.dataset.assignment);
    else if (action === "campus-build-draft") prepareCampusAssignment();
    else if (action === "campus-submit-assignment") submitCampusAssignment();
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
    else if (action === "course-tab") switchCourseTab(target.dataset.tab);
    else if (action === "recording-edit-credentials") { state.recordings.editingCredentials = true; render(); }
    else if (action === "recording-cancel-credentials") { state.recordings.editingCredentials = false; render(); }
    else if (action === "recording-save-credentials") saveRecordingCredentials();
    else if (action === "recording-sync") syncRecordingIndex();
    else if (action === "recording-cancel-sync") cancelRecordingSync();
    else if (action === "recording-process") processRecording(target.dataset.recording);
    else if (action === "recording-download") downloadRecording(target.dataset.recording);
    else if (action === "recording-open-result" && target.dataset.url) openNotionPreview(target.dataset.url);
    else if (action === "organize-open") organizeOpen();
    else if (action === "organize-confirm") organizeConfirm();
    else if (action === "organize-cancel") { state.organize.open = false; state.organize.blocked = ""; state.organize.output = ""; render(); }
    else if (action === "organize-retry") { state.organize.blocked = ""; state.organize.output = ""; state.organize.open = true; render(); }
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
      if (target.dataset.url) openNotionPreview(target.dataset.url);
    } else if (action === "launch") {
      launch(target.dataset.kind, target.dataset.target);
    } else if (action === "retry-launch") {
      launch(target.dataset.kind, target.dataset.target);
    } else if (action === "launch-fallback") {
      if (target.dataset.url) openNotionPreview(target.dataset.url);
    }
  });

  document.addEventListener("click", function (event) {
    var view = event.target.closest("[data-material-view]");
    if (view) setMaterialView(view.dataset.materialView);
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
    if (event.target.matches("[data-recording-source]")) { chooseRecordingSource(event.target.value); return; }
    if (event.target.matches("[data-recording-lecture]")) {
      var row = event.target.closest("[data-recording]");
      if (row) state.recordings.lectureByKey[row.dataset.recording] = event.target.value;
      return;
    }
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

  document.addEventListener("loadedmetadata", function (event) {
    if (!event.target.matches || !event.target.matches("[data-term-review-video]")) return;
    var seek = Number(event.target.dataset.seek);
    if (Number.isFinite(seek)) event.target.currentTime = Math.min(seek, event.target.duration || seek);
  }, true);
  document.addEventListener("submit", function (event) {
    event.preventDefault();
    if (event.target.id === "auth-form") submitAuth();
    else if (event.target.matches("[data-redeem-form]")) redeem(event.target);
    else if (event.target.id === "activation-form") activate();
  });

  window.setInterval(refreshAccount, 8000);
  window.setInterval(pollRecordingTask, 1500);
  window.addEventListener("focus", refreshAccount);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshAccount();
  });

  loadUpdate().then(render).catch(function () {
    // A release-check failure never blocks or replaces the local panel.
  });
  loadQuota()
    .then(loadCampusCourses)
    .then(loadConnection)
    .then(refreshAccount)
    .then(function () {
      if (state.activated && state.connection.connected) return loadDirectory();
      return null;
    })
    .then(loadNotionParents)
    .then(render)
    .catch(function () {
      // an unreachable loopback API must not leave a blank surface
      render();
    });
})();
