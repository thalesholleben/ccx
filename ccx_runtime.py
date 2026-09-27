"""Contrato local de estado e observação de tentativas Claude.

Não inicia clientes, não troca contas e não renova tokens. A fronteira de
execução deve provar checkpoint e ownership antes de usar uma sugestão de retry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import re
import time

import ccx

SCHEMA_VERSION = 1
ERRORS = frozenset({
    "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
    "billing_error", "rate_limit", "overloaded", "invalid_request",
    "model_not_found", "server_error", "max_output_tokens", "unknown",
})
WINDOWS = ("5h", "7d", "modelo")


def utc_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def measurement_error(value: object) -> str | None:
    if not value:
        return None
    return {
        "HTTP 429": "measurement_rate_limited", "HTTP 401": "measurement_unauthorized",
        "HTTP 403": "measurement_forbidden", "TimeoutError": "measurement_timeout",
        "URLError": "measurement_network_error",
    }.get(value, "measurement_unavailable") if isinstance(value, str) else "measurement_unavailable"


def quota_snapshot(entry: dict, now: float) -> dict:
    """Projeção não destrutiva; cache legado não prova medição nem conta ociosa.

    O coletor antigo modifica percentuais após resets e renova known_at no
    fallback de 429. Não promover esses campos a uma observação real da API.
    """
    observed = entry.get("known_at", entry.get("at"))
    valid_time = finite_number(observed) and 0 <= observed <= now
    age = now - observed if valid_time else None
    usage = entry.get("usage")
    windows = {}
    if isinstance(usage, dict):
        for name in WINDOWS:
            window = usage.get(name)
            if not isinstance(window, dict):
                continue
            pct = window.get("pct")
            if not finite_number(pct) or not 0 <= pct <= 100:
                continue
            reset = ccx.parse_reset(window.get("resets_at") if isinstance(window.get("resets_at"), str) else None)
            reset_known = reset != ccx.FAR_FUTURE
            projected = reset_known and reset.timestamp() <= now
            windows[name] = {
                "percent": 0.0 if projected else pct,
                "snapshot_percent": pct,
                "reset_at": reset.isoformat() if reset_known else None,
                "source": "projected" if projected else "legacy_cache",
            }
    return {
        "snapshot_at": utc_at(observed) if valid_time else None,
        "measurement_at": None,
        "measurement_verified": False,
        "age_seconds": age,
        "freshness": "unknown" if not windows or age is None else
                     "recent_snapshot" if age < ccx.USAGE_CACHE_TTL_S else "stale_snapshot",
        "availability_confirmed": False,
        "windows": windows,
        "error_code": measurement_error(entry.get("error")),
    }


def status_snapshot() -> dict:
    """Somente disco, projeção allowlisted. Nenhum payload OAuth sai daqui."""
    with ccx.store_lock():
        store = ccx.load_store()
        active = ccx.active_slot(store)
        pin = ccx.pinned_slot(store)
        generation = ccx.control_generation(store)
        now = time.time()
        slots = []
        for key, slot in store["slots"].items():
            live_slot = slot
            # Lê a renovação feita pelo cliente sem gravar/sincronizar o store.
            if key == active:
                live_slot = {**slot, "oauth": ccx.live_identity()[0]}
            auth = ccx.slot_auth_state(live_slot)
            entry = store.get("usage_cache", {}).get(key, {})
            slots.append({
                "slot": key, "selected": key == active, "pinned": key == pin,
                "auth_state": auth,
                "quota": quota_snapshot(entry if isinstance(entry, dict) else {}, now),
            })
        return {
            "schema_version": SCHEMA_VERSION, "provider": "claude",
            "generated_at": utc_at(now), "control_generation": generation,
            "selected_slot": active, "pinned_slot": pin, "slots": slots,
            "monitor": {"alive": ccx.auto_monitor_alive(), "job_progress_verified": False},
            "execution": {"mode": "legacy", "automatic_recovery_available": False},
        }


@dataclass(frozen=True)
class AttemptOutcome:
    state: str
    reason_code: str
    action: str
    session_id: str
    attempt: int

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


class AttemptObservation:
    """Interpreta eventos estruturados, nunca texto livre de ferramenta/erro.

    Não é um supervisor. O chamador informa exit code, cancelamento e prova de
    checkpoint; ter recebido um result no pipe não prova sua persistência.
    """

    def __init__(self, session_id: str, attempt: int = 1):
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session_id):
            raise ValueError("Identificador de sessao invalido")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("Tentativa invalida")
        self.session_id = session_id
        self.attempt = attempt
        self.pending_tools: set[str] = set()
        self.seen_tools: set[str] = set()
        self.error: str | None = None
        self.result: dict | None = None
        self.protocol_error = False

    def observe(self, event: dict) -> None:
        if not isinstance(event, dict):
            self.protocol_error = True
            return
        if event.get("session_id") not in (None, self.session_id):
            self.protocol_error = True
            return
        kind = event.get("type")
        if self.result is not None:
            # Aceitar eventos depois do terminal mascararia saída truncada/replay.
            self.protocol_error = True
            return
        if kind == "assistant":
            error = event.get("error")
            if error is not None:
                self.error = error if isinstance(error, str) and error in ERRORS else "unknown"
        if kind in ("assistant", "user"):
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and kind == "assistant":
                        tool_id = block.get("id")
                        if not isinstance(tool_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", tool_id):
                            self.protocol_error = True
                        else:
                            if tool_id in self.seen_tools:
                                self.protocol_error = True
                            self.seen_tools.add(tool_id)
                            self.pending_tools.add(tool_id)
                    elif block.get("type") == "tool_result" and kind == "user":
                        tool_id = block.get("tool_use_id")
                        if not isinstance(tool_id, str) or tool_id not in self.pending_tools:
                            self.protocol_error = True
                        else:
                            self.pending_tools.remove(tool_id)
        if kind == "result":
            if event.get("session_id") != self.session_id:
                self.protocol_error = True
                return
            # Não reter texto, errors, usage ou payload arbitrário do cliente.
            self.result = {key: event.get(key) for key in
                           ("is_error", "terminal_reason", "api_error_status")}

    def finish(self, exit_code: int, *, checkpoint_verified: bool = False,
               cancelled: bool = False) -> AttemptOutcome:
        def outcome(state, reason, action="stop"):
            return AttemptOutcome(state, reason, action, self.session_id, self.attempt)

        if cancelled:
            return outcome("cancelled", "operator_cancelled")
        if self.protocol_error or self.result is None:
            return outcome("needs_attention", "protocol_incomplete")
        if self.pending_tools:
            return outcome("needs_attention", "tool_result_ambiguous")
        if (exit_code == 0 and self.result["is_error"] is False
                and self.result["terminal_reason"] == "completed" and self.error is None):
            return outcome("completed", "request_completed", "none")
        if self.result["is_error"] is not True or self.error is None:
            return outcome("needs_attention", "unclassified_failure")
        if self.error in {"invalid_request", "model_not_found", "max_output_tokens",
                          "oauth_org_not_allowed", "account_on_hold", "billing_error", "unknown"}:
            return outcome("needs_attention", self.error)
        if not checkpoint_verified:
            return outcome("needs_attention", "checkpoint_unverified")
        if self.error == "authentication_failed":
            return outcome("waiting_auth", self.error, "reauthenticate")
        if self.error == "rate_limit":
            return outcome("recovering", "request_rate_limited", "select_account")
        if self.error in {"overloaded", "server_error"}:
            return outcome("recovering", self.error, "retry_account")
        return outcome("needs_attention", "unclassified_failure")
