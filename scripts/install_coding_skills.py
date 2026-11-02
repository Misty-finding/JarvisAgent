from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from jarvis.core.skills.loader import _parse_skill_file

LIBRARY = Path(__file__).resolve().parents[1] / ".agents" / "skills"


# 校验锁文件中的整个 Skill 目录，拒绝来源不明的新增或改动文件
def validate_library(library: Path) -> list[tuple[str, dict[str, str]]]:
    lock = json.loads((library / "skills.lock.json").read_text(encoding="utf-8"))
    if lock.get("format_version") != 1:
        raise ValueError("Unsupported skill lock format")
    entries: list[tuple[str, dict[str, str]]] = []
    for item in lock["skills"]:
        name = item["name"]
        if Path(name).name != name or name in (".", ".."):
            raise ValueError(f"Invalid skill name: {name}")
        root = library / name
        files = item["files"]
        if _parse_skill_file(root / "SKILL.md").name != name:
            raise ValueError(f"Skill name mismatch: {name}")
        if directory_hashes(root) != files:
            raise ValueError(f"Skill files differ from lock: {name}")
        entries.append((name, files))
    if len({name for name, _ in entries}) != len(entries):
        raise ValueError("Duplicate skill names in lock")
    return entries


# 读取目录内文件哈希，保留资源、许可和来源信息的完整性
def directory_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Skill must not contain symlinks: {path}")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


# 预先检查冲突后逐目录原子安装，相同版本跳过，已有自定义版本绝不覆盖
def install_library(library: Path, destination: Path) -> tuple[int, int]:
    entries = validate_library(library)
    pending: list[str] = []
    for name, files in entries:
        target = destination / name
        if target.exists():
            if directory_hashes(target) != files:
                raise ValueError(f"Existing skill differs; refusing to overwrite: {target}")
        else:
            pending.append(name)
    destination.mkdir(parents=True, exist_ok=True)
    for name in pending:
        with tempfile.TemporaryDirectory(prefix=".jarvis-skill-", dir=destination) as staging:
            copied = Path(staging) / name
            shutil.copytree(library / name, copied)
            copied.rename(destination / name)
    return len(pending), len(entries) - len(pending)


# 校验仓库内固定版本的编码库，或安装到通用全局目录及显式目标目录
def main() -> None:
    parser = argparse.ArgumentParser(description="Validate/install the pinned Jarvis coding skills")
    parser.add_argument("--dest", type=Path, default=Path("~/.agents/skills").expanduser())
    parser.add_argument("--check", action="store_true", help="Validate vendored files without writing")
    args = parser.parse_args()
    try:
        if args.check:
            print(f"Validated {len(validate_library(LIBRARY))} coding skills")
        else:
            installed, skipped = install_library(LIBRARY, args.dest.expanduser().resolve())
            print(f"Installed {installed}; identical versions skipped {skipped}; destination: {args.dest}")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Skill installation failed: {exc}\n")


if __name__ == "__main__":
    main()
