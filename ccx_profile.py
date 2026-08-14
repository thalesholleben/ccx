#!/usr/bin/env python3
"""Inicia Claude Code ou Codex CLI em perfis de autenticação isolados.

Não consulta nem altera os slots globais do CCX. Cada perfil é um diretório
independente em ~/.ccx/profiles, usado somente pelo processo filho iniciado aqui.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROFILE_ROOT = Path.home() / ".ccx" / "profiles"


def profile_path(provider: str, slot: str) -> Path:
    if provider not in ("claude", "codex"):
        raise ValueError(f"Provedor inválido: {provider!r}")
    if not slot.isascii() or not slot.isdecimal() or int(slot) < 1:
        raise ValueError("Perfil precisa ser um número inteiro positivo.")
    return PROFILE_ROOT / provider / slot


def executable(provider: str) -> str:
    path = shutil.which(provider)
    if not path:
        raise FileNotFoundError(f"Executável '{provider}' não encontrado no PATH.")
    return path


def child_environment(provider: str, slot: str) -> dict[str, str]:
    env = os.environ.copy()
    path = profile_path(provider, slot)
    path.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"] = str(path)
    return env


def command(provider: str, executable_path: str, arguments: list[str], login: bool) -> list[str]:
    if provider == "claude":
        return [executable_path, "auth", "login"] if login else [executable_path, *arguments]
    prefix = [executable_path, "-c", 'cli_auth_credentials_store="file"']
    return [*prefix, "login"] if login else [*prefix, *arguments]


def run(provider: str, slot: str, arguments: list[str], *, login: bool = False) -> int:
    if not login and not arguments:
        raise ValueError("Use '--' seguido dos argumentos do cliente.")
    executable_path = executable(provider)
    result = subprocess.run(
        command(provider, executable_path, arguments, login),
        env=child_environment(provider, slot),
        check=False,
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="ccx_profile", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    login = sub.add_parser("login", help="faz login em um perfil isolado")
    login.add_argument("provider", choices=("claude", "codex"))
    login.add_argument("slot")

    run_cmd = sub.add_parser("run", help="executa o cliente em um perfil isolado")
    run_cmd.add_argument("provider", choices=("claude", "codex"))
    run_cmd.add_argument("slot")
    run_cmd.add_argument("arguments", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    arguments: list[str] = []
    if args.action == "run":
        if "--" not in argv:
            parser.error("Use '--' antes dos argumentos do cliente.")
        arguments = argv[argv.index("--") + 1 :]
    try:
        return run(args.provider, args.slot, arguments, login=args.action == "login")
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
