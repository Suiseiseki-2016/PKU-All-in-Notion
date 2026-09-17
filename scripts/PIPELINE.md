# PKU 课程同步管道总览

本文件是整个项目的入口说明：各组件怎么串起来、规范写在哪里、出问题看哪里。

组合 E2E 的场景矩阵、真实 Notion 写入边界、幂等断言和证据格式见
`docs/E2E_TEST_PLAN.md`。本文描述管道架构和运行规则；E2E 文档描述如何
验收跨组件用户工作流。
首次部署和日常操作见 `docs/USER_GUIDE.md`。

## 1. 每天发生什么

```
教学网 + 课堂录屏
      │  定时触发（示例 06:00）→ uv run pku-sync automate
      │  （由所在机器的系统调度器拉起；本项目不注册调度、不感知操作系统）
      ▼
pku_sync.cli daily        下载课件/作业/公告 + 录音转写(Whisper) + 抽关键帧
      │  最后一步原生生成当日中文简报（llm.py）
      │  产出写入 DATA_DIR\<课程>\…，日志在 DATA_DIR\logs\
      ▼
agent host × daily_review_mcp.md    与 Notion 对账，登记新作业/课件/公告（宿主=AGENT_HOST，MCP:notion + MCP:pku-sync）
      ▼
agent host × lecture_batch_creator.md  扫描本地转写，幂等补建缺失讲次页（同宿主，同两组 MCP）
```

- `automate` = daily + MCP 晨检 + 讲义批量建页，一条命令跑完；两个 agent 步骤串行、各 60 分钟超时，失败只记日志、不影响退出码（退出码只反映 daily 本身，简报失败同样只记日志）。**daily 失败不拖掉晨检**（2026-09-16 起）：daily 落完 EXIT_CODE 标记后 automate 继续跑晨检与建页，最后再把失败退出码抛回去——正是靠这条，晨检才能把 `⚠️ 管道异常` 写进 Notion 每日检查记录（`daily_review_mcp.md` §4 管道状态行）。
- 日志归属（2026-09-16 起）：`logs/daily_<stamp>.log` 与 `logs/latest.daily.log` 由 `uv run pku-sync daily` 自己捕获写出（rich capture + 去 ANSI，尾部带 `=== EXIT_CODE=… ===` 标记，简报自己的输出随后追加进两份文件）。daily.ps1 退役前靠它的 PowerShell 重定向写这两份文件，退役后 Python 是唯一写者——否则简报（`summarize` 读 latest.daily.log）和 MCP 晨检的 `get_daily_status` 都会停在旧文件上。
- 调度现状（2026-09-16 单机化）：唯一执行机是 Windows，计划任务 `pku-course-sync` 06:00 = `automate`（OS 层维护；改任务用系统自带的任务计划程序）。`pku-sync schedule install` 已移除出项目；Mac 侧所有 LaunchAgent（含归档）已卸载，Mac 不再承担任何运行角色。旧任务 `\PKU-Course-Daily-Sync`（daily.ps1 链）已删除（2026-09-16），daily.ps1 与其余遗留脚本（check_daily_syntax / stage_pull / stage_pull_v2 / stage_news / _cleanup2）也已删除。
- 故障发现（2026-09-16 起）：EXIT_CODE 尾标（Notion 晨检记录与 TUI 状态面板同读）+ 调度器的 Last Run Result；手机推送链路（notify.py）已按需求移除——失败提示收进 TUI 与晨检。一键自检 `uv run pku-sync doctor`（`doctor.py`）：配置/凭据/依赖/宿主 CLI/最近 daily EXIT_CODE/晨检与建页报告新鲜度/磁盘余量，共 7 项三态（ok/warn/fail），fail 项使退出码为 1，全部为纯 Python 检查、不依赖操作系统。
- 也可手动单步触发：`uv run pku-sync review`（晨检）、`uv run pku-sync lecture-batch`（批量建页）、`uv run pkutui`（控制台：状态 + 临期 DDL + 按键触发）；考前则按 `scripts/course_summary.md` 手动把单门课聚成一页课程总结。运行机上的日常启动统一使用 `uv run ...`（见 §4）。

## 2. 组件清单

| 组件 | 位置 | 作用 |
|---|---|---|
| 同步管道 | `pku_sync/`（Python 包） | 教学网抓取、录音下载（HLS 走分片并发 + AES-128 解密，ffmpeg 仅兜底）、Whisper 转写、关键帧抽取 |
| 转写后端 | `pku_sync/transcription.py` | `process` 的转写入口：`TRANSCRIPTION_BACKEND=local`（默认，本机 faster-whisper，行为不变）或 `cloud`（M2 包装服务客户端：本地抽 16k 音轨上传我们的中转、按平台账号计量，上游 ASR key 只在服务端、用户不持有任何 key；中转未部署/未配置时显式拒绝；见 `docs/SERVICE_PLAN.md` §1.7、§3.1-3.3） |
| 每日入口 | `pku_sync.cli automate` | 定时任务入口，一条命令串起 daily + 晨检 + 批量建页（v3 起 daily.ps1 退役，2026-09-16 已删除） |
| 原生简报 | `pku_sync/summarize.py` + `pku_sync/llm.py` | 当日简报：LLM 后端 provider 抽象（OpenAI 兼容 API 或已登录 claude/codex/droid CLI） |
| TUI 工作台 | `pku_sync/tui.py` + `tui_data.py` + `agent_runner.py` | `uv run pkutui`：先读本地课件、公告、作业、录音阶段和最近 Notion 报告，再登录教学网显示实时课程/DDL；课程表/DDL 失败会明确显示，绝不静默回退本地 DATA_DIR。管道状态**不等教学网即上屏**；实时抓取 4 路并发逐课推进，进度（N/总数 · 课程名 · 已用秒数）实时写进面板边框与状态行。选课后 `Enter` 看资料/录音详情，详情表给出摘要和本地路径；`Space` 勾选讲次，`a/o` 触发录音整理/评论重整，`z/g` 触发小测生成/批改，`n` 刷新 Notion 报告状态；所有写入交给受限 agent runbook + 官方 Notion MCP。数据层纯函数无 textual 依赖，单测覆盖本地工作台、实时快照、逐课错误、进度回调、并发抓取和无兜底失败路径 |
| 作业提交 | `pku_sync/submit.py` + CLI `submit` 组 | 文本或附件作业交回教学网：`submit list` 列作业条目（含 content_id、合并后的 DDL）；`submit text` / `submit file`（附件走 `action=newAttempt` 取表单 + `newFile_LocalFile0` 分段）默认干跑（取表单/核状态/预览），`--yes` 才 POST；已提交守卫（复查历史页探测）+ 逾期警告；回执 destinationUrl 供回写 Notion 任务行。手动工具，不进 automate；红线与线上行为备忘见 `scripts/assignment_submit.md` |
| 课表与成绩（只读） | `pku_sync/portal.py` + `pku_sync/grades.py` + CLI `coursetable` / `grades` | `coursetable`：门户 `portalPublicQuery` OAuth → `getXndXqList` / `getCourseInfo`，默认当前学期，`--list-terms` 列学期、`--term` 查指定学期；新学期未发布课表时给明确提示而不是空表。`grades`：教学网 Learn REST（`/users/me` 单对象 → 选课 → 课程名 → gradebook 列/分数），按 `grading.type` 过掉 Calculated 总计。两者纯读，手动工具，不进 automate |
| 一键自检 | `pku_sync/doctor.py` + CLI `doctor` | 7 项三态检查（见 §1 故障发现条），fail 退出码 1；CLI/磁盘探测全走注入点，单测不碰真实系统 |
| Notion 客户端 | `pku_sync/notion.py` | REST fallback：token 直连 Notion REST（页面/块读写、markdown→blocks、数据库查询建行、单文件上传 `file-upload://`）；CLI 子命令 `uv run pku-sync notion …`（默认路径改走官方 MCP，仅高级/无宿主环境使用） |
| Notion REST OAuth | `pku_sync/notion_login.py` | `uv run pku-sync notion login`：自建 public integration 的浏览器 OAuth，token 写入 `.env`（仅 REST fallback 用户需要） |
| `.env` 写入 | `pku_sync/envfile.py` | 通用 `.env` upsert（保注释保其他行）；login 写 token、setup 写账号共用 |
| 首跑向导 | `pku_sync/setup.py` | `uv run pku-sync setup`：教学网账号 → 可选 LLM key → 选 MCP 宿主 → 配置两组 MCP → 一次 Notion OAuth |
| 本地 MCP server | `pku_sync/mcp_server.py` + `mcp_tools.py` | 只读 stdio MCP：health / list_courses / list_course_files / read_course_text / list_recording_status / get_daily_status（路径限制在 DATA_DIR 内，读取截断上限 200k 字符） |
| MCP 宿主配置 | `pku_sync/mcp_setup.py` | `uv run pku-sync mcp configure <factory|claude|codex>`：为宿主注册 pku-sync + 官方 notion（`https://mcp.notion.com/mcp`）两个 MCP server，并可发起宿主侧 OAuth |
| Agent runner | `pku_sync/agent_runner.py` | `uv run pku-sync review` / `uv run pku-sync lecture-batch`：把 `scripts/daily_review_mcp.md` / `scripts/lecture_batch_creator.md` 交给所选宿主的无人值守会话（droid/claude/codex 三态命令），报告落 logs/review_YYYYMMDD.md、logs/lecture_YYYYMMDD.md |
| localhost 面板 | `pku_sync/panel/`（jobs + pipelines + webapi）+ CLI `panel` | `uv run pku-sync panel`（FastAPI + uvicorn 为默认依赖，只绑 127.0.0.1）：实时读 daily 日志尾/EXIT_CODE/录音阶段，按钮触发 daily/automate（单飞 JobRunner，忙时 409 不并行跑管道）；复用 M0 service 层同一代码路径；0 外部资源、浏览器即 UI（瘦客户端，迁移自由见 `docs/SERVICE_PLAN.md` §3.6）；产品化双视角设计后置 M3（§1.8） |
| 云端中转（M2 骨架） | 私有仓 `Suiseiseki-2016/PKU-All-in-Notion-server`（`server/accounts.py` + `relay.py`；服务端件不在本仓库） | 平台账号库（SQLite 单文件：账号/会话/兑换码/用量流水）+ `POST /v1/transcribe`（会话鉴权 → 余额闸 402 → ASR 注入位 → 实际秒数计量扣减 → 音频不落盘）+ `GET /v1/quota` + `POST /v1/redeem`（兑换码充值）+ `POST /v1/recharge`（501 支付预留）+ `/healthz`；上游 ASR 未配置时 503 响亮拒绝；用户不持有任何 key（§1.7）；部署目标 server-a/b/c.aeoluswu.info |
| 可移植晨检手册 | `scripts/daily_review_mcp.md` | 宿主无关的 Notion 对账登记规范（动态发现 hub/数据库，不硬编码用户 id） |
| 批量建页手册 | `scripts/lecture_batch_creator.md` | 为已有转写但还没有讲次页的课程幂等建页（v3 起可移植 MCP 版，由 agent_runner 分发，automate 自动拾起） |
| 讲次页手册 | `scripts/lecture_notes_writer.md` | 单页整理/重整（create / overwrite），页面格式规范的唯一来源 |
| 课程总结手册 | `scripts/course_summary.md` | 考前手动把一门课全部讲次页聚成一页《课程总结》快照（点名考点置顶+出处、考核与 DDL 汇总、缺口清单、口头任务兜底补登） |
| 单页运行器 | `scripts/run_lecture_notes.ps1` | Windows 上跑 writer 手册的 ASCII 包装（解决 stdin 中文乱码） |

## 3. 规范单一来源（重要）

**讲次页的格式规范只写在 `lecture_notes_writer.md` §4**（开头段 → 大主题段 → 📌待核 → 自测题 → 资料来源；内嵌关键帧图 6–12 张；每讲 3~5 题自测，答案须能在正文找到；不设独立考点板块，考点/作业就地加粗；标题与图注不带时间）。

- `lecture_batch_creator.md` 只引用该规范，不复制细节。
- 改格式时只改 writer 手册，然后检查 batch 手册的引用是否仍然成立。
- 2026-09-16 新增 §4.4 自测题段：对存量页是**纯增量**——旧页只缺这一段，可单独补题，不必全页重整；口头任务补登记走晨检 §3.1 的近 7 天扫描，更早的由课程总结手册（§2 表）兜底。
- **存量页面不会自动跟着规范变**。规范变更后，需要按 §4.2 之后的对齐流程（见 §5）手动重整存量页。

## 4. 运行环境（单机）

一台机器承担全部运行；本项目是纯 Python，不调用任何操作系统的专属接口。当前实例：

| | 运行机（唯一） | 开发机 |
|---|---|---|
| 主机 | Windows | Mac |
| 仓库 | `E:\remote_project\pku-course-sync` | `/Users/wudi/Desktop/remote_project/pku-course-sync` |
| 数据 | `E:\pku-course-data`（`DATA_DIR` 绝对路径） | — |
| Git/GitHub | — | `Suiseiseki-2016/PKU-All-in-Notion`（origin，main；`.env`/`data/`/`logs/`/`*.mp4` gitignore，永不入库） |

- 代码怎么到运行机由操作者决定（git clone / 任意文件同步工具均可）；项目本身不内置跨机同步机制，也不感知镜像。
- 定时任务、agent 宿主（droid exec）都在运行机上跑；调度 = 系统调度器里的一条定时命令（现役：计划任务 `pku-course-sync` 06:00 = `automate`）。
- **启动规则**：首次安装或明确升级依赖时执行 `uv sync --all-extras`；日常运行统一使用 `uv run pkutui` 或 `uv run pku-sync <子命令>`。媒体转写、关键帧和 OpenAI 依赖属于默认项目依赖，裸 `uv run` 不会卸载运行所需的媒体栈。不写 PowerShell profile，不设置 `UV_NO_SYNC`，不使用包装器，也不使用 `--no-sync`。依赖变更后重新执行 `uv sync --all-extras`，再运行 `uv run pku-sync doctor`。
- **项目入口**：`pkutui` 是 `pku_sync.cli:tui_cmd` 的项目脚本；`pku-sync` 提供同步、处理、Notion 晨检、批量建页和自检子命令。两者都必须通过 `uv run` 启动，以确保使用当前项目锁文件。
- Windows 运行机实测备忘（2026-09-16 切换时根治的坑）：
  - **schtasks 会话是完整用户环境**（有 PATH、无需代理快照）；`.env` 双路径查找已覆盖 system32 起点 cwd。
  - **npm shim 三件套**（无扩展名 sh 脚本 / `droid.cmd` / `droid.ps1`）里只有 `.cmd/.exe` 能被 CreateProcess 拉起。CLI 解析已统一为纯 PATH 探测（`shutil.which` 在 Windows 按 PATHEXT 解析，天然只拿可执行形式），宿主 CLI 必须在用户 PATH 上（`where droid` 可验证）。
  - **`droid mcp add` 会把参数里的反斜杠当转义吃掉**：注册 `E:\remote_project\...` 会在 `~/.factory/mcp.json` 里留下残缺命令。`mcp_setup` 已一律用正斜杠路径注册（Windows 下 CreateProcess 接受正斜杠）。
  - **Windows venv 是 `uv venv` 建的（没有 pip 模块）**：加依赖必须 `uv pip install -e .`（首次忘了补 `mcp` 包导致 pku-sync MCP 连不上，重跑该命令即可对齐 pyproject）。
- MCP 注册一律走 `scripts/mcp_launcher.py`：launcher 自己把项目根插入 sys.path，不依赖 editable-install `.pth`（历史：Mac iCloud 会把 `.venv` 的 .pth 标 hidden 导致宿主拉起本地 MCP 断连——Mac 退出运行后该问题消失，launcher 保留为对所有环境一致的稳健路径）。
- 已移出项目的机制（见 §7 Phase 5）：`schedule.py` 调度注册（→ 系统调度器自理）、`archive.py` 跨机归档、doctor 平台分支与双机一致性检查、TUI `--ssh`、`llm.resolve_cli` 常见目录兜底（→ 纯 PATH）。

### 4.1 TUI 工作台操作流

`uv run pkutui` 的数据顺序是“本地先行、教学网并发补充”：

1. 启动后先显示本地课件、公告、作业、录音处理阶段和最近 Notion 报告。
2. 教学网读取在后台并发进行，课程表显示 `N/总数`、当前课程和已用时间；网络失败会明确显示，不会伪装成本地结果。
3. 课程表按 `Enter` 打开详情表。详情表只展示摘要和本地路径，全文、转写和笔记沿路径查看，避免长内容阻塞 TUI。
4. 在详情表用 `Space` 选择多个录音讲次。`a/o/z/g` 读取这个选择范围；没有选择时，动作会阻塞并提示原因。
5. `n` 只刷新本地 Notion 报告状态；`d/r/b` 分别触发 daily、晨检和批量建页；`s` 刷新全部面板；`q` 退出。

AI 动作的成功标准不是子进程退出码，而是 runbook 最后一行的显式结果：

```text
TUI_RESULT=success url=<Notion URL>
TUI_RESULT=blocked reason=<原因>
```

只有 `success` 才代表实际完成 Notion 写入。`blocked` 或缺少结果标记时，TUI 保留 agent 输出和日志，不报告成功。Notion 写入始终通过官方 Notion MCP，TUI 和 `pku-sync` 不保存 Notion 凭证。

### 4.2 E2E 验证

Windows 运行机上执行真实 TUI 验证：

```bash
uv run python scripts/e2e_windows_tui.py
```

该脚本使用真实 `DATA_DIR` 和教学网 fetcher，验证课程表渲染、`Enter` 详情、`Space` 讲次选择和 `q` 退出，不写 Notion。

真实 Notion 小测验证：

```bash
uv run python scripts/e2e_notion_quiz.py
```

该脚本选择一个本地 `notes_ready` 讲次，通过配置的 agent 宿主和官方 Notion MCP 创建标题带 `[E2E]` 的独立测试页。它是有副作用的真实写入，只能在用户明确授权后运行；测试结束后可在 Notion 中手动删除页面。

隔离组合工作流 E2E：

```bash
uv run python scripts/e2e_composite_workflow.py
```

该脚本真实驱动 Textual TUI，覆盖 `a → z → z（幂等）→ g → o`，并用临时
数据和 fake Notion 边界断言课程页父级、两讲次范围、批改结果和评论重整。

以上命令是 smoke 验证，不等于完整组合工作流已经通过。发布前还必须按
`docs/E2E_TEST_PLAN.md` 执行资料到讲次页、小测生命周期、评论重整、每日
管道和失败恢复场景。

## 5. 存量页面对齐（规范变更后的标准动作）

2026-09-14 首次执行过一次全量对齐，流程沉淀如下：

1. 盘点：列出全部课程页下的讲次页，标记哪些仍是旧格式（有「考点速览」、图注带时间、缺关键帧图、尾部板块旧命名）。
2. 并发重整：每页派一个子 agent（或手动），动作固定为：删考点速览（先核对无独有信息丢失，有则合并进正文并加粗）→ 正文考点/作业就地加粗 → 图注统一为 `关键帧：<短语>` → 尾部统一为 `## 📌 需要自行核实的点` + `## 资料来源`（含关键帧总数）→ 缺图的按 writer §5 步骤 5b 上传并内嵌 6–12 张关键帧。
3. 逐页 fetch 复核：图全部解析为 S3 链接、无信息丢失、板块命名规范。
4. 子 agent 须知：先 ToolSearch 加载 notion 工具；若会话无 Notion 工具，立即停止并报告，不做任何写操作。

## 6. 12 门课映射

课程 ↔ 本地数据目录 ↔ Notion 课程页的映射表维护在 `lecture_batch_creator.md` §1（唯一来源）。新增课程时在该表加一行，并确认数据根目录下有对应文件夹。

## 7. v2 架构迁移

目标：确定性环节（同步、转写、简报）全部 Python 原生化；Notion 读写经官方 MCP 由 agent 宿主执行（droid / codex / claude 三选一皆可），手动精整仍走 agent 会话。

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | `llm.py`（openai/claude/codex/droid 四后端）+ `summarize.py` 原生简报 + `schedule install/uninstall/status` | 完成；Windows live 注册验证 2026-09-16（Register-ScheduledTask -StartWhenAvailable 补跑生效）；schedule 部分已于 Phase 5 移除出项目 |
| Phase 2a（降级为 fallback） | `notion.py` REST 客户端 + `pku-sync notion` CLI + `notion login` 自有 OAuth | 完成并保留；仅高级/无宿主环境使用，"内置 OAuth 客户端进包"的路线已放弃 |
| Phase 2b-MCP | **架构转向 MCP-first（2026-09-15 拍板）**：`mcp_server.py` 只读本地课程 MCP + `mcp_setup.py` 三宿主配置 + `agent_runner.py` + `daily_review_mcp.md` 可移植晨检 + `automate` 命令；Notion 凭证由宿主经官方 MCP 管理，包内不内置任何 secret | 代码+单测完成（155 全绿）；**live 验证完成（2026-09-15）**：claude OAuth 登录后 `pku-sync review` 端到端跑通——本地 MCP 读取 12 门课、notion search/fetch/query 定位 hub 与三库、一次 update-page 追加晨检记录，幂等键下 0 重复登记，报告落 `logs/review_20260915.md` |
| Phase 2c | 批量建页手册移植为可移植 MCP 版（复用 agent_runner 分发模式），daily.ps1 去掉对应 droid 步 | **完成（2026-09-16）**：`lecture_batch_creator.md` 改为宿主无关手册（运行时上下文给 data_root，直读数据树 + 双 MCP），`agent_runner.run_lecture_batch` + CLI `pku-sync lecture-batch` + `automate` 第 3 步；报告落 `logs/lecture_YYYYMMDD.md` |
| Phase 3 | 06:00 任务整体切到 `pku-sync automate`，daily.ps1 退役 | **完成（2026-09-16）**：Windows schtasks `pku-course-sync` 06:00 = `automate`，`.env` `AGENT_HOST=factory`，live 验证 2026-09-16（daily → droid MCP 晨检 → 讲义批量建页全链通过，3 部新转写补建讲次页）；旧任务 `\PKU-Course-Daily-Sync` 已删除，daily.ps1 退役；daily 日志改由 Python 写出（见 §1 日志归属）；Mac LaunchAgent 于同日先装后卸 |
| Phase 4 | 运维加固三件套：`notify.py` 失败推送（bark/telegram/webhook）+ `doctor.py` 一键自检 + `archive.py` 归档拉回 | 代码+单测完成（2026-09-16）；live 验证：13 个录像、186 MB 首轮归档成功，二次运行 0 传输；archive 已于 Phase 5 移除（归档目录内的存量数据保留），notify 亦于同日按需求移除（失败提示收进 TUI/晨检），doctor 保留 |
| Phase 5 | **单机化 + 纯 Python 化（2026-09-16 拍板）**：移除 `archive.py` 跨机归档、`schedule.py` 调度注册（OS 专属）、doctor 平台分支与双机一致性检查、TUI `--ssh`；CLI 解析退回纯 PATH 探测；`SCHEDULE`/`DAILY_SCHEDULE_REQUIRED`/`ARCHIVE_*` 配置项移除；Windows 为唯一执行机（OS 层计划任务 `pku-course-sync` 保留），Mac 侧 LaunchAgent 全部卸载 | **完成（2026-09-16）**：单测全绿；Windows live doctor 全通过 |

关键决定（实现时已固化）：

- **调度交给环境（Phase 5 最终形态）**：项目不内置调度注册器——`pku-sync automate` 是普通命令，定时触发由所在机器的系统调度器负责（现役：Windows 计划任务 `pku-course-sync`，06:00，OS 层维护）。理由：注册器必然绑定具体操作系统（schtasks/launchd/crontab 三套分支），而进程内常驻调度器挂掉即静默停摆。
- **LLM 后端 provider 抽象**（`LLM_PROVIDER=auto`）：有 `OPENAI_API_KEY` 走 OpenAI 兼容 API，否则把已登录的 claude / codex / droid CLI 当纯文本后端；CLI 解析为纯 PATH 探测（Phase 5 起移除常见安装目录兜底，保持零 OS 依赖；宿主 CLI 需在用户 PATH 上）。
- **子进程一律 `encoding="utf-8"`**：计划任务会话可能按本地代码页（如 GBK）解码 CLI 输出，中文简报会乱码；外部调度器自身回显也常是本地编码，所以只依赖退出码、不解析它的文本。
- **`.env` 双路径查找**：CWD 优先，兜底包根目录——调度器或服务从任意目录启动进程时也能找到配置。
- **Phase 1 切换点**：daily.ps1 删除 droid 简报步（`pku-sync daily` 已内置 summarize，退出码不变），晨检与批量建页两步暂留。
- **Phase 2a 客户端设计**（`notion.py`，现为 REST fallback）：统一 429/5xx 重试（尊重 Retry-After，上限 30s）；块请求按 API 上限分批 ≤100；`markdown_to_blocks` 覆盖 writer §4 全部语法（标题/无序列表（含 `+` 简报行）/有序/引用/分割线/代码块/**加粗**/`![图注](file-upload://…)`，本地路径直接报错提示先上传）；`replace_page_content` 发现子页面/子库即拒绝（红线：自动链永不删页）；id 解析接受 32 位 hex、带横杠 UUID、notion.so URL 与 `collection://` 形式；CLI 面 = MCP 用法的 shell 等价物。
- **REST fallback 凭证**（`notion_login.py`，仅自建集成的用户）：`pku-sync notion login` 走 Notion OAuth 公共集成——浏览器点一下、勾选要分享的 hub、token 自动写入 `.env` 的 `NOTION_TOKEN`（手填 token 保留为后备，两条路同一套 REST）。本地回调 `http://localhost:8765/callback`（`--port` 可换，须与集成登记一致）；state 校验拒绝过期页/串扰请求但不中断等待；token 交换故意**不重试**（code 单次有效，失败就重跑 login）。待 live 验证点：Notion 是否接受 http://localhost 回调（不接受则改托管跳转页方案）。
- **MCP-first，REST 仅 fallback**（2026-09-15 拍板，取代早先"OAuth 客户端打进包内"的方案）：Notion 凭证不由项目发行或保管——用户选定一个 agent 宿主（Factory / Claude Code / Codex，`.env` 的 `AGENT_HOST`），`pku-sync setup` / `pku-sync mcp configure` 为宿主注册官方 notion MCP（`https://mcp.notion.com/mcp`）+ 本地 pku-sync MCP；Notion OAuth 在宿主里完成一次，凭证存宿主自己的 keyring（droid 存系统 keyring 并跨项目复用）。放弃内置 BUNDLED_CLIENT_ID/SECRET 的原因：长期 client secret 不应进发行包，且官方 MCP 对三种宿主零适配成本。`automate` = 原生 daily + `agent_runner.run_review`（60 分钟超时，失败只记日志不动本地管道退出码）；本地 MCP 严格只读（DATA_DIR 路径限制、按段列文件、read 上限 200k 字符），Notion 写入全部经官方 MCP；`daily_review_mcp.md` 动态发现 hub/数据库、不硬编码用户 id（同一手册三宿主通用）。setup 一条龙：PKU 账号（getpass 不回显，留空保留现有）→ 可选 OPENAI key → 选宿主 → 配置两组 MCP → 一次 OAuth。脱敏规则：短密钥只显示「已设置」，长 key 才露前 6 位。live 验证（2026-09-15）：claude `-p --permission-mode auto` 下免确认确认通过——pku-sync 只读工具与 notion 读+写（update-page 追加晨检）均可用；两个坑已修：① `--add-dir` 是 variadic，提示词必须放在它前面否则被吞成目录参数（claude 报 "Input must be provided either through stdin or as a prompt argument"）；② notion HTTP server 比本地 stdio 慢，短会话曾见 "still connecting"，`agent_runner` 对 claude 已设 `MCP_TIMEOUT=120000`。droid 宿主开关语义已实测（2026-09-16，droid 0.218.2）：**没有 `--enabled-tools` 这个旗标**，加 MCP server 用 `--add-tools MCP:notion,MCP:pku-sync`（agent_runner 已改并有测试钉住）；notion OAuth 凭证复用旧 daily.ps1 链的 keyring 存量，无需重新授权。剩余待验证：codex `exec -` 的 stdin 提示路径。
- **考点内容不可压缩（2026-09-16 拍板）**：转写笔记（`media.py` 窗口摘要 prompt）与讲次页（`lecture_notes_writer.md` §4.2 硬性要求）双层都加同一条红线——老师明示"要考 / 会考 / 小测可能会考 / 期末会问"的内容必须逐条保留原话并加粗，压缩只能作用于普通内容；写讲次页时必须再通读 `transcript.json` 把 notes.md 压缩丢掉的考点补回（notes.md 是压缩版，transcript.json 才是全量依据）。背景：认知心理学第一讲（9/9）老师口头明示 4 处考点，旧链页面全部漏收。

### 7.1 Phase 2b 设计输入（2026-09-15 用会话内 Notion MCP 只读核对）

- **Class Notes hub（`3d491b6f-53e1-8089-b7a7-ec1d6d2b06f4`）是顶层单树**：12 门课的课程页、学习中心、课堂录像笔记 hub、5 个数据库（考核 / 学习任务 / 资料索引 + 课表 `9aef2bf0-3104-4ff4-a8ce-0d6beb2c4656`、知识点与错题 `b761160f-23c3-419b-8209-0c999b4d6c4f`，其中课表当前流程不用，知识点与错题自 2026-09-16 起由晨检自测错题联动使用）全部是它的直接子页 → **OAuth 授权只需勾这一个页面**。
- 三库 schema 快照（运行时仍按 runbook 规则先 fetch schema 再建行，快照仅作设计参考）：
  - **2026秋季学习任务**：任务(title)｜任务类型(select: 课后复盘/作业/小测准备/考试复习/资料整理/周计划)｜优先级(select: 最高/高/中/低)｜截止日期(date)｜状态(status: Not started/In progress/Done)｜说明(text)｜课程(text)｜预计用时(number)
  - **2026秋季考核**：考核(title)｜类型(select: 期中考试/期末考试/随堂小测/论文/随堂考/上机考试/待确认)｜课程(text)｜日期(date)｜时段(select: 上午/下午/待确认)｜确认状态(select: 已确认/待确认/存在冲突)｜备注(text)
  - **课程资料索引**：资料(title)｜课程(text)｜资料类型(select: 课程手册/讲义/课堂课件/复习资料/往年题/作业/参考阅读/平台说明)｜处理状态(select: 待阅读/已索引/重点/待确认)｜来源路径(text)｜备注(text)
