"""Preset configurations, ported from betting-companion/src/engine/{presets,ladder}.ts."""

from __future__ import annotations

from dataclasses import dataclass

from .engine import SessionConfig, StrategyConfig, create_ladder

# Baccarat Banker-bet odds approximated as an even-money bet with the house
# edge (~1.06%) folded in — matches the app's Monte Carlo risk check.
WIN_PROBABILITY = 0.4946


@dataclass(frozen=True)
class PresetConfig:
    name: str
    display_name: str
    bridging_policy: str
    recovery_target_pct: float
    crossover_offset: int
    description: str


PRESETS: dict[str, PresetConfig] = {
    "default": PresetConfig(
        name="default",
        display_name="Default",
        bridging_policy="carry_over_index_delta",
        recovery_target_pct=0.5,
        crossover_offset=0,
        description="Balanced approach - recover 50% of losses",
    ),
    "aggressive": PresetConfig(
        name="aggressive",
        display_name="Aggressive",
        bridging_policy="carry_over_index_delta",
        recovery_target_pct=0.75,
        crossover_offset=2,
        description="Higher recovery target, start mid-ladder",
    ),
    "conservative": PresetConfig(
        name="conservative",
        display_name="Conservative",
        bridging_policy="carry_over_index_delta",
        recovery_target_pct=0.25,
        crossover_offset=0,
        description="Quick resets, lower variance",
    ),
    "moderate": PresetConfig(
        name="moderate",
        display_name="Moderate",
        bridging_policy="carry_over_index_delta",
        recovery_target_pct=0.5,
        crossover_offset=1,
        description="Balanced with slight offset",
    ),
    "full_recovery": PresetConfig(
        name="full_recovery",
        display_name="Full Recovery",
        bridging_policy="carry_over_index_delta",
        recovery_target_pct=1.0,
        crossover_offset=0,
        description="Must recover all losses before reset",
    ),
    "quick_reset": PresetConfig(
        name="quick_reset",
        display_name="Quick Reset",
        bridging_policy="carry_over_index_delta",
        recovery_target_pct=0.1,
        crossover_offset=0,
        description="Minimal recovery, fast resets",
    ),
    "high_offset": PresetConfig(
        name="high_offset",
        display_name="High Offset",
        bridging_policy="carry_over_index_delta",
        recovery_target_pct=0.5,
        crossover_offset=3,
        description="Standard recovery, start higher in next ladder",
    ),
}


DEFAULT_LADDERS: tuple = (
    create_ladder("L1", (5, 10, 15, 25, 40, 65, 105, 170, 275)),
    create_ladder("L2", (50, 100, 150, 250, 400, 650, 1050, 1750)),
    create_ladder(
        "L3", (500, 1000, 1500, 2500, 4000, 6500, 10500, 17000, 27500, 44500)
    ),
)


DEFAULT_SESSION_CONFIG = SessionConfig(
    bankroll=10000,
    profit_target=500,
    stop_loss_abs=1000,
    max_rounds=5000,
    starting_ladder=0,
)


def create_strategy_from_preset(
    preset_name: str, ladders: tuple = DEFAULT_LADDERS
) -> StrategyConfig:
    preset = PRESETS.get(preset_name)
    if preset is None:
        raise ValueError(f"Unknown preset: {preset_name}")
    return StrategyConfig(
        ladders=tuple(ladders),
        bridging_policy=preset.bridging_policy,
        recovery_target_pct=preset.recovery_target_pct,
        crossover_offset=preset.crossover_offset,
    )


def preset_names() -> tuple[str, ...]:
    return tuple(PRESETS.keys())
