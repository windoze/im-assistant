# 落地计划 — 钉钉 AI 助手

> 状态:v0.1(2026-07-06)
> 范围:**钉钉 + 单组织**。目标是把 [architecture.md](docs/architecture.md) 的抽象落成可运行系统。
> 配套文档:架构 [architecture.md](docs/architecture.md)、钉钉能力 [dingtalk.md](docs/dingtalk.md)、企业微信对比 [wecom.md](docs/wecom.md)。

---

## 0. 技术选型(建议,可改)

| 项 | 选择 | 理由 |
|---|---|---|
| 语言/运行时 | **Python 3.11+** | 钉钉有官方维护的 Stream SDK(Python);Claude 有官方 SDK;生态阻力最小 |
| 钉钉接入 | `dingtalk-stream`(官方 Stream SDK)+ `requests`/`httpx` 调 OpenAPI | Stream 收、HTTP 发(见架构 §8.1) |
| LLM | Claude(Anthropic API,官方 SDK) | agent loop + tool use 原生支持 |
| 存储(原型) | SQLite + 本地文件 | 单机、够用;TokenVault/审计/会话状态先落 SQLite |
| 密钥加密 | `cryptography`(Fernet/AES) | TokenVault 静态加密 |
| 进程模型 | 单进程 asyncio + per-session 串行队列 | 对齐架构 §8.1 串行 inbox;沙箱化 tool 执行后置 |
| 配置 | `.env` + `config.yaml` | 读取钉钉后台产物(架构 §10),不进程序抽象 |

> 备选:若团队更熟 Node/Go,钉钉也有 SDK,但 Python 的官方支持最完整。原型阶段不建议自研 WebSocket 协议层。

---

## 1. 里程碑总览

| 里程碑 | 目标 | 产出 | 依赖 |
|---|---|---|---|
| **M0** 环境与骨架 | 钉钉应用就绪 + 项目骨架 | 能连上 Stream、能发消息 | — |
| **M1** 最小对话闭环 | @机器人 → LLM → 回复 | 群/单聊可对话的机器人 | M0 |
| **M2** 会话运行时 | Session 抽象 + 串行 inbox + agent loop | 多轮对话、可打断 | M1 |
| **M3** 能力层 | Tool/Skill 注册 + 可见性闸门 + 三级目录 | 能调工具(先无 OBO) | M2 |
| **M4** OBO 授权 | OAuth2 + TokenVault + Authorizer 三态 | "今日日程总结"端到端 | M3 |
| **M5** 带外交互 | confirm/consent + 取消 + 系统通告 | 高敏感操作需确认 | M4 |
| **M6** 指令通道 | slash command 注册表 + 入站三岔口 | `/reset` `/help` `/connect` 等 | M2 |
| **M7** 加固 | 审计、沙箱、可观测、错误恢复 | 可试运行的原型 | M3–M6 |

M1 是"看得见的第一个成果";M4 是"架构核心(OBO)的验证";M5/M6 完成后架构 §8 的运行时就完整了。

---

## 2. 分阶段详细计划

### M0 — 环境与骨架

**钉钉侧(一次性,配置手册)**
- [ ] 开发者后台创建**企业内部应用**,添加**机器人**能力,配置名称/头像 → 独立身份。
- [ ] 开启 **Stream 模式**接入。
- [ ] 申请权限点:机器人发送消息、通讯录读取(拿 userId/unionId)。
- [ ] 记录 `AppKey / AppSecret / robotCode` 到 `.env`。

**代码侧**
- [ ] 项目骨架:`adapters/ core/ capabilities/ infra/` 分层(对齐架构 §2)。
- [ ] `infra/config`:读取 `.env` + `config.yaml`。
- [ ] `infra/dingtalk_client`:应用级 access_token 获取 + 缓存 + 刷新(7200s)。
- [ ] 冒烟:调 `oToMessages/batchSend` 给自己发一条,`contact/users/me`/通讯录跑通。

**验收**:能拿到 access_token,能主动给指定 userId 发一条消息。

---

### M1 — 最小对话闭环

- [ ] `adapters/dingtalk`:Stream SDK 接入,订阅机器人消息回调。
- [ ] 入站归一化:回调 → 内部 `InboundMessage {text, senderStaffId, conversationType, conversationId, sessionWebhook}`。
- [ ] 触发判定(架构 §8.2):单聊直接触发;群聊钉钉只回调 @ 消息,无需自行过滤。
- [ ] 出站:封装"发消息"= HTTP OpenAPI(单聊 `oToMessages/batchSend` / 群聊 `groupMessages/send`)或 `sessionWebhook`。
- [ ] 接一次 Claude:收到文本 → 调 LLM → 回发。**先无历史、无工具、无 Session**,一问一答。

**验收**:群里 @机器人、或私聊机器人,能得到 LLM 回复,发送者显示为机器人独立身份。

> 这一步等价于我们在企业微信用 wecom-cli 跑通的 demo,但这次是**真·独立身份 + 真·多用户 + 真·可交互**。

---

### M2 — 会话运行时(Session)

对齐架构 §8。

- [ ] `Session` 抽象:`kind(dm|group) / bot / principal / actor / context / inbox / lifecycle`(架构 §5)。
- [ ] Session 路由:`(conversationId)` → Session;群聊共享一个 Session,actor = 发送者。
- [ ] **per-session 串行 inbox**(asyncio queue):同会话消息依次处理,不并发。
- [ ] 多轮上下文:维护对话历史(先存内存/SQLite)。
- [ ] agent loop:LLM ↔ (工具占位) 循环骨架,支持 suspend/resume 状态位。
- [ ] 会话状态机:`Idle / RunningAgent / AwaitingInteraction`(架构 §8.1,AwaitingInteraction 在 M5 用上)。
- [ ] 生命周期:首次被 @ 记录会话已激活 + 欢迎语(架构 §8.3)。

**验收**:多轮对话有上下文;同一会话并发发多条能串行正确处理。

---

### M3 — 能力层(Tools / Skills)

对齐架构 §5、§6。**本阶段只做不需要 OBO 的工具**。

- [ ] `Capability` 定义:`origin(system|base|user) / availableIn / requires / sensitivity`(架构 §5)。
- [ ] 三级目录叠加:`system/ → base/ → user/<userid>/`(架构 §6.2)。
- [ ] **可见性闸门 `canUse`**(架构 §6.1):纯函数,按 mode/actor/channel 过滤。
- [ ] 把工具暴露给 LLM(Claude tool use);agent loop 真正执行工具调用。
- [ ] 首批工具(应用级,无 OBO):
  - `send_doc`:建钉钉文档 + 写入(对标 wecom 实测的 doc 闭环)。
  - `contact_lookup`:userId ↔ 姓名(通讯录)。
  - `create_todo` / `create_schedule`:应用级 + unionId 即可(dingtalk.md §3)。
- [ ] `channel → enabled capabilities` 配置(群专属能力,架构 §6.2)。

**验收**:群里说"帮我建个文档记录XX",机器人真的建出文档并回链接。

---

### M4 — OBO 授权(架构核心验证)

对齐架构 §6.3、§7。**这是钉钉相对企业微信的关键红利,必须跑通。**

- [ ] `infra/token_vault`:`(principal, service) → {userAccessToken, refreshToken, scopes, exp}`,静态加密,支持刷新/撤销。
- [ ] OAuth2 流程:
  - [ ] 公网 HTTPS 回调端点 `/oauth/start` `/oauth/callback`(原型可用隧道/临时域名)。
  - [ ] `login.dingtalk.com/oauth2/auth` → code → `userAccessToken` + `refreshToken`。
  - [ ] `state` 防 CSRF、单次短时效。
- [ ] **身份核对**(架构 §7.2):用户 token 调 `contact/users/me` 拿 unionId,核对 == 发起会话的 actor,不符拒绝。
- [ ] **Authorizer 三态**(架构 §6.3):`Granted / NeedsConsent(url) / Denied`。
- [ ] `CredentialContext`:按资源选应用级+unionId 或用户级 OBO(架构 §7.1)。
- [ ] 静默刷新:accessToken 过期用 refreshToken 换,用户无感。
- [ ] 首个 OBO 工具:**`schedule_summary`(今日日程总结)**——架构 §6.4 的招牌示例,用 `me` 接口读本人日程。

**验收**:私聊"总结我今天的日程" → 首次弹授权链接 → 授权后读到**本人**日程并总结;再次问无需授权(静默刷新)。

---

### M5 — 带外交互(confirm / consent / 系统通告)

对齐架构 §8.4b。

- [ ] `SessionInterrupt` 原语:`kind(confirm|consent) / payload / correlation_id / responder / expires_at / resolve`。
- [ ] `ctx.confirm(...)`:工具执行副作用前挂起,发钉钉互动卡片,等回复。
- [ ] 卡片回调 → 按 `correlation_id` + responder 匹配 → `resolve()`(绕过 LLM)。
- [ ] 取消双来源(架构 §8.4b):新消息(`superseded`)/ 30 分钟超时(`timeout`)→ `Cancelled`。
- [ ] 取消分工:运行时**直接推**系统消息("已取消…")+ agent 收尾轮**静默**(仅历史留 Cancelled)。
- [ ] 出站三来源约束(架构 §8.4b):AI 回复 / 交互原语 / 系统通告。
- [ ] 把 M4 的 `consent`(NeedsConsent)归并到该原语。

**验收**:一个"发通知/改数据"类工具执行前弹确认卡;点确认才执行;发新消息则取消并收到系统"已取消"通告。

---

### M6 — 指令通道(slash command)

对齐架构 §8.4(入站三岔口)、§8.5。可与 M4/M5 并行。

- [ ] **入站分类器**(架构 §8.4):① pending 回复 → resolve ② `/...` → 指令 ③ 其余 → agent loop。
- [ ] `Command` 注册表(独立于 AI 工具表):`name / availableIn / requires_role / handler`。
- [ ] 触发语法:DM `/reset`;群聊 `@助手 /reset`。
- [ ] `requires_role` 用 actor 鉴权(架构 §3)。
- [ ] **注入消息 API**:指令影响会话的唯一途径(架构 §8.5),往 session 追加消息。
- [ ] 首批指令:`/help` `/reset` `/whoami` `/connect <service>`(主动 OBO 预热)`/disconnect <service>`(清 TokenVault)`/cancel`。

**验收**:`/help` 列能力;`/reset` 清上下文;`/connect calendar` 主动走一次授权。

---

### M7 — 加固(可试运行)

对齐架构 §9。

- [ ] **审计**:OBO 取数、confirm/取消决议、指令执行全链路日志(谁/代表谁/何时/何 scope/做了什么)。
- [ ] **Tool 执行沙箱**:若引入执行任意代码/脚本的工具,子进程/容器 + 受限 FS;Session 逻辑隔离(独立 workdir/上下文/凭证视图)。
- [ ] **不可信输入边界**:高敏感工具走 confirm/白名单,不靠 LLM 自觉(架构 §9)。
- [ ] 可观测:结构化日志、关键指标(消息量、工具调用、授权成功率)。
- [ ] 错误恢复:Stream 重连/幂等(MsgId 去重)/背压;access_token 失效重取;OBO 刷新失败降级到重新授权。
- [ ] 会话状态 + pending 交互**可落盘可恢复**(架构 §8.1)。

**验收**:进程重启后进行中的授权/确认能恢复;断线自动重连;重复消息不重复处理。

---

## 3. 目录结构(建议)

```
im-assistant/
├── PLAN.md, docs/
├── config.yaml, .env
├── src/
│   ├── adapters/dingtalk/      # Stream 收 + HTTP 发 + 消息归一化
│   ├── core/
│   │   ├── session.py          # Session、状态机、串行 inbox
│   │   ├── agent_loop.py       # LLM + tool use 循环, suspend/resume
│   │   ├── router.py           # 入站三岔口(§8.4)
│   │   ├── interrupt.py        # SessionInterrupt(confirm/consent)
│   │   └── commands.py         # 指令注册表(§8.5)
│   ├── capabilities/
│   │   ├── registry.py         # canUse 可见性闸门, 三级目录
│   │   ├── authorizer.py       # Authorizer 三态(§6.3)
│   │   ├── credential.py       # CredentialContext(应用级/OBO)
│   │   ├── system/ base/ user/ # 三级能力目录
│   ├── infra/
│   │   ├── token_vault.py      # OBO token 加密存储/刷新/撤销
│   │   ├── oauth.py            # 钉钉 OAuth2 + 身份核对
│   │   ├── audit.py            # 审计日志
│   │   └── store.py            # SQLite(会话/绑定/审计)
│   └── main.py
└── tests/
```

---

## 4. 风险与开放问题

| 风险 | 影响 | 应对 |
|---|---|---|
| **会话历史读不到**(§0) | "会话总结"招牌功能做不了 | 一期不依赖群历史;做"基于当次上下文/主动汇报/投喂内容"的替代形态 |
| OAuth 需公网 HTTPS 回调 | M4 本地开发受阻 | 原型用隧道(如 frp/cloudflared)或临时域名 |
| 钉钉权限点审批门槛 | 某些能力申请慢 | 先做通讯录/日程/待办等常见权限;敏感能力后置 |
| 钉钉文档正文读取完整度 | 文档类工具可用性 | M3 先做"建/写",读取完整度待 PoC 验证 |
| API 频率限制 | 主动发消息受限 | 出站限流 + 队列;避免刷屏 |
| 单进程扩展性 | 用户量大时瓶颈 | 原型阶段够用;后续按 Session 分片/多 worker |

**待定(承接架构 §11)**
- [ ] `Authorizer` / `Requirement` 最终接口签名与抽象 scope 命名。
- [ ] 注入消息 API 的具体形态。
- [ ] 指令表与工具表功能重叠时的组织方式。
- [ ] 钉钉群变更事件是否用作显式激活闸门。
- [ ] 沙箱形态(子进程 vs 容器)。

---

## 5. 建议推进顺序

1. **先做 M0 → M1**,拿到"独立身份、可交互的机器人"这个看得见的成果。
2. **再做 M2 → M3**,把运行时和能力层立起来(此时已能做"建文档/记待办"等应用级功能)。
3. **重点攻 M4(OBO)**,这是验证架构核心、也是钉钉相对企业微信最大价值的一步。
4. M5/M6 并行补齐运行时(确认 + 指令),M7 加固到可试运行。

每个里程碑都有明确验收,可独立演示。M1、M4 是两个关键节点。
