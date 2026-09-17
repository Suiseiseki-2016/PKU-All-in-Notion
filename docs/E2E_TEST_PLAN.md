# 组合 E2E 测试规范

本文档定义课程同步与 Notion 工作台的端到端测试边界。目标不是让每个
Python 函数都访问真实服务，而是证明用户从一个入口开始，经过多个组件和
外部边界后，最终得到正确且可复核的结果。

## 1. 测试结论标准

一个 E2E 只有同时满足下面四项，才算通过：

1. **入口真实**：从实际用户入口开始，例如 `uv run pkutui`、`uv run
   pku-sync automate` 或 agent runbook。
2. **边界真实**：场景涉及的教学网、文件系统、agent host 或 Notion
   边界按场景要求使用真实服务或隔离替身，不能只调用一个内部函数。
3. **结果真实**：检查页面层级、页面内容、文件产物、状态和幂等结果，不能
   只检查进程退出码或 `TUI_RESULT=success`。
4. **证据完整**：保存场景编号、运行时间、输入范围、输出路径/URL、关键
   断言和失败日志。真实 Notion 写入必须记录页面 URL 和清理状态。

`TUI_RESULT=success` 只是 agent runbook 的最低协议。它不能替代下面的
页面和内容断言。

## 2. 测试分层

| 层级 | 目标 | 外部服务 | 运行频率 |
| --- | --- | --- | --- |
| 单元测试 | 解析、状态转换、payload、幂等键、错误判定 | 全部替身 | 每次提交 |
| 组件集成 | TUI + 本地数据、runner + fake agent、MCP 请求协议 | fake 教学网/fake Notion | 每次提交 |
| Windows TUI E2E | 真实 `uv run`、真实数据目录、真实教学网读取、键盘生命周期 | 教学网真实读；Notion 不写 | 每次运行机变更 |
| 隔离工作流 E2E | 多组件串联和失败恢复 | fake MCP 或专用测试 workspace | 每次发布候选 |
| 真实 Notion E2E | 写入、页面层级、内容、幂等、批改链路 | 官方 Notion MCP | 明确授权后或发布前 |
| 真实提交 E2E | 作业提交闭环 | 专用测试课程/账号 | 默认不运行，单独授权 |

不要把真实 Notion 写入放进普通 pytest，也不要为了覆盖内部函数而重复
创建页面。组合场景优先复用隔离 fixture，关键写入边界再用少量真实 E2E
确认。

## 3. 环境和数据隔离

### 3.1 本地 fixture

每个组合场景使用独立临时 `DATA_DIR`，至少包含：

```text
<course>/
  materials/
  announcements/
  assignments/
  recordings/<lecture>/
    recording.json
    transcript.json
    notes.md
    keyframes/
logs/
```

fixture 必须同时覆盖：

- 一个 `notes_ready` 讲次；
- 两个可以同时勾选的讲次；
- 一个缺少 `notes.md` 或转写未完成的讲次；
- 一个不可用录音；
- 已存在的 Notion 页面和已处理的报告。

### 3.2 Windows 运行机

真实 TUI 验证必须在 Windows 镜像上执行：

```bash
uv run python scripts/e2e_windows_tui.py
```

它可以读取真实教学网，但默认不进行 Notion 写入。验证结果必须记录：

- `uv run` 是否能自行同步并找到 `pkutui`；
- 本地状态是否先于教学网结果显示；
- 课程实时读取是否显示逐课进度；
- `Enter`、`Space` 和 `q` 是否完成预期动作。

### 3.3 Notion 测试页

真实 Notion E2E 只能使用标题带 `[E2E]` 的页面，并且必须：

- 创建在目标课程页下，而不是工作区根或不相关课程；
- 在页面内容中写明课程、讲次、运行时间和 E2E 场景编号；
- 保存页面 URL、父页面 ID 和关键内容断言；
- 测试结束后由操作者确认删除，或明确标记“保留用于排查”。

页面 URL 本身不代表归属正确。每个页面必须额外 fetch，断言：

```text
parent.id == expected_course_page_id
course == selected_course
lecture_scope == selected_recordings
```

## 4. 组合场景目录

### FLOW-001：资料到讲次页

**目的**：证明资料同步、录音处理、TUI 选择、agent runner 和 Notion
建页能够串联。

```text
同步课程资料
→ 下载录音
→ 转写/关键帧/notes.md
→ uv run pkutui
→ Enter 打开详情
→ Space 选择两个讲次
→ a 整理录音
→ 验证 Notion 讲次页
→ n 刷新状态
```

必须断言：

- 两个讲次都属于同一课程；
- 每个页面的父级是目标课程页；
- 页面包含正文、资料来源和关键帧；
- 老师明确的考点/作业没有被压缩丢失；
- TUI 报告显示真实 URL；
- 第二次运行不会创建重复讲次页。

### FLOW-002：小测完整生命周期

**目的**：覆盖当前最重要的 `z → g` 组合，而不是只测试“创建了一页”。

```text
选择两个 notes_ready 讲次
→ z 生成小测
→ fetch 小测页面
→ 检查父级、题目、来源和答案区
→ 第二次 z
→ 检查没有重复小测
→ 写入 E2E 测试答案
→ g 批改
→ fetch 小测页面
→ 检查分数、逐题批改和错题登记
→ n 刷新 Notion 状态
```

必须断言：

- 小测直接挂在对应课程页；
- 标题包含所有选中讲次范围；
- 题数在 3～5 题；
- 每题都有对应讲次或本地来源；
- 答案在教师区，不出现在作答区；
- 第二次生成不会重复创建相同范围的小测；
- 批改不会删除原答案；
- 错题写入正确的知识点/错题目标；
- 每个 agent 步骤都有 `TUI_RESULT=success`，且页面 fetch
  结果与该标记一致。

真实 Notion 命令：

```bash
uv run python scripts/e2e_notion_quiz.py
```

该脚本会选择最多两个讲次，创建页面，再用完全相同的范围运行第二次，
要求第二次复用同一页面，最后调用只读验收 runbook fetch 页面、解析权威
课程映射、检查父级、范围、题目、来源和教师区。仅创建一页不能作为最终验收。

小测批改真实 E2E：

```bash
uv run python scripts/e2e_notion_grade.py
```

该脚本准备四道题的 E2E 答案 fixture，运行两次批改，再只读验收总分、
逐题结果、原答案保留和错题登记。页面必须带 `[E2E]`，完成后手动清理。

### FLOW-003：评论驱动重整

**目的**：证明已有讲次页、评论读取、局部重整和幂等行为正确。

```text
准备一个 [E2E] 讲次页
→ 添加一条测试评论
→ o 读取评论并重整
→ fetch 页面正文
→ 检查评论要求已经落地
→ 再次 o
→ 检查没有重复段落或重复图片
```

必须断言：

- 只修改目标讲次页；
- 页面 URL 不变；
- 原正文、答案和来源没有被删除；
- 评论要求落地后有可定位的正文证据；
- 第二次运行保持幂等。

真实 Notion 命令：

```bash
uv run python scripts/e2e_notion_reorganize.py
```

它会创建一个 `[E2E]` 讲次页、写入测试评论、运行两次评论重整，再只读
验收父级、marker、原页面 URL 和幂等结果。

### FLOW-004：每日管道闭环

**目的**：覆盖确定性 Python 管道和两个 Notion agent 步骤之间的边界。

```text
uv run pku-sync daily
→ 检查 daily log 和 EXIT_CODE
→ review
→ lecture-batch
→ fetch 晨检记录和新增讲次页
→ uv run pkutui 查看报告
```

必须断言：

- daily 的日志、摘要和退出码互相一致；
- review 登记新的公告/作业，不重复登记；
- lecture-batch 只创建缺失讲次页；
- TUI 显示最新报告；
- 第二次完整运行不会重复写入。

隔离编排 E2E：

```bash
uv run python scripts/e2e_automate_workflow.py
```

它从 `automate` 入口运行成功和失败两种情况，用隔离替身模拟教学网和
agent 边界，断言 `daily → review → lecture-batch` 顺序、失败时仍继续
下游步骤，以及最终把 daily 失败码返回给调度器。

### FLOW-005：失败后恢复

**目的**：覆盖“退出码为 0 但没有实际写入”以及 MCP 暂时不可用。

```text
agent 返回 0，但缺少 TUI_RESULT
→ TUI 显示 blocked
→ 报告保留原始输出和原因
→ 恢复 MCP 或修正 target
→ 重试同一范围
→ success
→ 验证只产生一个最终页面
```

必须断言：

- blocked 永远不显示成功；
- 失败不会污染正式页面；
- 重试可以继续，不需要手动删除半成品；
- 最终结果只有一个目标页面/一条目标记录。

### FLOW-006：作业提交边界

**目的**：验证作业预览、已提交守卫和最终回执。

默认只跑到 dry-run：

```bash
uv run pku-sync submit list --course <测试课程>
uv run pku-sync submit text --course <测试课程> --content <测试作业> --file answer.md
uv run pku-sync submit file --course <测试课程> --content <测试作业> --file 作业.pdf
```

真实 `--yes` 只允许使用教学网专用测试作业和明确授权账号。必须断言：

- dry-run 不发送 POST；
- 已提交作业不会重复提交；
- 逾期会警告；
- 成功回执写回正确 Notion 任务；
- 网络失败时不误报完成。

隔离提交 E2E：

```bash
uv run python scripts/e2e_assignment_submit.py
```

该脚本覆盖干跑不 POST、multipart 请求、`destinationUrl` 回执和
`AlreadySubmitted` 守卫，文本与附件两条路径都在内（附件路径另外断言
`newAttempt` 取表单、FilePicker 字段和文件分段），但永远不连接真实教学网。真实 `--yes` E2E 需要
专用测试作业和明确的 `course_id/content_id`，不得把普通课程作业当测试目标。

## 5. 组合矩阵

至少覆盖以下维度。不是所有维度都要全排列，发布前使用 pairwise
组合，加粗场景必须单独覆盖。

| 维度 | 最低组合 |
| --- | --- |
| 讲次范围 | 单讲次、两讲次、混合顺序 |
| 页面状态 | 不存在、已存在、部分存在 |
| agent 结果 | success、blocked、退出码 0 但缺标记、超时 |
| MCP 状态 | 可用、启动慢、不可用后恢复 |
| 本地资料 | notes_ready、转写未完成、不可用录音 |
| Notion 内容 | 无评论、有评论、已有答案、有错题 |
| 重跑 | 首次、立即重跑、失败后重试 |
| 运行平台 | macOS 单测/组件、Windows 真实 TUI |

## 6. 证据和清理

每个场景至少保存：

```text
scenario_id
started_at / finished_at
selected_course
selected_recordings
local_data_root
agent_host
TUI_RESULT
output_report
notion_urls
expected_parent_ids
assertions
cleanup_status
```

失败报告不能只保存“agent 失败”。必须包括：

- 实际收到的操作 target；
- agent stdout/stderr 尾部；
- 页面 URL（若已创建）；
- 页面父级和当前状态；
- 下一次重试是否安全。

清理顺序：

1. 先 fetch 记录页面 URL、父级和内容摘要；
2. 确认页面标题包含 `[E2E]`；
3. 删除或移动到专用测试区；
4. 再次 fetch 确认清理结果；
5. 保留失败场景的页面时，报告中明确说明原因。

## 7. 当前覆盖与缺口

当前已经具备：

- 本地 250 个单元测试（新增面板 10 项：路由、单飞触发、409/404 边界）；（更新版）
- localhost 面板真实起服冒烟（2026-09-18）：`127.0.0.1:8791` 回环绑定、`/healthz`、
  `/api/status` 读真实数据树、无效动作 404；只读冒烟，未触发真实管道写入；
- `scripts/e2e_pkutui_entry.py`：**真实 `pkutui` console-script 入口** E2E
  （在 pty 中启动 `uv run pkutui`、渲染课程表、SIGTERM 干净退出）。填补了
  之前只测 `PkuSyncApp` 内部对象、从不验证用户真实命令的空白；
- Windows TUI 的课程表、详情、录音选择和退出 smoke E2E；
- 隔离组合工作流 `scripts/e2e_composite_workflow.py`：真实 Textual
  生命周期和 `Enter/Space` 键盘路径，再调用同一 App action 的
  `a → z（blocked 恢复）→ z → z（幂等）→ g → o`，验证页面父级、范围、
  批改结果和失败恢复；
- 隔离编排 `scripts/e2e_automate_workflow.py`：验证 daily 失败时
  `review` 和 `lecture-batch` 仍执行，并将失败码返回调度器；
- 一次真实 Notion 小测创建 + 页面层级/内容验收 E2E；
- Factory prompt 文件传输和 `TUI_RESULT` 判定。

当前仍缺：

- FLOW-001 的真实**建页**闭环（已有页面的幂等/blocked 半边已验证；建页
  半边要等一门课出现「notes 已就绪但 Notion 页不存在」的新讲次时才能
  首次验证）；
- FLOW-005 的**自定义 runbook 套嵌 claude 会话**（`run_e2e_reorganize_setup`
  由外部 claude 会话在内部再启一个 claude 子进程）会被宿主会话结束杀掉
  （现象 `[killed]`）。规避方式已验证：**从宿主直接 `uv run` 启动 runner**
  （不嵌套）。这是实验层面的机制约束，非产品缺陷；
- FLOW-006 真实测试作业提交（需专用测试作业 + 明确授权账号）。

已补齐（2026-09-18 追加）：

- **Notion OAuth login 完整真实闭环**：public integration（client_id
  `3ded872b…`）已注册，redirect URI `http://localhost:8765/callback`（Notion
  实测接受 localhost 的 http 回调，推翻 SERVICE_PLAN 旧假设"回调必须
  HTTPS"）。真实入口 `pku-sync notion login` → 浏览器真实授权（运营者本人，
  现有 workspace「Di Wu 的 Notion」，勾选课程 hub）→ `localhost:8765/callback`
  收到 code → 一次性交换成功 → `NOTION_TOKEN` 写入 `.env`（.gitignore 内）→
  新进程 `pku-sync notion check` 验证 whoami「PKU-All-in-Notion @ workspace」。
  共 5 轮：前 4 轮因用户离开超时（超时报错、可重跑行为符合设计），第 5 轮
  `--timeout 900` 完成。顺带修正：回调页三处文案去掉"回到终端"字样（目标
  用户是非 dev，页面引导回 pku-sync 而不是终端）。

已补齐（2026-09-17 追加）：

- **FLOW-004 真实闭环**：`uv run pku-sync automate --skip-process
  --skip-summary` 真实读教学网 + 真实写 Notion，daily→review→
  lecture-batch 全退出 0。review 登记 1 条真实新任务（发展心理学未签到
  补报）；06:00 与 10:00 两次运行共用同一条 Notion 晨检记录（幂等追加，
  未替换）；lecture-batch 全 10 候选已存在、0 新建。日志
  `data/logs/review_20260917.md`、`lecture_20260917.md`；
- **FLOW-005 setup 真实成功**：`run_e2e_reorganize_setup`（宿主直连
  claude）已建 `[E2E] CNS解剖(26-27学年第1学期)…
  recordings/2026-09-09_2026-09-09第5-6节`
  https://app.notion.com/p/3de91b6f53e1817389ddfed6ccc82e10
  （父级 CNS解剖→Class Notes，正文含 `E2E_REORGANIZE_BASELINE`，1 条
  comment）。重整成功/幂等 两个 run 撞到 claude 会话额度
  （`session limit`，resets ~14:10 SGT）**未执行**，净值待补跑后写入；
- real reorg 因额度 blocked 时**未写任何页面**（无副作用）。

已补齐（2026-09-16 授权 live 执行）：

- FLOW-002 真实批改：`e2e_notion_grade.py` 通过，创建 `[E2E] CNS解剖小测
  （第2轮）`，两次批改幂等，总分 4/8、逐题 2/2、0/2、2/2、0/2，错题
  marker `E2E_WRONG_Q2` / `E2E_WRONG_Q4` 登记到 `知识点与错题`，原答案
  未删除；
- FLOW-003 评论重整：`e2e_notion_reorganize.py` 通过，创建 `[E2E] 评论重整
  测试页`，评论要求落地为正文 `E2E_REORGANIZED_MARKER`，重跑幂等 no-op，
  父级为 `CNS解剖` 课程页；
- 只读查询 live 验证（2026-09-16）：`coursetable` 与 `grades` 直连真实服务
  跑通，并据此修掉三处真实数据才暴露的缺陷——门户 `(辅双)` / HTML 包裹的
  课程名解析、当前学期课表未发布时的误报（改为提示 + `--list-terms` /
  `--term`）、成绩接口 `/users/me` 非分页结构与 `grading.type` 字段名；
- 真实页面 URL、父级 ID 和断言结果保存在 `data/logs/e2e_*_20260916.md`；
- 清理状态：**success（2026-09-17）**——5 个测试页已由用户在 Notion 界面
  删除（移入废纸篓），随后对每个页面 ID 重新 fetch 复核，全部返回
  `deleted`：3 个 `[E2E]` 页（CNS解剖小测第2轮、评论重整测试页、CNS解剖
  小测第1轮）＋2 个错题页（小脑三分区、下丘脑摄食双中枢）。agent host
  无法自行删除（官方 MCP 无 delete/archive/trash 工具、`.env` 无
  `NOTION_TOKEN`），runbook `scripts/e2e_notion_cleanup.md` 的
  `E2E_CLEANUP=success deleted=5` 输出由人工删除＋本机 fetch 复核共同完成。
- 真实 FLOW-001/FLOW-005 运行（2026-09-17）：以真实 host=claude、真实
  runbook、真实数据对计算机网络 09-07＋09-09 两讲执行 `a` 动作，结果
  blocked——两讲讲次页已存在（第一讲、第二讲，父级均正确），按「不建重复
  页」规则未创建新页，无评论故重整无输入；`TUI_RESULT=blocked reason=…`
  如实报告且未写入。这真实证明了「建页幂等/不重复」「已有页面正确识别」
  与「blocked 不假装成功」（FLOW-005 前半）。报告：
  `data/logs/tui_notes_20260917.md`。真实建页本身已有长期证据：12 门课
  的 ready 讲次均已有父级正确的讲次页（计算机网络三讲、CNS解剖、心理测量、
  组织管理、发展、认知、咨询引论、计算概论B、信息安全引论，均已 fetch
  复核）。

在这些缺口补齐前，项目只能声称“关键入口有 smoke E2E”，不能声称
“课程到 Notion 的完整工作流已通过 E2E”。

## 8. 推荐执行顺序

1. 明确授权后运行 `e2e_notion_grade.py`，检查真实页面父级、成绩和错题。
2. 明确授权后运行 `e2e_notion_reorganize.py`，检查评论、正文 marker 和幂等。
3. 增加 Windows FLOW-001 的真实 `a` agent 闭环。
4. 将 daily/review/lecture-batch 组合成发布前验证。
5. 最后才考虑专用测试账号的提交 E2E；普通课程永远不带 `--yes`。
