"""Session state derived from native Codex responses and notifications."""

from copy import deepcopy


def is_subagent(thread: dict) -> bool:
    return isinstance(thread.get("source"), dict) and "subAgent" in thread["source"]


def thread_time(thread: dict, sort_key: str = "recency_at") -> int:
    field = {"recency_at": "recencyAt", "updated_at": "updatedAt", "created_at": "createdAt"}[
        sort_key
    ]
    return thread.get(field) or thread.get("updatedAt") or thread.get("createdAt") or 0


class SessionState:
    def __init__(self):
        self.active_turns: dict[tuple[str, str], str] = {}
        self.observed_threads: set[tuple[str, str]] = set()
        self.resume_threads: set[tuple[str, str]] = set()
        self.recovering_turns: dict[tuple[str, str], str] = {}
        self.thread_details: dict[tuple[str, str], dict] = {}
        self.fresh_threads: dict[tuple[str, str], dict] = {}
        self.thread_settings: dict[tuple[str, str], dict] = {}
        self.token_usage: dict[tuple[str, str], dict] = {}
        self.goals: dict[tuple[str, str], dict | None] = {}
        self.goal_support: dict[tuple[str, str], bool] = {}
        self.compaction_ids: dict[tuple[str, str], set[str]] = {}
        self.compactions_loaded: set[tuple[str, str]] = set()
        self.message_starts: dict[tuple[str, str, str], int] = {}
        self.usage_bootstrapped: set[tuple[str, str]] = set()

    def apply(self, machine_id: str, method: str, params: dict) -> None:
        thread_id = params.get("threadId") or params.get("thread", {}).get("id")
        key = (machine_id, thread_id)
        # thread/started is broadcast to other connections as well. Only our
        # successful start/resume/fork response establishes an observation.
        if method == "turn/started":
            self.active_turns[key] = params["turn"]["id"]
        elif method in {"turn/completed", "thread/closed"}:
            self.active_turns.pop(key, None)
        if method in {"thread/closed", "thread/archived", "thread/deleted"}:
            self.observed_threads.discard(key)
            self.resume_threads.discard(key)
            self.recovering_turns.pop(key, None)
            fresh = self.fresh_threads.get(key)
            if method == "thread/archived" and fresh and not fresh["turns"]:
                fresh["archived"] = True
            else:
                self.fresh_threads.pop(key, None)
            self.active_turns.pop(key, None)
            self.thread_settings.pop(key, None)
            self.token_usage.pop(key, None)
            self.compaction_ids.pop(key, None)
            self.compactions_loaded.discard(key)
            self.usage_bootstrapped.discard(key)
        if method == "thread/unarchived" and key in self.fresh_threads:
            self.fresh_threads[key]["archived"] = False
        if method == "thread/reverted":
            self.fresh_threads.pop(key, None)
            self.active_turns.pop(key, None)
            self.token_usage.pop(key, None)
            self.compaction_ids.pop(key, None)
            self.compactions_loaded.discard(key)
            self.usage_bootstrapped.discard(key)
        if method == "thread/settings/updated":
            self.remember_settings(key, params["threadSettings"])
        elif method == "thread/goal/updated":
            self.goals[key] = deepcopy(params["goal"])
            self.goal_support[key] = True
        elif method == "thread/goal/cleared":
            self.goals[key] = None
        elif method == "thread/tokenUsage/updated":
            self.token_usage[key] = deepcopy(params["tokenUsage"])
        elif method == "item/completed" and params["item"]["type"] == "contextCompaction":
            self.compaction_ids.setdefault(key, set()).add(params["item"]["id"])
        elif method == "thread/started":
            self.remember_thread(machine_id, params["thread"])
            self.remember_settings(key, params["thread"], initial=True)
        elif method == "thread/name/updated" and key in self.fresh_threads:
            self.fresh_threads[key]["name"] = params.get("threadName") or params.get("name")
        if method == "thread/name/updated" and key in self.thread_details:
            self.thread_details[key]["name"] = params.get("threadName") or params.get("name")
        if key in self.thread_details:
            if method == "thread/status/changed":
                self.thread_details[key]["status"] = params["status"]
            elif method in {"turn/started", "turn/completed", "thread/closed", "thread/archived"}:
                self.thread_details[key]["status"] = {
                    "type": "active" if method == "turn/started" else "idle"
                }
            if method in {"thread/archived", "thread/unarchived"}:
                self.thread_details[key]["archived"] = method == "thread/archived"
            if method == "thread/deleted":
                self.thread_details.pop(key)
        if method == "thread/deleted":
            self.goals.pop(key, None)
            self.goal_support.pop(key, None)
        if key in self.fresh_threads and method in {"turn/started", "turn/completed"}:
            fresh = self.fresh_threads[key]
            turns = fresh["turns"]
            existing = next((turn for turn in turns if turn["id"] == params["turn"]["id"]), None)
            if existing:
                existing.update(deepcopy(params["turn"]))
            else:
                turns.append(deepcopy(params["turn"]))
            fresh["status"] = (
                {"type": "active", "activeFlags": []}
                if method == "turn/started"
                else {"type": "idle"}
            )
        if method == "turn/completed":
            self.remember_turns(machine_id, thread_id, [params["turn"]])
            self.fresh_threads.pop(key, None)
            self.recovering_turns.pop(key, None)

    def remember_thread(self, machine_id: str, thread: dict) -> None:
        details = self.thread_details.setdefault((machine_id, thread["id"]), {})
        # Native summaries can omit a saved name; explicit name updates clear it.
        if not thread.get("name") and details.get("name"):
            thread["name"] = details["name"]
        details.update({field: value for field, value in thread.items() if field != "turns"})

    def remember_turns(self, machine_id: str, thread_id: str, turns: list[dict]) -> None:
        for turn in turns:
            self.compaction_ids.setdefault((machine_id, thread_id), set()).update(
                item["id"]
                for item in turn.get("items", [])
                if item["type"] == "contextCompaction"
                and (turn.get("status") != "inProgress" or item.get("completedAtMs") is not None)
            )
            if turn.get("status") == "inProgress":
                self.active_turns[(machine_id, thread_id)] = turn["id"]
            elif self.active_turns.get((machine_id, thread_id)) == turn["id"]:
                self.active_turns.pop((machine_id, thread_id))

    def remember_settings(self, key: tuple[str, str], source: dict, initial: bool = False) -> None:
        settings = self.thread_settings.setdefault(key, {})
        fields = {
            "model": "model",
            "reasoningEffort": "reasoningEffort",
            "effort": "reasoningEffort",
            "serviceTier": "serviceTier",
            "collaborationMode": "collaborationMode",
            "summary": "summary",
            "approvalPolicy": "approvalPolicy",
            "sandbox": "sandbox",
            "sandboxPolicy": "sandbox",
        }
        for source_field, field in fields.items():
            if source_field in source and (not initial or field not in settings):
                settings[field] = deepcopy(source[source_field])

    def clear_machine(self, machine_id: str) -> None:
        self.goals = {key: value for key, value in self.goals.items() if key[0] != machine_id}
        self.goal_support = {
            key: value for key, value in self.goal_support.items() if key[0] != machine_id
        }
        for key in list(self.active_turns):
            if key[0] == machine_id:
                self.active_turns.pop(key)
        self.observed_threads = {key for key in self.observed_threads if key[0] != machine_id}
        self.fresh_threads = {
            key: value for key, value in self.fresh_threads.items() if key[0] != machine_id
        }
        self.thread_settings = {
            key: value for key, value in self.thread_settings.items() if key[0] != machine_id
        }
        self.token_usage = {
            key: value for key, value in self.token_usage.items() if key[0] != machine_id
        }
        self.compaction_ids = {
            key: value for key, value in self.compaction_ids.items() if key[0] != machine_id
        }
        self.compactions_loaded = {key for key in self.compactions_loaded if key[0] != machine_id}
        self.message_starts = {
            key: value for key, value in self.message_starts.items() if key[0] != machine_id
        }
        self.usage_bootstrapped = {key for key in self.usage_bootstrapped if key[0] != machine_id}

    def forget_machine(self, machine_id: str) -> None:
        self.clear_machine(machine_id)
        self.resume_threads = {key for key in self.resume_threads if key[0] != machine_id}
        self.recovering_turns = {
            key: turn for key, turn in self.recovering_turns.items() if key[0] != machine_id
        }
        self.thread_details = {
            key: thread for key, thread in self.thread_details.items() if key[0] != machine_id
        }

    def apply_result(self, machine_id: str, method: str, params: dict, result: dict) -> None:
        key = (machine_id, params.get("threadId"))
        if method in {"thread/goal/get", "thread/goal/set"}:
            self.goals[key] = deepcopy(result.get("goal"))
            self.goal_support[key] = True
        elif method == "thread/goal/clear":
            self.goals[key] = None
        if method == "thread/delete":
            self.apply(machine_id, "thread/deleted", params)
            return
        if method == "thread/list":
            for thread in result.get("data", []):
                thread["archived"] = bool(params.get("archived"))
                self.remember_thread(machine_id, thread)
        if method == "turn/start" and result.get("turn"):
            if params.get("summary") is not None:
                self.remember_settings(key, {"summary": params["summary"]})
            self.active_turns[(machine_id, params["threadId"])] = result["turn"]["id"]
            if key in self.fresh_threads:
                fresh = self.fresh_threads[key]
                if not any(turn["id"] == result["turn"]["id"] for turn in fresh["turns"]):
                    fresh["turns"].append(deepcopy(result["turn"]))
        if method == "thread/name/set":
            for threads in (self.fresh_threads, self.thread_details):
                if key in threads:
                    threads[key]["name"] = params["name"]
        if "thread" in result:
            thread = result["thread"]
            thread["archived"] = bool(
                thread.get("archived")
                or "archived_sessions" in (thread.get("path") or "").split("/")
            )
            self.remember_thread(machine_id, result["thread"])
            result_key = (machine_id, result["thread"]["id"])
            if method in {"thread/start", "thread/resume", "thread/fork"}:
                self.observed_threads.add(result_key)
                self.resume_threads.add(result_key)
                self.remember_settings(result_key, result)
                if summary := (params.get("config") or {}).get("model_reasoning_summary"):
                    self.remember_settings(result_key, {"summary": summary})
            self.remember_settings(result_key, result["thread"], initial=True)
            if method == "thread/start":
                self.fresh_threads[result_key] = deepcopy(result["thread"])
            full_history = (
                method == "thread/start"
                or method == "thread/read"
                and params.get("includeTurns")
                or method in {"thread/resume", "thread/fork"}
                and not params.get("excludeTurns", False)
                and not params.get("initialTurnsPage")
            )
            if full_history:
                self.compaction_ids[result_key] = set()
                self.compactions_loaded.add(result_key)
            self.remember_turns(
                machine_id, result["thread"]["id"], result["thread"].get("turns", [])
            )
        if method == "thread/turns/list":
            if result.get("data"):
                self.fresh_threads.pop(key, None)
            self.remember_turns(machine_id, params["threadId"], result.get("data", []))
        if method in {"thread/archive", "thread/unsubscribe"}:
            self.observed_threads.discard(key)
            self.resume_threads.discard(key)
            fresh = self.fresh_threads.get(key)
            if method == "thread/archive" and fresh and not fresh["turns"]:
                fresh["archived"] = True
            else:
                self.fresh_threads.pop(key, None)
            self.active_turns.pop(key, None)
            self.usage_bootstrapped.discard(key)
        if method == "thread/unarchive" and key in self.fresh_threads:
            self.fresh_threads[key]["archived"] = False
