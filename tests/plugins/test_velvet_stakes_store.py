"""Persistence tests for the velvet-stakes store."""

import json

import pytest

from tests.plugins._velvet_stakes import load_module

engine = load_module("engine")
presets = load_module("presets")
store_mod = load_module("store")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _new_session(preset="default"):
    strategy = presets.create_strategy_from_preset(preset)
    config = presets.DEFAULT_SESSION_CONFIG
    return store_mod.ActiveSession(
        preset_name=preset,
        config=config,
        strategy=strategy,
        state=engine.create_initial_state(strategy, config.starting_ladder),
        bet_history=(),
        events=(),
        started_at=1_750_000_000_000,
    )


KEY = "agent:main:telegram:dm:12345"


class TestActiveSessionRoundTrip:
    def test_round_trip_preserves_dataclass_equality(self, hermes_home):
        store = store_mod.VelvetStakesStore()
        session = _new_session("aggressive")
        state = engine.process_bet(
            session.state, session.config, session.strategy, False, "at_bridging_only"
        )
        bet = engine.BetRecord(
            round=1, timestamp=1_750_000_001_000, ladder=0, index=0,
            stake=5, won=False, pnl_after=-5,
        )
        session = store_mod.with_bet(session, state, bet)
        store.save_active(KEY, session)

        reloaded = store_mod.VelvetStakesStore()
        assert reloaded.get_active(KEY) == session

    def test_key_isolation(self, hermes_home):
        store = store_mod.VelvetStakesStore()
        store.save_active(KEY, _new_session())
        assert store.get_active("agent:main:whatsapp:dm:999") is None

    def test_discard(self, hermes_home):
        store = store_mod.VelvetStakesStore()
        store.save_active(KEY, _new_session())
        store.discard_active(KEY)
        assert store.get_active(KEY) is None
        assert store_mod.VelvetStakesStore().get_active(KEY) is None

    def test_corrupt_file_degrades_to_empty(self, hermes_home):
        base = hermes_home / "velvet_stakes"
        base.mkdir()
        (base / "active_sessions.json").write_text("{not json")
        store = store_mod.VelvetStakesStore()
        assert store.get_active(KEY) is None


class TestFinish:
    def test_finish_appends_session_result_shape(self, hermes_home):
        store = store_mod.VelvetStakesStore()
        session = _new_session()
        state = session.state
        for won in (False, True):
            state = engine.process_bet(
                state, session.config, session.strategy, won, "at_bridging_only"
            )
        store.save_active(KEY, store_mod.with_state(session, state))

        result = store.finish(KEY, ended_at=1_750_000_100_000)

        assert store.get_active(KEY) is None
        # SessionResult shape (camelCase, matching the app's types.ts)
        for field in (
            "id", "startTime", "endTime", "hitTarget", "hitStopLoss",
            "hitMaxRounds", "hitTableLimit", "bankrollExhausted", "userStopped",
            "finalPnl", "roundsPlayed", "totalWagered", "maxStakeSeen",
            "maxDrawdown", "ladderTouches", "topOfLadderTouches", "finalLadder",
            "finalIndex", "config", "strategy", "betHistory", "events",
        ):
            assert field in result, field
        assert result["startTime"] == 1_750_000_000_000
        assert result["endTime"] == 1_750_000_100_000
        assert result["roundsPlayed"] == state.rounds
        assert result["ladderTouches"] == {"0": 2, "1": 0, "2": 0}
        assert result["config"]["profitTarget"] == 500
        assert result["strategy"]["bridgingPolicy"] == "carry_over_index_delta"

        history_file = hermes_home / "velvet_stakes" / "history.jsonl"
        lines = history_file.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["id"] == result["id"]

    def test_finish_without_active_session_returns_none(self, hermes_home):
        store = store_mod.VelvetStakesStore()
        assert store.finish(KEY, ended_at=1) is None


class TestAtomicity:
    def test_no_partial_file_on_disk(self, hermes_home):
        store = store_mod.VelvetStakesStore()
        store.save_active(KEY, _new_session())
        raw = (hermes_home / "velvet_stakes" / "active_sessions.json").read_text()
        data = json.loads(raw)  # valid JSON on disk
        assert KEY in data
        leftovers = [
            p for p in (hermes_home / "velvet_stakes").iterdir()
            if p.name not in {"active_sessions.json", "history.jsonl"}
        ]
        assert leftovers == []
