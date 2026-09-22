# macOS / Linux 非开发者验收规程（VALIDATION PROCEDURE）

本文件是 macOS / Linux 的真机验收规程（对应契约 `VAL-PKG-013`）：让一位
**非开发者**用户，在一台干净的 macOS 或 Linux 机器上，按下面的步骤完成
安装 → 启动 → 激活 → 同步 → 浏览，并把每一步的**证据**存档。每一步都写明了
「要保存什么」和「失败意味着什么」。

关联文档：试点学生指南 [`PILOT_GUIDE.md`](PILOT_GUIDE.md)；开发者手册
[`USER_GUIDE.md`](USER_GUIDE.md)；跨平台保证（无 OS 专属代码）与三平台 CI
由仓库的 `tests/test_os_agnostic_scan.py` 与 `.github/workflows/ci.yml`
维护（见 README）。

> **验收先决条件（在本机执行前确认）**
>
> 1. 机房使用的公开软件源已发布本应用最新版：
>    `https://pku.aeoluswu.info/packages/simple/pku-course-sync/`
>    能打开并且列出 wheel（`curl` 一下即可，无需登录）。
> 2. 有一个未使用过的**兑换码**（由试点运营者发放）。
> 3. 有 Web 浏览器（面板会自动打开）。
>
> 如果本规程因为「手边没有 macOS / Linux 机器」而无法执行，见文末的
> 「验收延期（Deferral）」一节——延期必须由用户**明确接受**，不能默认跳过。

---

## 规程总览

```text
S1 安装 uv             → 证据：uv --version 输出
S2 装 Python 3.11      → 证据：uv python list 输出
S3 安装应用            → 证据：终端输出 + uv tool list
S4 启动面板（launcher）→ 证据：启动行（含端口端口）+ 浏览器截图
S5 /healthz            → 证据：curl 输出 {"ok":true} + netstat/ss 回环行
S6 激活                → 证据：面板激活成功截图（含剩余分钟）+ .env 行（脱敏）
S7 同步 + 目录浏览      → 证据：同步完成截图 + 课程/讲次/资料/练习截图
```

全程大约 15–25 分钟。每一步的「失败意味着什么」在步骤内给出；任何一步不
符合预期就停下来，把现象截图发给试点运营者（不要把账号密码、兑换码、
token 发出去）。

---

## S1 安装 uv（官方安装脚本）

打开「终端」（macOS：启动台 → 其他 → 终端；Linux：Ctrl+Alt+T 或应用
菜单里的 Terminal），执行：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装脚本结束后，**关掉当前终端再开一个新的**（让 `uv` 进入 PATH），然后
验证：

```bash
uv --version
```

- **要保存的证据**：`uv --version` 的输出（终端截图或复制文本）。
- **失败意味着什么**：
  - 网络不通 / 脚本被墙 → 重试或换网络；`curl` 报错时先查网络。
  - `uv: command not found` → 新终端没生效；换新的终端窗口再试，或在
    当前终端执行 `source ~/.local/bin/env`（zsh/bash）后再试。
  - 其他错误 → 截图发给运营者。

## S2 安装 uv 管理的 Python 3.11

```bash
uv python install 3.11
```

- **要保存的证据**：命令输出（`Installed Python 3.11.x` 或
  `Python 3.11 is already installed`），然后 `uv python list` 的输出里
  能看到 `cpython-3.11.x-...`。
- **失败意味着什么**：通常是网络问题（Python 解释器从 GitHub releases
  下载）。重试即可；仍失败则截图。

> **macOS iCloud 说明（复用旧结论前先读这节）**
>
> 旧版开发环境文档里有一条 macOS 专属故障：项目虚拟环境 `.venv` 放在
> iCloud 同步目录（桌面 / 文稿）时会染上 `hidden` 标记，导致 Python 的
> `site` 模块跳过可编辑安装的 `.pth`，`uv run pku-sync` 找不到模块。
> **该故障模式对本安装方式不适用**，因为：应用通过 `uv tool` 安装，运行
> 环境位于 `~/.local/share/uv/tools/`（不在 iCloud 同步范围），且安装的是
> 普通 wheel 包（非可编辑安装）。**无需** `chflags -R nohidden`，也**无需**
> 把 `.venv` 软链到外部。这个旧故障的根治方案只在开发者从源代码仓库
> 克隆开发时才相关（见 USER_GUIDE）。

## S3 安装应用（按名字从公开软件源）

```bash
uv tool install --managed-python --python 3.11 --index https://pku.aeoluswu.info/packages/simple/ pku-course-sync
```

安装结束后验证：

```bash
uv tool list
```

- **要保存的证据**：`uv tool install` 输出的最后几行 + `uv tool list`
  里出现 `pku-course-sync v0.1.0`（或当前发布版本）与 `pku-sync`、`pkutui`
  两个命令。
- **失败意味着什么**：
  - 找不到包 / 连接公开软件源失败 → 软件源未发布或网络问题；先
    `curl -s https://pku.aeoluswu.info/packages/simple/pku-course-sync/`
    看是否能打开。
  - 依赖下载失败 → 网络；可设置镜像加速后重试（见 PILOT_GUIDE §5）。
  - 其它错误 → 截图发给运营者。

## S4 启动面板（launcher）

先创建个人数据目录并进入（这一步等价于 Windows 快捷方式的「固定工作
目录」，保证 `.env` 和数据落在用户目录、安装目录永远干净）：

```bash
mkdir -p ~/PKU-All-in-Notion
cd ~/PKU-All-in-Notion
pku-sync panel
```

面板启动后会自动打开浏览器。终端里会出现一行启动信息，例如：

```text
面板已启动：http://127.0.0.1:8791/
```

- **要保存的证据**：终端里该启动行的截图；浏览器打开的页面截图。
- **失败意味着什么**：
  - `本地面板端口 8791、8792、8793 都被占用：关闭占用端口的应用后重试。`
    → 有程序占用了这三个回环端口之一；关闭占用程序后重试。（本机只会有
    回环监听，面板绝不会绑定到 0.0.0.0。）
  - 其它报错 → 截图发给运营者。

## S5 /healthz 与回环监听证明

保持面板运行，新开一个终端：

```bash
curl -s http://127.0.0.1:8791/healthz
```

macOS 用：

```bash
netstat -an | grep 8791
```

Linux 用：

```bash
ss -ltn | grep 8791
```

- **要保存的证据**：`curl` 的输出必须**恰好**是 `{"ok":true}`；监听行
  必须是 `127.0.0.1:8791 ... LISTEN`（macOS/Linux 上若端口号是 8792/8793，
  说明 8791 当时被占用——正常，地址仍是回环）。
- **失败意味着什么**：
  - curl 无输出 / 连接被拒 → 面板没在跑；回 S4。
  - 监听行出现 `0.0.0.0:8791` 或 `::8791` → **异常**，立即停止使用并截图
    发给运营者（面板被要求只绑回环，永远不该出现在非回环地址上）。

## S6 激活

浏览器回到面板：点击主页上的「**打开学生面板**」进入
`http://127.0.0.1:<端口>/app`，输入运营者发的**兑换码**，点「**立即激活**」。

- **要保存的证据**：
  1. 激活成功页截图（应显示类似「激活成功，剩余 N 分钟。」与转写分钟 /
     AI 点）；
  2. 个人数据目录里的 `.env` 只保存了这几行（**只截脱敏行，不要贴完整
     token**）：

     ```text
     TRANSCRIPTION_BACKEND=cloud
     CLOUD_TRANSCRIBE_URL=https://pku.aeoluswu.info/v1/transcribe
     PLATFORM_TOKEN=<只看前几位，其余打码>
     ```

- **失败意味着什么**：
  - `兑换码无效或已使用` → 跟运营者核对兑换码；
  - `无法连接云端服务` → 网络问题；
  - 其它提示 → 截图（脱敏）发给运营者。
  - **如果 `.env` 里没有 `TRANSCRIPTION_BACKEND=cloud`/`PLATFORM_TOKEN`**
    （激活说成功但没写入）→ 异常，截图并发给运营者。

## S7 同步 + 目录浏览

激活后，在面板里点击 Notion 连接行的「重新连接」完成授权（浏览器里点
允许），然后等待索引同步完成。

- **要保存的证据**（每张都截图）：
  1. 总览页（统计卡 + 课程卡片）；
  2. 任一课程页 + 讲次列表；
  3. 任一讲次的资料视图（`按讲次` / `按资料类型` / `全部资料` 任选其一）；
  4. 练习目录（有内容或空态均可）。
  同步完成时顶部显示「同步完成」。
- **失败意味着什么**：
  - 同步显示「同步遇到问题」而**不是**「同步完成」→ 这是既定行为
    （失败的同步永不冒充成功）；把画面截图发给运营者。
  - Notion 显示「已断开」→ 授权未完成或已撤销；点「重新连接」再来一次。
  - 页面打开后**找不到**任何内容（空白练习/空目录属正常状态，正文写在了
    Notion 页面里，客户端只显示索引）。

---

## 验收延期（Deferral）——用户必须明确接受

如果当前**没有可用的 macOS / Linux 机器**，本规程无法当场执行。延期
**不是默认选项**，必须由用户明确接受。请用户确认下面的原话（或内容等价的
明确表态）：

> 我已了解「PKU All in Notion」本试点的 macOS / Linux 真机验收尚未执行。
> 现有证据包括：三平台 CI（compileall + 全量假后端测试，见
> `.github/workflows/ci.yml`）、无操作系统专属代码的代码级扫描
> （`tests/test_os_agnostic_scan.py`）、本机（Windows）对打包成品的逐条
> 命令实测（见 docs/PILOT_GUIDE.md 附表和本次验收证据包），以及本
> 验收规程文档。我接受在获得 macOS / Linux 机器后，再按
> docs/MACOS_LINUX_VALIDATION.md 执行真机步骤并把证据存档；在真机证据
> 补齐之前，将 macOS / Linux 的安装与启动标注为「已文档化、未真机验证，
> 用户已接受延期」。

一旦有机器可用，按上面的 S1–S7 执行即可；每步的证据名建议写成
`S<N>-<步骤>-<日期>.png`（如 `S5-healthz-2026-09-23.png`），存放在当次
验收的证据文件夹里。**真机执行证据会自动升级本验收记录**：从「用户接受
延期」变为「真机通过」。
