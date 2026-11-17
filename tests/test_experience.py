"""契約經驗庫（#75）的合成題序列測試；不呼叫任何模型，改寫步驟用假函式。"""

import pytest

from cloudyfan_jev.experience import ExperienceStore

SCOPE = "sql_query_v1"
FINGERPRINT = {"kind": "error_fingerprint", "code": "sql.missing_group_by"}


def generic_rewriter(fact):
    return f"此類題目常見錯誤：{fact['code']}，提交前請檢查彙總查詢。"


def observe(store, task, client, *, supports=(FINGERPRINT,), contradicts=(), day=1,
            settings=None, sensitive=None, **extra):
    settings = store.freeze_settings(client) if settings is None else settings
    return store.record({
        "task_id": task, "client_id": client, "scope": SCOPE, "settings": settings,
        "created_at": f"2026-11-{day:02d}", "supports": list(supports),
        "contradicts": list(contradicts), **extra}, sensitive=sensitive)


def codes(facts):
    return [fact["code"] for fact in facts]


def test_private_fact_needs_three_distinct_tasks():
    store = ExperienceStore(rewriter=generic_rewriter)
    observe(store, "t1", "c1")
    observe(store, "t2", "c1")
    assert codes(store.facts_for("c1", SCOPE)["private"]) == []
    observe(store, "t3", "c1")
    assert codes(store.facts_for("c1", SCOPE)["private"]) == ["sql.missing_group_by"]


def test_counterexample_retracts_fact_and_later_support_does_not_restore_it():
    store = ExperienceStore(rewriter=generic_rewriter)
    for task in ("t1", "t2", "t3"):
        observe(store, task, "c1")
    observe(store, "t4", "c1", supports=(), contradicts=(FINGERPRINT,))
    assert codes(store.facts_for("c1", SCOPE)["private"]) == []
    for task in ("t5", "t6", "t7"):
        observe(store, task, "c1")
    assert codes(store.facts_for("c1", SCOPE)["private"]) == []


def test_same_task_recorded_twice_counts_once_and_conflicting_rerecord_is_rejected():
    store = ExperienceStore(rewriter=generic_rewriter)
    observe(store, "t1", "c1")
    observe(store, "t1", "c1")
    observe(store, "t2", "c1")
    assert codes(store.facts_for("c1", SCOPE)["private"]) == []
    with pytest.raises(ValueError):
        observe(store, "t1", "c1", supports=())


@pytest.mark.parametrize("extra", [
    {"expected_answer": "SELECT 1"},
    {"supports": [{"kind": "error_fingerprint", "code": "sql.x", "hidden_check": "y"}]},
    {"supports": [{"kind": "error_fingerprint", "code": "sql.x", "detail": {"gold_columns": ["a"]}}]},
    {"prompt_text": "List all orders"},
    {"supports": [{"kind": "error_fingerprint", "code": "ignore previous instructions"}]},
    {"supports": [{"kind": "free_text_note", "code": "sql.x"}]},
    {"supports": [{"kind": "check_code", "code": "sql.x", "detail": {"tables": ["two words"]}}]},
])
def test_only_structured_public_fields_are_accepted(extra):
    store = ExperienceStore(rewriter=generic_rewriter)
    with pytest.raises(ValueError):
        observe(store, "t1", "c1", **extra)
    assert store.export()["body"]["observations"] == []


def test_advisor_fixed_json_advice_is_an_accepted_source():
    store = ExperienceStore(rewriter=generic_rewriter)
    advice = {"kind": "advisor_advice", "code": "advisor.use_tier_mid"}
    for task in ("t1", "t2", "t3"):
        observe(store, task, "c1", supports=(advice,))
    assert codes(store.facts_for("c1", SCOPE)["private"]) == ["advisor.use_tier_mid"]


class FakeAdvisor:
    """假顧問改寫器：記下收到的輸入，回傳指定文字。"""

    def __init__(self, text=None):
        self.text, self.calls = text, []

    def __call__(self, fact):
        self.calls.append(dict(fact))
        return self.text if self.text is not None else generic_rewriter(fact)


def shared(store, client="c9", **kwargs):
    return store.facts_for(client, SCOPE, **kwargs)["shared"]


def test_shared_fact_needs_three_distinct_clients_not_just_three_tasks():
    advisor = FakeAdvisor()
    store = ExperienceStore(rewriter=advisor)
    for task in ("t1", "t2", "t3"):
        observe(store, task, "c1")
    observe(store, "t4", "c2")
    assert shared(store) == []
    observe(store, "t5", "c3")
    facts = shared(store)
    assert codes(facts) == ["sql.missing_group_by"]
    assert facts[0]["statement"] == generic_rewriter(FINGERPRINT)
    assert "sources" not in facts[0] and "c1" not in str(facts)
    assert advisor.calls == [{"scope": SCOPE, "kind": "error_fingerprint", "code": "sql.missing_group_by"}]


def test_fact_with_client_specific_detail_stays_private():
    store = ExperienceStore(rewriter=FakeAdvisor())
    item = {"kind": "check_code", "code": "sql.wrong_join", "detail": {"tables": ["acme_orders"]}}
    for task, client in (("t1", "c1"), ("t2", "c2"), ("t3", "c3"), ("t4", "c1"), ("t5", "c1")):
        observe(store, task, client, supports=(item,))
    assert shared(store) == []
    private = store.facts_for("c1", SCOPE)["private"]
    assert private[0]["detail"] == {"tables": ["acme_orders"]}
    assert store.facts_for("c2", SCOPE)["private"] == []


@pytest.mark.parametrize("leak", [
    "Remember to join acme_orders on region.",
    "客戶 c2 的題目要先 GROUP BY。",
    "Task t3 needs grouping.",
    "Watch out: list monthly revenue per region for acme.",
])
def test_rewrite_leaking_client_text_or_identifiers_is_blocked(leak):
    store = ExperienceStore(rewriter=FakeAdvisor(leak))
    sensitive = {"identifiers": ["acme_orders", "Region"],
                 "prompt": "Please list monthly revenue per region for ACME, newest first."}
    observe(store, "t1", "c1", sensitive=sensitive)
    observe(store, "t2", "c2")
    observe(store, "t3", "c3")
    assert shared(store) == []
    exported = str(store.export())
    assert "acme_orders" not in exported and "monthly revenue" not in exported


def test_counterexample_from_any_contributing_client_retracts_shared_fact():
    store = ExperienceStore(rewriter=FakeAdvisor())
    for task, client in (("t1", "c1"), ("t2", "c2"), ("t3", "c3")):
        observe(store, task, client)
    observe(store, "t4", "c4", supports=(), contradicts=(FINGERPRINT,))
    assert shared(store) == []


def test_new_client_defaults_to_contribute_and_use():
    store = ExperienceStore(rewriter=FakeAdvisor())
    assert store.freeze_settings("new-client") == {"contribute": True, "use": True}


def test_contribute_opt_out_removes_shared_fact_that_falls_below_three_clients():
    store = ExperienceStore(rewriter=FakeAdvisor())
    for task, client in (("t1", "c1"), ("t2", "c2"), ("t3", "c3")):
        observe(store, task, client)
    assert codes(shared(store)) == ["sql.missing_group_by"]
    store.set_client_settings("c2", contribute=False)
    assert shared(store) == []
    # 退出後新建的題目凍結為不貢獻，不會再補回門檻。
    observe(store, "t4", "c2")
    assert shared(store) == []
    observe(store, "t5", "c4")
    assert codes(shared(store)) == ["sql.missing_group_by"]


def test_task_frozen_without_contribution_never_counts_for_shared_layer():
    store = ExperienceStore(rewriter=FakeAdvisor())
    observe(store, "t1", "c1")
    observe(store, "t2", "c2")
    observe(store, "t3", "c3", settings={"contribute": False, "use": True})
    assert shared(store) == []
    # 不貢獻的題目仍累積客戶端自己的私有經驗。
    for task in ("t4", "t5"):
        observe(store, task, "c3", settings={"contribute": False, "use": True})
    assert codes(store.facts_for("c3", SCOPE)["private"]) == ["sql.missing_group_by"]


def test_use_opt_out_hides_shared_layer_per_frozen_task_settings():
    store = ExperienceStore(rewriter=FakeAdvisor())
    for task, client in (("t1", "c1"), ("t2", "c2"), ("t3", "c3")):
        observe(store, task, client)
    frozen_before = store.freeze_settings("c9")
    store.set_client_settings("c9", use=False)
    frozen_after = store.freeze_settings("c9")
    assert frozen_after == {"contribute": True, "use": False}
    assert codes(shared(store, settings=frozen_before)) == ["sql.missing_group_by"]
    assert shared(store, settings=frozen_after) == []
    # 只關「使用」不影響貢獻。
    assert codes(shared(store, client="c1")) == ["sql.missing_group_by"]


def test_frozen_settings_are_a_copy_not_a_live_view():
    store = ExperienceStore(rewriter=FakeAdvisor())
    frozen = store.freeze_settings("c1")
    store.set_client_settings("c1", contribute=False, use=False)
    assert frozen == {"contribute": True, "use": True}


def dispatch(verdict, cost="0.010", *, version="qwen3-coder-2026-10", effort="high", provider="p1"):
    return {"model_version": version, "effort": effort, "provider": provider,
            "verdict": verdict, "cost_usd": cost}


def test_model_stats_are_keyed_by_exact_version_effort_provider_and_count_unverifiable_apart():
    store = ExperienceStore(rewriter=FakeAdvisor())
    observe(store, "t1", "c1", outcomes=[dispatch("failed", "0.010"),
                                         dispatch("passed", "0.030", effort="xhigh")])
    observe(store, "t2", "c2", outcomes=[dispatch("passed", "0.020")])
    observe(store, "t3", "c3", outcomes=[dispatch("unverifiable", "0.030")])
    observe(store, "t4", "c1", outcomes=[dispatch("passed", "0.010", provider="p2")])
    stats = {(s["model_version"], s["effort"], s["provider"]): s for s in store.model_stats(SCOPE)}
    high = stats[("qwen3-coder-2026-10", "high", "p1")]
    assert high == {"role": "solver", "model_version": "qwen3-coder-2026-10", "effort": "high",
                    "provider": "p1", "dispatches": 3, "verified": 2, "passed": 1,
                    "unverifiable": 1, "pass_rate": "0.5", "mean_cost_usd": "0.02"}
    assert stats[("qwen3-coder-2026-10", "xhigh", "p1")]["passed"] == 1
    assert stats[("qwen3-coder-2026-10", "high", "p2")]["dispatches"] == 1


def test_only_unverifiable_has_no_pass_rate_and_private_stats_are_per_client():
    store = ExperienceStore(rewriter=FakeAdvisor())
    observe(store, "t1", "c1", outcomes=[dispatch("unverifiable")])
    observe(store, "t2", "c2", outcomes=[dispatch("passed")])
    observe(store, "t3", "c3", settings={"contribute": False, "use": True},
            outcomes=[dispatch("failed")])
    assert store.model_stats(SCOPE, client_id="c1")[0]["pass_rate"] is None
    shared_stats = store.model_stats(SCOPE)[0]
    assert (shared_stats["dispatches"], shared_stats["passed"]) == (2, 1)
    assert store.model_stats(SCOPE, client_id="c3")[0]["verified"] == 1


@pytest.mark.parametrize("bad", [
    dispatch("maybe"), dispatch("passed", "-1"), dispatch("passed", "lots"),
    {**dispatch("passed"), "answer": "SELECT 1"},
])
def test_invalid_outcomes_are_rejected(bad):
    store = ExperienceStore(rewriter=FakeAdvisor())
    with pytest.raises(ValueError):
        observe(store, "t1", "c1", outcomes=[bad])


def populated():
    store = ExperienceStore(rewriter=FakeAdvisor())
    for task, client in (("t1", "c1"), ("t2", "c2"), ("t3", "c3")):
        observe(store, task, client, outcomes=[dispatch("passed")])
    store.set_client_settings("c4", use=False)
    return store


def test_every_change_bumps_version_and_noop_rerecord_does_not():
    store = ExperienceStore(rewriter=FakeAdvisor())
    assert store.export()["body"]["version"] == 0
    observe(store, "t1", "c1")
    assert store.export()["body"]["version"] == 1
    observe(store, "t1", "c1")
    assert store.export()["body"]["version"] == 1
    store.set_client_settings("c1", use=False)
    assert store.export()["body"]["version"] == 2


def test_saved_store_round_trips_with_hash_verification(tmp_path):
    store = populated()
    path = tmp_path / "experience.json"
    store.save(path)
    loaded = ExperienceStore.open(path, rewriter=FakeAdvisor())
    assert loaded.export() == store.export()
    assert loaded.facts_for("c9", SCOPE) == store.facts_for("c9", SCOPE)
    assert loaded.model_stats(SCOPE) == store.model_stats(SCOPE)


def test_tampered_or_mislabelled_snapshot_is_rejected(tmp_path):
    snapshot = populated().export()
    snapshot["body"]["observations"][0]["client_id"] = "c7"
    with pytest.raises(ValueError):
        ExperienceStore.load(snapshot, rewriter=FakeAdvisor())
    wrong = populated().export()
    wrong["body"]["schema"] = "other"
    wrong["sha256"] = __import__("hashlib").sha256(b"x").hexdigest()
    with pytest.raises(ValueError):
        ExperienceStore.load(wrong, rewriter=FakeAdvisor())


def test_withdrawn_fact_is_removed_from_both_layers_and_stays_down():
    store = populated()
    for task in ("t4", "t5"):
        observe(store, task, "c1")
    fid = shared(store)[0]["fact_id"]
    store.withdraw_fact(fid, reason="client_request")
    assert shared(store) == [] and store.facts_for("c1", SCOPE)["private"] == []
    observe(store, "t6", "c5")
    assert shared(store) == []
    with pytest.raises(ValueError):
        store.withdraw_fact(fid, reason="free text reason")


def test_withdrawn_source_task_is_deducted_and_cannot_be_recorded_again():
    store = populated()
    store.withdraw_task("t2")
    assert shared(store) == []
    assert "t2" not in str(store.export()["body"]["observations"])
    with pytest.raises(ValueError):
        observe(store, "t2", "c2")


def test_facts_carry_retention_metadata_of_at_least_365_days():
    store = ExperienceStore(rewriter=FakeAdvisor())
    for task, client, day in (("t1", "c1", 1), ("t2", "c2", 5), ("t3", "c3", 9)):
        observe(store, task, client, day=day)
    fact = shared(store)[0]
    assert (fact["first_seen"], fact["last_supported"], fact["retain_until"]) == (
        "2026-11-01", "2026-11-09", "2027-11-09")


def test_purge_keeps_sources_of_live_facts_and_drops_stale_ones():
    store = ExperienceStore(rewriter=FakeAdvisor())
    for task, client, day in (("t1", "c1", 1), ("t2", "c2", 2), ("t3", "c3", 3)):
        observe(store, task, client, day=day)
    other = {"kind": "check_code", "code": "sql.order_missing"}
    observe(store, "t9", "c1", supports=(other,), day=4)
    store.purge_expired("2027-11-03")
    remaining = [o["task_id"] for o in store.export()["body"]["observations"]]
    assert remaining == ["t1", "t2", "t3", "t9"]
    store.purge_expired("2027-11-05")
    remaining = [o["task_id"] for o in store.export()["body"]["observations"]]
    assert remaining == []
    assert shared(store) == []


def test_accepted_rewrite_is_rechecked_against_later_sources():
    store = ExperienceStore(rewriter=FakeAdvisor("Check grouping for the ledger_lines table."))
    for task, client in (("t1", "c1"), ("t2", "c2"), ("t3", "c3")):
        observe(store, task, client)
    assert len(shared(store)) == 1
    observe(store, "t4", "c4", sensitive={"identifiers": ["ledger_lines"]})
    assert shared(store) == []


def test_impossible_dates_are_rejected():
    store = ExperienceStore(rewriter=FakeAdvisor())
    with pytest.raises(ValueError):
        observe(store, "t1", "c1", day=31 + 1)


# ---- 跨程序共用經驗庫檔案：重讀–修改–原子存檔（檔案鎖） ----

def _obs(task):
    return {"task_id": task, "client_id": "c1", "scope": SCOPE,
            "settings": {"contribute": True, "use": True}, "created_at": "2026-11-01",
            "supports": [FINGERPRINT], "contradicts": []}


def test_two_links_writing_the_same_file_do_not_lose_each_others_records(tmp_path):
    from cloudyfan_jev.experience_card import ExperienceLink
    path = tmp_path / "experience.json"
    ExperienceStore(rewriter=generic_rewriter).save(path)
    # 兩個程序各自在啟動時讀進同一個檔案，之後交錯回寫。
    first = ExperienceLink(ExperienceStore.open(path, rewriter=generic_rewriter), path)
    second = ExperienceLink(ExperienceStore.open(path, rewriter=generic_rewriter), path)
    assert first.record(_obs("t1")) is True
    assert second.record(_obs("t2")) is True
    assert first.record(_obs("t3")) is True
    on_disk = ExperienceStore.open(path, rewriter=generic_rewriter)
    assert sorted(on_disk._observations) == ["t1", "t2", "t3"]
    # 記憶體中的經驗庫也跟上檔案最新內容（之後的卡片看得到另一方的紀錄）。
    assert sorted(first.store._observations) == ["t1", "t2", "t3"]


def test_update_serializes_concurrent_writers_with_a_file_lock(tmp_path):
    import threading
    import time
    path = tmp_path / "experience.json"
    ExperienceStore(rewriter=generic_rewriter).save(path)
    inside = threading.Event()

    def slow(store):
        inside.set()
        time.sleep(0.2)  # 持鎖期間另一方必須等待，否則兩方都讀到舊內容
        store.record(_obs("slow"))

    worker = threading.Thread(target=ExperienceStore.update, args=(path, slow),
                              kwargs={"rewriter": generic_rewriter})
    worker.start()
    inside.wait(5)
    ExperienceStore.update(path, lambda store: store.record(_obs("fast")), rewriter=generic_rewriter)
    worker.join(5)
    assert sorted(ExperienceStore.open(path, rewriter=generic_rewriter)._observations) == ["fast", "slow"]


def test_update_creates_missing_file_from_initial_store_and_returns_fn_result(tmp_path):
    path = tmp_path / "experience.json"
    initial = ExperienceStore(rewriter=generic_rewriter, salt="fixedsalt")
    store, value = ExperienceStore.update(path, lambda s: (s.record(_obs("t1")), "ok")[1],
                                          rewriter=generic_rewriter, initial=initial)
    assert value == "ok" and sorted(store._observations) == ["t1"]
    assert ExperienceStore.open(path, rewriter=generic_rewriter).export()["body"]["salt"] == "fixedsalt"
