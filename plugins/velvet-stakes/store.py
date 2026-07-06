"""Persistence for velvet-stakes sessions.

Active sessions live in ``~/.hermes/velvet_stakes/active_sessions.json``
(one entry per gateway session key, so each chat has its own betting
session and sessions survive gateway restarts). Completed sessions are
appended to ``history.jsonl`` as SessionResult-shaped JSON matching the
Velvet Stakes app's types.ts — importable by the app as-is.

All JSON uses the app's camelCase field names; writes are atomic
(temp file + os.replace). The gateway is a single asyncio process and the
hook body is synchronous, so no cross-task locking is needed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .engine import (
    BetRecord,
    LadderSpec,
    SessionConfig,
    SessionEvent,
    SessionState,
    StrategyConfig,
)

logger = logging.getLogger(__name__)

ACTIVE_FILE = "active_sessions.json"
HISTORY_FILE = "history.jsonl"


@dataclass(frozen=True)
class ActiveSession:
    preset_name: str
    config: SessionConfig
    strategy: StrategyConfig
    state: SessionState
    bet_history: tuple
    events: tuple
    started_at: int  # epoch ms


def with_state(session: ActiveSession, state: SessionState) -> ActiveSession:
    return replace(session, state=state)


def with_bet(session: ActiveSession, state: SessionState, bet: BetRecord) -> ActiveSession:
    return replace(session, state=state, bet_history=session.bet_history + (bet,))


def with_event(session: ActiveSession, state: SessionState, event: SessionEvent) -> ActiveSession:
    return replace(session, state=state, events=session.events + (event,))


# ---------------------------------------------------------------------------
# JSON (de)serialization — camelCase, matching the app's types.ts
# ---------------------------------------------------------------------------

def _strategy_to_json(strategy: StrategyConfig) -> dict:
    return {
        "ladders": [
            {"name": ladder.name, "stakes": list(ladder.stakes)}
            for ladder in strategy.ladders
        ],
        "bridgingPolicy": strategy.bridging_policy,
        "recoveryTargetPct": strategy.recovery_target_pct,
        "crossoverOffset": strategy.crossover_offset,
    }


def _strategy_from_json(data: dict) -> StrategyConfig:
    return StrategyConfig(
        ladders=tuple(
            LadderSpec(name=entry["name"], stakes=tuple(entry["stakes"]))
            for entry in data["ladders"]
        ),
        bridging_policy=data["bridgingPolicy"],
        recovery_target_pct=data["recoveryTargetPct"],
        crossover_offset=data["crossoverOffset"],
    )


def _config_to_json(config: SessionConfig) -> dict:
    return {
        "bankroll": config.bankroll,
        "profitTarget": config.profit_target,
        "stopLossAbs": config.stop_loss_abs,
        "maxRounds": config.max_rounds,
        "tableMax": config.table_max,
        "startingLadder": config.starting_ladder,
    }


def _config_from_json(data: dict) -> SessionConfig:
    return SessionConfig(
        bankroll=data["bankroll"],
        profit_target=data["profitTarget"],
        stop_loss_abs=data["stopLossAbs"],
        max_rounds=data["maxRounds"],
        table_max=data.get("tableMax"),
        starting_ladder=data.get("startingLadder", 0),
    )


def _state_to_json(state: SessionState) -> dict:
    return {
        "currentLadder": state.current_ladder,
        "currentIndex": state.current_index,
        "pnl": state.pnl,
        "rounds": state.rounds,
        "totalWagered": state.total_wagered,
        "maxStake": state.max_stake,
        "maxDrawdown": state.max_drawdown,
        "peakPnl": state.peak_pnl,
        "ladderTouches": {str(i): n for i, n in enumerate(state.ladder_touches)},
        "topTouches": state.top_touches,
        "stopped": state.stopped,
        "stopReason": state.stop_reason,
        "inRecovery": state.in_recovery,
        "recoveryTargetPnl": state.recovery_target_pnl,
        "awaitingDecision": state.awaiting_decision,
        "pendingDecisionType": state.pending_decision_type,
    }


def _state_from_json(data: dict) -> SessionState:
    touches = data["ladderTouches"]
    return SessionState(
        current_ladder=data["currentLadder"],
        current_index=data["currentIndex"],
        pnl=data["pnl"],
        rounds=data["rounds"],
        total_wagered=data["totalWagered"],
        max_stake=data["maxStake"],
        max_drawdown=data["maxDrawdown"],
        peak_pnl=data["peakPnl"],
        ladder_touches=tuple(touches[str(i)] for i in range(len(touches))),
        top_touches=data["topTouches"],
        stopped=data["stopped"],
        stop_reason=data["stopReason"],
        in_recovery=data["inRecovery"],
        recovery_target_pnl=data["recoveryTargetPnl"],
        awaiting_decision=data["awaitingDecision"],
        pending_decision_type=data["pendingDecisionType"],
    )


def _bet_to_json(bet: BetRecord) -> dict:
    return {
        "round": bet.round,
        "timestamp": bet.timestamp,
        "ladder": bet.ladder,
        "index": bet.index,
        "stake": bet.stake,
        "won": bet.won,
        "pnlAfter": bet.pnl_after,
    }


def _bet_from_json(data: dict) -> BetRecord:
    return BetRecord(
        round=data["round"],
        timestamp=data["timestamp"],
        ladder=data["ladder"],
        index=data["index"],
        stake=data["stake"],
        won=data["won"],
        pnl_after=data["pnlAfter"],
    )


def _event_to_json(event: SessionEvent) -> dict:
    return {
        "round": event.round,
        "timestamp": event.timestamp,
        "type": event.type,
        "pnlAt": event.pnl_at,
        "fromLadder": event.from_ladder,
        "toLadder": event.to_ladder,
    }


def _event_from_json(data: dict) -> SessionEvent:
    return SessionEvent(
        round=data["round"],
        timestamp=data["timestamp"],
        type=data["type"],
        pnl_at=data["pnlAt"],
        from_ladder=data["fromLadder"],
        to_ladder=data["toLadder"],
    )


def _session_to_json(session: ActiveSession) -> dict:
    return {
        "presetName": session.preset_name,
        "startedAt": session.started_at,
        "config": _config_to_json(session.config),
        "strategy": _strategy_to_json(session.strategy),
        "state": _state_to_json(session.state),
        "betHistory": [_bet_to_json(b) for b in session.bet_history],
        "events": [_event_to_json(e) for e in session.events],
    }


def _session_from_json(data: dict) -> ActiveSession:
    return ActiveSession(
        preset_name=data["presetName"],
        config=_config_from_json(data["config"]),
        strategy=_strategy_from_json(data["strategy"]),
        state=_state_from_json(data["state"]),
        bet_history=tuple(_bet_from_json(b) for b in data.get("betHistory", [])),
        events=tuple(_event_from_json(e) for e in data.get("events", [])),
        started_at=data["startedAt"],
    )


def build_session_result(session: ActiveSession, ended_at: int) -> dict:
    """SessionResult-shaped dict (types.ts), ready for app import."""
    state = session.state
    return {
        "id": str(uuid.uuid4()),
        "startTime": session.started_at,
        "endTime": ended_at,
        "hitTarget": state.stop_reason == "profit_target",
        "hitStopLoss": state.stop_reason == "stop_loss",
        "hitMaxRounds": state.stop_reason == "max_rounds",
        "hitTableLimit": state.stop_reason == "table_limit",
        "bankrollExhausted": state.stop_reason == "bankroll_exhausted",
        "userStopped": state.stop_reason == "user_stopped",
        "finalPnl": state.pnl,
        "roundsPlayed": state.rounds,
        "totalWagered": state.total_wagered,
        "maxStakeSeen": state.max_stake,
        "maxDrawdown": state.max_drawdown,
        "ladderTouches": {str(i): n for i, n in enumerate(state.ladder_touches)},
        "topOfLadderTouches": state.top_touches,
        "finalLadder": state.current_ladder,
        "finalIndex": state.current_index,
        "config": _config_to_json(session.config),
        "strategy": _strategy_to_json(session.strategy),
        "betHistory": [_bet_to_json(b) for b in session.bet_history],
        "events": [_event_to_json(e) for e in session.events],
    }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class VelvetStakesStore:
    """Write-through store: in-memory dict cache + atomic JSON persistence."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        if base_dir is None:
            from hermes_constants import get_hermes_home

            base_dir = Path(get_hermes_home()) / "velvet_stakes"
        self._base_dir = Path(base_dir)
        self._active: dict[str, ActiveSession] = {}
        self._load()

    @property
    def history_path(self) -> Path:
        return self._base_dir / HISTORY_FILE

    def _load(self) -> None:
        path = self._base_dir / ACTIVE_FILE
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._active = {
                key: _session_from_json(entry) for key, entry in data.items()
            }
        except Exception as exc:
            logger.warning("velvet-stakes: could not load %s: %s", path, exc)
            self._active = {}

    def _persist(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {key: _session_to_json(s) for key, s in self._active.items()}, indent=2
        )
        fd, tmp_path = tempfile.mkstemp(dir=str(self._base_dir), prefix=".active-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_path, self._base_dir / ACTIVE_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_active(self, key: str) -> Optional[ActiveSession]:
        return self._active.get(key)

    def save_active(self, key: str, session: ActiveSession) -> None:
        self._active = {**self._active, key: session}
        self._persist()

    def discard_active(self, key: str) -> None:
        if key in self._active:
            self._active = {k: v for k, v in self._active.items() if k != key}
            self._persist()

    def finish(self, key: str, ended_at: int) -> Optional[dict]:
        """Complete the session: append SessionResult to history, drop active."""
        session = self._active.get(key)
        if session is None:
            return None
        result = build_session_result(session, ended_at)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(result) + "\n")
        self.discard_active(key)
        return result
