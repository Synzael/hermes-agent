"""Hook-flow tests for the velvet-stakes plugin.

Mirrors the fake-gateway style of tests/gateway/test_pre_gateway_dispatch.py:
a SimpleNamespace gateway with an AsyncMock adapter, real SessionSource
objects so build_session_key produces real keys.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource

from tests.plugins._velvet_stakes import load_module, load_plugin_init

store_mod = load_module("store")
plugin = load_plugin_init()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugin, "_store", store_mod.VelvetStakesStore())
    return home


@pytest.fixture
def gateway():
    adapter = SimpleNamespace(send=AsyncMock())
    gw = SimpleNamespace(
        _is_user_authorized=lambda source: True,
        _adapter_for_source=lambda source: adapter,
        _thread_metadata_for_source=lambda source: None,
    )
    return gw, adapter


def _event(text: str, chat_id: str = "12345") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        message_type=None,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            user_id="u1",
            chat_type="dm",
        ),
    )


async def _dispatch(gw, text: str, chat_id: str = "12345"):
    """Invoke the sync hook, then flush the scheduled send task."""
    result = plugin.on_pre_gateway_dispatch(event=_event(text, chat_id), gateway=gw)
    for _ in range(3):
        await asyncio.sleep(0)
    return result


def _last_reply(adapter) -> str:
    assert adapter.send.await_count > 0, "expected a reply"
    args, kwargs = adapter.send.await_args
    return args[1]


class TestFallThrough:
    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self, hermes_home, gateway):
        gw, adapter = gateway
        gw._is_user_authorized = lambda source: False
        assert await _dispatch(gw, "bet start") is None
        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chitchat_falls_through(self, hermes_home, gateway):
        gw, adapter = gateway
        assert await _dispatch(gw, "what's the weather like?") is None
        assert await _dispatch(gw, "win") is None  # no active session
        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_text_message_ignored(self, hermes_home, gateway):
        gw, adapter = gateway
        event = _event("win")
        event.message_type = SimpleNamespace(value="photo")
        assert plugin.on_pre_gateway_dispatch(event=event, gateway=gw) is None

    @pytest.mark.asyncio
    async def test_missing_event_or_gateway(self, hermes_home, gateway):
        gw, _adapter = gateway
        assert plugin.on_pre_gateway_dispatch(event=None, gateway=gw) is None
        assert plugin.on_pre_gateway_dispatch(event=_event("win"), gateway=None) is None


class TestSessionFlow:
    @pytest.mark.asyncio
    async def test_start_records_session_and_replies(self, hermes_home, gateway):
        gw, adapter = gateway
        result = await _dispatch(gw, "bet start aggressive 5000 250 500")
        assert result == {"action": "skip", "reason": "velvet-stakes"}
        reply = _last_reply(adapter)
        assert "Session started (aggressive)" in reply
        assert "$5,000" in reply

    @pytest.mark.asyncio
    async def test_win_loss_flow(self, hermes_home, gateway):
        gw, adapter = gateway
        await _dispatch(gw, "bet start")
        await _dispatch(gw, "w")
        assert "WIN +$5" in _last_reply(adapter)
        await _dispatch(gw, "0")
        assert "LOSS -$5" in _last_reply(adapter)

    @pytest.mark.asyncio
    async def test_bridging_flow_reject_then_carry(self, hermes_home, gateway):
        gw, adapter = gateway
        await _dispatch(gw, "bet start")
        for _ in range(9):
            await _dispatch(gw, "l")
        assert "carry" in _last_reply(adapter)  # bridging prompt

        await _dispatch(gw, "yes")  # ambiguous mid-decision → rejected
        assert "decide first" in _last_reply(adapter)

        await _dispatch(gw, "carry")
        assert "Carried over to L2" in _last_reply(adapter)

    @pytest.mark.asyncio
    async def test_status_and_stop(self, hermes_home, gateway):
        gw, adapter = gateway
        await _dispatch(gw, "bet start")
        await _dispatch(gw, "status")
        assert "next bet $5" in _last_reply(adapter)

        await _dispatch(gw, "stop")
        reply = _last_reply(adapter)
        assert "Session Ended" in reply

        history = (hermes_home / "velvet_stakes" / "history.jsonl").read_text()
        assert len(history.strip().splitlines()) == 1
        # Session gone → triggers fall through again
        assert await _dispatch(gw, "win") is None

    @pytest.mark.asyncio
    async def test_sessions_isolated_per_chat(self, hermes_home, gateway):
        gw, adapter = gateway
        await _dispatch(gw, "bet start", chat_id="alice")
        assert await _dispatch(gw, "win", chat_id="bob") is None  # bob has no session
        assert await _dispatch(gw, "win", chat_id="alice") is not None

    @pytest.mark.asyncio
    async def test_odds_ack_then_results(self, hermes_home, gateway, monkeypatch):
        monkeypatch.setenv("VELVET_STAKES_TRIALS", "50")
        gw, adapter = gateway
        await _dispatch(gw, "bet start")
        result = await _dispatch(gw, "odds")
        assert result == {"action": "skip", "reason": "velvet-stakes"}

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)

        replies = [call.args[1] for call in adapter.send.await_args_list]
        assert any("Crunching odds" in r for r in replies)
        assert any("Odds from here (50 sims" in r for r in replies)

    @pytest.mark.asyncio
    async def test_adapter_missing_degrades_gracefully(self, hermes_home, gateway):
        gw, adapter = gateway
        gw._adapter_for_source = lambda source: None
        result = await _dispatch(gw, "bet start")
        # Still consumed (state was saved); reply is dropped with a warning.
        assert result == {"action": "skip", "reason": "velvet-stakes"}


class TestRegister:
    def test_register_hooks_up(self, hermes_home):
        registered = {}

        def register_hook(name, callback):
            registered[name] = callback

        plugin.register(SimpleNamespace(register_hook=register_hook))
        assert registered == {"pre_gateway_dispatch": plugin.on_pre_gateway_dispatch}
