# 钉钉能力可用性总结

> 状态:调研(v0.1,2026-07-06)。尚未实测,结论来自官方文档 + 开发者百科/apifox 镜像交叉核对。
> 目的:评估钉钉作为本项目 IM 平台的可行性,并与企业微信对比(见 [wecom.md](wecom.md))。
> 结论:**钉钉在本项目的三个核心诉求(独立机器人身份、多用户、真正的 OBO)上全部满足,明显优于企业微信,建议作为原型与第一目标平台。**

---

## 0. 一句话结论

我们在企业微信踩到的每一个致命坑,钉钉都更好:

| 核心诉求 | 企业微信 | 钉钉 |
|---|---|---|
| 机器人独立身份 | wecom-cli 通道显示为授权用户本人 ❌ | ✅ 企业内部应用机器人是独立身份("XX助手") |
| 多用户 | wecom-cli "仅创建者可对话" ❌ | ✅ 群里任何人 @ 都能触发 |
| 主动发消息 | 要求"用户先发过消息" | ✅ 直接推,无前置 |
| 免公网部署 | 长连接可行 | ✅ Stream 模式(WebSocket),官方推荐 |
| **真正的 OBO** | 无 delegated token ❌ | ✅ **OAuth2 用户级 token + refresh_token + `me` 接口族** |
| 读用户既有数据 | 只能读应用自建的 ❌ | ✅ 钉盘/日历/待办可到用户维度 |
| 群历史消息 | 需付费会话存档 | ❌ 同样受限(先跳过) |

**唯一劣势**:钉钉没有企业微信 wecom-cli 那种现成 CLI 工具箱,原型需自己写代码调 OpenAPI / Stream SDK。

---

## 1. 机器人与消息收发

### 1.1 机器人类型

| 类型 | 归属 | 收消息 | 发消息 | 单聊 | 群聊 | 调 OpenAPI |
|---|---|---|---|---|---|---|
| **企业内部应用机器人(推荐)** | 企业内部应用 | ✅ | ✅ | ✅ | ✅ | 全部 |
| Webhook 自定义机器人 | 群 | ❌ | 仅发 | ❌ | 仅群 | ❌ |
| Stream 模式 | 是"接入方式"不是机器人类型 | — | — | — | — | — |

- **企业内部应用机器人**是唯一能接收消息、被 @ 回调的形态,具备完整 OpenAPI 能力。这就是我们要用的。
- Webhook 自定义机器人只能往一个群单向推送(告警场景),排除。

### 1.2 机器人身份(与企业微信的关键差异)

- 钉钉企业内部应用机器人是**独立身份**:群里/单聊显示为机器人自己(设置的名称+头像,带"机器人"标识),**不是代表授权用户本人**。
- 有自己的 `robotCode`(= AppKey),身份在开发者后台单独配置。
- **这正是企业微信 wecom-cli 通道做不到的**(那边发消息显示成授权人徐辰)。钉钉天然符合架构 §4 的 `BotIdentity`"对内恒用助手自己身份"模型。

### 1.3 接收消息 / 被 @

- **群聊**:仅"@机器人"的消息才回调(不 @ 收不到)——与架构 §8.2 触发语义一致。
- **单聊**:用户直发即收到,无需 @。
- **接收机制**:两种二选一——**Stream 模式(WebSocket 长连接,推荐)** 或 HTTP 回调(需公网地址)。
- **能拿到发送者身份**:回调体含 `senderStaffId`(= 企业内 userId,可直接用于回消息/查通讯录)、`senderNick`、`conversationType`(1=单聊 2=群聊)、`conversationId`/`openConversationId`、`sessionWebhook`(临时回复地址,有过期)。

### 1.4 主动发消息(非被动回复)

用应用级 AccessToken 调 OpenAPI,**无"用户须先发过消息"的前置限制**:
- 单聊(批量):`POST /v1.0/robot/oToMessages/batchSend`(`robotCode` + `userIds` + `msgKey` + `msgParam`)
- 群聊:`POST /v1.0/robot/groupMessages/send`(`openConversationId` + `robotCode` + ...)
- 需申请"企业内机器人发送消息权限";目标用户须在机器人可见范围内;群聊需机器人已在群内;有频率限制;`msgKey` 用钉钉消息模板(sampleText / sampleMarkdown / 互动卡片等)。

### 1.5 Stream 模式(服务端部署的关键便利)

- WebSocket 反向长连接,钉钉把回调推给服务端。支持:机器人收消息、事件订阅、互动卡片回调。
- 优势:**零公网 IP / 零域名 / 零证书 / 零验签 / 零内网穿透**,只要能出网即可。官方推荐,非常适合服务端 AI 助手。
- ⚠️ **重要限制**:Stream 只能"收",**不能通过 WebSocket 通道"回消息"**。回复/发送仍须走 HTTP:调 OpenAPI(需 AccessToken)或用回调里的 `sessionWebhook`。
- 典型架构:**Stream 收消息 → 服务端 AI 处理 → HTTP OpenAPI 发消息**。
- 官方 Python SDK:https://github.com/open-dingtalk/dingtalk-stream-sdk-python

---

## 2. 身份认证与授权(OBO —— 钉钉的核心优势)

钉钉同时提供两套 token,这是它能做真正 OBO 的根本。

### 2.1 应用级 access_token

- 企业内部应用:AppKey + AppSecret 换取。旧版 `GET oapi.dingtalk.com/gettoken`;新版 `POST api.dingtalk.com/v1.0/oauth2/accessToken`。
- 有效期 7200s,有效期内重复获取返回同一 token,需自行缓存;无 refresh 概念,过期用 Key/Secret 再换。
- **应用维度授权**,权限来自后台勾选的权限点 + 可见范围。调用用户相关接口靠传 userid/unionId 参数——**这一层与企业微信同源**。

### 2.2 用户级 access token(delegated / OBO)

标准 OAuth2 授权码流程:
```
① 引导用户到授权页
   https://login.dingtalk.com/oauth2/auth?redirect_uri=...&response_type=code
     &client_id={AppKey}&scope=openid&prompt=consent
   (scope=openid corpid 可额外返回用户所选组织 corpId)
② code 换用户 token
   POST https://api.dingtalk.com/v1.0/oauth2/userAccessToken
   { clientId, clientSecret, code, grantType: "authorization_code" }
   → 返回 accessToken + refreshToken + expireIn
③ 过期用 grantType=refresh_token + refreshToken 静默刷新,无需用户重新授权
```
- **令牌代表"用户 + 应用",按用户本人权限裁剪数据** —— 这就是 delegated/OBO,企业微信没有。
- 企业内部应用与第三方应用在**用户级授权上流程一致**(差别仅在应用级 token 获取链路)。

### 2.3 `me` 接口族(OBO 的落地形态)

新版 v1.0 API 统一用 header `x-acs-dingtalk-access-token`,该 header **既可放应用级 token,也可放用户级 token**。放用户 token 且访问 `.../users/me/...` 路径时,即以操作人身份、按其权限访问——真正的 OBO。

### 2.4 身份核对

用户 token 调 `GET /v1.0/contact/users/me` 拿 unionId/openId/nick,与"操作发起人"比对 → 落实架构 §7.2 的身份核对(OIDC 式确认"授权人 == 发起人")。

### 2.5 两种"代表用户访问"的方式(设计选项)

钉钉实际给了两条路,可按敏感度选择:

| 方式 | 授权主体 | 特点 |
|---|---|---|
| 应用级 token + unionId 参数 | 管理员(后台一次性授权权限点) | 免逐用户 OAuth,方便;但不是按用户本人权限,更像"应用代管" |
| 用户级 token(`me`) | 用户本人(OAuth 同意) | 合规、按本人权限裁剪;需引导授权 + 持久化 refreshToken |

> 这正好对应架构里 `CredentialContext` 的可插拔:低敏感场景用应用级+unionId,高敏感/需用户本人授权的场景用 OBO。

---

## 3. 业务能力(能否读取用户"本人已有"数据)

授权级别:**应用级+unionId** 表示管理员授权后免逐用户 OAuth;标注 OBO 表示可用用户 token 走 `me`。

| 能力 | 能读用户本人已有数据 | 授权级别 | 关键接口 |
|---|---|---|---|
| **钉盘/云盘** | ✅(含个人空间 personal) | 应用级+unionId,或 OBO | `/v1.0/storage/spaces/...`、`/v1.0/drive/spaces` |
| **日历/日程** | ✅ 读本人日程含标题详情 | 应用级(path 带 userId),或 OBO(`me`) | `/v1.0/calendar/users/{userId}/calendars/{calendarId}/events`、`/v1.0/calendar/primary` |
| **待办** | ✅ 读本人待办列表 | 应用级+unionId,或 OBO(`me`) | `/v1.0/todo/users/{unionId}/tasks` |
| **文档/知识库** | ⚠️ 可读有权限的已有文档(块级内容),受该资源可见范围约束 | 应用级+unionId,或 OBO | `/v1.0/doc/...`、`/v1.0/wiki/...`、"获取我的文档知识库" |
| **通讯录** | ✅(组织数据) | 纯应用级 | `topapi/v2/user/get`、`/v1.0/contact/users/me`、unionId 互查 |
| **会议/音视频** | 云录制内容可取 | 应用级+unionId | `/v1.0/conference/videoConferences/{id}/cloudRecords/getVideos` |
| **会话历史** | ❌ 读不到聊天历史 | — | 无等价"会话存档"通用 API |

要点:
- **相比企业微信的根本超越**:日历/待办/钉盘都能到**用户维度**读"本人已有的"数据,而不是只能读"应用自己创建的"。企业微信这三项都被限死在应用自建范围。
- 大量 v1.0 API 需要 `unionId`,先用通讯录接口建立 userId↔unionId 映射。
- 授权点开了 ≠ 能看别人私有未共享文件,仍受钉盘/文档本身可见范围约束。

### 3.1 会话历史:同样受限
- 钉钉**无**面向普通企业的通用"会话存档 / 拉取群历史消息"API。
- 机器人只能实时收到"@它的消息",拿不到历史,也拿不到与本应用无关的会话。
- 有合规审计类能力但门槛高(近专属/私有化方案),不在通用开放平台范围。
- 结论:**"群会话总结依赖历史消息"这条,钉钉和企业微信一样先跳过**。可做的是"机器人被 @ 后基于当次上下文/主动汇报"这类不依赖历史回溯的形态。

---

## 4. 对架构文档(architecture.md)的印证

- **§4 身份方向性**:钉钉独立机器人身份完美对应"对内恒用 BotIdentity";DM 里的 OBO 有真实 delegated token 支撑 → 判定表右下角"对外·用户本人资源"**在钉钉上是实的**(企业微信几乎是空的)。
- **§6 授权流水线 / §7.2 身份核对 / TokenVault**:原本为 Teams/Entra 预留的 OAuth consent + 用户 token 存储 + 身份核对,**在钉钉上就能完整跑起来**,不必等接微软。`CredentialContext` 的"未来:真正 per-user token"这一支,钉钉即可兑现。
- **§8.1 强制异步**:钉钉 Stream 收 / HTTP 发的分离,与 suspend/resume、"收消息与回消息不同通道"天然契合。
- **能力现实**:会话历史受限这条与企业微信一致,`architecture.md` 中依赖群历史的功能(会话总结)在两个平台都需另寻路径。

---

## 5. 应用形态对比

| 维度 | 企业内部应用(用这个) | 第三方企业应用(ISV) |
|---|---|---|
| 服务范围 | 只服务本企业 | 上架应用市场,服务多企业 |
| 应用级 token | AppKey+AppSecret 直接换 | SuiteKey/Secret → suite_ticket → corp_access_token |
| 用户级 token / OBO | ✅ | ✅ |

单组织场景 → **企业内部应用**,授权链路最简单。

---

## 6. 建议的原型技术路径

1. 钉钉开发者后台创建**企业内部应用** + 机器人能力,配置独立名称/头像 → 得到独立身份 + AppKey/AppSecret。
2. 消息接收选 **Stream 模式**(免公网域名),集成官方 Python Stream SDK。
3. 申请权限:机器人发送消息、通讯录读取(拿 userId/unionId);按需再加日历/待办/钉盘。
4. 收消息:Stream 收 → `senderStaffId` 拿发起人 → LLM 处理 → HTTP 发消息(单聊 `oToMessages/batchSend` / 群聊 `groupMessages/send` / 或 `sessionWebhook`)。
5. OBO 场景(读本人日程/待办/钉盘):引导用户走一次 `login.dingtalk.com/oauth2/auth` → 换 userAccessToken → 持久化 refreshToken → 后续静默刷新。
6. 记住的坑:**Stream 只收不发**,发必须走 HTTP OpenAPI / sessionWebhook。

最小闭环(对标企业微信用 wecom-cli 跑的,但这次是真·独立身份 + 真·可交互):
```
@机器人 → Stream 收消息 → LLM → HTTP 回复    (群/单聊,机器人自己身份)
```

---

## 7. 待验证 / 待办

- [ ] 实测:注册企业内部应用 + Stream 模式,跑通"@机器人→收→LLM→回复"最小闭环。
- [ ] 实测:OAuth2 用户授权 → userAccessToken → 用 `me` 读本人日程/待办,验证真 OBO。
- [ ] 确认各能力权限点的后台申请流程与审批门槛。
- [ ] `msgKey` 支持的消息模板 / 互动卡片(含 AI 卡片流式输出)清单。
- [ ] 钉钉文档正文结构化读取的实际完整度(块级内容够不够喂 LLM)。

---

## 附:关键官方文档

- 机器人概述:https://open.dingtalk.com/document/group/robot-overview
- 接收消息:https://open.dingtalk.com/document/orgapp/receive-message
- 机器人发送消息:https://open.dingtalk.com/document/orgapp/robot-reply-and-send-messages
- 批量发单聊:https://open.dingtalk.com/document/isvapp/chatbots-send-one-on-one-chat-messages-in-batches
- Stream 模式:https://open.dingtalk.com/document/development/introduction-to-stream-mode
- 获取用户 token(OBO 核心):https://open.dingtalk.com/document/development/obtain-user-token
- 获取应用 access_token:https://open.dingtalk.com/document/development/api-gettoken
- 用户个人信息(me):https://open.dingtalk.com/document/orgapp/dingtalk-retrieve-user-information
- 日程:https://open.dingtalk.com/document/orgapp/query-schedule-list
- 待办:https://open.dingtalk.com/document/orgapp/add-dingtalk-to-do-tasks
- 钉盘/存储:https://open.dingtalk.com/document/orgapp/networkdisk-overview
- Python Stream SDK:https://github.com/open-dingtalk/dingtalk-stream-sdk-python
