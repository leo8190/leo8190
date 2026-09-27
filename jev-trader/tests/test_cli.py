from __future__ import annotations

import builtins
import os

import pytest

from jev import cli
from jev.cli import main
from jev.journal import Journal


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Run every CLI test in an empty directory with no JEV_* / Anthropic credentials."""
    monkeypatch.chdir(tmp_path)
    keys = {k for k in os.environ if k.startswith("JEV_")} | {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    for key in keys:
        # setenv first so the undo is recorded even when the variable did not exist
        # (the CLI may export ANTHROPIC_API_KEY from a .env file into os.environ).
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")  # never reach a real API
    return tmp_path


def test_backtest_synthetic_writes_html_report(tmp_path, capsys):
    report = tmp_path / "out" / "bt.html"
    code = main(["backtest", "--source", "synthetic", "--engine", "rules", "--candles", "300", "--report", str(report)])
    out = capsys.readouterr().out
    assert code == 0
    assert report.is_file() and "<html" in report.read_text(encoding="utf-8").lower()
    assert "Resumen de rendimiento" in out and str(report) in out
    assert "Datos sintéticos" in out


def test_backtest_default_report_path_is_under_reports(tmp_path):
    assert main(["backtest", "--engine", "rules", "--candles", "200"]) == 0
    assert (tmp_path / "reports" / "backtest.html").is_file()


def test_backtest_llm_cost_guard_requires_yes(tmp_path, capsys):
    report = tmp_path / "c.html"
    code = main(["backtest", "--engine", "claude", "--candles", "3000", "--max-llm-calls", "1000",
                 "--report", str(report)])
    captured = capsys.readouterr()
    assert code == 2 and not report.exists()
    assert "Coste LLM estimado" in captured.out and "--yes" in captured.err


def test_backtest_hybrid_without_key_degrades_to_hold(tmp_path, capsys):
    report = tmp_path / "h.html"
    code = main(["backtest", "--engine", "hybrid", "--candles", "150", "--max-llm-calls", "3", "--report", str(report)])
    captured = capsys.readouterr()
    assert code == 0 and report.is_file()
    assert "ANTHROPIC_API_KEY" in captured.err and "Traceback" not in captured.err
    assert "Límite de 3 llamadas" in captured.out


def test_backtest_csv_needs_a_path(capsys):
    assert main(["backtest", "--source", "csv"]) == 2
    assert "--csv" in capsys.readouterr().err


def test_backtest_from_csv(tmp_path, capsys):
    from jev.market.csv_source import save_candles
    from jev.market.synthetic import SyntheticMarket

    path = tmp_path / "data.csv"
    save_candles(path, SyntheticMarket(seed=1, initial_history=0).generate(150))
    code = main(["backtest", "--source", "csv", "--csv", str(path), "--engine", "rules", "--report",
                 str(tmp_path / "r.html")])
    assert code == 0 and "CSV" in capsys.readouterr().out


def test_decide_synthetic_rules(capsys):
    assert main(["decide", "--synthetic", "--engine", "rules"]) == 0
    out = capsys.readouterr().out
    assert "NO se envían órdenes" in out
    assert "Decisión" in out and "fuente rules" in out and "Latencia" in out and "Coste" in out


def test_decide_synthetic_claude_without_key_is_a_clean_fallback(capsys):
    assert main(["decide", "--synthetic", "--engine", "claude"]) == 0
    captured = capsys.readouterr()
    assert "HOLD" in captured.out and "fuente fallback" in captured.out
    assert "Traceback" not in captured.err and "ANTHROPIC_API_KEY" in captured.err


def test_paper_synthetic_fast_runs_and_status_reads_it(tmp_path, capsys):
    journal = tmp_path / "paper.sqlite3"
    code = main(["paper", "--synthetic", "--fast", "--max-iterations", "30", "--engine", "rules",
                 "--journal", str(journal)])
    out = capsys.readouterr().out
    assert code == 0 and "PAPER TRADING" in out and "Fin de la sesión: 30 pasos" in out
    with Journal(str(journal)) as j:
        assert j.summary()["decisions"] >= 25
        assert j.state_keys("engine:") == ["engine:paper:synthetic:BTC/USDT:5m"]

    assert main(["status", "--journal", str(journal), "--limit", "3"]) == 0
    status = capsys.readouterr().out
    assert "Decisiones" in status and "Últimas 3 decisiones" in status and "paper:synthetic" in status


def test_paper_fast_requires_synthetic(capsys):
    assert main(["paper", "--fast"]) == 2


def test_status_without_journal_does_not_create_one(tmp_path, capsys):
    assert main(["status"]) == 0
    assert "Todavía no hay journal" in capsys.readouterr().out
    assert not (tmp_path / "jev_journal.sqlite3").exists()


def test_status_on_an_empty_journal(tmp_path, capsys):
    path = tmp_path / "empty.sqlite3"
    Journal(str(path)).close()
    assert main(["status", "--journal", str(path)]) == 0
    assert "Decisiones   0" in capsys.readouterr().out


def test_status_reset_halt(tmp_path, capsys):
    from jev.portfolio import Portfolio

    path = str(tmp_path / "j.sqlite3")
    p = Portfolio("BTC/USDT", "USDT", "BTC")
    p.mark(1_767_225_600_000, 900.0)
    p.halt("max_drawdown")
    with Journal(path) as j:
        j.save_state("engine:live:x", {"portfolio": p.to_state(), "last_candle_ts": None})
    assert main(["status", "--journal", path, "--reset-halt"]) == 0
    assert "HALT max_drawdown" in capsys.readouterr().out
    with Journal(path) as j:
        assert j.load_state("engine:live:x")["portfolio"]["halted_reason"] is None
        assert any(e["kind"] == "halt_reset" for e in j.recent_events())


def test_live_is_refused_without_live_mode(capsys):
    assert main(["live"]) == 2
    assert "JEV_MODE=live" in capsys.readouterr().err


def test_live_mode_without_keys_is_a_config_error(monkeypatch, capsys):
    monkeypatch.setenv("JEV_MODE", "live")
    assert main(["live"]) == 2
    assert "JEV_API_KEY" in capsys.readouterr().err


def test_live_mainnet_requires_typing_the_symbol(monkeypatch, capsys):
    monkeypatch.setenv("JEV_MODE", "live")
    monkeypatch.setenv("JEV_USE_TESTNET", "false")
    monkeypatch.setenv("JEV_API_KEY", "key-123")
    monkeypatch.setenv("JEV_API_SECRET", "secret-456")
    monkeypatch.setenv("JEV_LIVE_CONFIRM", "YES_I_ACCEPT_REAL_MONEY_RISK")
    monkeypatch.setattr(builtins, "input", lambda prompt="": "ETH/USDT")
    created = []
    monkeypatch.setattr("jev.execution.ccxt_broker.CcxtBroker.__init__", lambda *a, **k: created.append(1))
    assert main(["live"]) == 2
    captured = capsys.readouterr()
    assert "MAINNET" in captured.out and "DINERO REAL" in captured.out
    assert created == []
    assert "key-123" not in captured.out + captured.err and "secret-456" not in captured.out + captured.err


def test_mainnet_confirmation_rules(monkeypatch):
    from argparse import Namespace

    from jev.config import Settings

    s = Settings(symbol="BTC/USDT", live_confirm="YES_I_ACCEPT_REAL_MONEY_RISK")
    assert cli._confirm_mainnet(Namespace(yes=True), s) is True
    monkeypatch.setattr(builtins, "input", lambda prompt="": "BTC/USDT")
    assert cli._confirm_mainnet(Namespace(yes=False), Settings(symbol="BTC/USDT")) is True

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    assert cli._confirm_mainnet(Namespace(yes=True), Settings(symbol="BTC/USDT")) is False


def test_download_rejects_bad_ranges_and_unknown_exchanges(tmp_path, monkeypatch, capsys):
    out = str(tmp_path / "x.csv")
    assert main(["download", "--since", "2024-02-01", "--until", "2024-01-01", "--out", out]) == 2
    monkeypatch.setenv("JEV_EXCHANGE", "no-such-exchange")
    assert main(["download", "--since", "2024-01-01", "--until", "2024-01-02", "--out", out]) == 1
    assert "no-such-exchange" in capsys.readouterr().err


def test_bad_arguments_and_bad_config_exit_2(monkeypatch, capsys):
    assert main(["download", "--since", "yesterday", "--out", "x.csv"]) == 2
    assert main(["nope"]) == 2
    monkeypatch.setenv("JEV_ENGINE", "magic")
    assert main(["decide", "--synthetic"]) == 2
    assert "JEV_ENGINE" in capsys.readouterr().err
    monkeypatch.delenv("JEV_ENGINE")
    monkeypatch.setenv("JEV_TIMEFRAME", "1M")
    assert main(["decide", "--synthetic", "--engine", "rules"]) == 2


def test_dotenv_with_inline_comments_and_anthropic_key(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-test-0000000000\nJEV_ENGINE=rules   # hybrid | claude | rules\n", encoding="utf-8"
    )
    assert main(["decide", "--synthetic"]) == 0
    captured = capsys.readouterr()
    assert "Motor        rules" in captured.out
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test-0000000000"
    assert "sk-ant-test" not in captured.out + captured.err


def test_help_exits_0(capsys):
    assert main(["--help"]) == 0
    assert "backtest" in capsys.readouterr().out
