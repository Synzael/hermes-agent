"""Trigger-grammar tests for the velvet-stakes parser.

The parser is strict and deterministic: whole-message matches only, mode
dependent, and anything unmatched returns None so it falls through to
normal Hermes chat.
"""

import pytest

from tests.plugins._velvet_stakes import load_module

parser = load_module("parser")

IDLE = parser.MODE_IDLE
ACTIVE = parser.MODE_ACTIVE
DECIDING = parser.MODE_DECIDING

WIN_TOKENS = ["win", "won", "w", "1", "yes", "y"]
LOSS_TOKENS = ["loss", "lose", "lost", "l", "0", "no", "n"]


class TestNormalization:
    @pytest.mark.parametrize("text", ["WIN", "Win!", "  win  ", "win.", "WIN!!"])
    def test_case_whitespace_punctuation(self, text):
        cmd = parser.parse(text, ACTIVE)
        assert isinstance(cmd, parser.BetResult)
        assert cmd.won is True

    def test_internal_whitespace_collapsed(self):
        assert isinstance(parser.parse("end   session", ACTIVE), parser.End)


class TestIdleMode:
    def test_bare_triggers_not_consumed_when_idle(self):
        for text in WIN_TOKENS + LOSS_TOKENS + ["status", "odds", "stop", "end"]:
            assert parser.parse(text, IDLE) is None, text

    def test_chitchat_not_consumed(self):
        for text in ["hello", "bet on it", "1pm works", "winter is coming", "help"]:
            assert parser.parse(text, IDLE) is None, text

    def test_bet_start_defaults(self):
        cmd = parser.parse("bet start", IDLE)
        assert cmd == parser.Start(
            preset="default", bankroll=10000, target=500, stop_loss=1000
        )

    def test_bet_start_preset_only(self):
        cmd = parser.parse("bet start aggressive", IDLE)
        assert cmd.preset == "aggressive"
        assert cmd.bankroll == 10000

    def test_bet_start_full_args(self):
        cmd = parser.parse("bet start conservative 5000 250 500", IDLE)
        assert cmd == parser.Start(
            preset="conservative", bankroll=5000, target=250, stop_loss=500
        )

    def test_bet_start_numbers_without_preset(self):
        cmd = parser.parse("bet start 2000 100 300", IDLE)
        assert cmd == parser.Start(
            preset="default", bankroll=2000, target=100, stop_loss=300
        )

    def test_bet_start_accepts_currency_formatting(self):
        cmd = parser.parse("bet start default $10,000 $500 $1,000", IDLE)
        assert (cmd.bankroll, cmd.target, cmd.stop_loss) == (10000, 500, 1000)

    def test_bet_start_bad_preset_is_error(self):
        cmd = parser.parse("bet start yolo", IDLE)
        assert isinstance(cmd, parser.StartError)
        assert "yolo" in cmd.message

    def test_bet_start_bad_numbers_are_errors(self):
        assert isinstance(parser.parse("bet start default -5", IDLE), parser.StartError)
        assert isinstance(parser.parse("bet start default 0", IDLE), parser.StartError)
        assert isinstance(
            parser.parse("bet start default 100 200 300 400", IDLE), parser.StartError
        )

    def test_bet_subcommands(self):
        assert isinstance(parser.parse("bet presets", IDLE), parser.Presets)
        assert isinstance(parser.parse("bet status", IDLE), parser.Status)
        assert isinstance(parser.parse("bet help", IDLE), parser.Help)

    def test_unknown_bet_subcommand_falls_through(self):
        assert parser.parse("bet me something", IDLE) is None


class TestActiveMode:
    @pytest.mark.parametrize("token", WIN_TOKENS)
    def test_win_tokens(self, token):
        cmd = parser.parse(token, ACTIVE)
        assert isinstance(cmd, parser.BetResult) and cmd.won is True

    @pytest.mark.parametrize("token", LOSS_TOKENS)
    def test_loss_tokens(self, token):
        cmd = parser.parse(token, ACTIVE)
        assert isinstance(cmd, parser.BetResult) and cmd.won is False

    def test_control_commands(self):
        assert isinstance(parser.parse("odds", ACTIVE), parser.Odds)
        assert isinstance(parser.parse("bet odds", ACTIVE), parser.Odds)
        assert isinstance(parser.parse("status", ACTIVE), parser.Status)
        assert isinstance(parser.parse("bet status", ACTIVE), parser.Status)
        assert isinstance(parser.parse("stop", ACTIVE), parser.End)
        assert isinstance(parser.parse("end", ACTIVE), parser.End)
        assert isinstance(parser.parse("end session", ACTIVE), parser.End)
        assert isinstance(parser.parse("stop session", ACTIVE), parser.End)
        assert isinstance(parser.parse("bet end", ACTIVE), parser.End)
        assert isinstance(parser.parse("bet help", ACTIVE), parser.Help)

    def test_start_while_active(self):
        assert isinstance(parser.parse("bet start", ACTIVE), parser.AlreadyActive)

    def test_multiword_and_sentences_fall_through(self):
        for text in ["i think i won", "1pm works", "no way jose", "wins"]:
            assert parser.parse(text, ACTIVE) is None, text


class TestDecidingMode:
    @pytest.mark.parametrize("token", ["carry", "carry over", "c"])
    def test_carry(self, token):
        cmd = parser.parse(token, DECIDING)
        assert cmd == parser.Decision("carry_over")

    @pytest.mark.parametrize("token", ["write", "write off", "write-off", "wo"])
    def test_write_off(self, token):
        cmd = parser.parse(token, DECIDING)
        assert cmd == parser.Decision("write_off")

    @pytest.mark.parametrize("token", ["stop", "end", "stop session", "end session"])
    def test_stop(self, token):
        cmd = parser.parse(token, DECIDING)
        assert cmd == parser.Decision("stop_session")

    @pytest.mark.parametrize("token", WIN_TOKENS + LOSS_TOKENS)
    def test_result_tokens_rejected_not_recorded(self, token):
        """yes/no are ambiguous mid-decision — consumed with a reminder."""
        assert isinstance(parser.parse(token, DECIDING), parser.RejectedResult)

    def test_status_and_odds_allowed(self):
        assert isinstance(parser.parse("status", DECIDING), parser.Status)
        assert isinstance(parser.parse("odds", DECIDING), parser.Odds)

    def test_other_text_falls_through(self):
        assert parser.parse("what should i do?", DECIDING) is None
