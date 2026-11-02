# Jarvis 编码 Skill 库

30 个标准 Agent Skills，保持上游 `SKILL.md` 与引用资源原样。
每个目录包含 `LICENSE` 和 `UPSTREAM.json`；`skills.lock.json` 记录固定提交和文件 SHA-256。
这些是可加载的任务指南；依赖的软件工具仍需在具体任务环境中具备。

| 方向 | Skill |
| --- | --- |
| Java / Spring | java-coding-standards、springboot-patterns、springboot-security、springboot-tdd、springboot-verification、jpa-patterns、mybatis-patterns |
| 数据库与缓存 | mysql-patterns、redis-patterns、database-migrations、database-optimizer |
| Python | python-patterns、python-testing、fastapi-patterns、python-pro |
| 后端与工程 | api-design、backend-patterns、docker-patterns、microservices-architect |
| 调试、审查与测试 | debugging-wizard、code-reviewer、test-master、security-reviewer、playwright-expert |
| 前端 | react-expert、typescript-pro、nextjs-developer |
| Agent 开发 | mcp-developer、rag-architect、prompt-engineer |

上游来源（均为 MIT）：

- [affaan-m/ECC](https://github.com/affaan-m/ECC/tree/90606c8315ee09cafc303515c5f33024db6b9882/skills)，16 个。
- [Jeffallan/claude-skills](https://github.com/Jeffallan/claude-skills/tree/02b05028ba6c93f7aed45314b15c0dd5abead9d6/skills)，14 个。

从项目根目录运行：

```powershell
uv run python scripts/install_coding_skills.py --check
uv run python scripts/install_coding_skills.py
```

第二条命令复制完整目录到 `~/.agents/skills/`，使其他工作区也能发现这些 Skill。
同版本重复安装会跳过，已存在的自定义版本会报冲突并保持原样。

TUI 中输入 `/` 筛选，示例：

```text
/python-pro 检查 src/jarvis 的异步取消和异常处理
/springboot-tdd 为订单幂等逻辑设计回归测试
/code-reviewer 审查当前修改
/mcp-developer 分析 MCP 工具接入
```

也可直接描述任务，由当前模型通过 `activate_skill` 选择相关指南；尚未接入 Jev 路由。
名称和描述在目录层披露，正文在激活时加载，引用文件按需读取。
Skill 中示例代码引用的工程文件（例如 Docker 初始化 SQL）由任务项目提供，不属于缺失资源。
