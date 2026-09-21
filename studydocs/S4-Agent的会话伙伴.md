# S4 - 把 Agent 变成会话伙伴

> 来源：用户于 2026-09-09 提供的 S4 阶段截图整理。
>
> 本文记录的是阶段设计与预期实现，不代表当前 `stage/s3` 工作区已经包含这些代码。截图中局部代码因画面截断，以下以可读的结构化转录为准。

## 阶段定位

| 项目 | 内容 |
| --- | --- |
| 阶段 | S4 |
| 分支 | `stage/s4` |
| 本阶段新增 | `Session` 容器、`thread`/`notes` 分层记忆、`note_save` 工具、`jarvis chat`、带输入框的 TUI |
| 依赖 | S3 的 `AgentRunner`、`ToolRegistry`、任务系统、终端 TUI；S2 的 IPC 事件流 |

S3 中，一次 `jarvis run --goal ...` 对应一次独立 run。run 结束后，下一次 run 不再拥有上一轮的对话、工具结果和结论。S4 的核心改变是把运行单位从 **run 提升到 session**：一个 session 包含多轮 run，因而把 Agent 从一次性执行器变成可连续对话的伙伴。

例如，第一轮询问项目 Python 版本，Agent 读取 `pyproject.toml` 后回答；第二轮要求写一个适配该版本的 demo 时，Agent 应知道上一轮结论，不必重新读取同一文件。

## 核心对象与存储

```text
Session
  ├── meta.json       会话元数据
  ├── thread.jsonl    完整 API 对话消息流
  ├── notes.md        Agent 主动保存的持久事实/决策
  └── runs/
      └── <run_id>/
          ├── events.jsonl
          └── .tasks/
```

预期根目录是用户级的 `~/.jarvis/sessions/sess-<id>/`，而不是项目目录下的 `runs/`。原因是连续会话不应随当前工作目录切换而丢失；S4 希望将 session、配置和日志统一放在用户级目录。

`meta.json` 记录 session ID、模式、状态、标题、创建/更新时间，以及其包含的 `run_ids`。在该模型中：

```text
run     = 一次用户输入触发的一轮 Agent 执行
session = 多轮 run 的连续会话容器
```

## SessionManager

`CoreApp` 将 `session.create`、`session.send_message` 等 IPC 命令交给 `SessionManager`。它是 daemon 内的会话入口，负责创建、状态转换、锁、事件发布和持久化。

创建 session 的概念流程：

```python
async def create(self, mode: SessionMode, title: str = "") -> Session:
    sid = f"sess-{uuid.uuid4().hex[:12]}"
    ts = _now()
    session = Session(
        id=sid,
        mode=mode,
        status="active",
        title=title,
        created_at=ts,
        updated_at=ts,
        run_ids=[],
    )
    self._sessions[sid] = session
    self._locks[sid] = asyncio.Lock()
    self._store.write_meta(session)
    await self._bus.publish(SessionCreatedEvent(...))
    return session
```

每个 session 配一把 `asyncio.Lock`。如果同一会话的上一个 run 尚在执行，新的消息应返回 `SESSION_BUSY`，而不是允许两轮对话并发改写同一份 thread 和 notes。

`send_message` 的关键顺序是：

```text
校验 session 存在、未关闭、未忙
  ↓
如果此前为 waiting_for_input，发布 resumed 事件
  ↓
先把用户消息追加到 thread.jsonl
  ↓
生成 run_id，写入 session.run_ids 与 meta.json
  ↓
调用 AgentRunner.run_and_capture(..., session=session, store=store)
  ↓
chat 模式：转为 waiting_for_input 并发布事件
one_shot 模式：转为 closed 并发布事件
```

必须在启动 `AgentRunner` 前写入用户消息，否则本轮输入不会在 runner 恢复的历史中出现。

## 两层记忆

### thread.jsonl：完整对话回放

`thread.jsonl` 按行保存 API 消息，内容不是仅供人阅读的聊天日志，而是能够直接作为 Anthropic `messages` 传回模型的消息结构：

```jsonl
{"role":"user","content":"项目用什么 Python 版本？"}
{"role":"assistant","content":[{"type":"text","text":"先看 pyproject.toml。"},{"type":"tool_use","id":"tool_01","name":"read_file","input":{"path":"pyproject.toml"}}]}
{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool_01","content":"requires-python = \">=3.12\""}]}
{"role":"assistant","content":[{"type":"text","text":"项目使用 Python 3.12。"}]}
```

读取时应逐行 JSON 解析，跳过损坏记录，只保留 `user` / `assistant` 角色，并调用类似 `_trim_orphan_tool_use()` 的逻辑移除无匹配 `tool_result` 的 `tool_use`。这是为了保证回放后的 history 符合模型 API 的工具调用配对要求。

S4 的选择是**完整回放整个 thread**，而不是只取最近 K 轮。理由：

- `tool_use` 和 `tool_result` 必须保持配对；
- 早期工具结果可能仍是后续回答的依据；
- 不将“哪些历史重要”这一判断交给框架；
- 利用 Anthropic prompt caching 缓存未改变的历史前缀，降低完整回放的成本。

### notes.md：Agent 主动写入的耐久事实

`notes.md` 不记录所有对话，而只记录未来仍有用的事实和决策，例如“项目使用 Python 3.12”。它为未来的 context compaction 预留：thread 变长后可压缩，但 notes 应保留为不会被压缩掉的事实层。

`note_save` 只在 session run 内注册：

```python
class NoteSaveTool(BaseTool):
    name = "note_save"
    description = (
        "Save a fact or decision to the session's notes. "
        "These notes will be visible to you in future turns of this session."
    )

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        content = str(params["content"]).strip()
        if not content:
            return ToolResult(content="empty content", is_error=True)
        self._store.append_note(self._sid, content, self._run_id)
        return ToolResult(content="saved")
```

不在每轮结束后自动总结为 notes，原因是额外 LLM 调用会增加成本和延迟，自动总结也难以可靠判断保留内容。让 Agent 在做出决策当下调用 `note_save`，该意图更明确，也可在工具事件和 TUI 中审计。

### 两层记忆如何供给 LLM

session run 启动时，`AgentRunner` 从 store 恢复：

```python
history = store.read_messages(session.id)  # thread.jsonl
notes = store.read_notes(session.id)       # notes.md

context = ExecutionContext(
    run_id=run_id,
    goal=goal,
    max_steps=config.agent.max_steps,
    prefill_messages=history,
    session_notes=notes,
)
```

`history` 作为 `messages` 前缀；本轮结束时，runner 只将本轮新增消息写回 thread：

```python
store.append_messages(session.id, context.messages[prefill_len:], run_id=run_id)
```

`notes` 不伪装成一条 user message，而是拼接进 system prompt：

```text
<base system prompt>

## Session Notes
<notes.md 的内容>

Remember important durable facts by calling note_save.
```

这样模型把 notes 当作会话级持久上下文，而非“用户刚刚说过的一句话”。S4 尚未处理 notes 中的 prompt injection；截图将其列为后续权限和上下文治理阶段的责任。

## `jarvis chat`：会话协议的 CLI 参考实现

`jarvis run` 是一次性命令：发送 goal，等 run 结束，然后退出。`jarvis chat` 连接不断开，主要做三件事：

1. 接收用户输入；
2. 将输入通过 IPC 发给 daemon；
3. 打印 daemon 推送的事件。

概念流程：

```python
client = SocketClient(config.host, config.port)
await client.connect()
client.on_event(printer.handle)
loop_task = asyncio.create_task(client.run_event_loop())

await client.send_command("event.subscribe", {
    "topics": ["session.*", "run.*", "step.*", "tool.*", "llm.*"],
})
created = await client.send_command("session.create", {"mode": "chat"})
session_id = str(created["session_id"])

while True:
    line = await _readline("> ")
    if not line.strip():
        continue
    await client.send_command("session.send_message", {
        "session_id": session_id,
        "content": line,
    })
```

必须先订阅事件再创建/发送消息，避免错过早期 session 事件。异步函数不能直接调用阻塞式 `input()`，因此 `_readline()` 应使用 `loop.run_in_executor(None, input, prompt)`，让 socket 的事件循环继续接收流式 token 和工具事件。

CLI 与 TUI 都只是 daemon 的客户端外观，不各自维护 session 状态；真正的 session、thread、notes、run 调度都由 daemon 持有。

## `jarvis run` 的兼容方式

S4 不删除原有 `jarvis run --goal ...`。它在 daemon 内部创建一个 `mode="one_shot"` 的 session，再调用同一个 `SessionManager.send_message()`：

```text
jarvis run
  → 创建 one_shot session
  → 发送唯一的一条用户消息
  → 运行结束后关闭 session
```

`chat` 与 `one_shot` 的区别只在 run 结束后的状态：

- `chat`：`waiting_for_input`，可接收下一轮消息；
- `one_shot`：`closed`，本次会话结束。

这是一次有意的存储迁移：即便一次性 run 的事件与 TUI 行为不变，底层 run 数据也归入 `~/.jarvis/sessions/<sid>/runs/<run_id>/`。

## TUI：从只读事件窗到可输入会话

S4 的 TUI 底部增加输入框。提交后走与 `jarvis chat` 相同的 `session.send_message` 协议。

使用 `TextArea` 而非单行 `Input`，并自定义按键行为：

- `Enter`：提交；
- `Shift+Enter` / `Ctrl+J` / `Super+Enter`：换行；
- run 执行期间禁用输入；
- 收到 `session.waiting_for_input` 后重新启用输入。

因此，CLI 的 `[waiting for input]` 和 TUI 中输入框恢复可用，是同一个 daemon session 状态的两个客户端表现。

## 验证目标

预期手动验证：

当前版本已经把下面两个历史步骤合并为一个项目级启动命令：

```powershell
cd D:\path\to\project
jarvis
```

在 TUI 中依次发送：

```text
项目的 Python 版本是多少？看 pyproject.toml
写一个适合该版本的新特性 demo 到 /tmp/demo.py
```

检查结果：

- session 目录有 `meta.json`、`thread.jsonl`、`notes.md`、`runs/`；
- `thread.jsonl` 含完整 `tool_use` / `tool_result` 块，而非仅最终文本；
- `notes.md` 含 Agent 通过 `note_save` 主动保存的事实；
- 第二轮不必重复读取 `pyproject.toml`，可直接按第一轮获得的版本信息写 demo；
- 第二个 run 的 `events.jsonl` 不应再次出现对 `pyproject.toml` 的 `read_file` 调用。

## S1 - S4 演进

```text
S1：单进程 Agent 最小闭环
S2：Core daemon + IPC 事件流
S3：Agent 自主规划任务，并可操作文件和 shell
S4：session 将多个 run 组织为连续会话，加入 thread + notes 分层记忆
```

S4 的目标不是简单把上一轮最终文本拼接回 prompt，而是同时恢复：

1. 完整、可被 API 接受的消息流（`thread.jsonl`）；
2. Agent 主动沉淀的会话级耐久事实（`notes.md`）；
3. 所有 run、锁、状态、事件与磁盘存储的统一会话容器（`SessionManager`）。
