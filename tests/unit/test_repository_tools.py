from __future__ import annotations

from pathlib import Path

from jarvis.core.tools.builtin.project_map import ProjectMapTool
from jarvis.core.tools.builtin.search_files import SearchFilesTool


async def test_project_map_reports_modules_and_skips_generated_dirs(
    tmp_path: Path,
) -> None:
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "service" / "App.java").write_text("class App {}", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "noise.js").write_text("noise", encoding="utf-8")

    result = await ProjectMapTool().invoke({"path": str(tmp_path)})

    assert not result.is_error
    assert "service" in result.content
    assert "service/pom.xml" in result.content
    assert "node_modules" not in result.content
    assert ".java=1" in result.content


async def test_search_files_returns_paths_and_line_numbers(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "api.py").write_text(
        "def query_order():\n    return 'ok'\n",
        encoding="utf-8",
    )
    (source / "other.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = await SearchFilesTool().invoke({
        "pattern": "query_order",
        "path": str(tmp_path),
        "glob": "*.py",
    })

    assert not result.is_error
    assert "api.py" in result.content
    assert ":1:" in result.content
    assert "query_order" in result.content
