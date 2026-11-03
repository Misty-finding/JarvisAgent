# 会话沙箱与管理面板

所有项目默认使用 CubeSandbox。新会话首次运行时创建一个沙箱，后续消息、Sub-Agent
和恢复后的会话继续使用同一实例及 `/workspace/project` 项目副本，直到用户显式销毁。
空会话不会预占实例。已完成初始化的沙箱不会重新导入项目，避免覆盖会话中的修改。
初始化阶段中断时，恢复会重新导入 Core 已保存的原始副本。

## `/sandbox`

聊天中输入 `/sandbox` 打开管理面板。面板从配置的 Cube 服务读取所有实例，并与 Core
的全局会话索引关联，显示会话标题、会话 ID、项目目录、沙箱 ID 和状态。
当前会话的沙箱放在第一位；没有沙箱时不显示占位实例。
任务运行期间也可以打开面板，普通消息仍需等待当前任务结束。
审批占用输入框时，可用 Ctrl+S 打开同一面板。

每项都有“销毁”按钮。销毁流程是：

1. 如果所属会话正在运行，先请求其 Core 取消任务，等待执行退出。
2. 将项目变更保存到所属会话的 `execution/reviews/<review-id>.json`。
3. 释放本次管理操作的执行进程，再删除 Cube 实例。
4. 更新会话映射和全局索引，广播销毁事件，刷新面板。

保存变更不会自动写回本地项目。需要时可以使用下方 CLI 命令检查并应用保存的 review。
如果连接失败、保存失败或停止未确认，默认保留实例。界面显示原因，允许重试；用户
也可以显式点击“放弃内容并销毁”，跳过保存。未关联 Jarvis 会话的实例只能通过这一
显式操作销毁。会话元数据、聊天记录和已保存的 review 不随实例删除。

跨项目操作优先交给所属项目正在运行的 Core，它负责取消自己的任务和记录事件。
所属 Core 离线时，使用本机可信索引中的映射操作，并取得会话执行锁；无法取得锁时
拒绝销毁，不与运行中的任务抢占工作区。远端实例 metadata 不作为本机文件路径依据。
离线管理使用所属项目的文件排除规则，连接及隔离策略仍由当前 Core 的可信配置决定。
新增排除规则的文件不会被当作已删除文件回写本地。

## 取消任务与实例生命周期

| 操作 | 执行进程 | 沙箱实例及项目文件 |
| --- | --- | --- |
| 一轮任务完成 | 清理本轮残留进程 | 保留 |
| Ctrl+X 取消任务 | 停止本轮主任务与所有 Sub-Agent 进程 | 保留，可继续当前会话 |
| 退出 TUI、关闭会话 | 不触发销毁 | 保留 |
| `/sandbox` 点击销毁 | 先停止所属任务 | 保存变更成功后销毁 |
| 达到实例数量限制 | 新建失败并提示管理已有实例 | 不自动删除旧实例 |

任务进程使用普通用户、`no_new_privs` 和独立的 cgroup v2 分组。Shell、文件工具与
Sub-Agent 都进入同一个任务分组。取消操作先标记任务停止，阻止新操作进入，再发送
终止信号并使用 `cgroup.kill` 清理包括脱离 Shell 会话的后代进程，检查分组为空后才
报告成功。管理进程在分组外运行，不把 Cube 凭证传入任务环境。
镜像不支持这一机制时初始化失败，禁止退回本机执行。
恢复连接过程中取消任务，也会停止上轮遗留进程并保留已完成导入的状态；再次恢复
继续使用沙箱中的修改，不重新导入原始项目覆盖它们。

默认 `sandbox_timeout = -1` 使用 Cube 原生的“不自动到期”，不依靠客户端持续续租。
它表示没有自动过期策略；主机、WSL 或 Cube 服务故障仍可能使实例不可用。
恢复时验证原实例存在。意外丢失或连接失败时报告错误，不自动替换。
用户通过管理面板或 CLI 显式销毁成功后，下次发送消息会为同一会话创建新沙箱，
按当前导入规则重新复制本地项目。聊天历史保留；旧实例的文件、依赖和后台进程不继承。
创建及使用事件继续广播到 EventBus、trace、thread.jsonl 和 IPC。
旧变更 review 及其基线单独保留，仍可检查、确认后应用到本地；重建不会自动应用它们。
仅查看列表、状态或变更不会触发重建。销毁未确认成功时仍禁止替换。

## 全局配置

以下连接及安全策略放在用户的 `~/.jarvis/config.toml`：

```toml
[execution]
backend = "cubesandbox"
connection_file = "D:/CubeSandbox/client/connection.env"
sandbox_timeout = -1
max_running_sandboxes = 3
registry_dir = "~/.jarvis/sandboxes"
allow_internet_access = false
```

`connection.env` 留在 Core，只存连接所需凭证，不提交 Git。项目配置只能选择
`include_uncommitted`、`exclude` 和文件数量/大小限制；项目排除规则与全局规则合并，
大小限制只能收紧。项目 `.env` 不能改变后端、连接文件、全局索引或配置文件位置。
本机执行必须由用户在全局配置或 Core 系统环境中显式启用；已绑定沙箱的会话仍禁止
切换成本机执行。沙箱服务不可用时直接报错。

全局索引只保存实例与会话的对应关系，凭证留在各会话的 Core 私有状态文件中。
默认同一服务最多运行三个实例，包含未关联会话的实例；达到上限时请打开面板清理。

## CLI 与 IPC

```powershell
jarvis sandbox list
jarvis sandbox status --session sess-xxx
jarvis sandbox changes --session sess-xxx
jarvis sandbox destroy --id <sandbox-id>
# 明确放弃尚未保存的内容：
jarvis sandbox destroy --id <sandbox-id> --discard-changes
# 检查已保存的 review，再明确应用；销毁后仍可使用：
jarvis sandbox changes --session sess-xxx --review <review-id>
jarvis sandbox apply --session sess-xxx --review <review-id> --confirm <review-id>
```

`sandbox.list` 接受 `current_session_id`，返回脱敏后的列表；`sandbox.manage` 的
`destroy` 接受沙箱 ID 或会话 ID。旧 CLI `stop` 是显式销毁的兼容别名。
这些管理命令属于用户控制面，不向模型提供销毁工具。

`sandbox.starting/created/ready/reused/using/released/stopping/destroyed/failed` 以及
`sandbox.cancel_requested/cancelled/cancel_failed` 都通过同一 EventBus 广播，进入
trace、执行事件日志、所属会话的 `thread.jsonl` 和 IPC 的 `sandbox.*` 订阅。
事件包含会话、运行和沙箱 ID，以及项目归属；不包含凭证和项目文件内容。
