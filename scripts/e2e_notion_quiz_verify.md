# E2E：只读验收 Notion 小测页面

这是一个只读验收 runbook。不得创建、编辑、移动或删除任何 Notion 页面。
运行时上下文和 `TUI operation target` 是唯一目标来源。

## 输入

target 必须包含：

- `operation=verify_quiz`
- `page_url`
- `course_folder`
- `course_id`
- `course_name`
- 一个或多个 `recording_dir`

缺少任何字段时输出 `E2E_VERIFY=blocked reason=<原因>` 并停止。

## 验收步骤

1. 通过官方 Notion MCP fetch `page_url`。
2. Read `scripts/lecture_batch_creator.md` §1 and resolve `course_folder` to its
   authoritative `course_page_id`.
3. fetch 页面父级，确认父级是 target 对应课程页，不是学习中心、工作区根
   或其他课程。
4. 确认页面标题包含 `[E2E]`、课程名和目标讲次范围。
5. 确认页面包含 3～5 道题，并且每道题都有对应来源讲次或本地来源路径。
6. 确认答案/评分标准位于明确的教师区，不是用户作答区。
7. 确认页面正文没有其他课程名、未选讲次或跨课程资料。

## 输出

严格输出：

```text
页面：<标题>
父级：<课程标题>（<课程 page id>）
范围：<recording_dir 列表>
题目：<数量>
来源：<数量>
教师区：present

E2E_VERIFY=success url=<Notion URL>
```

任何断言失败都必须输出：

```text
E2E_VERIFY=blocked reason=<具体断言>
```
