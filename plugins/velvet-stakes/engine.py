"""Pure ladder-betting engine.

Line-for-line port of the Velvet Stakes TypeScript engine
(betting-companion/src/engine/{types,ladder,session}.ts). The check order in
process_bet — affordability, table max, stop checks, then step — must not be
reordered: statistical parity with the app depends on it.

All state objects are frozen dataclasses; every transition returns a new
state and never mutates its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

# StopReason values: profit_target | stop_loss | max_rounds | table_limit |
# bankroll_exhausted | user_stopped | None (still running).


@dataclass(frozen=True)
class LadderSpec:
    name: str
    stakes: tuple[float, ...]


@dataclass(frozen=True)
class StrategyConfig:
    ladders: tuple[LadderSpec, ...]
    bridging_policy: str  # advance_to_next_ladder_start | carry_over_index_delta | stop_at_table_limit
    recovery_target_pct: float  # 0.0-1.0
    crossover_offset: int


@dataclass(frozen=True)
class SessionConfig:
    bankroll: float
    profit_target: float
    stop_loss_abs: float
    max_rounds: int
    table_max: Optional[float] = None
    starting_ladder: int = 0


@dataclass(frozen=True)
class SessionState:
    current_ladder: int
    current_index: int
    pnl: float
    rounds: int
    total_wagered: float
    max_stake: float
    max_drawdown: float
    peak_pnl: float
    ladder_touches: tuple[int, ...]  # bet count per ladder, indexed by ladder
    top_touches: int
    stopped: bool
    stop_reason: Optional[str]
    in_recovery: bool
    recovery_target_pnl: float
    awaiting_decision: bool
    pending_decision_type: Optional[str]  # "bridging" | "every_bet"


@dataclass(frozen=True)
class BetRecord:
    round: int
    timestamp: int  # epoch ms
    ladder: int
    index: int
    stake: float
    won: bool
    pnl_after: float


@dataclass(frozen=True)
class SessionEvent:
    round: int
    timestamp: int  # epoch ms
    type: str  # "carry_over" | "write_off"
    pnl_at: float
    from_ladder: int
    to_ladder: int


def create_ladder(name: str, stakes: tuple[float, ...]) -> LadderSpec:
    if len(stakes) == 0:
        raise ValueError("Ladder must have at least one stake")
    if any(s <= 0 for s in stakes):
        raise ValueError("All stakes must be positive")
    return LadderSpec(name=name, stakes=tuple(stakes))


def get_max_index(ladder: LadderSpec) -> int:
    return len(ladder.stakes) - 1


def get_stake(ladder: LadderSpec, index: int) -> float:
    clamped = max(0, min(index, get_max_index(ladder)))
    return ladder.stakes[clamped]


def create_initial_state(strategy: StrategyConfig, starting_ladder: int = 0) -> SessionState:
    valid_starting_ladder = max(0, min(starting_ladder, len(strategy.ladders) - 1))
    return SessionState(
        current_ladder=valid_starting_ladder,
        current_index=0,
        pnl=0,
        rounds=0,
        total_wagered=0,
        max_stake=0,
        max_drawdown=0,
        peak_pnl=0,
        ladder_touches=tuple(0 for _ in strategy.ladders),
        top_touches=0,
        stopped=False,
        stop_reason=None,
        in_recovery=False,
        recovery_target_pnl=0,
        awaiting_decision=False,
        pending_decision_type=None,
    )


def get_current_stake(state: SessionState, strategy: StrategyConfig) -> float:
    return get_stake(strategy.ladders[state.current_ladder], state.current_index)


def get_current_bankroll(state: SessionState, config: SessionConfig) -> float:
    return config.bankroll + state.pnl


def can_afford_stake(state: SessionState, config: SessionConfig, strategy: StrategyConfig) -> bool:
    return get_current_bankroll(state, config) >= get_current_stake(state, strategy)


def exceeds_table_max(state: SessionState, config: SessionConfig, strategy: StrategyConfig) -> bool:
    if not config.table_max:
        return False
    return get_current_stake(state, strategy) > config.table_max


def process_bet(
    state: SessionState,
    config: SessionConfig,
    strategy: StrategyConfig,
    won: bool,
    decision_mode: str = "at_bridging_only",
) -> SessionState:
    """Process a bet result. Returns a new state; never mutates the input."""
    if state.stopped or state.awaiting_decision:
        return state

    stake = get_current_stake(state, strategy)

    if not can_afford_stake(state, config, strategy):
        return replace(state, stopped=True, stop_reason="bankroll_exhausted")

    if exceeds_table_max(state, config, strategy):
        return replace(state, stopped=True, stop_reason="table_limit")

    round_pnl = stake if won else -stake

    touches = list(state.ladder_touches)
    touches[state.current_ladder] += 1
    new_state = replace(
        state,
        pnl=state.pnl + round_pnl,
        rounds=state.rounds + 1,
        total_wagered=state.total_wagered + stake,
        max_stake=max(state.max_stake, stake),
        ladder_touches=tuple(touches),
    )

    new_state = replace(
        new_state,
        peak_pnl=max(new_state.peak_pnl, new_state.pnl),
        max_drawdown=max(new_state.max_drawdown, new_state.peak_pnl - new_state.pnl),
    )

    if new_state.pnl >= config.profit_target:
        return replace(new_state, stopped=True, stop_reason="profit_target")

    if -new_state.pnl >= config.stop_loss_abs:
        return replace(new_state, stopped=True, stop_reason="stop_loss")

    if new_state.rounds >= config.max_rounds:
        return replace(new_state, stopped=True, stop_reason="max_rounds")

    new_state = _step_index(new_state, strategy, won)

    if decision_mode == "every_bet" and not new_state.stopped and not new_state.awaiting_decision:
        new_state = replace(new_state, awaiting_decision=True, pending_decision_type="every_bet")

    return new_state


def _step_index(state: SessionState, strategy: StrategyConfig, won: bool) -> SessionState:
    """Win: index -2; loss: index +1; clamped. Losing at the top bridges."""
    current_ladder = strategy.ladders[state.current_ladder]
    max_index = get_max_index(current_ladder)
    at_top_before_step = state.current_index >= max_index

    new_index = state.current_index - 2 if won else state.current_index + 1

    if not won and at_top_before_step:
        return _handle_bridging(state, strategy)

    new_index = max(0, min(new_index, max_index))
    new_state = replace(state, current_index=new_index)

    if new_state.in_recovery and new_state.pnl >= new_state.recovery_target_pnl:
        new_state = replace(
            new_state,
            in_recovery=False,
            recovery_target_pnl=0,
            current_ladder=0,
            current_index=0,
        )

    return new_state


def _handle_bridging(state: SessionState, strategy: StrategyConfig) -> SessionState:
    at_last_ladder = state.current_ladder == len(strategy.ladders) - 1

    new_state = replace(state, top_touches=state.top_touches + 1)

    if strategy.bridging_policy == "stop_at_table_limit":
        return replace(new_state, stopped=True, stop_reason="table_limit")

    if at_last_ladder:
        return replace(new_state, stopped=True, stop_reason="table_limit")

    return replace(new_state, awaiting_decision=True, pending_decision_type="bridging")


def process_bridging_decision(
    state: SessionState, strategy: StrategyConfig, decision: str
) -> SessionState:
    """Apply the user's decision: carry_over | write_off | stop_session."""
    if not state.awaiting_decision:
        return state

    if state.pending_decision_type == "every_bet":
        if decision == "stop_session":
            return replace(
                state,
                stopped=True,
                stop_reason="user_stopped",
                awaiting_decision=False,
                pending_decision_type=None,
            )
        return replace(state, awaiting_decision=False, pending_decision_type=None)

    if decision == "stop_session":
        return replace(
            state,
            stopped=True,
            stop_reason="user_stopped",
            awaiting_decision=False,
            pending_decision_type=None,
        )

    if decision == "write_off":
        return replace(
            state,
            current_ladder=0,
            current_index=0,
            in_recovery=False,
            recovery_target_pnl=0,
            awaiting_decision=False,
            pending_decision_type=None,
        )

    if decision == "carry_over":
        return _execute_carry_over(state, strategy)

    return state


def _execute_carry_over(state: SessionState, strategy: StrategyConfig) -> SessionState:
    new_state = state

    if not new_state.in_recovery:
        if new_state.pnl < 0:
            recovery_amount = abs(new_state.pnl) * strategy.recovery_target_pct
            recovery_target_pnl = new_state.pnl + recovery_amount
        else:
            # Edge case: in profit, no recovery needed
            recovery_target_pnl = new_state.pnl
        new_state = replace(new_state, in_recovery=True, recovery_target_pnl=recovery_target_pnl)

    next_ladder = strategy.ladders[new_state.current_ladder + 1]
    clamped_offset = min(strategy.crossover_offset, get_max_index(next_ladder))

    return replace(
        new_state,
        current_ladder=new_state.current_ladder + 1,
        current_index=clamped_offset,
        awaiting_decision=False,
        pending_decision_type=None,
    )


def stop_session(state: SessionState) -> SessionState:
    """Manual user stop (the app's equivalent is the End Session button)."""
    if state.stopped:
        return state
    return replace(
        state,
        stopped=True,
        stop_reason="user_stopped",
        awaiting_decision=False,
        pending_decision_type=None,
    )


_STOP_REASON_TEXT = {
    "profit_target": "Target Reached!",
    "stop_loss": "Stop Loss Hit",
    "max_rounds": "Max Rounds Reached",
    "table_limit": "Table Limit Hit",
    "bankroll_exhausted": "Bankroll Exhausted",
    "user_stopped": "Session Ended",
}


def get_stop_reason_text(reason: Optional[str]) -> str:
    return _STOP_REASON_TEXT.get(reason or "", "Session Active")


def get_ladder_name(state: SessionState, strategy: StrategyConfig) -> str:
    return strategy.ladders[state.current_ladder].name
