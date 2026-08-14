#!/usr/bin/env python3
"""Checagem mínima do launcher de perfis: python test_ccx_profile.py"""

import os
import tempfile
import unittest.mock as mock
from pathlib import Path

import ccx_profile


def test_profile_path_valida_slot_sem_travessia():
    root = ccx_profile.PROFILE_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        ccx_profile.PROFILE_ROOT = Path(tmp)
        try:
            assert ccx_profile.profile_path("claude", "2") == Path(tmp) / "claude" / "2"
            for slot in ("0", "../2", "dois"):
                try:
                    ccx_profile.profile_path("codex", slot)
                    raise AssertionError(f"deveria recusar {slot!r}")
                except ValueError:
                    pass
        finally:
            ccx_profile.PROFILE_ROOT = root


def test_command_codex_forca_store_em_arquivo():
    assert ccx_profile.command("claude", "claude.exe", ["-p", "oi"], False) == [
        "claude.exe",
        "-p",
        "oi",
    ]
    assert ccx_profile.command("codex", "codex.exe", ["--model", "gpt"], False) == [
        "codex.exe",
        "-c",
        'cli_auth_credentials_store="file"',
        "--model",
        "gpt",
    ]
    assert ccx_profile.command("codex", "codex.exe", [], True) == [
        "codex.exe",
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
    ]


def test_run_isola_apenas_o_processo_filho():
    root = ccx_profile.PROFILE_ROOT
    original = os.environ.get("CODEX_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        ccx_profile.PROFILE_ROOT = Path(tmp)
        try:
            with (
                mock.patch.object(ccx_profile, "executable", return_value="codex.exe"),
                mock.patch.object(ccx_profile.subprocess, "run") as child,
            ):
                child.return_value.returncode = 7
                assert ccx_profile.run("codex", "3", ["--version"]) == 7
            command, = child.call_args.args
            env = child.call_args.kwargs["env"]
            assert command[:3] == ["codex.exe", "-c", 'cli_auth_credentials_store="file"']
            assert env["CODEX_HOME"] == str(Path(tmp) / "codex" / "3")
            assert os.environ.get("CODEX_HOME") == original
        finally:
            ccx_profile.PROFILE_ROOT = root


def test_run_exige_argumentos_e_executavel():
    try:
        ccx_profile.run("claude", "1", [])
        raise AssertionError("deveria exigir argumentos")
    except ValueError:
        pass
    with mock.patch.object(ccx_profile, "run", return_value=0) as launcher:
        assert ccx_profile.main(["run", "claude", "1", "--", "--version"]) == 0
    launcher.assert_called_once_with("claude", "1", ["--version"], login=False)
    try:
        ccx_profile.main(["run", "claude", "1", "--version"])
        raise AssertionError("deveria exigir separador --")
    except SystemExit as exc:
        assert exc.code == 2
    with mock.patch.object(ccx_profile.shutil, "which", return_value=None):
        try:
            ccx_profile.executable("claude")
            raise AssertionError("deveria exigir executável")
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("tudo passou")
