"""Engine tests for the velvet-stakes plugin.

Scripted win/loss sequences with hand-computed expectations, mirroring the
scenario coverage of the reference repo's own engine tests. The engine is a
line-for-line port of betting-companion/src/engine/session.ts.
"""

import dataclasses

import pytest

from tests.plugins._velvet_stakes import load_module

engine = load_module("engine")
presets = load_module("presets")


def _default_strategy():
    return presets.create_strategy_from_preset("default")


def _config(**overrides):
    base = {
        "bankroll": 10000,
        "profit_target": 500,
        "stop_loss_abs": 1000,
        "max_rounds": 5000,
    }
    base.update(overrides)
    return engine.SessionConfig(**base)


def _play(state, config, strategy, sequence):
    """Feed a W/L string through process_bet."""
    for ch in sequence:
        state = engine.process_bet(state, config, strategy, ch == "W", "at_bridging_only")
    return state


class TestLadderBasics:
    def test_create_ladder_rejects_empty_and_nonpositive(self):
        with pytest.raises(ValueError):
            engine.create_ladder("X", ())
        with pytest.raises(ValueError):
            engine.create_ladder("X", (5, 0))

    def test_get_stake_clamps_index(self):
        ladder = engine.create_ladder("L1", (5, 10, 15))
        assert engine.get_stake(ladder, -3) == 5
        assert engine.get_stake(ladder, 1) == 10
        assert engine.get_stake(ladder, 99) == 15

    def test_default_ladders_shape(self):
        l1, l2, l3 = presets.DEFAULT_LADDERS
        assert l1.stakes == (5, 10, 15, 25, 40, 65, 105, 170, 275)
        assert l2.stakes == (50, 100, 150, 250, 400, 650, 1050, 1750)
        assert l3.stakes == (
            500, 1000, 1500, 2500, 4000, 6500, 10500, 17000, 27500, 44500,
        )


class TestInitialState:
    def test_initial_state_defaults(self):
        state = engine.create_initial_state(_default_strategy())
        assert state.current_ladder == 0
        assert state.current_index == 0
        assert state.pnl == 0
        assert state.rounds == 0
        assert state.ladder_touches == (0, 0, 0)
        assert not state.stopped
        assert not state.awaiting_decision

    def test_starting_ladder_clamped(self):
        strategy = _default_strategy()
        assert engine.create_initial_state(strategy, 99).current_ladder == 2
        assert engine.create_initial_state(strategy, -5).current_ladder == 0


class TestStepping:
    def test_loss_steps_up_win_steps_down_two(self):
        strategy = _default_strategy()
        config = _config()
        state = engine.create_initial_state(strategy)

        state = _play(state, config, strategy, "LLL")
        assert state.current_index == 3
        assert state.pnl == -30  # -5 -10 -15

        state = _play(state, config, strategy, "W")
        assert state.current_index == 1
        assert state.pnl == -5  # won 25 at index 3

    def test_win_at_bottom_clamps_to_zero(self):
        strategy = _default_strategy()
        config = _config()
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "W")
        assert state.current_index == 0
        assert state.pnl == 5

    def test_tracking_metrics(self):
        strategy = _default_strategy()
        config = _config()
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "LWL")
        # bets: 5 (loss), 10 (win), 5 (loss) — index 0->1->0(-2 clamped)->1
        assert state.rounds == 3
        assert state.total_wagered == 20
        assert state.max_stake == 10
        assert state.pnl == -0  # -5 +10 -5
        assert state.peak_pnl == 5
        assert state.max_drawdown == 5
        assert state.ladder_touches == (3, 0, 0)


class TestBridging:
    def _climb_to_bridging(self, strategy, config):
        """Nine straight losses on L1: 8 climb to the top, the 9th bridges."""
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "L" * 8)
        assert state.current_index == 8
        assert state.pnl == -435
        state = _play(state, config, strategy, "L")
        return state

    def test_loss_at_top_pauses_for_decision(self):
        strategy = _default_strategy()
        config = _config()
        state = self._climb_to_bridging(strategy, config)
        assert state.pnl == -710
        assert state.awaiting_decision
        assert state.pending_decision_type == "bridging"
        assert state.top_touches == 1
        assert not state.stopped

    def test_bets_ignored_while_awaiting_decision(self):
        strategy = _default_strategy()
        config = _config()
        state = self._climb_to_bridging(strategy, config)
        assert engine.process_bet(state, config, strategy, True, "at_bridging_only") is state

    def test_carry_over_enters_recovery_with_target(self):
        strategy = _default_strategy()  # recovery 0.5, offset 0
        config = _config()
        state = self._climb_to_bridging(strategy, config)
        state = engine.process_bridging_decision(state, strategy, "carry_over")
        assert state.current_ladder == 1
        assert state.current_index == 0
        assert state.in_recovery
        assert state.recovery_target_pnl == pytest.approx(-355)  # -710 + 710*0.5
        assert not state.awaiting_decision

    def test_recovery_completion_resets_to_ladder_zero(self):
        strategy = _default_strategy()
        config = _config()
        state = self._climb_to_bridging(strategy, config)
        state = engine.process_bridging_decision(state, strategy, "carry_over")
        # Eight $50 wins at L2 index 0: -710 -> -310, crossing the -355 target.
        state = _play(state, config, strategy, "W" * 7)
        assert state.in_recovery
        assert state.pnl == -360
        state = _play(state, config, strategy, "W")
        assert state.pnl == -310
        assert not state.in_recovery
        assert state.recovery_target_pnl == 0
        assert state.current_ladder == 0
        assert state.current_index == 0

    def test_carry_over_in_profit_sets_target_to_current_pnl(self):
        strategy = _default_strategy()
        state = engine.create_initial_state(strategy)
        state = dataclasses.replace(
            state, pnl=100, awaiting_decision=True, pending_decision_type="bridging"
        )
        state = engine.process_bridging_decision(state, strategy, "carry_over")
        assert state.in_recovery
        assert state.recovery_target_pnl == 100

    def test_carry_over_while_in_recovery_keeps_target(self):
        strategy = _default_strategy()
        state = engine.create_initial_state(strategy)
        state = dataclasses.replace(
            state,
            current_ladder=1,
            current_index=7,
            pnl=-2000,
            in_recovery=True,
            recovery_target_pnl=-355,
            awaiting_decision=True,
            pending_decision_type="bridging",
        )
        state = engine.process_bridging_decision(state, strategy, "carry_over")
        assert state.recovery_target_pnl == -355
        assert state.current_ladder == 2

    def test_crossover_offset_applied_and_clamped(self):
        aggressive = presets.create_strategy_from_preset("aggressive")  # offset 2
        config = _config()
        state = self._climb_to_bridging(aggressive, config)
        state = engine.process_bridging_decision(state, aggressive, "carry_over")
        assert state.current_index == 2

        huge_offset = dataclasses.replace(aggressive, crossover_offset=99)
        state = self._climb_to_bridging(huge_offset, config)
        state = engine.process_bridging_decision(state, huge_offset, "carry_over")
        assert state.current_index == 7  # L2 max index

    def test_write_off_resets_and_keeps_pnl(self):
        strategy = _default_strategy()
        config = _config()
        state = self._climb_to_bridging(strategy, config)
        state = engine.process_bridging_decision(state, strategy, "write_off")
        assert state.current_ladder == 0
        assert state.current_index == 0
        assert state.pnl == -710
        assert not state.in_recovery
        assert not state.awaiting_decision

    def test_stop_session_decision(self):
        strategy = _default_strategy()
        config = _config()
        state = self._climb_to_bridging(strategy, config)
        state = engine.process_bridging_decision(state, strategy, "stop_session")
        assert state.stopped
        assert state.stop_reason == "user_stopped"

    def test_decision_ignored_when_not_awaiting(self):
        strategy = _default_strategy()
        state = engine.create_initial_state(strategy)
        assert engine.process_bridging_decision(state, strategy, "carry_over") is state


class TestStopReasons:
    def test_profit_target(self):
        strategy = _default_strategy()
        config = _config(profit_target=5)
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "W")
        assert state.stopped
        assert state.stop_reason == "profit_target"
        assert state.current_index == 0  # stops before stepping

    def test_stop_loss(self):
        strategy = _default_strategy()
        config = _config(stop_loss_abs=5)
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "L")
        assert state.stopped
        assert state.stop_reason == "stop_loss"

    def test_max_rounds(self):
        strategy = _default_strategy()
        config = _config(max_rounds=1)
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "L")
        assert state.stopped
        assert state.stop_reason == "max_rounds"

    def test_bankroll_exhausted(self):
        strategy = _default_strategy()
        config = _config(bankroll=4)
        state = engine.create_initial_state(strategy)
        state = engine.process_bet(state, config, strategy, True, "at_bridging_only")
        assert state.stopped
        assert state.stop_reason == "bankroll_exhausted"
        assert state.rounds == 0  # bet never placed

    def test_table_max(self):
        strategy = _default_strategy()
        config = _config(table_max=4)
        state = engine.create_initial_state(strategy)
        state = engine.process_bet(state, config, strategy, True, "at_bridging_only")
        assert state.stopped
        assert state.stop_reason == "table_limit"
        assert state.rounds == 0

    def test_loss_at_top_of_last_ladder_forces_table_limit(self):
        strategy = engine.StrategyConfig(
            ladders=(engine.create_ladder("only", (5, 10)),),
            bridging_policy="carry_over_index_delta",
            recovery_target_pct=0.5,
            crossover_offset=0,
        )
        config = _config()
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "LL")
        assert state.stopped
        assert state.stop_reason == "table_limit"
        assert state.top_touches == 1

    def test_stop_at_table_limit_policy_stops_without_decision(self):
        strategy = dataclasses.replace(
            _default_strategy(), bridging_policy="stop_at_table_limit"
        )
        config = _config()
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "L" * 9)
        assert state.stopped
        assert state.stop_reason == "table_limit"
        assert not state.awaiting_decision

    def test_bets_ignored_after_stop(self):
        strategy = _default_strategy()
        config = _config(max_rounds=1)
        state = engine.create_initial_state(strategy)
        state = _play(state, config, strategy, "L")
        assert engine.process_bet(state, config, strategy, True, "at_bridging_only") is state


class TestEveryBetMode:
    def test_every_bet_pauses_after_each_bet(self):
        strategy = _default_strategy()
        config = _config()
        state = engine.create_initial_state(strategy)
        state = engine.process_bet(state, config, strategy, False, "every_bet")
        assert state.awaiting_decision
        assert state.pending_decision_type == "every_bet"

    def test_every_bet_continue_clears_pause(self):
        strategy = _default_strategy()
        config = _config()
        state = engine.create_initial_state(strategy)
        state = engine.process_bet(state, config, strategy, False, "every_bet")
        state = engine.process_bridging_decision(state, strategy, "carry_over")
        assert not state.awaiting_decision
        assert not state.stopped

    def test_every_bet_stop_session(self):
        strategy = _default_strategy()
        config = _config()
        state = engine.create_initial_state(strategy)
        state = engine.process_bet(state, config, strategy, False, "every_bet")
        state = engine.process_bridging_decision(state, strategy, "stop_session")
        assert state.stopped
        assert state.stop_reason == "user_stopped"


class TestImmutability:
    def test_process_bet_does_not_mutate_input(self):
        strategy = _default_strategy()
        config = _config()
        before = engine.create_initial_state(strategy)
        snapshot = dataclasses.replace(before)
        engine.process_bet(before, config, strategy, False, "at_bridging_only")
        assert before == snapshot

    def test_state_is_frozen(self):
        state = engine.create_initial_state(_default_strategy())
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.pnl = 999  # type: ignore[misc]


class TestPresets:
    def test_all_seven_presets_present(self):
        assert set(presets.PRESETS) == {
            "default", "aggressive", "conservative", "moderate",
            "full_recovery", "quick_reset", "high_offset",
        }

    @pytest.mark.parametrize(
        "name,pct,offset",
        [
            ("default", 0.5, 0),
            ("aggressive", 0.75, 2),
            ("conservative", 0.25, 0),
            ("moderate", 0.5, 1),
            ("full_recovery", 1.0, 0),
            ("quick_reset", 0.1, 0),
            ("high_offset", 0.5, 3),
        ],
    )
    def test_preset_values(self, name, pct, offset):
        preset = presets.PRESETS[name]
        assert preset.recovery_target_pct == pct
        assert preset.crossover_offset == offset
        assert preset.bridging_policy == "carry_over_index_delta"

    def test_win_probability(self):
        assert presets.WIN_PROBABILITY == 0.4946

    def test_default_session_config(self):
        cfg = presets.DEFAULT_SESSION_CONFIG
        assert (cfg.bankroll, cfg.profit_target, cfg.stop_loss_abs, cfg.max_rounds) == (
            10000, 500, 1000, 5000,
        )

    def test_unknown_preset_raises(self):
        with pytest.raises(ValueError):
            presets.create_strategy_from_preset("nope")

    def test_stop_reason_text(self):
        assert engine.get_stop_reason_text("profit_target") == "Target Reached!"
        assert engine.get_stop_reason_text("user_stopped") == "Session Ended"
        assert engine.get_stop_reason_text(None) == "Session Active"
