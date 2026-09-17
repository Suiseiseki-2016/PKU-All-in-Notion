# 讲义（讲次页）批量整理运行手册 (batch mode)

> 你由 daily 06:00 pipeline **自动**调用：`pku-sync automate` 的最后一步，紧跟 MCP 晨检（`daily_review_mcp.md`）之后；也可手动 `pku-sync lecture-batch` 触发。
> 目标：扫描全部 12 门课 + 对照 Notion 课程页下子页列表，**幂等**补建所有"notes.md 已存在但讲次页还没建"的缺漏。已存在的全部跳过；不在 batch 里 overwrite——重整仍走 `lecture_notes_writer.md mode=overwrite` 手动触发。

## 0. 触发方式（由 agent_runner 配好，宿主无关）

`pku_sync/agent_runner.py` 把本手册全文 + 运行时上下文（project_root、data_root）交给 `.env` 里 AGENT_HOST 的无人值守会话（factory=droid / claude / codex 皆可），整体 60 分钟超时；报告落 `logs/lecture_YYYYMMDD.md`。失败只记日志、不影响 daily 退出码（建页幂等，下次重试）。

- 无需 stdin 参数；**data_root 一律以运行时上下文为准**（Windows 为 `E:\pku-course-data`，macOS 手动跑时为项目 `data/`）。
- 数据访问：直接读 data_root 文件树（`notes.md` / `transcript.json` / `recording.json` 为文本，`keyframes/` 为待上传的二进制图）；pku-sync MCP 工具可用于交叉核对。
- Notion 读写一律走官方 notion MCP 工具。

> 若没有 Notion MCP 工具 → 回复 `NO_NOTION_TOOLS` 立即结束。

## 1. 12 门课映射（唯一来源）

data_root = `E:\pku-course-data`。

| 数据树文件夹 | Notion course_page_id | 备注 |
| --- | --- | --- |
| CNS解剖_26-27学年第1学期 | `3d691b6f53e180d89237f1cffcaff287` |  |
| 中苏关系及其对中国社会发展的影响_26-27学年第1学期 | `3db91b6f53e1813a93dcddcd30c176c6` | 9/11 起内容区开放 |
| 信息安全引论_26-27学年第1学期 | `3da91b6f53e181b38634dfe3006d851a` |  |
| 发展心理学_26-27学年第1学期 | `3d591b6f53e18044b3f8f0b49afac2ea` |  |
| 开源软件技术_26-27学年第1学期 | — | 无课程页（教师未开放工具），**跳过整门课** |
| 心理咨询与治疗引论_26-27学年第1学期本研合上 | `3d491b6f53e1802e8759d6a5e38ec783` |  |
| 心理测量_26-27学年第1学期 | `3da91b6f53e181f29f7bc92fcb32ce79` |  |
| 组织管理心理学_26-27学年第1学期 | `3d691b6f53e180c39b74fb4782723523` |  |
| 计算机网络_26-27学年第1学期 | `3d791b6f53e181a7a7c1f379bbde9086` |  |
| 计算概论_B_26-27学年第1学期 | `3d691b6f53e1818db116ec325c293bfb` |  |
| 认知心理学_26-27学年第1学期_101577 | `3d591b6f53e1803f99c4e79453198c04` | 通知无权限；若有 recordings 也按此页登入；不报错 |
| 认知心理学_26-27学年第1学期_101578 | `3d591b6f53e1803f99c4e79453198c04` | 与 101577 共用一 course_page（分班不同教师；101578 才是有完整录屏的那一门） |

## 2. 扫描

按 12 门课映射表顺序逐门处理（无课程页的整门跳过）：

1. 读 `data_root/<course_folder>/recordings/` 子目录。每个子目录 = 一个 slug。
2. 准入检查（逐 slug）：
   - `recordings/<slug>/notes.md` 存在且 ≥ 5 KB；
   - `recordings/<slug>/transcript.json` 可 parse（json 不为空）；
   - `recordings/<slug>/recording.json.unavailable_reason` 为空。
   - 任一不通过：跳过，记入「失败」段报告（带原因）。
3. 拉课程页 `course_page_id` 当前子页面列表（`notion-fetch`），用于幂等判定。

## 3. 幂等判定（决定是否建页）

构造新标题：`《第<N>讲 · <(course_name)>（<YYYY-MM-DD> 第<X-Y节）>`，其中：

- N = (当前子页最大 N + 1)；无任何讲次子页时 N=1，名为「第一讲」。
- `YYYY-MM-DD` + `第X-Y节` 从 `recording.json` 取：
  - `recorded_at` → YYYY-MM-DD（如 `2026-09-09 08:00:00` → `2026-09-09`）；
  - `title` → 第 X-Y 节（如 `2026-09-09第1-2节` → `第1-2节`）；
  - `course_name` 从映射表 "数据树文件夹" 处理：去 `_26-27学年第1学期` / `_26-27学年第1学期本研合上` 等后缀；分班课程只取主名（`认知心理学` / `认知心理学(101578)` 之类按既有讲次页命名习惯）。

判定：
- 课程页子页列表中**已存在** YYYY-MM-DD + 第X-Y节 完全一致的标题：**跳过**，归「跳过」段报告（带原 URL）。
- 否则：进 §4 建页流程。

## 4. 建页（每个待建 slug 严格按 §6 落地）

页面规范**完全照搬** `lecture_notes_writer.md` §4（开头 1 段 + 大主题段 + 📌 待核实点 + 自测题 + 资料来源；流水笔记风格，考点/重点/作业在正文加粗，无考点速览板块）。**不要**创新样式。

规范 2026-09-16 已加详细度硬性要求，批量建页同样适用，务必先读 writer §4 全文再动手：

- 信息完整优先于篇幅：3~6 个大主题段，每段 8~18 条主 bullet，**一条 bullet 只讲一件事**，例子/数据/老师原话下沉为子 bullet。
- 考点、作业与考核信息、**老师口头布置的任务/DDL/小测**三类必须逐条进正文、保留老师原话并加粗；写完必须重读 `transcript.json` 按关键词核对补漏。
- 编程类课程（如计算概论B）**必须**用代码块收录老师演示过的完整代码与报错；对比/分类信息用表格。
- 关键帧每页 6~12 张。
- 每讲 3~5 道自测题（writer §4.4）：优先考三类加粗内容，答案必须能在本页正文找到。

执行步骤：

1. `notion-create-pages`，`parent.page_id = course_page_id`，`properties.title = <构造标题>`，content = Notion-flavored markdown（按规范拼）。icon **不**设。
2. 课堂录像笔记 hub（`3d791b6f53e18112a603e15256c706d5`）下对应课程子页**追加**一行：`《第N讲 · <course>（YYYY-MM-DD 第X-Y节）》→ <url>`。
   - hub 子页缺失 → **不创建**新 hub 子页；仅在「失败」段报告"hub 没该课程子页"。
3. 学习中心（`3d691b6f53e181d3a394f9b10e21920e`）「每日检查记录」末尾追加（如有已有今日条目则加子项；否则新建）：
   `+ 讲义整理（管道自动，batch=create，YYYY-MM-DD）：《第N讲 · <course>（...）》→ <url>，关键帧 X 张、来源路径：<data_root>/<course>/recordings/<slug>/`

任一步失败：
- 第 1 步失败 → 「失败」段记录；该 slug 当日不补；下次 batch 重试可重建（幂等判定仍把它当作"未建"）。
- 第 2/3 步失败 → 第 1 步已生效，**保留子页**；在「失败」段记录后续因（如 hub 缺失）；不阻塞后续 slug。

## 5. 控制台回复（最终对外四段）

- **扫描**：覆盖 12 门课，逐门列：本课扫描到 N 个 slug，过滤 K 个（无权限/转写未完），剩余候选 M 个。
- **创建**：列出刚创建的讲次页 URL 列表（含关键帧张数）。
- **跳过**：列出幂等跳过的讲次（slug → 已存在 URL）。
- **失败**：列出扫描/准入/建页失败的 slug + 错误摘要 + 补救路径（不阻塞其他讲次）。

格式样例：
```
扫描：12 门课覆盖完成，候选总数 X（X1 待建，X2 跳过，X3 失败）。
创建：
  - 计算机网络《第一讲 · ...（2026-09-07 第5-6节）》→ https://notion.so/<url>，关键帧 82 张
  - …
跳过：…（已存讲次页 URL）
失败：
  - 开源软件技术：教师未开放工具，整门课跳过
  - 认知心理学_101577/2026-09-08：recording.json.unavailable_reason 非空，无权限
  - …
```

## 6. 失败模式

- **没有 Notion MCP 工具** → `NO_NOTION_TOOLS` 立即结束。
- 单 slug 准入失败 / 建页失败 → 不阻塞其他 slug，记录到「失败」段。
- 单 Notion create 失败（限流） → 该 slug 跳过；下次 batch 重试仍会重新尝试（幂等判定还会把它当作"未建"）。
- 60 分钟整体超时 → agent_runner 终止会话，已建好的子页**保留**（不回滚）；`logs/lecture_YYYYMMDD.md` 里留下已建到一半的进度，下次 batch 幂等续建。

## 7. 红线

- **不 overwrite** 已存在的讲次页（用户手动 mode=overwrite 才能这么做）。
- **不删**任何页面 / 数据库行 / 简报行。
- **不**主动跑转写、不发起 schtasks、不写 `E:\pku-course-data`（只读）。
- **不**"猜测"建页：任一输入不齐 / 命名不齐就跳过 + 报告，不写 placeholder。
- **必须**内嵌关键帧图（按 `lecture_notes_writer.md` §4.2 选帧、§5 第 5b 步上传，每页 6~12 张）；漏嵌视为不合规。
- 关键术语未定稿前不要写 placeholder 文字（提示：用「待核」而不是"未知"）。
