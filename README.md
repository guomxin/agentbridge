# AgentBridge 智能体能力桥接平台

AgentBridge 使用 Python 构建，以非侵入方式把遗留 B/S 系统封装为智能体可调用的业务能力，
不修改目标系统源码、二进制和数据库，也不把浏览器、Cookie、密码或底层接口暴露给智能体。

当前运行形态是中心化 AgentBridge：

- CLI、远程 MCP 和 OpenClaw 共用同一业务能力内核；
- 每个用户拥有独立的系统身份绑定、会话、浏览器 Profile、权限和操作账本；
- 登录、业务字段填写和最终授权通过模型循环之外的可信卡片完成；
- 正式写入遵循“准备、授权、提交、核验”的固定流程；
- Telegram、微信和 Agent Workspace 支持跨端任务、文本、卡片、图片和文件同步；
- 管理控制台提供持久化链路、运行事件、试点 SLO、安全恢复账本和可验证每日备份；
- OpenClaw 与独立 Reference Host 使用同一 `agentbridge.host.v1` 契约和远程 MCP；
- 跨系统目标可编译为中心端持久任务计划，按已注册能力和确定性转换执行，等待登录、字段或授权后自动续办；
- 已接入协同办公、泰华日志、语雀部门信息库和照明实验室四个系统；
- Chrome 扩展、旧浏览器桥、localhost daemon 和代理型 CLI 已于 2026-07-13 退役。

项目当前状态见 [项目当前状态](docs/项目当前状态.md)，全部文档从
[文档导航](docs/文档导航.md) 进入。

## 一、核心架构

```text
Telegram / 微信 / Agent Workspace / 其他 MCP 宿主
                         |
                         v
                 OpenClaw 或 MCP 客户端
                         |
                         v
              AgentBridge 中心能力与治理层
       +-----------------+------------------+
       |                 |                  |
   身份与会话        可信交互与任务       操作账本与审计
       |                 |                  |
       +-----------------+------------------+
                         |
                         v
       协同办公 / 泰华日志 / 语雀 / 照明实验室
```

智能体调用的是 `oa.workflow.pending.list`、`taihua.work_log.create.prepare`、
`smartlight.alarm.analysis` 这类有业务含义的能力，而不是任意 HTTP、JavaScript、DOM、
选择器或数据库语句。

## 二、运行要求

- Python 3.12 或更高版本；
- Playwright 支持的 Chromium；
- 中心节点能够访问目标遗留系统；
- Linux 使用权限受限的 32 字节会话密钥；
- 远程访问使用固定私网 IP、HTTPS 和内部 CA；
- OpenClaw 当前兼容基线为 `2026.7.1`，插件当前版本见 [项目当前状态](docs/项目当前状态.md)。

本地开发安装：

```bash
python -m pip install -e .
python -m playwright install chromium
python -m agentbridge.cli.main --home .agentbridge system init-seeyon-oa
```

## 三、业务能力

查看中央能力目录：

```bash
python -m agentbridge.cli.main --home .agentbridge capability list
python -m agentbridge.cli.main --home .agentbridge capability describe oa.template.list
python -m agentbridge.cli.main --home .agentbridge capability describe oa.business_trip.prepare
```

截至 2026-10-10，经 `scripts/current_facts.py` 核对，当前源码的中央注册表包含 110 个业务能力。
此处统计代码注册项，部署及实际验收以[项目当前状态](docs/项目当前状态.md)中的带日期证据为准：

| 系统 | 数量 | 主要范围 |
| --- | ---: | --- |
| 协同办公 | 67 | 模板、流程与意见、证书、组织通讯录、发起、审批、知会、确认、会议、会议室申请与撤销 |
| 泰华日志 | 5 | 个人日志、团队日志、项目搜索、日志填写与创建 |
| 语雀部门信息库 | 4 | 知识库、跨库目录、组织搜索、结构化文档读取 |
| 照明实验室 | 34 | 静态资产、运行态、RTU/单灯告警、能耗、RTU/单灯巡测、真实支路漏电、巡检与检修、报告、RTU 告警受控写入 |

独立数据库能力不计入上述业务适配器注册表。当前源码的中央 MCP 目录共 158 个工具，还包含会话、可信交互、任务、文件和治理入口；
实际宿主可见目录受身份授权与宿主等级约束。模型只看到读取能力和受治理的写入
入口，内部提交、续办和宿主协调工具不会暴露给模型。

### 跨系统任务计划

宿主先调用 `agentbridge_task_plan_catalog` 读取当前 Token 可用的安全计划目录，再通过
`agentbridge_task_plan_prepare` 提交结构化计划。中央服务校验能力、参数、依赖、Scope、数据绑定
和副作用后持久执行；`agentbridge_task_plan_get` 和 `agentbridge_task_plan_cancel` 用于查看与取消。
一期允许多个读取和注册转换，但最多只有一个受控写入终点；正式写入仍必须经过字段核对、独立授权、
私有 commit 和权威回读。

### 协同办公能力

读取能力区分不同页面集合，不把“待办、已发、已办、跟踪”混成一个概念：

- `oa.template.list`
- `oa.workflow.pending.list`
- `oa.workflow.sent.list`
- `oa.workflow.done.list`
- `oa.workflow.tracked.list`
- `oa.workflow.detail.get`
- `oa.workflow.opinions.list`
- `oa.document.certificate.search`
- `oa.addressbook.organization.tree`
- `oa.addressbook.department.members`
- `oa.addressbook.person.search` / `person.get`
- `oa.addressbook.group.list` / `group.members`
- `oa.addressbook.private_contact.search` / `private_contact.get`
- `oa.addressbook.export`
- `oa.meeting_room.availability.list`
- `oa.meeting_room.my_applications.list`
- `oa.meeting_room.application.prepare` / `application.create`
- `oa.meeting_room.application.cancel.prepare` / `application.cancel`

写入能力按具体业务流程组织，已覆盖出差、请假发起与请假待办审批、补签、效能数据、差旅费、劳动合同续签、
知识产权申报、加班、考勤确认、周报、普通协同、会议和流程撤销。智能体只调用每个流程的
准备入口；最终提交能力由可信授权续办，不作为普通模型工具公开。

补签接收处理还提供 `oa.missed_punch.approval.batch.prepare`：中心端冻结当前用户的补签待办，逐项展示
独立字段卡和授权卡，当前项权威成功后自动进入下一项，不需要用户在事项之间发送“继续”。

协同办公详细能力见 [事项能力矩阵](docs/系统适配/协同办公系统/事项能力矩阵.md)、
[OA 通讯录只读能力](docs/系统适配/协同办公系统/通讯录只读能力.md)和
[OA 会议室读取能力](docs/系统适配/协同办公系统/会议室读取能力.md)和
[OA 会议室申请与撤销能力](docs/系统适配/协同办公系统/会议室申请与撤销能力.md)。

### 泰华日志能力

- `taihua.work_log.my.list`
- `taihua.work_log.team.list`
- `taihua.project.search`
- `taihua.work_log.create.prepare`
- `taihua.work_log.create`

正常读写使用中心 Token 会话和刷新机制，不需要每次打开浏览器。详见
[泰华日志系统适配说明](docs/系统适配/泰华日志系统/泰华日志系统适配说明.md)。

### 语雀部门信息库能力

- `yuque.public_books.list`
- `yuque.document.catalog`
- `yuque.document.search`
- `yuque.document.read`

当前账号无法创建正式访问令牌，登录通过按挑战隔离的 noVNC 可信浏览器完成滑块和短信验证。
读取阶段对 Doc、Sheet、Table、表格、图片文字、链接和附件元数据做统一结构化；搜索结果不返回
可能泄露秘密的服务端摘要。详见
[语雀部门信息库适配说明](docs/系统适配/语雀部门信息库/语雀部门信息库适配说明.md)。

### 照明实验室能力

读取能力覆盖静态概览、灯杆与资产、当前运行状态、RTU 告警、单灯告警、能耗、RTU/单灯巡测、
真实 RTU 支路漏电、巡检统计、检修记录和报告导出。旧“漏电”入口保留兼容，但智能体的普通
漏电请求固定进入真实 RTU 支路漏电工具。写入能力聚焦
一条精确 RTU 告警的备注、工区提交/撤回和不可逆处置，不开放通用设备控制、任意告警处置或
通用增删改查。

每次写入都冻结目标快照，执行授权、提交前并发检查和权威回读。可逆动作提供补偿路径，不可逆
处置单独标记并要求独立权限。详见
[照明实验室系统适配说明](docs/系统适配/照明实验室系统/照明实验室系统适配说明.md)。

## 四、可信登录与会话

用户需要登录下游系统时，AgentBridge 返回短期可信认证卡：

1. 用户在卡片中填写账号、密码、验证码或完成交互式登录；
2. 凭据通过同源 TLS 直接进入中心凭据代理，不经过模型、MCP 参数或聊天；
3. AgentBridge 建立每用户中心会话并核验实际下游主体；
4. 登录成功后自动恢复原始任务，不要求用户重新发送请求；
5. 会话按系统采用 Cookie 探测、Token 刷新、CAS/JWT 再签或浏览器保活。

当前保活间隔为 10 分钟，活动租约为 7 天。后台探测不会无限续租，也不会把下游已经注销的
会话伪装为有效。实际登录身份与预期主体不一致时，会话立即隔离并失败关闭。

## 五、可信字段与写入授权

写操作分为三个用户可理解的阶段：

```text
填写业务信息 -> 核对冻结计划 -> 明确授权执行
```

完整服务端状态机为：

```text
prepare -> field input -> authorize -> internal commit -> verify
```

安全要求：

- 字段卡根据用户对话中已经给出的信息预填；
- 系统自动计算字段和只读字段不能伪装成用户可编辑字段；
- 授权绑定用户、系统、会话、能力版本、目标、计划哈希和有效期；
- 授权只能消费一次，跨用户、跨会话、过期或目标漂移均拒绝；
- 提交成功必须通过目标系统权威页面或接口回读；
- 结果未知时停止且不自动重试，先由用户或运维对账。

详细约束见 [受控写入模型](docs/架构设计/受控写入模型.md) 和
[智能体交互协议](docs/架构设计/智能体交互协议.md)。

## 六、多用户与多端任务

每个 MCP Token 在服务端绑定：

- `userSubject`；
- 允许系统和权限范围；
- 每个系统的预期下游主体；
- Token 有效期和审计原因。

同一用户可以在 Telegram、微信和 Agent Workspace 使用同一业务身份继续任务，但设备和通道
不会扩大权限。不同用户使用独立 Token、会话、Profile、账本和回复路由。

Agent Workspace 支持：

- 一次性身份配对与持久登录；
- 流式模型输出；
- 跨端有序文本、状态和应用卡片；
- 图片输入、预览、放大和下载；
- 任务文件交付、过期展示和重新生成下载入口；
- 使用“继续刚才的任务”等自然语言跨端接续，而不是要求记忆任务号。

详见 [多端智能体任务接续设计](docs/架构设计/多端智能体任务接续设计.md) 和
[智能体工作台](docs/平台能力/智能体工作台.md)。

## 七、管理控制台

管理控制台位于 `https://10.10.50.213:8782`，与普通用户工作台分离。当前可查看和治理：

- 用户、下游身份绑定和系统会话；
- MCP Token、权限范围和有效期；
- 操作、可信交互、多端任务和任务文件；
- 写入暂停、会话失效和追加式管理审计；
- AgentBridge、Workspace Gateway 和 OpenClaw 的脱敏运行状态。
- OpenClaw 与 Reference Host 的兼容等级、实例健康和协调 Lease。

管理账户不会自动获得任何下游业务身份，也不能代替用户完成业务写入。详见
[管理控制台](docs/平台能力/管理控制台.md)。

## 八、内网部署

当前中心服务部署在 Linux `10.10.50.213:/home/guomao/agentbridge`，由
`agentbridge.service` 托管：

| 端口 | 服务 |
| ---: | --- |
| 8780 | 可信认证、字段和授权卡片 |
| 8781 | 按挑战开放的 noVNC 网关 |
| 8782 | 管理控制台 |
| 8783 | Agent Workspace |
| 8790 | Streamable HTTP MCP |

Workspace 通过服务器回环地址和宿主工作站主动建立的反向 SSH 隧道访问 OpenClaw
Gateway，工作站切换网络时无需修改服务器配置。完整说明见
[当前内网部署](docs/部署运维/当前内网部署.md)。

## 九、验证与发布

后续维护以 macOS 开发和本机宿主、Linux 中心服务及 CI 为目标，不再使用 Windows。
常用验证命令（Python 3.12+ 环境）：

```sh
scripts/agentbridge-native validate --test tests/test_plan_write_boundaries.py --test tests/test_documentation.py
scripts/agentbridge-native validate --full
```

正式发布入口为 `scripts/agentbridge-native publish`。须提供外置环境配置、SSH 身份、可信主机及宿主交接证据；
默认仅生成计划，使用 `--apply` 执行。依赖准备、完整参数和独立验收见[苹果系统原生维护](docs/部署运维/苹果系统原生维护.md)。

发布脚本负责全量验证、构建 wheel、部署版本化 Release、安装受控 systemd unit、重启服务、
执行治理验收并推送 GitHub。纯文档变更不需要重启或部署 AgentBridge，但仍须通过文档链接、
目录规范和相关回归测试。

开发边界见 [开发安全策略](docs/部署运维/开发安全策略.md)，完整流程见
[开发验证与发布流程](docs/部署运维/开发验证与发布流程.md)。

## 十、安全不变量

- 最终用户设备不安装浏览器扩展、本地 daemon 或遗留系统连接器；
- 用户会话和目标系统身份不可串用；
- 密码、验证码、Cookie、Token 和完整业务字段不进入模型；
- 智能体不能调用任意 Shell、HTTP、JavaScript、DOM 或底层提交工具；
- 所有写入都有独立授权、幂等控制和结果回读；
- 不存在静默降级到旧桥或更弱治理路径；
- 结果未知时不自动重试；
- 当前逻辑隔离不冒充生产级 OS/容器隔离。

## 命名与平台

核心分发包、Python 模块与命令统一为 `agentbridge`（0.2.0）。旧 `bscli` 仅转发启动，计划在 0.3.0 删除。新安装使用 `~/.agentbridge`；检测到旧数据时须显式 `--home`，不会静默建立空环境。维护目标为 macOS 和 Linux。迁移、宿主重新登记及证书操作见[命名迁移与证书维护](docs/部署运维/命名迁移与证书维护.md)与[原生维护](docs/部署运维/苹果系统原生维护.md)。

## 十一、文档

从 [文档导航](docs/文档导航.md) 开始。核心资料：

- [项目当前状态](docs/项目当前状态.md)
- [面向智能体的遗留系统适配设计](docs/架构设计/面向智能体的遗留系统适配设计.md)
- [智能体交互协议](docs/架构设计/智能体交互协议.md)
- [受控写入模型](docs/架构设计/受控写入模型.md)
- [当前内网部署](docs/部署运维/当前内网部署.md)
- [系统适配导航](docs/系统适配/系统适配导航.md)
- [验收记录导航](docs/验收记录/验收记录导航.md)
- [后续增强事项](docs/后续规划/后续增强事项.md)
