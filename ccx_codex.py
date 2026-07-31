#!/usr/bin/env python3
"""ccx_codex - monitora a cota das contas Codex CLI (ChatGPT) e troca antes de bater o limite.

Modulo separado do ccx.py (Claude Code), como o README do ccx ja previa.
Reusa a engine de decisao e os primitivos de lock/escrita atomica de la via
import (mesma arquitetura, sem duplicar), e so implementa o que e
genuinamente diferente: o formato do auth.json do Codex CLI e o protocolo
da API de usage da OpenAI.

    ccx_codex add                captura a conta logada agora num slot
    ccx_codex status             as contas lado a lado
    ccx_codex switch [n]         troca manual
    ccx_codex auto               monitora e troca sozinho

Referencia de formato (lida do proprio Codex CLI):
  ~/.codex/auth.json  -> auth_mode, OPENAI_API_KEY, last_refresh,
                         tokens{id_token, access_token, refresh_token, account_id}

Diferencas relevantes em relacao ao modulo Claude (ccx.py):
  - Um arquivo so (auth.json), nao dois. Nao ha um equivalente ao mcpOAuth
    para preservar, mas a troca continua cirurgica (so mexe em "tokens" e
    "last_refresh") caso o Codex CLI passe a gravar outras chaves no futuro.
  - Sem "expiresAt" gravado: a expiracao vem do claim "exp" do access_token,
    que e um JWT. Decodificado localmente so para leitura, sem verificar
    assinatura (o servidor e quem valida de verdade quando o token e usado).
  - SEM lock cooperativo confirmado com o Codex CLI real. O modulo Claude
    tem um lock de diretorio documentado no proprio codigo do Claude Code;
    nao ha confirmacao equivalente para o Codex CLI (o codex-lb, projeto que
    inspirou este modulo, e um proxy de rede e nunca escreve o auth.json
    local por baixo de um Codex CLI rodando, entao nao serviu de referencia
    aqui). A escrita continua atomica (tmp + os.replace), mas trocar bem no
    instante em que o Codex CLI esta renovando o proprio token e uma janela
    de corrida que este modulo nao cobre. Evite rodar `ccx_codex auto` e usar
    o Codex CLI ativamente no mesmo segundo em que uma troca cair.
  - Rotulos internos "5h"/"7d" sao so posicionais (primary_window vira "5h",
    secondary_window vira "7d"), nao uma promessa de duracao: toda a engine
    de decisao do ccx.py (pick_target, band_delay, next_wake) e reusada sem
    modificacao porque ela so olha pct/resets_at, nao a duracao real da
    janela. O rotulo MOSTRADO no status, esse sim, e calculado a partir de
    "limit_window_seconds" que a API manda, entao nao mente. Isso importa na
    pratica: em julho/2026 a OpenAI tirou a janela curta do endpoint de usage
    (ainda em beta, pode voltar) e "primary_window" passou a trazer sozinha a
    janela semanal (604800s). Sem essa conta dinamica, o status mostraria uma
    coluna "5h" resetando em 158h, o que e mentira. Se so uma janela vier
    preenchida (a outra "null"), a coluna da ausente aparece com "-" e o
    resto continua funcionando normalmente, decisao de troca incluida.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import ccx  # reusa IO, lock, decisao e formatacao do modulo Claude

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_SCOPE = "openid profile email"

STORE = Path.home() / ".ccx" / "codex_accounts.json"
REFRESH_SKEW_S = 300  # 5min, mesma folga do modulo Claude

# Codigos de erro documentados como permanentes pela propria API de refresh da
# OpenAI (visto no codex-lb, app/core/balancer/logic.py): refresh token morto
# de vez, nao adianta tentar de novo sem relogar.
DEAD_REFRESH_CODES = {
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
    "invalid_grant",
    "token_invalidated",
    "token_expired",
    "app_session_terminated",
    "account_session_expired",
    "account_auth_invalidated",
    "account_deactivated",
    "account_suspended",
    "account_deleted",
}

dir_lock = ccx.claude_lock  # generico: mkdir em <target>.lock e o mutex


# --------------------------------------------------------------------------
# caminhos e identidade


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def auth_path() -> Path:
    return codex_home() / "auth.json"


def jwt_claims(token: str) -> dict:
    """Claims do meio de um JWT, sem verificar assinatura (so leitura local).

    id_token e access_token do Codex CLI sao JWTs. O access_token traz "exp";
    o id_token traz email/account_id/workspace_id, inclusive dentro do claim
    aninhado "https://api.openai.com/auth".
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def identity_from_tokens(tokens: dict) -> dict:
    claims = jwt_claims(tokens.get("id_token") or "")
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    return {
        "email": claims.get("email") or "",
        "account_id": tokens.get("account_id") or auth_claims.get("chatgpt_account_id") or "",
        "workspace_id": auth_claims.get("workspace_id") or claims.get("workspace_id") or "",
    }


def token_expired(tokens: dict) -> bool:
    claims = jwt_claims(tokens.get("access_token") or "")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return datetime.now(timezone.utc).timestamp() + REFRESH_SKEW_S >= float(exp)


# --------------------------------------------------------------------------
# API


def refresh_tokens(tokens: dict) -> tuple[dict | None, str]:
    """Renova o par de tokens. Devolve (tokens_novos, erro).

    erro: "" ok, "dead" refresh token rejeitado de vez, "transient" o resto.
    """
    rt = tokens.get("refresh_token")
    if not rt:
        return None, "dead"
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": rt,
            "scope": OAUTH_SCOPE,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "ccx/1.0"},
        method="POST",
    )
    try:
        data = ccx.http_json(req)
        new = dict(tokens)
        for key in ("access_token", "refresh_token", "id_token"):
            if data.get(key):
                new[key] = data[key]
        return new, ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        try:
            err = json.loads(raw)
        except Exception:
            err = {}
        error_field = err.get("error")
        code = (error_field if isinstance(error_field, str) else None) or err.get("code")
        if e.code in (400, 401, 403) and (code in DEAD_REFRESH_CODES or "invalid_grant" in raw):
            return None, "dead"
        return None, "transient"
    except Exception:
        return None, "transient"


def _resets_at_iso(win: dict) -> str | None:
    reset_at = win.get("reset_at")
    if isinstance(reset_at, (int, float)):
        return datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()
    after = win.get("reset_after_seconds")
    if isinstance(after, (int, float)):
        ts = datetime.now(timezone.utc).timestamp() + after
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return None


def fetch_usage(access_token: str, account_id: str) -> dict:
    """Janela curta e semanal, no mesmo formato {"5h":..., "7d":...} do
    modulo Claude, para reusar pick_target/band_delay/next_wake sem
    modificacao. Levanta em erro: quem chama decide o que fazer.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "ccx/1.0",
    }
    if account_id and not account_id.startswith(("email_", "local_")):
        headers["chatgpt-account-id"] = account_id
    req = urllib.request.Request(USAGE_URL, headers=headers)
    raw = ccx.http_json(req, timeout=8.0)
    rate_limit = raw.get("rate_limit") or {}
    out = {}
    for key, label in (("primary_window", "5h"), ("secondary_window", "7d")):
        win = rate_limit.get(key)
        if isinstance(win, dict) and win.get("used_percent") is not None:
            out[label] = {
                "pct": float(win["used_percent"]),
                "resets_at": _resets_at_iso(win),
                "window_seconds": win.get("limit_window_seconds"),
            }
    if not out:
        # Resposta 200 sem nenhuma janela reconhecida: nao virar {} (isso
        # faria a conta parecer com 0% de uso e ser eleita por ignorancia).
        raise ValueError("resposta de usage sem janelas reconheciveis")
    return out


# --------------------------------------------------------------------------
# store


def load_store() -> dict:
    s = ccx.read_json(STORE)
    s.setdefault("slots", {})
    s.setdefault("last_switch", 0)
    s.setdefault("usage_cache", {})
    return s


def live_identity() -> tuple[dict, dict]:
    """(tokens, identidade) da conta ativa no Codex CLI agora."""
    auth = ccx.read_json(auth_path())
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise SystemExit(
            "Nenhuma conta Codex CLI logada nesta maquina. Rode 'codex login'."
        )
    return tokens, identity_from_tokens(tokens)


def active_slot(store: dict) -> str | None:
    """Casa por email (do id_token), nao por token: o Codex CLI tambem
    rotaciona o refresh token da conta ativa, entao comparar token perderia
    a identidade na primeira rotacao (mesmo risco do modulo Claude).
    """
    try:
        tokens, ident = live_identity()
    except SystemExit:
        return None
    email = (ident.get("email") or "").lower()
    if email:
        for key, slot in store["slots"].items():
            if (slot.get("email") or "").lower() == email:
                return key
    rt = tokens.get("refresh_token")
    for key, slot in store["slots"].items():
        if rt and slot["tokens"].get("refresh_token") == rt:
            return key
    return None


def sync_active_slot(store: dict, active: str | None) -> None:
    if not active or active not in store["slots"]:
        return
    try:
        tokens, ident = live_identity()
    except SystemExit:
        return
    slot = store["slots"][active]
    if slot["tokens"].get("access_token") == tokens.get("access_token"):
        return
    slot["tokens"] = tokens
    slot["email"] = ident.get("email") or slot.get("email", "")
    slot["account_id"] = ident.get("account_id") or slot.get("account_id", "")
    slot["workspace_id"] = ident.get("workspace_id") or slot.get("workspace_id", "")
    slot.pop("dead", None)
    ccx.write_json(STORE, store)


def apply_slot(slot: dict) -> None:
    """Troca a conta ativa escrevendo so 'tokens' e 'last_refresh'.

    Cirurgico como no modulo Claude: preserva auth_mode/OPENAI_API_KEY como
    estao no arquivo em vez de reescreve-lo inteiro.
    """
    target = auth_path()
    with dir_lock(target):
        auth = ccx.read_json(target)
        auth["tokens"] = slot["tokens"]
        auth["last_refresh"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        )
        ccx.write_json(target, auth)


def store_lock():
    return dir_lock(STORE.parent / "codex_store")


def slot_usage(key: str, slot: dict, is_active: bool, store: dict) -> tuple[dict | None, str]:
    tokens = slot["tokens"]
    if not is_active and token_expired(tokens):
        new, err = refresh_tokens(tokens)
        if new:
            slot["tokens"] = new
            tokens = new
            ccx.write_json(STORE, store)
        elif err == "dead":
            slot["dead"] = True
            ccx.write_json(STORE, store)
        else:
            return None, f"refresh falhou ({err})"
    if slot.get("dead"):
        return None, "token morto: relogue e rode 'ccx_codex add'"
    try:
        return fetch_usage(tokens["access_token"], slot.get("account_id", "")), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, type(e).__name__


def collect(store: dict) -> tuple[dict[str, dict | None], dict[str, str], str | None]:
    with store_lock():
        fresh = load_store()
        store.clear()
        store.update(fresh)
        active = active_slot(store)
        sync_active_slot(store, active)
        usage_map, err_map = {}, {}
        now = time.time()
        changed = False
        for key, slot in store["slots"].items():
            cached = ccx.cached_slot_usage(store, key, now)
            if cached is None:
                cached = slot_usage(key, slot, key == active, store)
                ccx.remember_slot_usage(store, key, *cached, now)
                changed = True
            usage_map[key], err_map[key] = cached
        if changed:
            ccx.write_json(STORE, store)
    return usage_map, err_map, active


# --------------------------------------------------------------------------
# comandos


def _window_label(seconds: object, fallback: str) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return fallback
    hours = seconds / 3600
    return f"{hours:.0f}h" if hours < 30 else f"{hours / 24:.0f}d"


def _column_labels(usage_map: dict) -> tuple[str, str]:
    """Rotulo de cada coluna a partir da duracao real da janela (quando a API
    manda "limit_window_seconds"), em vez de assumir "5h"/"7d" fixo. Sem
    isso a coluna mentiria se a OpenAI mudar (ou remover) uma janela, como
    aconteceu na beta atual do endpoint de usage.
    """
    label_5h, label_7d = "?", "?"
    for u in usage_map.values():
        if not u:
            continue
        if u.get("5h", {}).get("window_seconds"):
            label_5h = _window_label(u["5h"]["window_seconds"], "?")
        if u.get("7d", {}).get("window_seconds"):
            label_7d = _window_label(u["7d"]["window_seconds"], "?")
    return label_5h, label_7d


def cmd_add(args: argparse.Namespace) -> int:
    with store_lock():
        return _add(args)


def _add(args: argparse.Namespace) -> int:
    store = load_store()
    tokens, ident = live_identity()
    email = ident.get("email") or "?"
    existing = active_slot(store)
    if existing:
        slot = store["slots"][existing]
        slot.update(
            tokens=tokens,
            email=email,
            account_id=ident.get("account_id", ""),
            workspace_id=ident.get("workspace_id", ""),
        )
        slot.pop("dead", None)
        store.get("usage_cache", {}).pop(existing, None)
        ccx.write_json(STORE, store)
        print(f"slot {existing} atualizado: {email}")
        return 0
    if args.slot and args.slot in store["slots"]:
        ocupado = store["slots"][args.slot]["email"]
        print(
            f"Slot {args.slot} ja e de {ocupado}. Escolha outro numero ou remova "
            f"o arquivo {STORE} se quiser recomecar."
        )
        return 1
    key = args.slot or next(
        str(n) for n in range(1, len(store["slots"]) + 2) if str(n) not in store["slots"]
    )
    store["slots"][key] = {
        "email": email,
        "tokens": tokens,
        "account_id": ident.get("account_id", ""),
        "workspace_id": ident.get("workspace_id", ""),
    }
    ccx.write_json(STORE, store)
    print(f"slot {key} adicionado: {email}  (workspace {ident.get('workspace_id', '')[:8] or '-'})")
    # So acusa workspace compartilhado quando TODOS tem workspace_id conhecido
    # e igual: vazio significa "nao sei", nunca "igual ao outro".
    workspaces = [s.get("workspace_id") or "" for s in store["slots"].values()]
    if len(workspaces) > 1 and all(workspaces) and len(set(workspaces)) == 1:
        print(
            "  AVISO: as contas estao no mesmo workspace. Seat de Team/Enterprise\n"
            "  pode dividir cota, trocar pode nao adiantar."
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = load_store()
    if not store["slots"]:
        print("Nenhuma conta. Rode 'codex login' e depois 'ccx_codex add'.")
        return 1
    usage_map, err_map, active = collect(store)
    lbl5, lbl7 = _column_labels(usage_map)
    print(f"{'':2} {'conta':28} {lbl5:>6} {'reset':>7} {lbl7:>6} {'reset':>7}")
    for key, slot in store["slots"].items():
        usage = usage_map[key]
        mark = "*" if key == active else " "
        tag = f"  {err_map[key]}" if err_map[key] else ""
        print(
            f"{mark}{key} {slot['email'][:28]:28} "
            f"{ccx.fmt_window(usage, '5h')} {ccx.fmt_reset(usage, '5h'):>7} "
            f"{ccx.fmt_window(usage, '7d')} {ccx.fmt_reset(usage, '7d'):>7}{tag}"
        )
    target = ccx.pick_target(usage_map, args.threshold, args.strategy)
    hold_active = ccx.hold_active_on_unknown_usage(store, usage_map, active)
    if target and target != active:
        print(f"\n-> {args.strategy} recomenda o slot {target}")
        if hold_active:
            print(
                f"-> monitor mantem o slot {active}: erro da conta ativa nao "
                "confirma falta de cota"
            )
        else:
            print("-> troca pendente; monitor aplica automaticamente")
    elif not target:
        print("\n-> todas travadas em 100%, nada para onde trocar")
    if not target or target == active or hold_active:
        print(
            f"-> proxima checagem em "
            f"~{ccx.fmt_delay(ccx.next_wake(usage_map, err_map, active))}"
        )
    return 0


def do_switch(store: dict, key: str) -> None:
    with store_lock():
        fresh = load_store()
        slot = fresh["slots"].get(key)
        if slot is None:
            raise ValueError(f"Slot {key} nao existe mais.")
        apply_slot(slot)
        fresh["last_switch"] = time.time()
        fresh.get("usage_cache", {}).pop(key, None)
        ccx.write_json(STORE, fresh)
        store.clear()
        store.update(fresh)
    print(f"[{time.strftime('%H:%M:%S')}] slot {key} ativo: {slot['email']}")


def cmd_switch(args: argparse.Namespace) -> int:
    store = load_store()
    keys = list(store["slots"])
    if not keys:
        print("Nenhuma conta cadastrada.")
        return 1
    if args.slot:
        if args.slot not in store["slots"]:
            print(f"Slot {args.slot} nao existe. Tem: {', '.join(keys)}")
            return 1
        target = args.slot
    else:
        active = active_slot(store)
        target = keys[(keys.index(active) + 1) % len(keys)] if active else keys[0]
    do_switch(store, target)
    return 0


def check_once(args: argparse.Namespace) -> tuple[int, float]:
    """(codigo, segundos ate a proxima). 0 trocou, 2 nada a fazer, 3 sem alvo."""
    store = load_store()
    usage_map, err_map, active = collect(store)
    target = ccx.pick_target(usage_map, args.threshold, args.strategy)
    delay = float(args.poll) if args.poll else ccx.next_wake(usage_map, err_map, active)
    stamp = time.strftime("%H:%M:%S")
    if ccx.hold_active_on_unknown_usage(store, usage_map, active):
        print(
            f"[{stamp}] cota do slot {active} ilegivel ({err_map.get(active)}), "
            "mantendo a conta atual"
        )
        return 2, delay
    line = "  ".join(
        f"{k}{'*' if k == active else ''}:{ccx.fmt_window(u, '5h').strip()}/"
        f"{ccx.fmt_window(u, '7d').strip()}"
        for k, u in usage_map.items()
    )
    problems = [f"{k} {e}" for k, e in err_map.items() if e]
    tail = f"  [{'; '.join(problems)}]" if problems else ""
    nxt = f"  proxima em {ccx.fmt_delay(delay)}"
    if target is None:
        print(f"[{stamp}] {line}  todas travadas{tail}{nxt}")
        return 3, delay
    if target == active:
        print(f"[{stamp}] {line}  ok{tail}{nxt}")
        return 2, delay
    waited = time.time() - store["last_switch"]
    if waited < args.cooldown:
        print(f"[{stamp}] {line}  cooldown {int(args.cooldown - waited)}s{tail}")
        return 2, min(delay, args.cooldown - waited + 1)
    print(f"[{stamp}] {line}  trocando {active} -> {target}{tail}")
    do_switch(store, target)
    return 0, delay


def cmd_auto(args: argparse.Namespace) -> int:
    store = load_store()
    if len(store["slots"]) < 2:
        print("Precisa de pelo menos 2 contas. Use 'ccx_codex add' com cada uma logada.")
        return 1
    if args.once:
        return check_once(args)[0]

    try:
        guard = dir_lock(STORE.parent / "codex_auto", timeout=0)
        guard.__enter__()
    except TimeoutError:
        print("ccx_codex auto ja esta rodando nesta maquina. Nada a fazer.")
        return 0

    faixa = "dinamico 100-240s" if not args.poll else f"fixo {args.poll}s"
    print(
        f"ccx_codex auto: {args.strategy}, limiar {args.threshold}%, poll {faixa}. "
        "Ctrl+C para sair."
    )
    try:
        while True:
            try:
                _, delay = check_once(args)
            except TimeoutError as e:
                print(f"[{time.strftime('%H:%M:%S')}] {e}")
                delay = ccx.random.uniform(*ccx.POLL_WIDE)
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nsaindo")
        return 0
    finally:
        guard.__exit__(None, None, None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ccx_codex", description=__doc__.split("\n")[0])
    ap.add_argument("--threshold", type=float, default=ccx.DEFAULT_THRESHOLD)
    ap.add_argument(
        "--strategy", choices=("consume-first", "best"), default="consume-first"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="captura a conta logada agora")
    p.add_argument("slot", nargs="?")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("status", help="as contas lado a lado")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("switch", help="troca manual")
    p.add_argument("slot", nargs="?")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("auto", help="monitora e troca sozinho")
    p.add_argument(
        "--poll", type=int, default=0, help="segundos fixos (0 = dinamico, padrao)"
    )
    p.add_argument("--cooldown", type=int, default=ccx.DEFAULT_COOLDOWN_S)
    p.add_argument("--once", action="store_true", help="uma checagem so, para cron")
    p.set_defaults(func=cmd_auto)

    args = ap.parse_args(argv)
    if not 0 < args.threshold <= 100:
        ap.error("--threshold precisa estar entre 0 (exclusivo) e 100")
    if getattr(args, "poll", 0) < 0:
        ap.error("--poll nao pode ser negativo")
    if getattr(args, "cooldown", 0) < 0:
        ap.error("--cooldown nao pode ser negativo")
    try:
        return args.func(args)
    except ccx.CorruptFile as e:
        print(f"ERRO: {e}")
        return 1
    except TimeoutError as e:
        print(f"ERRO: {e}")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
