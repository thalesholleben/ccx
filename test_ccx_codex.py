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
    switch.assert_called_once_with(store, "2")


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
    switch.assert_called_once_with(store, "2", only_if_pinned=True)


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
        mock.patch.object(ccx.time, "time", return_value=500.0),
    ):
        ccx_codex.do_switch(stale, "1")

    apply.assert_called_once_with(fresh["slots"]["1"])
    write.assert_called_once_with(ccx_codex.STORE, fresh)
    assert stale == fresh
    assert stale["usage_cache"]["2"]["usage"]["5h"]["pct"] == 3


def test_window_label_arredonda_horas_e_dias():
    assert ccx_codex._window_label(18000, "?") == "5h"  # 5h em segundos
    assert ccx_codex._window_label(604800, "?") == "7d"  # 7d em segundos
    assert ccx_codex._window_label(None, "?") == "?"
    assert ccx_codex._window_label(0, "?") == "?"


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("tudo passou")
