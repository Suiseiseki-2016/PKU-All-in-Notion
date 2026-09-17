# PKU 课程工作台使用手册

本文面向第一次部署的用户。目标是用一条 setup 命令选择
Factory、Claude Code 或 Codex，完成基础环境和 MCP 配置，然后用
`uv run` 启动课程工作台。

## 1. 先准备什么

每台运行机需要：

- Python 3.11+
- `uv`
- 教学网账号
- 至少一个 agent CLI：
  - Factory：`droid`
  - Claude Code：`claude`
  - Codex：`codex`
- 能访问教学网和 `https://mcp.notion.com/mcp`

安装项目依赖：

```bash
uv sync --all-extras
```

不要激活虚拟环境，也不要使用 PowerShell profile、包装器、`UV_NO_SYNC`
或 `--no-sync`。所有日常命令都通过 `uv run` 执行。

## 2. 一键部署

setup 会完成以下工作：

1. 保存教学网账号；
2. 选择 LLM/API 配置；
3. 写入 `AGENT_HOST`；
4. 给所选宿主注册本地只读 `pku-sync` MCP；
5. 注册官方 Notion MCP；
6. 引导完成一次 Notion OAuth；
7. 打印脱敏后的配置摘要。

### Factory / Droid

```bash
uv run pku-sync setup --host factory
```

Factory 的官方 Notion OAuth 需要在 Factory/Droid 中打开 `/mcp`，选择
`notion` 并点击浏览器授权。setup 会打印这条提示。授权凭证由 Factory
自己的 keyring 管理，项目不会保存 Notion token。

### Claude Code

```bash
uv run pku-sync setup --host claude
```

setup 会调用：

```text
claude mcp add --scope user pku-sync ...
claude mcp add --scope user --transport http notion https://mcp.notion.com/mcp
claude mcp login notion
```

### Codex

```bash
uv run pku-sync setup --host codex
```

setup 会调用：

```text
codex mcp add pku-sync ...
codex mcp add notion --url https://mcp.notion.com/mcp
codex mcp login notion
```

如果某个 server 已经存在，setup 会报告 `already configured`，不会重复
添加。换宿主时可以重新执行同一条 setup 命令，例如：

```bash
uv run pku-sync setup --host codex
```

### 为什么不是完全无交互

教学网密码和 Notion OAuth 都是安全边界。项目可以一键完成配置流程，
但不会把密码写进命令行参数，也不会绕过浏览器授权。用户只需要在 setup
提示时输入密码，并在宿主的 OAuth 页面点击一次允许。

## 3. 部署后基础验收

先运行自检：

```bash
uv run pku-sync doctor
```

至少应看到：

- `.env 配置`：ok
- `凭据`：ok
- `依赖`：ok
- `Agent 宿主`：ok
- `磁盘`：ok

然后确认教学网读取：

```bash
uv run pku-sync discover
uv run pku-sync recordings
```

最后启动工作台：

```bash
uv run pkutui
```

启动成功的最低标准：

1. 本地资料先显示；
2. 课程表能显示教学网读取状态；
3. `Enter` 能打开课程详情；
4. `Space` 能选择录音；
5. `q` 能退出。

Windows 运行机还应执行：

```bash
uv run python scripts/e2e_windows_tui.py
```

macOS / Linux 运行机可另跑入口级 smoke（pty 启动真实 `pkutui`、验证课程表渲染与干净退出）：

```bash
uv run python scripts/e2e_pkutui_entry.py
```

## 4. 第一次课程处理

建议先只处理一门课或一个录音：

```bash
uv run pku-sync sync --course <course_id>
uv run pku-sync download --course <course_id> --limit 1
uv run pku-sync process --course <course_id> --limit 1
```

确认本地出现 `notes.md` 后再运行完整链路：

```bash
uv run pku-sync daily
uv run pku-sync review
uv run pku-sync lecture-batch
```

Notion 写入全部由所选宿主通过官方 MCP 执行。查看结果：

```bash
uv run pkutui
```

按 `n` 查看最近晨检和建页报告。

## 5. TUI 操作速查

| 按键 | 操作 | 是否可能写 Notion |
| --- | --- | --- |
| `Enter` | 打开课程资料、公告、作业和录音详情 | 否 |
| `Space` | 勾选/取消勾选录音讲次 | 否 |
| `a` | 整理录音并创建/更新讲次页 | 是 |
| `o` | 读取评论并重整讲次页 | 是 |
| `z` | 按勾选范围生成小测 | 是 |
| `g` | 批改小测并登记错题 | 是 |
| `n` | 刷新 Notion 报告状态 | 只读本地报告 |
| `d` | 运行 daily | 可能间接写 Notion |
| `r` | 运行 Notion 晨检 | 是 |
| `b` | 批量创建讲次页 | 是 |
| `s` | 刷新所有面板 | 否 |
| `q` | 退出 | 否 |

长正文不会全部塞进表格。详情表展示摘要和本地路径，全文沿路径查看。
AI 操作只使用当前选中的讲次，不会自动扩大范围。

## 6. 安全确认

以下动作会产生真实外部副作用：

- `a`、`o`、`z`、`g`
- `r`、`b`
- `uv run pku-sync review`
- `uv run pku-sync lecture-batch`
- 作业提交的 `--yes`

runbook 必须输出：

```text
TUI_RESULT=success url=<Notion URL>
```

没有这个标记，即使进程退出码为 0，也只能显示 `blocked`。

第一次验证建议使用隔离组合 E2E：

```bash
uv run python scripts/e2e_composite_workflow.py
uv run python scripts/e2e_automate_workflow.py
```

前者真实启动 Textual TUI，使用真实 `Enter/Space` 交互和同一组 App actions；
后者验证 `automate` 编排和失败恢复。两者都用临时数据和 fake 外部边界，
不会改动用户空间。

作业提交边界的安全 E2E：

```bash
uv run python scripts/e2e_assignment_submit.py
```

它不连接真实教学网，也不会提交作业。真实 `--yes` 只能绑定专用测试课程
和专用测试作业。

入口级 smoke（macOS / Linux）只验证真实 `pkutui` 入口的启动、渲染与干净
退出，不写 Notion：

```bash
uv run python scripts/e2e_pkutui_entry.py
```

真实 Notion 小测 E2E 只有在明确授权后运行：

```bash
uv run python scripts/e2e_notion_quiz.py
```

它会选择最多两个讲次，创建标题带 `[E2E]` 的测试页面，使用相同范围
再次运行并验证幂等复用，然后通过只读 runbook 验证页面父级课程页、讲次
范围、题目数量、来源和教师区。测试完成后，根据报告中的 URL 再手动删除
或保留测试页。

评论重整和批改 E2E：

```bash
uv run python scripts/e2e_notion_reorganize.py
uv run python scripts/e2e_notion_grade.py
```

前者创建测试讲次页和测试评论，运行两次评论重整并验证页面 marker；
后者准备测试答案、运行两次批改并验证成绩和错题。两者都会真实写入
Notion，只能在明确授权后执行，完成后按报告 URL 手动清理 `[E2E]` 页面。

完整组合场景和发布前验收见：

```text
docs/E2E_TEST_PLAN.md
```

## 7. 常见问题

### 找不到 agent CLI

```bash
uv run pku-sync doctor
```

如果显示找不到宿主，先把对应 CLI 放进当前用户的 `PATH`，再重复 setup。
不要修改项目代码去猜测宿主的安装目录。

### MCP server already configured

这是幂等提示，不是错误。直接运行：

```bash
uv run pku-sync mcp configure <factory|claude|codex> --login
```

### Notion OAuth 未完成

确认宿主 CLI 已登录，并重新执行对应宿主的 setup。Factory 需要在
`/mcp` 面板中手动选择 `notion` 授权。

### pku-sync 命令报 `No module named 'pku_sync'`（macOS + iCloud）

`uv run pytest` 正常、但 `uv run pku-sync …` 报找不到模块时，检查虚拟环境
是不是放在 iCloud 同步目录里（`~/Desktop`、`~/Documents` 在开启「桌面与文档
同步」后都属于 iCloud）：

```bash
ls -lO .venv/lib/python*/site-packages/*.pth   # 出现 hidden 就是这个问题
```

iCloud 的 `bird` 守护进程会给 `.venv` 里的文件加上 macOS `hidden` 标记，而
Python 3.11+ 的 `site` 模块**会跳过带 hidden 标记的 `.pth` 文件**，于是
editable 安装的路径注册失效，控制台命令就找不到包。`chflags -R nohidden .venv`
只能顶一会儿，iCloud 隔几分钟又会加回去。

根治办法是把虚拟环境挪出同步目录，再用软链接留在原位（`uv sync` 不会破坏它）：

```bash
mkdir -p ~/.venvs
mv .venv ~/.venvs/pku-course-sync
ln -s ~/.venvs/pku-course-sync .venv
chflags -R nohidden ~/.venvs/pku-course-sync
uv run pku-sync doctor            # 验证
```

不要为此把 `UV_PROJECT_ENVIRONMENT` 导出到 shell 配置里：那个变量是全局的，
会让其它 uv 项目也共用同一个环境。

### `uv run pkutui` 启动后卡住不动

`pkutui` 是全屏交互式 TUI，需要真实的终端（TTY）。在 CI、管道、部分 IDE
内置控制台等没有交互终端的环境里启动，它会一直等待输入，看起来像「卡
住」——这不是故障。请在真实终端里运行，用 `q` 退出。想机器验证入口本身
是否健康：

```bash
uv run python scripts/e2e_pkutui_entry.py
```

### TUI 显示 blocked

查看 `DATA_DIR/logs/tui_*.md` 或 `tui_grade_*.md`，重点检查：

1. 是否有完整的 `TUI operation target`；
2. agent 是否能看到官方 Notion MCP；
3. 输出末尾是否缺少 `TUI_RESULT`；
4. 页面是否已经创建但后续索引步骤失败。

不要只看进程退出码。没有明确成功标记就按未写入处理。

## 8. 一次完整验收清单

```text
[ ] uv sync --all-extras
[ ] uv run pku-sync setup --host <factory|claude|codex>
[ ] uv run pku-sync doctor
[ ] uv run pku-sync discover
[ ] uv run python scripts/e2e_composite_workflow.py
[ ] uv run python scripts/e2e_automate_workflow.py
[ ] uv run python scripts/e2e_assignment_submit.py
[ ] uv run python scripts/e2e_pkutui_entry.py    # macOS / Linux；Windows 用 e2e_windows_tui.py
[ ] uv run pkutui
[ ] Enter 打开详情
[ ] Space 选择两个讲次
[ ] q 退出
[ ] 明确授权后再运行真实 Notion E2E
```

真实评论重整和批改 E2E 会创建/更新 `[E2E]` Notion 页面，必须单独确认
授权后再运行：

```bash
uv run python scripts/e2e_notion_reorganize.py
uv run python scripts/e2e_notion_grade.py
```
