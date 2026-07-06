# 任务列表 — 钉钉 AI 助手

> 依据 [PLAN.md](PLAN.md) 拆解,按执行顺序排列。
> 状态标记:`[TODO]` 未开始 / `[WIP]` 进行中 / `[DONE]` 已完成 / `[BLOCKED]` 受阻。
> coding agent 执行完一个任务后更新其标题里的状态。
> 技术栈:Python 3.11+ / asyncio / `dingtalk-stream` SDK / Anthropic SDK / SQLite / `cryptography`。
> 分层与目录见 PLAN.md §3。所有小节引用指向 [docs/architecture.md](docs/architecture.md)。

---

## 约定(所有任务通用)

- 包根目录 `src/`,以 `python -m src.main` 启动。
- 配置:`.env`(密钥)+ `config.yaml`(非密钥);用 `infra/config.py` 统一读取,**禁止**在代码里硬编码密钥。
- 钉钉 OpenAPI 基址:新版 `https://api.dingtalk.com`,旧版 `https://oapi.dingtalk.com`;新版用 header `x-acs-dingtalk-access-token`。
- 所有对外调用有超时、有结构化日志(`infra/log.py`);错误不吞掉。
- 每个任务完成后:能 `python -m src.main` 启动不报错;新增逻辑配最小单测(`tests/`)。
- 异步:全程 asyncio;阻塞 HTTP 用 `httpx.AsyncClient`。

---

# M0 — 环境与骨架

## [DONE] T01 初始化项目骨架与依赖
- 建目录结构(PLAN.md §3):`src/{adapters/dingtalk,core,capabilities/{system,base,user},infra}`、`tests/`,每个包加 `__init__.py`。
- `pyproject.toml`:依赖 `dingtalk-stream`、`anthropic`、`httpx`、`pyyaml`、`python-dotenv`、`cryptography`、`aiosqlite`;dev 依赖 `pytest`、`pytest-asyncio`、`ruff`。
- `src/main.py`:空的 asyncio 入口(`async def main()` + `asyncio.run`),打印启动日志。
- `.env.example`:列出 `DINGTALK_APP_KEY` `DINGTALK_APP_SECRET` `DINGTALK_ROBOT_CODE` `ANTHROPIC_API_KEY` `OAUTH_REDIRECT_URI` 占位。
- `.gitignore`:`.env`、`*.db`、`__pycache__`、`.venv`。
- **验收**:`python -m src.main` 能启动并打印日志;`pytest` 能跑(即使 0 用例)。
- **完成记录(2026-07-07)**:
  - 已创建 PLAN.md §3 要求的 Python 包目录、`__init__.py`、`src/main.py` 异步入口、依赖元数据、环境变量示例、忽略规则、README 和最小 smoke test。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`python -m src.main`、`.venv/bin/pytest`。

## [DONE] T02 配置加载与日志基础设施
- `infra/config.py`:`load_config()` 合并 `.env`(dotenv)+ `config.yaml`,返回带类型的配置对象(dataclass);缺必填项报清晰错误。
- `config.yaml`:含 `llm.model`(默认 `claude-sonnet-5`)、`session.confirm_timeout_sec: 1800`、`dingtalk.api_base`、日志级别等非密钥项。
- `infra/log.py`:结构化日志(JSON 行或 key=value),提供 `get_logger(name)`。
- **验收**:单测覆盖"缺失必填项报错""正常加载";`get_logger` 可用。
- **完成记录(2026-07-07)**:
  - 已实现 `src/infra/config.py` typed dataclass 配置加载,合并 `.env` 与 `config.yaml`,缺少必填密钥时报告具体变量名。
  - 已新增默认 `config.yaml` 非密钥配置,实现 `src/infra/log.py` JSON 行结构化日志与 `get_logger(name)`,并将入口日志切换到该基础设施。
  - 已补充配置加载与日志可用性单测,并更新 README 的配置说明。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`。

## [DONE] T03 钉钉应用级 access_token 客户端
- `infra/dingtalk_client.py`:`DingTalkClient`。
  - `async get_access_token()`:调 `POST https://api.dingtalk.com/v1.0/oauth2/accessToken`(body `appKey`/`appSecret`),返回 `accessToken`+`expireIn`。
  - **进程内缓存 + 提前 5 分钟刷新**;并发安全(asyncio.Lock)。
  - `async api_post(path, json, use_user_token=None)` / `api_get(...)`:自动带 `x-acs-dingtalk-access-token`(应用级或传入的用户级);统一错误处理(记录 errcode/errmsg)。
- **验收**:能拿到 token 并缓存;第二次调用不重复请求网络(单测用 mock)。
- **完成记录(2026-07-07)**:
  - 已新增 `src/infra/dingtalk_client.py`,实现 `DingTalkClient`、`AccessToken`、`DingTalkAPIError`、应用级 access_token 获取和 `asyncio.Lock` 保护的进程内缓存。
  - 已实现提前 5 分钟刷新、`api_post` / `api_get` 自动注入 `x-acs-dingtalk-access-token`(应用级或传入用户级 token),并对 HTTP/API 错误记录 `errcode`/`errmsg` 后抛出异常。
  - 已补充 mock 单测覆盖 token 获取与缓存、提前刷新、并发只请求一次、应用/用户 token 请求头和 API 错误日志。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`。

## [DONE] T04 出站发送与通讯录冒烟
- 在 `DingTalkClient` 加:
  - `async send_oto(user_ids: list[str], text: str)` → `POST /v1.0/robot/oToMessages/batchSend`(`robotCode`、`userIds`、`msgKey="sampleText"`、`msgParam={"content":...}`)。
  - `async send_group(open_conversation_id, text)` → `POST /v1.0/robot/groupMessages/send`。
  - `async get_user_list()` / `async user_by_id(userid)`:通讯录接口,建立 userId→姓名映射。
- `scripts/smoke_send.py`:给指定 userId 发一条测试消息 + 打印通讯录。
- **验收**:运行冒烟脚本,自己钉钉能收到机器人消息;通讯录能列出成员。
- **完成记录(2026-07-07)**:
  - 已在 `DingTalkClient` 增加 `send_oto()`、`send_group()`、`get_user_list()`、`user_by_id()`,统一复用应用级 access_token、`sampleText` 文本消息模板和结构化 API 错误处理。
  - 已新增 `DingTalkUser` 归一化模型,支持通讯录分页读取并生成 `userId → 姓名` 映射。
  - 已新增 `scripts/smoke_send.py`,可对指定 `userId` 发送单聊冒烟消息并打印指定部门通讯录;README 已补充运行示例。
  - 已补充 mock 单测覆盖单聊发送、群聊发送、分页通讯录映射和按 userId 查询。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`、`.venv/bin/python scripts/smoke_send.py --help`。
  - 真实钉钉发送冒烟需具备本地 `.env` 凭据和目标 `userId`;当前环境未配置 `DINGTALK_SMOKE_USER_ID`,未执行对真实账号的发送。

## [DONE] T05 【REVIEW】M0 骨架与钉钉接入审阅
- 审阅 T01–T04:目录/依赖是否符合 PLAN.md §3;token 缓存与刷新是否正确、并发安全;密钥是否只从配置读取无硬编码;错误处理是否完整;日志是否结构化。
- 跑 `ruff` + `pytest`,确认无告警、用例通过。
- 输出问题清单并修复;确认冒烟脚本真实可用。
- **完成记录(2026-07-07)**:
  - 已审阅 T01–T04 的项目目录、依赖、配置/日志、钉钉 access_token 缓存与提前刷新、并发锁、应用/用户 token 请求头、OpenAPI 错误记录与冒烟脚本。
  - 问题清单:冒烟脚本前序完成记录引用 `DINGTALK_SMOKE_USER_ID`,但脚本、`.env.example` 和 README 未支持/说明该可重复本地冒烟入口。已修复为可选 positional `user_id`,缺省时从环境变量或 `.env` 读取 `DINGTALK_SMOKE_USER_ID`,并补充单测与文档。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`、`.venv/bin/python scripts/smoke_send.py --help`。
  - 当前环境无 `.env` 和真实 `DINGTALK_SMOKE_USER_ID`,未对外部钉钉账号执行真实发送;脚本所需凭据、机器人发消息权限和通讯录读取权限已在 README 中明确。

---

# M1 — 最小对话闭环

## [DONE] T06 钉钉 Stream 接入与消息归一化
- `adapters/dingtalk/stream.py`:用 `dingtalk-stream` SDK 建 WebSocket 连接,注册机器人消息回调(chatbot callback)。
- `adapters/dingtalk/message.py`:定义 `InboundMessage` dataclass(`text`、`sender_staff_id`、`sender_nick`、`conversation_type`(1单聊/2群聊)、`conversation_id`、`open_conversation_id`、`session_webhook`、`msg_id`);把 SDK 回调体归一化成它。
- 回调里先只做:归一化 → 打日志 → 交给一个 `on_message(InboundMessage)` 回调(下一任务接 LLM)。
- **验收**:私聊机器人 / 群里 @机器人,服务端日志能打印出归一化后的 `InboundMessage`,字段正确。
- **完成记录(2026-07-07)**:
  - 已新增 `src/adapters/dingtalk/message.py`,实现 `InboundMessage`、`MessageNormalizationError` 与 SDK/raw callback 到归一化消息的转换,覆盖文本、发送者、会话、webhook 和消息 ID 字段校验。
  - 已新增 `src/adapters/dingtalk/stream.py`,用 `dingtalk-stream` SDK 创建 Stream client,注册 `ChatbotMessage.TOPIC` 回调,在回调中归一化、结构化记录 `dingtalk_inbound_message`,再交给异步 `on_message(InboundMessage)`。
  - 已为 `python -m src.main` 增加可选 `--stream` 接收模式;默认无参数启动仍不依赖本地 `.env`,便于 smoke 检查。
  - 已补充单测覆盖归一化成功、必填字段缺失、非文本拒绝、单聊 `openConversationId` 兼容、SDK topic 注册和 invalid callback ACK。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`、`.venv/bin/python -m src.main --help`。
  - 当前环境无真实钉钉 `.env`/Stream 连接凭据,未执行私聊机器人和群 @ 机器人的外部人工验证;`--stream` 路径已就绪,具备凭据和应用 Stream 配置后可直接观察归一化日志。

## [DONE] T07 触发判定与出站封装
- `adapters/dingtalk/outbound.py`:`async reply(inbound, text)` —— 优先用 `session_webhook`(未过期)否则回退 OpenAPI(单聊 `send_oto`、群聊 `send_group`,按 `conversation_type` 选)。
- 触发判定(架构 §8.2):单聊直接触发;群聊钉钉只回调 @ 消息,直接视为触发。把非文本消息类型先记录并回一句"暂只支持文本"。
- **验收**:能把一段固定文本正确回到来源会话(单聊和群聊各验证一次)。
- **完成记录(2026-07-07)**:
  - 已新增 `src/adapters/dingtalk/outbound.py`,实现 `DingTalkOutbound.reply()` 和模块级 `reply(...)`,优先使用未过期 `sessionWebhook`,否则按 `conversation_type` 回退单聊 `send_oto()` 或群聊 `send_group()`。
  - 已扩展入站归一化以保留 `sessionWebhookExpiredTime`,并为非文本消息生成可回复的 `UnsupportedInboundMessage` 元数据;Stream 不再把非文本消息直接拒为 bad request。
  - 已新增触发判定,单聊消息和群聊 @ 回调直接进入处理;当前 T07 阶段对文本消息回复固定文本 `收到`,非文本消息记录类型并回复 `暂只支持文本`。
  - 已补充单测覆盖 webhook 优先、单聊/群聊 OpenAPI 回退、非文本归一化与 Stream 分发、运行时固定回复和不支持消息回复;README 已更新 Stream 行为说明。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`、`.venv/bin/python -m src.main --help`。
  - 当前环境无真实钉钉 `.env`/Stream 连接凭据,未执行单聊和群聊外部人工发送验证;相关发送路径已由 mock HTTP 测试覆盖,具备凭据和 Stream 配置后可用 `python -m src.main --stream` 验证。

## [DONE] T08 接入 Claude,一问一答
- `infra/llm.py`:`LLMClient`,封装 Anthropic SDK;`async complete(system, messages) -> str`;模型从配置读(默认 `claude-sonnet-5`);超时与错误处理。
- 在 `on_message` 里:`InboundMessage.text` → `LLMClient.complete` → `outbound.reply`。**先无历史、无工具、无 Session**。
- 系统提示:简短说明"你是企业内 AI 助手"。
- **验收**:@机器人或私聊,能得到 LLM 回复;群里发送者显示为机器人独立身份(人工确认)。
- **完成记录(2026-07-07)**:
  - 已新增 `src/infra/llm.py`,实现 `LLMClient.complete(system, messages) -> str`,封装 Anthropic async Messages API,从配置读取模型/API key,设置请求超时,并将 Anthropic 错误包装为 `LLMError` 后记录结构化日志。
  - 已将 T07 的固定文本回复替换为一问一答 LLM 路径:`InboundMessage.text` 以无历史、无工具、无 Session 的单轮 `user` 消息发送给 Claude,再通过既有 `DingTalkOutbound.reply()` 回到来源会话。
  - 已设置系统提示 `你是企业内 AI 助手。请简洁、准确地回答用户问题。`;非文本消息仍不进入 LLM,继续回复 `暂只支持文本`。
  - 已补充 LLM wrapper 与主消息处理单测,并更新 README 的 Stream 行为说明。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`、`.venv/bin/python -m src.main --help`。
  - 当前环境无真实钉钉 Stream/Anthropic `.env` 凭据,未执行私聊/@ 机器人外部人工验证;代码路径已由 mock 单测覆盖,具备凭据后可用 `python -m src.main --stream` 验证真实 LLM 回复和机器人独立身份。

## [DONE] T09 【REVIEW】M1 对话闭环审阅
- 审阅 T06–T08:Stream 回调是否稳健(异常不断连);归一化字段是否齐全;出站 webhook/OpenAPI 回退逻辑;LLM 调用错误处理。
- 端到端人工验证:单聊、群聊 @ 各跑一次,确认独立身份。
- 跑 `ruff`+`pytest`;输出问题清单并修复。
- **完成记录(2026-07-07)**:
  - 已审阅 T06–T08 的 DingTalk Stream 注册与回调处理、文本/非文本归一化字段、触发判定、`sessionWebhook` 优先与单聊/群聊 OpenAPI 回退、Claude 单轮调用与错误包装。
  - 问题清单:未发现需要修改生产代码的缺陷;为审阅关注点补充了回归覆盖,包括 Stream `on_message` 异常被捕获且后续消息仍可处理、Anthropic SDK 异常被包装为 `LLMError`。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`。
  - 当前环境无 `.env`,且缺少 `DINGTALK_APP_KEY`、`DINGTALK_APP_SECRET`、`DINGTALK_ROBOT_CODE`、`ANTHROPIC_API_KEY`、`OAUTH_REDIRECT_URI` 环境变量,未执行真实单聊/群聊 @ 的外部人工验证;M1 相关路径已由 mock 单测覆盖,具备凭据和钉钉 Stream 配置后可用 `python -m src.main --stream` 进行人工复验。

---

# M2 — 会话运行时

## [DONE] T10 SQLite 存储层
- `infra/store.py`:用 `aiosqlite`;建表 `sessions`、`messages`(会话历史)、`identity_bindings`、`audit_log`、`token_vault`(后续里程碑用);提供异步 CRUD 封装。
- 迁移/建表在启动时幂等执行。
- **验收**:单测覆盖建表 + 基本读写。
- **完成记录(2026-07-07)**:
  - 已新增 `src/infra/store.py`,基于 `aiosqlite` 实现 `SQLiteStore`、幂等 schema 初始化和 `sessions`、`messages`、`identity_bindings`、`audit_log`、`token_vault` 五张表。
  - 已提供会话、消息历史、身份绑定、审计日志和 token vault 的异步读写/更新/删除封装,并用 JSON 字段保留后续 Session/能力/OBO 扩展所需的灵活元数据。
  - 已新增 `storage.database_path` 非密钥配置,并在 Stream 启动路径中执行数据库初始化;README 已补充 SQLite 初始化说明。
  - 已补充单测覆盖建表幂等性和五张表的基本 CRUD 行为。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`。

## [DONE] T11 Session 抽象与路由
- `core/session.py`:`Session` dataclass(架构 §5:`kind(dm|group)`、`bot`、`principal`、`actor`、`context`(历史)、`state`(Idle/RunningAgent/AwaitingInteraction)、`lifecycle`)。
- `core/session_manager.py`:按 `conversation_id` 取/建 Session;群聊共享一个 Session,`actor` 每条消息更新为发送者;持久化到 `sessions` 表。
- 首次激活:群聊首次被 @ 记录激活 + 发欢迎语(架构 §8.3)。
- **验收**:同一会话多次消息命中同一 Session;actor 正确随发送者变化。
- **完成记录(2026-07-07)**:
  - 已新增 `src/core/session.py`,定义 `Session`、`BotIdentity`、`Principal`、`Actor` 以及 `Idle` / `RunningAgent` / `AwaitingInteraction` 状态和 `dm` / `group` 会话类型。
  - 已新增 `src/core/session_manager.py`,按 DingTalk `conversation_id` 取/建持久化 Session;私聊 principal 绑定触发用户,群聊 principal 绑定 `openConversationId`,同一群共享一个 Session。
  - 已将 Stream 处理路径接入 `SessionManager`,每条触发消息都会更新当前 `actor`;群聊首次激活会记录 `activated`、`activated_by`、`activation_msg_id` 并发送欢迎语后继续正常处理。
  - 已补充单测覆盖同一会话命中同一 Session、群聊 actor 随发送者变化、首次群激活欢迎语路由;README 已更新 Session runtime 行为说明。
  - 已验证:`.venv/bin/ruff format .`、`.venv/bin/ruff check .`、`.venv/bin/pytest`、`python -m src.main`。

## T12 `[TODO]` per-session 串行 inbox
- `core/inbox.py`:每个 Session 一个 asyncio 队列 + 单 worker 协程,消息**依次**处理不并发(架构 §8.1)。
- 全局调度:`on_message` 改为"入队到对应 session 的 inbox";worker 取出后走处理流程。
- **验收**:对同一会话快速连发 3 条,日志显示严格串行处理;不同会话可并行。

## T13 `[TODO]` 多轮上下文与 agent loop 骨架
- `core/agent_loop.py`:维护对话历史(从 `messages` 表加载/追加);`async run(session, user_text)`:组装历史 → LLM → 回复 → 存历史。
- 预留 `suspend/resume` 状态位与 tool 调用挂点(工具在 M3 接);状态机 `Idle→RunningAgent→Idle`(架构 §8.1)。
- **验收**:多轮对话有上下文(如"我叫X"后再问"我叫什么"能答对);历史落库。

## T14 `[TODO]` 【REVIEW】M2 会话运行时审阅
- 审阅 T10–T13:Session 路由是否正确(群共享/actor 更新);串行 inbox 是否真串行、不同会话真并行;历史加载是否有上限/截断策略;状态机流转是否清晰。
- 并发压力小验证:多会话并发不串味。
- 跑 `ruff`+`pytest`;输出问题清单并修复。

---

# M3 — 能力层(无 OBO)

## T15 `[TODO]` Capability 模型与三级目录加载
- `capabilities/base.py`:`Capability`(架构 §5:`name`、`origin(system|base|user)`、`available_in(list of global|group|dm)`、`requires(list[Requirement])`、`sensitivity`、`handler`);`Requirement`(`service`、`scopes`、`on_behalf_of`)。
- `capabilities/registry.py`:从 `system/ → base/ → user/<userid>/` 三级目录加载并叠加(架构 §6.2)。
- **验收**:能注册并列出能力;三级叠加顺序正确(user 覆盖 base 覆盖 system 同名)。

## T16 `[TODO]` 可见性闸门 canUse
- `capabilities/registry.py` 加 `can_use(cap, mode, actor, channel) -> bool`,**纯函数**,逻辑严格按架构 §6.1:
  - `requires_user_authority`(= requires 中有 on_behalf_of 项)且 mode≠DM → False。
  - global → True;DM 且 origin=user 且 actor 拥有 → True;Group 且在 channel.enabledCaps → True;否则 False。
- `channel → enabled capabilities` 配置读取(架构 §6.2)。
- **验收**:单测覆盖 §6.1 每条分支;OBO 类能力在群聊被过滤。

## T17 `[TODO]` agent loop 接入工具执行(Claude tool use)
- `core/agent_loop.py`:把 `can_use` 过滤后的能力转成 Claude tool 定义;LLM 请求工具 → 执行 handler → 回填结果 → 继续循环,直到无工具调用。
- 工具执行错误 → 作为 tool_result 回给 LLM(不崩溃)。
- **验收**:LLM 能选择并调用一个占位工具(如 echo),结果回填后继续对话。

## T18 `[TODO]` 首批应用级工具(无 OBO)
- `capabilities/system/`:
  - `contact_lookup`:userId↔姓名(用 T04 通讯录)。
  - `create_doc`:建钉钉文档 + 写入内容(参考 wecom 实测的建文档→写入闭环;钉钉用 `/v1.0/doc` 或知识库接口,`available_in=[dm,group]`,应用级 token)。
  - `create_todo`:创建待办(应用级 + unionId,dingtalk.md §3)。
- 均 `requires=[]`(无 OBO)。
- **验收**:群里说"帮我建个文档记录XX",机器人真的建出文档并回链接。

## T19 `[TODO]` 【REVIEW】M3 能力层审阅
- 审阅 T15–T18:Capability 模型是否贴合架构 §5;`can_use` 是否与 §6.1 完全一致(重点边界);工具执行错误处理;三级目录叠加正确性;首批工具是否真调用成功(非 mock)。
- 跑 `ruff`+`pytest`;端到端验证建文档;输出问题清单并修复。

---

# M4 — OBO 授权(架构核心)

## T20 `[TODO]` TokenVault(用户级 token 加密存储)
- `infra/token_vault.py`:`(principal, service) → {user_access_token, refresh_token, scopes, exp}`,存 `token_vault` 表,值用 `cryptography` Fernet 加密(密钥从 `.env` 读)。
- API:`get(principal, service)`、`put(...)`、`revoke(principal, service)`;`get` 返回时若快过期返回"需刷新"标记。
- **验收**:单测覆盖存/取/撤销/加密(密文不可读)。

## T21 `[TODO]` 钉钉 OAuth2 端点与 code 换 token
- `infra/oauth.py` + 一个轻量 HTTP 服务(`aiohttp`/`fastapi`,原型可临时域名/隧道):
  - `/oauth/start?nonce=`:查 pending,构造 `https://login.dingtalk.com/oauth2/auth?...client_id=APP_KEY&response_type=code&scope=openid&state=nonce&redirect_uri=...&prompt=consent`,302 跳转。
  - `/oauth/callback?code=&state=`:校验 state(单次、短时效)→ `POST /v1.0/oauth2/userAccessToken`(`clientId`/`clientSecret`/`code`/`grantType=authorization_code`)→ 得 `accessToken`+`refreshToken`+`expireIn`。
- `PendingAuthStore`:`nonce → {principal, session, service, scopes, exp}`,单次使用。
- **验收**:走完浏览器授权能拿到 userAccessToken+refreshToken(手动验证一次)。

## T22 `[TODO]` 身份核对与 TokenVault 落库
- 回调拿到 user token 后:调 `GET /v1.0/contact/users/me`(带用户 token)取 `unionId`;核对 == pending 里记录的 actor 身份(架构 §7.2),不符则拒绝并作废 nonce。
- 核对通过 → 写入 TokenVault;唤醒挂起的会话(resume)。
- **验收**:用他人账号完成授权会被拒(人工构造验证);本人授权成功落库。

## T23 `[TODO]` 静默刷新
- `infra/dingtalk_client.py`/`token_vault`:用户 token 过期时用 `grantType=refresh_token`+refreshToken 换新;刷新失败(refresh 失效)→ 清 vault 条目,标记需重新授权。
- **验收**:模拟 token 过期,能自动刷新;refresh 失效时正确降级。

## T24 `[TODO]` Authorizer 三态与 CredentialContext
- `capabilities/authorizer.py`:`async resolve(requirement, actor, mode)` → `Granted(handle) | NeedsConsent(url) | Denied(reason)`(架构 §6.3):
  - 查 TokenVault:有效/可刷新 → Granted;无 → 生成 pending+授权 url → NeedsConsent;群模式需 OBO → Denied。
- `capabilities/credential.py`:`CredentialContext`,按资源选应用级+unionId 或用户级 OBO(架构 §7.1);向工具暴露 `ctx.user.*` / `ctx.group.*`。
- agent loop:工具执行前对其 `requires` 逐条 `resolve`;NeedsConsent → 挂起会话 + 发授权链接(接 M5 的 consent 原语,或本任务先用简单发链接方式,M5 再归并)。
- **验收**:单测覆盖三态;缺授权时会话挂起并发出链接。

## T25 `[TODO]` OBO 工具:今日日程总结(招牌 case)
- `capabilities/system/schedule_summary.py`:`requires=[Requirement(service="calendar", scopes=["calendar:read"], on_behalf_of="actor")]`,`available_in=[dm]`。
- handler:用用户 token 调 `/v1.0/calendar/users/me/...`(先 `/v1.0/calendar/primary` 取主日历,再查当天 events),拿到日程 → 交 LLM 总结 → 返回。
- **验收**(架构 §6.4):私聊"总结我今天的日程"→ 首次弹授权 → 授权后读到**本人**日程并总结;再问无需授权(静默刷新)。

## T26 `[TODO]` 【REVIEW】M4 OBO 审阅
- 审阅 T20–T25:OAuth 流程 state/nonce 防护;**身份核对是否真能挡住冒名授权**(重点安全项);TokenVault 加密与撤销;三态逻辑;静默刷新;`me` 接口是否真按本人权限。
- 端到端:日程总结 case 完整跑通(含首次授权 + 二次免授权)。
- 跑 `ruff`+`pytest`;输出问题清单并修复。

---

# M5 — 带外交互(confirm / consent / 通告)

## T27 `[TODO]` SessionInterrupt 原语与 AwaitingInteraction
- `core/interrupt.py`:`SessionInterrupt`(架构 §8.4b:`kind(confirm|consent)`、`payload`、`correlation_id`、`responder`、`expires_at`、`resolve`);pending 表(可落盘,架构 §8.1)。
- Session 状态机加 `RunningAgent→AwaitingInteraction→resume`。
- **验收**:能创建一个 interrupt 并挂起会话;resolve 后恢复。

## T28 `[TODO]` confirm 卡片与回调匹配
- `ctx.confirm(action, details)`:发钉钉互动卡片(按钮:确认/取消),`correlation_id` 藏卡片回调数据;挂起等回复。
- `adapters/dingtalk/stream.py`:注册卡片回调事件;`core/router.py` 把卡片回调按 `correlation_id`+responder 匹配到 pending → `resolve()`(绕过 LLM)。
- 内容来自**工具入参**,非 LLM 措辞(架构 §8.4b 安全属性)。
- **验收**:一个"发通知"类工具执行前弹确认卡,点确认才执行,点取消则不执行。

## T29 `[TODO]` 取消双来源与系统通告
- 取消来源(架构 §8.4b):AwaitingInteraction 时收到新消息(`superseded_by_new_message`)/ 30分钟超时(`timeout`)→ `Cancelled`。
- 分工:运行时**直接推**系统消息("已取消:…[action]…未执行",内容来自入参)+ 挂起工具返回 Cancelled → agent 收尾轮**静默**(仅历史留痕)。
- 出站三来源约束落实(架构 §8.4b);把 M4 的 NeedsConsent 归并为 `consent` 实例。
- **验收**:等确认时发新消息 → 收到"已取消"系统消息 + 新消息被正常处理;超时同理;AI 不重复播报取消。

## T30 `[TODO]` 【REVIEW】M5 带外交互审阅
- 审阅 T27–T29:correlation_id 是否不可伪造、responder 校验;取消两来源归一;系统通告 vs AI 收尾静默是否严格分工(防幻觉);pending 是否可落盘恢复。
- 端到端:confirm、超时取消、新消息取消三条路径各验证。
- 跑 `ruff`+`pytest`;输出问题清单并修复。

---

# M6 — 指令通道(slash command)

## T31 `[TODO]` 入站三岔口分类器
- `core/router.py`:入 agent loop 前的确定性分类器(架构 §8.4):① 命中 pending interaction → resolve ② `/` 开头(群聊需先 @ 命中)→ 指令处理器 ③ 其余 → agent loop。
- **验收**:三类消息分别走对分支;单测覆盖分类逻辑。

## T32 `[TODO]` 指令注册表与鉴权
- `core/commands.py`:`Command`(架构 §8.5:`name`、`available_in`、`requires_role(user|channel_admin|org_admin)`、`args_spec`、`handler`);注册表**独立于 AI 工具表**。
- `requires_role` 用 actor 鉴权(架构 §3);越权拒绝。
- 注入消息 API:`inject_message(session, text)` —— 指令影响会话的唯一途径(架构 §8.5)。
- **验收**:注册表可列出;越权指令被拒;注入 API 能让后续 agent 轮看到。

## T33 `[TODO]` 首批指令
- 实现:`/help`(列可用能力/指令)、`/reset`(清会话上下文)、`/whoami`(查身份绑定/授权状态)、`/connect <service>`(主动触发 OBO 预热授权)、`/disconnect <service>`(清 TokenVault)、`/cancel`(主动取消当前 pending 确认)。
- 默认完全跳过 AI;需影响会话的用 `inject_message`。
- **验收**:每条指令按预期工作;`/connect calendar` 能主动走授权;`/reset` 后上下文清空。

## T34 `[TODO]` 【REVIEW】M6 指令通道审阅
- 审阅 T31–T33:三岔口优先级正确;指令表与工具表边界清晰;鉴权到位;`/connect`/`/disconnect` 与 TokenVault 一致;注入 API 是否是唯一影响 AI 的途径。
- 跑 `ruff`+`pytest`;端到端验证各指令;输出问题清单并修复。

---

# M7 — 加固(可试运行)

## T35 `[TODO]` 审计日志
- `infra/audit.py`:记录 OBO 取数、confirm/取消决议、指令执行(谁/代表谁/何时/何 scope/做了什么),写 `audit_log` 表(架构 §9)。
- 在 Authorizer、interrupt resolve、command handler 三处埋点。
- **验收**:上述操作都留下可查审计记录。

## T36 `[TODO]` 错误恢复与鲁棒性
- Stream 断线自动重连(指数退避);消息按 `msg_id` 去重(幂等);access_token 失效重取;出站发消息限流(防刷屏)。
- 会话状态 + pending interaction 进程重启后可从 SQLite 恢复(架构 §8.1)。
- **验收**:杀掉进程重启,进行中的授权/确认能恢复;断网恢复后自动重连;重复消息不重复处理。

## T37 `[TODO]` 不可信输入边界与可观测
- 高敏感工具强制走 confirm/白名单(架构 §9),不依赖 LLM 自觉;在 Capability 上用 `sensitivity` 标记并由运行时强制。
- 关键指标:消息量、工具调用数、授权成功率、错误率(结构化日志或简单计数)。
- **验收**:标记为高敏感的工具必定触发 confirm;指标可从日志观察。

## T38 `[TODO]` (可选)Tool 执行沙箱
- 若已引入执行任意代码/脚本的工具:子进程/容器 + 受限 FS;Session 逻辑隔离(独立 workdir/上下文/凭证视图,架构 §9)。无此类工具则记录"暂不需要"并跳过。
- **验收**:沙箱内工具无法越权访问其他 session 的 workdir/凭证。

## T39 `[TODO]` 【REVIEW】M7 加固 + 全系统终审
- 审阅 T35–T38 + 回归全链路:审计完整性、重连/幂等/恢复、敏感边界、(沙箱)。
- 全系统端到端走查:M1 对话、M3 建文档、M4 日程总结(含授权)、M5 确认+取消、M6 指令,逐一验证。
- 跑 `ruff`+`pytest` 全量;确认与 architecture.md 无重大偏离;输出终审报告与遗留问题清单。

---

## 里程碑与 review 任务对照

| 里程碑 | 任务 | Review |
|---|---|---|
| M0 环境骨架 | T01–T04 | **T05** |
| M1 对话闭环 | T06–T08 | **T09** |
| M2 会话运行时 | T10–T13 | **T14** |
| M3 能力层 | T15–T18 | **T19** |
| M4 OBO 授权 | T20–T25 | **T26** |
| M5 带外交互 | T27–T29 | **T30** |
| M6 指令通道 | T31–T33 | **T34** |
| M7 加固 | T35–T38 | **T39** |
