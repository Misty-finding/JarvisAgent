from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from jarvis.core.execution.locking import execution_lock
from jarvis.core.execution.project import (
    apply_review,
    collect_project,
    load_object,
    make_review,
    portable_path,
    save_private,
    validate_export,
)
from jarvis.core.execution.settings import ExecutionConfig, parse_execution


def item(path: str, content: str) -> dict[str, object]:
    return {
        "path": path,
        "content": base64.b64encode(content.encode()).decode(),
        "executable": False,
    }


def setup_copy(tmp_path: Path) -> tuple[Path, Path, ExecutionConfig]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "modify.py").write_bytes(b"old\n")
    (root / "delete.py").write_bytes(b"delete\n")
    (root / "unchanged.py").write_bytes(b"keep\n")
    state = tmp_path / "core-state"
    config = ExecutionConfig()
    save_private(state / "baseline.json", collect_project(root, config))
    return root, state, config


def test_review_apply_add_modify_delete_preserves_unrelated_edit(tmp_path: Path) -> None:
    root, state, config = setup_copy(tmp_path)
    (root / "unchanged.py").write_text("user's concurrent edit\n")
    review = make_review(
        state,
        [item("modify.py", "new\n"), item("add.py", "added\n"), item("unchanged.py", "keep\n")],
        config,
    )
    assert {change["kind"] for change in review["changes"]} == {"add", "modify", "delete"}
    assert (root / "modify.py").read_text() == "old\n"  # review itself has no project effects
    result = apply_review(state, review["id"], config)
    assert set(result["applied"]) == {"add.py", "modify.py", "delete.py"}
    assert (root / "modify.py").read_text() == "new\n"
    assert not (root / "delete.py").exists()
    assert (root / "unchanged.py").read_text() == "user's concurrent edit\n"
    with pytest.raises(ValueError, match="stale|already"):
        apply_review(state, review["id"], config)


def test_edit_after_review_aborts_entire_apply(tmp_path: Path) -> None:
    root, state, config = setup_copy(tmp_path)
    review = make_review(
        state,
        [
            item("modify.py", "sandbox\n"),
            item("delete.py", "delete\n"),
            item("unchanged.py", "keep\n"),
            item("add.py", "new\n"),
        ],
        config,
    )
    (root / "modify.py").write_text("user edit\n")
    with pytest.raises(ValueError, match="conflict"):
        apply_review(state, review["id"], config)
    assert not (root / "add.py").exists()
    assert (root / "modify.py").read_text() == "user edit\n"


def test_git_only_selected_worktree_changes_are_imported(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    (tmp_path / "code.py").write_text("committed\n")
    (tmp_path / ".env").write_text("API_KEY=must-not-leak")
    git("add", ".")
    git(
        "-c",
        "user.name=Acceptance",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    (tmp_path / "code.py").write_text("dirty\n")
    (tmp_path / "new.py").write_text("new\n")
    state = tmp_path / ".jarvis" / "execution"
    baseline = collect_project(tmp_path, ExecutionConfig())
    assert len(baseline["files"]) == 1
    assert base64.b64decode(baseline["files"][0]["content"]) == b"committed\n"
    save_private(state / "baseline.json", baseline)
    review = make_review(state, [item("code.py", "sandbox\n")], ExecutionConfig())
    assert review["changes"][0]["conflict"]  # unselected user edits cannot be overwritten
    selected = collect_project(tmp_path, ExecutionConfig(include_uncommitted=["*.py"]))
    assert {entry["path"] for entry in selected["files"]} == {"code.py", "new.py"}
    assert base64.b64decode(selected["files"][0]["content"]) == (tmp_path / "code.py").read_bytes()


@pytest.mark.parametrize(
    "path", ["../evil", "/absolute", "C:/x", "a\\b", "a:stream", "CON.txt", "a/../b", "trailing."]
)
def test_reject_untrusted_linux_export_names(path: str) -> None:
    with pytest.raises(ValueError):
        portable_path(path)


def test_export_collision_secret_and_size_limits() -> None:
    config = ExecutionConfig(max_file_bytes=10)
    with pytest.raises(ValueError, match="size"):
        validate_export([item("file.py", "a" * 11)], config)
    with pytest.raises(ValueError, match="colliding"):
        validate_export([item("A.py", "a"), item("a.py", "b")], config)
    assert validate_export([item(".env", "secret")], config) == {}
    with pytest.raises(ValueError, match="secret"):
        validate_export([item("code.py", 'api_key="abcdefghijklmnop12345678"')], ExecutionConfig())


def test_new_exclusion_is_not_interpreted_as_remote_deletion(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("original\n")
    state = tmp_path / ".jarvis" / "execution"
    save_private(state / "baseline.json", collect_project(tmp_path, ExecutionConfig()))
    config = ExecutionConfig(exclude=["code.py"])
    review = make_review(state, [item("code.py", "remote edit\n")], config)
    assert review["changes"] == [], "Excluded baseline files must never become deletion candidates"


def test_import_excludes_core_data_and_secrets(tmp_path: Path) -> None:
    (tmp_path / ".jarvis").mkdir()
    (tmp_path / ".jarvis" / "session.json").write_text("private session")
    for directory in ("runs", ".tasks"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "trace.json").write_text("private runtime data")
    (tmp_path / "id_ed25519").write_text("private key")
    (tmp_path / "secret.py").write_text('api_key="abcdefghijklmnop12345678"')
    (tmp_path / "safe.py").write_text("print('hello')")
    baseline = collect_project(tmp_path, ExecutionConfig())
    assert [entry["path"] for entry in baseline["files"]] == ["safe.py"]


def test_cross_process_style_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    with execution_lock(tmp_path):
        with pytest.raises(RuntimeError, match="busy"):
            with execution_lock(tmp_path):
                pytest.fail("second lock acquired")
    with execution_lock(tmp_path):
        pass


def test_execution_config_fails_closed() -> None:
    for value in (
        {"backend": "cube-typo"},
        {"allow_internet_access": "false"},
        {"max_files": -1},
        {"unknown": True},
        {"allow_out": [1]},
    ):
        with pytest.raises(ValueError):
            parse_execution(value)


def test_review_content_tampering_is_rejected(tmp_path: Path) -> None:
    root, state, config = setup_copy(tmp_path)
    review = make_review(
        state,
        [item("modify.py", "new\n"), item("delete.py", "delete\n"), item("unchanged.py", "keep\n")],
        config,
    )
    path = state / "reviews" / (review["id"] + ".json")
    modified = load_object(path)
    modified["changes"][0]["file"]["content"] = base64.b64encode(b"tampered").decode()
    save_private(path, modified)
    with pytest.raises(ValueError, match="content changed"):
        apply_review(state, review["id"], config)
    assert (root / "modify.py").read_text() == "old\n"
