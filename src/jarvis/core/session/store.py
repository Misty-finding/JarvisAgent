from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.core.session.model import Session

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]

INTERRUPTED_TOOL_MESSAGE = (
    "Execution was interrupted before its result was recorded. Its effects are unknown: "
    "the operation may already have taken effect. Inspect the current files or external "
    "state before deciding whether to repeat it."
)


class SessionPersistenceError(OSError):
    pass


class SessionHistoryError(ValueError):
    pass


# 同目录写临时文件后原子替换，失败时保留原文件并清理临时文件
def _atomic_write(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
        os.replace(temporary, path)
    except OSError as exc:
        raise SessionPersistenceError(f"Cannot save session file: {path.name}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("cannot remove temporary session file %s", temporary)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # 初始化 session 文件存储根目录
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        if not re.fullmatch(r"sess-[A-Za-z0-9_-]+", sid):
            raise ValueError("invalid session ID")
        path = self._root / sid
        if path.resolve().parent != self._root.resolve():
            raise ValueError("session directory is outside this workspace")
        return path

    # 返回指定 session 下的 runs 目录路径
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 将 session meta 写入 meta.json
    def write_meta(self, session: Session) -> None:
        path = self.session_dir(session.id)
        _atomic_write(
            path / "meta.json",
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        data = json.loads((self.session_dir(sid) / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("run_ids", []), list):
            raise ValueError("invalid session metadata")
        session = Session.from_dict(data)
        if session.id != sid or session.schema_version != 1:
            raise ValueError("invalid or unsupported session metadata")
        if session.mode not in ("chat", "one_shot") or session.status not in (
            "active", "waiting_for_input", "closed"
        ):
            raise ValueError("invalid session state")
        if session.last_run_state not in (None, "running", "completed", "failed", "interrupted"):
            raise ValueError("invalid run state")
        if session.active_run_id is not None and not isinstance(session.active_run_id, str):
            raise ValueError("invalid active run ID")
        return session

    # 只扫描元信息并按更新时间排序，损坏会话返回警告供界面展示
    def list_sessions(self) -> tuple[list[Session], list[str]]:
        sessions: list[Session] = []
        warnings: list[str] = []
        for path in self._root.glob("sess-*/meta.json"):
            try:
                sessions.append(self.read_meta(path.parent.name))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("cannot read session metadata %s: %s", path, exc)
                warnings.append(f"会话 {path.parent.name} 的元信息无法读取，已跳过。")
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return sessions, warnings

    # 追加一条 Anthropic API 消息到 thread.jsonl
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {"ts": _now(), "role": role, "content": content}
        if run_id is not None:
            row["run_id"] = run_id
        self._append_row(sid, row)

    # 逐条追加并关闭文件，补齐旧文件缺少的换行，写入失败向上阻止继续执行
    def _append_row(self, sid: str, row: dict[str, Any]) -> None:
        path = self.session_dir(sid) / "thread.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as stream:
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b"\n":
                        stream.write(b"\n")
                stream.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
                stream.flush()
        except OSError as exc:
            raise SessionPersistenceError("Cannot save conversation history") from exc

    # 批量追加一次 run 新产生的消息到 thread.jsonl
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        for msg in messages:
            self.append_message(
                sid,
                role=str(msg["role"]),
                content=msg["content"],
                run_id=run_id,
            )

    # 读取完整 thread 并返回可直接传给 Anthropic 的 messages
    def read_messages(self, sid: str, *, truncate: bool = True) -> list[dict[str, Any]]:
        rows, _ = self._read_rows(sid)
        messages: list[dict[str, Any]] = []
        for row in rows:
            message = {"role": row["role"], "content": row["content"]}
            # 工具结果逐条落盘，加载时重新合并为合法的同轮结果消息
            content = message["content"]
            previous = messages[-1] if messages else None
            if (
                previous is not None and message["role"] == previous["role"] == "user"
                and self._is_tool_results(content)
                and self._is_tool_results(previous["content"])
            ):
                previous["content"].extend(content)
            else:
                messages.append(message)
        messages = self._trim_orphan_tool_use(messages)
        if not truncate:
            return messages
        from jarvis.core.compact.budget import truncate_tool_results
        return truncate_tool_results(messages)

    # 识别纯工具结果消息，避免把普通用户输入误并入工具结果
    @staticmethod
    def _is_tool_results(content: Any) -> bool:
        return isinstance(content, list) and bool(content) and all(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )

    # 严格读取原始记录；仅允许恢复时截去末尾未写完的一行
    def _read_rows(
        self, sid: str, *, repair_tail: bool = False
    ) -> tuple[list[dict[str, Any]], list[str]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return [], []
        lines = path.read_bytes().splitlines(keepends=True)
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                if repair_tail and index == len(lines) - 1 and not line.endswith(b"\n"):
                    self._backup_thread(path)
                    _atomic_write(path, b"".join(lines[:index]).decode("utf-8"))
                    warnings.append("上次退出时最后一条消息未保存完整，已保留有效历史并备份原文件。")
                    break
                raise SessionHistoryError(f"会话历史第 {index + 1} 行损坏，未修改原文件。") from exc
            if (
                not isinstance(row, dict) or row.get("role") not in ("user", "assistant")
                or not isinstance(row.get("content"), (str, list))
                or (isinstance(row["content"], list) and not all(
                    isinstance(block, dict) for block in row["content"]
                ))
            ):
                raise SessionHistoryError(f"会话历史第 {index + 1} 行格式不正确。")
            rows.append(row)
        return rows, warnings

    # 备份原历史，使用微秒时间戳防止连续压缩或恢复覆盖备份
    def _backup_thread(self, path: Path) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(path, path.with_name(f"thread_{stamp}.jsonl.bak"))

    # 补齐未配对调用的未知结果，幂等记录中断说明，并保留所有原始调用
    def recover_thread(self, sid: str, interrupted_run_id: str | None = None) -> list[str]:
        rows, warnings = self._read_rows(sid, repair_tail=True)
        pending: dict[str, str | None] = {}
        seen: set[str] = set()
        for row in rows:
            content = row["content"]
            if pending and row["role"] == "assistant":
                raise SessionHistoryError("未配对工具调用之后出现新的模型消息，无法安全恢复。")
            if isinstance(content, str):
                if pending:
                    raise SessionHistoryError("历史中存在未配对调用之后的普通消息，无法安全恢复。")
                continue
            for block in content:
                if row["role"] == "assistant" and block.get("type") == "tool_use":
                    call_id = block.get("id")
                    if not isinstance(call_id, str) or not call_id or call_id in seen:
                        raise SessionHistoryError("工具调用 ID 缺失或重复，无法安全恢复。")
                    seen.add(call_id)
                    pending[call_id] = row.get("run_id")
                elif row["role"] == "user" and block.get("type") == "tool_result":
                    call_id = block.get("tool_use_id")
                    if not isinstance(call_id, str) or call_id not in pending:
                        raise SessionHistoryError("存在没有对应调用的工具结果，无法安全恢复。")
                    del pending[call_id]
                elif pending and row["role"] == "user":
                    raise SessionHistoryError("未配对工具调用之间混入普通消息，无法安全恢复。")
        if pending:
            self._append_row(sid, {
                "ts": _now(), "role": "user", "run_id": interrupted_run_id,
                "recovery": "interrupted_tools",
                "content": [{
                    "type": "tool_result", "tool_use_id": call_id,
                    "content": INTERRUPTED_TOOL_MESSAGE, "is_error": True,
                } for call_id in pending],
            })
            warnings.append("上次有工具调用未记录结果，实际效果未知；继续前请先检查当前状态。")
        if interrupted_run_id and not any(
            row.get("recovery") == "interrupted_run" and row.get("run_id") == interrupted_run_id
            for row in rows
        ):
            self._append_row(sid, {
                "ts": _now(), "role": "user", "run_id": interrupted_run_id,
                "recovery": "interrupted_run",
                "content": (
                    "[Session recovery] The previous run was interrupted. Background work and "
                    "pending approvals were not resumed. Before continuing, inspect the current "
                    "files, git diff, and external state; "
                    "do not assume unfinished operations failed."
                ),
            })
            warnings.append("上次执行已中断。恢复会话不会自动继续执行，请发送消息后继续。")
        return warnings

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    # 将压缩后的消息对覆盖写入 thread.jsonl，原文件备份为 thread_<ts>.jsonl.bak
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        path = self.session_dir(sid) / "thread.jsonl"
        if path.exists():
            self._backup_thread(path)
        text = "".join(json.dumps(
            {"ts": _now(), "role": msg["role"], "content": msg["content"]},
            ensure_ascii=False,
        ) + "\n" for msg in messages)
        _atomic_write(path, text)

    # 读取 notes.md 全文，文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # 将一条主动笔记追加到 notes.md
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "notes.md").open("a", encoding="utf-8") as f:
            f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
