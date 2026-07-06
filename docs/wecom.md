# 企业微信能力可用性总结

> 状态:调研 + 实测(v0.1)
> 目的:记录企业微信对本项目各能力的真实支持情况,作为架构与原型的事实依据。
> 结论会随企业微信接口演进而变化,文中标注了「调研」与「实测」来源。

---

## 0. 一句话结论

企业微信有**两条完全不同的接入通道**,能力边界差异巨大:

1. **传统自建应用 + 服务端 API(qyapi)**:老通道。能"写/建/发",但**几乎读不到用户既有的个人数据**(收件箱、个人日程、用户自建文档、群历史)。
2. **智能机器人 + 托管 MCP 通道(aibot,即 `wecom-cli` 用的)**:新通道,面向 AI Agent。能力广得多,**10 人以下小团队场景可读会话历史/日程/待办**——但 >10 人企业只剩文档+待办。

**本项目当前阶段聚焦通道 2 + 10 人以下场景**,已实测跑通(见 §5)。

---

## 1. 两条通道对比

| 维度 | 传统自建应用(qyapi) | 智能机器人 MCP(aibot / wecom-cli) |
|---|---|---|
| 定位 | 企业应用集成 | AI Agent 原生 |
| 入口 | `qyapi.weixin.qq.com/cgi-bin/*` | `aibot/cli/get_mcp_config` → 托管 MCP server |
| 认证 | corpid + 应用 secret → access_token | bot secret 签名 → 换各品类 MCP URL(凭证内嵌 URL) |
| 协议 | HTTP + 自定义加解密 | 标准 MCP(JSON-RPC:tools/list、tools/call) |
| 能力下发 | 固定,客户端已知 | **服务端动态下发**(schema、URL 都是运行时拉取) |
| 群聊身份 | 自建应用无群成员身份;群需另用智能机器人 | 智能机器人本身可入群、被 @ |
| 读用户既有数据 | 基本不行(见 §2) | 小团队场景可读会话/日程/待办(见 §3) |
| 组织规模门槛 | 无 | **有:>10 人砍掉 msg/schedule/meeting** |

---

## 2. 传统自建应用(qyapi)通道 —— 能力现实

来源:调研(官方文档)。核心结论:**能写不能读用户既有数据**。

### 2.1 消息
- 收:URL 回调(AES 加解密 + 签名),`FromUserName` = 企业内稳定唯一 userid。
- 发:`message/send`(主动推送),`access_token` 有效期 2h 需缓存。
- **被动回复必须 5 秒内**,LLM 出不来 → 必须"回调即返 200 + 后台异步 + 主动推送"。
- 自建应用**收不到群消息、进不了用户普通群、不能被 @**。

### 2.2 身份与授权
- **企业微信无 delegated token / OBO 模型**。
- 网页 OAuth2(`snsapi_base`/`snsapi_privateinfo`)**只能确认"你是谁"(userid)**,`code`/`user_ticket` 换不到可代表用户调 API 的令牌。
- 业务 API 一律用**应用级 access_token**,权限由**应用可见范围 + 后台数据权限**控制,同意主体是**管理员**。
- API 里的 userid 是**业务参数/过滤条件**,不是用户凭证。

### 2.3 各资源的读取边界(均为应用级,读不到用户既有数据)

| 能力 | 写/建 | 读用户既有数据 | 堵点 |
|---|---|---|---|
| 邮件 | ✅ 应用发信 | ❌ 读不到用户收件箱 | `exmail/app/get_mail_list` 只读**应用自己**的邮箱,无 userid 参数 |
| 日程 | ✅ 创建 | ⚠️ 只能读**应用自建**日历;用户个人日程仅"忙/闲"无标题 | `get_by_calendar` 限应用自建日历 |
| 文档 | ✅ 创建 | ❌ 只能读**应用自建**文档 | 官方明文"应用仅可读取自己创建的文档";docid 仅创建时返回一次 |
| 网盘 | ✅ 建空间 | ⚠️ 只能读**应用所在空间** | `file_list` 无 userid,靠 spaceid |
| 群会话历史 | — | ❌ 机器人读不到 | 唯一路径是独立付费的**会话存档 msgaudit** |

### 2.4 会话存档(msgaudit)—— 传统通道下读群历史的唯一路径
- 独立付费席位 + 独立 secret + RSA 私钥 + **强制合规告知/外部同意**。
- 需自建采集服务持续拉取落库(企业微信不长期存,新版 HTTP 接口仅 5 天内)。
- 是一个**独立重型子系统**,不是一个 tool。
- 官方文档:https://developer.work.weixin.qq.com/document/path/91774

### 2.5 "今日邮件总结"在传统通道下不可行
企业微信 API 读不到用户收件箱。若坚持要做,只能走**企业邮箱 IMAP/Exchange + 用户级授权**(专用授权密码/OAuth)——这是**企业微信之外**的独立授权体系,正好落在架构的 `CredentialContext` + `TokenVault` 抽象上。

---

## 3. 智能机器人 MCP 通道(wecom-cli)—— 能力现实

来源:调研 + 实测(读 wecom-cli 源码 + 实际调用)。

### 3.1 架构(读源码确认)
```
wecom-cli (Rust 薄客户端)
  扫码 → 拿 bot_id + secret(智能机器人凭证)
  secret 签名(sha256(secret+bot_id+time+nonce)) → POST cgi-bin/aibot/cli/get_mcp_config
     ↓
返回一组托管 MCP server URL,按品类:{ url, transport_type, is_authed, biz_type }
     ↓
标准 MCP(JSON-RPC tools/list / tools/call)调这些 URL
认证凭证【内嵌在 URL 里】(签发时确定),调用时不再带 header
```
- 工具 schema、MCP URL **全部服务端动态下发**,腾讯可随时增改能力,客户端不更新。
- `is_authed` 字段说明**每个品类可能需独立授权**(浏览器/扫码流程),接近我们架构的 `Authorizer` 三态 `NeedsConsent(url)`。
- 本地:`~/.config/wecom/bot.enc`(加密 bot 凭证)、`mcp_config.enc`(加密 MCP 配置缓存)。

### 3.2 组织规模门槛(关键限制)
- **10 人以下(个人/小团队)**:msg、doc、schedule、meeting、todo、contact 全开放。
- **10 人以上企业**:仅 doc + todo。**msg/schedule/meeting 被砍**。
- ⚠️ 我们产品定位是"组织内所有用户"(可能 >10 人),届时**会话总结这类依赖 msg 的招牌功能会失效**。当前原型阶段先聚焦 10 人以下。

### 3.3 各品类能力(实测账号所见)

| 品类 | 工具 | 备注 |
|---|---|---|
| contact | `get_userlist` | 仅当前用户可见范围成员,返回 userid/name/alias |
| msg | `get_msg_chat_list` / `get_message` / `get_msg_media` / `send_message` | **能读单聊+群聊历史(仅 7 天内)**;发送仅文本 ≤2048 字节 |
| doc | `create_doc` / `edit_doc_content` / `get_doc_content` / 智能表格/智能文档系列 | 读取仅限**自己创建**的文档;`get_doc_content` 异步轮询 |
| schedule | `get_schedule_list_by_range` / `get_schedule_detail` / `create_schedule` / `check_availability` 等 | 前后 30 天;时间入参字符串、返回 Unix 时间戳 |
| meeting | `create_meeting` / `list_user_meetings` / `get_meeting_info` 等 | 前后 30 天,上限 100 |
| todo | `get_todo_list` / `create_todo` / `update_todo` / `change_todo_user_status` 等 | 仅当前授权用户自己的待办,前后一个月 |

> 注意:msg 通道能读群历史,这**推翻了传统通道"必须上会话存档才能读群历史"的结论**——至少在小团队 MCP 通道下,读会话历史是直接支持的。

### 3.4 定位错位风险(战略判断)
- wecom-cli 定位是**"个人/小团队在终端用的 CLI"**:单个 bot secret 存本地,授权绑定到扫码那个人,读的是该 bot 可见范围的会话。
- 我们要的是**"服务器上为组织多用户服务、按用户隔离/授权"**。两者不完全对齐。
- 待验证:一个 bot 的 MCP 通道能否在服务端为多用户服务、授权能否按用户维度隔离(见 §6)。

---

## 4. 对架构文档(architecture.md)的印证与修正

- **印证**:`CredentialContext` / `Authorizer` 抽象是对的——MCP 通道的"每品类 URL + is_authed + 授权 URL"几乎就是这套抽象的现成实现。
- **修正 1**:§6.4 的招牌示例"今日邮件总结"在企业微信**原生不可行**,应替换为能跑通的示例(建文档 / 会话总结)。
- **修正 2**:企业微信 OBO 比预想更弱,多数读接口**连 userid 参数都没有**;§4 判定表右下角"对外·用户本人资源"在企业微信原生 API 下几乎为空。
- **修正 3**:异步轮询(如 `get_doc_content`)是常见模式,与 `suspend/resume` 会话模型契合,应作为工具适配的标准处理。

---

## 5. 实测记录(2026-07-05,已授权 10 人以下测试组织)

环境:`wecom-cli 0.1.9`,已 `init` 授权。调用形如 `wecom-cli <category> <method> '<json>'`,schema 用 `--schema` 获取。

| 测试 | 结果 |
|---|---|
| `contact get_userlist` | ✅ 返回 5 个成员 |
| `msg get_msg_chat_list`(近 7 天) | ✅ errcode 0,但 `chats: []`(测试账号近期无会话) |
| `msg` begin_time 超 7 天 | ❌ errcode 850016(边界即"不能早于 7 天前") |
| `todo get_todo_list` | ✅ errcode 0,空列表 |
| `schedule get_schedule_list_by_range` | ✅ schema/接口正常 |
| **doc 完整闭环** | ✅ `create_doc`(拿 docid)→ `edit_doc_content`(写 Markdown)→ `get_doc_content`(异步轮询 task_done→读回内容),全绿 |

**实测踩坑:**
- `get_doc_content` 的 `type` 参数含义是**返回格式(2=Markdown)**,不是文档类型,传文档类型(3)会报 40058。
- `get_doc_content` 异步:首次返回 `task_id` + `task_done:false`,需带 task_id 轮询到 `task_done:true`。
- task_id 含 `+` `/` `=` 特殊字符,**拼 shell 命令会坏**,必须用程序构造 JSON。
- 返回体是 MCP 包裹:`result.content[0].text` 里是**字符串化的 JSON**,需二次解析;正文含换行控制符,解析要容错。

---

## 6. 待验证问题(决定 MCP 通道能否作为多用户服务端底座)

- [ ] **>10 人企业到底能用哪些品类?** msg 是否真被砍(直接决定会话总结在目标客群的可行性)。
- [ ] **一个 bot 的 MCP 通道能否在服务端为多用户服务、授权能否按用户隔离?**
- [ ] **get_mcp_config / MCP 通道是否有稳定公开契约**,还是随时会变的内部接口。
- [ ] 智能机器人加群/被 @ 的回调,与 MCP 通道如何协同(交互前端 + 能力后端的拼接)。

---

## 附:关键链接

- 传统通道:自建应用接收消息 https://developer.work.weixin.qq.com/document/path/90238 、发送消息 https://developer.work.weixin.qq.com/document/path/90236 、网页授权 https://developer.work.weixin.qq.com/document/path/91023
- 会话存档:https://developer.work.weixin.qq.com/document/path/91774
- 智能机器人:概述 https://developer.work.weixin.qq.com/document/path/101039 、长连接 https://developer.work.weixin.qq.com/document/path/101463
- wecom-cli:https://github.com/WecomTeam/wecom-cli (本地 clone 在 `../wecom-cli`,skill 定义在 `../wecom-cli/skills`)
