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
        redirect_stdout(saida),
    ):
        assert ccx.cmd_status(SimpleNamespace(threshold=85, strategy="consume-first")) == 0
    texto = saida.getvalue()
    assert "recomenda o slot 1" in texto
    assert "troca pendente" in texto
    assert "proxima checagem" not in texto


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
        assert not lock.exists(), "lock ficou pendurado"


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
