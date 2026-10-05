"""Run the configured Jev workflow with references resolved by 1Password CLI.

The service token is captured from the named macOS Keychain entry and only
supplied to op. The trading process receives the needed API keys, never that token.
This launcher is limited to paper and Binance testnet; it cannot enable mainnet.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .config import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
VAULT = "Codex"
SERVICE_ACCOUNT = "Codex lectura"
KEYCHAIN_SERVICE = "codex-1password-service"
KEYCHAIN_ACCOUNT = "leonardo-codex"
API_KEYS = {"TYPESAFE_API_KEY", "JEV_API_KEY", "JEV_API_SECRET", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
CONTROLLED = {"JEV_MODE", "JEV_USE_TESTNET", "JEV_LIVE_CONFIRM", "JEV_ENGINE", "JEV_HYBRID_CONFIRMER"}


class SetupError(Exception):
    """A safe, deliberately nonsensitive configuration error."""


def reference_file(command: str) -> Path:
    return ROOT / (".env.testnet.1password" if command == "live" else ".env.1password")


def validate_references(path: Path, command: str) -> None:
    if not path.is_file():
        raise SetupError("Falta el archivo de referencias de 1Password; consultá README.md.")
    values = load_dotenv(path)
    if values.get("JEV_EXCHANGE", "binance") != "binance":
        raise SetupError("Este lanzador sólo admite Binance testnet y sus datos públicos.")
    needed = {"TYPESAFE_API_KEY"}
    if command == "live":
        needed.update({"JEV_API_KEY", "JEV_API_SECRET"})
    for name, value in values.items():
        if name in CONTROLLED or name.startswith("OP_"):
            raise SetupError("El modo y el acceso a 1Password los controla el lanzador, no el archivo.")
        if name not in API_KEYS and not name.startswith("JEV_"):
            raise SetupError("El archivo contiene una variable ajena a Jev Trader.")
        if name in API_KEYS:
            if value and name not in needed:
                raise SetupError("El archivo incluye una credencial que este modo no necesita.")
            if value:
                parts = value.removeprefix("op://").split("/")
                if (not value.startswith("op://") or len(parts) < 3 or parts[0] != VAULT
                        or any(not part for part in parts) or "<" in value or ">" in value):
                    raise SetupError("Las claves deben ser referencias completas a la bóveda Codex, nunca valores reales.")
        elif "op://" in value:
            raise SetupError("Sólo las claves necesarias pueden contener referencias a secretos.")
    if any(not values.get(name) for name in needed):
        raise SetupError("Faltan referencias a las claves que necesita este modo.")


def _captured(args: list[str], env: dict[str, str]) -> str:
    """Never forward op/Keychain errors: they could contain sensitive output."""
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("No se pudo comprobar el acceso de 1Password; no se inició el bot.") from None
    if result.returncode:
        raise SetupError("Falta acceso autorizado a 1Password o al Llavero; no se inició el bot.")
    return result.stdout


def service_environment(op: str, command: str) -> dict[str, str]:
    # Discard alternate op credentials and ambient trading/API settings.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OP_", "JEV_")) and k not in API_KEYS}
    token = _captured([
        "/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
        "-a", KEYCHAIN_ACCOUNT, "-w",
    ], env).strip()
    if not token:
        raise SetupError("Falta el acceso de 1Password en el Llavero; no se inició el bot.")
    env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    try:
        identity = json.loads(_captured([op, "user", "get", "--me", "--format=json"], env))
        vaults = json.loads(_captured([op, "vault", "list", "--format=json"], env))
        matches = (isinstance(identity, dict) and identity.get("type") == "SERVICE_ACCOUNT"
                   and identity.get("name") == SERVICE_ACCOUNT and isinstance(vaults, list)
                   and len(vaults) == 1 and isinstance(vaults[0], dict) and vaults[0].get("name") == VAULT)
    except (ValueError, TypeError, KeyError):
        raise SetupError("No se pudo verificar la cuenta y la bóveda de 1Password.") from None
    if not matches:
        raise SetupError("Se requiere la cuenta Codex lectura con acceso únicamente a la bóveda Codex.")
    env.update(JEV_MODE="live" if command == "live" else "paper", JEV_USE_TESTNET="true",
               JEV_LIVE_CONFIRM="", JEV_ENGINE="hybrid", JEV_HYBRID_CONFIRMER="jev")
    return env


def run_command(op: str, path: Path, command: str, *, iterations: int | None,
                timeframe: str | None) -> list[str]:
    args = [op, "run", f"--env-file={path}", "--", "/usr/bin/env", "-u", "OP_SERVICE_ACCOUNT_TOKEN",
            sys.executable, "-m", "jev", command, "--env-file", str(path)]
    if timeframe:
        args.extend(["--timeframe", timeframe])
    if command == "live":
        args.extend(["--max-iterations", str(iterations or 1)])
        args.extend(["--journal", str(ROOT / "jev_testnet.sqlite3")])
    elif command == "paper" and iterations is not None:
        args.extend(["--max-iterations", str(iterations)])
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jev con 1Password: paper o testnet, sin secretos en archivos.")
    parser.add_argument("command", nargs="?", choices=("doctor", "paper", "live"), default="doctor")
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--timeframe")
    args = parser.parse_args(argv)
    if args.max_iterations is not None and args.max_iterations < 1:
        parser.error("--max-iterations debe ser positivo")
    if args.command == "doctor" and args.max_iterations is not None:
        parser.error("doctor no admite --max-iterations")
    try:
        op = shutil.which("op")
        if not op:
            raise SetupError("Falta instalar 1Password CLI (op); no se inició el bot.")
        path = reference_file(args.command)
        validate_references(path, args.command)
        env = service_environment(op, args.command)
        try:
            return subprocess.call(run_command(op, path, args.command, iterations=args.max_iterations,
                                               timeframe=args.timeframe), env=env, cwd=ROOT)
        finally:
            env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("No se pudo leer la configuración o iniciar 1Password; no se inició el bot.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
