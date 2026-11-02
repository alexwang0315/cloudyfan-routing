"""模擬試問：操作員在 Monitor 送一題，看它走過真的 MRC 流程；模型仍是 MockTransport。

題目經 Gateway.create 進入同一套 TaskService／BudgetedCore／Core，留下一般任務紀錄。
模擬上游不讀題目內容：情境決定每個模型池通過與否，答案固定，不代表回答了題目。
"""
import secrets

from .demo import CATEGORY, SCENARIO
from .store import MonitorError

CLIENT = "studio"
NOTICE = "模擬上游：情境由你選擇，答案、模型與費用皆為合成資料，不代表真實模型會怎麼回答或選擇"
MAX_PROMPT_BYTES = 32768
TERMINAL = ("passed", "failed", "unverifiable")
SCENARIOS = {
    "first_pass": {"label": "開源池第一次就通過", "fail": frozenset(), "low_mid": "mid"},
    "low_pass": {"label": "開源池未過，Jev 選低分池後通過", "fail": frozenset({"open_source"}), "low_mid": "low"},
    "mid_pass": {"label": "開源池未過，Jev 選中分池後通過", "fail": frozenset({"open_source"}), "low_mid": "mid"},
    "high_pass": {"label": "連續未過，升到高分池才通過", "fail": frozenset({"open_source", "mid"}), "low_mid": "mid"},
    "all_fail": {"label": "三次作答都未通過", "fail": frozenset({"open_source", "low", "mid", "high"}), "low_mid": "mid"},
}


class Playground:
    def __init__(self, gateway):
        if gateway.demo is not True:
            raise ValueError("Playground requires a synthetic gateway")
        self.gateway = gateway

    def descriptor(self):
        client = next((c for c in self.gateway.store.dashboard(CLIENT)["clients"] if c["id"] == CLIENT), {})
        return {"demo": True, "notice": NOTICE, "client": CLIENT, "category": CATEGORY,
                "effective_mode": client.get("effective_mode"),
                "scenarios": [{"id": key, "label": value["label"]} for key, value in SCENARIOS.items()]}

    def ask(self, prompt, scenario):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > MAX_PROMPT_BYTES:
            raise MonitorError("invalid_text_prompt")
        if not isinstance(scenario, str) or scenario not in SCENARIOS:
            raise MonitorError("playground_scenario_not_supported")
        run = "playground-" + secrets.token_hex(6)
        self.gateway.store.touch_client(CLIENT)
        token = SCENARIO.set(SCENARIOS[scenario])
        try:
            state = self.gateway.create(CLIENT, {"run_id": run, "category": CATEGORY, "prompt": prompt})
        finally:
            SCENARIO.reset(token)
        status = state.get("status")
        result = self.gateway.service.result(CLIENT, run, refresh=False) if status in TERMINAL else {}
        task = next((t for t in self.gateway.snapshot()["tasks"] if t["client"] == CLIENT and t["run"] == run), {})
        return {"demo": True, "notice": NOTICE, "run_id": run, "status": status, "scenario": scenario,
                "answer": result.get("answer"), "first_public_pass": result.get("first_public_pass"),
                "final_public_pass": result.get("final_public_pass"), "costs": result.get("costs"),
                "wall_seconds": result.get("wall_seconds"), "task": task}
