"""An external Ollama SDK customer; all service access is through HTTP.

This runner has no operator credential and never imports the server fixture.
The optional transport is only a test seam; the CLI always uses real HTTP.
"""
from importlib.metadata import version
import ipaddress
import re
import time
from urllib.parse import urlsplit

import httpx

ALIAS = "cloudyfan/mrc:simulation"


def question_requests(batch_id, question_numbers=tuple(range(1, 51))):
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,55}", batch_id):
        raise ValueError("Use a stable batch identifier of at most 56 simple characters")
    if (not isinstance(question_numbers, (tuple, list)) or not question_numbers
            or any(type(number) is not int or not 1 <= number <= 50 for number in question_numbers)
            or len(set(question_numbers)) != len(question_numbers)):
        raise ValueError("Choose distinct question numbers from 1 through 50")
    requests = []
    for number in question_numbers:
        prompt = (f"Simulation question {number:02d}. Return only the integer sum of "
                  f"{100 + number * 7} and {200 - number * 3}.")
        if number >= 46:
            prompt += " Public verification is unavailable for this request."
        requests.append({"question": number, "run_id": f"{batch_id}-q{number:02d}", "prompt": prompt})
    return requests


def local_origin(origin):
    try:
        parsed = urlsplit(origin)
        valid = (parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback
                 and parsed.username is None and parsed.password is None and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment and parsed.port is not None)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Customer simulation requires an explicit loopback HTTP origin and port")
    return f"{parsed.scheme}://{parsed.netloc}"


def run_customer50(origin, token, batch_id, *, question_numbers=tuple(range(1, 51)),
                   delay=.15, progress=None, transport=None):
    """Ask fifty independent public questions, retaining failed and unknown ones.

    Same batch + question yields the same idempotent run identifier. There are no
    automatic retries. A new batch creates fifty new tasks and synthetic fees.
    """
    origin = local_origin(origin)
    requests = question_requests(batch_id, question_numbers)
    if (not isinstance(token, str) or len(token) < 24 or type(delay) not in (float, int)
            or not 0 <= delay <= 1 or (progress is not None and not callable(progress))):
        raise ValueError("Explicit client credential and a bounded observation delay are required")
    try:
        from ollama import Client, ResponseError
    except ImportError:
        raise RuntimeError("Official ollama==0.6.3 is required for the customer simulation") from None
    report = {"schema_version": 1, "kind": "official-ollama-customer-http",
              "sdk": "ollama", "sdk_version": version("ollama"), "origin": origin,
              "alias": ALIAS, "batch_id": batch_id, "total": len(requests), "completed": 0,
              "model_upstream": "synthetic_mock_transport", "real_supplier_requests": 0,
              "outcomes": {"passed": 0, "failed": 0, "unverifiable": 0}, "questions": [],
              "observation_gap_seconds": delay}
    headers = {"Authorization": "Bearer " + token}
    options = {"trust_env": False, "follow_redirects": False,
               "timeout": httpx.Timeout(180, connect=5)}
    if transport is not None:
        options["transport"] = transport
    with httpx.Client(base_url=origin, headers=headers, **options) as customer:
        monitor = customer.get("/v1/monitor")
        if monitor.status_code != 200 or monitor.json().get("demo") is not True:
            raise RuntimeError("synthetic_service_required")
        aliases = customer.get("/api/tags")
        if (aliases.status_code != 200 or not any(isinstance(model, dict) and model.get("name") == ALIAS
                                                 for model in aliases.json().get("models", []))):
            raise RuntimeError("simulation_alias_not_registered")
        report["synthetic_service_confirmed"] = True
        for question in requests:
            code, sdk_answer = 200, None
            with Client(host=origin, headers={**headers, "X-CloudyFan-Run-ID": question["run_id"]},
                        **options) as ollama:
                try:
                    response = ollama.chat(model=ALIAS, messages=[{"role": "user", "content": question["prompt"]}],
                                           stream=False)
                    sdk_answer = response.message.content
                except ResponseError as error:
                    code = error.status_code
                    if code != 422:
                        raise RuntimeError("simulation_ollama_request_failed") from None
            # The SDK may discard MRC extension fields. Recover them from the
            # authenticated task API with exactly the same public run identity.
            observed = customer.get(f"/v1/tasks/{question['run_id']}/result")
            if observed.status_code != 200:
                raise RuntimeError("simulation_result_unavailable")
            result = observed.json()
            status = result.get("status")
            if status not in report["outcomes"] or (code == 200) != (status == "passed"):
                raise RuntimeError("simulation_protocol_result_disagrees")
            if code == 200 and sdk_answer != result.get("answer"):
                raise RuntimeError("simulation_answer_projection_disagrees")
            report["outcomes"][status] += 1
            report["questions"].append({**question, "ollama_http_status": code, "status": status,
                                         "answer": result.get("answer"), "result": result})
            report["completed"] += 1
            if progress is not None:
                progress({"completed": report["completed"], "total": report["total"],
                          "last_run_id": question["run_id"], "last_status": status,
                          "outcomes": dict(report["outcomes"])})
            if delay and report["completed"] < report["total"]:
                time.sleep(delay)
        billing = customer.get("/v1/billing")
        if billing.status_code != 200:
            raise RuntimeError("simulation_customer_billing_unavailable")
        report["customer_billing_before_server_reconciliation"] = billing.json()
    report["status"] = "completed"
    return report
