# E2E：准备小测答案 fixture

这是一个只针对 `[E2E]` 小测页的 setup runbook，必须使用官方 Notion MCP。

## 输入

target 必须包含 `operation=e2e_quiz_answer_setup`、`page_url`、`course_folder`、
`course_id`、`course_name` 和至少一个 `recording_dir`。缺参时 blocked。

## 操作

1. fetch `page_url`，确认它是目标课程页的直接子页，标题带 `[E2E]`。
2. 如果正文已有 `E2E_ANSWER_FIXTURE`，不要重复追加，直接报告同一个 URL。
3. 否则在小测页末尾追加以下用户答案区。答案故意设置为两题正确、两题错误，
   供 grader 验证逐题评分和错题登记：

   ```text
   ## E2E_ANSWER_FIXTURE
   1. answer-1
   2. deliberately-wrong-answer-2
   3. answer-3
   4. deliberately-wrong-answer-4
   ```

4. 不修改题目、标准答案或其他页面。

输出：

```text
E2E_SETUP=success url=<Notion URL>
```

失败输出：

```text
E2E_SETUP=blocked reason=<具体原因>
```
