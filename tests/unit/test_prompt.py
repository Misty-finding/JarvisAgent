from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.prompt import build_runtime_system_prompt, finalization_system_prompt


def test_windows_runtime_prompt_names_powershell_and_large_repo_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("jarvis.core.prompt.platform.system", lambda: "Windows")
    monkeypatch.setattr("jarvis.core.prompt.platform.release", lambda: "11")

    prompt = build_runtime_system_prompt(60)

    assert "Operating system: Windows 11" in prompt
    assert "Shell used by the `bash` tool: PowerShell" in prompt
    assert "Never use Unix-only forms" in prompt
    assert "project_map" in prompt
    assert "search_files" in prompt
    assert str(tmp_path) in prompt


def test_finalization_prompt_forbids_more_tools() -> None:
    prompt = finalization_system_prompt("BASE")

    assert prompt.startswith("BASE")
    assert "cannot call tools" in prompt
    assert "Do not claim the task is fully complete" in prompt
