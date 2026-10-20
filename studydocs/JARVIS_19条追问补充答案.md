# JARVIS 20 条面试追问补充答案

> 这份文档是对现有 24 张卡的补充，不修改原手册。每条都标明所属卡片，并区分当前代码事实、设计方案和项目边界。

---

## 1. 记忆的读写时机：这些文件什么时候产生、什么时候读？

**所属卡片：卡 01：Memory 设计**

项目里的记忆是在不同生命周期节点产生和读取的。

| 产物 | 产生/写入时机 | 读取时机 |
|---|---|---|
| meta.json | SessionManager.create() 创建 Session 时写入；发送消息、关闭 Session 时更新 | 当前 Core 进程管理 Session 时使用；重启后目前不会自动扫描恢复到内存索引 |
| thread.jsonl | send_message 后先追加 user 消息；Run 结束后追加本轮模型消息和工具结果 | Runner 启动 Run 时通过 SessionStore.read_messages() 读取 |
| notes.md | note_save 工具成功调用时立即追加一条带时间和 run_id 的 Note | 有 Session 的每次新 Run 启动时通过 read_notes() 读取 |
| ~/.jarvis/context.md | 人工编辑或相关 Skill/工具写入时产生 | 每次新 Run 创建时读取 |
| <workspace>/.jarvis/context.md | 项目初始化或人工编辑时产生 | 每次该工作区新 Run 创建时读取 |
| summary_*.md | 手动 /compact 或启用自动压缩后由 Compactor 写入 | 主要用于查看压缩记录；当前不会自动作为独立长期记忆检索 |
| events.jsonl | Run 执行期间由 EventWriter 逐条写入 EventBus 事件 | 事件回放、故障排查和重新订阅时读取 |

一次有 Session 的 Run 是：

```text
send_message
  → thread.jsonl 先写入本轮 user 消息
  → Runner 读取 thread.jsonl、notes.md、全局/项目 context.md
  → 创建当前 Run 的 ExecutionContext
  → Loop 在内存中追加 assistant/tool_result
  → Run 结束后把本轮新增消息追加到 thread.jsonl
```

边界是：如果 Core 在 Run 结束前崩溃，本轮还在内存 ExecutionContext 中的模型消息和工具结果可能没有追加到 thread.jsonl；但已经写入的事件、Notes 和 user 消息仍可能存在。

### 什么情况下会调用 note_save？

当前不是每轮自动调用，也没有单独的记忆分类器。Runner 会在存在 Session、SessionStore 和 run_id 时注册 note_save；ExecutionContext 的 system prompt 会提醒模型：重要的持久事实要通过 note_save 保存。最终是否调用，仍由模型根据任务和用户要求决定。

适合调用 note_save 的情况包括：

- 用户明确说“记住”“以后都按这个规则”“保存到本次会话”；
- 确认了后续 Run 仍然有效的用户偏好，例如输出格式、语言、代码风格；
- 当前任务确认了稳定约束，例如不能修改某个目录、必须使用某个版本或必须通过某个测试；
- 形成了后续工作需要的设计决策、接口约定、已确认结论或待办；
- 长任务中产生了需要跨多轮继续使用的阶段性摘要。

不适合保存的内容包括：一次性工具输出、完整聊天记录、临时中间变量、未经确认的模型猜测、密码和 API Key 等敏感凭据。大段原始证据应该保留在文件、Artifact、thread 或 events 中，Notes 只保存短小、稳定、后续确实会用到的事实。

当前的判断标准主要来自 Prompt 和 Skill 约束，不是自动学习出来的。比如 summarize Skill 会要求摘要完成后调用 note_save；用户也可以直接说“把这条保存到 Session Notes”，让模型调用该工具。工具成功后立即追加到当前 Session 的 notes.md，并记录时间和 run_id。它不会自动把所有历史消息转成 Notes，也不会自动把 Notes 升级到全局或项目 context.md。

---

## 2. 如果提示词注入要求删除 C 盘某个文件夹，怎么处理？

**所属卡片：卡 04：安全与恢复**

我会把外部文件中的“删除 C 盘目录”当作不可信指令。它不能因为出现在代码注释、README 或工具返回结果里，就自动获得删除权限。

当前链路是：

1. 模型只能提出 bash 或 write_file 等结构化工具调用。
2. 统一入口查 Registry、校验参数，再进入 PermissionManager。
3. bash 默认策略是 ASK，write_file 默认也是 ASK；正常 ASK 模式下会发出审批事件，暂停当前 Run 等用户决定。
4. 用户拒绝后，不执行命令，返回 permission_denied，并把拒绝结果回填模型。
5. 即使用户允许，当前项目仍主要是应用级治理，不是操作系统沙箱。

需要如实说明一个边界：PermissionManager 的显式 auto 模式会优先放行工具，绕过默认 ASK 和部分静态检查；所以不能宣称任何 C 盘删除命令都能被当前代码自动拦截。生产化应增加硬性 deny 规则，例如禁止删除工作区外路径、系统目录和递归根目录；Auto 也不能绕过硬拒绝；同时需要路径归一化、符号链接检查和受限执行器。

面试回答：

> Prompt Injection 只能影响模型建议，不能直接获得工具权限。默认模式下删除 C 盘目录会进入人工审批，我会拒绝它；但当前没有 OS 级沙箱，Auto 模式和 Windows 绝对路径检查仍有边界，所以不会包装成绝对安全。

---

## 3. 工具重复执行怎么办？有没有幂等性？失败有备用方案和降级策略吗？

**所属卡片：卡 03：工具系统设计；相关卡片：卡 04：安全与恢复**

当前统一入口有有限重试，但没有通用幂等键和执行账本。

- _MAX_RETRIES = 2，所以可重试错误最多是初始执行加两次重试。
- 当前可重试类型主要是 runtime_error 和 rate_limited。
- 超时、Schema 错误、权限拒绝不会进入这个重试循环；超时直接返回失败。
- 如果副作用已经发生，随后因为网络或进程异常返回 runtime_error，重试可能造成重复写入。当前没有统一 idempotency_key、外部执行账本或 exactly-once 保证。

因此不能说工具天然幂等：

> 读操作通常可安全重试；写文件、发布、扣款这类副作用工具必须提供幂等键、版本号或状态查询。结果未知时先核对外部状态，不要盲目重做。

当前也没有代码级的“工具 A 失败自动切换工具 B”的通用 fallback 图。工具失败后返回 ToolResult(is_error=True)，Loop 把错误回填模型，模型可能修正参数、换工具、询问用户或结束。生产化可以增加工具 fallback 列表、熔断、幂等状态账本、只读降级和人工接管。

---

## 4. 为什么没用 RAG？search_files 难道不需要 RAG 吗？

**所属卡片：卡 02：Context 设计；相关卡片：卡 03：工具系统设计**

search_files 和 RAG 解决的问题不同。

search_files 是代码仓库里的精确检索工具：优先使用 rg 做正则/文本搜索，返回文件路径和行号；没有 rg 时回退 Python 正则扫描。它适合查类名、函数名、配置键、错误字符串和调用引用。Code Agent 修改代码前需要精确证据，语义相似但路径错误的结果可能误导。

RAG 通常包含文档切分、Embedding、向量索引、召回、重排和引用管理，适合从大量非结构化知识中按语义找相关片段。当前项目没有向量库、Embedding Pipeline 或 RAG Retriever，所以不能说已经实现 RAG。

不是 RAG 没用，而是当前范围优先保证代码符号精确定位、路径行号可追溯和结果新鲜度。如果以后有大量设计文档、历史事故和 API 文档，可以在 search_files 之外增加文档 RAG；代码精确搜索仍应保留。

---

## 5. 一个工具反复失败怎么办？会一直重复执行吗？超大工具结果怎么处理？

**所属卡片：卡 03：工具系统设计；相关卡片：卡 02：Context 设计**

统一入口不会对一次 ToolCall 无限重试：

1. 初始执行加两次有限重试。
2. 只有 runtime_error 和 rate_limited 进入退避重试。
3. Schema 错误、权限拒绝、未知工具等形成错误结果。
4. 失败结果以 ToolResult(is_error=True) 回填模型。

但要区分“一次 ToolCall 的内部重试”和“模型下一轮又提出同样 ToolCall”。当前没有完善的相同工具、相同参数、相同错误检测和熔断器，所以模型理论上可能在后续轮次重新提出同一个失败调用；只能依靠错误回填、max_steps 和人工停止限制。生产化应增加重复动作指纹、失败计数、同参熔断和总工具调用预算。

大结果有三层处理：

- search_files 限制结果数和扫描规模；read_file 限制单文件大小；bash 限制超时和输出大小。
- Session 历史读取时，tool_result 超过 8,000 字符默认只保留前 4,000 字符，并添加省略提示。
- 完整输出保留在 Run 事件或工具原始产物中，后续通过路径、行范围、摘要或再次检索按需取回。

边界是：8,000/4,000 字符截断主要发生在从持久化 thread.jsonl 读取历史时；同一个 Run 的内存消息不一定立即经过同样截断。运行中自动压缩只有开启阈值并达到 context_pct 才触发。

---

## 6. 当前 Session 锁是不是意味着同一个 Session 不能有两个 TUI？

**所属卡片：卡 01：Memory/Session；相关卡片：卡 04：安全与恢复**

锁限制的是同一个 Core 进程里的同一个 Session 同时只能有一个写操作，不是限制 TUI 数量。

- 两个 TUI 可以分别连接同一个 Core。
- 使用不同 Session 时可以并行处理。
- 使用同一个 session_id 时，第一个 send_message、close 或 compact 持锁期间，第二个操作返回 SESSION_BUSY，当前不是排队。
- get_history 这种只读操作不需要这把写锁。

因此可以说“两个 TUI 可以连接 Core”，但不能说“两个 TUI 能同时修改同一个 Session”。这把锁是进程内 asyncio.Lock，不是跨 Core、跨机器或文件系统分布式锁。

---

## 7. 工具调用中的成功、可重试失败、不可重试失败、用户拒绝分别是什么？

**所属卡片：卡 03：工具系统设计**

| 类型 | 当前含义 | 处理方式 |
|---|---|---|
| 成功 | invoke 返回 ToolResult(is_error=False) | 发布完成事件，按 tool_use_id 回填模型 |
| 可重试失败 | 主要是 runtime_error、rate_limited | 有限指数退避；耗尽后返回错误 |
| 不可重试失败 | Schema 错误、未知工具、超时、明确业务错误等 | 直接返回结构化错误，要求模型修正、换方案或降级 |
| 用户拒绝 | PermissionManager 返回 deny 或 timeout，统一入口转成 permission_denied | 不执行工具，不因模型重复请求而偷偷重试 |

副作用工具要注意：runtime_error 可能发生在副作用已经提交之后，所以“可重试”只是错误分类允许重试，不等于业务上安全重试。

---

## 8. “审批不会卡死整个服务”到底指什么？会不会卡住本次 Run？

**所属卡片：卡 04：安全与恢复**

它指不会阻塞整个 Core 进程的 socket 读循环，但会等待审批的当前 Run。

1. 工具协程创建 pending Future，并发布 permission.requested。
2. 当前 Run 的协程异步等待 Future。
3. SocketServer 为请求使用独立 Task，读循环仍能读取新的 JSON-RPC 消息。
4. permission.respond 到达后，处理器 resolve 对应 Future。
5. 当前 Run 继续执行工具。

准确说法：

> 审批会暂停当前 Run 的工具步骤，但不会同步阻塞 Core 的事件循环、连接读循环或其他 Session。

如果用户一直不响应，当前 Run 会等到审批超时；默认 60 秒，配置为 0 表示不超时，可能长期挂起。当前 Session 的后续写操作还会受 Session 锁影响。所以它不是“任何东西都不卡”，而是“异步等待不把整个 Core 卡死”。

---

## 9. notes.md 保存在哪里？和 context.md 有什么区别？什么时候保存？能手动触发吗？

**所属卡片：卡 01：Memory 设计**

notes.md 保存于当前 Session 目录：

```text
<session_store_root>/<session_id>/notes.md
```

当前 Core 启动代码把 Session Store 根目录设置为当前工作区的 .jarvis/sessions，完整路径类似：

```text
<workspace>/.jarvis/sessions/sess-xxxxxxxxxxxx/notes.md
```

note_save 由 Runner 在存在 Session、Store 和 run_id 时注册。工具成功后，SessionStore.append_note() 立即追加：

```text
## Note (<UTC 时间>, <run_id>)
<content>
```

区别：

| 项目 | notes.md | context.md |
|---|---|---|
| 作用域 | 单个 Session | 全局用户或当前项目 |
| 内容 | 当前会话精选事实/决策 | 跨 Session 稳定背景、规则、偏好 |
| 写入方式 | note_save 主动追加 | 人工编辑、初始化 Skill 或文件写入 |
| 读取方式 | 当前 Session 下一次 Run | 每次 Run 读取全局/项目文件 |
| 生命周期 | 跟随 Session | 跨 Session、跨 Run |

当前没有独立分类器判断某个事实是否值得保存。System Prompt 会提醒模型在发现稳定约束、用户偏好、已确认决策或后续必须遵守的事实时调用 note_save。

可以手动触发：

- 直接告诉 Agent：“把这条保存到本 Session Notes。”
- 使用 summarize Skill；该 Skill 要求摘要完成后调用 note_save。
- 人工编辑 notes.md，但生产系统应增加来源、版本、删除和冲突管理。

---

## 10. 当前 Session 是否完全无法恢复？能不能读 thread.jsonl 和 notes.md 恢复？

**所属卡片：卡 04：安全与恢复**

不能说完全无法恢复，也不能说已经支持通过 session_id 自动恢复。

已落盘的数据仍然存在：

- thread.jsonl：历史消息；
- notes.md：Session Notes；
- meta.json：Session 元数据；
- Run/events.jsonl：已发生事件。

但 Core 重启后，SessionManager 的内存 _sessions 和 _locks 会重新创建，启动流程没有自动扫描 Session 目录并加载 meta.json。因此仅拿旧 session_id 调当前 Session RPC，可能得到 session not found。TUI 当前重连流程也会创建新 Session，不能自动回到旧 Session。

分情况：

- TUI 断线、Core 仍运行：旧 Session 还在 Core 内存中，知道旧 session_id 后理论上可以继续；但当前 TUI 自动重连会新建 Session，不是自动恢复。
- Core 崩溃重启：文件还在，但没有自动 Session 扫描、Run checkpoint、待审批 Future、子进程状态和副作用核对，不能无损续跑。
- 人工恢复：可以读 thread.jsonl 和 notes.md，重新创建 Session/Run，把历史和 Notes 装配进新的 ExecutionContext；这是手工或补充代码恢复。

生产化需要启动扫描未完成 Session/Run、重建内存索引和锁、保存 checkpoint、核对外部动作状态，并用幂等键决定续跑、补偿或人工确认。

---

## 11. events.jsonl 负责什么？它属于 Trace 吗？

**所属卡片：卡 05：Trace 与 Event**

events.jsonl 是某个 Run 的事件流水文件。EventBus 发布的 Run、Step、模型、工具、权限、子 Agent、压缩和结束事件，会由 EventWriter 序列化成 JSON 行追加并 flush。

用途：

- TUI 断线后的事件回放；
- 查看 Run 进度和状态变化；
- 用 run_id、tool_use_id 定位工具调用；
- 区分 Core 没执行和 Core 执行了但 UI 没展示。

关系是：

> events.jsonl 是 Run 级事件产物；Trace 是更广义的结构化观测产物。Core 会把 EventBus 事件转换成 TraceRecord(layer="event") 写入全局 Trace 文件，所以事件也会进入 Trace；Trace 还记录 IPC 和 LLM API 请求/响应，因此两者不是同一个文件。

---

## 12. context_pct 是怎么计算的？

**所属卡片：卡 02：Context 设计**

Provider 使用：

```python
context_pct = usage.input_tokens / _context_window(self._model)
```

其中：

- usage.input_tokens 是本次发给模型的输入 Token 数，包含 system、messages 和工具 Schema；
- _context_window(model) 返回模型的最大上下文窗口；未知模型使用代码默认窗口；
- 0.8 表示输入估计使用了上下文窗口的 80%。

自动压缩判断在工具轮完成后：

```text
auto_threshold > 0
  且 response.stop_reason == "tool_use"
  且 context_pct >= auto_threshold
  → 调用 Compactor
```

它不是剩余可用 Token 百分比，也不是费用比例；当前没有把未来新增消息单独折算成一个总预算。默认 auto_threshold=0，所以默认不自动压缩。

---

## 13. 卡片 3 里统一基类、统一 invoke、参数校验和权限审批到底怎么做？

**所属卡片：卡 03：工具系统设计**

统一链路：

```text
BaseTool
  → ToolRegistry.register()
  → Registry.tool_schemas() 给模型
  → 模型输出 name + id + input
  → invoke_tool()
      1. Registry.get(name)
      2. params_model.model_validate(input)
      3. PermissionManager.check_and_wait()
      4. asyncio.wait_for(tool.invoke(input))
      5. 成功/重试/失败事件
      6. ToolResult 按 tool_use_id 回填
```

具体职责：

- BaseTool 定义 name、description、input_schema、可选 params_model 和异步 invoke(params)。
- ToolRegistry 保存工具实例、按名称查找、生成模型 Schema。
- invoke_tool 统一处理未知工具、参数校验、权限、审批、超时、重试、事件和结果封装。
- 有 params_model 的工具在真实 invoke 前执行 Pydantic model_validate；失败返回 schema_error，不执行工具。
- PermissionManager 根据工具、参数和 Session 判断 allow/deny/ask；高风险工具进入 pending Future 并发 permission.requested。
- 只有校验和授权通过后才调用具体工具 invoke。

MCP 工具当前把 Server 的 inputSchema 暴露给模型，但 McpTool.params_model=None，所以 MCP Schema 没有自动变成本地完整 Pydantic 校验；统一权限、超时和事件链路仍然存在。

---

## 14. events.jsonl、thread.jsonl、Trace 和普通日志分别是什么产物？

**所属卡片：卡 05：Trace 与 Event**

| 产物 | 默认位置/形态 | 记录什么 | 是否作为模型历史 |
|---|---|---|---|
| events.jsonl | 每个 Run 目录 | EventBus 事件流水 | 否，主要回放和观测 |
| thread.jsonl | Session 目录 | user/assistant 消息、tool_use/tool_result | 是，读取后装配为下一次 Run 的 messages |
| Trace 文件 | 默认 ~/.jarvis/traces/daemon.jsonl | TraceRecord：IPC、Event、LLM API、run_id、step、耗时等 | 否，调试、审计和 Eval 归因 |
| 普通日志 | 默认 ~/.jarvis/logs/core.log | Python 组件日志、异常堆栈、连接和启动信息 | 否 |

TraceWriter 使用异步队列写 Trace；TracingProvider 记录 CORE→LLM 和 LLM→CORE；Core 订阅 EventBus，把事件写成 layer="event" 的 TraceRecord；IPC 层还可以记录客户端到 Core 的请求、响应和推送。普通日志重点是组件异常，Trace 重点是一次 Agent 的行为轨迹。

---

## 15. 为什么用 BFCL？79.16% 体现了 Agent 的什么能力？

**所属卡片：卡 06：Eval**

BFCL 提供相对标准的函数调用任务、工具定义和 Checker，可以评估模型是否根据用户目标产生正确的结构化工具调用和参数，比只看自然语言最终答案更接近 Tool Calling 接口。

当前报告：

- planned = 3,641
- scored = 3,638
- correct = 2,880
- accuracy = 2,880 / 3,638 = 79.16%
- coverage = 3,638 / 3,641 = 99.92%

79.16% 主要体现选定 BFCL 类别中的：

- 工具调用是否存在或是否选择合适工具；
- 工具参数结构和值是否符合 Checker；
- Provider、AgentLoop、参数规范化和结果记录这条函数调用链是否跑通。

它不能直接证明真实代码修改、Shell 副作用安全、长期记忆或崩溃恢复一定正确，也不是完整官方 BFCL v4 总分。面试时说“选定类别的端到端函数调用表现”，不要说“Agent 综合能力 79.16 分”。

---

## 16. 主 Loop 逐个 await 工具，算不算一定程度的并行？

**所属卡片：卡 12：多工具调用**

严格说当前工具执行不是并行，而是模型输出可以一次包含多个调用，运行时按顺序串行执行：

```python
for tc in response.tool_calls:
    result = await invoke_tool(...)
```

时间线：

```text
tool A 开始 → A 完成/失败
                  ↓
             tool B 开始 → B 完成/失败
                                   ↓
                         才进入下一次 Provider.chat()
```

所以它确实会等本轮所有工具结果回填后，Loop 才进入下一轮模型调用；但这只是本轮的同步屏障，不代表 A、B 同时执行。两个各 10 秒的工具，当前约需 20 秒，而真正并行才可能接近 10 秒。

后台子 Agent 使用 asyncio.create_task 交错运行，是另一条并发路径。要把独立只读工具改成并行，还要处理依赖、文件冲突、审批、取消、限流和结果 ID 配对。

---

## 17. “根据意图、任务类型、角色和权限缩小候选集”的逻辑在哪里？

**所属卡片：卡 13：多个工具怎么选择**

这句话要拆成当前实现和设计方案。

当前真正实现的候选集过滤主要在：

- SessionManager.send_message() 识别 /skill，从 Skill frontmatter 读取 allowed_tools；
- AgentRunner._build_registry(tool_whitelist=...) 只注册白名单工具；
- 子 Agent 的 planner、executor、reviewer Profile 可以提供 allowed_tools；
- 没有白名单时，Runner 注册内置工具、Session 工具、子 Agent 工具和 MCP 工具；
- PermissionManager 在工具实际执行前按工具和参数判断 allow/deny/ask。

当前没有独立的意图分类器或路由模型，也没有代码按自然语言任务类型自动生成工具集合。用户意图主要交给模型根据工具 name、description 和 input_schema 选择。

工具类型：

- 只读探索：project_map、list_dir、search_files、read_file；
- 写入/执行：write_file、bash；
- 状态记忆：Task 四件套、note_save；
- 委派扩展：spawn_agent、agent_result、MCP 工具。

角色主要是 planner、executor、reviewer。角色工具白名单限制暴露范围，但不等于操作系统只读隔离，最终仍要靠 PermissionManager 和工具自身检查。

准确回答是：

> 当前主要是 Skill、角色和显式白名单做静态缩小，模型基于 Schema 选择，PermissionManager 做最终授权；还没有独立意图路由器自动按任务类型筛工具。

---

## 18. 添加一个新 Skill 到底怎么做？在哪里加载？

**所属卡片：卡 14：Skill（对应你之前提到的原卡 15）**

Skill 是 Markdown 文件，不是 Python 插件。支持：

```text
项目级：
<workspace>/.jarvis/skills/my_skill.md
<workspace>/.jarvis/skills/my_skill/SKILL.md

用户级：
~/.jarvis/skills/my_skill.md
~/.jarvis/skills/my_skill/SKILL.md

内置：
src/jarvis/core/skills/builtin/my_skill.md
src/jarvis/core/skills/builtin/my_skill/SKILL.md
```

最小文件：

```markdown
---
name: review
description: 审查当前项目的代码改动
allowed_tools:
- project_map
- search_files
- read_file
- bash
---

你是代码审查 Agent。
目标：$ARGUMENTS

步骤：
1. 定位改动范围。
2. 读取相关实现和测试。
3. 只报告有证据的问题，并给出路径和行号。
```

加载流程在 SessionManager.send_message()：

1. 用户输入 /review 检查最近改动。
2. Manager 拆出 Skill 名称 review 和参数。
3. SkillLoader.resolve(name) 按项目级 → 用户级 → 内置搜索。
4. _parse_skill_file() 读取 name、description、allowed_tools 和正文。
5. render_prompt() 将 $ARGUMENTS 替换成用户参数。
6. Manager 将渲染正文作为 goal，将 Skill 正文作为 system_prompt_override，将 allowed_tools 传给 Runner。
7. Runner 的 _build_registry() 只注册白名单工具。
8. 发布 SkillInvokedEvent，供 TUI/Trace 观测。

文件不存在或 frontmatter 解析失败时，当前会退回普通消息流程。新增文件后下一次 /name 调用即可读取，不需要重新编译。

当前解析器实际处理的 frontmatter 主要是 name、description、allowed_tools 和 $ARGUMENTS；不能写一个 parameters、version 就认为运行时会自动使用。复杂参数校验需要增加代码解析/Schema，或在正文中约束并用测试验证。

---

## 19. MCP 配置在哪里？在哪里读取？

**所属卡片：卡 16：MCP**

MCP Server 配置由 get_config() 读取。未设置 JARVIS_CONFIG 时，TOML 的读取顺序是默认全局 ~/.jarvis/config.toml，然后当前工作区 .jarvis/config.toml；之后再由全局 .env、项目 .env 和系统环境变量覆盖。设置 JARVIS_CONFIG 后，则读取该显式 TOML 文件，仍会由项目/系统环境变量覆盖，不再走默认 TOML 和项目 TOML 的常规分支。

TOML 示例：

```toml
[mcp]
servers = [
  { name = "local-tools", transport = "stdio", command = "python", args = ["server.py"] },
  { name = "remote-tools", transport = "tcp", host = "127.0.0.1", port = 3000 }
]
```

启动时 CoreApp.start() 调用 get_config()，创建 McpServerManager；当 config.mcp.servers 非空时调用 start_all()。Manager 为每个 Server 建立 stdio 或 TCP 连接，完成 initialize，调用 tools/list，把工具包装成 McpTool。每个 Run 的 Runner 再按白名单把这些工具加入 ToolRegistry。

当前仓库和用户目录没有发现实际 config.toml/MCP Server 配置时，默认 mcp.servers=[]，因此项目具备 MCP Client/工具接入代码，但不能说当前已经启动了某个真实 MCP Server。配置存在也不等于连接一定成功；启动失败时 Manager 记录日志并跳过该 Server。

源码入口：src/jarvis/core/config.py 的 get_config/_apply_toml，src/jarvis/core/app.py 的 McpServerManager.start_all，以及 src/jarvis/core/mcp/server.py。

---

## 20. 当前安全方面的沙箱实现如何？生产级 Agent 怎么设计沙箱？多租户怎么隔离？

**所属卡片：卡 04：安全与恢复；相关卡片：卡 21：生产级 Agent 五层架构**

### 当前项目做到哪一步？

> 当前更准确的说法是“应用层运行时治理”，还不是完整的操作系统级沙箱。

目前已有的限制包括：

- 工具白名单和 Skill/角色工具白名单，限制模型能看到和注册的工具；
- Pydantic 参数 Schema 校验，以及工具自己的路径、文件大小、行范围、输出大小检查；
- `bash` 和 `write_file` 默认进入 ASK 审批，用户可以在执行前拒绝；
- Shell 有超时和输出截断，工具调用有统一事件、Trace 和错误分类；
- 权限策略支持 allow、deny、ask，以及 Session 级和持久化的授权记录。

这些机制能降低误操作概率，但没有建立宿主机之外的强安全边界：

- Shell 仍然启动宿主机原生进程，继承宿主权限、网络能力和部分环境；
- 工作目录不是沙箱，不能单独阻止绝对路径、符号链接、环境变量或命令解释器绕过；
- 当前文件路径检查主要拒绝包含 `..` 的路径，不能等同于“只能访问工作区”；
- 超时主要控制等待和直接子进程，不能保证完整进程树清理；
- 子 Agent 与主 Agent 共享工作区，可能产生文件冲突；
- 没有完整的 CPU、内存、磁盘、网络、进程数和文件描述符配额；
- 当前项目是本地单用户 Core/TUI 架构，没有完整的租户身份、租户级资源和数据隔离层。

所以面试时应说：

> 当前有工具白名单、Schema、权限审批、路径检查、超时、输出限制和 Trace，但没有容器或 microVM 级沙箱。它能治理工具调用，不能把宿主机隔离成安全执行环境。

### 生产级 Agent 的沙箱怎么设计？

生产级设计通常把“模型决策”和“高风险执行”分开，至少包含以下层次：

1. **身份和风险分级**：先认证调用者、租户、项目和任务风险；模型不能改变自己的租户或权限。
2. **控制面与执行面分离**：Core/Agent 编排服务只负责计划、审批、状态和 Trace；真正的 Shell、代码和文件操作交给受限执行器。
3. **一次任务一个隔离执行环境**：使用容器、microVM 或操作系统沙箱，以非 root 用户运行；只挂载任务所需的工作区子目录，不挂载宿主根目录、凭据目录和 Docker Socket。
4. **文件系统隔离**：路径归一化后做 allowlist；检查符号链接和挂载点；默认临时工作区，任务结束销毁或按策略保存 Artifact。
5. **进程和资源隔离**：PID namespace/Job Object、CPU、内存、磁盘、进程数、文件数和执行时间都设硬上限；取消时清理整个进程树。
6. **网络和秘密隔离**：默认禁止出网或只允许域名/端口 allowlist；通过短期凭据代理访问外部服务，不把长期 API Key 直接放进 Agent 环境变量。
7. **执行前后治理**：高风险工具先审批；执行前记录计划和参数，执行后记录退出码、文件差异、网络访问和 Artifact 哈希；对发布、删除、支付等动作增加二次确认和幂等键。
8. **验证和逃逸测试**：持续测试路径穿越、符号链接、Shell 注入、Prompt Injection、网络越权、容器逃逸和资源耗尽；安全失败要 fail-closed 并转人工。

容器或 microVM 只是实现手段，不是“用了容器就安全”。关键是挂载、权限、网络、凭据、资源和进程树都要有明确策略，并且由执行器强制执行。

### 多租户隔离怎么做？

多租户不能只靠给 Session 加一个 `tenant_id` 字段，至少要做四层隔离：

| 层次 | 生产要求 |
|---|---|
| 身份与授权 | 每个请求携带不可伪造的 tenant/user/project 身份；服务端从认证信息建立租户上下文，不能信任模型或客户端传入的租户字段；所有工具调用重新做租户授权 |
| 数据与文件 | Session、thread、Notes、Task、Artifact、Trace 按 tenant/project 分区；数据库使用租户作用域或 Row-Level Security；对象存储使用租户前缀和独立密钥；执行器只挂载当前租户工作区 |
| 执行与网络 | 每个租户使用独立容器/microVM 或至少独立 namespace、服务账号、网络策略和秘密；禁止跨租户路径、进程、端口和凭据访问 |
| 资源与观测 | 每租户设置并发、Token、时间、磁盘、网络和费用配额；Trace、日志、告警按租户隔离并脱敏；防止一个租户的长任务拖垮其他租户 |

请求链路应该把租户上下文贯穿到底：

```text
认证身份
  → tenant/user/project context
  → Session/Run/Tool 授权
  → 租户专属执行器
  → 租户作用域的文件、数据库、Artifact、Trace
```

如果采用共享 Core，也要在每个资源访问点强制检查租户上下文，并使用租户级队列、配额和存储隔离；不能只在入口检查一次。对高风险和强隔离场景，更适合每租户或每任务独立 worker/容器，牺牲一部分成本换取更清晰的爆炸半径。

当前 JARVIS 还不能承诺上述多租户能力。它已有 Session、权限和工作区概念，但 Session ID 不是租户身份，工具白名单也不是租户隔离；如果要生产化，我会先实现认证后的租户上下文、租户作用域存储、受限执行器和资源配额，再用跨租户访问、路径越界、网络越权和 noisy-neighbor 测试验收。
