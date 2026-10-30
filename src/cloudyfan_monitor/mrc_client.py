"""讓 Claude Code 等代理以一般客戶身分，把子任務交給 MRC 並讀回選模與費用。

只走既有客戶 HTTP API（/v1/tasks、/v1/tasks/<id>/result、/v1/monitor），
不匯入伺服器物件、不持有操作員權限，也不能指定模式或模型池；模式仍由操作員決定。
憑證只從環境變數或 --demo 讀取，不出現在命令列參數與輸出。
"""
import argparse
from datetime import datetime, timezone
import json
import os
import secrets
import sys

import httpx

from .customer_simulation import local_origin

TOKEN_ENV = "CLOUDYFAN_MRC_TOKEN"
ORIGIN_ENV = "CLOUDYFAN_MRC_ORIGIN"
DEFAULT_CATEGORY = "artificial-analysis:coding_index"
SYNTHETIC_NOTICE = "模擬上游：答案、模型與費用皆為合成資料，不代表真實模型能力或成本"


class MrcError(Exception):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


def new_run_id(now=None):
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    return f"cc-{stamp}-{secrets.token_hex(3)}"


class MrcClient:
    def __init__(self, origin, token, *, transport=None, timeout=180):
        if not isinstance(token, str) or len(token) < 24:
            raise MrcError("client_token_required")
        options = {"trust_env": False, "follow_redirects": False,
                   "timeout": httpx.Timeout(timeout, connect=5)}
        if transport is not None:
            options["transport"] = transport
        self.http = httpx.Client(base_url=local_origin(origin),
                                 headers={"Authorization": "Bearer " + token}, **options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.http.close()

    def _json(self, response):
        try:
            body = response.json()
        except ValueError:
            raise MrcError("non_json_response", response.status_code) from None
        if response.status_code != 200:
            error = body.get("error") if isinstance(body, dict) else None
            code = error.get("code") if isinstance(error, dict) else error
            raise MrcError(code if isinstance(code, str) else "request_failed", response.status_code)
        return body

    def monitor(self):
        return self._json(self.http.get("/v1/monitor"))

    def check(self):
        view = self.monitor()
        client = next(iter(view.get("clients") or []), {})
        categories = {name: sorted(mode for mode, info in modes.items() if info.get("contract_registered"))
                      for name, modes in (view.get("capabilities") or {}).get("categories", {}).items()}
        return {"status": "ready", "demo": view.get("demo") is True, "client": client.get("id"),
                "effective_mode": client.get("effective_mode"),
                "categories": {name: modes for name, modes in categories.items() if modes},
                **({"notice": SYNTHETIC_NOTICE} if view.get("demo") is True else {})}

    def ask(self, prompt, *, category=DEFAULT_CATEGORY, run_id=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise MrcError("prompt_required")
        run_id = run_id or new_run_id()
        self._json(self.http.post("/v1/tasks", json={"run_id": run_id, "category": category, "prompt": prompt}))
        return self.show(run_id)

    def result(self, run_id):
        return self._json(self.http.get(f"/v1/tasks/{run_id}/result"))

    def show(self, run_id, *, full=False):
        result, view = self.result(run_id), self.monitor()
        task = next((t for t in view.get("tasks", []) if t.get("run") == run_id), {})
        summary = summarize(result, task, demo=view.get("demo") is True)
        return {**summary, "result": result, "monitor_task": task} if full else summary


def summarize(result, task, *, demo):
    """把核心結果與 Monitor 任務紀錄整理成代理容易讀的摘要；不推測缺少的欄位。"""
    trace = task.get("trace") or {}
    path = [{"tier": step.get("tier"), "models": step.get("models"), "passed": step.get("passed")}
            for step in task.get("path") or []]
    decisions = [{key: decision.get(key) for key in ("stage", "trigger", "previous_tier", "reported_choice",
                                                     "selected_tier", "fallback", "fallback_reason", "cost_usd")}
                 for decision in trace.get("decisions") or []]
    summary = {"run_id": result.get("run_id"), "status": result.get("status"), "answer": result.get("answer"),
               "first_public_pass": result.get("first_public_pass"),
               "final_public_pass": result.get("final_public_pass"),
               "category": task.get("category"), "mode": task.get("mode"), "path": path,
               "jev_decisions": decisions, "costs": result.get("costs"),
               "routing_recommendation": result.get("routing_recommendation"),
               "wall_seconds": result.get("wall_seconds"), "demo": demo}
    if demo:
        summary["notice"] = SYNTHETIC_NOTICE
    return summary


def _token(args):
    if args.demo:
        from .demo import TOKENS
        return TOKENS["agent"]
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise MrcError("client_token_required")
    return token


def _prompt(args):
    if args.prompt is not None and args.prompt != "-":
        return args.prompt
    return sys.stdin.read(65536)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mrc-task", description=__doc__)
    parser.add_argument("--origin", default=os.environ.get(ORIGIN_ENV), help=f"loopback 位址，預設讀 {ORIGIN_ENV}")
    parser.add_argument("--demo", action="store_true",
                        help="使用 --legacy-demo 預覽的公開示範客戶憑證；僅限服務自報為示範模式")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="確認服務、客戶身分與可用分類")
    ask = commands.add_parser("ask", help="送出一件任務並回傳結果摘要")
    ask.add_argument("--prompt", help="題目文字；省略或 - 時從 stdin 讀取")
    ask.add_argument("--category", default=DEFAULT_CATEGORY)
    ask.add_argument("--run-id", help="相同 run ID 與內容重送只讀回原結果，不重新派送")
    show = commands.add_parser("show", help="查看既有任務")
    show.add_argument("run_id")
    show.add_argument("--full", action="store_true", help="附上原始結果與 Monitor 任務紀錄")
    args = parser.parse_args(argv)
    try:
        if not args.origin:
            raise MrcError("origin_required")
        with MrcClient(args.origin, _token(args)) as client:
            if args.demo and client.monitor().get("demo") is not True:
                raise MrcError("demo_token_requires_demo_service")
            if args.command == "check":
                output = client.check()
            elif args.command == "ask":
                output = client.ask(_prompt(args), category=args.category, run_id=args.run_id)
            else:
                output = client.show(args.run_id, full=args.full)
    except MrcError as error:
        print(json.dumps({"status": "error", "code": error.code, "http_status": error.http_status}))
        return 2
    except ValueError:
        print(json.dumps({"status": "error", "code": "loopback_origin_with_port_required"}))
        return 2
    except httpx.HTTPError as error:
        print(json.dumps({"status": "error", "code": "service_unreachable", "detail": type(error).__name__}))
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output.get("status") in ("ready", "passed") else 1


if __name__ == "__main__":
    sys.exit(main())
