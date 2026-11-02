# Jarvis Skills 使用与扩展指南

## 标准格式

Jarvis 支持 [Agent Skills](https://agentskills.io/specification) 的 YAML frontmatter、
Markdown 正文和完整资源目录。推荐结构：

```text
.agents/skills/my-skill/
  SKILL.md
  references/guide.md
  scripts/check.py
  assets/template.json
```

`SKILL.md` 示例：

```markdown
---
name: my-skill
description: 检查 Python 异步代码的取消、异常传播和资源释放。用于 asyncio 审查。
license: MIT
compatibility: Requires Python 3.12
metadata:
  author: my-team
  version: "1.0"
allowed-tools: Read Grep
---

先阅读 references/guide.md，再检查用户指定代码并运行必要验证。
```

目录式标准 Skill 要求 `name` 和目录名一致，使用 1–64 位小写字母、数字及单个连字符；
`description` 非空且最多 1024 字符。`compatibility` 最多 500 字符；`metadata` 是字符串映射。
完整 YAML 解析支持引号、折叠和保留换行的块标量、嵌套扩展字段；未知字段保存但不自动执行。
格式错误的 Skill 会被跳过并记入 Core 日志，其他 Skill 继续可用。

## 发现与覆盖

同名 Skill 的优先级从高到低：

1. `<workspace>/.jarvis/skills/`
2. `<workspace>/.agents/skills/`
3. `~/.jarvis/skills/`
4. `~/.agents/skills/`
5. Jarvis 安装包内的 `src/jarvis/core/skills/builtin/`

项目范围高于用户范围，同范围 Jarvis 专用目录高于通用目录。
项目目录绑定当前工作区，不受后台 Core 的启动目录影响。
`.agents/skills/` 只发现子目录内的 `SKILL.md`，忽略 README 和锁文件。

保留旧 `.jarvis/skills/name.md` 和 `.jarvis/skills/name/SKILL.md`。
旧扁平文件可省略 frontmatter，旧 `allowed_tools` 列表继续作为工具白名单。
标准目录式文件要求有效 frontmatter。

## 激活与渐进加载

TUI 输入 `/` 时显示名称和描述，可以继续输入名称片段筛选。
上下键导航，Tab/Enter 补全，输入参数后发送：

```text
/python-pro 检查 src/jarvis 的异步实现
/code-reviewer 审查当前 Git diff
/springboot-tdd 给订单去重逻辑补回归测试
```

补全最多显示 8 条，光标移动时滚动并显示总数；每次重新打开补全时刷新目录。

普通文本任务也可使用 Skill：`activate_skill` 的工具说明包含可用名称和描述，
当前模型根据任务选择并调用，工具返回完整正文。没有将全部 Skill 正文塞入上下文。
这是当前模型的选择能力，尚未接入 Jev。

旧 `allowed_tools` 限制型 Skill 仅通过斜杠调用，以维持原有工具限制语义。
扩展 `disable-model-invocation: true` 可以将 Skill 从模型目录中隐藏，仍允许用户手动调用。

## 引用资源和宿主适配

两种激活方式都附带 Skill 的绝对根目录、兼容要求和资源路径索引。
`references/`、`scripts/`、`assets/` 的内容不会被提前读取。
相对路径以 Skill 根目录为基准，调用 `read_file` 时使用完整绝对路径。
脚本可通过 `bash` 调用；项目构建仍在项目目录运行，读取 Skill 不会自动执行脚本。

第三方 Skill 不必包含 Jarvis 旧 `$ARGUMENTS` 占位符。
调用参数会单独注入，旧占位符仍支持替换。
Jarvis 基础运行规则、项目上下文和用户要求继续生效。

工具名称参考：

| 上游名称 | Jarvis 工具 |
| --- | --- |
| Read | read_file |
| Grep / Glob | search_files |
| Bash | bash，Windows 下为原生 PowerShell |
| Write | write_file |
| Edit | read_file 后 write_file |
| Skill | activate_skill |
| Task | spawn_agent |

此表是模型适配指导；只能调用当前实际暴露的工具，不会凭空创建第三方工具。
标准实验字段 `allowed-tools` 表示预批准工具声明，Jarvis 解析和保存该字段并提供给模型，
但不根据第三方文件免除现有权限审批，也不实现 Claude 的命令模式授权语法。
例如 `Bash(git:*)` 不会被扩张为任意 Shell 命令的自动授权。

## 首批 30 个编码 Skill

源码仓库的 `.agents/skills/` 已包含 30 个，详细名单见该目录的 README。
目录保留上游正文和完整资源，并附 MIT 许可证、固定提交、逐文件 SHA-256 锁定信息。

```powershell
uv run python scripts/install_coding_skills.py --check
uv run python scripts/install_coding_skills.py
uv run python scripts/install_coding_skills.py --dest 'D:\work\demo\.agents\skills'
```

安装器从本仓库复制完整目录，无需联网，不安装上游仓库的 hooks、插件或其他配置。
复制前校验锁文件；相同版本跳过，修改过的目标目录报冲突并保留原内容。
全局安装后其他工作区也可发现，新增 Skill 在重新打开 `/` 列表或下次 run 时生效。
升级 Jarvis 加载器代码则需要重启 Core 和 TUI。
