from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.skills.loader import Skill, SkillLoader


# 功能：内建 review skill 应能被 SkillLoader 查找到
# 设计：直接调用 resolve("review")，不依赖文件系统之外的任何状态
def test_builtin_skill_found() -> None:
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.name == "review"
    assert "审查" in skill.description or "review" in skill.description.lower()
    assert skill.system_prompt_template != ""


# 功能：内建 init / summarize / orchestrate skill 均可找到
# 设计：列举所有内建 skill 名，断言均能解析
@pytest.mark.parametrize("name", ["init", "review", "summarize", "orchestrate"])
def test_all_builtin_skills_found(name: str) -> None:
    loader = SkillLoader()
    skill = loader.resolve(name)
    assert skill is not None, f"builtin skill '{name}' not found"


# 功能：不存在的 skill 名应返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常
def test_unknown_skill_returns_none() -> None:
    loader = SkillLoader()
    result = loader.resolve("nonexistent_skill_xyz")
    assert result is None


# 功能：render_prompt 应将 $ARGUMENTS 替换为传入的参数字符串
# 设计：构造含 $ARGUMENTS 的 skill，验证 render_prompt 结果不含 "$ARGUMENTS" 且含参数值
def test_arguments_substituted() -> None:
    loader = SkillLoader()
    skill = Skill(
        name="test",
        description="test skill",
        system_prompt_template="Review this: $ARGUMENTS\nPlease be thorough.",
        allowed_tools=[],
    )
    rendered = loader.render_prompt(skill, "src/foo.py")
    assert "$ARGUMENTS" not in rendered
    assert "src/foo.py" in rendered


# 功能：frontmatter 中的 allowed_tools 列表应被正确解析
# 设计：构造含 allowed_tools 的 Markdown 文件，通过 _parse_skill_file 解析并验证结果
def test_frontmatter_parsed(tmp_path: Path) -> None:
    from jarvis.core.skills.loader import _parse_skill_file

    content = """\
---
name: custom
description: 自定义 skill 测试
allowed_tools:
  - read_file
  - bash
---
你是一个测试助手，目标：$ARGUMENTS
"""
    p = tmp_path / "custom.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "custom"
    assert skill.description == "自定义 skill 测试"
    assert "read_file" in skill.allowed_tools
    assert "bash" in skill.allowed_tools
    assert "$ARGUMENTS" in skill.system_prompt_template


# 功能：无 frontmatter 的 Markdown 文件仍可加载，allowed_tools 为空列表
# 设计：写入纯正文 Markdown，断言解析成功且 allowed_tools=[]
def test_no_frontmatter(tmp_path: Path) -> None:
    from jarvis.core.skills.loader import _parse_skill_file

    content = "你是助手，请帮助用户完成任务：$ARGUMENTS\n"
    p = tmp_path / "plain.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "plain"
    assert skill.allowed_tools == []
    assert "你是助手" in skill.system_prompt_template


# 功能：项目本地 skill 应覆盖内建同名 skill
# 设计：在 .jarvis/skills/ 中写入同名文件，用 monkeypatch 修改 cwd，断言加载到的是本地版本
def test_project_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_skills = tmp_path / ".jarvis" / "skills"
    local_skills.mkdir(parents=True)
    (local_skills / "review.md").write_text(
        "---\nname: review\ndescription: local override\n---\nlocal system prompt $ARGUMENTS\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.description == "local override"
    assert "local system prompt" in skill.system_prompt_template


# 功能：从其他当前目录也能解析显式工作区的项目 Skill
# 设计：将调用目录和项目目录分开，防止后台 Core 错用进程当前路径
def test_explicit_workspace_finds_project_skill_from_another_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    caller = tmp_path / "caller"
    local_skills = project / ".jarvis" / "skills"
    local_skills.mkdir(parents=True)
    caller.mkdir()
    (local_skills / "review.md").write_text(
        "---\nname: review\ndescription: workspace skill\n---\nworkspace prompt\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(caller)
    skill = SkillLoader(workspace=project).resolve("review")

    assert skill is not None
    assert skill.description == "workspace skill"


# 功能：标准 YAML 的折叠标量、引号、元数据和工具声明均能保留
# 设计：同时使用 CRLF、BOM 和含冒号字符串，覆盖原手写解析器容易误读的真实格式
def test_standard_frontmatter_and_resources(tmp_path: Path) -> None:
    from jarvis.core.skills.loader import _parse_skill_file

    root = tmp_path / "standard-skill"
    (root / "references").mkdir(parents=True)
    (root / "references" / "guide.md").write_text("private-reference-content", encoding="utf-8")
    path = root / "SKILL.md"
    path.write_bytes(("\ufeff---\nname: standard-skill\ndescription: >-\n"
                      "  Review code: safely\n  and thoroughly.\nlicense: MIT\n"
                      "compatibility: 'Requires Python: 3.12'\n"
                      "allowed-tools: Bash(git:*) Bash(jq:*) Read\n"
                      "metadata:\n  author: 'someone: else'\n  version: '1.0'\n"
                      "extra-client-field:\n  - not-a-tool\n---\n"
                      "Read references/guide.md before reviewing.\n").replace("\n", "\r\n").encode())
    skill = _parse_skill_file(path)
    assert skill.description == "Review code: safely and thoroughly."
    assert skill.metadata == {"author": "someone: else", "version": "1.0"}
    assert skill.allowed_tools == ["Bash(git:*)", "Bash(jq:*)", "Read"]
    assert skill.tool_whitelist is None
    assert skill.license == "MIT"
    assert skill.source_path == path.resolve()
    assert skill.resolve_resource("references/guide.md").read_text() == "private-reference-content"
    rendered = SkillLoader(tmp_path).render_prompt(skill, "fix asyncio cancellation")
    assert "fix asyncio cancellation" in rendered  # 上游无需包含 $ARGUMENTS
    assert str(root.resolve()) in rendered
    assert "references/guide.md" in rendered
    assert "private-reference-content" not in rendered
    assert "Requires Python: 3.12" in rendered
    with pytest.raises(ValueError, match="inside"):
        skill.resolve_resource("../../outside.txt")


# 功能：错误或不安全 YAML 与非法标准名称不会出现在目录中
# 设计：参数化格式边界并保留同目录有效 Skill，验证错误隔离而不是整个目录加载失败
@pytest.mark.parametrize("frontmatter", [
    "description: missing name", "name: broken", "name: other\ndescription: mismatch",
    "name: broken\ndescription: ''", "name: broken\ndescription: [not, a, string]",
    "name: broken\ndescription: yes", "name: broken\ndescription: test\nmetadata: [bad]",
    "name: broken\ndescription: !!python/object/apply:os.system ['exit 1']",
    "name: broken\ndescription: [unterminated", "- not\n- a mapping",
])
def test_invalid_standard_skill_is_isolated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, frontmatter: str,
) -> None:
    root = tmp_path / ".agents" / "skills" / "broken"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody", encoding="utf-8")
    loader = SkillLoader(tmp_path)
    assert loader.resolve("broken") is None
    assert loader.resolve("review") is not None
    assert "Skipping invalid skill" in caplog.text


# 功能：标准目录和旧目录按项目优先级覆盖，旧文件的列表不会误作工具声明
# 设计：隔离全局路径并逐层添加同名 Skill，验证补全列表和 resolve 使用同一解析规则
def test_standard_search_precedence_and_legacy_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    global_dir = tmp_path / "global" / ".agents" / "skills"
    standard_dir = project / ".agents" / "skills"
    legacy_dir = project / ".jarvis" / "skills"
    directories = [global_dir, standard_dir, legacy_dir]
    monkeypatch.setattr(SkillLoader, "_directories", lambda _: directories)
    loader = SkillLoader(project)
    for index, directory in enumerate(directories):
        skill_dir = directory / "shared"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: shared\ndescription: source {index}\n"
            "metadata:\n  origin: test\nother:\n  - not-a-tool\n---\nbody", encoding="utf-8",
        )
        skill = loader.resolve("shared")
        assert skill is not None and skill.description == f"source {index}"
        assert skill.allowed_tools == []
        assert loader.list_all() == ["shared"]
    assert loader.resolve("../shared") is None
    legacy = legacy_dir / "alias.md"
    legacy.write_text("---\nname: declared\ndescription: old\n"
                      "allowed_tools: [read_file, bash]\n---\nbody", encoding="utf-8")
    skill = loader.resolve("declared")
    assert skill is not None and skill.tool_whitelist == ["read_file", "bash"]
    assert "alias" not in loader.list_all()
