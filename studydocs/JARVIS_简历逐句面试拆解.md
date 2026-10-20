# Jarvis 项目简历逐句面试拆解与补课手册

> 面向：马腾简历中的 **Jarvis：本地 Code Agent Harness** 项目。
>
> 用法：先只看“面试官问”，闭卷口述 30 秒；再看“可直接回答”；最后看“继续追问”。不会的题不要跳过，先读答案，再回到源码按文件路径核对一遍。
>
> 证据口径：本文以简历 PDF、当前 `D:\agent\JarvisAgent` 仓库、`studydocs` 学习档案和 BFCL 复现 worktree 为依据。源码里没有证据的能力会明确标成“当前边界/改进方向”，面试时不要说成已实现。

## 目录

- [0. 先把项目讲明白](#0-先把项目讲明白)
- [1. Harness 原句拆解](#1-简历原句jarvis本地-code-agent-harness)
- [2. 六个核心模块](#2-简历原句面向本地的-code-agent实现了-harness-工程的工具上下文记忆loop安全以及-trace-等核心部分)
- [3. 双进程与 IPC](#3-简历原句整体架构设计-core-守护进程与-tui-双进程架构基于-asyncio-和-json-rpc-over-tcp-实现任务提交事件订阅与流式进度推送支持在任意目录启动项目工作区)
- [4. Tool 与 Loop](#4-简历原句tool-与-loop构建可扩展工具运行时与-react-执行闭环统一编排代码检索文件读写shellskillssub-agent-及-mcp-工具支持多工具调用失败结果回填与最终总结)
- [5. Context 与 Memory](#5-简历原句context-与-memory构建-runsession跨-session-三层上下文与记忆体系持久化对话历史和关键-notes并通过工具结果截断与历史摘要压缩控制上下文长度和-token-成本)
- [6. Safety 与 Trace](#6-简历原句safety-与-trace通过参数校验权限策略异步审批执行超时及失败重试约束高风险操作统一记录模型工具权限和-ipc-事件支持执行回放与故障定位)
- [7. BFCL Eval](#7-简历原句eval基于-bfcl-v4-官方数据集与评分器构建端到端评测链路在-3638-条工具调用用例上取得-7916-准确率和-9992-覆盖率实现基于工具-schema-的无损参数规范化与审计记录)
- [8. 串联追问](#8-面试官会把几句话串起来追问)
- [9. 不会时的回答模板](#9-不会时不要乱编安全回答模板)
- [10. 逐日补课清单](#10-逐日补课清单)
- [11. 最后背这 12 句](#11-最后背这-12-句)

---

## 0. 先把项目讲明白

### 30 秒版本

> Jarvis 是一个本地 Code Agent Harness。我把一次模型调用扩展成了一个可运行、可观察、可恢复的 Agent Runtime：用户通过 CLI/TUI 把目标提交给常驻 Core daemon，Core 通过 ReAct AgentLoop 调用 LLM，模型再通过 ToolRegistry 请求读写文件、检索、Shell、任务管理、Skills、Sub-Agent 和 MCP 工具。每次工具调用都经过参数校验、权限判断、超时和重试，执行过程通过 EventBus、events.jsonl 和 Trace 外化，Session、Notes 和 Context 负责多轮续航，上下文接近预算时自动压缩。最后用 BFCL v4 做端到端工具调用评测，3,638 个已评分样例的准确率是 79.16%，计划样例覆盖率是 99.92%。

### 3 分钟版本顺序

```text
项目目标：把一次模型 API 调用变成可运行的本地 Agent Runtime
  -> 双进程：CLI/TUI 是客户端，Core daemon 是唯一执行体
  -> AgentLoop：plan -> tool call -> observe -> tool result -> 再决策
  -> Tool/Safety：统一注册、Schema 校验、权限、超时、重试、错误回填
  -> Context/Memory：Run、Session、跨 Session 的上下文与持久化记忆
  -> Trace：事件流、运行文件、LLM/IPC 追踪，支持实时展示和定位
  -> Eval：BFCL 官方数据和 Checker，保留参数规范化审计
  -> 边界：当前不是强隔离沙箱，也没有完整的 Multi-Agent join barrier
```

### 10 分钟必须能画出来

```text
CLI / TUI
    |  JSON-RPC 2.0 over NDJSON, TCP loopback
    v
jarvis-core daemon
    |-- SocketServer / command handlers
    |-- AgentRunner
    |     |-- ExecutionContext + system prompt
    |     |-- AgentLoop -> LLM Provider
    |     |-- ToolRegistry -> built-in / Skill / Sub-Agent / MCP
    |     |-- PermissionManager -> allow / deny / ask
    |     |-- EventBus -> TUI / events.jsonl / Trace
    |     |-- SessionStore / notes / context.md
    |     '-- Compactor / tool-result budget
    '-- run directory: events.jsonl, trace, session messages, notes, summaries
```

---

## 1. 简历原句：`Jarvis：本地 Code Agent Harness`

### Q1【必问】你说的 Harness 是什么？和聊天机器人有什么区别？

**可直接回答：**

Harness 是围绕模型的运行时外壳。它不负责“替模型思考”，而是负责把模型的输出接入真实执行链：提供工具 Schema、接收 tool call、做参数校验和权限决策、执行工具、把结果回填给模型、发布过程事件、保存会话和控制上下文预算。聊天机器人通常是“用户问题 -> 模型文本”；Jarvis 是“用户目标 -> 循环规划 -> 工具执行 -> 结果观察 -> 再规划 -> 可审计的最终结果”。

**继续追问：Harness 的核心状态是什么？**

一次 Run 至少要有 `run_id`、目标、步骤号、消息列表、工具调用结果、状态和失败原因；跨 Run 还要有 Session、thread 和 notes。没有这些状态，就只能做一次性 Demo，不能续航、恢复或排查。

### Q2【必问】为什么叫 Code Agent，而不是普通 AI Agent？

**可直接回答：**

它的工具边界围绕代码工作区设计，模型能查看项目结构、搜索文件、读写文件、执行 Shell、维护任务清单，并通过 Skill、Sub-Agent 和 MCP 扩展能力。重点不是回答知识，而是在用户授权的工作区内完成“理解代码、修改代码、运行检查、汇报结果”的闭环。

### Q3【发散】本地 Agent 的价值是什么？为什么不直接用云端 IDE？

**可直接回答：**

本地运行可以直接访问本机工作区和开发工具，数据边界更清楚，适合代码、配置和实验文件；Core daemon 与客户端分离后，任务不会绑定某个 TUI 窗口，还可以接入不同前端。代价是要自己处理进程生命周期、权限、安全、协议和上下文持久化。

### Q4【追问】你个人真正负责了哪一部分？

**建议回答模板：**

> 我主要负责把 Agent Harness 的核心链路串起来：Core/TUI 双进程和 JSON-RPC IPC、AgentLoop 与 ToolRegistry、Run/Session/Notes 上下文、权限审批与失败重试、EventBus/Trace，以及 BFCL 评测链路。具体实现可以从 `src/jarvis/core/runner.py` 进入，再沿着 `loop.py`、`tools/invocation.py`、`context.py` 和 `trace/` 展开。评测数字来自独立的 `bfcl-reproducible-eval/KamaClaude` 复现分支，我会把它和 main 分支的实现边界分开说明。

---

## 2. 简历原句：`面向本地的 Code Agent，实现了 Harness 工程的工具、上下文、记忆、Loop、安全以及 trace 等核心部分`

### Q1【必问】一次用户请求在系统里怎么走？

**可直接回答：**

1. CLI 或 TUI 把用户目标封装为 JSON-RPC 请求发给 Core。
2. `SocketServer` 校验请求并路由到 handler。
3. `AgentRunner` 创建或复用 Run/Session，加载历史、Notes、Global/Project context。
4. `AgentLoop` 把 system prompt、历史和工具 Schema 交给 LLM。
5. 模型返回文本或一个/多个 tool call。
6. `invoke_tool` 依次做工具存在性检查、Pydantic 参数校验、权限审批、超时执行和失败重试。
7. 工具结果以 `tool_result` 加回消息，错误也回填给模型，让模型换方案。
8. EventBus 同时通知 TUI、`events.jsonl` 和 Trace；上下文达到阈值时由 Compactor 压缩。
9. 模型 `end_turn` 后 Run 完成，SessionStore 保存本轮新增消息。

### Q2【必问】工具、上下文、记忆、Loop、安全、Trace 之间是什么关系？

**可直接回答：**

Loop 是控制流；Tool 是动作；Context 是本次请求送给模型的输入；Memory 是跨请求保存并可被注入的信息；Safety 是动作执行前后的约束；Trace 是把控制流和关键数据留下来，便于实时观察、回放和定位。它们不是六个孤立模块：工具结果会进入 Context，Context 过长会触发 compact，权限和工具失败会产生事件，Trace 再把这些事件串起来。

### Q3【追问】为什么不把所有东西都写进一个大 Prompt？

**可直接回答：**

大 Prompt 不能替代状态管理。历史消息、工具结果、项目规则、用户偏好和运行状态的生命周期不同：历史要可回放，Notes 要可编辑，项目 context 要由用户维护，工具结果要受预算限制。分层后可以分别持久化、压缩、过滤和审计，避免每轮无条件把所有内容塞给模型。

### Q4【发散】一个 Agent Runtime 最容易被低估的工程问题是什么？

**可直接回答：**

不是调用模型本身，而是长任务状态、工具失败、权限等待、断连、上下文爆炸和结果可解释性。能跑通一条 happy path 只是 Demo；要成为 Runtime，还要能在工具失败后继续、在权限请求时不阻塞 IPC、在长会话中控制 Token、在失败后通过 run_id 和 tool_use_id 定位证据。

---

## 3. 简历原句：`整体架构：设计 Core 守护进程与 TUI 双进程架构，基于 asyncio 和 JSON-RPC over TCP 实现任务提交、事件订阅与流式进度推送，支持在任意目录启动项目工作区`

### Q1【必问】为什么要 Core daemon + TUI 双进程，而不是在 TUI 里直接跑 Agent？

**可直接回答：**

Core 是唯一执行体，TUI/CLI 只是客户端。这样做有四个好处：第一，TUI 退出不会天然杀掉长任务；第二，CLI、TUI 以及未来 Web 前端可以复用同一套协议；第三，事件、权限审批和 Trace 有统一出口；第四，执行边界清楚，便于测试和恢复。代价是要增加 IPC、连接管理和协议演进成本。

### Q2【必问】JSON-RPC 2.0 和 NDJSON 分别解决什么？

**可直接回答：**

JSON-RPC 2.0 规定请求有 `id/method/params`，响应有 `result` 或 `error`，可以把不同命令统一建模并关联请求响应。NDJSON 规定一行一个 JSON，TCP 流可以按换行持续解析，适合流式发送多条响应和事件。项目里命令走 JSON-RPC，服务器推送事件使用事件 envelope；客户端通过 `id` 把响应交给 pending Future，通过事件类型交给订阅者。

### Q3【追问】同一条 TCP 连接上，RPC 响应和事件不会串吗？

**可直接回答：**

不会靠发送顺序猜。`SocketClient._dispatch` 先解析 JSON：有 `jsonrpc` 字段的消息按 `id` 路由到 pending Future；事件消息交给 event handlers。每条消息独占一行，Pydantic 模型负责结构校验。服务端读循环对每一行创建独立 task，因此长时间运行的任务不会阻塞后续的审批响应。

### Q4【必问】asyncio 在这里具体解决了什么？

**可直接回答：**

它把网络读写、模型请求、工具执行、权限等待和事件写入组织成协程。`SocketServer` 为每条命令创建 task；权限请求用 Future 挂起，客户端稍后发送 `permission.respond` 时再唤醒；工具执行用 `asyncio.wait_for` 限时；后台 Sub-Agent 用 `asyncio.create_task` 运行。它提高的是 I/O 并发，不等于 CPU 计算自动并行。

### Q5【追问】为什么选 TCP loopback，不选 Unix Domain Socket？

**可直接回答：**

当前项目优先跨平台和实现简单，使用 `127.0.0.1` 只监听本机，CLI/TUI 都能用同一套 asyncio Stream API。Unix Domain Socket 在 Linux 权限控制和本机通信上更自然，但 Windows 兼容性、路径管理和客户端发现会额外复杂。若部署环境固定为 Linux，可以把传输抽象成接口后增加 UDS。

### Q6【必问】“支持在任意目录启动项目工作区”怎么实现？

**可直接回答：**

客户端启动时把当前目录规范化为 workspace，Core 根据 workspace 读取项目级 `.jarvis/context.md`、skills 和配置，并把工作目录传给工具。`AgentRunner` 以该 workspace 创建 `ProjectMapTool`、`ReadFileTool`、`WriteFileTool`、`BashTool` 等工具。核心点是 workspace 是运行时依赖，不把代码仓库写死在安装目录。

### Q7【追问】端口被占用、客户端断连、服务停止分别怎么处理？

**可直接回答：**

Core 启动前先探测 host/port，若已有 daemon 就退出，避免重复实例；SocketServer 停止时关闭活跃 writer 并等待 server 关闭；SocketClient 断连时取消所有 pending Future，避免调用方永久等待。当前 Run 的事件和 Session 消息会落盘，但“自动重连并恢复正在执行的请求”还不是完整能力，应该作为后续改进说。

### Q8【发散】事件流有慢消费者时会发生什么？

**可直接回答：**

当前 EventBus 按订阅者顺序 `await` handler，慢的 TUI、文件写入或 Trace handler 可能拖慢发布者。这种设计简单、保证事件顺序，但没有每订阅者独立队列和背压。改进方向是每个订阅者一个有界 asyncio.Queue，发布者只负责入队，消费者独立处理；队列满时按事件重要性丢弃 token 级事件，保留状态和错误事件。

### Q9【发散】如何防止重复提交导致重复执行？

**可直接回答：**

JSON-RPC `id` 只关联一次响应，不能自动提供业务幂等。需要在命令层引入 client request id 或 idempotency key，把“已接受/运行中/已完成”写入 Run 状态，再决定重复请求返回旧结果还是拒绝。当前代码主要依靠 `run_id`、SessionStore 和事件文件留痕，不能把它说成完整的幂等提交系统。

### Q10【补课】协议为什么要用 Pydantic discriminated union？

**答案：**

协议边界是跨进程的，`dict` 直接传递容易出现字段缺失、类型错误和分支判断散落。Pydantic 模型可以在入口处校验并序列化；以 `type` 为 discriminator 后，解析器能把消息路由成明确的命令/事件模型。新增命令需要修改 `bus/commands.py` 或 `events.py`，再运行 `scripts/gen_protocol_doc.py` 更新 `WIRE_PROTOCOL.md`。

**源码核对：** `src/jarvis/core/bus/envelope.py`、`commands.py`、`events.py`、`transport/socket_server.py`、`transport/socket_client.py`。

---

## 4. 简历原句：`Tool 与 Loop：构建可扩展工具运行时与 ReAct 执行闭环，统一编排代码检索、文件读写、Shell、Skills、Sub-Agent 及 MCP 工具，支持多工具调用、失败结果回填与最终总结`

### Q1【必问】Jarvis 的 ReAct Loop 怎么跑？

**可直接回答：**

每轮先把当前 Context 和工具 Schema 发给模型，模型进行 plan 并返回文本或 tool call；如果有 tool call，系统进入 act 阶段，执行工具并把结果作为 `tool_result` 回填；下一轮模型根据 observe 到的结果继续决策。模型返回 `end_turn` 时结束；如果达到最大工具步数，Jarvis 会关闭工具 Schema 再发一个 finalization turn，让模型在不能继续执行的情况下总结已经拿到的证据。

### Q2【追问】代码里 Loop 如何判断什么时候结束？

**可直接回答：**

`ExecutionContext.status` 从 `running` 变为 `success` 或 `failed`。模型 `end_turn` 直接成功结束；LLM 调用异常标为 `llm_error`；取消标为 `cancelled`；达到 `max_steps` 时设置 `finalization_turn`，下一轮不提供工具，只生成最终总结。工具失败本身不会直接结束 Run，而是作为带 `is_error` 的结果回填。

### Q3【必问】为什么工具失败后要回填给模型，而不是直接返回错误？

**可直接回答：**

失败也是环境观测的一部分。比如文件路径不存在，模型可能改用搜索；命令超时，模型可能缩小范围；权限拒绝，模型可以请求用户授权或走只读方案。把错误分类、错误信息和 `tool_use_id` 回填，模型就有机会修正计划；只有不可恢复的 LLM 错误、取消或最终步数用尽才结束 Run。

### Q4【必问】工具调用完整链路是什么？

**可直接回答：**

`ToolRegistry` 先按名称查找；未知工具返回 `runtime_error`。有 `params_model` 的工具用 Pydantic 校验参数，失败返回 `schema_error`。有 PermissionManager 时进行策略判断，必要时发 `permission.requested` 并等待用户；通过后用 `asyncio.wait_for` 执行；`RateLimitedError` 和 `runtime_error` 最多重试两次，采用指数退避；超时、权限拒绝和最终失败都会发 `ToolCallFailedEvent` 并转成 `ToolResult(is_error=True)`。

### Q5【追问】为什么参数 Schema 和执行逻辑要分开？

**可直接回答：**

Schema 解决“输入形状是否合法”，执行逻辑解决“这个动作是否应该执行以及怎么执行”。分开后模型能拿到统一的工具描述，调用层能集中做校验、权限、超时、重试和事件记录，具体工具只关心业务动作。否则每个工具都会重复实现安全和观测逻辑，错误分类也不一致。

### Q6【必问】如何支持多工具调用？是串行还是并行？

**可直接回答：**

模型返回多个 tool call 后，当前 `AgentLoop` 按列表逐个调用，并把同一步的多个结果合并到一条 user message，保证对话格式合法。这样顺序和权限行为更容易控制，但独立的只读工具没有并行收益。后续可以对无依赖、无写操作的工具做并发执行，同时为写操作和同一资源加依赖或锁。

### Q7【追问】失败重试为什么不是所有错误都重试？

**可直接回答：**

Schema 错误、权限拒绝和确定性的路径错误重试没有意义，可能重复危险操作；网络限流和暂时性运行错误才有恢复机会。当前可重试集合是 `runtime_error`、`rate_limited`，最多两次，退避时间是 `2 * 2^(attempt-1)` 秒；超时直接失败并回填。生产系统还应进一步区分幂等工具和非幂等写操作。

### Q8【必问】最终总结轮为什么需要额外一步？

**可直接回答：**

如果最后一次模型输出撞到工具步数上限，模型可能还没有生成面向用户的总结。继续提供工具会突破预算，所以把 `finalization_turn` 设为 true，下一次请求不带工具 Schema，只让模型总结已有证据，并在没有文本时返回明确的“达到工具步数限制”说明。这样既控制成本，又不丢最终可读结果。

### Q9【追问】Tool result 为什么要截断？会不会丢信息？

**可直接回答：**

代码工具可能返回很长的日志、文件或搜索结果，直接放进上下文会挤占后续推理 Token。当前 budget helper 对超长 `tool_result` 保留前 4,000 字符，并提示省略部分仍在 run events 中；触发 compact 后再用模型摘要压缩历史。它确实可能丢掉中间细节，所以错误定位要依赖原始 `events.jsonl`，不能只看压缩后的消息。

### Q10【必问】Skill、Sub-Agent、MCP 有什么区别？

**可直接回答：**

Skill 是 Prompt 模板加工具白名单，不启动独立 Loop；Sub-Agent 是隔离上下文的另一个 AgentLoop，可以前台阻塞或后台 asyncio task；MCP 是外部工具协议，`McpTool` 把外部 server 的工具适配成 `BaseTool`，本身不产生另一套 Loop。三者最终都接入 ToolRegistry/AgentRunner 的统一调用链。

### Q11【追问】Sub-Agent 的上下文为什么要隔离？

**可直接回答：**

父 Agent 的长历史可能让子任务受到无关信息干扰，也会增加 Token 成本。Jarvis 给子 Agent 一个干净的 `ExecutionContext`，只把 prompt 和可选的 role profile 传进去；子 Agent 的事件桥接到父 Bus，结果通过前台返回值或后台 `agent_result` 查询。当前有最大嵌套深度 2，但还没有完整的 join barrier、并发上限和取消状态机。

### Q12【补课】MCP 工具命名为什么带 `server_name__` 前缀？

**答案：**

不同 MCP Server 可能暴露同名工具。适配器把名称变为 `server_name__tool_name`，避免注册表覆盖，同时保留原始名称用于调用外部 server。工具 Schema 直接来自 MCP tool definition；调用失败统一映射为 `ToolResult(is_error=True)`，让主 Loop 仍按同一种错误回填机制处理。

### Q13【发散】如何防止模型通过 Shell 绕过文件工具？

**可直接回答：**

需要把 Bash 当高风险工具处理：默认 ASK，检查绝对路径、`~`、`..`、`$HOME`、`cd` 等工作区外迹象，审批后再执行；同时给工具配置白名单和超时。当前实现是启发式策略，不是 OS 级沙箱，不能阻止所有 Shell 逃逸、Junction、子进程或网络访问。真正的隔离需要容器、Windows Job Object/AppContainer 或受限权限令牌等执行边界。

### Q14【发散】如何新增一个工具？

**可直接回答：**

继承 `BaseTool`，定义 `name`、`description`、`input_schema`，需要结构校验时定义 Pydantic `params_model`，在 `invoke` 中返回 `ToolResult`。再在 `AgentRunner._build_registry` 注册，并补充参数校验、成功、超时、失败、权限和事件测试。工具本身不应该直接打印 UI 或修改 AgentLoop。

### Q15【边界】当前代码支持动态模型路由吗？

**可直接回答：**

配置里保留了 router 方向，但当前 Provider 是 static routing，所有任务默认使用同一模型。不要把“支持模型路由”写成已完成能力。改进时可以依据任务复杂度、工具数量、失败次数、风险等级和剩余预算选择小/大模型，并用离线 Eval 验证成本、延迟和成功率。

**源码核对：** `src/jarvis/core/loop.py`、`runner.py`、`tools/base.py`、`tools/registry.py`、`tools/invocation.py`、`subagent/tool.py`、`mcp/tool.py`、`skills/loader.py`。

---

## 5. 简历原句：`Context 与 Memory：构建 Run、Session、跨 Session 三层上下文与记忆体系，持久化对话历史和关键 Notes，并通过工具结果截断与历史摘要压缩控制上下文长度和 Token 成本`

### Q1【必问】Run、Session、跨 Session 三层分别是什么？

**可直接回答：**

Run 是一次目标执行的短生命周期状态，保存当前消息、step、结果和事件；Session 把多个 Run 串成一次持续对话，保存 thread 消息和 notes；跨 Session 层是 Global/Project context，例如 `~/.jarvis/context.md` 和项目 `.jarvis/context.md`，由用户维护，给不同 Session 提供稳定背景。记忆决定信息存在哪里、保留多久；Context 决定当前请求把哪些信息注入模型。

### Q2【必问】thread、notes、context.md 有什么区别？

**可直接回答：**

`thread` 是完整消息历史，适合恢复对话但可能很长；`notes` 是 Agent 主动保存的关键事实，适合跨 Run 复用；`context.md` 是用户或项目维护的固定规则和背景，不应由模型悄悄改写。三者都可能进入 prompt，但持久化、更新和审计职责不同。

### Q3【追问】历史消息什么时候持久化？

**可直接回答：**

`AgentRunner` 开始 Run 时从 SessionStore 读取已有 messages 和 notes；Run 完成后把本次相对于 `prefill_len` 的新增消息追加回 SessionStore。Run 期间的完整事件先写 `events.jsonl`，所以即使最终失败，也有过程证据。

### Q4【必问】上下文压缩什么时候触发？

**可直接回答：**

每轮工具调用后，如果 Provider 返回的 usage 中 `context_pct` 达到配置阈值，且 Run 还在继续，就调用 `Compactor`。Compactor 把当前 messages 转成文本，发一次不带工具的摘要请求，用摘要替换内存中的消息，并写 `summary_<timestamp>.md`，同时发布 `ContextCompactedEvent`。默认阈值由配置控制，常见设置是 0.80。

### Q5【追问】为什么不直接删除旧消息？

**可直接回答：**

直接删除会丢失任务目标、关键决策和工具证据，也无法解释模型为什么做出后续动作。摘要保留压缩后的语义，原始消息和事件文件继续留在持久化记录里，兼顾模型输入预算和审计回放。

### Q6【发散】模型摘要可能胡编，怎么降低风险？

**可直接回答：**

摘要 Prompt 要求只保留事实、目标、已完成动作、未完成事项和约束；保留原始 thread/events 作为证据；记录压缩前后 token 估算；重要结构化状态不能只依赖摘要，比如任务清单、权限决策、失败类型要单独持久化。对于高风险任务可以在摘要后保留最近若干轮原文。

### Q7【补课】工具结果截断和历史 compact 的区别？

**答案：**

工具结果截断是局部、廉价的输入治理：只改送给模型的内容，原始输出仍在 run events；compact 是全局历史治理：把多轮消息交给模型归纳成新的上下文快照。前者主要解决单个大输出，后者主要解决长会话累积。

### Q8【必问】上下文 Token 成本怎样控制？

**可直接回答：**

第一，限制工具结果长度；第二，使用 Provider usage 和字符数/4 的粗略估算监测水位；第三，达到阈值时 compact；第四，通过 tool whitelist 和角色 Prompt 减少无关工具 Schema；第五，保留原始事件但不把原文全部注入下一轮。粗略估算不是精确 tokenizer，因此最终以模型返回 usage 为准。

### Q9【边界】跨 Session 是否会自动学习用户偏好？

**可直接回答：**

当前没有自动长期偏好学习。Global/Project context 由用户维护，notes 由 Agent 通过 `note_save` 明确写入，Session thread 由 Store 持久化。自动提取偏好、冲突处理、过期和删除应作为后续功能，并且需要用户审批。

### Q10【发散】如果用户修改了项目规则，旧 Notes 怎么办？

**可直接回答：**

需要给 Notes 增加来源、作用域、创建/更新时间和版本；注入时按项目规则优先级覆盖旧事实；冲突时不要静默选择，应标记冲突并让用户确认；支持删除和失效。当前实现能保存 Notes，但没有完整的版本冲突治理。

**源码核对：** `src/jarvis/core/context.py`、`runner.py`、`session/store.py`、`memory/loader.py`、`compact/compactor.py`、`compact/budget.py`、`tools/builtin/note_save.py`。

---

## 6. 简历原句：`Safety 与 Trace：通过参数校验、权限策略、异步审批、执行超时及失败重试约束高风险操作；统一记录模型、工具、权限和 IPC 事件，支持执行回放与故障定位`

### Q1【必问】一个高风险工具调用如何被拦住？

**可直接回答：**

模型先提出 tool call，不直接获得执行权。调用层先校验工具和参数；PermissionManager 对工具和参数做策略评估。`bash` 默认 ASK，命中绝对路径、`~`、`..`、`$HOME`、`cd` 等工作区外启发式时强制 ASK；需要用户决定时发布 `permission.requested`，把 Future 挂起。用户返回 allow/deny/always 后，调用层再决定是否执行，并发布 granted/denied 事件。

### Q2【追问】为什么审批必须是异步的？

**可直接回答：**

审批等待可能持续几十秒，不能阻塞 SocketServer 继续读消息，否则同一连接上的 `permission.respond` 到不了处理器。当前每条命令独立 task，审批用 Future 等待；客户端继续通过 IPC 发响应，服务端唤醒对应 tool call。

### Q3【必问】Ask、Always 和 Auto 有什么区别？

**可直接回答：**

`allow_once` 只放行本次；`always_allow/always_deny` 写入 session 级和可选持久化策略；`ask` 每次需要用户确认；`auto` 是 session 模式，会显式放行工具。要特别诚实说明：当前 Auto 模式优先级很高，会绕过静态检查，因此它是便利模式，不应被描述为强安全模式。

### Q4【必问】超时和重试怎么设计？

**可直接回答：**

工具执行默认 120 秒，用 `asyncio.wait_for` 限时；超时返回 `timeout`，不盲目重试。运行时错误和限流错误最多重试两次，使用指数退避；权限拒绝和 Schema 错误直接回填。每次失败都带 `error_class`、`attempt`、耗时和 `tool_use_id`，便于模型和人排查。

### Q5【边界】当前权限系统能叫“沙箱”吗？

**可直接回答：**

不能。它是策略、审批、超时、重试和审计治理，不是 OS 级隔离。启发式路径检查无法完全阻止命令替换、Junction、子进程、网络访问或资源消耗；Auto 模式还会降低约束。要成为真正沙箱，需要规范化路径、受限文件系统、进程权限、网络限制和 CPU/内存/进程数配额。

### Q6【必问】events.jsonl、Trace、thread.jsonl、普通日志有什么区别？

**可直接回答：**

普通日志记录 daemon 生命周期和异常堆栈；`events.jsonl` 是单个 Run 的领域事件，适合回放 Agent 过程；Trace 把 IPC、EventBus、LLM 请求响应放到一条带时间和关联信息的系统时间线，适合跨层定位延迟；`thread.jsonl` 是可再次发给模型的 Session 消息历史。它们互相补充，不能用一个文件替代全部用途。

### Q7【追问】如何定位一次工具失败？

**可直接回答：**

先从 TUI 的 `run_id` 找对应 Run 目录；在 `events.jsonl` 按 `tool_use_id` 查 `tool.call.started/failed`，看 `error_class` 和 `attempt`；再看权限 requested/granted/denied 事件；如果怀疑模型或 IPC，按同一时间和 run/step 去 Trace 看 LLM、IPC 前后记录；最后检查 `core.log` 的异常堆栈。这样可以区分模型没发对、参数不合法、没过权限、工具超时和工具自身异常。

### Q8【追问】Trace 记录模型 Payload 会有什么风险？

**可直接回答：**

完整 Payload 可能包含源码、密钥、用户输入和工具结果。项目用配置项控制 `include_llm_payload`，生产环境应默认关闭或脱敏，只保留 model、step、token usage、耗时、哈希和引用路径；如果必须打开，要有访问权限和保留周期。

### Q9【发散】事件和 Trace 怎样保证关联？

**可直接回答：**

统一使用 `run_id`、`session_id`、`step`、`tool_use_id`、client id 和时间戳。工具调用事件至少要包含 run/tool/use/attempt/error；IPC Trace 记录请求 id 和 method；LLM Trace 记录 run/step。排查时先按 run_id 缩小范围，再用 tool_use_id 或 request id 串起局部链路。

### Q10【发散】事件写盘失败会不会影响 Agent？

**可直接回答：**

这是可靠性和可用性的取舍。当前 EventWriter 是 EventBus 订阅者，若按顺序 await，写盘异常或慢写入可能反向影响发布；生产实现应将事件落盘放进有界队列，重要状态事件采用同步确认，token 事件允许降级，并在磁盘故障时显式标记 observability degraded。

### Q11【补课】为什么权限决策要落事件而不只写日志？

**答案：**

权限是业务状态，决定工具是否执行，应该和 Run 的其他事件一起回放。事件可以被 TUI、审计器和测试消费；普通日志更偏运维文本，结构不稳定。`PermissionRequestedEvent`、`PermissionGrantedEvent` 和 `PermissionDeniedEvent` 还能帮助验证“审批后才执行”的不变式。

**源码核对：** `src/jarvis/core/permissions/policy.py`、`manager.py`、`tools/invocation.py`、`events/`、`trace/`、`transport/ipc_broadcaster.py`。

---

## 7. 简历原句：`Eval：基于 BFCL v4 官方数据集与评分器构建端到端评测链路，在 3,638 条工具调用用例上取得 79.16% 准确率和 99.92% 覆盖率；实现基于工具 Schema 的无损参数规范化与审计记录`

### Q1【必问】BFCL 是什么？你评测的到底是什么？

**可直接回答：**

BFCL 是 Berkeley Function Calling Leaderboard，提供不同类别的工具调用数据和官方 Checker。这里评测的是“模型 + Provider + AgentLoop + 工具参数处理”的端到端能力，不是单独测模型。每条样例包含用户目标、工具定义、模型工具调用和 Checker 结果，Run 还保留事件、Trace、usage 和参数审计。

### Q2【必问】3,638、79.16%、99.92% 分别从哪里来？

**可直接回答：**

复现报告的计划样例是 3,641，实际完成并进入正式评分的是 3,638，正确样例 2,880。因此 Accuracy = `2,880 / 3,638 = 79.1644%`，Coverage = `3,638 / 3,641 = 99.9176%`。简历里的“3,638 条工具调用用例”指 scored cases，不是计划总数。缺少 API key、网络失败或未完成样例不能直接算成错误分数。

### Q3【必问】Accuracy、Coverage、Attempt Accuracy 有什么区别？

**可直接回答：**

Accuracy 是 `correct / scored`，只在有正式 Checker 结果的样例上计算，是对外报告的准确率；Coverage 是 `scored / planned`，表示计划任务有多少完成了正式评分；Attempt Accuracy 是 `correct / planned`，把未评分样例视为这次整体尝试没有完成，适合观察端到端完成度，但不应冒充官方准确率。三者分开能避免把运行失败混进模型错误。

### Q4【追问】为什么覆盖率接近 100% 也不代表模型很准？

**可直接回答：**

覆盖率只说明样例跑完并拿到了可评分结果，不说明调用正确。一个系统可以每题都输出调用，Coverage 很高，但参数或工具名大量错误，Accuracy 仍然低。反过来只跑容易题可能 Accuracy 高，却没有代表性，所以报告要同时看 planned/scored/correct 和分类汇总。

### Q5【必问】什么叫“基于工具 Schema 的无损参数规范化”？

**可直接回答：**

模型有时会返回字符串数字、对象层级不规范、别名字段或多余包装。规范化器根据工具的 JSON Schema 做安全变换，例如把可明确解析的数字字符串转成数字、展开确定的参数包装，但不凭常识猜语义、不删除无法解释的字段、不覆盖原始参数。每次变换同时保存 `original_arguments`、`normalized_arguments`、规则名和 validation errors，Checker 用规范化后的参数，审计仍能看到原始输出。

### Q6【追问】“无损”为什么重要？

**可直接回答：**

如果只保存变换后的参数，评测结果出现争议时无法知道错误来自模型还是规范化器；如果规范化器偷偷补参数，又会把工程规则伪装成模型能力。保留原始值、规则和校验结果，才能复盘、重跑和审计。

### Q7【发散】如何保证评测可复现？

**可直接回答：**

固定数据版本、类别过滤、Provider、模型 profile、AgentLoop max steps、规范化器版本和 run id；记录环境、命令、planned/scored/correct、每题的 Checker、事件和 Trace 路径；支持 checkpoint 断点续跑；报告中区分运行失败和评分失败。当前复现资料位于 `C:\Users\23148\.codex\worktrees\bfcl-reproducible-eval\KamaClaude\repro\bfcl`。

### Q8【追问】这些数字能证明 Jarvis 比别的 Agent 好吗？

**可直接回答：**

不能直接下这个结论。它是固定数据、Provider、模型 profile 和工具规范化配置下的端到端结果，不能和不同数据、不同模型、不同 Checker 的数字横比。能证明的是：这条固定配置的评测链路可运行、结果可审计、覆盖率高，并给后续改动提供基线。

### Q9【发散】下一步怎样做消融实验？

**可直接回答：**

固定数据和模型，只切换一个变量：关闭参数规范化、关闭重试、不同 max steps、不同 Prompt、不同模型路由。比较 Accuracy、Coverage、Attempt Accuracy、延迟、Token、工具错误类别和每类数据的表现。这样才能知道收益来自 Loop、规范化、模型还是容错机制。

### Q10【边界】BFCL 评测代码在当前 main 里吗？

**可直接回答：**

复现实现和报告在独立 `bfcl-reproducible-eval/KamaClaude` worktree，当前主仓库的 `studydocs/CODE_READING_INTERVIEW_GUIDE.md` 也明确记录了这一点。面试时我会把“Jarvis 主运行时能力”和“独立 BFCL 复现分支的评测证据”分开讲，并说明存在代码漂移风险，后续应合并公共 Eval 基础设施。

**源码/证据核对：** `repro/bfcl/README.md`、`BFCL_OFFICIAL.md`、`src/kama_claude/evals/bfcl_reporting.py`、`bfcl_runner.py`、`core/tools/argument_normalizer.py`、报告 JSON。

---

## 8. 面试官会把几句话串起来追问

### Q1【系统设计】如果任务运行 30 分钟，TUI 关掉了怎么办？

**回答：** Core daemon 是执行主体，客户端断开不应自动取消 Run；events.jsonl、Trace 和 Session 消息落盘，重新连接后可按 run_id 订阅或读取历史。当前代码已具备落盘和客户端 pending Future 取消，但完整的“断线重连 + 未读事件补发 + 运行状态查询”需要增加事件游标和 Run 状态存储。

### Q2【系统设计】两个工具能不能并行？

**回答：** 独立只读工具可以并行，但当前主 Loop 是顺序调用，优先保证事件顺序、审批和错误回填简单。要并行需要给每个调用独立 task，收集结果后按原 tool call 顺序组装消息，并处理写操作依赖、权限交互、取消和限流。

### Q3【系统设计】怎么控制一个恶意或失控 Agent？

**回答：** 分层控制：max steps、单工具 timeout、重试上限、上下文预算、权限审批、工具白名单、工作区检查和事件审计。若面对不可信代码或强攻击模型，还必须升级到 OS/容器沙箱、网络隔离、资源配额和人工确认，不能只靠 Prompt 或正则。

### Q4【工程质量】你怎么测试？

**回答：** 协议、工具注册、参数校验、Loop、Compactor、权限、Session、Trace 和 TUI 都有单元测试；双进程 IPC、Session IPC 和端到端 Run 有集成测试。常用检查是 `uv run pytest tests/ -v`、`uv run ruff check src tests scripts`、`uv run mypy src`，协议模型改动后再运行 `gen_protocol_doc.py --check`。

### Q5【故障题】模型返回了一个不存在的工具怎么办？

**回答：** Registry 查不到，调用层生成 `runtime_error` 的失败事件和 ToolResult，回填给模型，让它换工具或解释无法完成。它不会让服务进程崩溃，也不会直接执行未知动作。

### Q6【故障题】用户一直不点审批怎么办？

**回答：** Future 有可配置超时，超时后返回 `timeout`、发布失败事件，模型可以换只读路径或结束。客户端断连时 PermissionManager 会取消该 session 的 pending request，避免永久挂起。

### Q7【取舍题】为什么不让工具自己处理权限？

**回答：** 权限是跨工具的横切关注点。统一放在调用层，才能保证 MCP、内置工具和 Sub-Agent 工具都经过同一套参数校验、审批、超时和事件记录；工具保持可测试、可复用。

### Q8【取舍题】为什么不用消息队列做 IPC？

**回答：** 当前目标是单机本地 Agent，TCP loopback + NDJSON 足够轻量，部署不需要额外 Broker，流式事件也直接。若要跨机器、多实例、离线重放和高吞吐广播，再考虑 NATS、Redis Streams 或专用消息总线；目前引入 MQ 会增加运维和一致性成本。

### Q9【开放题】你觉得项目最大缺点是什么？

**建议回答：**

第一，权限策略是启发式治理，不是强隔离沙箱；第二，EventBus 订阅者是顺序 await，慢消费者会拖慢主流程；第三，Sub-Agent 没有完整 join barrier、并发上限和取消状态机；第四，模型路由仍是 static；第五，长期记忆依赖用户维护，缺少自动偏好治理。这些边界我会在简历项目介绍里主动说明，并给出改进顺序。

### Q10【开放题】下一步你会先改什么？

**建议回答：**

优先做 Multi-Agent join barrier：给子任务增加 required/optional 状态、完成事件、超时取消和并发上限，避免主 Agent 在关键子任务未完成时提前总结。然后做分级模型路由和上下文预算，最后把策略安全升级为文件系统/进程级沙箱，并将 BFCL、成本、延迟和安全攻击用例纳入持续评测。

---

## 9. “不会时”不要乱编：安全回答模板

### 模板 A：知道原理，但没做过

> 这个问题的原理是……在 Jarvis 当前实现里，我做到的是……；还没有实现的是……。如果继续做，我会从……文件/接口开始，先补一个最小测试，再评估……指标。

### 模板 B：数字记不清

> 我不想现场猜数字。评测报告的口径是 planned、scored、correct 三个分母/分子，Accuracy 是 correct/scored，Coverage 是 scored/planned；具体值以报告 JSON 为准。简历中的结果是 3,638 scored、2,880 correct、79.16% accuracy、99.92% coverage。

### 模板 C：被问到当前没实现的能力

> 目前代码支持的是 A，不是完整的 B。B 需要增加……状态、……隔离或……测试。我会把它作为改进方向，不把它说成已经上线。

### 模板 D：被问到个人贡献

> 我可以沿着一次真实调用说明我负责的代码路径：从……进入，经过……，在……处做关键设计，测试在……。如果是协作完成的部分，我会说明接口边界和自己具体实现的模块。

---

## 10. 逐日补课清单

### 第 1 轮：闭环能说清

- [ ] 不看资料画出 CLI/TUI -> Core -> Runner -> Loop -> Tool -> Event/Session 的时序。
- [ ] 解释 JSON-RPC、NDJSON、pending Future 和事件分发。
- [ ] 用自己的话说明“工具失败是模型下一轮输入”。
- [ ] 说清 Run、Session、Notes、Global/Project context 的生命周期。

### 第 2 轮：高频追问能扛住

- [ ] 手写一次 `invoke_tool` 的错误分类：schema / permission / timeout / runtime / rate_limited。
- [ ] 解释 max steps、finalization turn、tool-result truncate、compact 的先后关系。
- [ ] 画 Permission ASK 的两条协程：工具等待 Future，客户端发送 respond。
- [ ] 说出 Event、Trace、thread、普通日志的用途差异。

### 第 3 轮：边界不说错

- [ ] 能主动说出“不是沙箱”。
- [ ] 能主动说出“Sub-Agent 没有完整 join barrier”。
- [ ] 能说明 BFCL 评测在独立 worktree，数字的分母是 scored/planned。
- [ ] 能提出事件背压、模型路由、长期记忆治理和 OS 级沙箱四个改进方向。

### 源码阅读入口

```text
架构/启动：
  src/jarvis/core/app.py
  src/jarvis/core/runner.py
  src/jarvis/core/transport/socket_server.py
  src/jarvis/core/transport/socket_client.py

Loop/工具：
  src/jarvis/core/loop.py
  src/jarvis/core/context.py
  src/jarvis/core/tools/invocation.py
  src/jarvis/core/tools/registry.py
  src/jarvis/core/subagent/tool.py
  src/jarvis/core/mcp/tool.py

记忆/压缩：
  src/jarvis/core/session/store.py
  src/jarvis/core/memory/loader.py
  src/jarvis/core/compact/compactor.py
  src/jarvis/core/compact/budget.py

安全/观测：
  src/jarvis/core/permissions/policy.py
  src/jarvis/core/permissions/manager.py
  src/jarvis/core/events/
  src/jarvis/core/trace/

证据/测试：
  tests/unit/test_loop.py
  tests/unit/test_tool_retry.py
  tests/unit/test_permission_manager.py
  tests/unit/test_compactor.py
  tests/unit/test_session_store.py
  tests/integration/test_s2_dual_process.py
  tests/integration/test_s4_session_ipc.py
  studydocs/CODE_READING_INTERVIEW_GUIDE.md
```

---

## 11. 最后背这 12 句

1. **Jarvis 不是一次调用模型，而是一个有协议、有状态、有事件、有权限和上下文治理的本地 Agent Runtime。**
2. **Core 是唯一执行体，CLI/TUI 是客户端，双进程让执行生命周期和界面生命周期解耦。**
3. **JSON-RPC 负责请求响应，NDJSON 负责在 TCP 流上逐行传输。**
4. **ReAct Loop 的关键是 tool call、执行、结果回填和再次决策。**
5. **工具失败是环境观测，不一定是 Run 失败。**
6. **参数校验解决输入形状，权限策略解决能不能做，工具执行解决怎么做。**
7. **Run 是一次执行，Session 串多次 Run，跨 Session context 提供稳定背景。**
8. **工具结果截断控制局部大输出，compact 控制长期历史增长，原始事件仍保留。**
9. **权限审批是异步 Future，避免阻塞 IPC 读取和审批响应。**
10. **events.jsonl 讲 Run 过程，Trace 讲跨层时间线，thread 讲可恢复消息历史。**
11. **BFCL 的 79.16% 是 2,880/3,638 的 scored accuracy，99.92% 是 3,638/3,641 的 coverage。**
12. **当前权限治理不是强沙箱，Sub-Agent 也没有完整 join barrier；边界说清楚比夸大能力更重要。**

> 面试前最后一次自测：随机挑一条简历原句，先不看本文讲 60 秒；面试官再问三个“为什么”，仍能指出源码、测试、指标和边界，就算真的掌握了。
