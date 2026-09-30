from __future__ import annotations

import pytest

from jev.config import load_dotenv, load_settings
from jev.models import ConfigError


def test_dotenv_strips_inline_comments_quotes_and_export(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "JEV_ENGINE=hybrid            # hybrid | claude | rules\n"
        'JEV_SYMBOL="ETH/USDT"  # quoted\n'
        "JEV_LIVE_CONFIRM='a # b'\n"
        "export JEV_TIMEFRAME=15m\n"
        "JEV_API_KEY=abc#def\n"
        "EMPTY=\n"
        "not a pair\n",
        encoding="utf-8",
    )
    values = load_dotenv(env)
    assert values["JEV_ENGINE"] == "hybrid"
    assert values["JEV_SYMBOL"] == "ETH/USDT"
    assert values["JEV_LIVE_CONFIRM"] == "a # b"
    assert values["JEV_TIMEFRAME"] == "15m"
    assert values["JEV_API_KEY"] == "abc#def"  # '#' without a preceding space is part of the value
    assert values["EMPTY"] == ""
    assert "not a pair" not in values


def test_env_example_loads_as_is(tmp_path, monkeypatch):
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    env = load_dotenv(tmp_path / ".env")
    settings = load_settings(env=env)
    assert settings.mode == "paper" and settings.use_testnet and settings.engine == "hybrid"
    assert settings.risk.flatten_on_kill is True and settings.risk.max_trades_per_day == 10


def test_settings_validation_still_applies(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(env={"JEV_ENGINE": "hybrid # oops"})


@pytest.mark.parametrize("value", ["si", "sí", "ture", "enabled", "yes please"])
def test_unrecognized_booleans_are_rejected_not_false(value):
    # regression safety-7 / runtime-4: a typo silently meant False (mainnet / no flatten)
    if value in ("si", "sí"):
        s = load_settings(env={"JEV_USE_TESTNET": value, "JEV_FLATTEN_ON_KILL": value})
        assert s.use_testnet is True and s.risk.flatten_on_kill is True
        return
    for var in ("JEV_USE_TESTNET", "JEV_FLATTEN_ON_KILL"):
        with pytest.raises(ConfigError, match=var):
            load_settings(env={var: value})


def test_known_booleans_and_live_max_capital():
    s = load_settings(env={"JEV_USE_TESTNET": "FALSE", "JEV_MODE": "paper", "JEV_FLATTEN_ON_KILL": "0",
                           "JEV_LIVE_MAX_CAPITAL": "250"})
    assert s.use_testnet is False and s.risk.flatten_on_kill is False and s.live_max_capital == 250.0
    assert load_settings(env={}).live_max_capital == 0.0
    with pytest.raises(ConfigError, match="JEV_LIVE_MAX_CAPITAL"):
        load_settings(env={"JEV_LIVE_MAX_CAPITAL": "-5"})


def test_jev_engine_is_selectable():
    # regression runtime-9: JEV_ENGINE=jev was a configuration error
    from jev.brain.factory import build_engine
    from jev.brain.jev_engine import JevDecisionEngine

    s = load_settings(env={"JEV_ENGINE": "jev", "JEV_MAX_POSITION_PCT": "40"})
    engine = build_engine(s)
    assert isinstance(engine, JevDecisionEngine) and engine.max_position_pct == 40.0
    assert engine.round_trip_cost_pct == pytest.approx(2 * (s.fee_pct + s.slippage_pct))


def test_jev_is_the_default_brain():
    from jev.config import load_settings

    s = load_settings(env={})
    assert (s.engine, s.hybrid_confirmer, s.heartbeat_candles) == ("hybrid", "jev", 1)
    assert (s.jev_model, s.jev_timeout_s, s.jev_price_per_mtok_input) == ("jev-latest", 3.0, 0.042)
    assert s.claude_model == "claude-haiku-4-5"


def test_ai_env_vars_are_parsed():
    from jev.config import load_settings

    s = load_settings(env={
        "JEV_ENGINE": "jev", "JEV_MODEL": "jev-1.13", "JEV_TIMEOUT_S": "1.5",
        "JEV_PRICE_PER_MTOK_INPUT": "0.05", "JEV_HYBRID_CONFIRMER": "claude",
        "JEV_HYBRID_HEARTBEAT_CANDLES": "4", "JEV_CLAUDE_MODEL": "claude-sonnet-5",
        "JEV_CLAUDE_TIMEOUT_S": "6", "JEV_CLAUDE_MAX_TOKENS": "300", "JEV_AI_MAX_RETRIES": "0",
        "JEV_MAX_AI_COST_USD_PER_DAY": "0.25",
    })
    assert (s.engine, s.jev_model, s.jev_timeout_s, s.jev_price_per_mtok_input) == ("jev", "jev-1.13", 1.5, 0.05)
    assert (s.hybrid_confirmer, s.heartbeat_candles) == ("claude", 4)
    assert (s.claude_model, s.claude_timeout_s, s.claude_max_tokens) == ("claude-sonnet-5", 6.0, 300)
    assert (s.ai_max_retries, s.max_ai_cost_usd_per_day) == (0, 0.25)


def test_invalid_hybrid_confirmer_is_rejected():
    import pytest

    from jev.config import load_settings
    from jev.models import ConfigError

    with pytest.raises(ConfigError, match="JEV_HYBRID_CONFIRMER"):
        load_settings(env={"JEV_HYBRID_CONFIRMER": "gpt"})
