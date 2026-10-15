# 强制产品 E2E 运行器

2026-10-09 按用户新要求，既有六旅程/十二项 Windows live/fault 的同步入口采用
`docs/PRODUCT_INCREMENTAL_SYNC_ACCEPTANCE.md` 和 `docs/WINDOWS_E2E_GOAL_PROMPT.md`。
风险映射见 `docs/PRODUCT_E2E_RELEASE_GATE.md` 首节。SCENARIOS 的 43 断言、47 故障
及 coverage fingerprint 保持；两新要求文件进入 source_digest，不能套用旧报告。

J2 先从实际窗口按课刷新，本课实际点击后的 90 秒包含 POST 和终态 GET；检查
其他课程缓存、学生文件、媒体及费用未变。后台全量使用独立任务 ID/scope、真实
官方课程集合、阶段/进度与每课结果；前台有效缓存仍可读，同课忙状态阻止重复，
异课入口可用。实际取消后同进程恢复，已完成课程不得重做或更改；只读/失败项
必须有具体原因，pending/running 不得冒充完成。保留原独立目录/来源/Notion 回读。

后台启动/进度上限 30 秒，最终观察上限 `30 + 90 × 官方课程数`，每课最多三次
尝试；物理服务请求每课最多 1452（来源与原真实 trace hash 见门禁文档）。这不是
延长单课 90 秒。真实 Retry-After 全额遵守、取消和恢复均从实际窗口操作；缺真实
故障请求、缺逐课结果或超过预算仍失败。J3 的重复修复及故障恢复只刷新本课，
原真实媒体、总结质量、收费、OSS、学生内容和未知写入断言继续执行。

元数据任务不隐式生成总结；显式总结任务显示本轮实际新建/复用数量，复用旧页
不能显示新建。旧全量九十秒失败和原四十二当前讲次审计保持不可变；新语义必须
取得新的实际安装包证据，当前主链 0/1、完整运行 0/12，不以开发检查代替。

`scripts/run_product_e2e.py` 固定收集目标的完整清单，无 -k、skip、xfail、选子集发布。
清单只有 6 条主旅程和 1 条真 Mac 跨平台旅程：Windows 12 次、Mac 14 次运行。
故障变体复用旅程及夹具，不为每项另建脚本；首次红→绿是历史证据，不要求以后重现旧 bug。
`product_e2e/native_driver.py` 的 `run_case(scenario, mode, context)` 已实现，并已执行
隔离安装候选包、真实 WebView2 操作、服务独立回读和实际请求边界故障。
各旅程仍有未完成能力与原生运行，不能把它们笼统描述为缺少 driver 或测试资源。
当前完整发布门禁 **0/12**，聚焦检查通过不代表整条旅程通过；最新事实见
`artifacts/windows-single-machine-20261006/execution-checkpoint.json`。

driver 从安装后的原生窗口操作，返回逐 assertion 与 fault_variants 的实际布尔结果；
每条 evidence 的 path 相对本次 run_dir，且具有 sha256 和 ui/oracle/network/runtime role。
result 必须含 entry=installed-native、mocked、injection、evidence、assertions、
fault_variants（fault 模式）、observed_live_hosts、metrics。独立 oracle 必须真实读回结果。
driver 必须拥有断言逻辑和证据采集，不能根据清单直接把所有 assertion 设置成 True。
runner 自动设置 case/artifact identity、时间和结果，全部通过后仍调用独立 gate。

执行配置通过 `--config` 指定一个放在仓库外的 JSON；定义测试账户凭据引用、
教学网 oracle、独立 Notion 测试 parent、费用/流量预算、驱动方式和旧版本安装包。
当前 Windows 单机范围禁止 secondary 原生实例，不执行真实教学网作业提交，Mac 不参与。
其他目标使用其原清单，不能把当前用户范围推广成默认跳过。不能把明文 token 存入报告。
runner 不输出 config。没有资源的场景应抛明确阻塞异常；runner 保存 failed，发布仍为红。

Windows 示例（路径替换为本次实际文件）：

```powershell
uv run python scripts/run_product_e2e.py --target windows --version 0.1.24 `
  --artifact "desktop/src-tauri/target/release/bundle/nsis/PKU All in Notion_0.1.24_x64-setup.exe" `
  --installed-path "C:/E2E/Installed/PKU All in Notion" `
  --config "C:/E2E/private/product-test-config.json" `
  --output-dir "C:/E2E/runs/<unique-run-id>"
```

候选未安装时 driver 先安装并验证版本与 SHA，不能读取已有正式安装假装是本次候选。
实际测试期间配置文件不变，禁止 driver 自行换包或服务端。每条场景都要包括配置约定
的预置/恢复/独立验证；不能以 missing fixture 跳过。运行器失败后先看缺驱动/缺资源，
这些失败不是产品已复现 bug 的证据。

结束后执行 Windows 验证脚本时必须传 `-E2EReport <run>/report.json`；Mac 调用
`product_e2e_gate.py --target macos`，真 Mac 原生测试的 driver 和发布接入由 Goal 补齐。
复用已有受控执行与实际发布入口完成可信证据及分发前校验，不再另建签名或发布平台。
这些接入尚未完成；当前本地门禁不能防管理员改脚本或伪造报告。
