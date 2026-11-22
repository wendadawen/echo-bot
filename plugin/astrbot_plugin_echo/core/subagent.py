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
    __slots__ = ("id", "kind", "prompt", "status", "result", "error",
                 "created", "finished")

    def __init__(self, kind, prompt, now=None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = str(kind or "task")
        self.prompt = str(prompt or "")
        self.status = "running"
        self.result = ""
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

    def submit(self, kind, prompt, runner):
        """runner(prompt) -> str；超过并发上限时返回 None，由调用方回一句忙。"""
        with self._lock:
            active = sum(1 for t in self._tasks.values() if t.status == "running")
            if active >= self.max_running:
                return None
            task = SubTask(kind, prompt)
            self._tasks[task.id] = task
            self._order.append(task.id)

        def work():
            try:
                result = runner(task.prompt)
                task.result = str(result or "").strip()[:800]
                task.status = "completed" if task.result else "failed"
                if not task.result:
                    task.error = "空结果"
            except Exception as exc:  # 后台线程不能把异常抛给主循环
                task.status = "failed"
                task.error = "{}: {}".format(type(exc).__name__, exc)[:200]
            finally:
                task.finished = time.time()
                self._trim()

        threading.Thread(target=work, name="echo-subagent", daemon=True).start()
        return task.id

    def _trim(self):
        with self._lock:
            finished = [tid for tid in self._order
                        if self._tasks.get(tid) and self._tasks[tid].status != "running"]
            for tid in finished[:max(0, len(finished) - self.keep)]:
                self._tasks.pop(tid, None)
            self._order = [tid for tid in self._order if tid in self._tasks]

    def poll(self):
        """取走已完成的结论（取一次就不再返回）。"""
        rows = []
        with self._lock:
            for task in list(self._tasks.values()):
                if task.status in ("completed", "failed") and task.id not in self._taken:
                    rows.append(task.as_dict())
                    self._taken.add(task.id)
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
