from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Label, ListView

from jarvis.core.bus.envelope import HandlerError
from jarvis.core.config import JarvisConfig, LlmConfig, ModelConfig, _apply_toml, get_config
from jarvis.core.events.bus import EventBus
from jarvis.core.llm.openai_responses import OpenAIResponsesProvider
from jarvis.core.llm.provider import create_llm_provider
from jarvis.core.llm.types import LlmResponse
from jarvis.core.runner import AgentRunner
from jarvis.core.session.manager import SESSION_BUSY, SessionManager
from jarvis.core.session.store import SessionStore
from jarvis.core.transport.socket_client import IpcError
from jarvis.tui.app import ChatTextArea, JarvisTuiApp, SelectableStatic
from jarvis.tui.model_picker import ModelItem, ModelPicker


# 构建含独立中转凭证的双模型配置
def _config() -> LlmConfig:
    return LlmConfig(
        provider="deepseek", default_model="deepseek-flash", deepseek_api_key="deepseek-key",
        base_url="https://api.deepseek.com/anthropic",
        models={"CCCX-Joe": ModelConfig(
            provider="openai", model="gpt-6.1-sol", base_url="https://relay.test/v1",
            display_name="6.1 Sol",
            reasoning_effort="xhigh", api_key_env="CCCX_JOE_API_KEY", api_key="relay-key",
        )},
    )


# 功能：验证模型配置加载独立密钥且日志 repr 不暴露密钥
# 设计：通过真实 TOML、环境优先级和 provider factory 联测，检查中转不继承 DeepSeek 地址
def test_profile_loads_credentials_and_keeps_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('''
[llm.models.CCCX-Joe]
provider = "openai"
model = "gpt-6.1-sol"
display_name = "6.1 Sol"
base_url = "https://relay.test/v1"
reasoning_effort = "xhigh"
store = false
api_key_env = "CCCX_JOE_API_KEY"
''', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_CONFIG", str(config_path))
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("CCCX_JOE_API_KEY", "unique-profile-key")
    config = get_config().llm
    provider = create_llm_provider(config, "CCCX-Joe")
    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider._base_url == "https://relay.test/v1"
    assert provider._reasoning_effort == "xhigh" and provider._store is False
    assert provider._api_key == "unique-profile-key"
    assert config.provider == "deepseek"
    assert config.models["CCCX-Joe"].display_name == "6.1 Sol"
    assert "unique-profile-key" not in repr(config)


# 功能：验证从其他目录启动时仍能读取全局模型，并允许项目增加或覆盖模型
# 设计：用隔离的用户目录与两个真实工作目录复现截图场景，不依赖开发机的实际配置
def test_global_models_are_available_across_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    settings = home / ".jarvis"
    settings.mkdir(parents=True)
    (settings / "config.toml").write_text('''
[llm.models.global-gpt]
provider = "openai"
model = "gpt-6.1-sol"
display_name = "6.1 Sol"
''', encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    (second / ".jarvis").mkdir(parents=True)
    (second / ".jarvis/config.toml").write_text('''
[llm.models.local-gpt]
provider = "openai"
model = "gpt-6-astra"
''', encoding="utf-8")
    monkeypatch.chdir(first)
    assert list(get_config().llm.models) == ["global-gpt"]
    monkeypatch.chdir(second)
    assert list(get_config().llm.models) == ["global-gpt", "local-gpt"]


# 功能：验证显式认证文件只在需要时读取，环境密钥优先且不会回显损坏文件内容
# 设计：先使用真实认证文件，再写入损坏内容并覆盖密钥，覆盖优先级与错误边界
def test_explicit_auth_file_and_key_precedence(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"OPENAI_API_KEY": "file-key"}), encoding="utf-8")
    config = LlmConfig(provider="openai", default_model="gpt-6.1-sol", api_key_file=str(auth))
    provider = create_llm_provider(config)
    assert isinstance(provider, OpenAIResponsesProvider) and provider._api_key == "file-key"
    auth.write_text("secret-invalid-json", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read") as exc:
        create_llm_provider(config)
    assert "secret-invalid-json" not in str(exc.value)
    config.openai_api_key = "env-key"
    assert create_llm_provider(config)._api_key == "env-key"  # type: ignore[attr-defined]


# 功能：验证新模型配置拒绝未知字段和不合法类型
# 设计：直接测试配置边界，避免错误字符串被当成布尔值后意外开启服务端存储
@pytest.mark.parametrize("field,value", [
    ("store", "false"), ("context_window", True), ("context_window", 0), ("unexpected", 1),
    ("display_name", 123),
])
def test_model_config_rejects_invalid_fields(field: str, value: Any) -> None:
    profile = {"provider": "openai", "model": "gpt-6.1-sol", field: value}
    with pytest.raises(SystemExit):
        _apply_toml(JarvisConfig(), {"llm": {"models": {"CCCX-Joe": profile}}})


# 功能：验证模型选择按会话隔离并能在重启后恢复，模型列表不暴露认证信息
# 设计：使用真实元信息存储和两份 SessionManager，避免只验证内存赋值
async def test_model_selection_is_per_session_and_persisted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    config = _config()
    manager = SessionManager(store, MagicMock(), EventBus(), llm_config=config)
    first = await manager.create("chat")
    second = await manager.create("chat")
    result = await manager.set_model(first.id, "CCCX-Joe")
    assert result.model == "gpt-6.1-sol"
    assert manager.list_models(first.id).selected == "CCCX-Joe"
    assert manager.list_models(second.id).selected == "default"
    assert manager.list_models(first.id).models[1].display_name == "6.1 Sol"
    assert "relay-key" not in manager.list_models(first.id).model_dump_json()
    restored = SessionManager(store, MagicMock(), EventBus(), llm_config=config)
    assert (await restored.resume(first.id)).model_alias == "CCCX-Joe"
    with pytest.raises(HandlerError, match="Unknown model"):
        await restored.set_model(first.id, "nonexistent")
    assert store.read_meta(first.id).model_alias == "CCCX-Joe"
    async with restored._locks[first.id]:
        with pytest.raises(HandlerError) as exc:
            await restored.set_model(first.id, "default")
        assert exc.value.code == SESSION_BUSY


# 功能：验证 AgentRunner 实际使用会话选择的模型并将协议状态写入历史
# 设计：替换 provider 工厂但保留真实 runner、loop 和 store，覆盖配置到执行的完整链路
async def test_runner_uses_selected_model_and_saves_state(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, MagicMock(), EventBus(), llm_config=_config())
    session = await manager.create("chat")
    session.model_alias = "CCCX-Joe"
    store.append_message(session.id, "user", "hello")

    # 返回带协议状态的最短完整响应
    async def chat(*args: Any, **kwargs: Any) -> LlmResponse:
        return LlmResponse(stop_reason="end_turn", text="done", provider_blocks=[{
            "type": "provider_state", "provider": "openai_responses", "items": [],
        }])

    provider = MagicMock()
    provider.chat = chat
    config = JarvisConfig(llm=_config(), workspace=tmp_path)
    with patch("jarvis.core.runner.create_llm_provider", return_value=provider) as factory:
        outcome = await AgentRunner(config).run_and_capture("hello", session=session, store=store)
    factory.assert_called_once_with(config.llm, "CCCX-Joe")
    assert outcome.status == "success"
    assert store.read_messages(session.id)[-1]["content"][0]["type"] == "provider_state"


class _ModelTui(JarvisTuiApp):
    # 阻止测试连接真实后台服务，保持正常的 Textual 挂载和键盘交互
    async def _socket_loop(self) -> None:
        pass


class _Client:
    # 保存模型命令以确认斜杠输入不会发送给 LLM
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.selected = "default"
        self.fail_switch = False

    # 模拟服务端列举与切换模型的响应
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "model.list":
            return {"selected": self.selected, "models": [
                {"name": "default", "model": "deepseek-flash"},
                {"name": "CCCX-Joe", "model": "gpt-6.1-sol", "display_name": "6.1 Sol"},
            ]}
        if self.fail_switch:
            raise IpcError(-32602, "模型认证未配置")
        self.selected = params["name"]
        return {
            "name": self.selected,
            "model": "deepseek-flash" if self.selected == "default" else "gpt-6.1-sol",
        }


# 功能：验证 /model 一次回车打开菜单，键盘和鼠标都能切换模型并更新标题
# 设计：走真实补全弹窗、模态菜单和 IPC stub，复现用户实际输入而非直接调用切换方法
async def test_tui_model_commands_list_and_switch() -> None:
    app = _ModelTui("127.0.0.1", 1)
    client = _Client()
    async with app.run_test(size=(140, 40)) as pilot:
        app._client = client  # type: ignore[assignment]
        app._session_id = "sess-test"
        prompt = app.query_one(ChatTextArea)
        prompt.disabled = False
        prompt.focus()
        await pilot.press("/", "m", "o", "d", "e", "l")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker)
        assert app.screen.query_one(ListView).index == 0
        assert "6.1 Sol" in str(app.screen.query(ModelItem)[1].query_one(Label).render())
        await pilot.press("down", "enter")
        await pilot.pause()
        assert [call[0] for call in client.calls] == ["model.list", "session.set_model"]
        assert app._model_alias == "CCCX-Joe" and not app._busy
        assert "CCCX-Joe" in str(app.query_one("#header", Label).render())
        assert any("gpt-6.1-sol" in str(row.render()) for row in app.query(SelectableStatic))
        prompt.text = "/model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one(ListView).index == 1
        await pilot.click(app.screen.query(ModelItem)[0])
        await pilot.pause()
        assert app._model_alias == "default"
        assert client.calls[-1] == (
            "session.set_model", {"session_id": "sess-test", "name": "default"},
        )


# 功能：验证取消菜单或再次确认当前模型不会发出切换命令，输入焦点正常恢复
# 设计：从非默认模型打开菜单检查初始位置和勾选，以避免菜单总是重置到第一项
@pytest.mark.parametrize("key", ["escape", "enter"])
async def test_tui_model_picker_preserves_selection(key: str) -> None:
    app = _ModelTui("127.0.0.1", 1)
    client = _Client()
    client.selected = "CCCX-Joe"
    async with app.run_test(size=(80, 24)) as pilot:
        app._client = client  # type: ignore[assignment]
        app._session_id = "sess-test"
        prompt = app.query_one(ChatTextArea)
        prompt.disabled = False
        prompt.focus()
        prompt.text = "/model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ModelPicker)
        assert app.screen.query_one(ListView).index == 1
        check = app.screen.query(ModelItem)[1].query_one(".model-check", Label)
        assert "✓" in str(check.render())
        await pilot.press(key)
        await pilot.pause()
        assert not isinstance(app.screen, ModelPicker)
        assert [call[0] for call in client.calls] == ["model.list"]
        assert app._model_alias == "CCCX-Joe" and not app._busy
        assert prompt.has_focus


# 功能：验证按名称直接切换仍可用，服务端拒绝切换时保留原模型并显示原因
# 设计：通过同一个输入路径覆盖成功与结构化错误，检查失败后的可继续操作状态
@pytest.mark.parametrize("fail_switch", [False, True])
async def test_tui_direct_model_command_and_failure(fail_switch: bool) -> None:
    app = _ModelTui("127.0.0.1", 1)
    client = _Client()
    client.fail_switch = fail_switch
    async with app.run_test(size=(100, 30)) as pilot:
        app._client = client  # type: ignore[assignment]
        app._session_id = "sess-test"
        prompt = app.query_one(ChatTextArea)
        prompt.disabled = False
        prompt.focus()
        prompt.text = "/model CCCX-Joe"
        await pilot.press("enter")
        await pilot.pause()
        assert [call[0] for call in client.calls] == ["session.set_model"]
        assert app._model_alias == ("default" if fail_switch else "CCCX-Joe")
        assert not app._busy and not isinstance(app.screen, ModelPicker)
        expected = "模型认证未配置" if fail_switch else "gpt-6.1-sol"
        assert any(expected in str(row.render()) for row in app.query(SelectableStatic))
