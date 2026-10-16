# pku-course-sync

北京大学教学网（Blackboard）本地课程工作台：查看课程、资料和作业，按需整理课堂录像并保存到本地。

纯 Python 单机项目：同一套代码在 Windows / macOS / Linux 上行为一致，不调用任何操作系统的专属接口。

## 产品状态

桌面端提供账号登录、教学网课程与资料作业的本地同步、关键帧与课件结合的课堂笔记、本地课程总结阅读，以及本地只读 MCP AI 工具连接。完整真实服务 E2E 的验收状态见 [`docs/PRODUCT_E2E_RELEASE_GATE.md`](docs/PRODUCT_E2E_RELEASE_GATE.md)。

## 安装（学生 / Pilot）

试点学生请直接看 **[`docs/PILOT_GUIDE.md`](docs/PILOT_GUIDE.md)**（非开发者
指南：按 OS 安装、激活、配额、恢复与支持边界）。Windows 用户装完
`PKU-All-in-Notion-Setup-<版本>.exe` 后从「开始菜单 / 桌面」快捷方式启动即可；
macOS / Linux 用户等效于下面三条命令（uv → uv 管理的 Python 3.11 → 按名字
从公开软件源安装应用）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 然后重新打开终端
uv python install 3.11
uv tool install --managed-python --python 3.11 --index https://pku.aeoluswu.info/packages/simple/ pku-course-sync
mkdir -p ~/PKU-All-in-Notion && cd ~/PKU-All-in-Notion && pku-sync panel
```

macOS / Linux 的真机验收步骤见 [`docs/MACOS_LINUX_VALIDATION.md`](docs/MACOS_LINUX_VALIDATION.md)。
下面的 `## 安装与启动` 一节是开发/自托管形态的说明，学生不需要。

## 桌面端首次使用

首次打开页面会显示启动引导：登录并验证邮箱 → 保存教学网账号并更新课程目录。可以跳过，也可以从「使用引导」重新打开；浏览器或桌面端会在本地记住已查看状态。

日常从「我的课程」查看讲次、作业、通知与资料，按需整理课堂笔记。旧学习资料和旧主页模式已退役，对应旧 CLI 命令已删除。尚未完成的小测、批改及相关确认入口暂时隐藏，已有数据保留。

## 功能

- **课程发现与同步**：IAAA 登录，自动识别当学期课程；同步公告、课件、作业到本地
- **课堂录像**：枚举录播、解析真实媒体地址、断点续传下载；HLS 走分片并发下载（AES-128 自动解密，ffmpeg 只作兜底）；发布讲次页时可将“回看”标记生成 ≤5 MB 的短视频块，失败时保留文字时间标记
- **转写与加工**：faster-whisper 转写全文、OpenCV 抽取关键帧、LLM 生成结构化讲次笔记
- **知识点图文笔记**：按课堂知识点分段整理，再按讲述顺序直接合并所有段落、配上对应课堂画面；不再让模型重写整堂笔记。已完成段落可复用，中断后继续整理。
- **考前课程总结**：桌面课程页可生成课程总结，汇总已确认的讲次笔记、正式作业与资料缺口。请核对教学网和老师最新通知。
- **作业提交**：`uv run pku-sync submit` 预览并保存文本或附件草稿，`--yes` 后仍需二次确认；与作业中心共用跨设备提交许可和教学网回执保存（运行手册见 `scripts/assignment_submit.md`）。
- **课表与成绩（只读）**：`uv run pku-sync coursetable` 读门户个人课表（`--list-terms` 列可选学期、`--term` 查指定学期），`uv run pku-sync grades` 读教学网成绩明细；两者只读，不写任何内容
- **一键自检**：`uv run pku-sync doctor` 七项三态检查（配置/凭据/依赖/宿主/日志/报告/磁盘），fail 项退出码 1
- **本地面板**：`uv run pku-sync panel` 启动当前课程工作台，只绑定本机 127.0.0.1。


## 安装与启动

项目使用 `uv` 管理跨平台环境。首次安装或明确升级依赖时执行一次：

```bash
uv sync --all-extras
uv run pku-sync setup         # 教学网账号与本地只读 MCP 宿主配置
```

`uv run pku-sync setup` 用于配置 CLI 与 agent 宿主的本地只读 MCP。桌面端或 `uv run pku-sync panel` 使用页面内的引导：注册或登录平台账号，保存教学网账号，读取课程并同步到本地。兑换码在登录后用于充值；教学网读取失败可以重试，平台登录和已保存的账号不会失效。高级配置见 `.env.example`，例如 `DATA_DIR`、课程筛选和媒体处理选项。

日常启动不需要激活虚拟环境，也不需要 profile 函数、包装器、`UV_NO_SYNC` 或 `--no-sync`：

```bash
uv run pku-sync doctor        # 运行环境自检
```

媒体转写、关键帧和 OpenAI 依赖属于默认项目依赖，所以普通 `uv run` 会保持完整环境。依赖变更后重新执行 `uv sync --all-extras`，不要在运行机上手工安装或卸载媒体包。

## 使用

```bash
uv run pku-sync setup               # 首次配置（账号 + MCP 宿主，一条命令）
uv run pku-sync discover            # 列出账号可见课程及本学期选中课程
uv run pku-sync sync                # 同步公告/课件/作业 + 录像元数据
uv run pku-sync download            # 下载已索引的录像（断点续传；可加 --workers 2）
uv run pku-sync process             # 转写 + 关键帧 + LLM 笔记（云端可加 --workers 2；本地 Whisper 保持串行）
uv run pku-sync doctor              # 一键自检：配置/凭据/依赖/宿主/日志/报告/磁盘
uv run pku-sync panel                # 本地面板（只绑 127.0.0.1 的学生面板；FastAPI/uvicorn 属默认依赖）
uv run pku-sync submit list --course 认知心理学
uv run pku-sync submit text --course … --content … --file answer.md  # 保存预览草稿；--yes 后仍需二次确认
uv run pku-sync submit file --course … --content … --file 作业.pdf   # 附件预览，与作业中心共用共享许可和回执
uv run pku-sync coursetable                     # 个人课表（--list-terms 列学期，--term 25-26-2 查历史学期）
uv run pku-sync grades                          # 成绩明细（--json 输出结构化数据）
uv run pku-sync mcp serve           # 启动本地只读课程 MCP server
uv run pku-sync mcp configure claude         # 注册本地只读 MCP 服务
uv run pytest                       # 运行测试
```

### TUI 工作台

完整自动化测试：

```bash
uv run pytest
```

首次使用页面流程见 `tests/test_first_run_course_setup_browser.py`，覆盖保存账号、同步失败重试、本地课程工作台和重启恢复。浏览器测试模拟外部服务；真实服务验收由 `product_e2e/` 与 `scripts/run_product_e2e.py` 执行，两者结果单独报告。

## 目录结构

```
pku_sync/     核心包：auth / discover / materials / recordings / media / pipeline / store / cli
scripts/      当前启动、发布与验收工具
docs/         设计与验收文档（组合 E2E 见 E2E_TEST_PLAN.md）
tests/        pytest
data/         课程数据（gitignored，按课程分目录）
```

## 运行环境

使用桌面安装包或 `uv run pku-sync panel` 启动。课程数据保存在 `DATA_DIR`；读取课程目录后即可使用本地课程工作台。当前仅提供本地服务，课堂笔记与课程总结可在阅读器中下载为 Markdown 文件。

## 讲次笔记

讲次整理从当前课程页发起，保留原始转写、资料依据和可核对的引用。未完成的小测界面暂不展示。
