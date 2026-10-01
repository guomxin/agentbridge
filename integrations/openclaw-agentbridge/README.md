# OpenClaw 的 AgentBridge 交互适配器

`agentbridge-interactions` 把中心 AgentBridge 的远程 MCP、可信交互和 Task Hub 接入
OpenClaw。它是宿主兼容层，不包含协同办公、泰华、语雀或照明系统的业务实现，也不修改
OpenClaw 核心源码。

当前版本：`0.4.100`

本版将组合任务规划检查提前至任务协调之前；独立填写不能绕过本轮来源约束。
新增未提交任务取消入口，自动区分普通卡片和持久计划，不撤销已提交的业务流程。

多个或全部 OA 待办使用 `oa_workflow_pending_batch_prepare` 建立通用持久批次；“上面的”事项传入已读取的精确 ID，
当前某类事项使用类型/关键词筛选。每项独立填写、授权和回读，中心自动返回下一张卡；不是模型循环单项工具。
默认上限 20，可显式设置到 100；超限或来源不完整时不启动。现有 `oa:write:approval` 足够，不新增权限范围。

可信卡片的只读状态查询遇到可重试网络错误时，在原有等待期限内退避重试；不会重放业务提交。
宿主运行状态日志保留清洗后的传输错误码，用于区分证书、连接和响应故障。

单项只读查询被登录打断时，中央服务按同任务的操作账本恢复原能力及完整参数，并先持久化结果消息、
再结束任务；Gateway 重启不再依赖内存中的查询描述。宿主收到中央续办完成回执后停止轮询，
不再重复查询或用心跳唤醒模型。组合计划和写操作仍使用原有计划或授权续办路径。

原生工具代理在 OpenClaw 裁剪结果前捕获可信卡片，交给同用户协调器后才返回清洗结果。
不将完整业务列表重新复制进 `details`，不将可信入口暴露给模型。TG、微信与 Workspace
的交互通知均可恢复接续；发起端不能仅凭 `origin_handled` 标记就忽略未接管的交互。
元数据恢复或卡片投递失败保留重试，已经发出续办请求但丢失响应时保留防重复执行状态。

同一用户消息可以先读取候选对象，再进入审批、填报、撤销等写操作。所有受治理的业务
`*_prepare` 入口都会建立独立任务，不复用已经由前置读取结束的任务；该写任务随后贯穿字段卡、
授权卡、提交和权威回读。登录入口仍绑定原读取任务，组合任务仍由持久计划统一协调。

兼容基线：OpenClaw `2026.7.1`
完整身份设计见 [智能体宿主多用户身份路由](../../docs/架构设计/智能体宿主多用户身份路由.md)。

## 一、职责边界

插件负责：

- 根据 Telegram、微信和 Agent Workspace 的可信宿主上下文选择 AgentBridge 身份；
- 为不同用户使用不同的环境变量 Token；
- 按 Token scope 裁剪模型可见工具；
- 把认证卡、字段卡和授权卡展示到合格端点；
- 有界轮询交互状态并在完成后续办；
- 同步任务文本、状态、图片和文件；
- 在登录完成后安全恢复允许自动重放的只读请求；
- 保持网页拉取端、Telegram 和微信各自正确的投递方式。

Telegram 回答文本在 `message_sent` 确认发送成功后同步，覆盖普通回复和
`message(action=send)` 工具发送。只接受已绑定且唯一的目标渠道/账号/会话路由，
失败发送和插件自身通知不重复发布；使用平台消息 ID 去重。微信专用卡片处理不变。

插件不负责：

- 保存或解释用户密码、验证码、Cookie 和业务字段；
- 实现目标系统流程、表单填充或提交逻辑；
- 让模型直接调用内部 commit、续办和 Task Hub 工具；
- 绕过 AgentBridge Token scope；
- 为 OpenClaw 修补网络代理、通道实现或核心源码。

## 二、模型工具面

中心 MCP 同时发布业务工具与宿主私有控制工具；受治理智能体工具由插件按身份权限动态裁剪：

- 读取工具；
- 受治理的 `prepare` 或登录入口；
- 一个本地身份状态工具。

内部 commit、可信交互续办、任务协调、端点管理和文件投递工具保持宿主私有。即使工具在模型
目录中可见，中央服务仍按当前用户 Token scope 逐次授权。

插件工具被限制性工具配置过滤时，需要显式加入：

```powershell
openclaw config set tools.alsoAllow '["agentbridge-interactions"]' --strict-json
```

不要使用 `group:plugins` 扩大全部插件权限。若 `tools.alsoAllow` 已有其他条目，应合并而不是覆盖。

## 三、安装

本地链接安装：

```powershell
openclaw plugins install --link D:\Codes\CLIExp\integrations\openclaw-agentbridge
openclaw config set env.vars.NODE_EXTRA_CA_CERTS "$env:USERPROFILE\.agentbridge\pki\root-ca.crt"
openclaw config set "plugins.entries.agentbridge-interactions.config.mcpUrl" https://10.10.50.213:8790/mcp
openclaw config set "plugins.entries.agentbridge-interactions.config.allowedCardOrigins[0]" https://10.10.50.213:8780
openclaw config set tools.alsoAllow '["agentbridge-interactions"]' --strict-json
openclaw plugins enable agentbridge-interactions
openclaw gateway restart
openclaw plugins inspect agentbridge-interactions --runtime --json
openclaw gateway status --deep --require-rpc --json
```

`NODE_EXTRA_CA_CERTS` 必须写入 OpenClaw 持久 `env.vars`，不能只在临时 PowerShell 环境中设置。
重建托管任务后，深度状态应在 `environmentValueSources` 中显示该变量。

源码链接并不意味着 Node 会自动重新加载模块。修改插件源码后必须完整重启 Gateway，并从启动
日志核对实际版本：

```text
AgentBridge interaction plugin registered (version=0.4.100, ...)
```

Windows 托管的 Gateway 重启可能超过两分钟。命令调用方超时不代表后台重启失败：

1. 等待至少 120 秒；
2. 不要重复执行重启；
3. 不要提前结束 Node 进程；
4. 最终检查 18789 监听、深度 RPC 和插件版本日志。

切换 Node/NVM 后若 Windows 计划任务丢失，使用：

```powershell
openclaw gateway install --force --json
openclaw gateway status --deep --require-rpc --json
```

## 四、单用户与多用户配置

### 4.1 单用户兼容模式

旧的单用户安装可以让插件复用全局 `mcp.servers.agentbridge` 地址和环境变量授权头。

```powershell
openclaw config set "mcp.servers.agentbridge.url" https://10.10.50.213:8790/mcp
openclaw config set "mcp.servers.agentbridge.timeout" 150
```

### 4.2 多用户模式

多用户部署不得共用一个全局 Bearer。配置一个 `mcpUrl` 和多条 `identityBindings`，每条绑定包含：

- 聊天通道；
- 发送者或网页身份；
- AgentBridge `userSubject`；
- 保存 Token 的环境变量名 `tokenEnv`；
- 允许卡片 Origin。

插件只把 `mcpUrl` 当作地址，根据可信发送者选择授权头；Token 值不写入配置、日志或模型上下文。
启用多用户绑定后应删除带共享 Bearer 的全局 MCP 服务配置，避免形成第二个共享身份工具面。

同一私聊或 Workspace 会话一旦绑定身份就不能切换。未知身份只能看到
`agentbridge_identity_status`，不能调用业务工具。

## 五、可信卡片投递

### 5.1 Telegram

HTTPS 可信卡优先显示 Telegram Web App 按钮。为兼容不信任用户安装内部 CA 的 Android
WebView，同一消息还可以显示“浏览器打开”入口。两个按钮都使用相同短期 URL，URL 只存在于
宿主展示元数据中，不进入模型结果。

### 5.2 微信

微信恢复活动时，已经延期的跨端聊天消息归档投递记录，不逐条补发历史；原始消息仍保留在 Workspace 时间线。新入站消息去重后才唤醒待处理通知，投递领取遵守重试与活动延迟时间。已结束任务的延期状态提醒归档；进行中的任务、可信卡、文件和计划结果仍可恢复。微信回复仅在带平台消息 ID 的 `message_sent` 确认成功后同步到时间线，忽略适配器无 ID 的重复成功回调。

腾讯微信适配器支持文本和媒体投递，但没有统一的卡片渲染器。插件把动作标签和短期 HTTPS
链接追加到宿主生成的出站文本；链接仍不进入模型。微信通道不可用时只报告通道错误，不得把
结果误投到 Telegram 或其他用户。

### 5.3 Agent Workspace

Workspace 是拉取式 Task Hub 客户端，通过 SSE 和历史接口获取有序文本、状态、卡片、图片和
文件。插件不得把 Workspace 当成聊天通道直推，也不得因为网页拉取失败额外唤醒模型。

同一可信交互可以在多个合格端展示，但服务端只能完成一次。完成后其他端同步终态，旧卡不会
长期停留在“等待用户”。主调用链与伴随端通知共享同一个续办占位；父任务进入终态后，插件会
终止该任务的残留轮询并清除恢复记录。中心端不会给终态任务重新签发协调租约，因此迟到通知
只会被确认并忽略，不会重复进入业务系统，也不会再产生虚假的 `RESUME_FAILED`。

## 六、完成后的续办

可信页面完成后：

1. 插件先读取服务端交互终态；
2. 若产生下一张可信卡，直接投递到原端点和其他合格端点；
3. 若业务已经结束，使用宿主固定文本投递成功、拒绝、过期或失败；
4. 需要模型继续理解结果时，使用不含秘密的私有唤醒事件；
5. 禁止同一轮重复调用 `agentbridge_interaction_get`。

### 6.1 登录后自动恢复

登录成功时，以下只读请求可以按白名单重放一次：

- 协同办公的已知流程集合读取；
- 泰华个人/团队日志和项目搜索；
- 语雀知识库、目录、搜索和选中文档读取；
- 照明系统的已注册只读查询。

只保留允许的过滤条件、日期和分页值，丢弃旧幂等键。重放仍使用原身份 MCP 客户端，结果回到
原任务和原通道。

草稿、审批、正式提交、会议、撤销和其他写工具绝不自动重放。字段卡和授权卡完成后也不能推断
成“重新登录后自动提交”。

## 七、跨端任务与时间线

插件在调用业务工具前把宿主请求绑定到中央 `AgentTask`：

- 网页、Telegram 和微信使用同一用户主体时可以继续同一任务；
- 文本、卡片和文件按服务端序号写入同一时间线；
- 相对表达“刚才、上一个、继续上面的任务”由受控任务引用解析；
- 多个候选任务时要求用户选择，不能猜测；
- 撤销是独立任务，通过业务对象关联原提交任务，不复用原提交卡片；
- 同一轮重复工具调用和重复卡片投递由幂等键与投递记录抑制。

网页刷新后从中央时间线恢复，不依赖浏览器内存。历史文件到期后保留“已过期”卡片，并可通过
新的准备操作重新生成下载入口。

## 八、文件交付

证书扫描件等文件由 AgentBridge 准备后绑定当前任务：

- 多文件使用一次批量准备调用；
- 每个文件独立投递，一个失败不阻断后续文件；
- Telegram/微信媒体发送失败时回退到同一短期下载链接；
- Workspace 展示文件名、大小、状态、到期时间和重新生成入口；
- 二进制不进入模型文本上下文；
- 插件不在 AgentBridge 授权过期后自行保留文件。

通道代理和重试由 OpenClaw 配置负责，插件不修改网络和代理设置。

## 九、超时与结果对账

协同办公提交可能包含浏览器准备、CAP4 多阶段发送和权威回读，MCP 超时应至少为 150 秒。

MCP 请求尚未获得响应时，插件只对以下工具执行两次短时传输恢复，等待间隔为 0.5 秒和 2 秒：

- 标记为幂等的只读工具；
- 对模型开放、尚未执行业务提交的 `prepare` 工具。

`prepare` 重试始终复用由工具调用生成的稳定 `idempotency_key`。底层连接错误码会保留在宿主
日志中，恢复成功只记一条诊断，不向用户重复播报失败。内部 `commit`、提交后的权威回读、
`MCP_TIMEOUT` 和 `RESULT_UNKNOWN` 均不进入该重试路径。

宿主超时不等于下游接受或拒绝。任何已经越过提交边界的超时必须：

1. 查询 AgentBridge 操作账本；
2. 查询下游已发、待办或详情等权威集合；
3. 能确认时返回明确结果；
4. 不能确认时返回 `RESULT_UNKNOWN`；
5. 在未对账前不自动重试。

## 十、诊断命令

私聊中：

- `/agentbridge status`：查看不含秘密的身份和运行诊断；
- `/agentbridge pending`：重新显示当前未过期可信交互。

`openclaw agent --deliver` 可以执行工具和发送模型文本，但它绕过正常入站回复路径，因此该命令
只出现文本不能证明卡片渲染失败。卡片验收应使用目标私聊的真实入站消息，或在同一私聊执行
`/agentbridge pending`。

`oa_session_status` 只检查会话，不创建认证卡。`SESSION_CHECK_UNAVAILABLE` 表示保留会话并稍后
重试，不能要求用户重新输入密码。要测试登录卡，应明确请求登录，使模型调用 `oa_session_login`。

## 十一、测试与打包

```powershell
Set-Location D:\Codes\CLIExp\integrations\openclaw-agentbridge
npm test
npm run pack:check
```

验收至少确认：

- 包版本、入口常量和运行时日志一致；
- CLI 与 Gateway 版本一致且深度 RPC 正常；
- 插件状态为 `loaded`，无版本漂移；
- 两个身份使用不同 Token 和工具范围；
- 未绑定用户不能调用业务工具；
- 三类卡片在 Telegram、微信和 Workspace 正确展示；
- 登录后只读请求自动恢复，写请求不自动重放；
- 网页拉取不触发聊天直推或重复模型唤醒；
- 同一任务文本、卡片和文件不重复、不乱序、不串用户。
