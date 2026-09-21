# Jarvis Skills 使用与扩展指南

## 1. 输入 `/` 时发生了什么

TUI 输入框检测到 `/` 前缀后，会构造一个绑定当前 workspace 的 `SkillLoader`，
读取所有可用 Skill 的名称和 description，然后把它们显示在补全列表中。

这里只是显示候选项，不会因为输入 `/` 就执行 Skill。只有发送类似下面的消息后，
Core 才会真正解析并执行：

```text
/review src/jarvis
/summarize
/my-skill 参数内容
```

## 2. Skill 搜索位置和优先级

Jarvis 会按照下面的优先级解析同名 Skill：

```text
项目特定 Skill
    ↓
用户通用 Skill
    ↓
Jarvis 内建 Skill
```

实际路径分别是：

```text
目标项目/.jarvis/skills/
C:\Users\23148\.jarvis\skills\
Jarvis 内建目录
```

项目目录不是 Jarvis 安装目录，而是你执行 `jarvis` 时所在的目标项目目录。
例如从 `D:\projects\demo` 启动：

```powershell
cd D:\projects\demo
jarvis
```

项目 Skill 就从下面的位置读取：

```text
D:\projects\demo\.jarvis\skills\
```

即使 Core 由 Jarvis 在后台启动，SkillLoader 也会使用这个 workspace，
不会因为 daemon 的启动方式改变而跑去错误目录搜索。

## 3. Jarvis 内建 Skills 是什么

Jarvis 内建 Skills 是随 Jarvis 源码或安装包一起提供的默认 Skill，位于源码中的：

```text
src/jarvis/core/skills/builtin/
```

目前包括：

```text
init.md
review.md
summarize.md
orchestrate.md
```

它们的作用是让 Jarvis 开箱即用，例如：

```text
/init
/review
/summarize
/orchestrate
```

内建 Skill 属于产品默认能力，升级 Jarvis 时可能随代码一起变化。
通常不建议直接修改内建文件；如果想定制同名 Skill，应该在用户目录或项目目录
创建同名文件，用更高优先级覆盖默认版本。

## 4. 通用 Skill

通用 Skill 放在：

```text
C:\Users\23148\.jarvis\skills\
```

它会对所有项目生效，适合放跨项目通用的工作流，例如：

```text
C:\Users\23148\.jarvis\skills\python-review.md
C:\Users\23148\.jarvis\skills\release-check\SKILL.md
```

### 4.1 扁平文件格式

创建 `C:\Users\23148\.jarvis\skills\python-review.md`：

```markdown
---
name: python-review
description: 检查 Python 代码质量、测试覆盖率和常见工程问题
allowed_tools:
  - project_map
  - search_files
  - read_file
  - list_dir
---

请审查当前 Python 项目：

1. 先使用 project_map 了解项目结构。
2. 再使用 search_files 定位入口、配置和测试。
3. 重点检查异常处理、类型标注、异步边界和测试覆盖率。
4. 最后按严重程度输出问题和建议。

用户补充要求：
$ARGUMENTS
```

重启 TUI，或重新打开 `/` 补全列表后，就会看到：

```text
/python-review
```

调用示例：

```text
/python-review src/jarvis
```

### 4.2 目录式格式

复杂 Skill 可以使用目录：

```text
C:\Users\23148\.jarvis\skills\release-check\SKILL.md
```

目录名默认就是 Skill 名，也可以在 frontmatter 中用 `name:` 明确指定。

## 5. 项目特定 Skill

项目特定 Skill 放在目标项目自己的：

```text
目标项目/.jarvis/skills/
```

例如：

```text
D:\projects\demo\.jarvis\skills\demo-release.md
```

这种 Skill 只对 `D:\projects\demo` 生效，不会污染其他项目。
适合放：

- 当前项目的目录结构和模块约定
- 项目专用测试、构建、部署流程
- 当前团队的代码审查标准
- 项目中的领域术语和业务规则

项目 Skill 可以覆盖同名通用 Skill。例如全局有：

```text
C:\Users\23148\.jarvis\skills\review.md
```

项目中也有：

```text
D:\projects\demo\.jarvis\skills\review.md
```

在 `D:\projects\demo` 中执行 `/review` 时，Jarvis 会优先使用项目版本。

## 6. `allowed_tools` 的作用

`allowed_tools` 是这个 Skill 允许模型使用的工具白名单。例如：

```yaml
allowed_tools:
  - project_map
  - search_files
  - read_file
```

如果不写 `allowed_tools` 或列表为空，当前实现不会因为 Skill 本身额外限制工具，
模型仍使用当前 Agent 的完整工具注册表和权限策略。

常用工具包括：

```text
project_map
search_files
read_file
list_dir
bash
write_file
task_create
task_update
```

Skill 的工具白名单不是权限系统的替代品。文件写入、Shell 等高风险操作仍然受
Jarvis 的 PermissionManager 和当前 ask/auto 模式控制。

## 7. 添加 Skill 的最短流程

### 通用 Skill

```powershell
New-Item -ItemType Directory -Force "$HOME\.jarvis\skills" | Out-Null
notepad "$HOME\.jarvis\skills\my-skill.md"
```

### 项目 Skill

```powershell
New-Item -ItemType Directory -Force ".jarvis\skills" | Out-Null
notepad ".jarvis\skills\my-skill.md"
```

保存后重新打开 TUI，输入 `/` 即可查看候选项。
`SkillLoader` 会读取 frontmatter 中的 `name` 和 `description`，正文会作为
该 Skill 的专用系统指令，`$ARGUMENTS` 会被替换成 `/my-skill` 后面的参数。
