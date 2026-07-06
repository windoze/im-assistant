# IM AI 助手 — 基线架构

> 状态:基线设计(v0.2)
> 范围约束:**单一 IM + 单一组织**。不做多 IM 打通,不做多租户。
> 目标:让使用该 IM 的公司建立**内部** AI 助手,面向组织内所有用户,而非跨公司网络服务。
>
> **平台选型(2026-07,见 §0)**:第一目标平台为**钉钉**。企业微信经调研在"机器人独立身份、多用户、OBO"三个核心诉求上均受限,退为参考。抽象层仍按多 IM 设计,以便日后接入 Teams/企业微信/飞书。

---

## 0. 平台选型(2026-07)

经两轮实测 + 调研,第一目标平台从企业微信改为**钉钉**。详见 [wecom.md](wecom.md) 与 [dingtalk.md](dingtalk.md)。

| 核心诉求 | 企业微信 | 钉钉 |
|---|---|---|
| 机器人独立身份 | ❌ wecom-cli 通道发消息显示为**授权用户本人** | ✅ 企业内部应用机器人独立身份("XX助手") |
| 多用户 | ❌ wecom-cli"仅创建者可对话" | ✅ 群里任何人 @ 都能触发 |
| 真正的 OBO | ❌ 无 delegated token,只有应用级 token | ✅ OAuth2 用户级 token + refresh_token + `me` 接口族 |
| 读用户既有数据 | ❌ 只能读应用自建的 | ✅ 钉盘/日历/待办可到用户维度 |
| 免公网部署 | 长连接可行 | ✅ Stream 模式(WebSocket) |
| **群历史消息** | ⚠️ 需付费会话存档 | ⚠️ 同样受限 |

**结论**:钉钉几乎"照着本架构就能实现",企业微信则处处需妥协(尤其 §4 身份方向性、§6 授权流水线在企业微信原生 API 下几乎落不了地)。

**关键取舍——会话历史两平台都受限**:标准开放平台都读不到群/单聊历史消息(企业微信需付费会话存档,钉钉无通用等价物)。因此**依赖群历史的"会话总结"不作为一期功能**;可做的是不依赖历史回溯的形态(基于当次 @ 的上下文、主动汇报、用户投喂内容的处理等)。
> 注:企业微信 wecom-cli 通道**能**读群历史(实测通过),但受"个人身份 + 仅创建者可对话"限制,不适合做组织级群助手——能力与形态错位。

---

## 1. 设计目标与非目标

### 目标
- 助手面向组织内**所有**用户,运行在**服务器**上(非用户桌面)。
- 每个**频道(群聊)**和每个用户的**私聊**拥有独立的运行环境(隔离概念先建立,具体隔离措施可延后)。
- 通过 tools/skills 执行功能,采用类 claude-code 的目录形态,支持**基础(base)/ 用户私有(user)** 两级能力叠加。
- 有一套抽象层屏蔽 IM 差异,上层表现为**"群 / 用户各自独立的权限与能力"**。
- 私聊场景支持"以用户身份获取其相关数据"的授权模型。

### 非目标(本阶段明确不做)
- 多 IM 适配(Teams / 企业微信 / 飞书):只保留抽象接缝,当前只实现钉钉。
- 多租户 / 多组织隔离、按组织计费。
- 系统侧一次性配置的抽象(IM 开放平台后台设置)——以**配置手册**交付,不进程序。
- 精细的 per-user 数据裁剪:留可插拔接口,当前为信任可见范围的空实现。
- 依赖群/单聊**历史消息**的功能(如会话总结)——两平台标准 API 均受限,见 §0。

---

## 2. 分层架构

```
┌─────────────────────────────────────────────────────────┐
│ IM 接入层 (Adapter)  —— 钉钉实现                          │
│   收消息: Stream 模式 (WebSocket 长连接, 免公网域名)      │
│   发消息: HTTP OpenAPI (机器人独立身份) / sessionWebhook  │
│   单聊+群聊同一机器人身份;职责:收发、能力协商与降级      │
│   ⚠️ Stream 只收不发,发必走 HTTP —— 天然契合异步模型      │
├─────────────────────────────────────────────────────────┤
│ 抽象层 (Domain Abstractions)  ★ 本系统的核心与未来接缝    │
│   Principal / BotIdentity / CredentialContext / Capability│
│   把 IM 现实(token 体系、身份、收发通道)                 │
│   翻译成上层统一模型:"群 / 用户各自独立的权限与能力"      │
├─────────────────────────────────────────────────────────┤
│ 会话运行时 (Session Runtime)                              │
│   per channel / per DM = 一个 Session,有生命周期         │
│   Agent Loop (LLM + tools + HITL)                        │
│   强制异步:回调即返,后台处理,主动推送结果 (suspend/resume)│
├─────────────────────────────────────────────────────────┤
│ 能力层 (Capabilities)                                     │
│   Tools/Skills 目录, system / base / user 三级叠加        │
│   可用性判定 = 纯函数(mode, actor, channel)              │
├─────────────────────────────────────────────────────────┤
│ 基础设施                                                  │
│   身份绑定存储 · 审计 · 配置加载 · per-session 事件队列   │
│   TokenVault(钉钉 OBO 的用户级 token / refreshToken 存储)│
└─────────────────────────────────────────────────────────┘
```

---

## 3. 两种执行模式

助手本质是两个模式,身份与权限来源完全不同:

| 维度 | 群聊模式 (Group) | 私聊模式 (DM) |
|---|---|---|
| 触发 | 群里 `@机器人` / 回复机器人 | 用户直接发消息 |
| 上层主体 | `GroupPrincipal`(群这个实体) | `UserPrincipal`(具体的人) |
| 身份 | 无用户身份,助手作为群成员 | 绑定到该用户 |
| 权限/能力 | 全局能力 + 群专属能力 | 全局能力 + 用户私有能力 |
| 以用户身份取数据 | ✗ 禁用 | ✓ 启用(见 §4、§6) |
| 触发者(actor) | **记录**,仅用于审计 / 群内鉴权,**不 impersonate** | 即会话所属用户 |
| 钉钉接入 | 同一企业内部应用机器人;群聊回调需 @ | 同一机器人;单聊直接触发 |

**群聊不持有任何用户凭证**,因此从根上消除了"用某人权限读数据、结果发全群"的越权泄露问题。

> 钉钉下 DM 与群聊是**同一个机器人身份**(§0),`senderStaffId` 提供 actor 的 userId;两种模式的差异在触发方式(群需 @)和是否启用 OBO,而非接入实体。

---

## 4. 身份方向性(对内 / 对外)

助手有**两张脸**,由交互方向决定用哪个身份。这是整个授权模型的地基。

- **对内(inbound):与用户/群交互** —— 恒用助手自己的身份 `BotIdentity`。
  收消息、发消息、被 @、发卡片、发授权链接,都是"助手作为一个 IM 成员在说话"。群聊与私聊在此方向**无区别**。
- **对外(outbound):向资源系统取数据/执行** —— 按**资源所有权**决定身份:
  - 资源归**组织/助手公共**(天气、公共知识库、组织级 API、往 IM 发消息/建群)→ 仍用**助手身份**。
  - 资源归**某个用户本人且受其权限保护**(日历、待办、钉盘)→ 用**该用户身份(OBO)**。

### 核心判定表

|          | 对内(收发消息) | 对外·公共/助手资源 | 对外·用户本人资源 |
|----------|:---:|:---:|:---:|
| **群助手** | 助手身份 | 助手身份 | ✗ 不成立(群里无单一 OBO 目标) |
| **DM 助手** | 助手身份 | 助手身份 | **用户身份 (OBO)** |

一句话:**身份默认永远是助手自己;唯一例外是"DM 里访问用户本人的受保护资源",此时切换到该用户(actor)身份。**

要点:
- 群聊的右下角是**语义上不成立**,不是策略性关闭——群里没有单一的 on_behalf_of 目标。
- `on_behalf_of` 修饰的是**对外这条边"用谁的权限取数据"**,不是"助手替谁说话"。助手永远只代表自己说话。
- 授权流水线(§6.3 的 Authorizer)**只守右下角那一格**,其余五格免授权。

---

## 5. 核心抽象(接口草图)

> 以下为语义草图,非最终签名。核心意图:**上层只见"群/用户的独立权限",不见底层 IM 的 token/身份/收发差异。**

```
Principal                      # 上层统一主体
  ├─ kind: user | group
  ├─ id                        # 规范内部标识
  ├─ capabilities              # 该主体可用的能力集(见 §6)
  └─ credentials: CredentialContext

UserPrincipal  extends Principal (kind=user)
  └─ identities                # { wecom_userid, (future) msgraph_aad_id, ... }

GroupPrincipal extends Principal (kind=group)
  └─ channel_id                # 钉钉 openConversationId

BotIdentity                    # 对外可被 @ / 收发的身份(两模式共用抽象)
  ├─ 用途: 群聊/私聊下所有出站消息的发出者
  └─ 底层(钉钉): 同一企业内部应用机器人(robotCode);收=Stream,发=HTTP

CredentialContext              # ★ 屏蔽授权差异的关键抽象
  ├─ 应用级: 应用 access_token (+ unionId 作参数) —— 管理员授权,免逐用户同意
  ├─ 用户级(OBO): 用户 access_token + refreshToken,调 me 接口按本人权限
  ├─ 裁剪层: 可插拔,当前 no-op(信任应用可见范围)
  └─ 选择依据: 按资源敏感度,低敏感走应用级+unionId,高敏感走 OBO
  # 上层调用形如 ctx.user.readCalendar() / ctx.group.send(),
  # 由适配层决定用哪种凭证、走哪条链路;换 IM 时上层不变。

Session                        # per (channel | dm),有生命周期
  ├─ kind: dm | group
  ├─ bot: BotIdentity
  ├─ principal: UserPrincipal | GroupPrincipal
  ├─ actor: 当前触发消息的发送者(group 也记录,仅审计/鉴权)
  ├─ context / workdir         # 隔离的对话历史与工作目录
  ├─ inbox                     # per-session 串行事件队列,可打断
  └─ lifecycle                 # 见 §8

Capability (Tool / Skill)
  ├─ origin: system | base | user   # 来源(见 §6):出厂内建 / 组织可配 / 用户私有
  ├─ availableIn: [global] | [group] | [dm] | [group, dm]
  ├─ requires: [Requirement]        # 声明式授权需求(见 §6.1),无则免授权
  └─ sensitivity                    # 是否需人工确认 / 白名单

Requirement                   # 工具运行所需的对外授权(仅"对外·用户资源"边)
  ├─ service: "calendar" | "todo" | "drive" | ...   # 抽象服务,不绑具体 IM
  ├─ scopes:  ["calendar:read", ...]                # 抽象 scope
  └─ on_behalf_of: "actor"                          # 目前唯一取值:触发者本人
```

---

## 6. 能力模型与授权

### 6.1 两阶段闸门

工具的分发(谁能用到)与授权(运行需要什么权限)是**正交**的两个轴,依次通过两道闸门:

```
① 可见性闸门 (canUse)      —— 编排前:此工具在当前模式/主体下能否出现
② 授权闸门   (Authorizer)  —— 执行前:能否拿到运行它所需的对外用户授权
```

**① 可见性闸门** —— 纯函数,不涉及跨租户逻辑:

```
canUse(cap, mode, actor, channel):
    if cap.requires_user_authority and mode != DM:     return false   # 群聊无 OBO 目标
    if global in cap.availableIn:                       return true
    if mode == DM    and cap.origin == user:            return actor 拥有该私有能力
    if mode == Group and cap in channel.enabledCaps:    return true
    return false
```

其中 `requires_user_authority` = `cap.requires` 中存在 `on_behalf_of` 项。这类工具**只可能出现在 DM**,由运行时强制,不依赖 LLM 自觉。

**② 授权闸门** —— 见 §6.3。仅对声明了 `requires`(对外·用户资源边)的工具生效;其余工具跳过此闸门。

### 6.2 能力来源:system / base / user 三级

| origin | 含义 | 可否卸载 |
|---|---|---|
| `system` | 随产品出厂的内建能力,深度复用授权模型(日程查询、待办、通讯录查询…) | 否 |
| `base` | 组织可配置的通用能力,全局叠加 | 是(管理员) |
| `user`  | 用户私有能力 | 是(用户自己) |

- 目录形态:类 claude-code,`system/`(出厂) → `base/`(组织) → `user/<userid>/`(私有)三层叠加。
- 群专属能力:一份 `channel_id → enabled capabilities` 配置,由管理员维护。
- 三级在授权上**一视同仁**——`system` 工具同样要走 §6.3 授权闸门(日程总结正是 `system` 但需用户授权的典型)。区别只在**来源与可否卸载**。

### 6.3 授权解析(Authorizer)

工具**声明式**地写出 `requires`,运行时在执行前对每个 requirement 调用
`Authorizer.resolve(requirement, actor, mode)`,返回三态之一:

```
Granted(handle)    → 凭证就绪,注入 CredentialContext,工具直接跑
NeedsConsent(url)  → 缺身份绑定 → 挂起会话,私聊发授权链接,授权后 resume
Denied(reason)     → 群模式需用户身份 / 用户明确拒绝 → 工具不可用
```

三态完全复用已有机制:`CredentialContext`(§5)、身份核对/绑定(§7.2)、`suspend/resume`(§8.1)、群聊零凭证(§3)。**未引入新机制,只是把工具接入这条授权流水线。** 其中 `NeedsConsent` 是 §8.4b 带外交互原语(`SessionInterrupt`)的 `consent` 实例,与执行确认 `confirm` 同源。

抽象 scope 是跨 IM 复用的关键——工具只声明 `calendar:read`,由 `CredentialContext` 在各 IM 下翻译:

```
工具声明 requires calendar:read (on behalf of actor)
   │
钉钉: 需用户级 token(OBO)带日历 scope;缺失 → NeedsConsent,发 OAuth 授权链接
      拿到 userAccessToken + refreshToken → 调 /v1.0/calendar/users/me/... 按本人权限读
Teams(未来): 需 delegated token 带 Calendars.Read;缺失 → OAuth consent 拿 token
   │
工具代码只写一次 calendar.today(actor),各 IM 都能跑,差异全在 Authorizer 背后。
```

### 6.4 示例:今日日程总结(钉钉 DM)

```
用户私聊: "帮我总结今天的日程"
  → 选中 schedule-summary (origin=system, requires calendar:read obo=actor)
  → ① 可见性: DM 模式 ✓
  → ② 授权: Authorizer.resolve(calendar:read, actor, DM)
        查 TokenVault(actor, dingtalk-calendar) —— 无有效用户 token
        → NeedsConsent: 挂起会话,私聊发"点此授权访问你的日程"链接
  → 用户点链接 → 钉钉 OAuth2(login.dingtalk.com/oauth2/auth)
    → 回调 code 换 userAccessToken + refreshToken
    → 用 token 调 contact/users/me 拿 unionId,核对 == actor ✓
    → 存入 TokenVault(加密, 带 refreshToken)
  → resume → 工具用用户 token 调 /v1.0/calendar/users/me/... 读本人日程 → LLM 总结
  → 对内边: 用 BotIdentity 推送结果给用户
  (再次询问时 token 有效或可静默刷新 → 直接 Granted,无中断)
```

> ✅ 该示例在钉钉上**能真实落地**:日历有用户级 OBO + `me` 接口(见 dingtalk.md §3)。
> 对比:同类"邮件总结"在企业微信原生 API **读不到用户收件箱**,做不了(见 wecom.md §2.5)。

---

## 7. 钉钉授权模型:落地方式

钉钉同时提供两套 token,让本架构 §4/§6 的授权设计得以**真实落地**(企业微信做不到)。详见 dingtalk.md §2。

### 7.1 两套 token(CredentialContext 的两种后端)

| | 应用级 access_token | 用户级 access_token(OBO) |
|---|---|---|
| 获取 | AppKey + AppSecret,7200s,自缓存 | OAuth2 授权码 → userAccessToken,带 **refreshToken** |
| 授权主体 | 管理员(后台权限点) | **用户本人**(OAuth 同意) |
| 代表谁 | 应用,靠 `unionId` 参数下沉到用户 | **令牌即代表用户+应用**,按本人权限裁剪 |
| 用途 | 发消息、组织级批处理、低敏感读取 | 读本人日程/待办/钉盘等个人数据 |
| API 形态 | `x-acs-dingtalk-access-token` + userid/unionId | 同 header 放用户 token,走 `.../users/me/...` |

**按资源敏感度选择**:低敏感 → 应用级+unionId(免逐用户授权,方便);高敏感/需本人授权 → OBO。二者都由 `CredentialContext` 封装,上层不感知。

### 7.2 身份核对(§6.4 用到)

需要 OBO 的操作,让用户走一次钉钉 OAuth2:
- `login.dingtalk.com/oauth2/auth` → code → `userAccessToken` + `refreshToken`。
- 用户 token 调 `contact/users/me` 拿 `unionId`,核对 == 会话 actor 的身份(由 Stream 回调 `senderStaffId` 提供)。
- 校验 `state`(防 CSRF)、回调走 HTTPS。
- 产物存入 **TokenVault**(加密,含 refreshToken),后续静默刷新。

### 7.3 抽象带来的收益(已兑现,非未来)
- 上层代码只写 `ctx.user.readCalendar()`,不关心底层是"应用 token+unionId"还是"用户 token"。
- **TokenVault 现在就启用**(钉钉 OBO 的 refreshToken 需持久化 + 静默刷新 + 可撤销)——不再是为 Teams 预留的空置接口。
- 未来接 Teams/Entra,`CredentialContext` 背后换成 Graph delegated token,**上层与能力代码不变**。
- **裁剪层**(判断本人是否有权访问目标资源)当前为 no-op(信任授权范围 + 钉钉自身可见范围),留作可插拔接口按资源类型选择性加严。

---

## 8. 会话生命周期与异步模型

### 8.1 强制异步(钉钉 Stream 天然如此)
- 钉钉 **Stream 模式只收不发**:WebSocket 收消息,回复/发送必须走 HTTP OpenAPI 或回调里的 `sessionWebhook`。收发天然分离。
- 因此标准链路是:**Stream 收消息 → 后台异步跑 Agent Loop → HTTP 发消息(单聊 `oToMessages/batchSend` / 群聊 `groupMessages/send` / `sessionWebhook`)**。
- 这与 `Session.suspend / resume` 天然契合:能力因等待(如需授权、需人工确认)可挂起,事件到达后恢复。
- 每个 Session 有一个**串行 inbox**:同一会话的消息依次处理,不并发。这是 §8.4b 取消收尾与新消息串行的基础。

会话状态机:

```
  Idle ──收到消息──▶ RunningAgent ──工具调用 confirm()/consent──▶ AwaitingInteraction
    ▲                     │                                            │
    │                     │(无挂起,正常出结果)                        │ resolve(reply)
    │                     ▼                                            │  / timeout(30min)
    └──────────────── 回复用户,回到 Idle ◀───────────── resume 收尾 ◀──┘
```

- `RunningAgent → AwaitingInteraction`:工具执行中调用带外交互(见 §8.4b)时挂起。
- `AwaitingInteraction`:入站路由**先问状态**,匹配的回复直接喂给挂起点(绕过 AI);不匹配则触发取消(见 §8.4b)。
- 挂起状态 + pending 交互必须**可落盘可恢复**——异步 IM 下从插卡片到用户响应可能跨很长时间,Session 可能已被换出内存。

### 8.2 触发语义
- 群聊:钉钉**只把 @机器人 的消息回调给我们**,天然只在被 @ 时触发,不必自行过滤全群消息(也因此读不到未 @ 的群消息,见 §0 会话历史限制)。
- 私聊:用户消息即触发(单聊无需 @)。
- 发送者身份由 Stream 回调的 `senderStaffId`(userId)提供,用作 actor。

### 8.3 生命周期
- 机器人不在群里就收不到回调;**首次被 @** 时记录该 `openConversationId` 为已激活并发欢迎语(事实闸门)。
- 停用 / 数据:依赖**长期不活跃归档**策略;审计日志无论如何保留。
- (钉钉的加群/群变更事件可经事件订阅获取,是否用作显式激活闸门待验证;当前用"首次被 @"即可。)

### 8.4 入站路由(三岔口)

每条入站消息在进入 agent loop **之前**,先经一个**确定性分类器**(不经 LLM),优先级从上到下:

```
入站消息 ──▶ 分类器
              ├─ 1. 对 pending 交互的回复?  → resolve()          (§8.4b)
              ├─ 2. 指令消息 (/...)?         → 指令处理器          (§8.5)
              └─ 3. 其余                      → agent loop         (AI 驱动)
                                                   ▲
                          指令 handler 可选地经"注入 API"────┘
                          (指令影响 AI 的唯一途径,见 §8.5)
```

这是所有"消息不进 LLM、走确定性程序"情况的统一框架。每个跨边界的信息流都只有**一条明确管道**:AI 影响外部世界只经工具;确认结局告知 AI 只经 `Cancelled` 结果;指令影响 AI 只经注入 API。

### 8.4b 带外交互与系统通告(confirm / consent)

某些 OBO 操作(发邮件、改文档等)在执行副作用**前**需要用户**显式确认**。该确认由**工具代码自动触发,不由 AI 触发**——防止 AI 因 prompt injection 或自作主张执行不当动作。这条交互**整个绕开 LLM**。

#### 统一原语:SessionInterrupt

`confirm`(执行确认)与 `consent`(§6.3 的授权同意 `NeedsConsent`)是**同一原语**的两个实例——都是"挂起 → 往消息流插一条 → 等特定回复 → resume"。

```
SessionInterrupt (带类型的带外交互)
  ├─ kind: confirm | consent | (future: pick_option / input)
  ├─ payload          # 运行时渲染的内容(确认卡片 / 授权链接)
  ├─ correlation_id   # 关联即将到来的回复(卡片按钮回调携带,不可伪造)
  ├─ responder        # 只有此人(= actor)的回复才算数
  ├─ expires_at       # confirm 默认 30 分钟
  └─ resolve(reply) → 决议,唤醒挂起的工具
```

#### 三条安全属性(为何必须 tool 触发、绕过 AI)

1. **AI 不能跳过**:确认是工具代码里执行副作用前的确定性闸门,不调用 `confirm()` 就到不了发送那步。
2. **AI 不能伪造内容**:确认卡片展示的是**工具入参的真实值**(真实收件人、真实正文),由**运行时渲染**,非 AI 复述。
3. **AI 不能替用户回答**:回复在**路由层被拦截**直接喂给 `resolve()`,不进 agent loop。

#### 工具侧形态

```
async def send_email(ctx, to, subject, body):
    decision = await ctx.confirm(
        action="发送邮件",
        details={ "收件人": to, "主题": subject, "正文预览": body[:200] },
    )                        # ← 挂起点:运行时插卡片、等回复、resume
    if not decision.approved:
        return "已取消,未发送。"     # Cancelled 分支
    ... 真正调 API 发送 ...
```

内容来自**工具入参**而非 AI 措辞。工具还可**按条件**决定是否确认(如仅当收件人为外部联系人),比静态的能力级 flag 更灵活。

#### 取消:两个来源,归一处理

Session 处于 `AwaitingInteraction` 时,以下两种情况都产生 `Cancelled` 决议,**被确认的动作绝不执行**:

| 来源 | reason | 后续 |
|---|---|---|
| ① 用户发来新消息(未点按钮) | `superseded_by_new_message` | 收尾 + 新消息进入 AI 消息流 |
| ② 30 分钟未响应 | `timeout` | 收尾,Session 回到 Idle |

取消发生时,**两件事分工完成,不重叠**:

```
1. 运行时【直接推送】系统消息 "已取消:未确认,[发送邮件] 未执行。"
     ← 唯一对用户可见的取消通告,内容来自工具入参,不经 AI
2. 挂起的工具调用返回 Cancelled 结果给 agent loop
     → 收尾轮【静默】:AI 消费该结果,不产生任何对外消息
       (既然运行时已显式通告,AI 再开口就重复且矛盾)
     → 目的仅是让对话历史如实记录"该 action 被取消",供后续轮次上下文
3. (来源①)新消息作为新一轮进入 agent loop,正常回复
```

> **关键**:取消这件事,**对用户**由运行时系统消息负责说,**对 AI** 由对话历史里的 `Cancelled` 结果负责记。否则 AI 会以为动作执行了(它调用过工具并在等返回)→ 幻觉级错误。
> 收尾轮与新消息轮在 per-session 串行 inbox 里**依次**发生,不并发。

#### 出站消息的三个来源

这个设计把"谁能往用户发消息"理清为三类:

| 来源 | 经 AI? | 例 |
|---|:---:|---|
| AI 回复 | 是 | 对话内容 |
| 交互原语 | 否 | 确认卡片 / 授权链接 |
| 系统通告 | 否 | "已取消" / "已超时" / "已授权" |

后两者是**带外消息**,共同特征:内容来自真实状态/入参,**AI 不可改写、不可跳过**。

#### UI 与降级

- **首选交互式卡片按钮**(钉钉互动卡片 / AI 卡片,点击产生独立**卡片回调事件**,`correlation_id` 藏在回调里,不可伪造)。
- 纯文本"确认/取消"作为降级,靠 correlation 绑定,安全性更弱。

### 8.5 指令消息(带外指令通道)

用户**主动**触发的确定性指令(类比 claude-code 的 `/...`),走一张**独立于 AI 工具列表**的注册表,由程序直接执行,**默认完全跳过 AI**。

> **指令 ≠ 工具**。工具是 AI 在 agent loop 里**自主决定**调用的(可被 injection 影响);指令是用户**显式**下达、程序**直接**执行的(AI 无从干预)。两张不同的注册表,即使某些功能两边都想暴露。
>
> 与 §8.4b 对称:`confirm` 是工具→用户、程序等回复;指令是用户→系统、程序直接执行。方向相反,都绕过 LLM。

#### 触发语法(复用 §8.2 的 bot 身份判定)

```
DM:    "/reset"              正文以 / 开头即为指令
群聊:  "@助手 /reset"        @命中 bot 身份 → 再看正文是否 / 开头
```

群聊分类顺序:先 @过滤 → 再指令识别 → 否则当作给 AI 的自然语言。

#### 注册表条目

```
Command
  ├─ name: "/enable"
  ├─ availableIn: [dm] | [group] | [both]
  ├─ requires_role: user | channel_admin | org_admin   # 用 §3 保留的 actor 身份鉴权
  ├─ args_spec                                          # 参数解析
  └─ handler(ctx, args) → result
```

示例分两类:
- **会话控制类**(通常无需 OBO):`/reset` 清空上下文、`/help` 列能力、`/cancel` 主动取消当前 pending 确认(与 §8.4b 呼应)、`/whoami` 查身份绑定状态。
- **管理/配置类**(需权限校验):`/enable <cap>` 在本群启用能力(改 §6.2 的 `channel.enabledCaps`)、`/disconnect calendar` 撤销 OBO 授权 / 清 TokenVault(§7.2)、`/skills` 管理 user 私有能力。

#### 执行语义

handler 可做三件事,自由组合:

```
1. 改系统 / 会话状态          (清空上下文、改配置、启用能力…)
2. 回带外系统消息给用户        (指令的直接反馈,§8.4b 第 3 类出站,不经 AI)
3. [可选] 调"注入消息"API      (仅当需要 AI 后续知晓时,往 session 写一条)
```

- **默认完全跳过 AI**:`/reset`、`/enable` 等是对话**之外**的元操作,与 AI 无关。
- **影响 AI 的唯一途径 = 注入 API**:如 `/mode 简洁` 想让 AI 知道,就由该指令**主动**注入一条"用户已切换到简洁模式"。系统不替指令猜是否留痕——注入是指令可选调用的**能力**,不是系统的隐式**策略**。这保证机制唯一:全系统只有"注入消息到会话"这一个动作。
- **注入 API 的语义**:往指定 session 的对话历史 / inbox 追加一条消息。**具体形态待定**(见 §11)。

#### 权限

指令不进 agent loop,但**不免鉴权**——`requires_role` 校验发起者(actor)。这正是 §3"群聊记录发送者但不 impersonate"保留 actor 的用途之一:如 `/enable` 要求 `channel_admin`,普通成员发了即拒绝。

---

## 9. 信任边界与安全(不可裁剪项)

即便单组织,以下因"有用户数据访问 + 群聊 + 不可信输入"这一组合本身而必需:

- **不可信输入 / Prompt Injection**:IM 消息(转发内容、外部客户消息、文件)是不可信外部输入。高敏感 / 需用户上下文的能力须**人工确认 / 白名单**,不靠 LLM 自觉。
- **群聊零用户凭证**:群模式不持有任何用户凭证(见 §3),从根消除 confused-deputy。
- **审计**:凡涉及"以用户身份取数据",全链路记录"谁、代表谁、何时、何 scope、做了什么"。**确认 / 取消决议**(§8.4b:批准 / 拒绝 / 超时,针对哪个 action)同样入审计。
- **Tool 执行沙箱**:若 tool 执行任意代码/脚本,建议子进程/容器 + 受限 FS;Session 本体可共享进程 + 逻辑隔离(独立 workdir / 上下文 / 凭证视图)。

---

## 10. 配置(交付物,不在程序内抽象)

钉钉开发者后台的一次性设置(创建企业内部应用、加机器人能力、开 Stream 模式、申请权限点、配 OAuth 登录)以**《部署配置手册》**交付。程序只**读取**其产物:

```
app_key + app_secret           # 企业内部应用凭证(应用级 token、Stream、robotCode)
robot_code                     # 机器人身份(= app_key),出站消息发出者
oauth_client_id + secret        # 用户 OBO 授权(= app_key/secret)
oauth_redirect_uri             # OAuth2 回调(身份核对 + 换 userAccessToken)
```
> 钉钉 Stream 模式免公网域名,无需回调 URL / 加解密密钥(对比企业微信简化很多)。

---

## 11. 待定 / 后续

- [ ] **钉钉最小闭环 PoC**:企业内部应用 + Stream 模式,跑通 "@机器人 → 收 → LLM → 回复"(独立身份、多用户)。
- [ ] **钉钉 OBO PoC**:OAuth2 用户授权 → userAccessToken → 用 `me` 读本人日程/待办,验证真 delegated。
- [ ] `channel → enabled capabilities` 配置的具体格式与管理方式。
- [ ] Tool 执行沙箱的具体形态(子进程 vs 容器)。
- [ ] 被移出群后数据的归档 / 清理保留期;钉钉群变更事件是否用作显式激活闸门。
- [ ] 裁剪层首个落地资源类型(接钉盘等敏感资源时)。
- [ ] Stream 连接的重连 / 幂等 / 背压细节。
- [ ] `Authorizer` / `Requirement` 的最终接口签名与抽象 scope 命名规范。
- [ ] "注入消息"API 的形态(§8.5:指令影响会话的唯一途径)。
- [ ] 指令注册表与 AI 工具列表的关系边界(功能两边都想暴露时如何组织)。
- [ ] 不依赖群历史的"会话总结"替代形态(§0:两平台历史消息均受限)。

---

## 附:参考文档

- 平台能力细节:[dingtalk.md](dingtalk.md)(第一目标平台)、[wecom.md](wecom.md)(企业微信,参考/对比)
- 钉钉机器人概述:https://open.dingtalk.com/document/group/robot-overview
- 钉钉 Stream 模式:https://open.dingtalk.com/document/development/introduction-to-stream-mode
- 钉钉获取用户 token(OBO):https://open.dingtalk.com/document/development/obtain-user-token
- 钉钉日程 / 待办 / 钉盘:见 dingtalk.md §3 附链接
