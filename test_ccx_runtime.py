"""Regressões da fundação de continuidade. Sem rede ou credenciais reais."""
import argparse
import copy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import ccx
import ccx_runtime as runtime


def slot(key):
    return {"email": f"fixture{key}@example.invalid", "org_uuid": f"org-{key}",
            "account": {"emailAddress": f"fixture{key}@example.invalid", "organizationUuid": f"org-{key}"},
            "oauth": {"accessToken": f"fixture-access-{key}", "refreshToken": f"fixture-refresh-{key}",
                      "expiresAt": (time.time() + 3600) * 1000}}


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix="ccx-runtime-test-") as tmp:
        root = Path(tmp)
        with (patch.object(ccx, "STORE", root / "accounts.json"),
              patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(root / "claude")}),
              patch.object(ccx, "http_json", side_effect=AssertionError("network forbidden"))):
            store = {"slots": {k: slot(k) for k in ("1", "2", "3")},
                     "last_switch": 0, "usage_cache": {}}
            ccx.write_json(ccx.STORE, store)
            ccx.apply_slot(store["slots"]["1"])
            yield root, store


def result(error=False):
    return {"type": "result", "session_id": "session-fixture", "subtype": "success",
            "is_error": error, "terminal_reason": "api_error" if error else "completed",
            "api_error_status": 401 if error else None}


def failed_attempt(error="authentication_failed"):
    obs = runtime.AttemptObservation("session-fixture")
    obs.observe({"type": "assistant", "session_id": "session-fixture", "error": error})
    obs.observe(result(True))
    return obs


def test_pin_revalidated_after_another_process_writes():
    with fixture() as (root, store), redirect_stdout(StringIO()):
        decision = ccx.switch_guard(store, "1", "2")
        subprocess.run([sys.executable, "-c", "import ccx,sys; from pathlib import Path; "
                        "ccx.STORE=Path(sys.argv[1]); ccx.configure_pin('3')", str(ccx.STORE)],
                       cwd=Path(__file__).parent, env=os.environ.copy(), check=True,
                       capture_output=True, timeout=20)
        assert ccx.do_switch(store, "2", expected=decision) is False
        assert ccx.active_slot(ccx.load_store()) == "1"
        assert ccx.pinned_slot(ccx.load_store()) == "3"


def test_pin_also_protects_calls_without_guard():
    with fixture() as (_, store), redirect_stdout(StringIO()):
        ccx.configure_pin("3")
        assert not ccx.do_switch(store, "2")
        assert ccx.active_slot(ccx.load_store()) == "1"


def test_generation_prevents_pin_aba_100_interleavings():
    with fixture() as (_, store), redirect_stdout(StringIO()):
        for _ in range(100):
            stale = ccx.load_store()
            decision = ccx.switch_guard(stale, "1", "2")
            ccx.configure_pin("3")
            ccx.configure_pin("off")
            assert ccx.pinned_slot(ccx.load_store()) is None
            assert not ccx.do_switch(stale, "2", expected=decision)
        assert ccx.active_slot(ccx.load_store()) == "1"


def test_changed_quota_invalidates_decision():
    with fixture() as (_, store), redirect_stdout(StringIO()):
        decision = ccx.switch_guard(store, "1", "2")
        fresh = ccx.load_store()
        fresh["usage_cache"]["2"] = {"usage": {"5h": {"pct": 100}}}
        ccx.write_json(ccx.STORE, fresh)
        assert not ccx.do_switch(store, "2", expected=decision)
        assert ccx.active_slot(ccx.load_store()) == "1"


def test_switch_rejects_dead_unrefreshable_expired_and_missing_access():
    for kind in ("dead", "expired", "missing"):
        with fixture() as (_, store), redirect_stdout(StringIO()):
            if kind == "dead":
                store["slots"]["2"]["dead"] = True
            elif kind == "expired":
                store["slots"]["2"]["oauth"]["expiresAt"] = 1
                store["slots"]["2"]["oauth"].pop("refreshToken")
            else:
                store["slots"]["2"]["oauth"].pop("accessToken")
            ccx.write_json(ccx.STORE, store)
            before = ccx.creds_path().read_bytes()
            assert not ccx.do_switch(store, "2")
            assert ccx.creds_path().read_bytes() == before
            assert ccx.active_slot(ccx.load_store()) == "1"


def test_switch_preserves_active_refresh_before_leaving():
    with fixture() as (_, store), redirect_stdout(StringIO()):
        live = copy.deepcopy(store["slots"]["1"])
        live["oauth"].update(accessToken="rotated-access", refreshToken="rotated-refresh")
        ccx.apply_slot(live)
        assert ccx.do_switch(store, "2", expected=ccx.switch_guard(store, "1", "2"))
        saved = ccx.load_store()
        assert saved["slots"]["1"]["oauth"]["refreshToken"] == "rotated-refresh"
        assert ccx.active_slot(saved) == "2"
        assert ccx.control_generation(saved) == 1


def test_unrefreshable_candidate_does_not_hide_usable_account():
    with fixture() as (_, store), redirect_stdout(StringIO()):
        store["slots"]["2"]["oauth"]["expiresAt"] = 1
        store["slots"]["2"]["oauth"].pop("refreshToken")
        for key, pct in (("1", 100), ("2", 0), ("3", 15)):
            store["usage_cache"][key] = {
                "at": time.time(), "known_at": time.time(), "inactive": key != "1",
                "usage": {"5h": {"pct": pct, "resets_at": "2099-01-01T00:00:00Z"}},
                "error": "",
            }
        ccx.write_json(ccx.STORE, store)
        args = argparse.Namespace(threshold=80, strategy="best", poll=60, cooldown=60)
        assert ccx.check_once(args)[0] == 0
        assert ccx.active_slot(ccx.load_store()) == "3"


def cached_quotas(store):
    for key, pct in (("1", 100), ("2", 10), ("3", 20)):
        store["usage_cache"][key] = {
            "at": time.time(), "known_at": time.time(), "inactive": key != "1",
            "usage": {"5h": {"pct": pct, "resets_at": "2099-01-01T00:00:00Z"}}, "error": "",
        }


def test_all_inactive_expired_keep_legacy_refresh_path():
    with fixture() as (root, store), redirect_stdout(StringIO()) as output:
        cached_quotas(store)
        for key in ("2", "3"):
            store["slots"][key]["oauth"]["expiresAt"] = 1
        ccx.write_json(ccx.STORE, store)
        args = argparse.Namespace(threshold=80, strategy="best", poll=60, cooldown=0)
        with patch.object(ccx, "refresh_token", side_effect=AssertionError("CCX must not refresh this grant")):
            assert ccx.check_once(args)[0] == 0
        assert ccx.active_slot(ccx.load_store()) == "2"
        assert ccx.slot_auth_state(ccx.load_store()["slots"]["2"]) == "refresh_required"
        assert "acesso nao confirmado" in output.getvalue()
        assert "refresh_required" in (root / "auto.log").read_text()
        assert not runtime.status_snapshot()["execution"]["automatic_recovery_available"]


def test_refresh_candidate_does_not_disable_preemptive_threshold():
    with fixture() as (_, store), redirect_stdout(StringIO()):
        cached_quotas(store)
        store["usage_cache"]["1"]["usage"]["5h"]["pct"] = 85
        for key in ("2", "3"):
            store["slots"][key]["oauth"]["expiresAt"] = 1
        ccx.write_json(ccx.STORE, store)
        args = argparse.Namespace(threshold=80, strategy="best", poll=60, cooldown=0)
        assert ccx.check_once(args)[0] == 0
        assert ccx.active_slot(ccx.load_store()) == "2"


def test_active_refresh_margin_does_not_trigger_churn_in_either_strategy():
    for strategy in ("best", "consume-first"):
        with fixture() as (_, store), redirect_stdout(StringIO()):
            store["slots"]["1"]["oauth"]["expiresAt"] = (time.time() + 240) * 1000
            ccx.write_json(ccx.STORE, store)
            ccx.apply_slot(store["slots"]["1"])
            def fetch(access):
                return {"5h": {"pct": 20 if access == "fixture-access-1" else 50,
                               "resets_at": "2099-01-01T00:00:00Z"}}
            args = argparse.Namespace(threshold=80, strategy=strategy, poll=60, cooldown=0)
            with (patch.object(ccx, "fetch_usage", side_effect=fetch) as requested,
                  patch.object(ccx, "refresh_token", side_effect=AssertionError("no refresh"))):
                for _ in range(3):
                    assert ccx.check_once(args)[0] == 2
                    assert ccx.active_slot(ccx.load_store()) == "1"
            assert requested.call_count == 3  # coleta inicial, depois cache; zero sondagem repetida
            assert ccx.control_generation(ccx.load_store()) == 0


def test_auth_exclusion_is_degraded_before_active_reaches_100():
    with fixture() as (_, store), redirect_stdout(StringIO()) as output:
        cached_quotas(store)
        store["usage_cache"]["1"]["usage"]["5h"]["pct"] = 95
        for key in ("2", "3"):
            store["slots"][key]["oauth"]["expiresAt"] = 1
            store["slots"][key]["oauth"].pop("refreshToken")
        ccx.write_json(ccx.STORE, store)
        args = argparse.Namespace(threshold=80, strategy="best", poll=60, cooldown=0)
        assert ccx.check_once(args)[0] == 3
        assert "  ok " not in output.getvalue() and "waiting_auth" in output.getvalue()


def test_auth_exclusion_visible_in_monitor_hook_log_and_status():
    with fixture() as (root, store):
        cached_quotas(store)
        for key in ("2", "3"):
            store["slots"][key]["oauth"]["expiresAt"] = 1
            store["slots"][key]["oauth"].pop("refreshToken")
        ccx.write_json(ccx.STORE, store)
        args = argparse.Namespace(threshold=80, strategy="best", poll=60, cooldown=0)
        with redirect_stdout(StringIO()) as output:
            assert ccx.check_once(args)[0] == 3
        assert "waiting_auth" in output.getvalue() and "  ok " not in output.getvalue()
        with redirect_stdout(StringIO()) as output:
            assert ccx.cmd_status(args) == 0
        assert "slot 2 excluido (expired)" in output.getvalue()
        with redirect_stdout(StringIO()) as output:
            assert ccx.cmd_hook(args) == 0
        assert output.getvalue() == ""
        assert "waiting_auth: slot 2 expired; slot 3 expired" in (root / "auto.log").read_text()
        assert ccx.active_slot(ccx.load_store()) == "1"


def test_pinned_expired_active_reports_refresh_required_without_network():
    with fixture() as (root, store):
        ccx.configure_pin("1")
        live = copy.deepcopy(store["slots"]["1"])
        live["oauth"]["expiresAt"] = 1
        ccx.apply_slot(live)
        with redirect_stdout(StringIO()) as output:
            assert ccx.check_once(argparse.Namespace())[0] == 3
        assert "refresh_required" in output.getvalue()
        assert "refresh_required" in (root / "auto.log").read_text()
        assert ccx.pinned_slot(ccx.load_store()) == "1"


def test_legacy_nonfinite_cache_does_not_crash_guard():
    with fixture() as (_, store), redirect_stdout(StringIO()):
        store["usage_cache"]["1"] = {"diagnostic": float("nan")}
        ccx.write_json(ccx.STORE, store)
        guard = ccx.switch_guard(ccx.load_store(), "1", "2")
        assert guard == ccx.switch_guard(ccx.load_store(), "1", "2")
        assert ccx.do_switch(store, "2", expected=guard)


def test_json_command_error_is_structured_sanitized_and_nonzero():
    with fixture() as (_, store):
        store["control_generation"] = -1
        ccx.write_json(ccx.STORE, store)
        before = ccx.STORE.read_bytes()
        proc = subprocess.run([sys.executable, "-c", "import ccx,sys; from pathlib import Path; "
                               "ccx.STORE=Path(sys.argv[1]); sys.exit(ccx.main(['status','--json']))", str(ccx.STORE)],
                              cwd=Path(__file__).parent, capture_output=True, text=True, timeout=20)
        assert proc.returncode == 4 and proc.stdout == ""
        assert json.loads(proc.stderr) == {"schema_version": 1, "provider": "claude", "error_code": "store_corrupt"}
        assert ccx.STORE.read_bytes() == before
        with (patch.object(runtime, "status_snapshot", side_effect=ValueError("SECRET-STATE")),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
            assert ccx.cmd_status(argparse.Namespace(as_json=True)) == 4
        assert output.getvalue() == "" and "SECRET-STATE" not in errors.getvalue()
        assert json.loads(errors.getvalue())["error_code"] == "invalid_state"


def test_malformed_auth_never_reaches_apply_or_refresh():
    for oauth in (None, [], {}, {"accessToken": " "}, {"accessToken": 1},
                  *({"accessToken": "synthetic", "expiresAt": exp}
                    for exp in ("tomorrow", True, float("nan"), float("inf"), -1))):
        with fixture() as (_, store), redirect_stdout(StringIO()):
            store["slots"]["2"]["oauth"] = oauth
            ccx.write_json(ccx.STORE, store)
            before = ccx.creds_path().read_bytes()
            with patch.object(ccx, "refresh_token", side_effect=AssertionError("refresh forbidden")):
                assert not ccx.do_switch(store, "2")
            assert ccx.creds_path().read_bytes() == before
            target = next(s for s in runtime.status_snapshot()["slots"] if s["slot"] == "2")
            assert target["auth_state"] == "invalid"


def test_unknown_control_generation_never_writes_credentials():
    with fixture() as (_, store):
        store["control_generation"] = "unsupported"
        ccx.write_json(ccx.STORE, store)
        before = ccx.creds_path().read_bytes()
        try:
            ccx.do_switch(store, "2")
        except ccx.CorruptFile:
            pass
        else:
            raise AssertionError("invalid generation accepted")
        assert ccx.creds_path().read_bytes() == before


def test_snapshot_is_local_sanitized_and_does_not_mutate_store():
    with fixture() as (_, store):
        store["usage_cache"]["2"] = {
            "known_at": time.time() - 500,
            "usage": {"5h": {"pct": 90, "resets_at": "2000-01-01T00:00:00Z", "secret": "hidden-window"}},
            "error": "SECRET-ERROR", "token": "hidden-cache",
        }
        ccx.write_json(ccx.STORE, store)
        before = ccx.STORE.read_bytes()
        snapshot = runtime.status_snapshot()
        assert ccx.STORE.read_bytes() == before
        data = json.dumps(snapshot, allow_nan=False)
        for forbidden in ("fixture-access", "fixture-refresh", "example.invalid", "hidden", "SECRET-ERROR"):
            assert forbidden not in data
        target = next(s for s in snapshot["slots"] if s["slot"] == "2")
        window = target["quota"]["windows"]["5h"]
        assert window["percent"] == 0 and window["snapshot_percent"] == 90
        assert window["source"] == "projected"
        assert not target["quota"]["availability_confirmed"]
        assert target["quota"]["measurement_at"] is None
        assert target["quota"]["measurement_verified"] is False
        assert target["quota"]["error_code"] == "measurement_unavailable"
        assert snapshot["execution"]["automatic_recovery_available"] is False


def test_measurement_429_is_not_a_model_rejection():
    observed = runtime.quota_snapshot({"error": "HTTP 429"}, time.time())
    assert observed["error_code"] == "measurement_rate_limited"
    assert observed["freshness"] == "unknown" and observed["windows"] == {}
    obs = runtime.AttemptObservation("session-fixture")
    obs.observe(result(False))
    assert obs.finish(0).state == "completed"


def test_success_subtype_does_not_override_is_error():
    outcome = failed_attempt().finish(1, checkpoint_verified=True)
    assert outcome.state == "waiting_auth" and outcome.action == "reauthenticate"
    assert failed_attempt().finish(1).reason_code == "checkpoint_unverified"


def test_safeguard_refusal_does_not_rotate_or_fallback():
    for error in ("invalid_request", "billing_error", "model_not_found", "account_on_hold"):
        outcome = failed_attempt(error).finish(1, checkpoint_verified=True)
        assert outcome.state == "needs_attention" and outcome.action == "stop"


def test_cancellation_wins_over_retry_and_success():
    assert failed_attempt("rate_limit").finish(1, checkpoint_verified=True, cancelled=True).state == "cancelled"
    obs = runtime.AttemptObservation("session-fixture")
    obs.observe(result())
    assert obs.finish(0, cancelled=True).state == "cancelled"


def test_tool_in_flight_prevents_automatic_retry():
    obs = runtime.AttemptObservation("session-fixture")
    obs.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1"}]}})
    obs.observe({"type": "assistant", "error": "rate_limit"})
    obs.observe(result(True))
    assert obs.finish(1, checkpoint_verified=True).reason_code == "tool_result_ambiguous"


def test_completed_tool_and_verified_checkpoint_can_suggest_recovery():
    obs = runtime.AttemptObservation("session-fixture")
    obs.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1"}]}})
    obs.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1"}]}})
    obs.observe({"type": "assistant", "error": "rate_limit"})
    obs.observe(result(True))
    assert obs.finish(1, checkpoint_verified=True).action == "select_account"


def test_foreign_session_late_terminal_and_truncation_are_not_success():
    for events in ([], [{"type": "assistant", "session_id": "other"}, result()], [result(), result()]):
        obs = runtime.AttemptObservation("session-fixture")
        for event in events:
            obs.observe(event)
        assert obs.finish(0).state == "needs_attention"


def test_duplicate_and_orphan_tool_ids_cannot_authorize_retry():
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1"}]}}
    done = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1"}]}}
    for sequence in ([done], [use, use, done], [use, done, done]):
        obs = runtime.AttemptObservation("session-fixture")
        for event in sequence:
            obs.observe(event)
        obs.observe({"type": "assistant", "error": "rate_limit"})
        obs.observe(result(True))
        assert obs.finish(1, checkpoint_verified=True).reason_code == "protocol_incomplete"


def test_snapshot_rejects_nonfinite_usage_and_never_exposes_unknown_fields():
    now = time.time()
    for value in (float("nan"), float("inf"), -1, 101, True, "50"):
        snapshot = runtime.quota_snapshot({"known_at": now, "usage": {"5h": {"pct": value}}}, now)
        assert snapshot["windows"] == {}
        json.dumps(snapshot, allow_nan=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_"))
    for name, test in tests:
        test()
        print("ok ", name)
    print(f"tudo passou ({len(tests)} testes; fundacao, nao supervisor completo)")
