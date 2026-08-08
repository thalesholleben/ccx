#!/usr/bin/env python3
"""Checagem minima do ccx: python test_ccx.py"""

import json
import os
import tempfile
import unittest.mock as mock
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import ccx
import ccx_codex
import ccx_watchdog


def usage(pct5, pct7, weekly_in_h, h5_in_h=1):
    def at(hours):
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    return {
        "5h": {"pct": pct5, "resets_at": at(h5_in_h)},
        "7d": {"pct": pct7, "resets_at": at(weekly_in_h)},
    }


def test_pick_target():
    # consume-first fica na conta de reset semanal mais proximo, mesmo com menos folga
    m = {"1": usage(40, 60, 18), "2": usage(10, 20, 120)}
    assert ccx.pick_target(m, 85, "consume-first") == "1"
    # best vai na de maior folga
    assert ccx.pick_target(m, 85, "best") == "2"
    # quem passou do limiar sai da disputa, mesmo resetando antes
    m = {"1": usage(90, 60, 1), "2": usage(10, 20, 120)}
    assert ccx.pick_target(m, 85, "consume-first") == "2"
    # a janela que aperta e a mais alta das duas: 7d em 95 bloqueia mesmo com 5h zerada
    m = {"1": usage(0, 95, 1), "2": usage(50, 50, 120)}
    assert ccx.pick_target(m, 85, "consume-first") == "2"
    # acima do limiar mas nenhuma travada: cai para a menos pior
    m = {"1": usage(99, 99, 1), "2": usage(90, 90, 2)}
    assert ccx.pick_target(m, 85, "consume-first") == "2"
    # o caso real: 5h travada em 100 vs semanal em 97 que ainda atende
    m = {"1": usage(100, 78, 65), "2": usage(30, 97, 48)}
    assert ccx.pick_target(m, 85, "consume-first") == "2"
    # travada de vez de todos os lados: ninguem
    m = {"1": usage(100, 50, 1), "2": usage(50, 100, 2)}
    assert ccx.pick_target(m, 85, "consume-first") is None
    # com alguem abaixo do limiar, o fallback nao entra em cena
    m = {"1": usage(99, 99, 1), "2": usage(10, 10, 200)}
    assert ccx.pick_target(m, 85, "consume-first") == "2"
    # cota desconhecida nunca e escolhida
    m = {"1": None, "2": usage(50, 50, 120)}
    assert ccx.pick_target(m, 85, "consume-first") == "2"
    assert ccx.pick_target({"1": None}, 85, "consume-first") is None
    # sem resets_at vai para o fim da fila, nao para o inicio
    m = {"1": {"5h": {"pct": 10}, "7d": {"pct": 10}}, "2": usage(50, 50, 200)}
    assert ccx.pick_target(m, 85, "consume-first") == "2"


def test_swap_preserva_mcp():
    """A troca nao pode derrubar os tokens de MCP que moram no mesmo arquivo."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_CONFIG_DIR"] = tmp
        try:
            ccx.write_json(
                ccx.creds_path(),
                {
                    "claudeAiOauth": {"accessToken": "velho", "refreshToken": "rv"},
                    "mcpOAuth": {"heygen|abc": {"token": "nao-me-perca"}},
                    "organizationUuid": "org-velha",
                },
            )
            ccx.write_json(
                ccx.global_config_path(),
                {"oauthAccount": {"emailAddress": "a@x.com"}, "outraCoisa": 1},
            )

            ccx.apply_slot(
                {
                    "oauth": {"accessToken": "novo", "refreshToken": "rn"},
                    "account": {"emailAddress": "b@x.com"},
                    "org_uuid": "org-nova",
                }
            )

            creds = ccx.read_json(ccx.creds_path())
            assert creds["claudeAiOauth"]["accessToken"] == "novo"
            assert creds["organizationUuid"] == "org-nova"
            assert creds["mcpOAuth"] == {"heygen|abc": {"token": "nao-me-perca"}}, (
                "mcpOAuth foi perdido na troca"
            )
            cfg = ccx.read_json(ccx.global_config_path())
            assert cfg["oauthAccount"]["emailAddress"] == "b@x.com"
            assert cfg["outraCoisa"] == 1, "resto do .claude.json foi perdido"
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)


def test_available_at():
    # nenhuma janela em 100: pode atender agora
    assert ccx.available_at(usage(99, 99, 5)) is None
    # 5h travada: volta no reset do 5h
    t = ccx.available_at(usage(100, 50, 60, h5_in_h=0.5))
    assert t is not None and 0 < (t - datetime.now(timezone.utc)).total_seconds() < 2000
    # as duas travadas: manda o reset mais TARDE, precisa das duas liberadas
    t = ccx.available_at(usage(100, 100, 60, h5_in_h=0.5))
    assert (t - datetime.now(timezone.utc)).total_seconds() > 3600 * 50


def test_next_wake_com_duas_utilizaveis():
    limpo = {"1": "", "2": ""}
    folgada = {"1": usage(30, 40, 100), "2": usage(20, 20, 100)}
    apertada = {"1": usage(75, 40, 100), "2": usage(20, 20, 100)}
    # faixa larga enquanto a ativa esta folgada
    for _ in range(30):
        assert 180 <= ccx.next_wake(folgada, limpo, "1") <= 240
    # aperta quando a ATIVA passa de 70, mesmo com a outra folgada
    for _ in range(30):
        assert 100 <= ccx.next_wake(apertada, limpo, "1") <= 120
    # a referencia e a ativa: ativa folgada com a outra em 75 segue largo
    for _ in range(30):
        assert 180 <= ccx.next_wake(apertada, limpo, "2") <= 240
    # semanal alto NAO aperta o poll: ele nao se move dentro da sessao
    alto7d = {"1": usage(37, 97, 48), "2": usage(20, 20, 100)}
    for _ in range(30):
        assert 180 <= ccx.next_wake(alto7d, limpo, "1") <= 240
    # erro em qualquer conta derruba para largo, para nao insistir num 429
    for _ in range(30):
        assert 180 <= ccx.next_wake(apertada, {"1": "HTTP 429", "2": ""}, "1") <= 240
    # jitter de verdade, nao valor fixo
    amostras = {round(ccx.next_wake(folgada, limpo, "1"), 3) for _ in range(30)}
    assert len(amostras) > 5, "deveria variar"


def test_next_wake_dorme_quando_nao_ha_decisao():
    limpo = {"1": "", "2": ""}
    # o caso real: slot 1 travado no 5h voltando em 44min, slot 2 utilizavel.
    # Sem 2 utilizaveis nao ha troca a decidir: dorme ate o reset, nao faz poll.
    m = {"1": usage(100, 78, 65, h5_in_h=44 / 60), "2": usage(37, 97, 48)}
    d = ccx.next_wake(m, limpo, "2")
    assert 2600 < d < 2700, f"deveria dormir ~44min, deu {d}"
    # reset distante entra no teto de 1h em vez de dormir 65h
    m = {"1": usage(100, 100, 65, h5_in_h=65), "2": usage(37, 50, 48)}
    assert ccx.next_wake(m, limpo, "2") == ccx.SLEEP_CAP_S
    # as duas travadas: acorda quando a PRIMEIRA volta
    m = {"1": usage(100, 50, 65, h5_in_h=0.2), "2": usage(100, 50, 48, h5_in_h=4)}
    d = ccx.next_wake(m, limpo, "1")
    assert 700 < d < 800, f"deveria dormir ~12min, deu {d}"
    # resets_at no passado (dado velho) nao vira busy loop
    m = {"1": usage(100, 50, 65, h5_in_h=-3), "2": usage(37, 50, 48)}
    assert ccx.next_wake(m, limpo, "2") == ccx.SLEEP_FLOOR_S


def test_hook_checa_sem_poluir_o_stop():
    saida = StringIO()
    with mock.patch.object(ccx, "check_once", return_value=(2, 60)) as check:
        with redirect_stdout(saida):
            assert ccx.main(["hook"]) == 0
    check.assert_called_once()
    assert saida.getvalue() == "", "hook nao deve aparecer na conversa"


def test_status_nao_anuncia_sono_longo_com_troca_pendente():
    store = {
        "slots": {
            "1": {"email": "a@x.com"},
            "3": {"email": "c@x.com"},
        }
    }
    cotas = {
        "1": usage(0, 97, 40),
        "3": usage(100, 10, 35),
    }
    saida = StringIO()
    with (
        mock.patch.object(ccx, "load_store", return_value=store),
        mock.patch.object(
            ccx, "collect", return_value=(cotas, {"1": "", "3": ""}, "3")
        ),
        mock.patch.object(ccx, "auto_monitor_alive", return_value=True),
        redirect_stdout(saida),
    ):
        assert ccx.cmd_status(SimpleNamespace(threshold=85, strategy="consume-first")) == 0
    texto = saida.getvalue()
    assert "recomenda o slot 1" in texto
    assert "troca pendente" in texto
    assert "proxima checagem" not in texto


def test_status_avisa_monitor_offline_em_vez_de_prometer_troca():
    store = {"slots": {"1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}}}
    cotas = {"1": usage(5, 5, 100), "2": usage(99, 50, 100)}
    saida = StringIO()
    with (
        mock.patch.object(ccx, "load_store", return_value=store),
        mock.patch.object(
            ccx, "collect", return_value=(cotas, {"1": "", "2": ""}, "2")
        ),
        mock.patch.object(ccx, "auto_monitor_alive", return_value=False),
        redirect_stdout(saida),
    ):
        assert ccx.cmd_status(SimpleNamespace(threshold=85, strategy="consume-first")) == 0
    texto = saida.getvalue()
    assert "recomenda o slot 1" in texto
    assert "monitor offline" in texto
    assert "\\CCX\\Claude Monitor" in texto
    assert "aplica automaticamente" not in texto


def test_status_nao_confunde_erro_de_usage_com_conta_esgotada():
    store = {
        "slots": {
            "2": {"email": "b@x.com"},
            "3": {"email": "c@x.com"},
        }
    }
    cotas = {"2": usage(88, 38, 100), "3": None}
    args = SimpleNamespace(threshold=85, strategy="consume-first")

    for error in ("HTTP 429", "TimeoutError"):
        saida = StringIO()
        with (
            mock.patch.object(ccx, "load_store", return_value=store),
            mock.patch.object(
                ccx, "collect", return_value=(cotas, {"2": "", "3": error}, "3")
            ),
            redirect_stdout(saida),
        ):
            assert ccx.cmd_status(args) == 0
        texto = saida.getvalue()
        assert "recomenda o slot 2" in texto
        assert "troca pendente" not in texto
        assert "monitor mantem o slot 3" in texto


def test_check_once_mantem_ativa_em_qualquer_erro_de_usage():
    store = {
        "slots": {
            "2": {"email": "b@x.com"},
            "3": {"email": "c@x.com"},
        },
        "last_switch": 0,
    }
    cotas = {"2": usage(88, 38, 100), "3": None}
    args = SimpleNamespace(
        threshold=85, strategy="consume-first", poll=60, cooldown=300
    )

    for error in ("HTTP 429", "TimeoutError"):
        with (
            mock.patch.object(ccx, "load_store", return_value=store),
            mock.patch.object(
                ccx, "collect", return_value=(cotas, {"2": "", "3": error}, "3")
            ),
            mock.patch.object(ccx, "do_switch") as switch,
        ):
            assert ccx.check_once(args) == (2, 60.0)
        switch.assert_not_called()


def test_check_once_troca_com_429_se_a_ativa_ja_foi_confirmada_esgotada():
    store = {
        "slots": {
            "2": {"email": "b@x.com"},
            "3": {"email": "c@x.com"},
        },
        "last_switch": 0,
    }
    cotas = {"2": usage(20, 10, 100), "3": usage(100, 20, 100)}
    args = SimpleNamespace(
        threshold=85, strategy="consume-first", poll=60, cooldown=300
    )

    with (
        mock.patch.object(ccx, "load_store", return_value=store),
        mock.patch.object(
            ccx, "collect", return_value=(cotas, {"2": "", "3": "HTTP 429"}, "3")
        ),
        mock.patch.object(ccx, "do_switch") as switch,
    ):
        assert ccx.check_once(args) == (0, 60.0)
    switch.assert_called_once_with(store, "2")


def test_check_once_respeita_slot_fixado_sem_consultar_cota():
    store = {
        "slots": {"1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}},
        "pinned_slot": "2",
    }
    args = SimpleNamespace(threshold=80, strategy="consume-first", poll=0, cooldown=300)
    with (
        mock.patch.object(ccx, "load_store", return_value=store),
        mock.patch.object(ccx, "active_slot", return_value="1"),
        mock.patch.object(ccx, "collect") as collect,
        mock.patch.object(ccx, "do_switch", return_value=True) as switch,
    ):
        assert ccx.check_once(args) == (0, ccx.PINNED_CHECK_S)
    collect.assert_not_called()
    switch.assert_called_once_with(store, "2", only_if_pinned=True)


def test_configure_pin_persiste_e_pode_ser_removido():
    with tempfile.TemporaryDirectory() as tmp:
        original = ccx.STORE
        ccx.STORE = Path(tmp) / "accounts.json"
        ccx.write_json(ccx.STORE, {"slots": {"1": {}, "2": {}}})
        try:
            with mock.patch.object(ccx, "active_slot", return_value="1"):
                assert ccx.configure_pin("2") == ("2", False)
            assert ccx.load_store()["pinned_slot"] == "2"
            assert ccx.configure_pin("off") == (None, False)
            assert "pinned_slot" not in ccx.load_store()
        finally:
            ccx.STORE = original


def test_usage_cache_aplica_ttl_maior_depois_de_erro():
    store = {"usage_cache": {}}
    conhecida = usage(20, 30, 100)

    ccx.remember_slot_usage(store, "1", conhecida, "", 100.0)
    assert ccx.cached_slot_usage(store, "1", 129.9) == (conhecida, "")
    assert ccx.cached_slot_usage(store, "1", 130.0) is None

    ccx.remember_slot_usage(store, "1", None, "HTTP 429", 100.0)
    assert ccx.cached_slot_usage(store, "1", 219.9) == (conhecida, "HTTP 429")
    assert ccx.cached_slot_usage(store, "1", 220.0) is None
    assert store["usage_cache"]["1"]["known_at"] == 100.0


def test_collect_rele_cache_sob_lock_e_nao_repete_usage():
    conhecida = usage(20, 30, 100)
    fresh = {
        "slots": {"1": {"email": "a@x.com"}},
        "last_switch": 0,
        "usage_cache": {
            "1": {"at": 100.0, "usage": conhecida, "error": ""},
        },
    }
    stale = {"slots": {"1": {"email": "a@x.com"}}, "last_switch": 0}

    with (
        mock.patch.object(ccx, "store_lock", return_value=nullcontext()),
        mock.patch.object(ccx, "load_store", return_value=fresh),
        mock.patch.object(ccx, "active_slot", return_value="1"),
        mock.patch.object(ccx, "sync_active_slot"),
        mock.patch.object(ccx.time, "time", return_value=120.0),
        mock.patch.object(ccx, "slot_usage") as fetch,
    ):
        usage_map, err_map, active = ccx.collect(stale)

    assert (usage_map, err_map, active) == ({"1": conhecida}, {"1": ""}, "1")
    fetch.assert_not_called()
    assert stale["usage_cache"] == fresh["usage_cache"]


def test_collect_atualiza_cache_vencido():
    antiga = usage(10, 20, 100)
    nova = usage(30, 40, 100)
    fresh = {
        "slots": {"1": {"email": "a@x.com"}},
        "last_switch": 0,
        "usage_cache": {"1": {"at": 0.0, "usage": antiga, "error": ""}},
    }
    store = {}

    with (
        mock.patch.object(ccx, "store_lock", return_value=nullcontext()),
        mock.patch.object(ccx, "load_store", return_value=fresh),
        mock.patch.object(ccx, "active_slot", return_value="1"),
        mock.patch.object(ccx, "sync_active_slot"),
        mock.patch.object(ccx.time, "time", return_value=500.0),
        mock.patch.object(ccx, "slot_usage", return_value=(nova, "")) as fetch,
        mock.patch.object(ccx, "write_json") as write,
    ):
        assert ccx.collect(store) == ({"1": nova}, {"1": ""}, "1")

    fetch.assert_called_once()
    write.assert_called_once_with(ccx.STORE, store)
    assert store["usage_cache"]["1"] == {
        "at": 500.0,
        "usage": nova,
        "error": "",
        "known_at": 500.0,
    }


def test_sync_rotacao_de_token_nao_descarta_usage_recente():
    conhecida = usage(20, 30, 100)
    store = {
        "slots": {"1": {"email": "a@x.com", "oauth": {"accessToken": "velho"}}},
        "usage_cache": {"1": {"at": 1.0, "usage": conhecida, "error": ""}},
    }
    with (
        mock.patch.object(
            ccx, "live_identity", return_value=({"accessToken": "novo"}, {}, "")
        ),
        mock.patch.object(ccx, "write_json"),
    ):
        ccx.sync_active_slot(store, "1")

    assert store["slots"]["1"]["oauth"]["accessToken"] == "novo"
    assert store["usage_cache"]["1"]["usage"] == conhecida


def test_do_switch_rele_store_e_preserva_refresh_e_cache_concorrentes():
    stale = {"slots": {"1": {"email": "velho"}}, "last_switch": 0}
    fresh = {
        "slots": {"1": {"email": "novo", "oauth": {"refreshToken": "novo"}}},
        "last_switch": 0,
        "usage_cache": {"2": {"at": 1.0, "usage": usage(2, 3, 100), "error": ""}},
    }
    with (
        mock.patch.object(ccx, "store_lock", return_value=nullcontext()),
        mock.patch.object(ccx, "load_store", return_value=fresh),
        mock.patch.object(ccx, "apply_slot") as apply,
        mock.patch.object(ccx, "write_json") as write,
        mock.patch.object(ccx, "auto_event"),
        mock.patch.object(ccx.time, "time", return_value=500.0),
    ):
        ccx.do_switch(stale, "1")

    apply.assert_called_once_with(fresh["slots"]["1"])
    write.assert_called_once_with(ccx.STORE, fresh)
    assert stale == fresh
    assert stale["usage_cache"]["2"]["usage"]["5h"]["pct"] == 2


def test_stats_consolida_claude_e_codex():
    saida = StringIO()
    args = SimpleNamespace(threshold=85, strategy="consume-first")
    with (
        mock.patch.object(ccx, "cmd_status", return_value=0) as claude_status,
        mock.patch.object(ccx_codex, "cmd_status", return_value=0) as codex_status,
        redirect_stdout(saida),
    ):
        assert ccx.cmd_stats(args) == 0
    claude_status.assert_called_once_with(args)
    codex_status.assert_called_once_with(args)
    assert "=== Claude Code ===" in saida.getvalue()
    assert "=== Codex CLI ===" in saida.getvalue()


def test_org_cai_para_oauthaccount():
    """Conta sem organizationUuid no .credentials.json pega o do oauthAccount."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_CONFIG_DIR"] = tmp
        try:
            ccx.write_json(ccx.creds_path(), {"claudeAiOauth": {"accessToken": "t"}})
            ccx.write_json(
                ccx.global_config_path(),
                {"oauthAccount": {"emailAddress": "b@x.com", "organizationUuid": "org-b"}},
            )
            _, account, org = ccx.live_identity()
            assert org == "org-b", f"org deveria vir do oauthAccount, veio {org!r}"
            assert account["emailAddress"] == "b@x.com"
            # topo do arquivo tem prioridade quando existe
            ccx.write_json(
                ccx.creds_path(),
                {"claudeAiOauth": {"accessToken": "t"}, "organizationUuid": "org-topo"},
            )
            assert ccx.live_identity()[2] == "org-topo"
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)


def _store(tmp_home):
    """Store com 2 slots, apontando ccx.STORE para o temp."""
    ccx.STORE = Path(tmp_home) / ".ccx" / "accounts.json"
    s = {
        "slots": {
            "1": {
                "email": "A@x.com",
                "oauth": {"accessToken": "a1", "refreshToken": "ra"},
                "account": {"emailAddress": "A@x.com", "organizationUuid": "org-a"},
                "org_uuid": "org-a",
            },
            "2": {
                "email": "b@x.com",
                "oauth": {"accessToken": "b1", "refreshToken": "rb"},
                "account": {"emailAddress": "b@x.com", "organizationUuid": "org-b"},
                "org_uuid": "org-b",
            },
        },
        "last_switch": 0,
    }
    ccx.write_json(ccx.STORE, s)
    return s


def test_identidade_sobrevive_rotacao_de_token():
    """O Claude Code rotaciona o token da conta ativa; a identidade nao pode
    depender dele, senao todo slot parece inativo e a ativa passa a ser
    renovada por baixo do Claude Code."""
    store_bkp = ccx.STORE
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_CONFIG_DIR"] = tmp
        try:
            s = _store(tmp)
            # token vivo NAO bate com nenhum guardado (ja rotacionou)
            ccx.write_json(
                ccx.creds_path(),
                {"claudeAiOauth": {"accessToken": "b9", "refreshToken": "rb-novo"}},
            )
            ccx.write_json(
                ccx.global_config_path(),
                {"oauthAccount": {"emailAddress": "B@X.com", "organizationUuid": "org-b"}},
            )
            # casa por email, sem depender de caixa
            assert ccx.active_slot(s) == "2"

            # e o sync traz a credencial rotacionada para o slot
            ccx.sync_active_slot(s, "2")
            assert s["slots"]["2"]["oauth"]["refreshToken"] == "rb-novo"
            assert json.loads(ccx.STORE.read_text(encoding="utf-8"))["slots"]["2"][
                "oauth"
            ]["refreshToken"] == "rb-novo", "sync nao persistiu"
            # o outro slot nao foi tocado
            assert s["slots"]["1"]["oauth"]["refreshToken"] == "ra"
            # sync marca o slot como vivo de novo
            s["slots"]["2"]["dead"] = True
            ccx.write_json(ccx.creds_path(), {"claudeAiOauth": {"accessToken": "b10"}})
            ccx.sync_active_slot(s, "2")
            assert not s["slots"]["2"].get("dead")
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            ccx.STORE = store_bkp


def test_json_corrompido_aborta_em_vez_de_virar_vazio():
    """Tratar corrompido como vazio é destrutivo: o passo seguinte reescreve o
    arquivo e leva junto o que não foi lido (ex.: mcpOAuth)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.json"
        p.write_text("{isso nao e json", encoding="utf-8")
        try:
            ccx.read_json(p)
            raise AssertionError("deveria ter levantado CorruptFile")
        except ccx.CorruptFile:
            pass
        # ausente continua sendo {} normalmente
        assert ccx.read_json(Path(tmp) / "nao-existe.json") == {}


def test_parse_reset_sem_timezone():
    """ISO sem offset não pode derrubar a subtração contra now(utc)."""
    t = ccx.parse_reset("2026-08-01T00:00:00")
    assert t.tzinfo is not None, "datetime ingênuo escaparia"
    (t - datetime.now(timezone.utc)).total_seconds()  # não pode levantar
    assert ccx.parse_reset(None) == ccx.FAR_FUTURE
    assert ccx.parse_reset("lixo") == ccx.FAR_FUTURE


def test_respostas_inesperadas_nao_derrubam():
    # 200 sem access_token vira transitório, não KeyError
    with mock.patch.object(ccx, "http_json", return_value={"nada": 1}):
        new, err = ccx.refresh_token({"refreshToken": "x"})
        assert new is None and err == "transient", (new, err)
    # usage 200 sem janelas levanta, para a conta virar "desconhecida" e não 0%
    with mock.patch.object(ccx, "http_json", return_value={"outra_coisa": {}}):
        try:
            ccx.fetch_usage("tok")
            raise AssertionError("deveria ter levantado")
        except ValueError:
            pass


def test_flags_invalidas_sao_recusadas():
    for argv in (["--threshold", "101", "status"], ["--threshold", "0", "status"],
                 ["auto", "--poll", "-1"], ["auto", "--cooldown", "-5"]):
        try:
            ccx.main(argv)
            raise AssertionError(f"deveria recusar {argv}")
        except SystemExit as e:
            assert e.code != 0, argv


def test_switch_nao_mistura_identidades():
    """Slot sem identidade não pode herdar a organização/perfil do anterior."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_CONFIG_DIR"] = tmp
        try:
            ccx.write_json(
                ccx.creds_path(),
                {"claudeAiOauth": {"accessToken": "a"}, "organizationUuid": "org-a"},
            )
            ccx.write_json(
                ccx.global_config_path(), {"oauthAccount": {"emailAddress": "a@x.com"}}
            )
            ccx.apply_slot({"oauth": {"accessToken": "b"}})  # sem org nem account
            creds = ccx.read_json(ccx.creds_path())
            cfg = ccx.read_json(ccx.global_config_path())
            assert "organizationUuid" not in creds, "org da conta anterior sobreviveu"
            assert "oauthAccount" not in cfg, "perfil da conta anterior sobreviveu"
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)


def test_lock_libera():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "alvo"
        lock = Path(tmp) / "alvo.lock"
        with ccx.claude_lock(target):
            assert lock.is_dir()
            assert not (lock / ccx.LOCK_OWNER_FILE).exists()
        assert not lock.exists(), "lock ficou pendurado"


def test_lock_timeout_zero_recupera_stale_e_recusa_lock_vivo():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "auto"
        lock = Path(tmp) / "auto.lock"

        lock.mkdir()
        os.utime(lock, (0, 0))
        with mock.patch.object(ccx.time, "monotonic", side_effect=[100.0, 100.001]):
            with ccx.claude_lock(target, timeout=0):
                assert lock.is_dir()
        assert not lock.exists(), "lock stale deveria ser tomado e liberado"

        lock.mkdir()
        try:
            with ccx.claude_lock(target, timeout=0):
                raise AssertionError("lock vivo nao pode ser tomado")
        except TimeoutError:
            pass
        finally:
            lock.rmdir()


def test_lock_timeout_zero_nao_toma_lock_stale_de_processo_vivo():
    with tempfile.TemporaryDirectory() as tmp:
        old_store = ccx.STORE
        ccx.STORE = Path(tmp) / "accounts.json"
        target = Path(tmp) / "auto"
        lock = Path(tmp) / "auto.lock"
        lock.mkdir()
        ccx.write_json(lock / ccx.LOCK_OWNER_FILE, {"pid": os.getpid(), "id": "vivo"})
        os.utime(lock, (0, 0))
        try:
            with ccx.claude_lock(target, timeout=0):
                raise AssertionError("lock de processo vivo nao pode ser tomado")
        except TimeoutError:
            pass
        finally:
            ccx.discard_lock(lock, "vivo")
            ccx.STORE = old_store


def test_auto_sobrevive_a_erro_inesperado():
    """Um erro fora dos ramos esperados nao pode encerrar a rotacao."""
    args = SimpleNamespace(
        once=False, strategy="consume-first", threshold=85, poll=0, cooldown=300
    )
    saida = StringIO()
    with (
        mock.patch.object(ccx, "load_store", return_value={"slots": {"1": {}, "2": {}}}),
        mock.patch.object(ccx, "claude_lock", return_value=nullcontext()),
        mock.patch.object(ccx, "check_once", side_effect=ValueError("inesperado")),
        mock.patch.object(ccx, "auto_event") as event,
        mock.patch.object(ccx.time, "sleep", side_effect=KeyboardInterrupt),
        redirect_stdout(saida),
    ):
        assert ccx.cmd_auto(args) == 0
    assert "erro no monitor: ValueError" in saida.getvalue()
    assert any("erro no monitor: ValueError" in call.args[0] for call in event.call_args_list)


def test_watchdog_relanca_monitor_morto_sem_consultar_usage():
    with (
        mock.patch.object(ccx_watchdog, "monitor_alive", return_value=False),
        mock.patch.object(ccx_watchdog, "start_monitor") as start,
    ):
        assert ccx_watchdog.main() == 0
    start.assert_called_once()

    with (
        mock.patch.object(ccx_watchdog, "monitor_alive", return_value=True),
        mock.patch.object(ccx_watchdog, "start_monitor") as start,
    ):
        assert ccx_watchdog.main() == 0
    start.assert_not_called()


def test_watchdog_nao_duplica_monitor_suspenso_com_pid_vivo():
    with tempfile.TemporaryDirectory() as tmp:
        old_store = ccx.STORE
        ccx.STORE = Path(tmp) / "accounts.json"
        lock = ccx.STORE.parent / "auto.lock"
        lock.mkdir()
        ccx.write_json(lock / ccx.LOCK_OWNER_FILE, {"pid": os.getpid(), "id": "vivo"})
        os.utime(lock, (0, 0))
        try:
            assert ccx_watchdog.monitor_alive()
        finally:
            ccx.discard_lock(lock, "vivo")
            ccx.STORE = old_store


def test_instalador_usa_pythonw_para_nao_piscar_console():
    installer = Path(__file__).with_name("install-ccx-monitor.ps1").read_text(
        encoding="utf-8"
    )
    assert "$pythonw =" in installer
    assert "-Execute $pythonw" in installer


def test_token_expirado():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert ccx.token_expired({"expiresAt": now_ms + 60_000})  # dentro da folga
    assert not ccx.token_expired({"expiresAt": now_ms + 3_600_000})
    assert not ccx.token_expired({})  # sem campo, nao presume expirado


def test_write_json_atomico():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sub" / "x.json"
        ccx.write_json(p, {"a": 1})
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
        assert not list(p.parent.glob("*.ccx-tmp")), "tmp nao foi limpo"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("tudo passou")
