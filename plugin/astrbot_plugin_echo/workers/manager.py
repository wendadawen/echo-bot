"""后台任务管理器：单群单任务去重、实时进度查询、随时喊停（直接强杀进程组）、重载持久化交付。"""

from __future__ import annotations

import collections
import re
import threading
import time
import uuid
from typing import Any, Optional

from ..domain import (
    HarnessBackend,
    LLMClient,
    MemoryRepository,
    PendingDelivery,
    TaskRequest,
    TaskResult,
    TaskStatusInfo,
    clip_text,
)
from .presenter import (
    DeliveryPresenter,
    _describe_files,
    _format_file_size,
    _split_code_blocks,
    filter_valid_files,
    rewrite_success,
)
from .sandbox import SandboxRunner, cleanup_file_entry


class _RunningJob:
    __slots__ = ("task_id", "req", "started_at", "cancel_event", "progress", "done")

    def __init__(self, task_id: str, req: TaskRequest, started_at: float):
        self.task_id = task_id
        self.req = req
        self.started_at = started_at
        self.cancel_event = threading.Event()
        self.progress = "已启动，正在准备…"
        self.done = False


class TaskManager:
    """后台任务生命周期管理器：单群并发去重、查进度、强杀喊停、重载持久化投递。"""

    def __init__(
        self,
        runner: SandboxRunner,
        backend: HarnessBackend,
        repo: Optional[MemoryRepository] = None,
        extra_secrets_fn: Optional[Any] = None,
    ):
        self._runner = runner
        self._backend = backend
        self._repo = repo
        self._extra_secrets_fn = extra_secrets_fn
        self._lock = threading.Lock()
        self._running: dict[str, _RunningJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._completed: collections.OrderedDict[str, PendingDelivery] = collections.OrderedDict()
        self._inflight_deliveries: set[str] = set()
        self._delivered_ids: collections.OrderedDict[str, float] = collections.OrderedDict()

    @property
    def outbox_root(self) -> str:
        return self._runner.outbox_root

    @property
    def timeout(self) -> int:
        return self._runner.timeout

    def available(self) -> bool:
        return self._runner.available(self._backend)

    def _secrets(self) -> tuple[str, ...]:
        out: list[str] = []
        if callable(self._extra_secrets_fn):
            try:
                out.extend(self._extra_secrets_fn() or ())
            except Exception:
                pass
        api_key = getattr(self._backend, "api_key", "")
        if api_key:
            out.append(str(api_key))
        return tuple(out)

    @staticmethod
    def format_start_notice(prompt: str, timeout: int, ack_text: str = "") -> str:
        summary = re.sub(r"\s+", " ", str(prompt or "")).strip()
        if len(summary) > 24:
            summary = summary[:24] + "…"
        notice = (
            "〔系统｜已启动后台工具：{} · 限时 {}s〕".format(summary, int(timeout))
            if summary
            else "〔系统｜已启动后台工具 · 限时 {}s〕".format(int(timeout))
        )
        ack = re.sub(r"\s+", " ", str(ack_text or "")).strip()[:80]
        return (ack + "\n" + notice) if ack else notice

    def running_for_group(self, group_id: str) -> Optional[TaskStatusInfo]:
        with self._lock:
            job = self._running.get(str(group_id))
            if job is None or job.done:
                return None
            return TaskStatusInfo(
                task_id=job.task_id,
                group_id=job.req.group_id,
                prompt=clip_text(job.req.prompt, 80),
                elapsed=round(max(0.0, time.monotonic() - job.started_at), 1),
                progress=job.progress,
            )

    def status(self, group_id: str) -> Optional[TaskStatusInfo]:
        return self.running_for_group(group_id)

    def stop(self, group_id: str) -> bool:
        with self._lock:
            job = self._running.get(str(group_id))
            if job is None or job.done:
                return False
            job.progress = "正在终止…"
            job.cancel_event.set()
            return True

    def submit(self, req: TaskRequest, ack_text: str = "") -> Optional[dict[str, Any]]:
        if not self.available():
            return None
        gid = str(req.group_id)
        with self._lock:
            existing = self._running.get(gid)
            if existing is not None and not existing.done:
                return {
                    "accepted": False,
                    "reason": "task_running",
                    "notice": "刚才那件「{}」还在后台查着呢，先等它结果回来，或者喊我停掉重来。".format(
                        clip_text(existing.req.prompt, 24)
                    ),
                }
            tid = uuid.uuid4().hex[:10]
            job = _RunningJob(tid, req, time.monotonic())
            self._running[gid] = job

        def _on_progress(step: str) -> None:
            with self._lock:
                if self._running.get(gid) is job and not job.done:
                    job.progress = str(step or "")[:120]

        def _worker() -> None:
            res = TaskResult(status="error", timeout=self.timeout, error="unknown")
            try:
                res = self._runner.run(
                    req,
                    self._backend,
                    cancel_event=job.cancel_event,
                    on_progress=_on_progress,
                    extra_secrets=self._secrets(),
                )
            except Exception as exc:
                res = TaskResult(status="error", timeout=self.timeout, error=str(exc))
            finally:
                delivery = PendingDelivery(
                    task_id=tid,
                    group_id=gid,
                    umo=req.umo,
                    prompt=req.prompt,
                    status="completed" if res.status == "ok" else "failed",
                    reason=res.status,
                    result=res.text,
                    partial=res.partial,
                    files=tuple(res.files or ()),
                    error=res.error,
                    pre_history=tuple(req.pre_history or ()),
                    created_at=time.time(),
                )
                if self._repo is not None:
                    try:
                        self._repo.save_pending_delivery(delivery)
                    except Exception:
                        pass
                with self._lock:
                    job.done = True
                    if self._running.get(gid) is job:
                        self._running.pop(gid, None)
                    self._completed[tid] = delivery
                    self._threads.pop(tid, None)

        th = threading.Thread(target=_worker, name="echo-worker-" + tid, daemon=True)
        with self._lock:
            self._threads[tid] = th
        try:
            th.start()
        except Exception:
            with self._lock:
                self._threads.pop(tid, None)
                if self._running.get(gid) is job:
                    self._running.pop(gid, None)
            raise
        return {
            "accepted": True,
            "reason": "task",
            "task_id": tid,
            "notice": self.format_start_notice(req.prompt, self.timeout, ack_text=ack_text),
        }

    def _mark_delivered_locked(self, tid: str) -> bool:
        if not tid:
            return True
        if tid in self._delivered_ids:
            return False
        self._delivered_ids[tid] = time.time()
        self._delivered_ids.move_to_end(tid)
        while len(self._delivered_ids) > 500:
            self._delivered_ids.popitem(last=False)
        return True

    def poll_deliveries(self) -> list[PendingDelivery]:
        out: list[PendingDelivery] = []
        seen_ids: set[str] = set()
        with self._lock:
            for tid, d in list(self._completed.items()):
                seen_ids.add(tid)
                if tid in self._delivered_ids or tid in self._inflight_deliveries:
                    continue
                self._inflight_deliveries.add(tid)
                out.append(d)
            if self._repo is not None:
                for d in self._repo.list_pending_deliveries():
                    tid = d.task_id
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    if tid in self._delivered_ids:
                        try:
                            self._repo.delete_pending_delivery(tid)
                        except Exception:
                            pass
                        for item in d.files:
                            cleanup_file_entry(item.get("path", ""), self.outbox_root)
                        continue
                    if tid in self._inflight_deliveries:
                        continue
                    self._inflight_deliveries.add(tid)
                    out.append(d)
        return out

    def ack_delivery(self, task_id: str) -> None:
        tid = str(task_id or "")
        with self._lock:
            self._inflight_deliveries.discard(tid)
            self._completed.pop(tid, None)
            self._mark_delivered_locked(tid)
        if self._repo is not None and tid:
            try:
                self._repo.delete_pending_delivery(tid)
            except Exception:
                pass

    def release_delivery(self, task_id: str) -> None:
        tid = str(task_id or "")
        if not tid:
            return
        with self._lock:
            self._inflight_deliveries.discard(tid)

    def shutdown(self, timeout: float = 3.0) -> None:
        with self._lock:
            for job in self._running.values():
                if not job.done:
                    job.progress = "正在终止…"
                    job.cancel_event.set()
            threads = list(self._threads.values())
        deadline = time.monotonic() + max(0.0, float(timeout))
        for th in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            th.join(timeout=remaining)

    def _filter_valid_files(self, files: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        return filter_valid_files(files, self.outbox_root)

    def present_delivery(self, delivery: PendingDelivery, llm: Optional[LLMClient]) -> dict[str, Any]:
        presenter = DeliveryPresenter(outbox_root=self.outbox_root, timeout=self.timeout, repo=self._repo)
        return presenter.present_delivery(delivery, llm, secrets=self._secrets())

    def _rewrite_success(
        self,
        group_id: str,
        prompt: str,
        raw_result: str,
        files: tuple[dict[str, Any], ...],
        llm: Optional[LLMClient],
        secrets: tuple[str, ...],
    ) -> str:
        return rewrite_success(prompt, raw_result, files, llm, secrets)
