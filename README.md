# pku-course-sync

北京大学教学网（Blackboard）课程资料 + 课堂录像的每日自动同步管道，配合 Notion 实现课程内容的自动登记与讲次笔记生成。

纯 Python 单机项目：同一套代码在 Windows / macOS / Linux 上行为一致，不调用任何操作系统的专属接口。

## 功能

- **课程发现与同步**：IAAA 登录，自动识别当学期课程；同步公告、课件、作业到本地
- **课堂录像**：枚举录播、解析真实媒体地址、断点续传下载；HLS 走分片并发下载（AES-128 自动解密，ffmpeg 只作兜底）
- **转写与加工**：faster-whisper 转写全文、OpenCV 抽取关键帧、LLM 生成结构化讲次笔记
- **每日管道**：`uv run pku-sync automate` 一条命令跑完整链条，由你选择的系统调度器定时触发（项目本身不注册调度）
- **Notion 自动化**：晨检对账（新作业/课件/公告登记 + 讲次页口头任务、自测错题联动）+ 讲次页幂等创建与重整（每讲附 3~5 题自测）
- **考前课程总结**：`scripts/course_summary.md` 把一门课全部讲次页聚成一页快照——老师点名考点置顶带出处、考核与 DDL 汇总、缺口清单
- **TUI 工作台**：`uv run pkutui` 先展示本地课件、公告、作业、录音处理阶段和最近 Notion 报告，再实时读取教学网课程/DDL；管道健康状态**不等教学网秒级上屏**，实时抓取 4 路并发逐课推进。选中课程后可用 `Enter` 查看详情，`a/o` 让 AI 整理录音或读取评论重整讲次页，`z/g` 生成或批改 Notion 小测，`n` 查看 Notion 状态；所有 Notion 写入仍经 Factory/Claude/Codex 的官方 MCP runbook
- **作业提交**：`uv run pku-sync submit` 把文本或附件作业交回教学网——list 查 content_id，text / file 都默认干跑、`--yes` 才真正提交，带已提交守卫与逾期警告（运行手册见 `scripts/assignment_submit.md`）
- **课表与成绩（只读）**：`uv run pku-sync coursetable` 读门户个人课表（`--list-terms` 列可选学期、`--term` 查指定学期），`uv run pku-sync grades` 读教学网成绩明细；两者只读，不写任何内容
- **一键自检**：`uv run pku-sync doctor` 七项三态检查（配置/凭据/依赖/宿主/日志/报告/磁盘），fail 项退出码 1
- **本地面板**：`uv run pku-sync panel` 起一个只绑 127.0.0.1 的浏览器状态页（FastAPI + uvicorn 属默认依赖）——实时看 daily 日志、EXIT_CODE、各课录音阶段，页面上点按钮就能跑 daily/automate（忙时提示，绝不并行跑管道）。面板是瘦客户端的 UI 层：纯 Python + 浏览器渲染，零 OS 专属 GUI 代码，整包可迁移到其他系统

## 每日管道

```
uv run pku-sync automate                同步资料 → 下载新录像 → 转写/关键帧/笔记 → 当日简报（原生 LLM）
  └─ agent 宿主 × daily_review_mcp.md     与 Notion 对账登记（宿主经官方 notion MCP 写 Notion）
```

- Notion 步骤由所选 agent 宿主（Factory / Claude Code / Codex）执行：宿主经官方 Notion MCP（OAuth 一次确认、凭证存宿主 keyring）+ 本地 pku-sync MCP（只读课程数据）完成登记，`pku-sync` 自己不保存任何 Notion 凭证。
- 简报由 Python 原生生成：`llm.py` 支持 OpenAI 兼容 API，或把已登录的 claude / codex / droid CLI 当纯文本后端（`LLM_PROVIDER=auto` 自动探测）。
- 定时运行交给系统调度器：任务计划程序 / launchd / cron……任何能定时跑一条命令的工具都行（例如每天 06:00 执行 `uv run pku-sync automate`）。本项目不做调度注册，也不假设具体操作系统。
- daily 失败不会拖掉晨检：automate 会继续跑 Notion 步骤，让晨检把 `⚠️ 管道异常`（EXIT_CODE）写进每日检查记录。失败提示就两条路：Notion 晨检记录和 TUI 状态面板（❌ EXIT_CODE），不做手机推送。
- review / 批量建页各 60 分钟超时，失败只记日志、不影响本地管道退出码。

**完整说明见 `scripts/PIPELINE.md`**（组件清单、规范单一来源、存量页对齐流程、运行环境、迁移进度）。
组合 E2E 的场景矩阵和验收标准见 [`docs/E2E_TEST_PLAN.md`](docs/E2E_TEST_PLAN.md)。
首次部署和日常操作见 [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)。
服务化方向：本项目是「PKU All in Notion」的**瘦客户端**（开源半边）；云端中转（转写计量、平台账号、兑换码）由平台另行运营，代码在私有仓，不在本仓库。

## 安装与启动

项目使用 `uv` 管理跨平台环境。首次安装或明确升级依赖时执行一次：

```bash
uv sync --all-extras
uv run pku-sync setup         # 教学网账号 → agent 宿主 → Notion OAuth
```

`uv run pku-sync setup` 把 PKU 账号和 `OPENAI_API_KEY` 写进 `.env`，并为所选 agent 宿主（Factory / Claude Code / Codex，`.env` 的 `AGENT_HOST`）注册两个 MCP server：官方 Notion MCP 与本包的只读课程 MCP。Notion 授权由宿主管理（浏览器点一次「允许」，凭证存宿主自己的 keyring）——项目不打包任何 client secret，用户也不需要复制 token。其余高级配置仍可手改 `.env`（全部可选项见 `.env.example`）：`DATA_DIR`（数据根目录，建议绝对路径）、`COURSE_ALLOWLIST/DENYLIST`、`COURSE_TERM`、`WHISPER_MODEL/DEVICE/COMPUTE_TYPE`、`DELETE_VIDEO_AFTER_PROCESSING`。

日常启动不需要激活虚拟环境，也不需要 profile 函数、包装器、`UV_NO_SYNC` 或 `--no-sync`：

```bash
uv run pkutui                 # 交互式课程工作台
uv run pku-sync daily         # 单步管道
uv run pku-sync automate      # 定时任务使用的完整管道
uv run pku-sync doctor        # 运行环境自检
```

媒体转写、关键帧和 OpenAI 依赖属于默认项目依赖，所以普通 `uv run` 会保持完整环境。依赖变更后重新执行 `uv sync --all-extras`，不要在运行机上手工安装或卸载媒体包。

## 使用

```bash
uv run pku-sync setup               # 首次配置（账号 + MCP 宿主，一条命令）
uv run pku-sync discover            # 列出账号可见课程及本学期选中课程
uv run pku-sync sync                # 同步公告/课件/作业 + 录像元数据
uv run pku-sync download            # 下载已索引的录像（断点续传）
uv run pku-sync process             # 转写 + 关键帧 + LLM 笔记
uv run pku-sync daily               # 以上三步 + 当日简报（--skip-summary 跳过简报）
uv run pku-sync review              # 单独跑 Notion 晨检登记（经所选宿主的两组 MCP）
uv run pku-sync automate            # daily + review 一把梭
uv run pkutui                       # 交互式控制台：实时课程/DDL/录音 + 管道状态 + 按键触发
uv run pku-sync doctor              # 一键自检：配置/凭据/依赖/宿主/日志/报告/磁盘
uv run pku-sync panel                # localhost 状态面板（server extra：uv sync --extra server）
uv run pku-sync submit list --course 认知心理学
uv run pku-sync submit text --course … --content … --file answer.md  # 默认干跑；确认后加 --yes 才提交
uv run pku-sync submit file --course … --content … --file 作业.pdf   # 附件提交，同样默认干跑
uv run pku-sync coursetable                     # 个人课表（--list-terms 列学期，--term 25-26-2 查历史学期）
uv run pku-sync grades                          # 成绩明细（--json 输出结构化数据）
uv run pku-sync summarize           # 只生成当日简报（--log 可指定日志）
uv run pku-sync mcp serve           # 启动本地只读课程 MCP server
uv run pku-sync mcp configure claude --login  # 注册两组 MCP 并完成 OAuth
uv run pku-sync notion …            # REST fallback（无 agent 环境的高级路径）
uv run pytest                       # 运行测试
```

### TUI 工作台

`uv run pkutui` 先显示本地资料和管道状态，再并发读取教学网课程与 DDL。网络读取较慢时，本地状态仍会立即显示；课程表会显示逐课 `N/总数`、课程名和耗时。

在课程表中移动到课程后：

- `Enter`：打开课程详情表。
- 详情表显示作业/DDL、课件、公告、录音的标题、摘要和本地路径；长正文不在表格中展开，沿本地路径查看全文。
- `Space`：勾选或取消勾选录音。AI 操作只作用于勾选的讲次。
- `a`：整理勾选讲次的录音、转写和笔记，并创建或更新 Notion 讲次页。
- `o`：读取 Notion 评论，按评论重整勾选讲次页面。
- `z`：按勾选范围生成 Notion 小测。
- `g`：批改目标 Notion 小测并登记错题。
- `n`：刷新最近的 Notion 晨检、建页和 TUI 报告状态。
- `d` / `r` / `b`：分别运行 daily、Notion 晨检、批量建页。
- `s`：刷新；`q`：退出。

Notion 写入不由 TUI 直接完成，而是交给配置好的 Factory、Claude Code 或 Codex 宿主，通过官方 Notion MCP 执行。TUI 只有在 runbook 输出 `TUI_RESULT=success` 时才显示成功；退出码为 0 但未写入时会显示阻塞，并保留报告供排查。

### E2E 验证

Windows 运行机上的真实 TUI 验证使用本地数据和真实教学网读取，不经过 SSH：

```bash
uv run python scripts/e2e_windows_tui.py
```

它检查课程表、`Enter` 详情、`Space` 多讲次选择和 `q` 退出，不写 Notion。

macOS / Linux 用入口级 smoke 验证真实 `pkutui` 控制台入口（不写 Notion）：

```bash
uv run python scripts/e2e_pkutui_entry.py
```

它在 pty 里启动真实入口、等待课程表渲染、SIGTERM 后断言干净退出；`q` 等交互按键路径由测试套件的 `run_test` 覆盖。

隔离组合工作流 E2E（真实 TUI、临时数据、不会写 Notion）：

```bash
uv run python scripts/e2e_composite_workflow.py
uv run python scripts/e2e_automate_workflow.py
uv run python scripts/e2e_assignment_submit.py
```

明确授权后还可运行真实 Notion 评论重整和小测批改 E2E：

```bash
uv run python scripts/e2e_notion_reorganize.py
uv run python scripts/e2e_notion_grade.py
```

真实 Notion 小测链路使用官方 MCP 创建一个标题带 `[E2E]` 的测试页：

```bash
uv run python scripts/e2e_notion_quiz.py
```

该命令会产生真实 Notion 写入，只应在明确授权后运行。测试页不会冒充正式小测，完成后可在 Notion 中手动删除。

以上命令是 smoke 验证，不代表完整工作流已经通过。发布前的多步骤组合、
幂等、失败恢复和证据要求见 [`docs/E2E_TEST_PLAN.md`](docs/E2E_TEST_PLAN.md)。

## 目录结构

```
pku_sync/     核心包：auth / discover / materials / recordings / media / pipeline / store / cli
scripts/      管道入口与 agent 运行手册（PIPELINE.md 为总览，先读它）
docs/         设计与验收文档（组合 E2E 见 E2E_TEST_PLAN.md）
tests/        pytest
data/         课程数据（gitignored，按课程分目录）
```

## 运行环境

一台机器跑全部：`uv run pku-sync automate` 在哪台机器跑，数据（`DATA_DIR`）与日志就落在哪台机器。Windows / macOS / Linux 均可；媒体依赖由项目默认依赖提供（含 ffmpeg 运行时要求）。定时触发由该机的系统调度器负责，与项目无关。

## 讲次页格式规范

讲次页（Notion）的格式规范只维护在 `scripts/lecture_notes_writer.md` §4：流水笔记、内嵌关键帧图 6–12 张、考点/作业就地加粗、每讲 3–5 题自测（答错标 ✗，晨检自动登记错题）、无时间标签。规范变更后需按 `PIPELINE.md` §5 对齐存量页面。
