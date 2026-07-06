"""Monte Carlo + PRNG tests for the velvet-stakes plugin.

The golden fixture (tests/fixtures/velvet_stakes_parity.json) was generated
by running the reference TypeScript engine directly (see the plugin README
for the regen script), so these tests prove bit-level parity between the
Python port and the app's own risk check.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from tests.plugins._velvet_stakes import load_module

engine = load_module("engine")
presets = load_module("presets")
prng = load_module("prng")
monte_carlo = load_module("monte_carlo")

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "velvet_stakes_parity.json").read_text()
)


def _default_setup():
    strategy = presets.create_strategy_from_preset("default")
    config = presets.DEFAULT_SESSION_CONFIG
    return config, strategy


class TestPrngParity:
    @pytest.mark.parametrize("seed", [0, 42, 12345])
    def test_mulberry32_matches_reference(self, seed):
        rng = prng.mulberry32(seed)
        expected = FIXTURE["prngSamples"][str(seed)]
        assert [rng() for _ in range(20)] == expected

    def test_hash_to_seed_matches_reference(self):
        for text, expected in FIXTURE["hashVectors"].items():
            assert prng.hash_to_seed(text) == expected

    def test_floats_in_unit_interval(self):
        rng = prng.mulberry32(987654321)
        assert all(0 <= rng() < 1 for _ in range(1000))


class TestSessionParity:
    def test_simulated_sessions_match_reference(self):
        config, strategy = _default_setup()
        for entry in FIXTURE["sessions"]:
            rng = prng.mulberry32(entry["seed"])
            state = monte_carlo.simulate_to_completion(
                engine.create_initial_state(strategy, config.starting_ladder),
                config,
                strategy,
                presets.WIN_PROBABILITY,
                rng,
            )
            ref = entry["final"]
            assert state.pnl == pytest.approx(ref["pnl"], rel=1e-12), entry["seed"]
            assert state.rounds == ref["rounds"]
            assert state.stop_reason == ref["stopReason"]
            assert state.total_wagered == pytest.approx(ref["totalWagered"], rel=1e-12)
            assert state.max_stake == ref["maxStake"]
            assert state.max_drawdown == pytest.approx(ref["maxDrawdown"], rel=1e-12)
            assert state.top_touches == ref["topTouches"]
            expected_touches = tuple(
                ref["ladderTouches"][str(i)] for i in range(len(strategy.ladders))
            )
            assert state.ladder_touches == expected_touches


class TestRiskCheckParity:
    def test_full_run_matches_reference(self):
        config, strategy = _default_setup()
        results = monte_carlo.run_risk_check(
            config, strategy, presets.WIN_PROBABILITY, n_trials=2500, seed=12345
        )
        ref = FIXTURE["monteCarlo"]
        pairs = {
            "prob_hit_target": "probHitTarget",
            "prob_hit_stop_loss": "probHitStopLoss",
            "prob_hit_max_rounds": "probHitMaxRounds",
            "prob_hit_table_limit": "probHitTableLimit",
            "prob_bankroll_exhausted": "probBankrollExhausted",
            "mean_pnl": "meanPnl",
            "median_pnl": "medianPnl",
            "pnl_p5": "pnlP5",
            "pnl_p95": "pnlP95",
            "median_rounds": "medianRounds",
            "mean_max_drawdown": "meanMaxDrawdown",
            "median_max_drawdown": "medianMaxDrawdown",
            "mean_max_stake": "meanMaxStake",
            "prob_reached_bridging": "probReachedBridging",
        }
        for py_name, ts_name in pairs.items():
            assert getattr(results, py_name) == pytest.approx(
                ref[ts_name], rel=1e-12
            ), py_name
        if ref["medianRoundsToTarget"] is None:
            assert results.median_rounds_to_target is None
        else:
            assert results.median_rounds_to_target == pytest.approx(
                ref["medianRoundsToTarget"], rel=1e-12
            )


class TestRiskCheckBehaviour:
    def test_deterministic_for_same_seed(self):
        config, strategy = _default_setup()
        a = monte_carlo.run_risk_check(config, strategy, 0.4946, n_trials=200, seed=7)
        b = monte_carlo.run_risk_check(config, strategy, 0.4946, n_trials=200, seed=7)
        assert a == b

    def test_different_seed_differs(self):
        config, strategy = _default_setup()
        a = monte_carlo.run_risk_check(config, strategy, 0.4946, n_trials=200, seed=7)
        b = monte_carlo.run_risk_check(config, strategy, 0.4946, n_trials=200, seed=8)
        assert a != b

    def test_probabilities_sum_to_one(self):
        config, strategy = _default_setup()
        r = monte_carlo.run_risk_check(config, strategy, 0.4946, n_trials=300, seed=3)
        total = (
            r.prob_hit_target
            + r.prob_hit_stop_loss
            + r.prob_hit_max_rounds
            + r.prob_hit_table_limit
            + r.prob_bankroll_exhausted
        )
        assert total == pytest.approx(1.0)

    def test_invalid_params_rejected(self):
        config, strategy = _default_setup()
        with pytest.raises(ValueError):
            monte_carlo.run_risk_check(config, strategy, 0.5, n_trials=0, seed=1)
        with pytest.raises(ValueError):
            monte_carlo.run_risk_check(config, strategy, 1.5, n_trials=10, seed=1)

    def test_mid_state_start_respects_remaining_rounds(self):
        config, strategy = _default_setup()
        near_end = dataclasses.replace(
            engine.create_initial_state(strategy),
            rounds=config.max_rounds - 5,
            pnl=-100,
        )
        r = monte_carlo.run_risk_check(
            config, strategy, 0.4946, n_trials=100, seed=11, initial_state=near_end
        )
        # Every trial ends within 5 more rounds; pnl percentiles are absolute
        # session P&L continuing from -100.
        assert r.median_rounds <= config.max_rounds
        assert r.median_rounds >= config.max_rounds - 5
        assert r.pnl_p95 <= -100 + 5 * 275  # can't gain more than 5 top stakes

    def test_awaiting_decision_start_auto_resolves(self):
        config, strategy = _default_setup()
        state = engine.create_initial_state(strategy)
        # Drive to the bridging pause with real losses.
        for _ in range(9):
            state = engine.process_bet(state, config, strategy, False, "at_bridging_only")
        assert state.awaiting_decision
        r = monte_carlo.run_risk_check(
            config, strategy, 0.4946, n_trials=50, seed=5, initial_state=state
        )
        assert r.n_trials == 50
        assert r.prob_reached_bridging == 1.0  # carried-in top_touches counts

    def test_stopped_state_returns_immediately(self):
        config, strategy = _default_setup()
        stopped = dataclasses.replace(
            engine.create_initial_state(strategy),
            stopped=True,
            stop_reason="user_stopped",
            pnl=42,
        )
        final = monte_carlo.simulate_to_completion(
            stopped, config, strategy, 0.4946, prng.mulberry32(1)
        )
        assert final is stopped
