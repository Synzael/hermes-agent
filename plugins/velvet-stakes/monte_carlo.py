"""Monte Carlo risk check, ported from betting-companion/src/engine/monte-carlo.ts.

Extends the reference implementation with one capability the texting UX
needs: trials can start from an arbitrary mid-session state ("what are my
odds from here?") instead of always starting fresh. With the default fresh
start the aggregation is numerically identical to the app's runMonteCarlo
(proven by the golden-fixture parity tests).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from .engine import (
    SessionConfig,
    SessionState,
    StrategyConfig,
    create_initial_state,
    process_bet,
    process_bridging_decision,
)

# 2,000 pure-Python trials keep the "odds" reply under a couple of seconds;
# override with the VELVET_STAKES_TRIALS env var (read by the plugin hook).
DEFAULT_RISK_CHECK_TRIALS = 2000


@dataclass(frozen=True)
class RiskCheckResults:
    n_trials: int
    prob_hit_target: float
    prob_hit_stop_loss: float
    prob_hit_max_rounds: float
    prob_hit_table_limit: float
    prob_bankroll_exhausted: float
    mean_pnl: float
    median_pnl: float
    pnl_p5: float
    pnl_p95: float
    median_rounds: float
    median_rounds_to_target: Optional[float]
    mean_max_drawdown: float
    median_max_drawdown: float
    mean_max_stake: float
    prob_reached_bridging: float


def decision_for_policy(policy: str) -> str:
    """Auto-answer for the engine's bridging pause, as the app's simulator does."""
    if policy == "carry_over_index_delta":
        return "carry_over"
    if policy == "advance_to_next_ladder_start":
        return "write_off"
    return "stop_session"


def simulate_to_completion(
    state: SessionState,
    config: SessionConfig,
    strategy: StrategyConfig,
    win_probability: float,
    rng: Callable[[], float],
) -> SessionState:
    """Run one session to termination from the given state.

    Identical to the reference simulateOneSession when handed a fresh
    initial state; a pending bridging decision is resolved per policy first.
    """
    decision = decision_for_policy(strategy.bridging_policy)

    if state.awaiting_decision:
        state = process_bridging_decision(state, strategy, decision)

    max_iterations = max(0, config.max_rounds - state.rounds) * 2 + 100
    i = 0
    while not state.stopped and i < max_iterations:
        state = process_bet(
            state, config, strategy, rng() < win_probability, "at_bridging_only"
        )
        if state.awaiting_decision:
            state = process_bridging_decision(state, strategy, decision)
        i += 1

    return state


def _quantile_sorted(sorted_values: list, q: float) -> float:
    """Linear-interpolation quantile of a pre-sorted ascending list."""
    if not sorted_values:
        return 0
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _mean(values: list) -> float:
    if not values:
        return 0
    return sum(values) / len(values)


def run_risk_check(
    config: SessionConfig,
    strategy: StrategyConfig,
    win_probability: float,
    n_trials: int,
    seed: int,
    initial_state: Optional[SessionState] = None,
) -> RiskCheckResults:
    """Run the Monte Carlo and aggregate results (mirrors runMonteCarlo)."""
    if n_trials <= 0:
        raise ValueError("n_trials must be positive")
    if win_probability < 0 or win_probability > 1:
        raise ValueError("win_probability must be within [0, 1]")

    from .prng import mulberry32

    rng = mulberry32(seed)

    pnls: list = []
    rounds: list = []
    drawdowns: list = []
    max_stakes: list = []
    rounds_to_target: list = []
    hit_target = 0
    hit_stop_loss = 0
    hit_max_rounds = 0
    hit_table_limit = 0
    bankroll_exhausted = 0
    reached_bridging = 0

    for _ in range(n_trials):
        start = (
            initial_state
            if initial_state is not None
            else create_initial_state(strategy, config.starting_ladder)
        )
        final = simulate_to_completion(start, config, strategy, win_probability, rng)

        pnls.append(final.pnl)
        rounds.append(final.rounds)
        drawdowns.append(final.max_drawdown)
        max_stakes.append(final.max_stake)
        if final.top_touches > 0:
            reached_bridging += 1

        if final.stop_reason == "profit_target":
            hit_target += 1
            rounds_to_target.append(final.rounds)
        elif final.stop_reason == "stop_loss":
            hit_stop_loss += 1
        elif final.stop_reason == "table_limit":
            hit_table_limit += 1
        elif final.stop_reason == "bankroll_exhausted":
            bankroll_exhausted += 1
        else:
            # max_rounds, or a defensive-cap exit without a stop reason.
            hit_max_rounds += 1

    sorted_pnls = sorted(pnls)
    sorted_rounds = sorted(rounds)
    sorted_drawdowns = sorted(drawdowns)
    sorted_rounds_to_target = sorted(rounds_to_target)

    return RiskCheckResults(
        n_trials=n_trials,
        prob_hit_target=hit_target / n_trials,
        prob_hit_stop_loss=hit_stop_loss / n_trials,
        prob_hit_max_rounds=hit_max_rounds / n_trials,
        prob_hit_table_limit=hit_table_limit / n_trials,
        prob_bankroll_exhausted=bankroll_exhausted / n_trials,
        mean_pnl=_mean(pnls),
        median_pnl=_quantile_sorted(sorted_pnls, 0.5),
        pnl_p5=_quantile_sorted(sorted_pnls, 0.05),
        pnl_p95=_quantile_sorted(sorted_pnls, 0.95),
        median_rounds=_quantile_sorted(sorted_rounds, 0.5),
        median_rounds_to_target=(
            _quantile_sorted(sorted_rounds_to_target, 0.5)
            if sorted_rounds_to_target
            else None
        ),
        mean_max_drawdown=_mean(drawdowns),
        median_max_drawdown=_quantile_sorted(sorted_drawdowns, 0.5),
        mean_max_stake=_mean(max_stakes),
        prob_reached_bridging=reached_bridging / n_trials,
    )
