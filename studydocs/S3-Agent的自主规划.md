# S3-Agent 的自主规划

> 来源：[语雀原文](https://www.yuque.com/chengxuyuancarl/nvd81p/vduokwbgevrbxzqq)
>
> 本文为语雀页面「S3-Agent 的自主规划」的本地 Markdown 整理版。

## 第 3 阶段：Agent 的自主规划与终端可视化

| 项目 | 内容 |
| --- | --- |
| 阶段 | s3 |
| 分支 | `stage/s3` |
| 本阶段新增 | Agent 内部任务系统、扩展工具集（`bash` / `write_file` / `list_dir`）、TUI 终端化改版 |
| 依赖上一阶段 | s2 的守护进程架构、EventBus、IPC 订阅机制 |

## 本阶段要做什么

s2 结束时，整个系统架构已经完成：

- `jarvis-core` 守护进程持续运行；
- `jarvis run` 发送目标、订阅事件；
- TUI 实时显示进度。

但它有一个根本性的局限：Agent 只能执行用户分解好的单条指令。

如果目标是“分析这个 Python 项目的代码风格并写一份报告”，不能直接把这句话扔给 Agent。它会在一次对话循环里把所有事情混在一起做，结果很可能是混乱的。需要手动拆解：先分析目录结构，再逐一读文件，再总结……这些步骤完全由人来排列和触发。

s2 的任务系统（`task_create` / `task_update`）确实存在，但它暴露在 IPC 层上，是用户通过客户端操控的外部接口。问题在于：任务分解本质上是一件认知性工作，LLM 在这件事上比人更系统、更不容易漏项。为什么不让 Agent 自己来？

s3 做了一个根本性的翻转：

> 把任务系统从用户可见的控制接口，变成 Agent 的私有认知工具。

用户只给一个目标，Agent 自己决定要不要拆解、拆成什么、按什么顺序做。从用户视角看，命令没有变；变的是 Agent 在这个目标下的行为模式——它现在会先主动规划任务，然后按计划执行。

## 守护进程启动 run

用户的命令经过 CLI → SocketClient → daemon 这条 s2 已经建好的链路，到达 `CoreApp._agent_run_handler`。

这里有一个重要的细节：`run_id` 在 handler 里生成，然后传入 `runner.run()`。这样 `AgentRunResult` 能立刻把 `run_id` 返回给客户端——TUI 在 Agent 跑完之前就拿到了 `run_id`，可以提前准备订阅，不会错过 `run.started` 事件。

`asyncio.create_task()` 把整个 run 甩到后台异步执行，handler 立即返回，不阻塞 socket 服务器接收下一条命令。

`_running_runs` 维护一个活跃 task 的集合，shutdown 时逐个 cancel，保证进程不会在 Agent 还在运行时直接退出。

## AgentRunner 接入 TaskManager

进入 `AgentRunner.run_and_capture()`，s3 在这里增加了两行关键代码。

`TaskManager` 的初始化路径是：

```text
runs/<run_id>/.tasks/
```

每个 run 的任务数据完全隔离，互不干扰，可以随时回溯某次 run 的完整规划记录。

`_build_registry(task_manager)` 把 4 个任务工具和这个 `task_manager` 实例一起注册进工具注册表。

所有 4 个任务工具共享同一个 `task_manager` 实例。这一点很关键：`task_create` 写入的文件，`task_update` 和 `task_list` 能读到，因为它们操作的是同一个 `.tasks/` 目录下的同一批 JSON 文件。

## TaskManager：同步的文件 CRUD

`TaskManager` 是 s3 任务系统的核心，但它有意做得很简单。

它没有异步方法，没有 EventBus 依赖，没有状态机。它是纯粹的同步文件 CRUD 层，每次操作就是读一个 JSON 文件、修改后再写回去。

为什么不用数据库？任务数量通常是个位数到十几个，文件 I/O 的开销完全可以忽略。使用文件的好处是：任务的完整历史可以直接用 `ls` 和 `cat` 查看，不需要任何额外工具，调试非常方便。

### ID 用整数，不用 UUID

工具 schema 里的 `task_id` 是 `integer`，调用 `task_update` 时参数类似：

```json
{"task_id": 1}
```

如果使用 UUID，LLM 需要先记住完整的随机字符串，再在下一个工具调用里原样复述，出错概率高得多。整数 ID 在对话历史里不占位置，LLM 几乎不会用错。

每个任务持久化为一个独立 JSON 文件。

任务状态只有三种：

- `pending`：等待；
- `in_progress`：进行中；
- `completed`：完成。

没有 `pause`、`retry`、`cancel`——这些是流程控制逻辑，不是任务本身的状态。Agent 如果需要重试，直接使用工具再做一遍即可。

## `blocked_by` 的自动级联

任务依赖关系用 `blocked_by` 字段表示：

```text
task_2.blocked_by = [1]
```

这意味着任务 2 在等待任务 1 完成后才能开始。

当 LLM 把任务 1 标记为 `completed` 时，`_clear_dependency(1)` 会扫描 `.tasks/` 目录下所有文件，把 `blocked_by` 里含有 `1` 的条目全部移除。不需要 LLM 手动更新依赖关系，任务会自动解锁。

这个设计让 LLM 的操作序列非常自然：创建任务时声明依赖，完成任务时只需更新自己的状态，其他任务的解锁是自动发生的副作用。

## 工具集扩展到 8 个

s3 的 `ToolRegistry` 里注册了 8 个工具。LLM 在每次 plan 阶段都能看到它们全部的 schema，自主决定使用哪些工具、按什么顺序使用。

### 任务工具（4 个）

| 工具 | 作用 |
| --- | --- |
| `task_create` | 创建新任务，可选设置 `blocked_by` 依赖 |
| `task_update` | 更新状态（`pending` / `in_progress` / `completed`）或调整依赖 |
| `task_list` | 列出所有任务的当前状态，返回格式化摘要 |
| `task_get` | 获取单个任务的完整 JSON |

`task_list` 的返回格式是专为 LLM 设计的紧凑表示：

- `[ ]` 表示 `pending`；
- `[>]` 表示 `in_progress`；
- `[x]` 表示 `completed`。

LLM 可以一眼判断哪些任务能够开始、哪些还在等待。

### 执行工具（4 个）

| 工具 | 作用 |
| --- | --- |
| `read_file` | 读取文件内容，最大 512 KB，自动截断 |
| `write_file` | 写文件，自动创建父目录，阻止路径穿越 |
| `list_dir` | 列出目录结构，可控制递归深度 |
| `bash` | 执行 shell 命令，合并 stdout/stderr，输出上限 64 KB |

s1 和 s2 只有 `read_file`。s3 加入 `bash`、`write_file`、`list_dir` 之后，Agent 能做的事情彻底不同了：它可以运行测试、调用命令行工具、写入文件、遍历目录。对于代码分析、文件生成这类任务，这几个工具的组合基本已经够用。

### `bash` 工具的实现

`bash` 使用 `asyncio.create_subprocess_shell` 执行 shell 命令，使用 `asyncio.wait_for` 控制超时。默认超时时间为 60 秒，最大允许 120 秒；超时后调用 `proc.kill()` 强制终止。

非零退出码会返回 `is_error=True` 的 `ToolResult`，让 LLM 知道命令执行失败。

### `bash` 工具的安全边界

当前实现没有沙箱，也没有权限限制。这是 s3 的已知局限，适合本地开发场景；生产部署需要额外的隔离层。

## LLM 拿到任务工具之后

这是整个 s3 最核心的一步，但没有新增 Agent 循环代码——完全依靠 s1 建好的 `AgentLoop + AnthropicProvider` 机制，加上新的工具 schema。

`AgentLoop.run()` 的循环体没有改变。改变的是 `tool_schemas` 的内容：现在包含 `task_create`、`task_update` 等 4 个任务工具的 schema。

LLM 读到这些 schema 后，会根据 system prompt 的指示，以及它自己对“什么时候应该拆解任务”的判断，决定是否在第一步就调用 `task_create`。

一次典型的多任务 run 中，`context.messages` 会不断演变：LLM 创建任务、更新任务状态、调用执行工具、接收工具结果，再决定下一步动作。

### 任务工具调用和普通工具调用完全一致

这是一个重要的设计决策：任务工具调用和普通工具调用在事件流上完全一样。

LLM 调用 `task_create` 产生的是 `tool.call_started` 和 `tool.call_finished` 事件，和调用 `bash` 或 `read_file` 完全相同的格式。

TUI 不需要知道“这是任务操作”——它只看到一个工具调用，显示一个工具调用块。

任务系统是 Agent 的认知行为，不是系统的控制操作。把它暴露成独立的事件类型反而会让 TUI 变得复杂。

## TUI 改版：单列终端滚动流

现在切换到用户视角。`jarvis-tui` 发生了什么变化？

整个界面就是一个 `VerticalScroll` 容器。事件进来时动态追加 widget，并始终自动滚动到底部。用户按 `Ctrl + Q` 退出。

### LLM 流式输出的原地累积

`JarvisTuiApp` 使用 `_handle_event_inner` 路由所有到来的事件。收到 `llm.token` 时，处理过程如下：

`LLMStreamBlock` 是一个 `Static` 子类，在同一个 widget 里累积所有 token。

每到一个 token，就把它追加到 `self._text`，然后用新字符串刷新 widget 显示。流结束时（收到非 token 事件），`finalize_markdown()` 把累积的文本整体渲染成 Markdown，代码块、列表、粗体都能正确显示。

### 为什么不每个 token 追加一个 widget？

如果每个 token 都 `mount` 一个新 widget，一次 LLM 回复可能产生几百个 widget 对象。Textual 的布局引擎需要反复计算整个 widget 树的高度和位置，帧率会显著下降，长输出时会出现明显卡顿。

使用 `update()` 在原地替换内容，布局引擎只需要重绘这一个 widget，代价小得多。

`_current_llm` 是当前“活跃”的 `LLMStreamBlock` 引用。当收到任何非 token 事件时，`_break_llm()` 会调用 `finalize_markdown()`，并把引用置为 `None`。下一个 token 到来时会新建一个 `LLMStreamBlock`，形成新的文字块。这样 LLM 在不同 step 里的思考内容在视觉上是分隔的，不会混在一起。

### 工具调用块的折叠展开

工具调用的显示需要解决一个矛盾：工具调用很频繁，但大多数时候用户只关心它成功了没有，不需要看完整的参数和输出。把所有内容默认展开会淹没 LLM 的思考文字；全部折叠又无法深入了解某次调用的细节。

s3 的解法是：默认折叠，点击展开。

`.detail` 子 widget 默认是 `display: none`——它存在于 DOM 里，但不占空间、不显示。给父 widget 加上 `expanded` 类后，CSS 规则：

```css
.ToolCallBlock.expanded > .detail {
    display: block;
}
```

会立即生效，让 detail 出现。

折叠和展开是纯 CSS 切换，不需要 `mount` / `remove` widget，也不需要重新布局整棵树，只是修改显示属性，因此点击行为很流畅。

工具出错时，摘要行的颜色会变红。即使处于折叠状态，也能看到出了问题，不需要展开细节。

工具输出通过 `ToolCallFinishedEvent.output` 字段传递给 TUI。s3 在这个 event 上新增 `output: str = ""` 字段，`invoke_tool()` 在发布事件之前把 `result.content` 填进去。这样 TUI 能直接从事件里拿到工具输出，不需要回查 `events.jsonl`。

## 本阶段的核心变化

本阶段的核心不是重新发明 AgentLoop，而是把以下能力接到已有的 Agent 循环上：

1. 为每次 run 建立隔离的 `.tasks/` 任务目录；
2. 让 Agent 能通过任务工具自己创建和更新任务；
3. 让任务之间能够表达 `blocked_by` 依赖，并在完成后自动解锁；
4. 增加 `bash`、`write_file`、`list_dir`，让 Agent 能够真正操作项目；
5. 让 TUI 以单列终端滚动流展示 LLM 输出和工具调用。

从用户角度看，命令仍然是：

```bash
jarvis run --goal "..."
```

但 Agent 的内部行为从“根据上下文临时决定下一个工具”变成了“可以先建立任务计划，再按照任务状态和依赖执行”。
