# E2E：准备评论重整测试页

这是一个只运行一次的 E2E setup runbook。必须使用官方 Notion MCP。

## 输入

target 必须包含 `operation=e2e_reorganize_setup`、`course_folder`、
`course_id`、`course_name` 和一个 `recording_dir`。缺参时 blocked。

## 操作

1. 读取 `scripts/lecture_batch_creator.md` §1 的权威课程映射，解析
   `course_folder` 对应的 `course_page_id`。
2. 在该课程页下创建一个标题包含 `[E2E]` 的子页面，标题中包含课程名和
   `recording_dir`。不能创建在学习中心、工作区根或其他课程下。
3. 页面正文必须包含：

   ```text
   E2E_REORGANIZE_BASELINE
   ```

   以及一段来自目标讲次本地 `notes.md` 的事实内容。
4. 在新页面添加一条评论：

   ```text
   E2E_REORGANIZE_REQUEST: add the exact marker E2E_REORGANIZED_MARKER to
   the page body and preserve the page URL and existing baseline.
   ```

5. 不修改其他页面。

## 输出

```text
E2E_SETUP=success url=<Notion URL>
```

失败输出：

```text
E2E_SETUP=blocked reason=<具体原因>
```
