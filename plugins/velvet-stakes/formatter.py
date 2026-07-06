"""Plain-text reply rendering for the velvet-stakes plugin.

No markdown — replies must render identically on Telegram, WhatsApp, and
Signal. Every reply is one short block.
"""

from __future__ import annotations

from .engine import (
    SessionState,
    StrategyConfig,
    get_current_stake,
    get_ladder_name,
    get_max_index,
    get_stake,
    get_stop_reason_text,
)
from .monte_carlo import RiskCheckResults
from .parser import PRESETS
from .store import ActiveSession


def money(value: float) -> str:
    if value == int(value):
        return f"${int(value):,}"
    return f"${value:,.2f}"


def signed(value: float) -> str:
    if value < 0:
        return f"-{money(-value)}"
    if value > 0:
        return f"+{money(value)}"
    return "$0"


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _position(state: SessionState, strategy: StrategyConfig) -> str:
    return f"{get_ladder_name(state, strategy)} #{state.current_index + 1}"


def render_help() -> str:
    return (
        "Velvet Stakes commands:\n"
        "bet start [preset] [bankroll] [target] [stoploss] — start a session\n"
        "bet presets — list presets\n"
        "During a session: win/w/1/yes or loss/l/0/no to record a bet,\n"
        "status, odds, stop/end to finish.\n"
        "At a ladder top: carry / write / stop."
    )


def render_presets() -> str:
    lines = ["Presets:"]
    for preset in PRESETS.values():
        lines.append(
            f"{preset.name} — recover {_pct(preset.recovery_target_pct)}, "
            f"offset {preset.crossover_offset} — {preset.description}"
        )
    return "\n".join(lines)


def render_no_session() -> str:
    return (
        "No active session. Start one with: "
        "bet start [preset] [bankroll] [target] [stoploss]"
    )


def render_already_active(session: ActiveSession) -> str:
    state = session.state
    return (
        f"A session is already running (P&L {signed(state.pnl)}, "
        f"round {state.rounds}). Send 'status' or finish it with 'stop'."
    )


def render_start(session: ActiveSession) -> str:
    state, strategy, config = session.state, session.strategy, session.config
    return (
        f"Session started ({session.preset_name}). "
        f"Bankroll {money(config.bankroll)}, target +{money(config.profit_target)}, "
        f"stop loss -{money(config.stop_loss_abs)}.\n"
        f"First bet: {money(get_current_stake(state, strategy))} "
        f"at {_position(state, strategy)}. Reply win or loss after each hand."
    )


def render_bet_ack(won: bool, stake: float, session: ActiveSession) -> str:
    state, strategy, config = session.state, session.strategy, session.config
    outcome = f"WIN +{money(stake)}" if won else f"LOSS -{money(stake)}"
    parts = [
        outcome,
        f"P&L {signed(state.pnl)}",
        f"{_position(state, strategy)} → next bet {money(get_current_stake(state, strategy))}",
        f"bankroll {money(config.bankroll + state.pnl)}",
        f"round {state.rounds}",
    ]
    line = " | ".join(parts)
    if state.in_recovery:
        line += f"\nRecovery mode: get back to {signed(state.recovery_target_pnl)}."
    return line


def render_bet_reply(
    won: bool, stake: float, state_before: SessionState, session: ActiveSession
) -> str:
    """Full reply for a recorded bet, covering pause/recovery transitions."""
    state = session.state
    if state.awaiting_decision:
        outcome = f"WIN +{money(stake)}" if won else f"LOSS -{money(stake)}"
        return (
            f"{outcome} | P&L {signed(state.pnl)} | round {state.rounds}\n"
            + render_bridging_prompt(session)
        )
    reply = render_bet_ack(won, stake, session)
    if state_before.in_recovery and not state.in_recovery and not state.stopped:
        reply = render_recovery_complete(session) + "\n" + reply
    return reply


def render_decision_ack(decision: str, session: ActiveSession) -> str:
    state, strategy = session.state, session.strategy
    stake = money(get_current_stake(state, strategy))
    if decision == "carry_over":
        return (
            f"Carried over to {_position(state, strategy)} at {stake}. "
            f"Recovery: get back to {signed(state.recovery_target_pnl)}."
        )
    return f"Loss written off. Back to {_position(state, strategy)} at {stake}."


def render_recovery_complete(session: ActiveSession) -> str:
    state, strategy = session.state, session.strategy
    return (
        f"Recovery target hit! Back to {_position(state, strategy)} "
        f"at {money(get_current_stake(state, strategy))}."
    )


def render_bridging_prompt(session: ActiveSession) -> str:
    state, strategy = session.state, session.strategy
    current = strategy.ladders[state.current_ladder]
    top_stake = current.stakes[-1]
    next_ladder = strategy.ladders[state.current_ladder + 1]
    offset = min(strategy.crossover_offset, get_max_index(next_ladder))
    carry_stake = get_stake(next_ladder, offset)

    if state.in_recovery:
        recovery_note = f"stay in recovery to {signed(state.recovery_target_pnl)}"
    elif state.pnl < 0:
        target = state.pnl + abs(state.pnl) * strategy.recovery_target_pct
        recovery_note = f"recover {_pct(strategy.recovery_target_pct)} = back to {signed(target)}"
    else:
        recovery_note = "no recovery needed"

    base = strategy.ladders[0]
    return (
        f"Lost at top of {current.name} ({money(top_stake)}). P&L {signed(state.pnl)}.\n"
        f"Reply: carry (→ {next_ladder.name} {money(carry_stake)}, {recovery_note}) "
        f"/ write (reset to {base.name} {money(base.stakes[0])}) / stop"
    )


def render_rejected(session: ActiveSession) -> str:
    return (
        "Ladder top reached — decide first: carry / write / stop.\n"
        + render_bridging_prompt(session)
    )


def render_status(session: ActiveSession) -> str:
    state, strategy, config = session.state, session.strategy, session.config
    if state.awaiting_decision:
        return "Awaiting your decision.\n" + render_bridging_prompt(session)
    lines = [
        f"{_position(state, strategy)} | next bet {money(get_current_stake(state, strategy))} "
        f"| P&L {signed(state.pnl)} | round {state.rounds}",
        f"bankroll {money(config.bankroll + state.pnl)} | "
        f"target +{money(config.profit_target)} | stop loss -{money(config.stop_loss_abs)}",
    ]
    if state.in_recovery:
        lines.append(f"Recovery mode: get back to {signed(state.recovery_target_pnl)}.")
    return "\n".join(lines)


def render_summary(session: ActiveSession) -> str:
    state = session.state
    return (
        f"{get_stop_reason_text(state.stop_reason)}\n"
        f"Final P&L {signed(state.pnl)} over {state.rounds} rounds | "
        f"wagered {money(state.total_wagered)} | max bet {money(state.max_stake)} | "
        f"max drawdown {money(state.max_drawdown)} | ladder tops hit {state.top_touches}\n"
        f"Saved to history. Start again with: bet start"
    )


def render_odds_ack(n_trials: int) -> str:
    return f"Crunching odds ({n_trials:,} simulated sessions)…"


def render_odds(results: RiskCheckResults, session: ActiveSession) -> str:
    config = session.config
    state = session.state
    other = (
        results.prob_hit_max_rounds
        + results.prob_hit_table_limit
        + results.prob_bankroll_exhausted
    )
    lines = [
        f"Odds from here ({results.n_trials:,} sims, seed-stable):",
        f"target +{money(config.profit_target)}: {_pct(results.prob_hit_target)} | "
        f"stop loss: {_pct(results.prob_hit_stop_loss)} | other: {_pct(other)}",
        f"median final P&L {signed(results.median_pnl)} "
        f"(P5 {signed(results.pnl_p5)} / P95 {signed(results.pnl_p95)})",
        f"median rounds at finish: {round(results.median_rounds)} (now: {state.rounds})",
    ]
    if state.awaiting_decision:
        lines.append("Note: sims auto-answer the pending decision per your preset policy.")
    return "\n".join(lines)
