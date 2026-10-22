"""Transactional race/recovery tests only; no network, keys or model answers."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from concurrent_ledger import ConcurrentLedger, LedgerStop

H = "a" * 64
G = "b" * 64
J = "c" * 64


def ledger(tmp_path, *, clock=lambda: 0, **kwargs):
    return ConcurrentLedger(
        tmp_path / "study.sqlite",
        plan_id="new-four-arm-plan",
        target_usd=kwargs.pop("target_usd", "1"),
        deadline=kwargs.pop("deadline", 100),
        clock=clock,
        **kwargs,
    )


def complete(book, wid="w", cid="c", cost="0.01", *, revision="usage", gid=None):
    assert book.begin(cid, wid, "generation", "0.01", H)
    book.save_response(cid, G)
    if gid:
        book.bind_generation(cid, gid)
    book.reconcile(cid, cost, J, revision)


def test_exact_cost_and_historical_reserve_remain_separate(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.243455651", historical_reserve_usd="0.01")
    book.admit("w", "0.05", 3, 2)
    complete(book, cost="0.0003541525")
    state = book.snapshot()
    assert state["known_cumulative_usd"] == "0.2438098035"
    assert state["actual_cumulative_usd"] is None
    assert state["budget_claim_usd"] == "0.303455651"
    assert state["unused_reservations_usd"] == "0.0496458475"
    book.finish("w")
    assert book.snapshot()["budget_claim_usd"] == "0.2538098035"


def test_four_admissions_cannot_race_past_budget(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.1")
    barrier = Barrier(4)

    def admit(i):
        barrier.wait()
        try:
            return book.admit("w" + str(i), "0.3", 2, 0)
        except LedgerStop as error:
            assert error.code == "COMPLETE_WORKFLOW_RESERVATION_EXCEEDS_TARGET"
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(admit, range(4))) == 3
    assert book.snapshot()["budget_claim_usd"] == "1"
    assert book.snapshot()["global_stop"] is None


def test_four_inflight_calls_and_incremental_costs(tmp_path):
    book = ledger(tmp_path)
    for i in range(4):
        book.admit("w" + str(i), "0.2", 3, 2)
    barrier, release = Barrier(5), Event()

    def call(i):
        wid, cid = "w" + str(i), "c" + str(i)
        assert book.begin(cid, wid, "generation", "0.1", H)
        barrier.wait()
        assert release.wait(2)
        book.save_response(cid, G)
        book.bind_generation(cid, "gen-" + str(i))
        book.reconcile(cid, "0.03", J)
        book.finish(wid)

    with ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(call, i) for i in range(4)]
        barrier.wait()
        state = book.snapshot()
        assert state["in_flight_calls"] == 4 and state["unknown_calls"] == 0
        assert state["actual_cumulative_usd"] is None
        release.set()
        for future in futures:
            future.result()
    state = book.snapshot()
    assert state["actual_usd"] == "0.12" and state["unused_reservations_usd"] == "0"
    assert len(state["generation_bindings"]) == 4


def test_same_call_id_has_only_one_dispatch_winner(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.5", 3, 2)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: book.begin("c", "w", "generation", "0.1", H), range(20)))
    assert sum(results) == 1
    assert book.lookup("c")["state"] == "intent_in_flight"
    assert book.snapshot()["workflows"][0]["generations_started"] == 1


def test_generation_and_classifier_caps_count_all_attempts(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.5", 2, 1)
    complete(book, cid="c1", cost="0.01")
    complete(book, cid="c2", cost="0.01")
    with pytest.raises(LedgerStop, match="CAP_EXCEEDED"):
        book.begin("c3", "w", "generation", "0.01", H)
    assert book.begin("class1", "w", "classifier", "0.02", H)
    with pytest.raises(LedgerStop, match="CAP_EXCEEDED"):
        book.begin("class2", "w", "classifier", "0.02", H)
    assert book.snapshot()["global_stop"] is None


def test_unknown_stops_every_new_call_but_allows_evidence_finalization(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.4", 3, 2)
    book.admit("other", "0.4", 2, 0)
    book.begin("c", "w", "generation", "0.1", H)
    book.mark_unknown("c", "missing_charge")
    with pytest.raises(LedgerStop, match="GLOBAL"):
        book.begin("new", "other", "generation", "0.1", H)
    assert book.snapshot()["unused_reservations_usd"] == "0.8"
    book.save_response("c", G)
    book.reconcile("c", "0.02", J)
    book.finish("w")
    assert book.snapshot()["global_stop"] is not None
    assert book.lookup("c")["cost_usd"] == "0.02"


def test_constructor_replays_cache_without_quarantining_live_workers(tmp_path):
    first = ledger(tmp_path)
    first.admit("w", "0.5", 3, 0)
    first.begin("c", "w", "generation", "0.1", H)
    second = ledger(tmp_path)
    assert second.snapshot()["unknown_calls"] == 0
    assert second.begin("c", "w", "generation", "0.1", H) is False
    assert second.snapshot()["global_stop"] is None
    recovered = second.recover()
    assert recovered["unknown_calls"] == 1
    assert recovered["unused_reservations_usd"] == "0.5"
    assert recovered["global_stop"]["code"] == "RECOVERY_UNRESOLVED_COST"
    assert second.recover()["event_head"] == recovered["event_head"]


def test_crash_after_raw_before_cost_is_quarantined(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    book.begin("c", "w", "generation", "0.1", H)
    book.save_response("c", G)
    assert book.recover()["global_stop"] is not None
    assert "response_without_verified_cost" in book.lookup("c")["unknown_reason"]
    assert book.begin("c", "w", "generation", "0.1", H) is False


def test_release_only_undispatched_reservation(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.8", 2, 0)
    book.release("w")
    assert book.snapshot()["unused_reservations_usd"] == "0"
    book.admit("active", "0.8", 2, 0)
    book.begin("c", "active", "generation", "0.1", H)
    with pytest.raises(LedgerStop, match="DISPATCHED"):
        book.release("active")
    with pytest.raises(LedgerStop, match="UNRESOLVED"):
        book.finish("active")
    assert book.snapshot()["unused_reservations_usd"] == "0.8"


def test_usage_then_invoice_replaces_once_and_keeps_both_events(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.1", 2, 0)
    complete(book, cost="0.0003541525")
    book.reconcile("c", "0.000354152", H, "generation")
    state = book.snapshot()
    assert state["actual_usd"] == "0.000354152"
    before = state["event_head"]
    book.reconcile("c", "0.0003541525", J)
    book.reconcile("c", "0.000354152", H, "generation")
    assert book.snapshot()["event_head"] == before
    assert ledger(tmp_path).snapshot()["actual_usd"] == "0.000354152"
    with sqlite3.connect(book.path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM events WHERE kind='cost'").fetchone()[0] == 2
        )


def test_usage_cannot_downgrade_invoice_when_it_arrives_later(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.1", 2, 0)
    complete(book, cost="0.02", revision="canonical")
    book.reconcile("c", "0.01", H)
    assert book.snapshot()["actual_usd"] == "0.02"
    assert ledger(tmp_path).lookup("c")["revision_id"] == "canonical"


def test_late_finished_workflow_fee_preserves_all_other_reserves(tmp_path):
    book = ledger(tmp_path, target_usd="0.1")
    book.admit("old", "0.02", 2, 0)
    complete(book, "old", cost="0.01")
    book.finish("old")
    book.admit("remaining", "0.08", 3, 2)
    with pytest.raises(LedgerStop, match="LATE_COST_DEFICIT"):
        book.reconcile("c", "0.04", H, "canonical")
    state = book.snapshot()
    assert state["actual_usd"] == "0.04"
    assert state["unused_reservations_usd"] == "0.08"
    assert state["budget_claim_usd"] == "0.12"
    assert ledger(tmp_path, target_usd="0.1").snapshot()["global_stop"] is not None


def test_current_workflow_fee_replacement_uses_only_own_reserve(tmp_path):
    book = ledger(tmp_path, target_usd="0.1")
    book.admit("w", "0.05", 3, 0)
    book.admit("other", "0.05", 3, 0)
    complete(book, cost="0.01")
    book.reconcile("c", "0.04", H, "canonical")
    state = book.snapshot()
    assert state["budget_claim_usd"] == "0.1"
    assert state["workflows"][0]["unused_reservation_usd"] == "0.05"
    assert state["workflows"][1]["unused_reservation_usd"] == "0.01"


def test_negative_canonical_adjustment_is_not_double_counted(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    complete(book, cost="0.1")
    book.finish("w")
    book.reconcile("c", "0.04", H, "canonical")
    assert book.snapshot()["actual_usd"] == "0.04"
    assert book.snapshot()["budget_claim_usd"] == "0.04"


def test_rebuild_ignores_corrupted_cached_totals(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.3", 2, 0)
    complete(book, cost="0.02")
    book.finish("w")
    with sqlite3.connect(book.path) as c:
        c.execute(
            "UPDATE meta SET value=? WHERE key='aggregate'",
            (json.dumps({"actual_usd": "0", "reservations_usd": "0", "global_stop": None}),),
        )
    assert ledger(tmp_path).snapshot()["actual_usd"] == "0.02"


def test_append_only_events_and_chain_tamper_rejected(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    with sqlite3.connect(book.path) as c:
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            c.execute("DELETE FROM events")
        c.execute("DROP TRIGGER events_no_update")
        c.execute("UPDATE events SET event_hash=? WHERE seq=2", ("f" * 64,))
    with pytest.raises(LedgerStop, match="EVENT_CHAIN_CHANGED"):
        ledger(tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"target_usd": "2"},
        {"prior_known_usd": "0.1"},
        {"deadline": 101},
        {"historical_reserve_usd": "0.01"},
    ],
)
def test_binding_cannot_change_on_restart(tmp_path, change):
    book = ledger(tmp_path)
    with pytest.raises(LedgerStop, match="BINDING"):
        ledger(tmp_path, **change)
    assert book.snapshot()["global_stop"] is None


def test_expiry_blocks_new_intents_but_preserves_late_response_and_invoice(tmp_path):
    now = [0]
    book = ledger(tmp_path, clock=lambda: now[0])
    book.admit("w", "0.2", 2, 0)
    book.begin("c", "w", "generation", "0.1", H)
    now[0] = 100
    with pytest.raises(LedgerStop, match="EXPIRED"):
        book.begin("next", "w", "generation", "0.05", H)
    book.save_response("c", G)
    book.reconcile("c", "0.01", J)
    book.finish("w")
    assert book.snapshot()["actual_usd"] == "0.01"
    assert book.snapshot()["expired"] is True
    assert book.snapshot()["global_stop"] is not None


def test_inflight_subreservations_cannot_exceed_workflow_forecast(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.1", 3, 0)
    book.begin("c1", "w", "generation", "0.07", H)
    with pytest.raises(LedgerStop, match="INFLIGHT"):
        book.begin("c2", "w", "generation", "0.04", H)
    assert book.snapshot()["workflows"][0]["generations_started"] == 1


def test_global_generation_duplicate_race_preserves_response_evidence_and_stops(tmp_path):
    book = ledger(tmp_path)
    for i in range(2):
        book.admit("w" + str(i), "0.2", 2, 0)
        book.begin("c" + str(i), "w" + str(i), "generation", "0.1", H)
        book.save_response("c" + str(i), G)
    barrier = Barrier(2)

    def bind(i):
        barrier.wait()
        try:
            book.bind_generation("c" + str(i), "same-generation")
            return True
        except LedgerStop as error:
            assert "DUPLICATE_GLOBAL" in str(error)
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(bind, range(2))) == 1
    state = book.snapshot()
    assert len(state["generation_bindings"]) == 1
    assert len(state["duplicate_generation_calls"]) == 1
    assert all(c["response_sha256"] == G for c in state["calls"])
    assert state["global_stop"] is not None


@pytest.mark.parametrize("conflict", ["request", "response", "cost"])
def test_conflicting_duplicate_evidence_is_durable_stop(tmp_path, conflict):
    book = ledger(tmp_path)
    book.admit("w", "0.3", 2, 0)
    complete(book)
    with pytest.raises(LedgerStop, match="CONFLICT"):
        if conflict == "request":
            book.begin("c", "w", "generation", "0.01", G)
        elif conflict == "response":
            book.save_response("c", J)
        else:
            book.reconcile("c", "0.02", H)
    state = ledger(tmp_path).snapshot()
    assert state["global_stop"] is not None
    assert state["actual_usd"] == "0.01"


def test_unknown_and_prior_money_are_never_silently_zero(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.243455651", historical_reserve_usd="0.01")
    book.admit("w", "0.1", 2, 0)
    book.begin("c", "w", "generation", "0.05", H)
    book.mark_unknown("c", "provider_525_no_receipt")
    assert book.snapshot()["budget_claim_usd"] == "0.353455651"
    assert book.lookup("c")["cost_usd"] is None
    assert book.snapshot()["actual_cumulative_usd"] is None


@pytest.mark.parametrize("value", [True, 0.1, "NaN", "-1", "Infinity"])
def test_invalid_or_inexact_money_rejected(tmp_path, value):
    with pytest.raises(LedgerStop):
        ledger(tmp_path, target_usd=value)


def test_known_cost_requires_raw_response_evidence(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    book.begin("c", "w", "generation", "0.1", H)
    with pytest.raises(LedgerStop, match="RAW_RESPONSE"):
        book.reconcile("c", "0.01", H)
    assert book.lookup("c")["cost_usd"] is None


def test_no_rebuild_on_normal_snapshot_or_begin(tmp_path, monkeypatch):
    book = ledger(tmp_path)
    monkeypatch.setattr(
        book, "_rebuild", lambda _: pytest.fail("Normal operation must use incremental cache")
    )
    book.admit("w", "0.2", 2, 0)
    assert book.begin("c", "w", "generation", "0.1", H)
    assert book.snapshot()["in_flight_calls"] == 1


def test_final_begin_clock_check_after_budget_work(tmp_path):
    now = [0]
    book = ledger(tmp_path, clock=lambda: now[0])
    book.admit("w", "0.2", 2, 0)
    ticks = iter([0, 100, 100])
    book.clock = lambda: next(ticks, 100)
    with pytest.raises(LedgerStop, match="EXPIRED"):
        book.begin("c", "w", "generation", "0.1", H)
    assert book.lookup("c") is None


def test_deadline_attribute_cannot_extend_bound_plan(tmp_path):
    book = ledger(tmp_path)
    book.deadline = 200
    with pytest.raises(LedgerStop, match="BINDING"):
        book.admit("w", "0.2", 2, 0)


def test_independent_instances_see_incremental_fee_adjustments(tmp_path):
    first = ledger(tmp_path)
    first.admit("w", "0.2", 2, 0)
    complete(first, cost="0.01")
    second = ledger(tmp_path)
    second.reconcile("c", "0.04", H, "invoice")
    assert first.lookup("c")["cost_usd"] == "0.04"
    assert first.snapshot()["budget_claim_usd"] == "0.2"


def test_no_cost_finalization_for_duplicate_generation_attribution(tmp_path):
    book = ledger(tmp_path)
    for i in range(2):
        wid, cid = "w" + str(i), "c" + str(i)
        book.admit(wid, "0.2", 2, 0)
        book.begin(cid, wid, "generation", "0.1", H)
        book.save_response(cid, G)
    book.bind_generation("c0", "gen-one")
    with pytest.raises(LedgerStop, match="DUPLICATE_GLOBAL"):
        book.bind_generation("c1", "gen-one")
    with pytest.raises(LedgerStop, match="UNATTRIBUTABLE"):
        book.reconcile("c1", "0.01", H)
    assert book.lookup("c1")["cost_usd"] is None
    assert book.lookup("c1")["unknown"] is True


def test_transient_admission_denial_can_succeed_after_fixed_predecessor_finishes(tmp_path):
    book = ledger(tmp_path, target_usd="0.2")
    book.admit("first", "0.15", 2, 0)
    with pytest.raises(LedgerStop, match="RESERVATION_EXCEEDS"):
        book.admit("next", "0.15", 2, 0)
    complete(book, "first", cost="0.01")
    book.finish("first")
    assert book.admit("next", "0.15", 2, 0) is True
    assert book.admit("next", "0.15", 2, 0) is False
    assert book.snapshot()["global_stop"] is None


def test_event_rebuild_and_multiple_instances_are_exact_under_four_thread_revisions(tmp_path):
    book = ledger(tmp_path)
    for i in range(4):
        book.admit("w" + str(i), "0.1", 3, 0)
        complete(book, "w" + str(i), "c" + str(i), "0.0003541525")
    with ThreadPoolExecutor(4) as pool:
        list(
            pool.map(
                lambda i: book.reconcile("c" + str(i), "0.000354152", H, "generation"), range(4)
            )
        )
    assert book.snapshot()["actual_usd"] == "0.001416608"
    assert book.recover()["actual_usd"] == "0.001416608"
    assert ledger(tmp_path).snapshot()["actual_usd"] == "0.001416608"


def test_unrelated_existing_database_is_not_modified(tmp_path):
    path = tmp_path / "study.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE unrelated (secret TEXT)")
        c.execute("INSERT INTO unrelated VALUES('synthetic only')")
    before = path.read_bytes()
    with pytest.raises(LedgerStop, match="UNRECOGNIZED"):
        ledger(tmp_path)
    assert path.read_bytes() == before


def test_snapshot_pending_cost_is_not_falsely_complete_actual_total(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    book.begin("c", "w", "generation", "0.1", H)
    assert book.snapshot()["unknown_calls"] == 0
    assert book.snapshot()["pending_cost_calls"] == 1
    assert book.snapshot()["actual_cumulative_usd"] is None


def test_global_generation_binding_survives_verified_rebuild(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.1", 2, 0)
    complete(book, gid="gen-original")
    restarted = ledger(tmp_path)
    assert restarted.lookup("c")["generation_id"] == "gen-original"
    assert restarted.snapshot()["generation_bindings"] == {"gen-original": "c"}


def test_initial_historical_reserve_is_not_rounded_out_of_budget(tmp_path):
    with pytest.raises(LedgerStop, match="INITIAL_BUDGET"):
        ledger(
            tmp_path, prior_known_usd="1", historical_reserve_usd="0.000000000000000000000000000001"
        )


def test_completion_seal_is_idempotent_and_survives_event_rebuild(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    complete(book)
    book.finish("w")
    book.seal_completion("w", H)
    head = book.snapshot()["event_head"]
    book.seal_completion("w", H)
    assert book.snapshot()["event_head"] == head
    assert book.snapshot()["workflows"][0]["completion_sha256"] == H
    assert ledger(tmp_path).snapshot()["workflows"][0]["completion_sha256"] == H


def test_completion_seal_requires_finished_workflow(tmp_path):
    book = ledger(tmp_path)
    with pytest.raises(LedgerStop, match="MUST_BE_FINISHED"):
        book.seal_completion("not-admitted", H)
    book.admit("w", "0.2", 2, 0)
    with pytest.raises(LedgerStop, match="MUST_BE_FINISHED"):
        book.seal_completion("w", H)
    complete(book)
    with pytest.raises(LedgerStop, match="MUST_BE_FINISHED"):
        book.seal_completion("w", H)
    assert book.snapshot()["workflows"][0]["completion_sha256"] is None


def test_changed_completion_hash_persists_stop_and_preserves_original(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    complete(book)
    book.finish("w")
    book.seal_completion("w", H)
    with pytest.raises(LedgerStop, match="SEAL_IDENTITY_CHANGED"):
        book.seal_completion("w", G)
    restored = ledger(tmp_path).snapshot()
    assert restored["global_stop"] is not None
    assert restored["workflows"][0]["completion_sha256"] == H
    assert restored["actual_usd"] == "0.01"


def test_four_seal_contenders_append_one_completion_event(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    complete(book)
    book.finish("w")
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: book.seal_completion("w", H), range(4)))
    with sqlite3.connect(book.path) as c:
        assert c.execute("SELECT count(*) FROM events WHERE kind='completion'").fetchone()[0] == 1


def test_lookup_workflow_reads_one_cached_row_without_snapshot_scan(tmp_path, monkeypatch):
    book = ledger(tmp_path)
    assert book.lookup_workflow("missing") is None
    book.admit("w", "0.2", 2, 0)
    assert book.lookup_workflow("w")["status"] == "reserved"
    complete(book)
    book.finish("w")
    book.seal_completion("w", H)
    monkeypatch.setattr(book, "_snapshot", lambda _: pytest.fail("No all-call scan needed"))
    row = book.lookup_workflow("w")
    assert row["status"] == "finished" and row["completion_sha256"] == H
    row["actual_usd"] = "0"
    assert book.lookup_workflow("w")["actual_usd"] == "0.01"


def import_paid(
    book,
    cid="old",
    wid="w",
    *,
    cost="0.01",
    required="0.2",
    gid="gen-old",
    response=G,
    kind="generation",
):
    return book.import_historical_call(
        cid, wid, kind, H, response, gid, cost, J, required_prior_known_usd=required
    )


def test_historical_prefix_fee_included_once_and_full_forecast_preserved(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2", historical_reserve_usd="0.01")
    book.admit("w", "0.3", 3, 2)
    assert import_paid(book) is True
    s = book.snapshot()
    assert s["actual_usd"] == "0"
    assert s["known_cumulative_usd"] == "0.2"
    assert s["budget_claim_usd"] == "0.51"
    assert s["reservations_usd"] == "0.3"
    assert s["historical_imported_usd"] == "0.01"
    assert s["workflows"][0]["generations_started"] == 1
    assert s["workflows"][0]["actual_usd"] == "0"
    assert book.lookup("old")["historical"] is True
    assert book.lookup("old")["cost_usd"] == "0.01"
    assert s["actual_cumulative_usd"] is None  # Old unknown reserve remains.
    complete(book, cid="new", cost="0.02")
    assert book.snapshot()["known_cumulative_usd"] == "0.22"
    assert book.snapshot()["budget_claim_usd"] == "0.51"


def test_historical_import_idempotent_rebuild_and_cap(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.3", 1, 0)
    import_paid(book)
    head = book.snapshot()["event_head"]
    assert import_paid(book) is False
    assert book.snapshot()["event_head"] == head
    restarted = ledger(tmp_path, prior_known_usd="0.2")
    assert restarted.lookup("old") == book.lookup("old")
    assert restarted.snapshot()["historical_imported_usd"] == "0.01"
    with pytest.raises(LedgerStop, match="CAP_EXCEEDED"):
        restarted.begin("new", "w", "generation", "0.01", H)
    restarted.finish("w")
    assert restarted.snapshot()["budget_claim_usd"] == "0.2"
    assert import_paid(restarted) is False


def test_historical_import_after_expiry_does_not_authorize_dispatch(tmp_path):
    time = [0]
    book = ledger(tmp_path, prior_known_usd="0.2", clock=lambda: time[0])
    book.admit("w", "0.2", 2, 0)
    time[0] = 101
    assert import_paid(book) is True
    with pytest.raises(LedgerStop, match="EXPIRED"):
        book.begin("new", "w", "generation", "0.01", H)
    assert book.lookup("new") is None
    assert book.lookup("old")["cost_usd"] == "0.01"


def test_historical_import_requires_admission(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2")
    with pytest.raises(LedgerStop, match="NOT_ADMITTED"):
        import_paid(book)
    assert book.snapshot()["historical_imported_usd"] == "0"


@pytest.mark.parametrize("cost,required", [("0.21", "0.2"), ("0.01", "0.3")])
def test_historical_import_must_fit_explicit_prior_binding(tmp_path, cost, required):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.2", 3, 0)
    with pytest.raises(LedgerStop, match="EXCEEDS_BOUND_PRIOR_KNOWN"):
        import_paid(book, cost=cost, required=required)
    assert book.snapshot()["global_stop"] is not None
    assert book.lookup("old") is None


def test_historical_import_aggregate_cannot_recycle_prior_prefix(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.2", 3, 0)
    import_paid(book, cost="0.15")
    with pytest.raises(LedgerStop, match="EXCEEDS_BOUND_PRIOR_KNOWN"):
        import_paid(book, cid="old2", cost="0.06", gid="gen-two", response=H)
    assert book.snapshot()["historical_imported_usd"] == "0.15"
    assert book.snapshot()["actual_usd"] == "0"


@pytest.mark.parametrize("field", ["gid", "response"])
def test_historical_import_refuses_duplicate_global_evidence(tmp_path, field):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.2", 3, 0)
    import_paid(book)
    args = {"gid": "gen-two", "response": H}
    args[field] = "gen-old" if field == "gid" else G
    with pytest.raises(LedgerStop, match="DUPLICATE"):
        import_paid(book, cid="old2", **args)
    assert book.snapshot()["global_stop"] is not None
    assert book.snapshot()["historical_imported_usd"] == "0.01"


def test_historical_changed_identity_and_ordinary_conversion_fail_closed(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.2", 3, 0)
    complete(book)
    with pytest.raises(LedgerStop, match="HISTORICAL_CALL_IDENTITY_CONFLICT"):
        import_paid(book, cid="c")
    assert book.lookup("c").get("historical") is None
    assert book.snapshot()["actual_usd"] == "0.01"


def test_historical_cost_reconcile_exact_only_never_adds_to_actual(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.2", 3, 0)
    import_paid(book)
    head = book.snapshot()["event_head"]
    book.reconcile("old", "0.01", J)
    assert book.snapshot()["event_head"] == head
    with pytest.raises(LedgerStop, match="REQUIRES_PRIOR_LEDGER_ADJUSTMENT"):
        book.reconcile("old", "0.02", H, "canonical")
    s = ledger(tmp_path, prior_known_usd="0.2").snapshot()
    assert s["actual_usd"] == "0" and s["known_cumulative_usd"] == "0.2"
    assert s["global_stop"] is not None
    assert s["calls"][0]["cost_usd"] == "0.01"


def test_historical_classifier_consumes_classifier_slot_only(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.2", 1, 1)
    import_paid(book, kind="classifier")
    w = book.lookup_workflow("w")
    assert w["classifiers_started"] == 1 and w["generations_started"] == 0
    with pytest.raises(LedgerStop, match="CAP_EXCEEDED"):
        book.begin("other", "w", "classifier", "0.01", H)
    assert book.begin("gen", "w", "generation", "0.01", H)


def test_halt_persists_without_inventing_unknown_call_or_fee(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    book.halt("LOCAL_RAW_WITHOUT_LEDGER_INTENT")
    head = book.snapshot()["event_head"]
    book.halt("other-later-stop")
    assert book.snapshot()["event_head"] == head
    restored = ledger(tmp_path)
    s = restored.snapshot()
    assert s["global_stop"]["code"] == "LOCAL_RAW_WITHOUT_LEDGER_INTENT"
    assert s["calls"] == [] and s["actual_usd"] == "0"
    with pytest.raises(LedgerStop, match="GLOBAL_DURABLE_STOP"):
        restored.begin("new", "w", "generation", "0.01", H)


def test_step_seal_requires_saved_known_response_and_rebuilds(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    with pytest.raises(LedgerStop, match="KNOWN_RESPONSE_COST_REQUIRED"):
        book.seal_step("c", H)
    book.begin("c", "w", "generation", "0.01", H)
    book.save_response("c", G)
    with pytest.raises(LedgerStop, match="KNOWN_RESPONSE_COST_REQUIRED"):
        book.seal_step("c", H)
    book.reconcile("c", "0.01", J)
    book.seal_step("c", H)
    head = book.snapshot()["event_head"]
    book.seal_step("c", H)
    assert book.snapshot()["event_head"] == head
    assert ledger(tmp_path).lookup("c")["step_sha256"] == H
    book.reconcile("c", "0.015", G, "canonical")
    assert book.lookup("c")["step_sha256"] == H
    assert book.snapshot()["actual_usd"] == "0.015"


def test_changed_step_seal_persists_stop_without_changing_original(tmp_path):
    book = ledger(tmp_path)
    book.admit("w", "0.2", 2, 0)
    complete(book)
    book.seal_step("c", H)
    with pytest.raises(LedgerStop, match="STEP_SEAL_IDENTITY_CHANGED"):
        book.seal_step("c", G)
    rebuilt = ledger(tmp_path)
    assert rebuilt.lookup("c")["step_sha256"] == H
    assert rebuilt.snapshot()["global_stop"] is not None


def test_historical_step_seal_allowed_but_unknown_attempt_cannot_be_sealed(tmp_path):
    book = ledger(tmp_path, prior_known_usd="0.2")
    book.admit("w", "0.2", 2, 0)
    import_paid(book)
    book.seal_step("old", H)
    assert book.lookup("old")["step_sha256"] == H
    assert book.snapshot()["actual_usd"] == "0"
    book.mark_unknown("old", "synthetic identity conflict")
    with pytest.raises(LedgerStop, match="KNOWN_RESPONSE_COST_REQUIRED"):
        book.seal_step("old", H)
