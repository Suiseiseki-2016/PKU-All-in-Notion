# E2E:清理测试页面(已授权)

只删除 `TUI operation target` 中列出的五个页面。这是用户明确授权的 E2E
清理操作。

## 前置约束(2026-09-17 复核)

**官方 Notion MCP 没有删除或归档页面的工具。** 用 `fetch id=self` 查
`current_tool_access`,里面只有 create/update/move/duplicate,没有
delete/archive/trash——Notion 官方就是不开放这个能力。因此:

- 本手册**不能**由 agent 宿主自动完成删除,宿主只能做第 1 步身份确认;
- 实际删除有两条路,都要人来做或另配凭据:
  1. 在 Notion 界面上删除(移入废纸篓,可恢复)——推荐,最快;
  2. 自建 internal integration,拿 token 走 REST
     `PATCH /v1/pages/{id}` `{"archived": true}`。注意
     `pku_sync/notion.py` 是故意做成不能归档页面的(见该文件顶部说明),
     所以这条路不要改本项目的客户端去实现。
- `move_pages` 不是替代品:它只能换父级,不能删除。把测试页移到别处只是
  换个地方留着,不算清理。

## 输入

target 必须包含 `operation=e2e_cleanup` 和五个 `page_url`(先删的 E2E 页面
三个,后删的错题登记页两个),全部以 `page_url=` 逐行列出。缺任何一项时
blocked,不执行任何删除。

## 待清理页面(2026-09-17 已逐页核对身份)

E2E 测试页(标题含 `[E2E]`,父级 `Class Notes 2026 下半学期 / CNS解剖`):

```text
page_url=https://app.notion.com/p/3dd91b6f53e181d4ad44fb6c390c5d74  [E2E] CNS解剖小测(第2轮)｜2026-09-09 第5-6节
page_url=https://app.notion.com/p/3dd91b6f53e181d8a432fcd634150705  [E2E] 评论重整测试页｜CNS解剖
page_url=https://app.notion.com/p/3dd91b6f53e181ba8fabe2c156f210c4  [E2E] CNS解剖小测｜2026-09-09 第5-6节
```

错题登记页(父级 `Class Notes 2026 下半学期 / 知识点与错题`,正文出自
第 2 轮 E2E 批改,对应 `E2E_WRONG_Q2` / `E2E_WRONG_Q4`):

```text
page_url=https://app.notion.com/p/3dd91b6f53e1811bb581eff36657e353  小脑三分区(小测第 2 题得 0 分)
page_url=https://app.notion.com/p/3dd91b6f53e1814fa122ed0c4b4ac9ec  下丘脑摄食双中枢(小测第 4 题得 0 分)
```

## 操作

1. 对每个 `page_url` 先 fetch 确认:
   - 三个 E2E 测试页的标题必须包含 `[E2E]`;
   - 两个错题页必须位于 `Class Notes 2026 下半学期 / 知识点与错题` 下,
     且正文来自 `E2E` 批改登记;
   - 不符合时停止,不删除该页,并在输出中说明原因。
2. 全部确认后按「前置约束」里的两条路之一删除这五个页面。agent 宿主到这
   一步只能报 `blocked reason=no_delete_tool`,并把已核对的页面清单交回
   给人。
3. 删除后再次 fetch,确认五个页面都不存在;不要删除或修改任何其他页面、
   课程页或正式讲次页。

## 输出

最后一行必须三选一:

```text
E2E_CLEANUP=success deleted=5
```

```text
E2E_CLEANUP=blocked reason=no_delete_tool
```

或

```text
E2E_CLEANUP=blocked reason=<具体原因>
```
