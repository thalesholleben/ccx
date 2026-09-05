#!/usr/bin/env python3
"""Testes do bridge stdio/control do Codex: python test_ccx_codex_bridge.py"""

import base64
import io
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ccx
import ccx_codex_bridge as bridge


def make_jwt(claims: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


def payload(name: str) -> dict:
    return {"accessToken": f"access-{name}", "chatgptAccountId": f"account-{name}"}


def fresh_bridge() -> bridge.AppServerBridge:
    with mock.patch.object(bridge, "_initial_payload", return_value=payload("old")):
        instance = bridge.AppServerBridge(Path("codex.exe"), ["app-server"])
    instance.initialized = True
    return instance


def test_frame_roundtrip_e_limite():
    left, right = socket.socketpair()
    try:
        value = {"ok": True, "texto": "ação"}
        bridge.send_frame(left, value)
        assert bridge.recv_frame(right) == value
        left.sendall((bridge.MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        try:
            bridge.recv_frame(right)
        except bridge.BridgeError as exc:
            assert str(exc) == "frame_invalido"
        else:
            raise AssertionError("frame sem limite aceito")
    finally:
        left.close()
        right.close()


def test_payload_de_controle_descarta_campos_persistentes():
    clean = bridge._safe_payload(
        {
            **payload("new"),
            "refreshToken": "refresh-nao-pode-passar",
            "idToken": "id-nao-pode-passar",
        }
    )
    assert clean == payload("new")


def test_ids_preservam_tipo():
    assert len({bridge._id_key(1), bridge._id_key(True), bridge._id_key("1")}) == 3


def test_resposta_interna_atrasada_nunca_vaza_para_extensao():
    instance = fresh_bridge()
    key = bridge._id_key("ccx-test-id")
    instance.own_ids.add(key)
    assert instance._observe_server_message({"id": "ccx-test-id", "result": {}})
    assert key not in instance.own_ids


def test_resposta_do_cliente_nao_sobrescreve_request_de_trabalho_com_mesmo_id():
    instance = fresh_bridge()
    frames = (
        json.dumps({"id": 7, "method": "turn/start", "params": {}})
        + "\n"
        + json.dumps({"id": 7, "result": {"decision": "approved"}})
        + "\n"
    ).encode()
    forwarded = []
    with (
        mock.patch.object(
            bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(frames))
        ),
        mock.patch.object(instance, "_write_child", side_effect=forwarded.append),
    ):
        instance._client_loop()
    key = bridge._id_key(7)
    assert len(forwarded) == 2
    assert instance.client_requests[key] == "turn/start"
    assert instance.pending_work_requests == {key}

    instance._observe_server_message(
        {
            "id": 7,
            "result": {
                "threadId": "thread-7",
                "turn": {"id": "turn-7", "status": "inProgress"},
            },
        }
    )
    instance._observe_server_message(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-7",
                "turn": {"id": "turn-7", "status": "completed"},
            },
        }
    )
    with (
        mock.patch.object(instance, "_login"),
        mock.patch.object(bridge, "_initial_payload", return_value=payload("old")),
    ):
        assert instance._begin_switch(payload("new"), 0.2) == payload("old")
    instance._release_switch()


def test_notificacao_invalida_de_trabalho_nao_trava_barreira():
    instance = fresh_bridge()
    frame = (json.dumps({"method": "turn/start", "params": {}}) + "\n").encode()
    forwarded = []
    with (
        mock.patch.object(
            bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(frame))
        ),
        mock.patch.object(instance, "_write_child", side_effect=forwarded.append),
    ):
        instance._client_loop()
    assert not forwarded
    assert not instance.pending_work_requests
    assert {"thread/queue/add", "thread/queue/start"} <= bridge.WORK_METHODS


def test_initialize_preserva_cliente_e_habilita_api_experimental():
    instance = fresh_bridge()
    original = {
        "id": "1",
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "codex_vscode", "version": "x"},
            "capabilities": {"requestAttestation": False},
        },
    }
    updated = instance._enable_experimental_api(original)
    assert updated["params"]["clientInfo"] == original["params"]["clientInfo"]
    assert updated["params"]["capabilities"] == {
        "requestAttestation": False,
        "experimentalApi": True,
    }
    assert "experimentalApi" not in original["params"]["capabilities"]


def test_turnos_sao_identificados_por_thread_e_turn():
    instance = fresh_bridge()
    for thread_id in ("thread-a", "thread-b"):
        instance._observe_server_message(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": "turn-1", "status": "inProgress"},
                },
            }
        )
    assert instance.active_turns == {
        ("thread-a", "turn-1"),
        ("thread-b", "turn-1"),
    }
    instance._observe_server_message(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-a",
                "turn": {"id": "turn-1", "status": "failed"},
            },
        }
    )
    assert instance.active_turns == {("thread-b", "turn-1")}


def test_switch_espera_turno_completar_e_mantem_barreira_ate_commit():
    instance = fresh_bridge()
    instance.active_turns.add(("thread-a", "turn-a"))
    logged_in = threading.Event()
    result = {}

    def fake_login(target):
        result["login"] = target
        logged_in.set()

    def begin():
        result["previous"] = instance._begin_switch(payload("new"), 2.0)

    with (
        mock.patch.object(instance, "_login", side_effect=fake_login),
        mock.patch.object(bridge, "_initial_payload", return_value=payload("old")),
    ):
        worker = threading.Thread(target=begin)
        worker.start()
        time.sleep(0.05)
        assert not logged_in.is_set()
        instance._observe_server_message(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-a",
                    "turn": {"id": "turn-a", "status": "interrupted"},
                },
            }
        )
        worker.join(2)
    assert not worker.is_alive()
    assert result == {"login": payload("new"), "previous": payload("old")}
    assert instance.switching
    instance._release_switch()
    assert not instance.switching


def test_refresh_recusa_account_id_diferente_do_slot_externo():
    instance = fresh_bridge()
    responses = []
    request = {
        "id": 7,
        "method": "account/chatgptAuthTokens/refresh",
        "params": {"reason": "unauthorized", "previousAccountId": "account-other"},
    }
    with (
        mock.patch.object(bridge.ccx_codex, "refresh_for_bridge") as refresh,
        mock.patch.object(instance, "_write_child", side_effect=responses.append),
    ):
        instance._refresh_external_auth(request)
    refresh.assert_not_called()
    assert responses[0]["id"] == 7
    assert responses[0]["error"]["code"] == -32000


def test_refresh_responde_mesmo_id_e_atualiza_payload():
    instance = fresh_bridge()
    responses = []
    request = {
        "id": "server-9",
        "method": "account/chatgptAuthTokens/refresh",
        "params": {"reason": "unauthorized", "previousAccountId": "account-old"},
    }
    with (
        mock.patch.object(
            bridge.ccx_codex, "refresh_for_bridge", return_value=payload("renewed")
        ) as refresh,
        mock.patch.object(instance, "_write_child", side_effect=responses.append),
    ):
        instance._refresh_external_auth(request)
    refresh.assert_called_once_with("account-old", "access-old")
    assert responses == [{"id": "server-9", "result": payload("renewed")}]
    assert instance.current_payload == payload("renewed")


def _fake_control_server(decision_box: dict, *, abort=False):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    secret = "control-secret"
    registration = {
        "host": "127.0.0.1",
        "port": server.getsockname()[1],
        "secret": secret,
    }

    def serve():
        connection, _ = server.accept()
        with connection:
            decision_box["request"] = bridge.recv_frame(connection)
            bridge.send_frame(connection, {"ok": True, "stage": "ready"})
            decision_box["decision"] = bridge.recv_frame(connection)
            stage = "aborted" if abort else "done"
            bridge.send_frame(connection, {"ok": True, "stage": stage})
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    return registration, thread


def test_transacao_so_commita_depois_do_bridge_ficar_pronto():
    observed = {}
    registration, server = _fake_control_server(observed)

    def commit():
        assert "request" in observed
        observed["committed"] = True

    with mock.patch.object(bridge, "discover_bridges", return_value=[registration]):
        assert bridge.transactional_switch(
            {**payload("new"), "refreshToken": "nao-envie"}, commit
        )
    server.join(2)
    assert observed["committed"]
    assert observed["decision"] == {"op": "commit"}
    assert observed["request"]["payload"] == payload("new")


def test_transacao_aborta_bridge_quando_commit_local_falha():
    observed = {}
    registration, server = _fake_control_server(observed, abort=True)

    def fail():
        raise OSError("disco indisponivel")

    with mock.patch.object(bridge, "discover_bridges", return_value=[registration]):
        try:
            bridge.transactional_switch(payload("new"), fail)
        except OSError:
            pass
        else:
            raise AssertionError("erro local nao foi propagado")
    server.join(2)
    assert observed["decision"] == {"op": "abort"}


def test_sem_bridge_mantem_fallback_para_novas_sessoes():
    committed = []
    with mock.patch.object(bridge, "discover_bridges", return_value=[]):
        live = bridge.transactional_switch(payload("new"), lambda: committed.append(True))
    assert not live
    assert committed == [True]


def test_multiplos_bridges_recusam_troca_parcial():
    committed = []
    with mock.patch.object(bridge, "discover_bridges", return_value=[{}, {}]):
        try:
            bridge.transactional_switch(payload("new"), lambda: committed.append(True))
        except bridge.BridgeError as exc:
            assert "multiplos_app_servers" in str(exc)
        else:
            raise AssertionError("troca parcial foi permitida")
    assert not committed


def test_path_da_extensao_vence_sidecar_antigo():
    with tempfile.TemporaryDirectory() as tmp:
        active_dir = Path(tmp) / "openai.chatgpt-new" / "bin" / "windows-x86_64"
        active_dir.mkdir(parents=True)
        active = active_dir / "codex.exe"
        active.write_bytes(b"new")
        stale = Path(tmp) / "old-codex.exe"
        stale.write_bytes(b"old")
        env = {
            "PATH": os.pathsep.join((str(Path(tmp) / "other"), str(active_dir))),
            bridge.REAL_CODEX_ENV: str(stale),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            assert bridge.resolve_real_codex() == active


def _wait_json(lines: queue.Queue, request_id: object, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            break
        message = json.loads(line)
        if message.get("id") == request_id:
            return message
    raise AssertionError(f"sem resposta para {request_id!r}")


def test_runtime_real_aceita_hot_login_sem_reiniciar_app_server():
    """Smoke local, sem chamada de modelo/rede e sem credencial real."""
    try:
        real_codex = bridge.resolve_real_codex()
    except bridge.BridgeError:
        return
    now = int(time.time())
    old_id = make_jwt(
        {
            "email": "old@example.invalid",
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-old"},
        }
    )
    old_access = make_jwt({"exp": now + 86_400})
    new_access = make_jwt({"exp": now + 86_400, "sub": "new"})
    third_access = make_jwt({"exp": now + 86_400, "sub": "third"})
    with tempfile.TemporaryDirectory(prefix="ccx-bridge-runtime-") as tmp:
        root = Path(tmp)
        codex_home = root / "codex-home"
        registry = root / "registry"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "id_token": old_id,
                        "access_token": old_access,
                        "refresh_token": "fake-refresh-old",
                        "account_id": "account-old",
                    },
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(codex_home),
                bridge.REGISTRY_ENV: str(registry),
                bridge.REAL_CODEX_ENV: str(real_codex),
                "PYTHONUTF8": "1",
            }
        )
        process = subprocess.Popen(
            [sys.executable, str(Path(bridge.__file__)), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        lines: queue.Queue = queue.Queue()

        def read_stdout():
            assert process.stdout
            for line in process.stdout:
                lines.put(line)

        threading.Thread(target=read_stdout, daemon=True).start()
        try:
            assert process.stdin
            initialize = {
                "id": "init-test",
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex_vscode",
                        "title": "CCX bridge test",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }
            process.stdin.write(json.dumps(initialize) + "\n")
            process.stdin.flush()
            response = _wait_json(lines, "init-test", 15.0)
            assert "error" not in response

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not list(registry.glob("*.json")):
                time.sleep(0.05)
            files = list(registry.glob("*.json"))
            assert len(files) == 1
            raw_registration = files[0].read_text(encoding="utf-8")
            assert old_access not in raw_registration
            assert new_access not in raw_registration
            assert third_access not in raw_registration

            committed = []
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home), bridge.REGISTRY_ENV: str(registry)},
                clear=False,
            ):
                assert bridge.transactional_switch(
                    {
                        "accessToken": new_access,
                        "chatgptAccountId": "account-new",
                    },
                    lambda: committed.append(True),
                    timeout=10.0,
                )
                assert bridge.transactional_switch(
                    {
                        "accessToken": third_access,
                        "chatgptAccountId": "account-third",
                    },
                    lambda: committed.append(True),
                    timeout=10.0,
                )
            assert committed == [True, True]
            assert process.poll() is None, "app-server reiniciou durante o hot login"
        finally:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(5)
            stderr = process.stderr.read() if process.stderr else ""
            assert (
                old_access not in stderr
                and new_access not in stderr
                and third_access not in stderr
            )


def test_instalador_e_launcher_preservam_config_stdio_argv_e_filhos():
    if os.name != "nt":
        return
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    csc = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    try:
        real_codex = bridge.resolve_real_codex()
    except bridge.BridgeError:
        return
    if not powershell or not csc.exists():
        return
    installer = Path(__file__).with_name("install-ccx-codex-bridge.ps1")
    with tempfile.TemporaryDirectory(prefix="ccx bridge installer ") as tmp:
        root = Path(tmp)
        settings = root / "settings.json"
        install_dir = root / "bin"
        original_settings = '{\n    "editor.fontSize": 14\n}\n'
        settings.write_text(original_settings, encoding="utf-8")
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            "-SettingsPath",
            str(settings),
            "-InstallDir",
            str(install_dir),
            "-CodexExecutable",
            str(real_codex),
            "-PythonExecutable",
            sys.executable,
        ]
        installed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert installed.returncode == 0, installed.stderr
        launcher = install_dir / "ccx-codex-bridge.exe"
        config = install_dir / "ccx-codex-bridge.cfg"
        parsed = json.loads(settings.read_text(encoding="utf-8"))
        assert parsed["chatgpt.cliExecutable"] == str(launcher)
        assert "chatgpt.cliExecutable" in parsed["settingsSync.ignoredSettings"]
        settings_backup = Path(str(settings) + ".ccx-codex-bridge.bak")
        assert settings_backup.read_text(encoding="utf-8") == original_settings

        # Idempotencia nao duplica as duas configuracoes.
        again = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert again.returncode == 0, again.stderr
        text = settings.read_text(encoding="utf-8")
        assert text.count('"chatgpt.cliExecutable"') == 2  # chave + item ignorado

        fake = root / "fake child.py"
        fake.write_text(
            "import json, os, sys, time\n"
            "if sys.argv[1] == 'io':\n"
            " print(json.dumps(sys.argv[1:])); print(sys.stdin.read(), end=''); print('stderr-ok', file=sys.stderr); raise SystemExit(23)\n"
            "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
            "while True: time.sleep(1)\n",
            encoding="utf-8",
        )
        config.write_text(
            "\n".join((sys.executable, str(fake), str(real_codex))), encoding="utf-8"
        )
        io_run = subprocess.run(
            [str(launcher), "io", "argumento com espaço", 'aspas"aqui'],
            input="stdin-ok",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert io_run.returncode == 23
        assert json.loads(io_run.stdout.splitlines()[0]) == [
            "io",
            "argumento com espaço",
            'aspas"aqui',
        ]
        assert io_run.stdout.endswith("stdin-ok")
        assert "stderr-ok" in io_run.stderr

        child_pid_file = root / "child.pid"
        launched = subprocess.Popen(
            [str(launcher), "sleep", str(child_pid_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        launched.kill()
        launched.wait(5)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ccx.process_alive(child_pid):
            time.sleep(0.05)
        assert not ccx.process_alive(child_pid), "launcher deixou Python orfao"

        # Uma edicao alheia sobrevive ao uninstall; so as duas chaves do CCX saem.
        current = json.loads(settings.read_text(encoding="utf-8"))
        current["files.autoSave"] = "off"
        settings.write_text(json.dumps(current, indent=2), encoding="utf-8")
        removed = subprocess.run(
            command[:6] + ["-Uninstall"] + command[6:],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert removed.returncode == 0, removed.stderr
        final = json.loads(settings.read_text(encoding="utf-8"))
        assert final == {"editor.fontSize": 14, "files.autoSave": "off"}
        assert not settings_backup.exists()

        # O primeiro token JSONC define o objeto raiz, mesmo se um comentario
        # anterior contiver uma chave. Sem edicoes alheias, uninstall e exato.
        commented = root / "commented.json"
        commented_install = root / "commented-bin"
        commented_original = (
            "// VS Code aceita comentario aqui. Ex.: use { } para blocos.\r\n"
            "{\r\n"
            '    "editor.fontSize": 14\r\n'
            "}\r\n"
        )
        commented.write_bytes(commented_original.encode("utf-8"))
        commented_command = list(command)
        commented_command[commented_command.index(str(settings))] = str(commented)
        commented_command[commented_command.index(str(install_dir))] = str(
            commented_install
        )
        commented_result = subprocess.run(
            commented_command, capture_output=True, text=True, timeout=30
        )
        assert commented_result.returncode == 0, commented_result.stderr
        uncommented = "\n".join(
            line
            for line in commented.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("//")
        )
        commented_parsed = json.loads(uncommented)
        assert commented_parsed["chatgpt.cliExecutable"] == str(
            commented_install / "ccx-codex-bridge.exe"
        )
        commented_backup = Path(str(commented) + ".ccx-codex-bridge.bak")
        assert commented_backup.read_bytes() == commented_original.encode("utf-8")
        commented_removed = subprocess.run(
            commented_command[:6] + ["-Uninstall"] + commented_command[6:],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert commented_removed.returncode == 0, commented_removed.stderr
        assert commented.read_bytes() == commented_original.encode("utf-8")
        assert not commented_backup.exists()

        # Um objeto vazio continua sendo JSON estrito, sem virgula sobrando.
        empty = root / "empty.json"
        empty_install = root / "empty-bin"
        empty.write_text("{}", encoding="utf-8")
        empty_command = list(command)
        empty_command[empty_command.index(str(settings))] = str(empty)
        empty_command[empty_command.index(str(install_dir))] = str(empty_install)
        empty_result = subprocess.run(
            empty_command, capture_output=True, text=True, timeout=30
        )
        assert empty_result.returncode == 0, empty_result.stderr
        empty_parsed = json.loads(empty.read_text(encoding="utf-8"))
        assert empty_parsed["chatgpt.cliExecutable"] == str(
            empty_install / "ccx-codex-bridge.exe"
        )
        empty_removed = subprocess.run(
            empty_command[:6] + ["-Uninstall"] + empty_command[6:],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert empty_removed.returncode == 0, empty_removed.stderr
        assert json.loads(empty.read_text(encoding="utf-8")) == {}
        assert not Path(str(empty) + ".ccx-codex-bridge.bak").exists()

        # Um custom executable preexistente e intocavel, inclusive byte a byte.
        guarded = root / "guarded.json"
        original = b'{\r\n  "chatgpt.cliExecutable": "C:\\\\other.exe"\r\n}\r\n'
        guarded.write_bytes(original)
        blocked_command = list(command)
        blocked_command[blocked_command.index(str(settings))] = str(guarded)
        blocked = subprocess.run(
            blocked_command, capture_output=True, text=True, timeout=15
        )
        assert blocked.returncode != 0
        assert guarded.read_bytes() == original


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("tudo passou")
