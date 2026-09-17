# TUI：读取评论并重整 Notion 讲次页

你由 TUI 的“重整评论”动作触发。运行时上下文和末尾的
`TUI operation target` 是唯一目标来源；每组 `recording_dir` /
`recording_title` / `recorded_at` 对应一个要重整的讲次页。

## 规则

- 逐讲读取目标课程的本地 `notes.md`、`transcript.json` 和关键帧，作为事实来源。
- 逐讲通过官方 Notion MCP 找到对应讲次页，读取正文、页面讨论和评论。
- 若 target 含 `operation=e2e_reorganize` 和 `page_url`，必须只使用这个页面；
  先 fetch 并确认它属于目标课程页，不能按“最近页面”猜测。
- 把用户的新评论、批注和待办当作编辑要求，但不能把评论中的猜测直接写成事实。
- 按 `scripts/lecture_notes_writer.md` 规范重新组织正文，保留已有 `✗` 错题标记、
  原页面 URL、标题和讲次号。
- 只允许原地替换正文；不得创建重复讲次页、归档页面或删除数据库行。
- 完成后在该页面添加一条简短评论，说明本次依据哪些批注完成了哪些整理；
  没有实际修改时不要添加“已完成”评论。
- `operation=e2e_reorganize` 时，必须把评论中的
  `E2E_REORGANIZED_MARKER` 原样写入目标页面正文，并保留
  `E2E_REORGANIZE_BASELINE` 和原页面 URL。
- `operation=e2e_reorganize` 重跑时，如果 marker 已存在且页面已经满足要求，
  不得重复追加正文或评论；这是幂等 no-op，仍输出同一个页面 URL 和
  `TUI_RESULT=success`。
- 若找不到唯一目标页，停止并列出候选，不猜测。

## 输出

严格输出四段：目标定位 / 评论采纳 / 页面结果 / 待处理。
最后一行必须二选一：实际重整成功时输出 `TUI_RESULT=success url=<Notion URL>`；未写入时输出 `TUI_RESULT=blocked reason=<原因>`。
