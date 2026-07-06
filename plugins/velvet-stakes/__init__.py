"""Velvet Stakes plugin — ladder-betting session tracking over any chat channel.

Intercepts messages via the ``pre_gateway_dispatch`` hook: strict trigger
words (win/loss, carry/write/stop, status, odds, bet start/end) are handled
deterministically — engine step, persist, reply straight through the platform
adapter — and everything else falls through to normal Hermes chat.

The hook body is fully synchronous (the hook contract requires it: async
callbacks are not awaited). Only the reply send and the Monte Carlo run are
scheduled as asyncio tasks, after state is already persisted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from . import formatter, parser
from .engine import (
    BetRecord,
    SessionConfig,
    SessionEvent,
    create_initial_state,
    get_current_stake,
    process_bet,
    process_bridging_decision,
    stop_session,
)
from .monte_carlo import DEFAULT_RISK_CHECK_TRIALS, run_risk_check
from .presets import WIN_PROBABILITY, create_strategy_from_preset
from .prng import hash_to_seed
from .store import (
    ActiveSession,
    VelvetStakesStore,
    _config_to_json,
    _state_to_json,
    with_bet,
    with_event,
    with_state,
)

logger = logging.getLogger(__name__)

_store: Optional[VelvetStakesStore] = None


def _get_store() -> VelvetStakesStore:
    global _store
    if _store is None:
        _store = VelvetStakesStore()
    return _store


def _now_ms() -> int:
    return int(time.time() * 1000)


def _n_trials() -> int:
    raw = os.environ.get("VELVET_STAKES_TRIALS", "")
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_RISK_CHECK_TRIALS
    except ValueError:
        return DEFAULT_RISK_CHECK_TRIALS


def _session_key(source: Any) -> Optional[str]:
    try:
        from gateway.session import build_session_key

        return build_session_key(source, profile=getattr(source, "profile", None))
    except Exception as exc:
        logger.warning("velvet-stakes: could not build session key: %s", exc)
        return None


def _mode_for(session: Optional[ActiveSession]) -> str:
    if session is None or session.state.stopped:
        return parser.MODE_IDLE
    if session.state.awaiting_decision:
        return parser.MODE_DECIDING
    return parser.MODE_ACTIVE


# ---------------------------------------------------------------------------
# Replying without the LLM: send through the platform adapter, then skip.
# ---------------------------------------------------------------------------

async def _send(gateway: Any, event: Any, text: str) -> None:
    try:
        adapter = gateway._adapter_for_source(event.source)
        if adapter is None:
            logger.warning(
                "velvet-stakes: no adapter for source %s; reply dropped",
                getattr(event.source, "platform", None),
            )
            return
        try:
            metadata = gateway._thread_metadata_for_source(event.source)
        except Exception:
            metadata = None
        await adapter.send(event.source.chat_id, text, metadata=metadata)
    except Exception as exc:
        logger.warning("velvet-stakes: reply send failed: %s", exc)


def _reply_and_skip(gateway: Any, event: Any, text: str) -> dict:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send(gateway, event, text))
    except RuntimeError:
        logger.warning("velvet-stakes: no running event loop; reply dropped")
    return {"action": "skip", "reason": "velvet-stakes"}


# ---------------------------------------------------------------------------
# Command application
# ---------------------------------------------------------------------------

def _start_session(cmd: parser.Start, key: str, store: VelvetStakesStore) -> str:
    strategy = create_strategy_from_preset(cmd.preset)
    config = SessionConfig(
        bankroll=cmd.bankroll,
        profit_target=cmd.target,
        stop_loss_abs=cmd.stop_loss,
        max_rounds=5000,
        starting_ladder=0,
    )
    session = ActiveSession(
        preset_name=cmd.preset,
        config=config,
        strategy=strategy,
        state=create_initial_state(strategy, config.starting_ladder),
        bet_history=(),
        events=(),
        started_at=_now_ms(),
    )
    store.save_active(key, session)
    return formatter.render_start(session)


def _record_bet(
    cmd: parser.BetResult, key: str, session: ActiveSession, store: VelvetStakesStore
) -> str:
    state_before = session.state
    stake = get_current_stake(state_before, session.strategy)
    new_state = process_bet(
        state_before, session.config, session.strategy, cmd.won, "at_bridging_only"
    )

    if new_state.rounds > state_before.rounds:
        bet = BetRecord(
            round=new_state.rounds,
            timestamp=_now_ms(),
            ladder=state_before.current_ladder,
            index=state_before.current_index,
            stake=stake,
            won=cmd.won,
            pnl_after=new_state.pnl,
        )
        session = with_bet(session, new_state, bet)
    else:
        # Stopped before the bet was placed (bankroll/table-limit check).
        session = with_state(session, new_state)

    store.save_active(key, session)

    if new_state.stopped:
        store.finish(key, ended_at=_now_ms())
        return formatter.render_summary(session)
    return formatter.render_bet_reply(cmd.won, stake, state_before, session)


def _apply_decision(
    decision: str, key: str, session: ActiveSession, store: VelvetStakesStore
) -> str:
    state_before = session.state
    new_state = process_bridging_decision(state_before, session.strategy, decision)

    if decision in ("carry_over", "write_off") and not new_state.stopped:
        event = SessionEvent(
            round=state_before.rounds,
            timestamp=_now_ms(),
            type=decision,
            pnl_at=state_before.pnl,
            from_ladder=state_before.current_ladder,
            to_ladder=new_state.current_ladder,
        )
        session = with_event(session, new_state, event)
    else:
        session = with_state(session, new_state)

    store.save_active(key, session)

    if new_state.stopped:
        store.finish(key, ended_at=_now_ms())
        return formatter.render_summary(session)
    return formatter.render_decision_ack(decision, session)


def _end_session(key: str, session: ActiveSession, store: VelvetStakesStore) -> str:
    session = with_state(session, stop_session(session.state))
    store.save_active(key, session)
    store.finish(key, ended_at=_now_ms())
    return formatter.render_summary(session)


def _odds_seed(session: ActiveSession) -> int:
    snapshot = json.dumps(
        {
            "state": _state_to_json(session.state),
            "config": _config_to_json(session.config),
        },
        sort_keys=True,
    )
    return hash_to_seed(snapshot)


async def _odds_task(gateway: Any, event: Any, session: ActiveSession, n_trials: int) -> None:
    try:
        results = await asyncio.to_thread(
            run_risk_check,
            session.config,
            session.strategy,
            WIN_PROBABILITY,
            n_trials,
            _odds_seed(session),
            session.state,
        )
        await _send(gateway, event, formatter.render_odds(results, session))
    except Exception as exc:
        logger.warning("velvet-stakes: odds computation failed: %s", exc)
        await _send(gateway, event, "Odds check failed — see gateway logs.")


def _apply(
    cmd: parser.Command,
    key: str,
    session: Optional[ActiveSession],
    store: VelvetStakesStore,
    gateway: Any,
    event: Any,
) -> Optional[str]:
    if isinstance(cmd, parser.Start):
        return _start_session(cmd, key, store)
    if isinstance(cmd, parser.StartError):
        return cmd.message
    if isinstance(cmd, parser.Presets):
        return formatter.render_presets()
    if isinstance(cmd, parser.Help):
        return formatter.render_help()
    if isinstance(cmd, parser.Status):
        return formatter.render_status(session) if session else formatter.render_no_session()

    if session is None:
        return formatter.render_no_session()

    if isinstance(cmd, parser.AlreadyActive):
        return formatter.render_already_active(session)
    if isinstance(cmd, parser.BetResult):
        return _record_bet(cmd, key, session, store)
    if isinstance(cmd, parser.Decision):
        return _apply_decision(cmd.decision, key, session, store)
    if isinstance(cmd, parser.RejectedResult):
        return formatter.render_rejected(session)
    if isinstance(cmd, parser.End):
        return _end_session(key, session, store)
    if isinstance(cmd, parser.Odds):
        n_trials = _n_trials()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_odds_task(gateway, event, session, n_trials))
        except RuntimeError:
            return "Odds check unavailable (no event loop)."
        return formatter.render_odds_ack(n_trials)
    return None


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def on_pre_gateway_dispatch(
    event: Any = None, gateway: Any = None, session_store: Any = None, **kwargs: Any
) -> Optional[dict]:
    """Synchronous hook: consume strict triggers, fall through otherwise."""
    try:
        if event is None or gateway is None:
            return None
        text = getattr(event, "text", None)
        if not text or not isinstance(text, str):
            return None
        message_type = getattr(event, "message_type", None)
        if message_type is not None and getattr(message_type, "value", "text") != "text":
            return None
        source = getattr(event, "source", None)
        if source is None:
            return None

        # The hook fires BEFORE gateway authorization — self-gate or
        # unauthorized strangers could drive a betting session.
        try:
            if not gateway._is_user_authorized(source):
                return None
        except Exception:
            return None

        key = _session_key(source)
        if key is None:
            return None

        store = _get_store()
        session = store.get_active(key)
        cmd = parser.parse(text, _mode_for(session))
        if cmd is None:
            return None

        reply = _apply(cmd, key, session, store, gateway, event)
        if reply is None:
            return None
        return _reply_and_skip(gateway, event, reply)
    except Exception as exc:
        logger.warning("velvet-stakes hook error: %s", exc, exc_info=True)
        return None


def register(ctx: Any) -> None:
    """Called once by the plugin loader."""
    _get_store()  # restore persisted sessions at startup
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
