# 作业提交运行手册（文本 / 附件作业）

把 Notion 里定稿的答案提交回教学网（Blackboard）。代码在 `pku_sync/submit.py` +
CLI `pku-sync submit` 组，重建自 2026-09-12 逆向会话（Notion《Blackboard 作业提交
自动化经验（2026-09-12）》；当时的一次性脚本 `scripts/_submit4.py` 已在清理中遗失，
本模块是其产品化形态，线上行为一致）。

适用范围：**纯文本答案的在线提交**（`submit text`）和**附件上传**
（`submit file`，docx/PDF/zip 等）。测验、讨论区不在内（见文末边界）。

## 安全红线

1. **没有用户明确确认，绝不带 `--yes`。** 默认就是干跑：取表单、核状态、预览答案，
   不发 POST。用户看过干跑输出、明确说“提交”之后，才允许加 `--yes` 重跑。
2. **已提交守卫**：作业页面呈“复查提交历史”状态时 `fetch_form` 抛
   `AlreadySubmitted`，CLI 直接停——已交过的作业不会被二次提交。
3. **逾期警告不阻断**（有的课允许补交），但必须把 ⚠️ 警告原样转告用户，由用户拍板。
4. 答案为空拒绝提交；`submit text` 的 `--file` 与 `--text` 二选一。
   `submit file` 的 `--file` 必须是已存在的普通文件。
5. 提交成功后必须把回执写回 Notion（第 6 步），让任务闭环留在 Notion 里。

## 使用步骤

1. **草稿**：在 Notion 学习任务数据库的作业行页面里写好答案（agent 可代起草，
   用户定稿）。
2. **定位**：`pku-sync submit list --course <课程名片段或 course_id>` 列出该课
   作业条目，拿到 `content_id`。课程名匹配到多门时会拒绝并列出候选，改用
   `course_id`（形如 `_101578_1`）精确指定。
3. **导出**：把答案正文导出为 UTF-8 文本文件（如 `/tmp/answer.md`）。
4. **干跑**（默认）：`pku-sync submit text --course <课> --content <id> --file /tmp/answer.md`。
   核对输出的作业标题、截止时间、答案字数与开头预览；逾期会有红色 ⚠️。
   附件作业改用 `pku-sync submit file --course <课> --content <id> --file 作业.pdf`，
   干跑会显示文件名、字节数和类型（第 3 步的文本导出可跳过）。
5. **提交**：用户明确确认后，同一命令加 `--yes`。
6. **回写 Notion**：学习任务行状态改为完成；在作业行页面附：提交时间、回执
   `destinationUrl`、答案字数。闭环完成后作业不再出现在“作业 · 未完成”视图。

## 线上行为备忘（逆向结论）

- GET `/webapps/assignment/uploadAssignment?content_id=…&course_id=…` 渲染提交
  表单；隐藏域含 AJAX nonce（`blackboard.platform.security.NonceUtil.nonce.ajax`）
  和 `studentSubmission.text_f` / `.text_w` 两个会话字段。该页同时是状态探针：
  已提交后表单消失、显示复查历史——防重复提交守卫就建在这上面。
- POST 同路径加 `?action=submit`：必须 `multipart/form-data`（代码用一个空 files
  项强制 multipart 编码），必须带 `X-Requested-With: XMLHttpRequest`。缺任意一个
  就是 500（“访问已拒绝” / “not MultipartHttpServletRequest”）。
- 成功返回 HTTP 200 + JSON `{"destinationUrl": …}`，destinationUrl 即提交回执。
- 附件上传走同一端点和同一 AJAX multipart 约定：先用 `action=newAttempt` 取表单，
  文件放在 `newFile_LocalFile0` 分段，另带 FilePicker 字段（见 `submit.py` 的
  `FILE_EXTRA_FIELDS`）和 `newFile_linkTitle`。附件提交常常返回空 body，
  所以回执是可选的——没有 JSON 不代表失败。

## 边界（本模块不做）

- 非文本型入口：测验（Test）、讨论区等是另一套端点。
- 成绩与教师反馈回读。

## 故障对照

| 报错 | 含义 | 处理 |
|---|---|---|
| 该作业已提交过（AlreadySubmitted） | 守卫命中，页面为复查历史状态 | 正常；去 Notion 核对登记状态即可 |
| 页面没有可解析的提交表单 | 作业未开放 / 非文本入口 / 无权限 | 浏览器手动确认入口形态 |
| 提交被拒（HTTP 500） | 服务端拒绝（会话过期等） | 重跑一次；仍 500 则浏览器手动提交并回报 |
| 响应不是 JSON / 缺少 destinationUrl | 文本提交返回了错误页；附件提交空 body 属正常 | 文本路径先浏览器确认是否实际已提交；附件路径按成功处理，再去页面核对 |
| 匹配到多门课程 | 课程名片段不唯一（如分两班的课） | 改用 `course_id` 精确指定 |
