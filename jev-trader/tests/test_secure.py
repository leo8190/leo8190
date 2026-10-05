"""Security boundaries of the 1Password launcher, using synthetic credentials only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev import secure


def refs(path: Path, live: bool = False) -> Path:
    names = ["TYPESAFE_API_KEY"] + (["JEV_API_KEY", "JEV_API_SECRET"] if live else [])
    path.write_text("\n".join(f'{name}="op://Codex/Jev Trader/{name}"' for name in names))
    return path


def captured_identity(monkeypatch, *, kind="SERVICE_ACCOUNT", name="Codex lectura", vaults=None):
    calls = []

    def captured(args, env):
        calls.append((args, dict(env)))
        if args[0] == "/usr/bin/security":
            return "synthetic-service-token\n"
        if "user" in args:
            return json.dumps({"type": kind, "name": name})
        return json.dumps(vaults if vaults is not None else [{"name": "Codex"}])

    monkeypatch.setattr(secure, "_captured", captured)
    return calls


@pytest.mark.parametrize("value", ["synthetic-plaintext-secret", "op://Private/Item/key", "op://Codex/Item",
                                 "op://Codex//key", "op://Codex/<item>/key"])
def test_invalid_references_never_echo_their_value(tmp_path, value):
    p = tmp_path / "refs.env"
    p.write_text(f"TYPESAFE_API_KEY={value}\n")
    with pytest.raises(secure.SetupError) as exc:
        secure.validate_references(p, "paper")
    assert value not in str(exc.value)


def test_paper_profile_cannot_fetch_binance_credentials(tmp_path):
    p = refs(tmp_path / "refs.env", live=True)
    with pytest.raises(secure.SetupError, match="no necesita"):
        secure.validate_references(p, "paper")
    secure.validate_references(p, "live")


@pytest.mark.parametrize("setting", ["JEV_MODE", "JEV_USE_TESTNET", "JEV_LIVE_CONFIRM", "OP_SERVICE_ACCOUNT_TOKEN"])
def test_env_file_cannot_override_network_or_service_auth(tmp_path, setting):
    p = refs(tmp_path / "refs.env")
    p.write_text(p.read_text() + f"\n{setting}=synthetic-value\n")
    with pytest.raises(secure.SetupError, match="controla el lanzador"):
        secure.validate_references(p, "paper")


def test_ref_file_cannot_fetch_a_secret_as_an_ordinary_setting(tmp_path):
    p = refs(tmp_path / "refs.env")
    p.write_text(p.read_text() + "\nJEV_SYMBOL=op://Codex/Other/item\n")
    with pytest.raises(secure.SetupError, match="Sólo las claves"):
        secure.validate_references(p, "paper")


def test_launcher_refuses_an_exchange_other_than_binance(tmp_path):
    p = refs(tmp_path / "refs.env", live=True)
    p.write_text(p.read_text() + "\nJEV_EXCHANGE=another-exchange\n")
    with pytest.raises(secure.SetupError, match="Binance testnet"):
        secure.validate_references(p, "live")


@pytest.mark.parametrize("identity", [
    {"kind": "USER"}, {"name": "Other account"}, {"vaults": [{"name": "Codex"}, {"name": "Private"}]},
])
def test_identity_or_vault_mismatch_stops_before_loading_api_keys(monkeypatch, identity):
    calls = captured_identity(monkeypatch, **identity)
    with pytest.raises(secure.SetupError, match="únicamente"):
        secure.service_environment("/fake/op", "live")
    assert all("run" not in args for args, env in calls)


def test_service_token_only_goes_to_op_and_ambient_access_is_discarded(monkeypatch):
    calls = captured_identity(monkeypatch)
    monkeypatch.setenv("OP_CONNECT_TOKEN", "synthetic-other-token")
    monkeypatch.setenv("OP_SESSION_other", "synthetic-session")
    monkeypatch.setenv("JEV_USE_TESTNET", "false")
    monkeypatch.setenv("JEV_API_KEY", "synthetic-api-key")
    env = secure.service_environment("/fake/op", "live")
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in calls[0][1]  # Keychain itself gets no token
    assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "synthetic-service-token"
    assert env["JEV_MODE"] == "live" and env["JEV_USE_TESTNET"] == "true" and env["JEV_LIVE_CONFIRM"] == ""
    assert not any(k in env for k in ("OP_CONNECT_TOKEN", "OP_SESSION_other", "JEV_API_KEY"))
    assert all("synthetic-service-token" not in arg for args, captured_env in calls for arg in args)


def test_keychain_error_is_not_forwarded(monkeypatch):
    monkeypatch.setattr(secure.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="synthetic-secret", stderr="synthetic-secret in provider error"))
    with pytest.raises(secure.SetupError) as exc:
        secure._captured(["/usr/bin/security"], {})
    assert "synthetic-secret" not in str(exc.value)


def test_live_launch_strips_vault_token_and_is_bounded_with_separate_journal(tmp_path):
    p = refs(tmp_path / "testnet.env", live=True)
    args = secure.run_command("/fake/op", p, "live", iterations=None, timeframe=None)
    assert args[:4] == ["/fake/op", "run", f"--env-file={p}", "--"]
    assert args[4:7] == ["/usr/bin/env", "-u", "OP_SERVICE_ACCOUNT_TOKEN"]
    assert args[args.index("--max-iterations") + 1] == "1"
    assert Path(args[args.index("--journal") + 1]).name == "jev_testnet.sqlite3"
    assert args[args.index("--env-file") + 1] == str(p)  # never falls back to the old .env


def test_missing_keychain_access_does_not_start_the_bot(tmp_path, monkeypatch, capsys):
    p = refs(tmp_path / "refs.env")
    monkeypatch.setattr(secure, "reference_file", lambda command: p)
    monkeypatch.setattr(secure.shutil, "which", lambda command: "/fake/op")
    monkeypatch.setattr(secure.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(secure.subprocess, "call", lambda *a, **kw: pytest.fail("Bot must not start"))
    assert secure.main(["doctor"]) == 1
    assert "Falta acceso autorizado" in capsys.readouterr().err


def test_successful_launch_clears_token_after_op_exits(tmp_path, monkeypatch):
    p = refs(tmp_path / "refs.env")
    captured_identity(monkeypatch)
    monkeypatch.setattr(secure, "reference_file", lambda command: p)
    monkeypatch.setattr(secure.shutil, "which", lambda command: "/fake/op")
    seen = []

    def call(args, env, cwd):
        seen.append(env)
        assert env["JEV_MODE"] == "paper" and env["JEV_USE_TESTNET"] == "true"
        assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "synthetic-service-token"
        return 0

    monkeypatch.setattr(secure.subprocess, "call", call)
    assert secure.main(["paper", "--max-iterations", "2"]) == 0
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in seen[0]
