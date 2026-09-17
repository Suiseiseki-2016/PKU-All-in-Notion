# TUI：整理选中的课程录音

你由 TUI 的“整理录音”动作触发。运行时上下文和末尾的
`TUI operation target` 是唯一目标来源；其中每一组 `recording_dir` /
`recording_title` / `recorded_at` 都是一讲，按给出的全部讲次处理。

## 规则

- 必须使用官方 Notion MCP 工具读写 Notion；不要调用 REST、浏览器或 shell 上传。
- 逐讲读取 `data_root/<recording_dir>/notes.md`、`transcript.json`、
  `recording.json` 和 `keyframes/`。
- `notes.md` 和 `transcript.json` 不存在、为空或明显未完成时，报告缺口并停止。
- 按 `scripts/lecture_notes_writer.md` 的完整规范整理：保留考点、作业/DDL、
  老师口头任务、代码、待核点、3~5 道自测题和关键帧来源。
- 先搜索当前学期的 Class Notes hub 和对应课程页，再检查日期/节次是否已有同名讲次页。
- 已有同名页时不要创建第二页；改用 `tui_notion_reorganizer.md` 的重整动作。
- 新页创建成功后，更新课堂录像 hub 和学习中心的索引；索引已存在则跳过。
- 不删除任何页面或数据库行，不泄露 token、密码或完整音频内容。

## 输出

严格输出四段：输入检查 / Notion 页面结果 / 已落索引 / 待处理。
任何一步失败都要说明，不得假装成功。
最后一行必须二选一：实际写入成功时输出 `TUI_RESULT=success url=<Notion URL>`；未写入时输出 `TUI_RESULT=blocked reason=<原因>`。
