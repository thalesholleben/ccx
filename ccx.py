#!/usr/bin/env python3
"""ccx - monitora a cota das contas Claude Code e troca antes de bater o limite.

Sem ping, sem prompt: a cota vem da API de usage (leitura pura, nao abre janela
de 5h e nao consome cota).

    ccx add                captura a conta logada agora num slot
    ccx status             as contas lado a lado
    ccx switch [n]         troca manual
    ccx auto               monitora e troca sozinho

Referencias de formato (lidas do proprio Claude Code):
  ~/.claude/.credentials.json  -> bloco claudeAiOauth + organizationUuid
  ~/.claude.json               -> oauthAccount (identidade exibida)
  ~/.claude.lock               -> proper-lockfile do refresh de token
"""

from __future__ import annotations

import argparse
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA_HEADER = "oauth-2025-04-20"

STORE = Path.home() / ".ccx" / "accounts.json"
# proper-lockfile do Claude Code: stale em 10s, holder toca a cada 5s.
LOCK_STALE_S = 10.0
LOCK_TOUCH_S = 3.0
LOCK_TIMEOUT_S = 9.0
REFRESH_SKEW_MS = 5 * 60 * 1000

DEFAULT_THRESHOLD = 85.0
DEFAULT_COOLDOWN_S = 300
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)

# Intervalo de poll dinamico. Cota sobe devagar, entao checar de minuto em minuto
# com a conta folgada e so trafego. Aperta quando a conta ativa passa de
# TIGHTEN_AT. O jitter tambem evita um batimento perfeitamente periodico.
POLL_WIDE = (180.0, 240.0)
POLL_TIGHT = (100.0, 120.0)
POLL_TIGHTEN_AT = 70.0
# Com menos de 2 contas utilizaveis nao ha decisao de troca a tomar, entao nao
# se faz polling: dorme ate o reset conhecido da primeira que volta. O teto e
# seguro barato caso o resets_at venha errado ou mude.
SLEEP_CAP_S = 3600.0
SLEEP_FLOOR_S = 60.0
SLEEP_MARGIN_S = 15.0

_SSL_CTX: ssl.SSLContext | None = None


# --------------------------------------------------------------------------
# caminhos e IO


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def creds_path() -> Path:
    return claude_home() / ".credentials.json"


def global_config_path() -> Path:
    legacy = claude_home() / ".config.json"
    if legacy.exists():
        return legacy
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home()) / ".claude.json"


class CorruptFile(Exception):
    """Arquivo existe mas nao e JSON valido."""


def read_json(path: Path) -> dict:
    """Conteudo do arquivo, ou {} se ele nao existe.

    JSON invalido levanta em vez de virar {}. Confundir "corrompido" com
    "vazio" e destrutivo: o passo seguinte reescreve o arquivo e leva junto o
    que nao foi lido, como o mcpOAuth do .credentials.json.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise CorruptFile(f"{path} nao e JSON valido ({e}). Nao vou sobrescrever.")


def write_json(path: Path, data: dict) -> None:
    """Escreve via tmp + os.replace para nao deixar arquivo truncado."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".ccx-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except BaseException:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)


@contextmanager
def claude_lock(target: Path, timeout: float = LOCK_TIMEOUT_S):
    """Segura o lock do Claude Code (diretorio <target>.lock, mkdir e o mutex).

    Sem isso, uma troca que cai no meio do refresh do Claude Code e sobrescrita
    pelo token da conta velha, e a troca se perde em silencio.
    """
    lock_dir = target.parent / (target.name + ".lock")
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            pass
        if time.monotonic() - start > timeout:
            raise TimeoutError(
                f"{lock_dir.name} preso: Claude Code parece estar renovando o "
                "token. Tente de novo em alguns segundos."
            )
        try:
            age = time.time() - os.stat(lock_dir).st_mtime
        except FileNotFoundError:
            continue
        if age > LOCK_STALE_S:
            try:
                os.rmdir(lock_dir)
            except OSError:
                time.sleep(0.05)
            continue
        time.sleep(0.25 + random.random() * 0.25)

    stop = threading.Event()

    def keepalive() -> None:
        while not stop.wait(LOCK_TOUCH_S):
            try:
                os.utime(lock_dir)
            except OSError:
                return

    t = threading.Thread(target=keepalive, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=1.0)
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


# --------------------------------------------------------------------------
# API


@contextmanager
def store_lock():
    """Serializa qualquer operacao do ccx que renove token ou grave o store.

    Sem isso, `status` rodando junto com `auto` faz os dois renovarem o mesmo
    refresh token; a rotacao do servidor invalida a copia de quem perdeu a
    corrida, e a credencial guardada morre com invalid_grant na proxima vez que
    voce tentar voltar para aquela conta.
    """
    with claude_lock(STORE.parent / "store"):
        yield


def ssl_context() -> ssl.SSLContext:
    """Contexto TLS preferindo o bundle do certifi quando ele existe.

    O armazenamento de certificados do Windows carrega raizes legadas expiradas,
    e a cadeia da Let's Encrypt (platform.claude.com, onde fica o endpoint de
    refresh) falha por ali com CERTIFICATE_VERIFY_FAILED enquanto
    api.anthropic.com, que usa outra CA, passa. O sintoma e cruel: a leitura de
    cota funciona e so o refresh quebra, entao a conta inativa vai apodrecendo
    sem erro visivel. Sem certifi, cai no padrao e segue funcionando.
    """
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import certifi

            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def http_json(req: urllib.request.Request, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        return json.loads(resp.read().decode())


def fetch_usage(token: str) -> dict:
    """5h e 7d da conta. Levanta em erro: quem chama decide o que fazer."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": "ccx/1.0",
        },
    )
    raw = http_json(req, timeout=8.0)
    out = {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        win = raw.get(key)
        if isinstance(win, dict) and win.get("utilization") is not None:
            out[label] = {
                "pct": float(win["utilization"]),
                "resets_at": win.get("resets_at"),
            }
    if not out:
        # Resposta 200 sem nenhuma janela reconhecida. Devolver {} faria
        # utilization() responder 0.0, e a conta seria eleita como a mais
        # folgada de todas justamente por nao sabermos nada dela.
        raise ValueError("resposta de usage sem janelas reconheciveis")
    return out


def token_expired(oauth: dict) -> bool:
    exp = oauth.get("expiresAt")
    if not isinstance(exp, (int, float)):
        return False
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms + REFRESH_SKEW_MS >= int(exp)


def refresh_token(oauth: dict) -> tuple[dict | None, str]:
    """Renova o access token. Devolve (bloco_novo, erro).

    erro: "" ok, "dead" refresh token rejeitado de vez, "transient" o resto.
    """
    rt = oauth.get("refreshToken")
    if not rt:
        return None, "dead"
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID}
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "ccx/1.0"},
        method="POST",
    )
    try:
        data = http_json(req)
        # Parsing dentro do try: um 200 com corpo inesperado levantaria KeyError
        # fora dele e derrubaria o processo em vez de virar erro transitorio.
        new = dict(oauth)
        new["accessToken"] = data["access_token"]
        new["expiresAt"] = int(datetime.now(timezone.utc).timestamp() * 1000) + int(
            data["expires_in"]
        ) * 1000
        if data.get("refresh_token"):
            new["refreshToken"] = data["refresh_token"]
        if data.get("scope"):
            new["scopes"] = data["scope"].split()
        return new, ""
    except urllib.error.HTTPError as e:
        payload = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        if e.code in (400, 401, 403) and (
            "invalid_grant" in payload or "invalid_client" in payload
        ):
            return None, "dead"
        return None, "transient"
    except Exception:
        return None, "transient"


# --------------------------------------------------------------------------
# store


def load_store() -> dict:
    s = read_json(STORE)
    s.setdefault("slots", {})
    s.setdefault("last_switch", 0)
    return s


def live_identity() -> tuple[dict, dict, str]:
    """(bloco claudeAiOauth, oauthAccount, organizationUuid) da conta ativa.

    O Claude Code nem sempre grava organizationUuid no topo do .credentials.json,
    entao cai para o do oauthAccount, que sempre tem.
    """
    creds = read_json(creds_path())
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise SystemExit(
            "Nenhuma conta Claude Code logada nesta maquina. Rode 'claude' e faca login."
        )
    account = read_json(global_config_path()).get("oauthAccount") or {}
    org = (creds.get("organizationUuid") or account.get("organizationUuid") or "")
    return oauth, account, org


def active_slot(store: dict) -> str | None:
    """Slot correspondente a conta logada agora.

    Casa por (email, organizationUuid), NAO por token. O Claude Code rotaciona
    o refresh token da conta que esta usando, entao comparar token faz a
    identidade se perder na primeira rotacao. E quando ela se perde, todo slot
    parece inativo, inclusive o vivo, que passa a ser renovado por baixo do
    Claude Code: justamente o que nunca pode acontecer.
    """
    try:
        oauth, account, org = live_identity()
    except SystemExit:
        return None
    email = (account.get("emailAddress") or "").lower()
    if email:
        for key, slot in store["slots"].items():
            if (slot.get("email") or "").lower() != email:
                continue
            stored_org = slot.get("org_uuid") or ""
            if not org or not stored_org or stored_org == org:
                return key
    rt = oauth.get("refreshToken")  # fallback: .claude.json sem identidade
    for key, slot in store["slots"].items():
        if rt and slot["oauth"].get("refreshToken") == rt:
            return key
    return None


def sync_active_slot(store: dict, active: str | None) -> None:
    """Copia a credencial viva para o slot ativo.

    O Claude Code rotaciona o token da conta que usa, e o refresh token antigo
    morre. Sem re-sincronizar, a copia guardada apodrece e a conta fica
    inutilizavel quando voce tenta voltar para ela (invalid_grant no refresh).
    """
    if not active or active not in store["slots"]:
        return
    try:
        oauth, account, org = live_identity()
    except SystemExit:
        return
    slot = store["slots"][active]
    if slot["oauth"].get("accessToken") == oauth.get("accessToken"):
        return
    slot["oauth"] = oauth
    if account:
        slot["account"] = account
        slot["email"] = account.get("emailAddress") or slot.get("email", "")
    if org:
        slot["org_uuid"] = org
    slot.pop("dead", None)
    write_json(STORE, store)


def apply_slot(slot: dict) -> None:
    """Troca a conta ativa. Cirurgico: mexe so no que identifica a conta.

    O .credentials.json guarda tambem mcpOAuth (tokens de MCP, ex. HeyGen).
    Copiar o arquivo inteiro derrubaria esses logins a cada troca.
    """
    target = creds_path()
    cfg_path = global_config_path()
    # Os dois locks juntos: entre as duas escritas o estado fica inconsistente
    # (token da conta B com oauthAccount da A), e nessa janela o active_slot le
    # a identidade errada e o sync copia o token de B para o slot da A.
    with claude_lock(claude_home()), claude_lock(cfg_path):
        creds = read_json(target)
        creds["claudeAiOauth"] = slot["oauth"]
        # Sempre escreve a identidade, inclusive removendo: deixar o valor
        # antigo casaria o token de uma conta com a organizacao de outra.
        if slot.get("org_uuid"):
            creds["organizationUuid"] = slot["org_uuid"]
        else:
            creds.pop("organizationUuid", None)
        write_json(target, creds)

        cfg = read_json(cfg_path)
        if slot.get("account"):
            cfg["oauthAccount"] = slot["account"]
        else:
            cfg.pop("oauthAccount", None)
        write_json(cfg_path, cfg)


def slot_usage(
    key: str, slot: dict, is_active: bool, store: dict
) -> tuple[dict | None, str]:
    """(cota, erro) do slot. Renova o token so de conta inativa: a ativa e do
    Claude Code, e se os dois renovarem um invalida o token do outro.

    O erro volta para quem chama em vez de virar None silencioso: um 429 no
    endpoint de usage deixaria as duas contas como '?' para sempre sem nenhuma
    pista do motivo.
    """
    oauth = slot["oauth"]
    if not is_active and token_expired(oauth):
        new, err = refresh_token(oauth)
        if new:
            slot["oauth"] = new
            oauth = new
            write_json(STORE, store)
        elif err == "dead":
            slot["dead"] = True
            write_json(STORE, store)
        else:
            # Refresh transitorio falhou. Bater no usage com um token que
            # sabidamente expirou nao pode dar certo e ainda queima a cota do
            # endpoint: foi assim que um refresh quebrado virou HTTP 429.
            return None, f"refresh falhou ({err})"
    if slot.get("dead"):
        return None, "token morto: relogue e rode 'ccx add'"
    try:
        return fetch_usage(oauth["accessToken"]), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, type(e).__name__


# --------------------------------------------------------------------------
# decisao


def parse_reset(value: str | None) -> datetime:
    """Sempre devolve datetime com fuso. Um ISO sem offset e assumido UTC.

    Sem isso, uma resposta com timestamp ingenuo faria toda subtracao contra
    now(utc) levantar TypeError e derrubar o loop.
    """
    if not value:
        return FAR_FUTURE
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return FAR_FUTURE
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def utilization(usage: dict) -> float:
    """A janela que aperta: quem bloqueia e a mais alta das duas."""
    return max((w["pct"] for w in usage.values()), default=0.0)


def pick_target(
    usage_map: dict[str, dict | None], threshold: float, strategy: str
) -> str | None:
    """Slot que deveria estar ativo agora, ou None se nenhum tem folga.

    consume-first: entre os que tem folga, o de reset semanal mais proximo,
    porque cota semanal e pereciva. best: o de maior folga.
    Cota desconhecida (None) nunca e escolhida.

    Quando ninguem esta abaixo do limiar, cai para a menos pior que ainda nao
    esta travada (util < 100). O limiar existe para trocar ANTES de bater; nao
    serve de motivo para deixar voce parado numa conta travada tendo outra que
    ainda atende.
    """
    under, spare = [], []
    for key, usage in usage_map.items():
        if usage is None:
            continue
        util = utilization(usage)
        weekly = parse_reset((usage.get("7d") or {}).get("resets_at"))
        if util >= threshold:
            if util < 100:
                spare.append((key, util, weekly))
            continue
        under.append((key, util, weekly))
    ranked = under or spare
    if not ranked:
        return None
    if strategy == "consume-first" and under:
        ranked.sort(key=lambda r: (r[2], r[1]))
    else:
        # No fallback a folga e o que importa: reset semanal proximo nao ajuda
        # quem ja nao tem cota para gastar.
        ranked.sort(key=lambda r: r[1])
    return ranked[0][0]


# --------------------------------------------------------------------------
# comandos


def fmt_window(usage: dict | None, label: str) -> str:
    if usage is None:
        return "   ?  "
    win = usage.get(label)
    if not win:
        return "   -  "
    return f"{win['pct']:5.1f}%"


def fmt_reset(usage: dict | None, label: str) -> str:
    if usage is None:
        return "?"
    win = usage.get(label)
    if not win or not win.get("resets_at"):
        return "-"
    delta = parse_reset(win["resets_at"]) - datetime.now(timezone.utc)
    mins = int(delta.total_seconds() // 60)
    if mins < 0:
        return "agora"
    return f"{mins // 60}h{mins % 60:02d}m"


def fmt_delay(secs: float) -> str:
    return f"{secs:.0f}s" if secs < 120 else f"{secs / 60:.0f}min"


def collect(store: dict) -> tuple[dict[str, dict | None], dict[str, str], str | None]:
    """Cota de cada slot. Sob store_lock: renova tokens e grava o store."""
    with store_lock():
        active = active_slot(store)
        sync_active_slot(store, active)
        usage_map, err_map = {}, {}
        for key, slot in store["slots"].items():
            usage_map[key], err_map[key] = slot_usage(key, slot, key == active, store)
    return usage_map, err_map, active


def available_at(usage: dict) -> datetime | None:
    """Quando a conta volta a poder atender, ou None se ja pode agora.

    Travada e a janela em 100%. Precisa das duas abaixo de 100 para atender,
    entao quem manda e o reset mais TARDE entre as travadas.
    """
    blocked = [w for w in usage.values() if w["pct"] >= 100]
    if not blocked:
        return None
    return max(parse_reset(w.get("resets_at")) for w in blocked)


def band_delay(usage_map: dict[str, dict | None], active: str | None) -> float:
    """Faixa de poll com jitter, guiada pela janela de 5h da conta ativa.

    O 5h e a unica que se move rapido dentro de uma sessao. O semanal sobe
    devagar e leva dias para resetar, entao usa-lo aqui prenderia o poll na
    faixa apertada por dias inteiros sem que nada estivesse por acontecer.
    """
    ref = usage_map.get(active) if active else None
    if not ref:
        ref = max(
            (u for u in usage_map.values() if u),
            key=lambda u: (u.get("5h") or {}).get("pct", 0.0),
            default=None,
        )
    if ref and ref.get("5h"):
        util = ref["5h"]["pct"]
    else:
        util = utilization(ref) if ref else 0.0
    return random.uniform(*(POLL_TIGHT if util >= POLL_TIGHTEN_AT else POLL_WIDE))


def next_wake(
    usage_map: dict[str, dict | None], err_map: dict[str, str], active: str | None
) -> float:
    """Segundos ate a proxima checagem.

    Polling so serve para decidir uma troca, e decisao de troca exige duas
    contas utilizaveis. Com menos que isso nao ha o que decidir ate uma delas
    resetar, e o horario do reset e conhecido: dorme ate lá em vez de checar.

    Erro em qualquer conta cai para a faixa larga: se o endpoint esta empurrando
    de volta, insistir de 100 em 100s so piora.
    """
    if any(err_map.values()):
        return random.uniform(*POLL_WIDE)
    known = [u for u in usage_map.values() if u]
    returns = [available_at(u) for u in known]
    if sum(1 for t in returns if t is None) >= 2:
        return band_delay(usage_map, active)
    pending = [t for t in returns if t is not None]
    if not pending:
        return random.uniform(*POLL_WIDE)
    secs = (min(pending) - datetime.now(timezone.utc)).total_seconds()
    return max(SLEEP_FLOOR_S, min(secs + SLEEP_MARGIN_S, SLEEP_CAP_S))


def cmd_add(args: argparse.Namespace) -> int:
    with store_lock():
        return _add(args)


def _add(args: argparse.Namespace) -> int:
    store = load_store()
    oauth, account, org = live_identity()
    email = account.get("emailAddress") or "?"
    # Casa por identidade, nao por token: o refresh token roda, e comparar por
    # ele faria um segundo `add` da mesma conta criar um slot duplicado.
    existing = active_slot(store)
    if existing:
        slot = store["slots"][existing]
        slot.update(oauth=oauth, account=account, org_uuid=org, email=email)
        slot.pop("dead", None)
        write_json(STORE, store)
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
        "oauth": oauth,
        "account": account,
        "org_uuid": org,
    }
    write_json(STORE, store)
    print(f"slot {key} adicionado: {email}  (org {org[:8] or '?'})")
    # So acusa org compartilhada quando TODAS as contas tem org conhecida e igual:
    # org vazia significa "nao sei", nunca "igual a da outra".
    orgs = [s.get("org_uuid") or "" for s in store["slots"].values()]
    if len(orgs) > 1 and all(orgs) and len(set(orgs)) == 1:
        print(
            "  AVISO: as contas estao na mesma organizacao. Contas com o mesmo\n"
            "  organizationUuid dividem uma cota so, entao trocar nao adianta."
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = load_store()
    if not store["slots"]:
        print("Nenhuma conta. Logue no Claude Code e rode 'ccx add'.")
        return 1
    usage_map, err_map, active = collect(store)
    print(f"{'':2} {'conta':28} {'5h':>6} {'reset':>7} {'7d':>6} {'reset':>7}")
    for key, slot in store["slots"].items():
        usage = usage_map[key]
        mark = "*" if key == active else " "
        tag = f"  {err_map[key]}" if err_map[key] else ""
        print(
            f"{mark}{key} {slot['email'][:28]:28} "
            f"{fmt_window(usage, '5h')} {fmt_reset(usage, '5h'):>7} "
            f"{fmt_window(usage, '7d')} {fmt_reset(usage, '7d'):>7}{tag}"
        )
    target = pick_target(usage_map, args.threshold, args.strategy)
    if target and target != active:
        print(f"\n-> {args.strategy} recomenda o slot {target}")
        print("-> troca pendente; 'status' nao acorda o monitor")
    elif not target:
        print("\n-> todas travadas em 100%, nada para onde trocar")
    if not target or target == active:
        print(f"-> proxima checagem em ~{fmt_delay(next_wake(usage_map, err_map, active))}")
    return 0


def do_switch(store: dict, key: str) -> None:
    slot = store["slots"][key]
    with store_lock():
        apply_slot(slot)
        store["last_switch"] = time.time()
        write_json(STORE, store)
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
    target = pick_target(usage_map, args.threshold, args.strategy)
    delay = float(args.poll) if args.poll else next_wake(usage_map, err_map, active)
    stamp = time.strftime("%H:%M:%S")
    # Nao abandonar a conta ativa so porque a cota dela ficou ilegivel. Nao
    # conseguir LER a cota nao diz nada sobre a conta funcionar, e trocar nesse
    # estado e decidir por ignorancia: foi assim que uma falha de refresh no
    # slot ativo jogou a sessao numa conta com 99% do semanal gasto. Token
    # morto e diferente, ai sabemos que quebrou e sair e o certo.
    if (
        active
        and usage_map.get(active) is None
        and not store["slots"].get(active, {}).get("dead")
    ):
        print(
            f"[{stamp}] cota do slot {active} ilegivel ({err_map.get(active)}), "
            "mantendo a conta atual"
        )
        return 2, delay
    line = "  ".join(
        f"{k}{'*' if k == active else ''}:{fmt_window(u, '5h').strip()}/"
        f"{fmt_window(u, '7d').strip()}"
        for k, u in usage_map.items()
    )
    problems = [f"{k} {e}" for k, e in err_map.items() if e]
    tail = f"  [{'; '.join(problems)}]" if problems else ""
    nxt = f"  proxima em {fmt_delay(delay)}"
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
        print("Precisa de pelo menos 2 contas. Use 'ccx add' com cada uma logada.")
        return 1
    if args.once:
        return check_once(args)[0]

    # Uma instancia por maquina. Sem isso, abrir a pasta em tres janelas do VS
    # Code sobe tres loops que triplicam o trafego e disputam a mesma troca.
    # Reusa o lock de diretorio: timeout 0 falha na hora se ja tem alguem.
    try:
        guard = claude_lock(STORE.parent / "auto", timeout=0)
        guard.__enter__()
    except TimeoutError:
        print("ccx auto ja esta rodando nesta maquina. Nada a fazer.")
        return 0

    faixa = "dinamico 100-240s" if not args.poll else f"fixo {args.poll}s"
    print(
        f"ccx auto: {args.strategy}, limiar {args.threshold}%, poll {faixa}. "
        "Ctrl+C para sair."
    )
    try:
        while True:
            try:
                _, delay = check_once(args)
            except TimeoutError as e:
                print(f"[{time.strftime('%H:%M:%S')}] {e}")
                delay = random.uniform(*POLL_WIDE)
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nsaindo")
        return 0
    finally:
        guard.__exit__(None, None, None)


def cmd_hook(args: argparse.Namespace) -> int:
    """Checagem silenciosa para hooks do Claude Code."""
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink):
        check_once(args)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ccx", description=__doc__.split("\n")[0])
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
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
    p.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_S)
    p.add_argument("--once", action="store_true", help="uma checagem so, para cron")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("hook", help="checagem silenciosa para hook Stop")
    p.set_defaults(
        func=cmd_hook, poll=0, cooldown=DEFAULT_COOLDOWN_S, once=True
    )

    args = ap.parse_args(argv)
    # Validacao: limiar acima de 100 elegeria conta travada, e poll negativo
    # levaria o loop a time.sleep(-1) e encerraria com erro.
    if not 0 < args.threshold <= 100:
        ap.error("--threshold precisa estar entre 0 (exclusivo) e 100")
    if getattr(args, "poll", 0) < 0:
        ap.error("--poll nao pode ser negativo")
    if getattr(args, "cooldown", 0) < 0:
        ap.error("--cooldown nao pode ser negativo")
    try:
        return args.func(args)
    except CorruptFile as e:
        print(f"ERRO: {e}")
        return 1
    except TimeoutError as e:
        print(f"ERRO: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
