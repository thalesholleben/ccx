#!/usr/bin/env python3
"""Checagem minima do ccx_codex: python test_ccx_codex.py"""

import base64
import json
import os
import tempfile
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import ccx
import ccx_codex


def make_jwt(claims: dict) -> str:
    """JWT fake so para leitura: cabecalho/assinatura nao importam aqui,
    jwt_claims() so olha o segundo segmento."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def test_jwt_claims_decodifica_e_tolera_lixo():
    tok = make_jwt({"email": "a@x.com", "exp": 123})
    assert ccx_codex.jwt_claims(tok) == {"email": "a@x.com", "exp": 123}
    assert ccx_codex.jwt_claims("nao-e-jwt") == {}
    assert ccx_codex.jwt_claims("") == {}
    assert ccx_codex.jwt_claims("a.b.c") == {}  # b nao decodifica pra JSON valido


def test_identity_from_tokens():
    id_token = make_jwt(
        {
            "email": "a@x.com",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc-123",
                "workspace_id": "ws-1",
            },
        }
    )
    ident = ccx_codex.identity_from_tokens({"id_token": id_token})
    assert ident == {"email": "a@x.com", "account_id": "acc-123", "workspace_id": "ws-1"}
    # account_id no topo do auth.json manda sobre o do claim aninhado
    ident2 = ccx_codex.identity_from_tokens({"id_token": id_token, "account_id": "acc-topo"})
    assert ident2["account_id"] == "acc-topo"
    # sem id_token nao quebra, so vem tudo vazio
    assert ccx_codex.identity_from_tokens({}) == {"email": "", "account_id": "", "workspace_id": ""}


def test_token_expirado_via_exp_do_jwt():
    now = datetime.now(timezone.utc).timestamp()
    expirando = {"access_token": make_jwt({"exp": now + 60})}  # dentro da folga de 5min
    folgado = {"access_token": make_jwt({"exp": now + 3600})}
    assert ccx_codex.token_expired(expirando)
    assert not ccx_codex.token_expired(folgado)
    assert not ccx_codex.token_expired({})  # sem access_token, nao presume expirado


def test_fetch_usage_mapeia_primary_secondary_para_5h_7d():
    import unittest.mock as mock

    reset_at = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 42.0, "reset_at": reset_at},
            "secondary_window": {"used_percent": 10.0, "reset_after_seconds": 3600},
        }
    }
    with mock.patch.object(ccx, "http_json", return_value=payload):
        usage = ccx_codex.fetch_usage("tok", "acc-1")
    assert usage["5h"]["pct"] == 42.0
    assert usage["7d"]["pct"] == 10.0
    assert usage["5h"]["resets_at"] is not None
    assert usage["7d"]["resets_at"] is not None
    # formato compativel com a engine de decisao do ccx.py
    assert ccx.utilization(usage) == 42.0


def test_fetch_usage_sem_janela_reconhecida_levanta():
    import unittest.mock as mock

    with mock.patch.object(ccx, "http_json", return_value={"rate_limit": {}}):
        try:
            ccx_codex.fetch_usage("tok", "acc-1")
            raise AssertionError("deveria ter levantado")
        except ValueError:
            pass


def test_refresh_tokens_classifica_erro():
    import unittest.mock as mock
    import urllib.error

    # sem refresh_token no slot: morto na hora, sem nem tentar a rede
    new, err = ccx_codex.refresh_tokens({})
    assert new is None and err == "dead"

    # invalid_grant -> permanente
    resp_body = json.dumps({"error": "invalid_grant"}).encode()
    exc = urllib.error.HTTPError("url", 400, "bad", {}, None)
    exc.read = lambda: resp_body
    with mock.patch.object(ccx.urllib.request, "urlopen", side_effect=exc):
        new, err = ccx_codex.refresh_tokens({"refresh_token": "rt"})
    assert new is None and err == "dead"

    # 500 generico -> transitorio
    exc2 = urllib.error.HTTPError("url", 500, "boom", {}, None)
    exc2.read = lambda: b"{}"
    with mock.patch.object(ccx.urllib.request, "urlopen", side_effect=exc2):
        new, err = ccx_codex.refresh_tokens({"refresh_token": "rt"})
    assert new is None and err == "transient"


def test_swap_preserva_auth_mode_e_api_key():
    """A troca nao pode apagar auth_mode/OPENAI_API_KEY que ja estavam no
    arquivo, mesmo trocando so o bloco tokens."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CODEX_HOME"] = tmp
        try:
            ccx.write_json(
                ccx_codex.auth_path(),
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {"access_token": "velho", "refresh_token": "rv"},
                    "last_refresh": "2020-01-01T00:00:00.000000Z",
                },
            )
            ccx_codex.apply_slot(
                {
                    "tokens": {"access_token": "novo", "refresh_token": "rn", "id_token": "id"},
                    "email": "b@x.com",
                }
            )
            auth = ccx.read_json(ccx_codex.auth_path())
            assert auth["tokens"]["access_token"] == "novo"
            assert auth["auth_mode"] == "chatgpt", "auth_mode foi perdido na troca"
            assert "OPENAI_API_KEY" in auth, "OPENAI_API_KEY foi perdido na troca"
            assert auth["last_refresh"] != "2020-01-01T00:00:00.000000Z"
        finally:
            os.environ.pop("CODEX_HOME", None)


def _store(tmp_home):
    ccx_codex.STORE = Path(tmp_home) / ".ccx" / "codex_accounts.json"
    s = {
        "slots": {
            "1": {"email": "a@x.com", "tokens": {"access_token": "a1", "refresh_token": "ra"}},
            "2": {"email": "b@x.com", "tokens": {"access_token": "b1", "refresh_token": "rb"}},
        },
        "last_switch": 0,
    }
    ccx.write_json(ccx_codex.STORE, s)
    return s


def test_identidade_sobrevive_rotacao_de_token():
    """O Codex CLI tambem rotaciona o refresh token da conta ativa; casar por
    email em vez de token evita que o slot vivo pareca inativo."""
    store_bkp = ccx_codex.STORE
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CODEX_HOME"] = tmp
        try:
            s = _store(tmp)
            id_token = make_jwt({"email": "B@X.com"})
            ccx.write_json(
                ccx_codex.auth_path(),
                {"tokens": {"access_token": "b9", "refresh_token": "rb-novo", "id_token": id_token}},
            )
            assert ccx_codex.active_slot(s) == "2"  # casa por email, sem depender de caixa

            ccx_codex.sync_active_slot(s, "2")
            assert s["slots"]["2"]["tokens"]["refresh_token"] == "rb-novo"
            persisted = json.loads(ccx_codex.STORE.read_text(encoding="utf-8"))
            assert persisted["slots"]["2"]["tokens"]["refresh_token"] == "rb-novo", "sync nao persistiu"
            assert s["slots"]["1"]["tokens"]["refresh_token"] == "ra", "outro slot foi tocado"

            s["slots"]["2"]["dead"] = True
            ccx.write_json(
                ccx_codex.auth_path(),
                {"tokens": {"access_token": "b10", "refresh_token": "rb-novo", "id_token": id_token}},
            )
            ccx_codex.sync_active_slot(s, "2")
            assert not s["slots"]["2"].get("dead"), "sync deveria reviver o slot"
        finally:
            os.environ.pop("CODEX_HOME", None)
            ccx_codex.STORE = store_bkp


def test_live_identity_sem_login():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CODEX_HOME"] = tmp
        try:
            try:
                ccx_codex.live_identity()
                raise AssertionError("deveria ter levantado SystemExit")
            except SystemExit:
                pass
        finally:
            os.environ.pop("CODEX_HOME", None)


def test_column_labels_usa_duracao_real_da_janela():
    # caso real de 2026-07: OpenAI tirou a janela curta do endpoint, so
    # primary_window vem preenchida e com 604800s (semanal), nao 5h.
    usage_map = {
        "1": {"5h": {"pct": 10.0, "resets_at": None, "window_seconds": 604800}},
    }
    lbl5, lbl7 = ccx_codex._column_labels(usage_map)
    assert lbl5 == "7d", f"deveria rotular pela duracao real, veio {lbl5!r}"
    assert lbl7 == "?", "coluna sem nenhum dado nao pode inventar rotulo"

    # com as duas janelas normais (5h e 7d de verdade)
    usage_map2 = {
        "1": {
            "5h": {"pct": 5.0, "window_seconds": 18000},
            "7d": {"pct": 20.0, "window_seconds": 604800},
        },
    }
    assert ccx_codex._column_labels(usage_map2) == ("5h", "7d")


def test_check_once_nao_confunde_429_de_usage_com_conta_esgotada():
    import unittest.mock as mock

    store = {
        "slots": {
            "1": {"email": "a@x.com"},
            "2": {"email": "b@x.com"},
        },
        "last_switch": 0,
    }
    conhecida = {
        "5h": {"pct": 50.0, "resets_at": None},
        "7d": {"pct": 40.0, "resets_at": None},
    }
    cotas = {"1": None, "2": conhecida}
    args = SimpleNamespace(
        threshold=85, strategy="consume-first", poll=60, cooldown=300
    )

    for error in ("HTTP 429", "TimeoutError"):
        with (
            mock.patch.object(ccx_codex, "load_store", return_value=store),
            mock.patch.object(
                ccx_codex,
                "collect",
                return_value=(cotas, {"1": error, "2": ""}, "1"),
            ),
            mock.patch.object(ccx_codex, "do_switch") as switch,
        ):
            assert ccx_codex.check_once(args) == (2, 60.0)
        switch.assert_not_called()


def test_check_once_codex_troca_com_429_apos_confirmacao_recente():
    import unittest.mock as mock

    store = {
        "slots": {"1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}},
        "last_switch": 0,
    }
    cotas = {
        "1": {"5h": {"pct": 100.0, "resets_at": None}, "7d": {"pct": 20.0, "resets_at": None}},
        "2": {"5h": {"pct": 20.0, "resets_at": None}, "7d": {"pct": 10.0, "resets_at": None}},
    }
    args = SimpleNamespace(
        threshold=85, strategy="consume-first", poll=60, cooldown=300
    )

    with (
        mock.patch.object(ccx_codex, "load_store", return_value=store),
        mock.patch.object(
            ccx_codex,
            "collect",
            return_value=(cotas, {"1": "HTTP 429", "2": ""}, "1"),
        ),
        mock.patch.object(ccx_codex, "do_switch") as switch,
    ):
        assert ccx_codex.check_once(args) == (0, 60.0)
    switch.assert_called_once()
    assert switch.call_args.args == (store, "2")
    # O motivo carrega o snapshot da decisao; o formato exato e do check_once.
    assert switch.call_args.kwargs["reason"].startswith("limiar; ")


def test_check_once_codex_respeita_slot_fixado_sem_consultar_cota():
    store = {
        "slots": {"1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}},
        "pinned_slot": "2",
    }
    args = SimpleNamespace(threshold=80, strategy="consume-first", poll=0, cooldown=300)
    with (
        mock.patch.object(ccx_codex, "load_store", return_value=store),
        mock.patch.object(ccx_codex, "active_slot", return_value="1"),
        mock.patch.object(ccx_codex, "collect") as collect,
        mock.patch.object(ccx_codex, "do_switch", return_value=True) as switch,
    ):
        assert ccx_codex.check_once(args) == (0, ccx.PINNED_CHECK_S)
    collect.assert_not_called()
    switch.assert_called_once_with(store, "2", only_if_pinned=True, reason="fixacao")


def test_collect_codex_reusa_o_cache_compartilhado():
    import unittest.mock as mock
    from contextlib import nullcontext

    conhecida = {
        "5h": {"pct": 20.0, "resets_at": None},
        "7d": {"pct": 30.0, "resets_at": None},
    }
    fresh = {
        "slots": {"1": {"email": "a@x.com"}},
        "last_switch": 0,
        "usage_cache": {
            "1": {"at": 100.0, "usage": conhecida, "error": ""},
        },
    }

    with (
        mock.patch.object(ccx_codex, "store_lock", return_value=nullcontext()),
        mock.patch.object(ccx_codex, "load_store", return_value=fresh),
        mock.patch.object(ccx_codex, "active_slot", return_value="1"),
        mock.patch.object(ccx_codex, "sync_active_slot"),
        mock.patch.object(ccx.time, "time", return_value=120.0),
        mock.patch.object(ccx_codex, "slot_usage") as fetch,
    ):
        assert ccx_codex.collect({}) == ({"1": conhecida}, {"1": ""}, "1")

    fetch.assert_not_called()


def test_sync_codex_nao_descarta_usage_ao_rotacionar_token():
    import unittest.mock as mock

    conhecida = {"5h": {"pct": 20.0, "resets_at": None}}
    store = {
        "slots": {"1": {"email": "a@x.com", "tokens": {"access_token": "velho"}}},
        "usage_cache": {"1": {"at": 1.0, "usage": conhecida, "error": ""}},
    }
    with (
        mock.patch.object(
            ccx_codex,
            "live_identity",
            return_value=({"access_token": "novo"}, {"email": "a@x.com"}),
        ),
        mock.patch.object(ccx, "write_json"),
    ):
        ccx_codex.sync_active_slot(store, "1")

    assert store["slots"]["1"]["tokens"]["access_token"] == "novo"
    assert store["usage_cache"]["1"]["usage"] == conhecida


def test_do_switch_codex_rele_store_e_preserva_estado_concorrente():
    import unittest.mock as mock
    from contextlib import nullcontext

    stale = {"slots": {"1": {"email": "velho"}}, "last_switch": 0}
    fresh = {
        "slots": {"1": {"email": "novo", "tokens": {"refresh_token": "novo"}}},
        "last_switch": 0,
        "usage_cache": {"2": {"at": 1.0, "usage": {"5h": {"pct": 3}}, "error": ""}},
    }
    with (
        mock.patch.object(ccx_codex, "store_lock", return_value=nullcontext()),
        mock.patch.object(ccx_codex, "load_store", return_value=fresh),
        mock.patch.object(ccx_codex, "apply_slot") as apply,
        mock.patch.object(ccx, "write_json") as write,
        # auto_event escreve com open() direto, entao NAO passa pelo write_json
        # mockado acima: sem este patch o teste sujaria o ~/.ccx/auto.log real
        # com uma troca que nunca aconteceu.
        mock.patch.object(ccx, "auto_event") as log,
        mock.patch.object(ccx.time, "time", return_value=500.0),
    ):
        ccx_codex.do_switch(stale, "1", reason="limiar; 1*:9.0%/9.0%")

    log.assert_called_once()
    registrado = log.call_args.args[0]
    assert registrado.startswith("codex: troca para o slot 1")
    assert "limiar; 1*:9.0%/9.0%" in registrado
    assert "novo" not in registrado, "token/identidade vazou no log"
    apply.assert_called_once_with(fresh["slots"]["1"])
    write.assert_called_once_with(ccx_codex.STORE, fresh)
    assert stale == fresh
    assert stale["usage_cache"]["2"]["usage"]["5h"]["pct"] == 3


def test_window_label_arredonda_horas_e_dias():
    assert ccx_codex._window_label(18000, "?") == "5h"  # 5h em segundos
    assert ccx_codex._window_label(604800, "?") == "7d"  # 7d em segundos
    assert ccx_codex._window_label(None, "?") == "?"
    assert ccx_codex._window_label(0, "?") == "?"


def test_auto_codex_sobrevive_erro_inesperado_sem_vazar_detalhe():
    args = SimpleNamespace(
        pin=None, once=False, strategy="consume-first", threshold=80, poll=0, cooldown=300
    )
    store = {"slots": {"1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}}}
    guard = mock.MagicMock()
    with (
        mock.patch.object(ccx_codex, "load_store", return_value=store),
        mock.patch.object(ccx_codex, "dir_lock", return_value=guard),
        mock.patch.object(
            ccx_codex, "check_once", side_effect=RuntimeError("token-super-secreto")
        ),
        mock.patch.object(ccx_codex.ccx.random, "uniform", return_value=123.0),
        mock.patch.object(ccx_codex.time, "sleep", side_effect=KeyboardInterrupt),
        mock.patch.object(ccx_codex.ccx, "auto_event") as event,
        mock.patch("builtins.print") as output,
    ):
        assert ccx_codex.cmd_auto(args) == 0

    event.assert_called_once()
    logged = event.call_args.args[0]
    printed = " ".join(str(arg) for call in output.call_args_list for arg in call.args)
    assert "RuntimeError" in logged
    assert "token-super-secreto" not in logged
    assert "token-super-secreto" not in printed
    guard.__exit__.assert_called_once_with(None, None, None)


def test_flags_invalidas_sao_recusadas():
    for argv in (
        ["--threshold", "101", "status"],
        ["--threshold", "0", "status"],
        ["auto", "--poll", "-1"],
        ["auto", "--cooldown", "-5"],
    ):
        try:
            ccx_codex.main(argv)
            raise AssertionError(f"deveria recusar {argv}")
        except SystemExit as e:
            assert e.code != 0, argv


# --------------------------------------------------------------------------
# token revogado no servidor com exp ainda valido
#
# Cenario real de 2026-08-14: 'codex logout' faz POST /oauth/revoke, entao o
# grant morre no servidor enquanto o access token guardado no slot continua
# valido no papel por dias. Antes deste bloco o slot ficava em loop de HTTP 401
# para sempre, porque o refresh (unico caminho que classifica como morto) so
# rodava depois que o exp passava.


def usage_401(code: str = "token_revoked"):
    """HTTPError 401 do endpoint de usage, com o corpo real da OpenAI."""
    import urllib.error

    body = json.dumps(
        {
            "error": {
                "message": "Encountered invalidated oauth token for user, failing request",
                "code": code,
            },
            "status": 401,
        }
    ).encode()
    return urllib.error.HTTPError("url", 401, "Unauthorized", {}, None), body


def slot_vivo(**extra):
    """Slot com exp longe: sem isto o refresh dispararia pelo caminho antigo."""
    now = datetime.now(timezone.utc).timestamp()
    slot = {
        "email": "a@x.com",
        "tokens": {"access_token": make_jwt({"exp": now + 864000}), "refresh_token": "rt"},
        "account_id": "acc-1",
    }
    slot.update(extra)
    return slot


def test_refresh_tokens_classifica_codigo_aninhado():
    import urllib.error

    # Formato real da OpenAI: o codigo vem dentro de "error", nao na raiz.
    # Antes deste ramo isto voltava "transient" e o slot nunca morria.
    body = json.dumps(
        {
            "error": {
                "message": "Your session has ended. Please log in again.",
                "type": "invalid_request_error",
                "code": "refresh_token_invalidated",
            }
        }
    ).encode()
    exc = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    exc.read = lambda: body
    with mock.patch.object(ccx.urllib.request, "urlopen", side_effect=exc):
        new, err = ccx_codex.refresh_tokens({"refresh_token": "rt"})
    assert new is None and err == "dead"

    # codigo aninhado que nao e permanente continua transitorio
    body2 = json.dumps({"error": {"code": "server_error"}}).encode()
    exc2 = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    exc2.read = lambda: body2
    with mock.patch.object(ccx.urllib.request, "urlopen", side_effect=exc2):
        new, err = ccx_codex.refresh_tokens({"refresh_token": "rt"})
    assert new is None and err == "transient"


def test_slot_morto_nao_e_eleito_pelo_cache():
    # remember_slot_usage preserva a ultima leitura boa por 300s quando o
    # resultado novo e erro. Sem descartar o cache ao marcar morto, pick_target
    # elegeria a conta revogada por ate cinco minutos.
    conhecida = {"5h": {"pct": 1.0, "resets_at": None}}
    store = {
        "slots": {"1": slot_vivo(), "2": slot_vivo(email="b@x.com")},
        "usage_cache": {"1": {"at": 100.0, "usage": conhecida, "error": "", "known_at": 100.0}},
    }
    exc, body = usage_401()
    exc.read = lambda: body

    with (
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx.urllib.request, "urlopen", side_effect=exc),
        mock.patch.object(ccx_codex, "refresh_tokens", return_value=(None, "dead")),
    ):
        usage, erro = ccx_codex.slot_usage("1", store["slots"]["1"], False, store)

    assert usage is None and erro == ccx_codex.DEAD_SLOT_MSG
    assert "1" not in store["usage_cache"]

    # e o que a coleta grava depois nao ressuscita a leitura boa
    ccx.remember_slot_usage(store, "1", usage, erro, 120.0)
    usage_map = {"1": store["usage_cache"]["1"]["usage"], "2": {"5h": {"pct": 90.0}}}
    assert usage_map["1"] is None
    assert ccx.pick_target(usage_map, 80.0, "consume-first") != "1"


def test_slot_usage_devolve_sempre_dois_elementos():
    # collect faz "usage_map[key], err_map[key] = cached" e repassa com *cached
    # para remember_slot_usage: um terceiro elemento quebraria os dois.
    store = {"slots": {"1": slot_vivo()}, "usage_cache": {}}
    slot = store["slots"]["1"]
    boa = {"5h": {"pct": 10.0, "resets_at": None}}
    exc, body = usage_401()
    exc.read = lambda: body

    with mock.patch.object(ccx_codex, "fetch_usage", return_value=boa):
        assert len(ccx_codex.slot_usage("1", slot, False, store)) == 2

    with (
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx.urllib.request, "urlopen", side_effect=exc),
        mock.patch.object(ccx_codex, "refresh_tokens", return_value=({"access_token": "novo"}, "")),
        mock.patch.object(ccx_codex, "fetch_usage", side_effect=[urllib_401(), boa]),
    ):
        assert len(ccx_codex.slot_usage("1", slot, False, store)) == 2

    assert len(ccx_codex.slot_usage("1", slot_vivo(dead=True), False, store)) == 2


def urllib_401(code: str = "token_revoked"):
    exc, body = usage_401(code)
    exc.read = lambda: body
    return exc


def test_slot_usage_marca_morto_quando_401_e_refresh_permanente():
    store = {"slots": {"1": slot_vivo()}, "usage_cache": {}}
    slot = store["slots"]["1"]
    with (
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx_codex, "fetch_usage", side_effect=urllib_401()),
        mock.patch.object(ccx_codex, "refresh_tokens", return_value=(None, "dead")) as refresh,
    ):
        usage, erro = ccx_codex.slot_usage("1", slot, False, store)
    refresh.assert_called_once()
    assert usage is None and erro == ccx_codex.DEAD_SLOT_MSG
    assert slot["dead"] is True


def test_slot_usage_recupera_quando_401_e_refresh_funciona():
    store = {"slots": {"1": slot_vivo()}, "usage_cache": {}}
    slot = store["slots"]["1"]
    boa = {"5h": {"pct": 12.0, "resets_at": None}}
    novos = {"access_token": "novo", "refresh_token": "rt2"}
    with (
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx_codex, "fetch_usage", side_effect=[urllib_401(), boa]),
        mock.patch.object(ccx_codex, "refresh_tokens", return_value=(novos, "")),
    ):
        usage, erro = ccx_codex.slot_usage("1", slot, False, store)
    assert usage == boa and erro == ""
    assert slot["tokens"] == novos
    assert "dead" not in slot


def test_slot_usage_ignora_401_sem_codigo_token_revoked():
    # O endpoint de usage devolve 401 transitorio: em 2026-08-14 as quatro
    # contas deram 401 juntas e voltaram 200 segundos depois. Refresh aqui
    # rotacionaria o token de conta saudavel a toa.
    import urllib.error

    outro = urllib_401("some_other_code")

    sem_codigo = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    sem_codigo.read = lambda: json.dumps({"error": {"message": "nope"}}).encode()

    nao_json = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    nao_json.read = lambda: b"<html>proxy</html>"

    for exc in (outro, sem_codigo, nao_json):
        store = {"slots": {"1": slot_vivo()}, "usage_cache": {}}
        slot = store["slots"]["1"]
        with (
            mock.patch.object(ccx, "write_json"),
            mock.patch.object(ccx_codex, "fetch_usage", side_effect=exc),
            mock.patch.object(ccx_codex, "refresh_tokens") as refresh,
        ):
            usage, erro = ccx_codex.slot_usage("1", slot, False, store)
        refresh.assert_not_called()
        assert usage is None and erro == "HTTP 401"
        assert "dead" not in slot


def test_slot_usage_nao_marca_morto_em_401_com_refresh_transitorio():
    store = {"slots": {"1": slot_vivo()}, "usage_cache": {}}
    slot = store["slots"]["1"]
    with (
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx_codex, "fetch_usage", side_effect=urllib_401()),
        mock.patch.object(ccx_codex, "refresh_tokens", return_value=(None, "transient")),
    ):
        usage, erro = ccx_codex.slot_usage("1", slot, False, store)
    assert usage is None and erro == "HTTP 401"
    assert "dead" not in slot


def test_collect_nao_repete_usage_dentro_do_ttl_de_erro():
    # Critério 3: a nova tentativa so vem depois que o erro sai do usage_cache,
    # nao na checagem seguinte. Vale para todo erro, nao so para o 401.
    from contextlib import nullcontext

    fresh = {"slots": {"1": slot_vivo()}, "last_switch": 0, "usage_cache": {}}
    chamadas = []

    def falso_slot_usage(key, slot, is_active, store):
        chamadas.append(key)
        return None, "HTTP 401"

    with (
        mock.patch.object(ccx_codex, "store_lock", return_value=nullcontext()),
        mock.patch.object(ccx_codex, "load_store", return_value=fresh),
        mock.patch.object(ccx_codex, "active_slot", return_value=None),
        mock.patch.object(ccx_codex, "sync_active_slot"),
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx_codex, "slot_usage", side_effect=falso_slot_usage),
        mock.patch.object(ccx.time, "time", side_effect=[1000.0, 1010.0]),
    ):
        primeira = ccx_codex.collect({})
        segunda = ccx_codex.collect({})

    assert primeira[1] == {"1": "HTTP 401"}
    assert segunda[1] == {"1": "HTTP 401"}
    assert len(chamadas) == 1, "o cache de erro de 120s deve evitar a segunda leitura"


def test_slot_usage_nao_renova_conta_ativa_em_401():
    # A conta ativa e do Codex CLI. Renovar por baixo dele invalida o token que
    # o processo em execucao esta usando.
    store = {"slots": {"1": slot_vivo()}, "usage_cache": {}}
    slot = store["slots"]["1"]
    with (
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx_codex, "fetch_usage", side_effect=urllib_401()),
        mock.patch.object(ccx_codex, "refresh_tokens") as refresh,
    ):
        usage, erro = ccx_codex.slot_usage("1", slot, True, store)
    refresh.assert_not_called()
    assert usage is None and erro == "HTTP 401"
    assert "dead" not in slot


def test_slot_usage_nao_renova_duas_vezes_na_mesma_chamada():
    # exp vencido ja gastou o refresh no topo da funcao; um 401 depois disso
    # nao pode gastar outro.
    now = datetime.now(timezone.utc).timestamp()
    vencido = {"access_token": make_jwt({"exp": now + 60}), "refresh_token": "rt"}
    store = {"slots": {"1": slot_vivo(tokens=vencido)}, "usage_cache": {}}
    slot = store["slots"]["1"]
    novos = {"access_token": make_jwt({"exp": now + 864000}), "refresh_token": "rt2"}
    with (
        mock.patch.object(ccx, "write_json"),
        mock.patch.object(ccx_codex, "fetch_usage", side_effect=urllib_401()),
        mock.patch.object(ccx_codex, "refresh_tokens", return_value=(novos, "")) as refresh,
    ):
        usage, erro = ccx_codex.slot_usage("1", slot, False, store)
    assert refresh.call_count == 1
    assert usage is None and erro == "HTTP 401"
    assert "dead" not in slot


def test_slot_usage_nao_renova_em_erro_que_nao_e_401():
    import urllib.error

    # O 500 com "token_revoked" no corpo e o caso que a review cruzada pegou:
    # falha de servidor nao pode virar rotacao de credencial so porque o corpo
    # trouxe a string. O status tem de entrar na decisao junto com o codigo.
    quinhentos_com_codigo = urllib.error.HTTPError("url", 500, "boom", {}, None)
    quinhentos_com_codigo.read = lambda: json.dumps({"error": {"code": "token_revoked"}}).encode()

    erros = [
        urllib.error.HTTPError("url", 429, "slow down", {}, None),
        urllib.error.HTTPError("url", 500, "boom", {}, None),
        quinhentos_com_codigo,
        TimeoutError("timeout"),
    ]
    esperados = ["HTTP 429", "HTTP 500", "HTTP 500", "TimeoutError"]
    for exc, esperado in zip(erros, esperados):
        store = {"slots": {"1": slot_vivo()}, "usage_cache": {}}
        slot = store["slots"]["1"]
        with (
            mock.patch.object(ccx, "write_json"),
            mock.patch.object(ccx_codex, "fetch_usage", side_effect=exc),
            mock.patch.object(ccx_codex, "refresh_tokens") as refresh,
        ):
            usage, erro = ccx_codex.slot_usage("1", slot, False, store)
        refresh.assert_not_called()
        assert usage is None and erro == esperado
        assert "dead" not in slot


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("tudo passou")
