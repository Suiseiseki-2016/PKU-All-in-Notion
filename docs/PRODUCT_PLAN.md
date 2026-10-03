# 正式产品化方案

## 文档状态与免责声明

- **状态**：已确认的正式产品目标与设计，供产品、客户端、服务端和运营实施使用。
- 当前 `0.1.1` 是已发布的**技术试点**，不是本文目标的正式桌面产品。
- 本文描述目标、边界和待办，不代表这些能力已经实现，也不改变当前试点的操作方式。
- 首发桌面壳同时覆盖 Windows 与 macOS；安装包形态不同（exe / DMG），产品窗口与本机服务是同一套约定。Linux 可后接，不阻塞双端首发。

## 试点与正式产品的差异

| 项目 | 当前 0.1.1 技术试点 | 正式产品目标 |
| --- | --- | --- |
| 启动方式 | 安装后启动本地服务并打开浏览器面板 | 独立桌面窗口（Win / macOS 同一套壳），启动直接进入学生界面 |
| 界面 | 浏览器学生面板，另有高级 TUI | 产品窗口内的学生界面，不展示工程状态或日志页 |
| 账号 | 一次性激活码创建匿名平台会话 | 邮箱+密码产品账号；邮箱验证前只能登录和查看状态 |
| 兑换码 | 试点激活码直接激活匿名会话 | 兑换码不创建账号，只给已登录账号增加额度和 AI 点数 |
| 额度 | 试点按激活结果提供转写分钟和 AI 点 | 月度订阅按月滚动重置；一次性充值包成功兑换起 12 个月有效 |
| 卸载 | 依赖试点本机目录和 `.env` 数据保留方式 | 普通卸载保留本机数据；彻底清除需明确勾选和二次确认 |
| 后台 | 试点运营发码和现有服务能力 | 独立 admin 子域，Cloudflare Access + MFA，账号/订阅/额度/兑换码/审计管理 |

## 正常用户流程

1. 安装 Windows 或 macOS 桌面产品，启动后直接进入学生界面；产品通过本地 FastAPI 子进程提供本机能力。
2. 注册或登录产品账号（邮箱+密码）。邮箱注册后可登录和查看状态，但邮箱验证完成前不能兑换额度、使用 AI、转写或执行其他敏感操作。
3. 在本机保存 PKU IAAA 账号密码到操作系统凭据库，完成课程系统认证。凭据不上传平台服务器。
4. 通过系统外部浏览器完成 Notion OAuth；Notion token 只保存在本机。
5. 订阅生效，或在已登录且已验证邮箱的账号下兑换充值码。
6. 在学生界面同步课程并使用转写、AI 和学习功能；按额度模型扣减并展示状态。
7. 产品更新由桌面安装/更新机制完成，正常更新和重启不要求重新认证。
8. 普通卸载保留本机数据；如用户明确选择彻底清除并二次确认，则清除本机数据，重装后重新认证。

OAuth 和 Notion 页面均可继续使用系统外部浏览器，不在产品窗口内承载授权页面。

## 账号与身份边界

```text
产品账号（邮箱 + 密码）
  ├─ 服务端保存 Argon2id/bcrypt 哈希；邮箱验证、订阅、额度、审计归属
  ├─ 登录/重置限流；忘记密码使用一次性邮箱链接
  └─ 不等同于 PKU IAAA，也不等同于 Notion 身份

PKU IAAA（本机课程系统认证）
  └─ 账号密码仅存本机操作系统凭据库，不上传平台服务器
     Windows = Credential Manager / DPAPI
     macOS   = Keychain
     客户端只依赖 os_keyring 抽象，不直接调用某一 OS API

本机 Notion（OAuth）
  └─ token 仅存本机；清除本机数据后必须重新连接，不删除远程 Notion 内容

后台管理员
  └─ 通过独立 admin 子域登录，Cloudflare Access 邮箱白名单 + MFA；权限独立于学生账号
```

在 PKU 官方 SSO 未获批前，不得把当前逆向密码流程搬到服务器。未来 PKU 提供或批准正式浏览器 SSO 后，才评估接入正式 SSO。

## 额度与兑换码模型

- 产品账号是额度归属主体；兑换码不创建账号。
- 邮箱验证完成后，已登录用户才能兑换码、使用 AI、转写或进行其他敏感操作。
- 月度订阅额度从订阅生效日起按月滚动重置，具体套餐额度由运营配置。
- 一次性充值包在成功兑换时开始计时，12 个月有效；充值包作为独立余额保存。
- 消耗时优先使用最早过期的可用额度，避免较早到期的充值包被延后浪费。
- 转写额度与 AI 点数分别计量、扣减和审计；兑换成功、使用、过期和撤销均应可追溯。

## 邮箱验证与发信配置

验证和重置都是事务邮件：注册后发一次性验证链接，忘记密码发一次性重置链接。链接约 30 分钟过期；同一邮箱必须限流。未验证账号可以登录看状态，不能兑码、转写或使用 AI。

发信走可替换的 `send_email()` 适配器，服务端不绑定某一家 SMTP。首选 **Resend**（免费档 100 封/天、3000 封/月，够一个学期的验证和重置）。开发环境可以只写日志或打到本机 Mailpit，不消耗额度。不要用个人 Gmail SMTP 当产品发件人。

### 推荐域名

不要用根域 `pku.aeoluswu.info` 直接发信，以免和网站/中转的信誉绑在一起。使用独立发信子域：

```text
发信域     mail.pku.aeoluswu.info
发件人     noreply@mail.pku.aeoluswu.info
验证落地页 https://pku.aeoluswu.info/verify?token=…
重置落地页 https://pku.aeoluswu.info/reset?token=…
```

验证页由平台服务端处理，不经过学生客户端。

### 运营配置步骤（Resend + Cloudflare）

1. 在 [resend.com](https://resend.com) 注册免费账号（不需要信用卡）。
2. Dashboard → **Domains** → **Add Domain**，填入 `mail.pku.aeoluswu.info`。区域选离收件人近的（亚太学生可选新加坡，若控制台提供）。关闭 open/click tracking：验证和重置邮件不需要追踪。
3. Resend 会给出一组 DNS 记录（通常是多条 DKIM `CNAME`，以及 SPF / MX 相关记录）。到 Cloudflare 里 `pku.aeoluswu.info` 的 DNS 按控制台原样添加。
   - DKIM 的 CNAME 必须 **DNS only**（灰云），不要橙云代理，否则验不过。
   - 记录名以 Resend 控制台为准，不要手改主机名。
4. 另加一条 DMARC（Cloudflare TXT）：

   ```text
   名称   _dmarc.mail
   内容   v=DMARC1; p=none; rua=mailto:你的运维邮箱
   ```

   先 `p=none` 观察，进信箱稳定后再考虑隔离策略。
5. 回到 Resend 点 **Verify**。状态变为 Verified 后，该子域下任意地址都可发信。
6. Dashboard → **API Keys** → 建一把只用于发送的 key。只放在中转服务端环境变量里，不进开源客户端、不进 git、不进安装包。

```text
RESEND_API_KEY=re_********
MAIL_FROM=noreply@mail.pku.aeoluswu.info
MAIL_PROVIDER=resend          # 可换成 log / smtp，适配器切换
APP_PUBLIC_URL=https://pku.aeoluswu.info
```

7. 用 Resend 控制台或一次真实注册，往自己的邮箱发一封，确认进收件箱而不是垃圾箱。学校邮箱（`@pku.edu.cn` / `@stu.pku.edu.cn`）要单独测一次。

额度不够时再升 Resend Pro；代码侧只看到 `send_email()`，不改验证状态机。

## 后台管理与安全边界

后台使用独立 admin 子域，不与学生界面混用。入口由 Cloudflare Access 邮箱白名单和 MFA 保护，源站必须校验 Access JWT，不能只信任前端或来源网络。

后台范围包括：账号、邮箱验证状态、订阅、额度包、批量签发和撤销未使用兑换码、兑换记录、额度使用记录及管理员审计。支付只预留接口，不在本阶段实现支付流程。

服务端采用单主 SQLite 写入限制：写入集中到唯一主实例，避免多进程/多副本同时写入造成额度、兑换和审计不一致；备份、迁移和并发策略需在部署验收中明确。所有敏感管理动作应记录操作者、对象、时间、结果和必要的请求关联信息，不记录密码、token 或兑换码明文。

## 卸载与彻底清除

- **普通卸载**：移除应用本体，保留本机凭据、课程数据、缓存、日志、Notion 本地 token 和产品会话，便于重装后继续使用；普通重启也不要求重新认证。
- **彻底清除**：用户必须明确勾选并完成二次确认；删除本机凭据、课程数据、缓存、日志、Notion 本地 token 和产品会话。清除后重装必须重新认证。
- 两种操作都不删除云端产品账号、订阅、额度记录、兑换/使用/管理员审计，也不删除远程 Notion 内容。

## 跨平台桌面壳

- **状态：进行中**（`desktop/` Tauri 2 壳：拉起 `pku-sync panel --no-browser`，健康检查后打开 `/app`；单实例与关闭清理已落地）。**Windows 打包首条路径已锁定**：`tauri build` 嵌入 relocatable Python 运行时 + `externalBin` 侧车 `pku-sync`；安装包（Tauri NSIS 或 Inno 包装同一 payload）安装后快捷方式启动 **Tauri exe**（桌面窗口），不再依赖用户手装 Python/uv，也不再默认走浏览器。开发态 `tauri dev` 仍可用 PATH 上的 `pku-sync`。完整 WebView 验收以 Windows 实机为准；macOS DMG/公证为后续；管理后台 / Cloudflare Access 为**下一里程碑**。
- 首发同时覆盖 Windows 与 macOS。壳是薄封装：拉起本地 FastAPI 子进程，健康检查通过后用**系统 WebView** 打开学生界面 `/app`。
- 推荐实现：**Tauri**（Windows 用 WebView2，macOS 用 WKWebView）。不要先写一版 .NET / WebView2 专属壳再为 Mac 重写。
- 两边共用：单实例、子进程生命周期、健康检查、关闭时清理临时资源、OAuth / Notion 走系统默认外部浏览器。
- 只有安装器与签名不同：Windows 为 setup.exe（NSIS/Inno），macOS 为 DMG（公证与签名是发布步骤，不改变壳的产品约定）。本机数据目录仍为用户主目录下的 `PKU-All-in-Notion`。
- 产品窗口不展示工程日志或状态页作为学生入口。普通关闭不删除用户数据。

## 实施里程碑与前置条件

1. **产品账号与服务端基础**：邮箱注册/验证、登录、一次性重置链接、Argon2id/bcrypt 哈希、登录和重置限流、账号与会话状态；`send_email()` 适配器（Resend / 日志）。**客户端学生面板已接线**（见文末）；服务端能力在 `pku-server`，发信运营配置仍待验收。
2. **额度与后台**：订阅和充值包模型、最早过期优先扣减、兑换码批量签发/撤销、审计、单主 SQLite 写入约束；支付接口先留空。
3. **跨平台桌面壳**（进行中）：Tauri 窗口 + FastAPI 子进程 + 健康检查 + 单实例 + 关闭清理 + 外部浏览器；Windows sidecar 打包脚本与 Inno/NSIS 路径已进仓库（见 `desktop/README.md`）。WebView 实机验收、代码签名、macOS DMG、自动更新仍待完成。
4. **身份与本机数据**：`os_keyring` 保存 IAAA（Win Credential Manager / Mac Keychain），规划普通卸载与彻底清除，Notion token 本机保存和清除后的重新连接。
5. **后台 staging 与发布**（壳可启动验收之后的下一里程碑）：准备 admin staging、Cloudflare Access 邮箱白名单、MFA、源站 Access JWT 校验和审计检查；Windows 安装包与 macOS DMG 签名/公证。

当前前置条件/阻塞项：

- Tauri 壳与 Windows sidecar/NSIS/Inno 打包路径已在 `desktop/`、`installer/windows/`；各端系统 WebView 仍需在目标机器上验收；代码签名、macOS DMG、自动更新未完成。
- 客户端产品账号 API/面板已接线（见文末「客户端产品账号」）；服务端 Resend、`mail.pku.aeoluswu.info` 与发件人需在运营侧完成验证（邮件已可按服务端配置发送时，客户端只需对真实中转联调）。
- admin staging、Cloudflare Access、邮箱白名单、MFA 和源站 JWT 校验尚未完成配置（排在桌面壳可启动验收之后）。
- PKU 官方 SSO 审批尚未获得；在获批前继续采用本机 IAAA，不建设服务器代存密码流程。

## 验收重点

- Windows 与 macOS 启动都直接显示学生界面，单实例有效，FastAPI 健康检查失败可诊断，关闭后子进程清理。
- 产品账号、邮箱验证限制、一次性重置链接、限流和密码哈希符合安全要求。
- 验证/重置邮件从 `noreply@mail.pku.aeoluswu.info` 发出，学校邮箱可收到；未验证账号无法兑码或使用云端能力。
- IAAA 密码从未上传；Notion token 仅在本机；外部浏览器 OAuth 流程可用。
- 月度订阅、12 个月充值包、最早过期优先消耗、码不创建账号、过期/撤销/使用审计均可验证。
- 后台 Access JWT、MFA、管理员审计和单主 SQLite 写入限制可验证。
- 普通卸载保留本机数据，彻底清除需二次确认且只清除本机内容；云端账号和 Notion 内容不受影响。

当前 `0.1.1` 不属于本文已实现范围的能力包括：正式订阅/充值包模型、正式 admin 子域与 Access/MFA，以及产品化卸载清除流程。跨平台桌面壳已在 `desktop/` 开工（进行中，WebView 实机验收与打包仍待完成）；admin 为下一里程碑。

### 客户端产品账号（部分落地）

- **已实现（本仓库学生面板）**：邮箱+密码注册/登录/退出；本地保存 `PLATFORM_TOKEN`（与既有激活会话同一写法）；`/api/auth/*` 与 `/api/platform/redeem`；侧栏展示 `email_verified`；未验证时禁用兑换入口（服务端仍对 redeem/LLM/转写返回 403）；忘记密码只触发 `POST /v1/auth/forgot`（重置落地页在服务端）。
- **仍依赖服务端/运营**：Resend 发信与 `mail.pku.aeoluswu.info` 验证、真实邮箱收信、订阅额度模型与 admin。
- **未做/后续**：IAAA `os_keyring`、彻底清除本机数据 UI、auth 屏与侧栏的视觉打磨、工程状态面板仍保留试点 `/v1/activate` 路径。
