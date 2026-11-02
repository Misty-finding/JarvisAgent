from __future__ import annotations

import base64
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from jarvis.core.execution.settings import ExecutionConfig

EXCLUDED_DIRS = {
    ".git",
    ".jarvis",
    ".agents",
    ".claude",
    ".codex",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    "logs",
    "runtime-logs",
    "runs",
    ".tasks",
}
SECRET_NAMES = {
    "credentials",
    "credentials.json",
    "secrets.json",
    "secrets.toml",
    "connection.env",
    "config.toml",
    "id_rsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
}
SECRET_CONTENT = re.compile(
    rb"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----|"
    rb"(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)\s*[=:]\s*[\"']?"
    rb"[A-Za-z0-9_./+\-=]{16,}",
    re.IGNORECASE,
)


def digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def portable_path(value: str) -> str:
    """Validate untrusted Linux names before mapping them into a Windows project."""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or path.as_posix() != value:
        raise ValueError("Non-canonical project path")
    reserved = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)
    for part in path.parts:
        if (
            part in (".", "..")
            or part.endswith((".", " "))
            or reserved.match(part)
            or any(c in part for c in '\\:<>"|?*')
            or any(ord(c) < 32 for c in part)
        ):
            raise ValueError("Unsupported project path: " + value)
    return value


def excluded(value: str, patterns: list[str]) -> bool:
    path = PurePosixPath(value)
    name = path.name.lower()
    return (
        any(part.lower() in EXCLUDED_DIRS for part in path.parts)
        or name in SECRET_NAMES
        or name.startswith(".env")
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"))
        or any(fnmatch.fnmatch(value, pattern) for pattern in patterns)
    )


def local_path(root: Path, relative: str) -> Path:
    portable_path(relative)
    path = root / relative
    for item in (path, *path.parents):
        if item == root.parent:
            break
        if item.is_symlink() or item.is_junction():
            raise ValueError("Project links/reparse points are not supported: " + relative)
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Project reparse point is not supported: " + relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes project")
    return path


def read_local(root: Path, relative: str, max_bytes: int | None = None) -> bytes | None:
    path = local_path(root, relative)
    if path.exists() and not path.is_file():
        raise ValueError("Expected a regular file: " + relative)
    if path.exists() and max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError("Project file exceeds size limit: " + relative)
    return path.read_bytes() if path.exists() else None


def save_private(path: Path, value: Any) -> None:
    """Persist Core-only state atomically; task processes cannot see this directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid execution state")
    return value


def _git_run(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    # The project may contain executables or Git config. Do not execute its git.exe,
    # fsmonitor, diff drivers, or hooks; do not give this helper model credentials.
    trusted_paths = [
        value
        for value in os.environ.get("PATH", "").split(os.pathsep)
        if value and Path(value).is_absolute() and not Path(value).resolve().is_relative_to(root)
    ]
    executable = shutil.which("git", mode=os.F_OK, path=os.pathsep.join(trusted_paths))
    if executable is None:
        return subprocess.CompletedProcess(list(args), 128, b"", b"Git is unavailable")
    env = {
        name: os.environ[name]
        for name in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "SYSTEMDRIVE",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
        )
        if name in os.environ
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return subprocess.run(  # noqa: S603 -- fixed executable, argv only, read-only Git operations
        [
            executable,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "safe.directory=" + root.as_posix(),
            "-C",
            str(root),
            *args,
        ],
        capture_output=True,
        check=False,
        env=env,
        timeout=30,
    )


def _git(root: Path, *args: str) -> bytes:
    result = _git_run(root, *args)
    if result.returncode:
        raise ValueError("Cannot read Git project state")
    return result.stdout


def collect_project(root: Path, config: ExecutionConfig) -> dict[str, Any]:
    """Import committed files plus explicitly selected worktree changes, never Git metadata."""
    root = root.resolve()
    is_git = _git_run(root, "rev-parse", "--show-toplevel")
    if is_git.returncode and (root / ".git").exists():
        raise ValueError("Git metadata cannot be read; project import stopped")
    files: dict[str, tuple[bytes, bool]] = {}
    skipped: list[str] = []
    local_hashes: dict[str, str | None] = {}
    if is_git.returncode == 0:
        git_root = Path(os.fsdecode(is_git.stdout).strip()).resolve()
        if git_root != root:
            raise ValueError("Copy mode currently requires the Git repository root as workspace")
        # HEAD tree carries executable bits without exposing .git to the task.
        tree = _git(root, "ls-tree", "-rz", "HEAD")
        entries = []
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            mode, kind, _oid = metadata.split()
            name = os.fsdecode(raw_name)
            if kind == b"blob" and mode in (b"100644", b"100755"):
                entries.append((name, mode == b"100755"))
            else:
                skipped.append(name)
        changed = set(
            os.fsdecode(item)
            for item in _git(
                root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD"
            ).split(b"\0")
            if item
        )
        untracked = [
            os.fsdecode(item)
            for item in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
            if item
        ]
        entries.extend((name, False) for name in untracked)
        for name, executable in entries:
            selected = any(fnmatch.fnmatch(name, pattern) for pattern in config.include_uncommitted)
            if excluded(name, config.exclude) or (name in untracked and not selected):
                skipped.append(name)
                continue
            try:
                current = read_local(root, name, config.max_file_bytes)
            except ValueError:
                skipped.append(name)
                continue
            if name in changed and not selected:
                size = int(_git(root, "cat-file", "-s", "HEAD:" + name).strip())
                if size > config.max_file_bytes:
                    raise ValueError("Committed project file exceeds size limit: " + name)
            raw = (
                _git(root, "show", "HEAD:" + name) if name in changed and not selected else current
            )
            if raw is None:
                continue
            local_hashes[name] = digest(current)
            files[name] = (raw, executable)
    else:
        for current_dir, dirs, names in os.walk(root, followlinks=False):
            current_path = Path(current_dir)
            dirs[:] = sorted(
                d
                for d in dirs
                if not excluded((current_path / d).relative_to(root).as_posix(), config.exclude)
                and not (current_path / d).is_symlink()
                and not (current_path / d).is_junction()
            )
            for name in sorted(names):
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                if excluded(relative, config.exclude):
                    skipped.append(relative)
                    continue
                try:
                    raw = read_local(root, relative, config.max_file_bytes)
                except ValueError:
                    skipped.append(relative)
                    continue
                if raw is not None:
                    files[relative] = (
                        raw,
                        os.name != "nt" and bool(path.stat().st_mode & stat.S_IXUSR),
                    )
                    local_hashes[relative] = digest(raw)
    result: list[dict[str, Any]] = []
    total = 0
    case_names: set[str] = set()
    for name, (raw, executable) in sorted(files.items()):
        portable_path(name)
        if SECRET_CONTENT.search(raw):
            skipped.append(name)
            continue
        if name.casefold() in case_names:
            raise ValueError("Case-colliding project names")
        case_names.add(name.casefold())
        total += len(raw)
        if (
            len(raw) > config.max_file_bytes
            or total > config.max_total_bytes
            or len(result) >= config.max_files
        ):
            raise ValueError(
                "Project copy exceeds configured size limits; narrow execution.exclude"
            )
        result.append(
            {
                "path": name,
                "content": base64.b64encode(raw).decode(),
                "executable": executable,
                "local_hash": local_hashes[name],
            }
        )
    return {
        "root": str(root),
        "files": result,
        "skipped": sorted(set(skipped)),
        "revision": uuid.uuid4().hex,
    }


def validate_export(items: Any, config: ExecutionConfig) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list) or len(items) > config.max_files:
        raise ValueError("Invalid exported file list")
    result = {}
    case_names: set[str] = set()
    total = 0
    for item in items:
        name = portable_path(item["path"])
        if excluded(name, config.exclude):
            continue
        if name.casefold() in case_names:
            raise ValueError("Case-colliding export names")
        case_names.add(name.casefold())
        raw = base64.b64decode(item["content"], validate=True)
        total += len(raw)
        if len(raw) > config.max_file_bytes or total > config.max_total_bytes:
            raise ValueError("Export size exceeds configured limits")
        if SECRET_CONTENT.search(raw):
            raise ValueError("Potential secret in exported file: " + name)
        result[name] = {
            "path": name,
            "content": item["content"],
            "executable": bool(item.get("executable")),
        }
    return result


def make_review(state_dir: Path, exported: Any, config: ExecutionConfig) -> dict[str, Any]:
    baseline = load_object(state_dir / "baseline.json")
    root = Path(baseline["root"])
    before = {item["path"]: item for item in baseline["files"]}
    after = validate_export(exported, config)
    changes = []
    for name in sorted(before.keys() | after.keys()):
        old = before.get(name)
        new = after.get(name)
        if (
            old
            and new
            and old["content"] == new["content"]
            and old["executable"] == new["executable"]
        ):
            continue
        old_raw = base64.b64decode(old["content"]) if old else None
        new_raw = base64.b64decode(new["content"]) if new else None
        expected = old["local_hash"] if old else None
        try:
            local_raw = read_local(root, name)
            conflict = digest(local_raw) != expected or (
                old is not None and expected != digest(old_raw)
            )
        except ValueError:
            conflict = True
        diff = "".join(
            difflib.unified_diff(
                (old_raw or b"").decode("utf-8", "replace").splitlines(keepends=True),
                (new_raw or b"").decode("utf-8", "replace").splitlines(keepends=True),
                fromfile="a/" + name,
                tofile="b/" + name,
            )
        )
        changes.append(
            {
                "path": name,
                "kind": "add" if old is None else "delete" if new is None else "modify",
                "expected_local_hash": expected,
                "conflict": conflict,
                "diff": diff,
                "file": new,
                "content_hash": digest(new_raw),
            }
        )
    review = {
        "id": uuid.uuid4().hex,
        "baseline_revision": baseline["revision"],
        "root": str(root),
        "changes": changes,
    }
    save_private(state_dir / "reviews" / (review["id"] + ".json"), review)
    return review


def apply_review(state_dir: Path, review_id: str, config: ExecutionConfig) -> dict[str, Any]:
    """Apply reviewed bytes only after all paths pass a conflict preflight.

    A Core-side lock serializes imports/reviews/applies. Concurrent external editors
    are checked again immediately before each replacement; originals are backed up.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", review_id):
        raise ValueError("Invalid review ID")
    review_path = state_dir / "reviews" / (review_id + ".json")
    review = load_object(review_path)
    baseline = load_object(state_dir / "baseline.json")
    if review["baseline_revision"] != baseline["revision"] or review.get("applied"):
        raise ValueError("Review is stale or already applied; generate a new review")
    root = Path(baseline["root"])
    if review["root"] != str(root):
        raise ValueError("Review workspace mismatch")
    changes = review["changes"]
    conflicts = []
    backups: dict[str, bytes | None] = {}
    for change in changes:
        name = portable_path(change["path"])
        if excluded(name, config.exclude):
            raise ValueError("Cannot apply an excluded path")
        current = read_local(root, name)
        if change["conflict"] or digest(current) != change["expected_local_hash"]:
            conflicts.append(name)
        backups[name] = current
        if change["file"]:
            validate_export([change["file"]], config)
            if (
                change["file"]["path"] != name
                or digest(base64.b64decode(change["file"]["content"])) != change["content_hash"]
            ):
                raise ValueError("Review content changed")
    if conflicts:
        raise ValueError("Local changes conflict; no files applied: " + ", ".join(conflicts))
    save_private(
        state_dir / "backups" / (review_id + ".json"),
        {
            name: base64.b64encode(raw).decode() if raw is not None else None
            for name, raw in backups.items()
        },
    )
    applied: list[str] = []
    try:
        for change in changes:
            name = change["path"]
            target = local_path(root, name)
            if digest(read_local(root, name)) != change["expected_local_hash"]:
                raise ValueError("Local file changed while applying: " + name)
            if change["file"] is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".jarvis-apply-")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(base64.b64decode(change["file"]["content"]))
                    if digest(read_local(root, name)) != change["expected_local_hash"]:
                        raise ValueError("Local file changed while applying: " + name)
                    os.replace(temporary, target)
                    if os.name != "nt":
                        target.chmod(0o755 if change["file"]["executable"] else 0o644)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            applied.append(name)
    except Exception:
        for name in reversed(applied):
            change = next(item for item in changes if item["path"] == name)
            if digest(read_local(root, name)) == change["content_hash"]:
                if backups[name] is None:
                    local_path(root, name).unlink(missing_ok=True)
                else:
                    local_path(root, name).write_bytes(backups[name] or b"")
        raise
    # Advance the baseline only for bytes the user has explicitly accepted.
    before = {item["path"]: item for item in baseline["files"]}
    for change in changes:
        if change["file"] is None:
            before.pop(change["path"], None)
        else:
            before[change["path"]] = {**change["file"], "local_hash": change["content_hash"]}
    baseline["files"] = list(before.values())
    baseline["revision"] = uuid.uuid4().hex
    save_private(state_dir / "baseline.json", baseline)
    review["applied"] = True
    save_private(review_path, review)
    return {"applied": applied}
