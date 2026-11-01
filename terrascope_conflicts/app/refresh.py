"""In-process intel pipeline runner (the Qt app's IntelWorker without Qt).

The refresh button runs the full pipeline — gather (news/UCDP fetch) →
candidate match → rerank (llama-server) → summarize (Ollama) → brief —
exactly `intel.pipeline.run`, in a daemon thread. Results land on a queue
the UI loop drains. Nothing is duplicated: the pipeline writes the latest
run into new/data/newsgather.db, and the next data load reads it back.

Settings come from the same config.toml [intel] section the Qt app uses.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path


class RefreshWorker:
    """One pipeline run at a time; progress strings on `updates`."""

    def __init__(self, intel_cfg, root: Path) -> None:
        self.cfg = intel_cfg
        self.root = root          # package root: relative intel.db resolves here
        self.updates: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, gather: bool = True) -> bool:
        """Start one run (gather+rank+summarize); False if already running."""
        if self.busy:
            return False
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(gather,),
                                        name="intel-refresh", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self, gather: bool) -> None:
        from ..intel import pipeline
        c = self.cfg
        db = Path(c.db)
        settings = pipeline.Settings(
            db=db if db.is_absolute() else (self.root / db).resolve(),
            gather=gather and c.gather, llm=c.summarize,
            model=c.model, ollama_host=c.ollama_host, reranker_model=c.reranker_model,
            reranker_port=c.reranker_port, hours=c.hours, max_age_days=c.max_age_days,
            keep=c.keep, min_score=c.min_score)
        try:
            pipeline.run(settings, progress=self.updates.put,
                         cancelled=self._cancel.is_set)
            self.updates.put(("done", None))
        except Exception as exc:  # noqa: BLE001 - surfaced in the status line
            self.updates.put(("error", f"{type(exc).__name__}: {exc}"))

    def poll(self) -> list:
        """Drain progress strings / final markers without blocking."""
        out = []
        while True:
            try:
                out.append(self.updates.get_nowait())
            except queue.Empty:
                return out
