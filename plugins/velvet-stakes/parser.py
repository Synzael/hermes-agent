"""Strict, deterministic trigger grammar for the velvet-stakes plugin.

Whole-message matching only — a message either exactly matches a trigger for
the current mode or the parser returns None and the text falls through to
normal Hermes chat. No LLM is involved on the hot path.

Modes:
  idle     — no active session for this chat; only ``bet ...`` commands match
  active   — session running; bare win/loss tokens record bets
  deciding — ladder-top bridging pause; the trigger set switches entirely so
             yes/no can never be misread as a bet result
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union

from .presets import DEFAULT_SESSION_CONFIG, PRESETS

MODE_IDLE = "idle"
MODE_ACTIVE = "active"
MODE_DECIDING = "deciding"

WIN_TOKENS = frozenset({"win", "won", "w", "1", "yes", "y"})
LOSS_TOKENS = frozenset({"loss", "lose", "lost", "l", "0", "no", "n"})
_END_PHRASES = frozenset({"stop", "end", "end session", "stop session", "bet end"})
_CARRY_PHRASES = frozenset({"carry", "carry over", "c"})
_WRITE_PHRASES = frozenset({"write", "write off", "write-off", "wo"})


@dataclass(frozen=True)
class Start:
    preset: str
    bankroll: float
    target: float
    stop_loss: float


@dataclass(frozen=True)
class StartError:
    message: str


@dataclass(frozen=True)
class AlreadyActive:
    pass


@dataclass(frozen=True)
class BetResult:
    won: bool


@dataclass(frozen=True)
class Decision:
    decision: str  # carry_over | write_off | stop_session


@dataclass(frozen=True)
class RejectedResult:
    """Win/loss token sent while a bridging decision is pending."""


@dataclass(frozen=True)
class End:
    pass


@dataclass(frozen=True)
class Status:
    pass


@dataclass(frozen=True)
class Odds:
    pass


@dataclass(frozen=True)
class Presets:
    pass


@dataclass(frozen=True)
class Help:
    pass


Command = Union[
    Start, StartError, AlreadyActive, BetResult, Decision, RejectedResult,
    End, Status, Odds, Presets, Help,
]


def _normalize(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text.strip().lower())
    return collapsed.rstrip(".,!?")


def _parse_amount(token: str) -> Optional[float]:
    cleaned = token.lstrip("$").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _parse_start_args(args: list[str]) -> Union[Start, StartError]:
    preset = "default"
    remaining = list(args)

    if remaining and _parse_amount(remaining[0]) is None:
        candidate = remaining.pop(0)
        if candidate not in PRESETS:
            return StartError(
                f"Unknown preset '{candidate}'. Presets: {', '.join(PRESETS)}"
            )
        preset = candidate

    if len(remaining) > 3:
        return StartError(
            "Too many values. Usage: bet start [preset] [bankroll] [target] [stoploss]"
        )

    defaults = (
        DEFAULT_SESSION_CONFIG.bankroll,
        DEFAULT_SESSION_CONFIG.profit_target,
        DEFAULT_SESSION_CONFIG.stop_loss_abs,
    )
    values = []
    for i, default in enumerate(defaults):
        if i < len(remaining):
            amount = _parse_amount(remaining[i])
            if amount is None:
                return StartError(
                    f"'{remaining[i]}' is not a positive number. "
                    "Usage: bet start [preset] [bankroll] [target] [stoploss]"
                )
            values.append(amount)
        else:
            values.append(default)

    return Start(preset=preset, bankroll=values[0], target=values[1], stop_loss=values[2])


def _parse_bet_command(normalized: str, mode: str) -> Optional[Command]:
    """Handle ``bet ...`` prefixed messages, shared across modes."""
    parts = normalized.split(" ")
    if parts[0] != "bet" or len(parts) < 2:
        return None
    subcommand, args = parts[1], parts[2:]

    if subcommand == "start":
        if mode != MODE_IDLE:
            return AlreadyActive()
        return _parse_start_args(args)
    if args:
        return None
    if subcommand == "presets":
        return Presets()
    if subcommand == "status":
        return Status()
    if subcommand == "help":
        return Help()
    if subcommand == "odds":
        return Odds() if mode != MODE_IDLE else Status()
    if subcommand == "end":
        return End() if mode == MODE_ACTIVE else (
            Decision("stop_session") if mode == MODE_DECIDING else Status()
        )
    return None


def parse(text: str, mode: str) -> Optional[Command]:
    """Parse a message under the given mode; None means 'not for us'."""
    normalized = _normalize(text)
    if not normalized:
        return None

    bet_command = _parse_bet_command(normalized, mode)
    if bet_command is not None:
        return bet_command

    if mode == MODE_IDLE:
        return None

    if mode == MODE_ACTIVE:
        if normalized in WIN_TOKENS:
            return BetResult(won=True)
        if normalized in LOSS_TOKENS:
            return BetResult(won=False)
        if normalized == "odds":
            return Odds()
        if normalized == "status":
            return Status()
        if normalized in _END_PHRASES:
            return End()
        return None

    # MODE_DECIDING: the trigger set switches entirely — win/loss tokens are
    # consumed with a reminder, never recorded (yes/no would be ambiguous).
    if normalized in _CARRY_PHRASES:
        return Decision("carry_over")
    if normalized in _WRITE_PHRASES:
        return Decision("write_off")
    if normalized in _END_PHRASES:
        return Decision("stop_session")
    if normalized in WIN_TOKENS or normalized in LOSS_TOKENS:
        return RejectedResult()
    if normalized == "odds":
        return Odds()
    if normalized == "status":
        return Status()
    return None
