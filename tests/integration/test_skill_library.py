from __future__ import annotations

import subprocess
import sys
from pathlib import Path


# 功能：固定 Skill 库能完整离线安装、重复跳过且拒绝覆盖用户修改
# 设计：通过公开脚本运行真实目录复制和锁校验，覆盖 30 个实际第三方包的集成而非手造占位 Skill
def test_coding_library_install_is_complete_and_non_destructive(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    script = project / "scripts" / "install_coding_skills.py"
    check = subprocess.run([sys.executable, str(script), "--check"], cwd=project,
                           capture_output=True, text=True, check=True)
    assert "Validated 30" in check.stdout
    command = [sys.executable, str(script), "--dest", str(tmp_path / "skills")]
    first = subprocess.run(command, cwd=project, capture_output=True, text=True, check=True)
    assert "Installed 30" in first.stdout
    installed = list((tmp_path / "skills").glob("*/SKILL.md"))
    assert len(installed) == 30
    assert all((path.parent / "UPSTREAM.json").exists() for path in installed)
    assert all((path.parent / "LICENSE").exists() for path in installed)
    reference = tmp_path / "skills" / "python-pro" / "references" / "async-patterns.md"
    assert reference.exists()
    second = subprocess.run(command, cwd=project, capture_output=True, text=True, check=True)
    assert "Installed 0; identical versions skipped 30" in second.stdout
    reference.write_text("USER-CUSTOMIZED-REFERENCE", encoding="utf-8")
    conflicting = subprocess.run(command, cwd=project, capture_output=True, text=True)
    assert conflicting.returncode != 0
    assert "refusing to overwrite" in conflicting.stderr
    assert reference.read_text() == "USER-CUSTOMIZED-REFERENCE"
