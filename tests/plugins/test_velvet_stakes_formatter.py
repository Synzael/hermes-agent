"""Formatter tests: plain text, no markdown, key facts present."""

import pytest

from tests.plugins._velvet_stakes import load_module

engine = load_module("engine")
presets = load_module("presets")
store_mod = load_module("store")
monte_carlo = load_module("monte_carlo")
formatter = load_module("formatter")


def _session(state=None, preset="default"):
    strategy = presets.create_strategy_from_preset(preset)
    config = presets.DEFAULT_SESSION_CONFIG
    return store_mod.ActiveSession(
        preset_name=preset,
        config=config,
        strategy=strategy,
        state=state or engine.create_initial_state(strategy),
        bet_history=(),
        events=(),
        started_at=0,
    )


def _bridging_session():
    session = _session()
    state = session.state
    for _ in range(9):
        state = engine.process_bet(
            state, session.config, session.strategy, False, "at_bridging_only"
        )
    assert state.awaiting_decision
    return store_mod.with_state(session, state)


class TestMoney:
    def test_money_and_signed(self):
        assert formatter.money(1234) == "$1,234"
        assert formatter.money(12.5) == "$12.50"
        assert formatter.signed(-25) == "-$25"
        assert formatter.signed(15) == "+$15"
        assert formatter.signed(0) == "$0"


class TestRenders:
    def test_start(self):
        text = formatter.render_start(_session())
        assert "$10,000" in text
        assert "$5" in text  # first stake
        assert "L1" in text

    def test_bet_ack_win(self):
        session = _session()
        state = engine.process_bet(
            session.state, session.config, session.strategy, True, "at_bridging_only"
        )
        session = store_mod.with_state(session, state)
        text = formatter.render_bet_ack(True, 5, session)
        assert "WIN +$5" in text
        assert "P&L +$5" in text
        assert "round 1" in text

    def test_bridging_prompt_shows_choices_and_recovery_math(self):
        session = _bridging_session()
        text = formatter.render_bridging_prompt(session)
        assert "top of L1 ($275)" in text
        assert "P&L -$710" in text
        assert "carry" in text and "write" in text and "stop" in text
        assert "L2 $50" in text
        assert "-$355" in text  # 50% recovery target

    def test_rejected_repeats_prompt(self):
        text = formatter.render_rejected(_bridging_session())
        assert "decide first" in text
        assert "carry" in text

    def test_status_in_recovery(self):
        session = _bridging_session()
        state = engine.process_bridging_decision(
            session.state, session.strategy, "carry_over"
        )
        session = store_mod.with_state(session, state)
        text = formatter.render_status(session)
        assert "Recovery mode" in text
        assert "-$355" in text

    def test_summary(self):
        session = _bridging_session()
        state = engine.process_bridging_decision(
            session.state, session.strategy, "stop_session"
        )
        session = store_mod.with_state(session, state)
        text = formatter.render_summary(session)
        assert "Session Ended" in text
        assert "-$710" in text
        assert "9 rounds" in text

    def test_odds(self):
        session = _session()
        results = monte_carlo.run_risk_check(
            session.config, session.strategy, 0.4946, n_trials=100, seed=1
        )
        text = formatter.render_odds(results, session)
        assert "100 sims" in text
        assert "target +$500" in text
        assert "stop loss" in text

    def test_no_markdown_characters(self):
        for text in (
            formatter.render_help(),
            formatter.render_presets(),
            formatter.render_no_session(),
            formatter.render_start(_session()),
        ):
            assert "**" not in text and "###" not in text
