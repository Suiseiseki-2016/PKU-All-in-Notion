# E2E：只读验收评论重整页面

不得创建、编辑、移动或删除任何 Notion 内容。

## 输入

target 必须包含 `operation=verify_reorganize`、`page_url`、`course_folder`、
`course_id`、`course_name` 和 `recording_dir`。

## 断言

1. 根据 `course_folder` 的权威映射解析目标课程 page ID。
2. fetch `page_url`，确认父级就是目标课程页。
3. 标题包含 `[E2E]`。
4. 正文同时包含 `E2E_REORGANIZE_BASELINE` 和
   `E2E_REORGANIZED_MARKER`。
5. 页面 URL 未变化，目标讲次和课程没有变化。
6. 页面没有出现其他课程名或未选讲次。

输出：

```text
E2E_VERIFY=success url=<Notion URL>
```

任何断言失败输出：

```text
E2E_VERIFY=blocked reason=<具体原因>
```
