#!/usr/bin/env python3
"""Bridge stdio do Codex app-server com troca de conta em processo vivo.

O VS Code inicia este arquivo por um pequeno launcher nativo. Todo JSONL segue
inalterado entre extensao e app-server, salvo por duas operacoes deliberadas:

* habilitar ``experimentalApi`` no initialize;
* injetar ``account/login/start`` com ``chatgptAuthTokens`` quando o CCX pede.

O controle fica em loopback e exige um segredo aleatorio guardado no perfil do
usuario. Registro e logs nunca contem tokens. O token de acesso atravessa apenas
o socket local autenticado e a entrada stdio privada do processo filho.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import ccx
import ccx_codex

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
DEFAULT_IDLE_TIMEOUT_S = 45.0
COMMIT_TIMEOUT_S = 30.0
REGISTRY_ENV = "CCX_CODEX_BRIDGE_DIR"
REAL_CODEX_ENV = "CCX_CODEX_REAL"
WORK_METHODS = {
    "turn/start",
    "turn/steer",
    "review/start",
    "thread/compact/start",
    "thread/queue/add",
    "thread/queue/start",
    "thread/shellCommand",
}
TERMINAL_TURN_STATUS = {"completed", "interrupted", "failed"}


class BridgeError(RuntimeError):
    """Falha segura do bridge, sempre sem credencial na mensagem."""


def registry_dir() -> Path:
    configured = os.environ.get(REGISTRY_ENV)
    return Path(configured) if configured else ccx_codex.STORE.parent / "codex_bridges"


def _resolved_codex_home() -> str:
    return os.path.normcase(str(ccx_codex.codex_home().resolve()))


def _safe_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise BridgeError("payload_invalido")
    access = payload.get("accessToken")
    account = payload.get("chatgptAccountId")
    if not isinstance(access, str) or not access or len(access) > 32_768:
        raise BridgeError("access_token_invalido")
    if not isinstance(account, str) or not account or len(account) > 1_024:
        raise BridgeError("account_id_invalido")
    return {"accessToken": access, "chatgptAccountId": account}


def _safe_error_code(value: object, fallback: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        return fallback
    if not all(character.isalnum() or character in "_-; " for character in value):
        return fallback
    return value


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("socket fechado")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_frame(sock: socket.socket) -> dict:
    size = struct.unpack("!I", _recv_exact(sock, 4))[0]
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise BridgeError("frame_invalido")
    try:
        value = json.loads(_recv_exact(sock, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("json_invalido") from exc
    if not isinstance(value, dict):
        raise BridgeError("mensagem_invalida")
    return value


def send_frame(sock: socket.socket, value: dict) -> None:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise BridgeError("frame_grande_demais")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def _registration_files() -> list[Path]:
    root = registry_dir()
    try:
        return [path for path in root.glob("*.json") if not path.is_symlink()]
    except OSError:
        return []


def _discard_stale_registration(path: Path) -> None:
    try:
        if path.parent.resolve() == registry_dir().resolve() and not path.is_symlink():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def discover_bridges() -> list[dict]:
    """Registros vivos deste CODEX_HOME; remove somente arquivos comprovadamente orfaos."""
    found = []
    expected_home = _resolved_codex_home()
    for path in _registration_files():
        try:
            registration = ccx.read_json(path)
        except (ccx.CorruptFile, OSError):
            continue
        if not isinstance(registration, dict):
            continue
        if registration.get("version") != PROTOCOL_VERSION:
            continue
        if os.path.normcase(str(registration.get("codex_home", ""))) != expected_home:
            continue
        owner = {
            "pid": registration.get("pid"),
            "process_start": registration.get("process_start"),
        }
        if not ccx.owner_process_alive(owner):
            _discard_stale_registration(path)
            continue
        if (
            registration.get("host") != "127.0.0.1"
            or not isinstance(registration.get("port"), int)
            or not isinstance(registration.get("secret"), str)
        ):
            continue
        registration["_path"] = str(path)
        found.append(registration)
    return found


def bridge_count() -> int:
    return len(discover_bridges())


def transactional_switch(
    payload: dict,
    commit: Callable[[], None],
    *,
    timeout: float = DEFAULT_IDLE_TIMEOUT_S,
) -> bool:
    """Troca o app-server e executa ``commit`` antes de liberar novos turnos.

    Retorna ``True`` quando havia um bridge vivo. Sem bridge, preserva o modo
    legado e apenas executa o commit para que novas sessoes usem a conta.
    """
    clean = _safe_payload(payload)
    registrations = discover_bridges()
    if not registrations:
        commit()
        return False
    if len(registrations) != 1:
        raise BridgeError(
            "multiplos_app_servers; use perfis CODEX_HOME isolados para paralelismo"
        )
    registration = registrations[0]
    timeout = max(1.0, min(float(timeout), 120.0))
    try:
        sock = socket.create_connection(
            (registration["host"], registration["port"]), timeout=min(5.0, timeout)
        )
    except OSError as exc:
        raise BridgeError("bridge_inacessivel") from exc
    with sock:
        sock.settimeout(timeout + COMMIT_TIMEOUT_S + 5.0)
        send_frame(
            sock,
            {
                "version": PROTOCOL_VERSION,
                "secret": registration["secret"],
                "op": "switch",
                "timeout": timeout,
                "payload": clean,
            },
        )
        response = recv_frame(sock)
        if not response.get("ok") or response.get("stage") != "ready":
            raise BridgeError(_safe_error_code(response.get("error"), "troca_recusada"))
        try:
            commit()
        except BaseException:
            try:
                send_frame(sock, {"op": "abort"})
                recv_frame(sock)
            except (OSError, EOFError, BridgeError):
                pass
            raise
        send_frame(sock, {"op": "commit"})
        try:
            response = recv_frame(sock)
        except EOFError:
            # O commit local ja e atomico e a ordem chegou ao socket. Se o
            # bridge encerrou neste exato instante, a extensao o reinicia lendo
            # o auth.json novo; desfazer o disco recriaria a divergencia.
            return True
        if not response.get("ok") or response.get("stage") != "done":
            raise BridgeError(_safe_error_code(response.get("error"), "commit_recusado"))
    return True


def resolve_real_codex() -> Path:
    # A extensao acrescenta o proprio binario ao fim do PATH antes de iniciar
    # o custom executable. Preferi-lo permite sobreviver ao auto-update sem
    # reinstalar o bridge; o sidecar do instalador fica como fallback.
    for entry in reversed(os.environ.get("PATH", "").split(os.pathsep)):
        lowered = entry.replace("\\", "/").lower()
        if "openai.chatgpt-" not in lowered or not lowered.endswith(
            "/bin/windows-x86_64"
        ):
            continue
        candidate = Path(entry) / "codex.exe"
        if candidate.is_file():
            return candidate
    configured = os.environ.get(REAL_CODEX_ENV)
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
    candidates = []
    home = Path.home()
    for base in (home / ".vscode" / "extensions", home / ".vscode-insiders" / "extensions"):
        candidates.extend(base.glob("openai.chatgpt-*-win32-x64/bin/windows-x86_64/codex.exe"))
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        raise BridgeError("codex_real_nao_encontrado")
    return max(existing, key=lambda path: path.stat().st_mtime_ns)


def _initial_payload() -> dict | None:
    try:
        tokens, identity = ccx_codex.live_identity()
        return ccx_codex.external_auth_payload(tokens, identity.get("account_id", ""))
    except (SystemExit, ValueError, OSError, ccx.CorruptFile):
        return None


def _id_key(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"{type(value).__name__}:{encoded}"


def _parse_line(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class AppServerBridge:
    def __init__(self, real_codex: Path, argv: list[str]):
        self.real_codex = real_codex
        self.argv = argv
        self.child: subprocess.Popen | None = None
        self.control: socket.socket | None = None
        self.registration_path: Path | None = None
        self.secret = secrets.token_hex(32)
        self.child_write_lock = threading.Lock()
        self.stdout_lock = threading.Lock()
        self.state = threading.Condition()
        self.initialized = False
        self.switching = False
        self.pending_work_requests: set[str] = set()
        self.active_turns: set[tuple[str, str]] = set()
        self.client_requests: dict[str, str] = {}
        self.rpc_waiters: dict[str, tuple[threading.Event, dict]] = {}
        self.own_ids: set[str] = set()
        self.current_payload = _initial_payload()
        self.stopping = threading.Event()

    def _write_child(self, value: dict | bytes) -> None:
        if not self.child or not self.child.stdin:
            raise BridgeError("app_server_encerrado")
        raw = value if isinstance(value, bytes) else json.dumps(
            value, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8") + b"\n"
        with self.child_write_lock:
            self.child.stdin.write(raw)
            self.child.stdin.flush()

    def _write_client(self, raw: bytes) -> None:
        with self.stdout_lock:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    def _enable_experimental_api(self, message: dict) -> dict:
        updated = dict(message)
        params = dict(updated.get("params") or {})
        capabilities = dict(params.get("capabilities") or {})
        capabilities["experimentalApi"] = True
        params["capabilities"] = capabilities
        updated["params"] = params
        return updated

    def _client_loop(self) -> None:
        try:
            for raw in sys.stdin.buffer:
                message = _parse_line(raw)
                if message is None:
                    self._write_child(raw)
                    continue
                method = message.get("method")
                request_id = message.get("id")
                key = _id_key(request_id) if "id" in message else ""
                if method == "initialize":
                    message = self._enable_experimental_api(message)
                    raw = json.dumps(message, separators=(",", ":")).encode() + b"\n"
                is_client_request = "method" in message and bool(key)
                if is_client_request:
                    with self.state:
                        self.client_requests[key] = str(method or "")
                if method in WORK_METHODS:
                    # Esses metodos sao requests no contrato do app-server. Uma
                    # notificacao invalida nao tem resposta que permita fechar
                    # a barreira; descarta-la e mais seguro que iniciar trabalho
                    # impossivel de correlacionar e travar todo hot-swap futuro.
                    if not key:
                        continue
                    with self.state:
                        while (
                            (self.switching or not self.initialized)
                            and not self.stopping.is_set()
                        ):
                            self.state.wait(0.25)
                        self.pending_work_requests.add(key)
                try:
                    self._write_child(raw)
                except BaseException:
                    if method in WORK_METHODS:
                        with self.state:
                            self.pending_work_requests.discard(key)
                            self.state.notify_all()
                    raise
        except BaseException:
            pass
        finally:
            try:
                if self.child and self.child.stdin:
                    self.child.stdin.close()
            except OSError:
                pass

    def _turn(self, message: dict) -> tuple[str, str, str] | None:
        params = message.get("params")
        result = message.get("result")
        for container in (params, result):
            if not isinstance(container, dict):
                continue
            turn = container.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                thread_id = container.get("threadId")
                return (
                    thread_id if isinstance(thread_id, str) else "",
                    turn["id"],
                    str(turn.get("status") or ""),
                )
            turn_id = container.get("turnId")
            if isinstance(turn_id, str):
                thread_id = container.get("threadId")
                return (
                    thread_id if isinstance(thread_id, str) else "",
                    turn_id,
                    str(container.get("status") or ""),
                )
        return None

    def _observe_server_message(self, message: dict) -> bool:
        """Atualiza estado. True significa resposta interna, que nao e encaminhada."""
        response_key = _id_key(message.get("id")) if "id" in message else ""
        with self.state:
            waiter = self.rpc_waiters.get(response_key)
            if response_key in self.own_ids and "method" not in message:
                self.own_ids.discard(response_key)
                if waiter is not None:
                    waiter[1]["response"] = message
                    waiter[0].set()
                return True

            client_method = self.client_requests.pop(response_key, None)
            if client_method == "initialize":
                self.initialized = "error" not in message
            if client_method in WORK_METHODS:
                self.pending_work_requests.discard(response_key)
                turn = self._turn(message)
                if (
                    turn
                    and "error" not in message
                    and turn[2] not in TERMINAL_TURN_STATUS
                ):
                    self.active_turns.add((turn[0], turn[1]))

            method = message.get("method")
            turn = self._turn(message)
            if method == "turn/started" and turn:
                self.active_turns.add((turn[0], turn[1]))
            elif method == "turn/completed" and turn:
                self.active_turns.discard((turn[0], turn[1]))
            self.state.notify_all()
        return False

    def _refresh_external_auth(self, request: dict) -> None:
        request_id = request.get("id")
        params = request.get("params")
        previous = params.get("previousAccountId") if isinstance(params, dict) else None
        with self.state:
            current = dict(self.current_payload or {})
        account_id = previous or current.get("chatgptAccountId")
        stale = current.get("accessToken") or ""
        try:
            if account_id != current.get("chatgptAccountId"):
                raise BridgeError("refresh_de_outra_conta")
            payload = ccx_codex.refresh_for_bridge(account_id, stale)
            clean = _safe_payload(payload)
            with self.state:
                self.current_payload = clean
            response = {"id": request_id, "result": clean}
        except BaseException:
            response = {
                "id": request_id,
                "error": {"code": -32000, "message": "CCX external token refresh failed"},
            }
        try:
            self._write_child(response)
        except BaseException:
            pass

    def _server_loop(self) -> None:
        assert self.child and self.child.stdout
        for raw in iter(self.child.stdout.readline, b""):
            message = _parse_line(raw)
            if message is not None:
                if message.get("method") == "account/chatgptAuthTokens/refresh":
                    threading.Thread(
                        target=self._refresh_external_auth,
                        args=(message,),
                        name="ccx-codex-refresh",
                        daemon=True,
                    ).start()
                    continue
                if self._observe_server_message(message):
                    continue
            self._write_client(raw)

    def _rpc(self, method: str, params: dict, timeout: float = 10.0) -> dict:
        request_id = f"ccx-live:{uuid.uuid4().hex}"
        key = _id_key(request_id)
        event = threading.Event()
        box: dict = {}
        with self.state:
            self.rpc_waiters[key] = (event, box)
            self.own_ids.add(key)
        try:
            self._write_child({"id": request_id, "method": method, "params": params})
            if not event.wait(timeout):
                raise BridgeError("app_server_timeout")
            response = box.get("response") or {}
            if response.get("error") is not None:
                raise BridgeError("app_server_rejeitou_login")
            return response.get("result") or {}
        finally:
            with self.state:
                self.rpc_waiters.pop(key, None)

    def _login(self, payload: dict) -> None:
        params = {"type": "chatgptAuthTokens", **_safe_payload(payload)}
        self._rpc("account/login/start", params)

    def _begin_switch(self, payload: dict, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        with self.state:
            while self.switching:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("bridge_ocupado")
                self.state.wait(remaining)
            self.switching = True
            while (
                not self.initialized
                or self.pending_work_requests
                or self.active_turns
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.switching = False
                    self.state.notify_all()
                    raise BridgeError("turno_ativo_timeout")
                self.state.wait(remaining)
            disk_payload = _initial_payload()
            if disk_payload:
                self.current_payload = disk_payload
            previous = dict(self.current_payload or {})
            if not previous:
                self.switching = False
                self.state.notify_all()
                raise BridgeError("conta_anterior_desconhecida")
        try:
            self._login(payload)
        except BaseException:
            with self.state:
                self.switching = False
                self.state.notify_all()
            raise
        with self.state:
            self.current_payload = dict(payload)
        return previous

    def _release_switch(self) -> None:
        with self.state:
            self.switching = False
            self.state.notify_all()

    def _rollback(self, previous: dict) -> None:
        try:
            self._login(previous)
            with self.state:
                self.current_payload = previous
        finally:
            self._release_switch()

    def _control_client(self, connection: socket.socket) -> None:
        gate_open = False
        previous = None
        try:
            connection.settimeout(5.0)
            request = recv_frame(connection)
            if request.get("version") != PROTOCOL_VERSION or not hmac.compare_digest(
                str(request.get("secret", "")), self.secret
            ):
                send_frame(connection, {"ok": False, "error": "nao_autorizado"})
                return
            if request.get("op") != "switch":
                send_frame(connection, {"ok": False, "error": "operacao_invalida"})
                return
            timeout = max(1.0, min(float(request.get("timeout", 0)), 120.0))
            connection.settimeout(timeout + COMMIT_TIMEOUT_S + 5.0)
            payload = _safe_payload(request.get("payload"))
            previous = self._begin_switch(payload, timeout)
            gate_open = True
            send_frame(connection, {"ok": True, "stage": "ready"})
            connection.settimeout(COMMIT_TIMEOUT_S)
            decision = recv_frame(connection).get("op")
            if decision == "commit":
                self._release_switch()
                gate_open = False
                send_frame(connection, {"ok": True, "stage": "done"})
                return
            self._rollback(previous)
            gate_open = False
            send_frame(connection, {"ok": True, "stage": "aborted"})
        except (OSError, EOFError, ValueError, BridgeError):
            if gate_open and previous:
                try:
                    self._rollback(previous)
                except BaseException:
                    self._release_switch()
            elif gate_open:
                self._release_switch()
            try:
                send_frame(connection, {"ok": False, "error": "transacao_falhou"})
            except BaseException:
                pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _control_loop(self) -> None:
        assert self.control
        while not self.stopping.is_set():
            try:
                connection, address = self.control.accept()
            except OSError:
                break
            if address[0] != "127.0.0.1":
                connection.close()
                continue
            threading.Thread(
                target=self._control_client,
                args=(connection,),
                name="ccx-codex-control-client",
                daemon=True,
            ).start()

    def _register(self) -> None:
        assert self.control
        root = registry_dir()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        port = self.control.getsockname()[1]
        marker = ccx.process_start_marker(os.getpid())
        path = root / f"{os.getpid()}-{secrets.token_hex(8)}.json"
        ccx.write_json(
            path,
            {
                "version": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "process_start": marker,
                "codex_home": _resolved_codex_home(),
                "host": "127.0.0.1",
                "port": port,
                "secret": self.secret,
                "started_at": time.time(),
            },
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.registration_path = path

    def run(self) -> int:
        self.control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.control.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.control.bind(("127.0.0.1", 0))
        self.control.listen(4)
        self._register()
        try:
            self.child = subprocess.Popen(
                [str(self.real_codex), *self.argv],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=0,
            )
        except BaseException:
            if self.registration_path:
                _discard_stale_registration(self.registration_path)
            self.control.close()
            raise
        threading.Thread(
            target=self._control_loop, name="ccx-codex-control", daemon=True
        ).start()
        threading.Thread(
            target=self._client_loop, name="ccx-codex-stdin", daemon=True
        ).start()
        try:
            self._server_loop()
            return self.child.wait()
        finally:
            self.stopping.set()
            with self.state:
                self.state.notify_all()
            if self.control:
                try:
                    self.control.close()
                except OSError:
                    pass
            if self.registration_path:
                _discard_stale_registration(self.registration_path)
            if self.child.poll() is None:
                self.child.terminate()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        real_codex = resolve_real_codex()
        if "app-server" not in arguments:
            return subprocess.call([str(real_codex), *arguments])
        return AppServerBridge(real_codex, arguments).run()
    except BridgeError as exc:
        print(f"ccx bridge: {exc}", file=sys.stderr)
        return 1
    except BaseException as exc:
        print(f"ccx bridge: falha {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
