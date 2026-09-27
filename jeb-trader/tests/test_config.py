from __future__ import annotations

import pytest

from jeb.config import load_dotenv, load_settings
from jeb.models import ConfigError


def test_dotenv_strips_inline_comments_quotes_and_export(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "JEB_ENGINE=hybrid            # hybrid | claude | rules\n"
        'JEB_SYMBOL="ETH/USDT"  # quoted\n'
        "JEB_LIVE_CONFIRM='a # b'\n"
        "export JEB_TIMEFRAME=15m\n"
        "JEB_API_KEY=abc#def\n"
        "EMPTY=\n"
        "not a pair\n",
        encoding="utf-8",
    )
    values = load_dotenv(env)
    assert values["JEB_ENGINE"] == "hybrid"
    assert values["JEB_SYMBOL"] == "ETH/USDT"
    assert values["JEB_LIVE_CONFIRM"] == "a # b"
    assert values["JEB_TIMEFRAME"] == "15m"
    assert values["JEB_API_KEY"] == "abc#def"  # '#' without a preceding space is part of the value
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
        load_settings(env={"JEB_ENGINE": "hybrid # oops"})
