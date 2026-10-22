from copy import deepcopy
import json

import httpx
import pytest

from cloudyfan_jev.scores.categories import CATEGORY_SPECS
from cloudyfan_monitor.pools import OpenRouterPoolSource, empty_view, price_scenario
from cloudyfan_monitor.store import MonitorError

NOW = "2026-10-22T02:00:00+00:00"
SYNTHETIC_SOURCE_CREDENTIAL = "SYNTHETIC-PRIVATE-SOURCE"


def fixtures():
    models=[]
    for name,score,cost in [("a",10,"0.0000001"),("b",40,"0.0000002"),("c",70,"0.0000003")]:
        models.append({"id":"example/"+name,"canonical_slug":"example/"+name,"context_length":32000,
                       "architecture":{"output_modalities":["text"]},"supported_parameters":[],
                       "pricing":{"prompt":cost,"completion":cost}, "hugging_face_id":"weights/"+name})
    rows=[]
    for key,spec in CATEGORY_SPECS.items():
        for model,value in zip(models,[.1,.4,.7]):
            row={"source":spec["source"],"model_permaslug":model["id"],
                 spec["metric"]:value if spec["unit"]=="ratio0..1" else value*100,
                 **{k:spec[k] for k in ("arena","category","benchmark_type") if k in spec}}
            if spec["metric"]=="f1_by_item":
                row.update(primary_metric="f1_by_item",primary_score=value)
            rows.append(row)
    policies={c:{"low_mid":.25 if s["unit"]=="ratio0..1" else 25,
                 "mid_high":.50 if s["unit"]=="ratio0..1" else 50,
                 "max_source_age_hours":48} for c,s in CATEGORY_SPECS.items()}
    # AA entries contain their three dimensions once, not three ambiguous identities.
    aa={}
    others=[]
    for r in rows:
        if r["source"]=="artificial-analysis":
            aa.setdefault(r["model_permaslug"],{}).update(r)
        else:
            others.append(r)
    data={"meta":{"version":"v1","as_of":NOW},"data":[*aa.values(),*others]}
    return models,data,policies


def source(*, endpoint_limit=2, mutate=None):
    models,data,policies=fixtures()
    requests=[]
    if mutate: mutate(models,data)
    def serve(request):
        requests.append(request)
        assert request.method=="GET"
        if request.url.path.endswith("/models"):
            return httpx.Response(200,json={"data":models})
        if request.url.path.endswith("/benchmarks"):
            return httpx.Response(200,json=data if not request.url.query else {"meta":{"version":"v1"},"data":[]})
        model=request.url.path.removeprefix("/api/v1/models/").removesuffix("/endpoints")
        return httpx.Response(200,json={"data":{"id":model,"endpoints":[{
            "tag":"provider/one","model_id":model,"provider_name":"Provider",
            "pricing":{"prompt":"0.0000001","completion":"0.0000001"},
            "latency_last_30m":{"p50":200,"p90":500}, "throughput_last_30m":{"p50":42}}]}})
    obj=OpenRouterPoolSource(httpx.Client(transport=httpx.MockTransport(serve)),policies,
                             api_key=SYNTHETIC_SOURCE_CREDENTIAL,endpoint_limit=endpoint_limit,clock=lambda:NOW)
    return obj,requests


def test_all_23_categories_and_four_pools_update_via_bounded_get_only():
    obj,requests=source()
    view=obj.refresh()
    assert len(view["categories"])==23 and view["model_posts"]==0
    assert len(requests)==5 and view["endpoint_models_deferred"]==1
    assert SYNTHETIC_SOURCE_CREDENTIAL not in json.dumps(view)
    for cat in view["categories"].values():
        assert set(cat["pools"])=={"open_source","low","mid","high"}
        assert cat["pools"]["low"][0]["profile"]["model"]=="example/a"
        assert cat["pools"]["mid"][0]["profile"]["model"]=="example/b"
        assert cat["pools"]["high"][0]["profile"]["model"]=="example/c"
        assert len(cat["pools"]["open_source"])==3
        for candidates in cat["pools"].values():
            assert all(c.get("runtime_qualified") is not True for c in candidates)
    assert all(r.url.host=="openrouter.ai" for r in requests)
    low=view["categories"]["artificial-analysis:coding_index"]["pools"]["low"][0]
    assert low["endpoint_p50_ms"]==200 and low["profile"]["provider"]=="provider/one"
    assert low["flow_latency_ms"] is None  # endpoint p50 is not whole-task latency


def test_missing_price_speed_and_stale_source_are_not_invented_zero():
    def mutate(models,data):
        models[0]["pricing"]={"prompt":"-1","completion":"-1"}
        data["meta"]["as_of"]="2026-09-01T00:00:00+00:00"
    obj,_=source(mutate=mutate,endpoint_limit=0)
    view=obj.refresh()
    low=view["categories"]["artificial-analysis:coding_index"]["pools"]["low"][0]
    assert low["status"]=="pending_unqualified_stale_source"
    assert low["quote_usd"] is None and low["endpoint_p50_ms"] is None
    assert low["flow_cost_usd"] is None and low["flow_latency_ms"] is None


def test_ambiguous_variant_not_cherry_picked():
    def mutate(models,data):
        original=next(r for r in data["data"] if r["source"]=="artificial-analysis")
        modified=deepcopy(original);modified["coding_index"]=99;modified["reasoning_effort"]="high"
        data["data"].append(modified)
    obj,_=source(mutate=mutate)
    view=obj.refresh()
    pools=view["categories"]["artificial-analysis:coding_index"]["pools"]
    assert not any(c.get("profile",{}).get("model")=="example/a" for group in pools.values() for c in group)


def test_refresh_failure_or_redirect_does_not_reveal_token():
    _,_,policies=fixtures()
    requests=[]
    def redirect(request):
        requests.append(request)
        return httpx.Response(302,headers={"Location":"https://outside.example/steal"})
    obj=OpenRouterPoolSource(httpx.Client(transport=httpx.MockTransport(redirect)),policies,api_key="SECRET")
    with pytest.raises(MonitorError,match="source_get_failed"):
        obj.refresh()
    assert len(requests)==1 and requests[0].url.host=="openrouter.ai"
    with pytest.raises(MonitorError,match="source_url_not_allowed"):
        obj.get("/chat/completions",[])


def test_source_body_bound_and_only_one_refresh_in_progress():
    import cloudyfan_monitor.pools as module
    obj,_=source()
    obj.lock.acquire()
    with pytest.raises(MonitorError,match="busy"):
        obj.refresh()
    obj.lock.release()
    old=module.LIMIT
    module.LIMIT=10
    try:
        with pytest.raises(MonitorError,match="too_large"):
            obj.refresh()
    finally:
        module.LIMIT=old


def test_prices_are_not_complete_cost_bounds():
    assert price_scenario({"prompt":"0","completion":"0"})=="0"
    assert price_scenario({"prompt":"0"}) is None
    assert price_scenario({"prompt":"NaN","completion":"0"}) is None
    assert price_scenario({"prompt":"0.001","completion":".002"})=="4.000"
    assert empty_view("fixture",NOW)["runtime_qualified"] is False
