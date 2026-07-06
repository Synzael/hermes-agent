# Velvet Stakes plugin

Track [Velvet Stakes](https://game-agnostic-betting-simulator.vercel.app) ladder-betting sessions by texting Hermes from any gateway channel (Telegram, WhatsApp, Signal, …). Bets are recorded with **strict, deterministic trigger words** — no LLM in the hot path, so replies are instant and never misinterpreted. Anything that isn't an exact trigger falls through to normal Hermes chat.

The betting engine is a line-for-line Python port of the app's TypeScript engine (`betting-companion/src/engine/`), proven bit-exact by a golden parity fixture — same ladder stepping, bridging/recovery rules, presets, and Monte Carlo odds as the app.

## Enable

```bash
hermes plugins enable velvet-stakes
```

Then restart the gateway.

## Commands

No active session:

| Text | Effect |
|---|---|
| `bet start [preset] [bankroll] [target] [stoploss]` | Start a session (defaults: `default 10000 500 1000`) |
| `bet presets` | List presets |
| `bet help` | Cheat sheet |

Session active (bare words, whole message):

| Text | Effect |
|---|---|
| `win` `won` `w` `1` `yes` `y` | Record a win → reply with P&L and next stake |
| `loss` `lose` `lost` `l` `0` `no` `n` | Record a loss |
| `status` | Position, next bet, P&L, bankroll |
| `odds` | Monte Carlo risk check from the current state |
| `stop` / `end` | End the session → summary |

At a ladder top (bridging decision pending), the trigger set **switches** — win/loss words are refused with a reminder so `yes`/`no` can never be misread:

| Text | Effect |
|---|---|
| `carry` / `c` | Carry over to the next ladder (recovery mode) |
| `write` / `wo` | Write off the loss, reset to ladder 1 |
| `stop` / `end` | End the session |

## Storage

- Active sessions: `~/.hermes/velvet_stakes/active_sessions.json` (one per chat; survives gateway restarts)
- Completed sessions: `~/.hermes/velvet_stakes/history.jsonl` — one app-compatible `SessionResult` JSON per line, importable into the app's `betting-history:v1` localStorage store

## Config

- `VELVET_STAKES_TRIALS` — Monte Carlo trial count for `odds` (default 2000). The run executes in a worker thread; an ack is sent immediately.

## Notes

- The gateway hook fires before authorization, so the plugin self-gates on `_is_user_authorized` — unauthorized senders are ignored entirely.
- In group chats the default session-key rules apply (one shared betting session per group thread).
- Parity fixture (`tests/fixtures/velvet_stakes_parity.json`) regeneration: in the Velvet Stakes repo's `betting-companion/`, drop a vitest spec under `src/engine/` that dumps `mulberry32` samples, `hashToSeed` vectors, 25 `simulateOneSession` finals (seeds 1–25), and a `runMonteCarlo` run (2500 trials, seed 12345) for the default preset, run `npx vitest run`, and commit the JSON here. See `tests/plugins/test_velvet_stakes_monte_carlo.py` for the expected shape.
