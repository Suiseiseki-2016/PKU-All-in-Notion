# TUI：批改 Notion 小测答案

你由 TUI 的“批改小测”动作触发。运行时上下文和末尾的
`TUI operation target` 是唯一目标来源。

## 规则

- 通过官方 Notion MCP 找到选中课程最近的小测页，读取题目、标准答案和用户答案。
- 若 target 含 `operation=e2e_grade` 和 `page_url`，必须只使用这个页面；
  先确认父级是 target 对应课程页，不得按“最近页面”猜测。
- 只结合勾选讲次对应课程页和本地 `notes.md` / `transcript.json` 复核答案；
  不凭空补充知识。
- 对每题给出：得分、对错判断、简短理由、来源讲次，以及需要复习的知识点。
- 在小测页追加批改结果，不删除原答案；总分和批改时间写清楚。
- 将明显错题登记到学习中心的知识点/错题库，先查重再创建。
- 不修改用户原答案，不提交任何教学网作业，不把密码、token 或音频原文写入评论。
- `operation=e2e_grade` 时，批改结果必须包含唯一 marker
  `E2E_GRADE_RESULT`、四道逐题分数和两个错题 marker。重跑时若 marker
  已存在，不能重复追加，作为幂等 no-op 仍返回同一 URL 的 success。

## 输出

严格输出四段：批改对象 / 逐题结果 / 错题登记 / 待处理。
最后一行必须二选一：实际追加批改成功时输出 `TUI_RESULT=success url=<Notion URL>`；未写入时输出 `TUI_RESULT=blocked reason=<原因>`。
