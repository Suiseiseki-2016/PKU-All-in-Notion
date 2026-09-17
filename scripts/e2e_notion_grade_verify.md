# E2E：只读验收小测批改

不得创建、编辑、移动或删除任何 Notion 内容。

## 输入

target 必须包含 `operation=verify_grade`、`page_url`、`course_folder`、
`course_id`、`course_name` 和至少一个 `recording_dir`。

## 断言

1. 根据 `course_folder` 的权威映射解析课程 page ID，并确认页面父级正确。
2. 页面标题包含 `[E2E]`，正文包含 `E2E_ANSWER_FIXTURE`。
3. 页面包含 `E2E_GRADE_RESULT`、总分和四道逐题评分。
4. 页面包含两个明确的错题标记，并且没有删除原答案。
5. 重复批改不会产生第二个批改结果段。

输出：

```text
E2E_VERIFY=success url=<Notion URL>
```

否则输出：

```text
E2E_VERIFY=blocked reason=<具体原因>
```
