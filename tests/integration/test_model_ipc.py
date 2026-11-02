from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


# 准备隔离的动态模型配置，确保测试后台启动时还没有命名模型
@pytest.fixture
def live_model_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "models.toml"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG", str(path))
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("DYNAMIC_MODEL_API_KEY", "test-model-key")
    return path


# 先配置临时路径再启动真实后台，避免 fixture 顺序导致读取开发机配置
@pytest.fixture
def configurable_model_daemon(
    live_model_config: Path, running_daemon: subprocess.Popen[bytes],
) -> subprocess.Popen[bytes]:
    return running_daemon


# 通过真实 TCP 连接发送模型命令并读取结构化响应；容纳 Windows TLS 证书初始化开销
async def _rpc(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    method: str, params: dict[str, Any],
) -> dict[str, Any]:
    writer.write((json.dumps({
        "jsonrpc": "2.0", "id": method, "method": method, "params": params,
    }) + "\n").encode())
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), timeout=15))


# 功能：验证真实 Core 暴露模型列表、切换及恢复接口，并将未知模型作为结构化错误返回
# 设计：复用带测试密钥的 daemon fixture，只检查配置和会话操作，避免外部 API 与真实凭证依赖
async def test_model_selection_commands_over_ipc(
    running_daemon: subprocess.Popen[bytes], free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    try:
        created = await _rpc(reader, writer, "session.create", {"mode": "chat"})
        sid = created["result"]["session_id"]
        listing = await _rpc(reader, writer, "model.list", {"session_id": sid})
        assert listing["result"]["selected"] == "default"
        default = listing["result"]["models"][0]
        assert default["name"] == "default"
        assert "api_key" not in default and "api_key_file" not in default
        selected = await _rpc(reader, writer, "session.set_model", {
            "session_id": sid, "name": "default",
        })
        assert selected["result"]["model"] == default["model"]
        restored = await _rpc(reader, writer, "session.resume", {"session_id": sid})
        assert restored["result"]["model_alias"] == "default"
        invalid = await _rpc(reader, writer, "session.set_model", {
            "session_id": sid, "name": "unknown-test-model",
        })
        assert invalid["error"]["code"] == -32602
        await _rpc(reader, writer, "session.close", {"session_id": sid})
    finally:
        writer.close()
        await writer.wait_closed()


# 功能：验证旧 Core 无需重启即可列出和选择后来新增的模型，配置损坏不会终止后台
# 设计：后台启动后才写入 TOML，通过真实 IPC 检查菜单数据、直接切换、持久化及解析失败恢复
async def test_running_core_reloads_model_profiles(
    configurable_model_daemon: subprocess.Popen[bytes], live_model_config: Path, free_port: int,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    try:
        created = await _rpc(reader, writer, "session.create", {"mode": "chat"})
        sid = created["result"]["session_id"]
        params = {"session_id": sid}
        initial = await _rpc(reader, writer, "model.list", params)
        assert [model["name"] for model in initial["result"]["models"]] == ["default"]

        first = '''
[llm.models."gpt-6.1-sol"]
provider = "openai"
model = "gpt-6.1-sol"
display_name = "6.1 Sol"
base_url = "https://relay.test/v1"
api_key_env = "DYNAMIC_MODEL_API_KEY"
'''
        live_model_config.write_text(first, encoding="utf-8")
        updated = await _rpc(reader, writer, "model.list", params)
        assert [model["name"] for model in updated["result"]["models"]] == [
            "default", "gpt-6.1-sol",
        ]
        assert updated["result"]["models"][1]["display_name"] == "6.1 Sol"
        assert "test-model-key" not in json.dumps(updated)

        second = '''
[llm.models.gpt-6-astra]
provider = "openai"
model = "gpt-6-astra"
api_key_env = "DYNAMIC_MODEL_API_KEY"
'''
        live_model_config.write_text(first + second, encoding="utf-8")
        selected = await _rpc(reader, writer, "session.set_model", {
            **params, "name": "gpt-6-astra",
        })
        assert selected["result"]["model"] == "gpt-6-astra"
        restored = await _rpc(reader, writer, "session.resume", params)
        assert restored["result"]["model_alias"] == "gpt-6-astra"

        live_model_config.write_text("[llm.models.", encoding="utf-8")
        invalid = await _rpc(reader, writer, "model.list", params)
        assert invalid["error"]["code"] == -32602
        ping = await _rpc(reader, writer, "core.ping", {})
        assert "result" in ping

        live_model_config.write_text(first + second, encoding="utf-8")
        recovered = await _rpc(reader, writer, "model.list", params)
        assert len(recovered["result"]["models"]) == 3
        assert recovered["result"]["selected"] == "gpt-6-astra"
        await _rpc(reader, writer, "session.close", params)
    finally:
        writer.close()
        await writer.wait_closed()
