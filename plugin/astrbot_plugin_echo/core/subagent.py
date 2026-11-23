"""后台子代理：把耗时的活儿丢到后台，聊天继续。

对齐 MaiBot `src/maisaka/subagent_tasks.py`：提交后立即返回任务号，
最多同时跑 3 个，完成后把结论留在队列里等主循环取；保留最近 20 条。
执行器由调用方注入（复用同一个模型，独立上下文），本模块不管模型细节。
只依赖标准库。
"""

import threading
import time
import uuid

MAX_RUNNING = 3
KEEP_FINISHED = 20


class SubTask:
    __slots__ = ("id", "kind", "prompt", "status", "reason", "result",
                 "partial", "files", "error", "created", "finished")

    def __init__(self, kind, prompt, now=None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = str(kind or "task")
        self.prompt = str(prompt or "")
        self.status = "running"
        self.reason = ""
        self.result = ""
        self.partial = ""
        self.files = []
        self.error = ""
        self.created = time.time() if now is None else now
        self.finished = 0.0

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


class SubAgentManager:
    def __init__(self, max_running=MAX_RUNNING, keep=KEEP_FINISHED):
        self.max_running = max(1, int(max_running))
        self.keep = max(4, int(keep))
        self._lock = threading.Lock()
        self._tasks = {}
        self._order = []
        self._taken = set()

    def running(self):
        with self._lock:
            return [task for task in self._tasks.values() if task.status == "running"]

    def submit(self, kind, prompt, runner, on_complete=None, on_register=None):
        """runner(prompt) -> str/dict；超过并发上限时返回 None，由调用方回一句忙。"""
        with self._lock:
            active = sum(1 for t in self._tasks.values() if t.status == "running")
            if active >= self.max_running:
                return None
            task = SubTask(kind, prompt)
            self._tasks[task.id] = task
            self._order.append(task.id)
        if callable(on_register):
            try:
                on_register(task.id)
            except Exception:
                pass

        def work():
            result = ""
            partial = ""
            files = []
            error = ""
            status = "failed"
            reason = "error"
            try:
                out = runner(task.prompt)
                if isinstance(out, dict):
                    outcome = str(out.get("status") or "").strip().lower()
                    result = str(out.get("text") or "").strip()[:1200]
                    partial = str(out.get("partial") or "").strip()[:400]
                    raw_files = out.get("files") or []
                    files = [f for f in raw_files if isinstance(f, dict) and f.get("path")]
                    raw_err = str(out.get("error") or "").strip()[:200]
                    if outcome == "timeout":
                        status, reason, error = "failed", "timeout", (raw_err or "执行超时")
                    elif outcome == "error":
                        status, reason, error = "failed", "error", (raw_err or "执行失败")
                    elif result or files:
                        status, reason, error = "completed", "ok", ""
                    else:
                        status, reason, error = "failed", "empty", (raw_err or "空结果")
                else:
                    result = str(out or "").strip()[:1200]
                    if result:
                        status, reason, error = "completed", "ok", ""
                    else:
                        status, reason, error = "failed", "empty", "空结果"
            except Exception as exc:  # 后台线程不能把异常抛给主循环
                status = "failed"
                reason = "error"
                error = "{}: {}".format(type(exc).__name__, exc)[:200]
            payload = None
            with self._lock:
                if task.status == "stopped":
                    return
                task.result = result
                task.partial = partial
                task.files = files
                task.error = error
                task.reason = reason
                task.finished = time.time()
                payload = dict(task.as_dict(), status=status)
            if callable(on_complete) and payload is not None:
                try:
                    on_complete(payload)
                except Exception:
                    pass
            with self._lock:
                if task.status == "stopped":
                    return
                task.status = status
            self._trim()

        threading.Thread(target=work, name="echo-subagent", daemon=True).start()
        return task.id

    def _trim(self):
        with self._lock:
            # 只淘汰「已经 poll 取走」且已结束的旧任务，避免积压未取走的结论被提前丢弃。
            finished_taken = [
                tid for tid in self._order
                if tid in self._taken
                and self._tasks.get(tid)
                and self._tasks[tid].status != "running"
            ]
            for tid in finished_taken[:max(0, len(finished_taken) - self.keep)]:
                self._tasks.pop(tid, None)
                self._taken.discard(tid)
            self._order = [tid for tid in self._order if tid in self._tasks]

    def poll(self):
        """取走已完成的结论（取一次就不再返回）。"""
        rows = []
        with self._lock:
            for task in list(self._tasks.values()):
                if task.status in ("completed", "failed") and task.id not in self._taken:
                    rows.append(task.as_dict())
                    self._taken.add(task.id)
        self._trim()
        return rows

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None or task.status != "running":
                return False
            task.status = "stopped"
            task.finished = time.time()
            return True

    def get(self, task_id):
        with self._lock:
            task = self._tasks.get(str(task_id))
            return task.as_dict() if task else None

    def summary(self):
        with self._lock:
            rows = list(self._tasks.values())
        return {
            "running": sum(1 for t in rows if t.status == "running"),
            "completed": sum(1 for t in rows if t.status == "completed"),
            "failed": sum(1 for t in rows if t.status == "failed"),
            "kept": len(rows),
        }
