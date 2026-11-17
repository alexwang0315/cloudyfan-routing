"""契約經驗庫（ADR 0036、#75）：只做儲存與累積，不接進 Core 或題目（#76 負責）。

狀態全部由「結構化觀察紀錄」推導：每題一筆，只含內部編號、代碼與成績，
不含題目或答案原文。事實分客戶端私有層與共用層。
"""
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import secrets

from .routing import _reject_private

SCHEMA = "cloudyfan-contract-experience-v1"
CONFIRMATIONS = 3
RETENTION_DAYS = 365  # 比照中繼資料至少保存 365 天（第 9 點）
# 來源只收這三種結構化資料（ADR 0036 第 2 點）；顧問建議只收固定 JSON 的代碼。
SOURCE_KINDS = ("error_fingerprint", "check_code", "advisor_advice")
OBSERVATION_FIELDS = ("task_id", "client_id", "scope", "settings", "created_at",
                      "supports", "contradicts", "outcomes")
OUTCOME_FIELDS = ("role", "model_version", "effort", "provider", "verdict", "cost_usd", "pool_snapshot")
VERDICTS = ("passed", "failed", "unverifiable")
ROLES = ("solver", "advisor")
ITEM_FIELDS = ("kind", "code", "detail")
# 只接受不含空白的代碼型字串，擋掉題目或答案裡的自由文字指令（第 6 點）。
# 「/」供模型版本（如 vendor/model-v1）使用（#76）。
TOKEN = re.compile(r"^[A-Za-z0-9_.:/\-]{1,120}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _token(value, what):
    if not isinstance(value, str) or not TOKEN.match(value):
        raise ValueError(f"{what} 必須是代碼型字串")
    return value


def _item(item):
    if not isinstance(item, Mapping) or set(item) - set(ITEM_FIELDS):
        raise ValueError("經驗來源只收 kind／code／detail 三個欄位")
    if item.get("kind") not in SOURCE_KINDS:
        raise ValueError("不支援的經驗來源種類")
    normalized = {"kind": item["kind"], "code": _token(item.get("code"), "code")}
    detail = item.get("detail")
    if detail is not None:
        if not isinstance(detail, Mapping) or not detail:
            raise ValueError("detail 必須是非空物件")
        normalized["detail"] = {}
        for key, values in detail.items():
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError("detail 值必須是非空清單")
            normalized["detail"][_token(key, "detail 鍵")] = sorted({_token(v, "detail 值") for v in values})
    return normalized


def _outcome(outcome):
    """一次派送的成績；只收精確設定、結果與成本，不收答案。"""
    if not isinstance(outcome, Mapping) or set(outcome) - set(OUTCOME_FIELDS):
        raise ValueError("派送成績含不允許的欄位")
    role = outcome.get("role", "solver")
    if role not in ROLES or outcome.get("verdict") not in VERDICTS:
        raise ValueError("role 或 verdict 不合法")
    try:
        cost = Decimal(str(outcome.get("cost_usd")))
    except InvalidOperation:
        raise ValueError("cost_usd 必須是數字") from None
    if not cost.is_finite() or cost < 0:
        raise ValueError("cost_usd 必須是非負數")
    result = {"role": role, "model_version": _token(outcome.get("model_version"), "model_version"),
              "effort": _token(outcome.get("effort"), "effort"),
              "provider": _token(outcome.get("provider"), "provider"),
              "verdict": outcome["verdict"], "cost_usd": str(cost)}
    if "pool_snapshot" in outcome:
        # 選用（#76）：成績綁定當時的模型池快照，換池後不沿用。
        result["pool_snapshot"] = _token(outcome["pool_snapshot"], "pool_snapshot")
    return result


def _normalize(observation):
    if not isinstance(observation, Mapping):
        raise ValueError("觀察紀錄必須是物件")
    _reject_private(observation)
    extra = set(observation) - set(OBSERVATION_FIELDS)
    if extra:
        raise ValueError(f"觀察紀錄含不允許的欄位：{sorted(extra)}")
    settings = observation.get("settings")
    if not isinstance(settings, Mapping) or set(settings) != {"contribute", "use"} \
            or not all(isinstance(v, bool) for v in settings.values()):
        raise ValueError("settings 必須是建題時凍結的 contribute／use 設定")
    created = observation.get("created_at")
    if not isinstance(created, str) or not DATE.match(created):
        raise ValueError("created_at 必須是 YYYY-MM-DD")
    date.fromisoformat(created)  # 不存在的日期會丟 ValueError
    result = {"task_id": _token(observation.get("task_id"), "task_id"),
              "client_id": _token(observation.get("client_id"), "client_id"),
              "scope": _token(observation.get("scope"), "scope"),
              "settings": dict(settings), "created_at": created}
    for side in ("supports", "contradicts"):
        items = observation.get(side, [])
        if not isinstance(items, (list, tuple)):
            raise ValueError(f"{side} 必須是清單")
        result[side] = sorted((_item(item) for item in items), key=_encoded)
    outcomes = observation.get("outcomes", [])
    if not isinstance(outcomes, (list, tuple)):
        raise ValueError("outcomes 必須是清單")
    result["outcomes"] = [_outcome(item) for item in outcomes]
    return result


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _key(item):
    return item["kind"], item["code"]


def fact_id(scope, kind, code):
    """事實的內部編號；只由契約範圍與代碼決定，不含客戶端資訊。"""
    return "fx-" + _hash([scope, kind, code])[:16]


SHINGLE = 12  # 題目原文以 12 字元片段比對，擋下逐字搬運


def _shingles(text):
    text = " ".join(text.lower().split())
    if len(text) <= SHINGLE:
        return {text} if text else set()
    return {text[i:i + SHINGLE] for i in range(len(text) - SHINGLE + 1)}


def _identifier_parts(value):
    value = value.lower()
    return {value, *value.split(".")} - {""}


def _statement_identifiers(statement):
    lower = statement.lower()
    return set(re.findall(r"[a-z0-9_.\-]+", lower)) | set(re.findall(r"[a-z0-9_]+", lower))


class ExperienceStore:
    """契約經驗庫。rewriter 是可注入的「開源顧問改寫」步驟：輸入只有範圍與代碼，
    回傳通用敘述；改寫結果一律再經程式檢查才可進共用層。"""

    def __init__(self, *, rewriter, salt=None):
        self._rewriter = rewriter
        self._salt = salt or secrets.token_hex(8)
        self._version = 0
        self._observations = {}
        self._rewrites = {}
        self._clients = {}
        self._withdrawn_facts = {}
        self._withdrawn_tasks = set()

    # ---- 客戶端設定（第 8 點）----

    def set_client_settings(self, client_id, *, contribute=None, use=None):
        """「貢獻」「使用」可分別關閉。關閉貢獻立即扣除該客戶端所有題目的共用支持。"""
        current = self.freeze_settings(_token(client_id, "client_id"))
        for name, value in (("contribute", contribute), ("use", use)):
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError(f"{name} 必須是布林值")
                current[name] = value
        if current != self.freeze_settings(client_id):
            self._clients[client_id] = current
            self._version += 1

    def freeze_settings(self, client_id):
        """建題時呼叫，把當下設定複製進該題；新客戶端預設兩者都開。"""
        return dict(self._clients.get(client_id, {"contribute": True, "use": True}))

    # ---- 寫入 ----

    def record(self, observation, *, sensitive=None):
        """記錄一題的結構化觀察。sensitive 是只用來做去識別檢查的題目原文與識別名稱，
        只以加鹽雜湊保存，原文不落地。"""
        observation = _normalize(observation)
        if observation["task_id"] in self._withdrawn_tasks:
            raise ValueError("此題已被要求刪除，不得再記錄")
        observation["blocked"] = self._blocked(observation, sensitive)
        task_id = observation["task_id"]
        previous = self._observations.get(task_id)
        if previous is not None:
            if _hash(previous) != _hash(observation):
                raise ValueError("同一題已記錄過不同的觀察內容")
            return
        self._observations[task_id] = observation
        self._version += 1
        self._rewrite_newly_eligible(observation["scope"])

    def _salted(self, value):
        return hashlib.sha256(f"{self._salt}:{value}".encode()).hexdigest()[:24]

    def _blocked(self, observation, sensitive):
        identifiers, prompt = set(), ""
        if sensitive is not None:
            if not isinstance(sensitive, Mapping) or set(sensitive) - {"identifiers", "prompt"}:
                raise ValueError("sensitive 只收 identifiers 與 prompt")
            for value in sensitive.get("identifiers", ()):
                if not isinstance(value, str):
                    raise ValueError("identifiers 必須是字串")
                identifiers |= _identifier_parts(value)
            prompt = sensitive.get("prompt") or ""
            if not isinstance(prompt, str):
                raise ValueError("prompt 必須是字串")
        for item in observation["supports"] + observation["contradicts"]:
            for key, values in item.get("detail", {}).items():
                for value in values:
                    identifiers |= _identifier_parts(value)
        return {"identifiers": sorted(self._salted(v) for v in identifiers),
                "prompt": sorted(self._salted(v) for v in _shingles(prompt))}

    def _rewrite_newly_eligible(self, scope):
        """新達共用門檻的事實才呼叫顧問改寫。未通過去識別檢查的改寫不保存原文，
        只記下當時的支持集合；支持集合改變後才再試，避免每次寫入都花一次改寫。"""
        for (kind, code), fact in self._shared_candidates(scope).items():
            fid, tasks = fact_id(scope, kind, code), sorted(fact["tasks"])
            previous = self._rewrites.get(fid)
            if previous is not None and (previous["status"] == "accepted"
                                         or previous["support_sha256"] == _hash(tasks)):
                continue
            statement = self._rewriter({"scope": scope, "kind": kind, "code": code})
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError("顧問改寫必須回傳非空字串")
            if self._deidentified(statement, tasks):
                self._rewrites[fid] = {"status": "accepted", "statement": statement}
            else:
                self._rewrites[fid] = {"status": "rejected", "support_sha256": _hash(tasks)}

    # ---- 推導 ----

    def _contributing(self, scope):
        return [obs for obs in self._observations.values()
                if obs["scope"] == scope and obs["settings"]["contribute"]
                and self.freeze_settings(obs["client_id"])["contribute"]]

    def _tally(self, scope, observations, *, shared):
        """把觀察紀錄彙整成事實；只回傳達門檻、無反例、未撤回的事實。"""
        facts, contradicted = {}, set()
        for obs in sorted(observations, key=lambda o: (o["created_at"], o["task_id"])):
            for item in obs["supports"]:
                if shared and "detail" in item:
                    continue  # 帶客戶專屬內容的事實只留在私有層
                fact = facts.setdefault(_key(item), {"tasks": set(), "clients": set(), "detail": {},
                                                     "first_seen": obs["created_at"]})
                fact["tasks"].add(obs["task_id"])
                fact["clients"].add(obs["client_id"])
                fact["last_supported"] = obs["created_at"]
                for name, values in item.get("detail", {}).items():
                    fact["detail"][name] = sorted(set(fact["detail"].get(name, ())) | set(values))
            contradicted.update(_key(item) for item in obs["contradicts"])
        live = {}
        for (kind, code), fact in sorted(facts.items()):
            enough = len(fact["tasks"]) >= CONFIRMATIONS and (
                not shared or len(fact["clients"]) >= CONFIRMATIONS)
            if enough and (kind, code) not in contradicted \
                    and fact_id(scope, kind, code) not in self._withdrawn_facts:
                live[(kind, code)] = fact
        return live

    def _shared_candidates(self, scope):
        """共用層：≥ 3 題、≥ 3 個不同客戶端支持、無反例、不帶客戶專屬 detail。"""
        return self._tally(scope, self._contributing(scope), shared=True)

    def _deidentified(self, statement, source_tasks):
        """程式檢查：改寫結果不得含來源題目的原文片段、識別名稱、客戶端或題目編號。"""
        words = _statement_identifiers(statement)
        salted_words = {self._salted(w) for w in words}
        salted_shingles = {self._salted(v) for v in _shingles(statement)}
        for task_id in source_tasks:
            obs = self._observations[task_id]
            if {obs["client_id"].lower(), obs["task_id"].lower()} & words:
                return False
            if salted_words & set(obs["blocked"]["identifiers"]):
                return False
            if salted_shingles & set(obs["blocked"]["prompt"]):
                return False
        return True

    @staticmethod
    def _retention(fact):
        last = date.fromisoformat(fact["last_supported"])
        return {"first_seen": fact["first_seen"], "last_supported": fact["last_supported"],
                "retain_until": (last + timedelta(days=RETENTION_DAYS)).isoformat()}

    def facts_for(self, client_id, scope, *, settings=None):
        """回傳此客戶端在此範圍可用的私有事實與共用事實；settings 傳該題凍結的設定。
        共用事實不帶來源編號，其他客戶端看不到來源。"""
        settings = self.freeze_settings(client_id) if settings is None else settings
        own = [obs for obs in self._observations.values()
               if obs["client_id"] == client_id and obs["scope"] == scope]
        private = []
        for (kind, code), fact in self._tally(scope, own, shared=False).items():
            entry = {"fact_id": fact_id(scope, kind, code), "kind": kind, "code": code}
            if fact["detail"]:
                entry["detail"] = fact["detail"]
            private.append({**entry, **self._retention(fact)})
        shared = []
        candidates = self._shared_candidates(scope) if settings["use"] else {}
        for (kind, code), fact in candidates.items():
            fid = fact_id(scope, kind, code)
            statement = self._rewrites.get(fid, {}).get("statement")
            # 每次讀取都重新檢查，後來加入的來源若帶新的識別名稱也會擋下。
            if statement is not None and self._deidentified(statement, fact["tasks"]):
                shared.append({"fact_id": fid, "kind": kind, "code": code, "statement": statement,
                               **self._retention(fact)})
        return {"private": private, "shared": shared}

    # ---- 撤回與保存期限（第 7、9 點）----

    def withdraw_fact(self, fid, *, reason):
        """下架一條事實（客戶端要求或人工判定）；之後再有支持也不恢復。"""
        self._withdrawn_facts[_token(fid, "fact_id")] = _token(reason, "reason")
        self._rewrites.pop(fid, None)
        self._version += 1

    def withdraw_task(self, task_id):
        """來源題目被要求刪除：移除該題觀察，相關事實依剩餘支持重算；之後不得再記錄。"""
        self._observations.pop(_token(task_id, "task_id"), None)
        self._withdrawn_tasks.add(task_id)
        self._version += 1

    def purge_expired(self, today):
        """移除超過 365 天的觀察；仍支持有效事實且該事實在 365 天內有新支持者延長保存。"""
        today = date.fromisoformat(today)
        keep_until = defaultdict(lambda: date.min)
        scopes = {obs["scope"] for obs in self._observations.values()}
        for scope in scopes:
            for layer in self._live_layers(scope):
                for fact in layer.values():
                    until = date.fromisoformat(self._retention(fact)["retain_until"])
                    for task in fact["tasks"]:
                        keep_until[task] = max(keep_until[task], until)
        expired = [task for task, obs in self._observations.items()
                   if date.fromisoformat(obs["created_at"]) + timedelta(days=RETENTION_DAYS) < today
                   and keep_until[task] < today]
        for task in expired:
            del self._observations[task]
        if expired:
            self._version += 1
        return sorted(expired)

    def _live_layers(self, scope):
        yield self._shared_candidates(scope)
        clients = {obs["client_id"] for obs in self._observations.values() if obs["scope"] == scope}
        for client in sorted(clients):
            own = [obs for obs in self._observations.values()
                   if obs["client_id"] == client and obs["scope"] == scope]
            yield self._tally(scope, own, shared=False)

    def is_withdrawn(self, task_id):
        """此題是否已被要求刪除（之後不得再記錄）。"""
        return task_id in self._withdrawn_tasks

    def model_stats(self, scope, *, client_id=None, pool_snapshot=None):
        """依精確角色／模型版本／強度／供應商統計。給 client_id 時是該客戶端私有成績，
        否則是貢獻中客戶端的共用成績。unverifiable 另計，不算入通過率。
        給 pool_snapshot 時只算綁定該模型池快照的成績（#76，換池不沿用）。"""
        if client_id is None:
            observations = self._contributing(scope)
        else:
            observations = [obs for obs in self._observations.values()
                            if obs["scope"] == scope and obs["client_id"] == client_id]
        groups = defaultdict(list)
        for obs in observations:
            for outcome in obs["outcomes"]:
                if pool_snapshot is not None and outcome.get("pool_snapshot") != pool_snapshot:
                    continue
                key = tuple(outcome[name] for name in OUTCOME_FIELDS[:4])
                groups[key].append(outcome)
        stats = []
        for key in sorted(groups):
            rows = groups[key]
            passed = sum(row["verdict"] == "passed" for row in rows)
            failed = sum(row["verdict"] == "failed" for row in rows)
            verified = passed + failed
            mean = sum(Decimal(row["cost_usd"]) for row in rows) / len(rows)
            stats.append({**dict(zip(OUTCOME_FIELDS[:4], key)), "dispatches": len(rows),
                          "verified": verified, "passed": passed,
                          "unverifiable": len(rows) - verified,
                          "pass_rate": str(Decimal(passed) / verified) if verified else None,
                          "mean_cost_usd": str(mean.normalize())})
        return stats

    # ---- 版本與完整性（第 7 點）----

    def export(self):
        body = {"schema": SCHEMA, "version": self._version, "salt": self._salt,
                "observations": [self._observations[k] for k in sorted(self._observations)],
                "rewrites": dict(sorted(self._rewrites.items())),
                "clients": dict(sorted(self._clients.items())),
                "withdrawn_facts": dict(sorted(self._withdrawn_facts.items())),
                "withdrawn_tasks": sorted(self._withdrawn_tasks)}
        return {"body": body, "sha256": _hash(body)}

    @classmethod
    def load(cls, snapshot, *, rewriter):
        """讀回快照；雜湊不符、格式不對或任何觀察紀錄不合規則都拒絕。"""
        if not isinstance(snapshot, Mapping) or set(snapshot) != {"body", "sha256"}:
            raise ValueError("經驗庫快照格式不對")
        body = snapshot["body"]
        if not isinstance(body, Mapping) or snapshot["sha256"] != _hash(body):
            raise ValueError("經驗庫完整性雜湊不符")
        if body.get("schema") != SCHEMA:
            raise ValueError("經驗庫 schema 不符")
        store = cls(rewriter=rewriter, salt=body["salt"])
        for observation in body["observations"]:
            stored = dict(observation)
            blocked = stored.pop("blocked")
            if _normalize(stored) != stored:
                raise ValueError("經驗庫內含不合規則的觀察紀錄")
            store._observations[stored["task_id"]] = {**stored, "blocked": blocked}
        store._rewrites = {key: dict(value) for key, value in body["rewrites"].items()}
        store._clients = {key: dict(value) for key, value in body["clients"].items()}
        store._withdrawn_facts = dict(body["withdrawn_facts"])
        store._withdrawn_tasks = set(body["withdrawn_tasks"])
        store._version = body["version"]
        if store.export() != snapshot:
            raise ValueError("經驗庫快照內容不一致")
        return store

    def save(self, path):
        path = Path(path)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(_encoded(self.export()))
        os.replace(temporary, path)

    @classmethod
    def open(cls, path, *, rewriter):
        return cls.load(json.loads(Path(path).read_bytes()), rewriter=rewriter)

    @staticmethod
    @contextmanager
    def _locked(path):
        """以旁邊的 ``<檔名>.lock`` 取得排他檔案鎖（fcntl.flock），跨程序與跨執行緒都會互斥。"""
        path = Path(path)
        with open(path.with_name(path.name + ".lock"), "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def update(cls, path, fn, *, rewriter, initial=None):
        """多個程序共用同一個經驗庫檔案時唯一安全的寫法：持檔案鎖「重讀–修改–原子存檔」。

        持鎖期間從檔案重讀最新內容（檔案不存在時用 ``initial``，否則新建空庫），
        呼叫 ``fn(store)`` 修改，再以 ``save``（暫存檔＋``os.replace``）原子寫回。
        回傳 ``(store, fn 的回傳值)``；``store`` 是寫回後的最新內容。
        直接對記憶體中的經驗庫 ``record`` 後再 ``save`` 會覆蓋其他程序同時寫入的紀錄。
        """
        path = Path(path)
        with cls._locked(path):
            if path.exists():
                store = cls.open(path, rewriter=rewriter)
            else:
                store = initial if initial is not None else cls(rewriter=rewriter)
            value = fn(store)
            store.save(path)
        return store, value

    def _adopt(self, other):
        """把記憶體中的狀態換成 ``other``（檔案最新內容）；改寫函式保留自己的。"""
        rewriter = self._rewriter
        self.__dict__.update(other.__dict__)
        self._rewriter = rewriter
