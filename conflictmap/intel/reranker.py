"""Cross-encoder reranking through an on-demand, loopback-only llama-server.

Same model and approach as Akari's Hikari worker (~/Workspace/SRC/tmp/b,
runtime/research/hikari_models.py + knowledge._rerank): bge-reranker-v2-m3
GGUF served by Homebrew llama.cpp with `--reranking`. The server is started on
first use (from the local HF cache; no download once cached) and stopped by
`close()` or after `idle_seconds` without use. If a server is already
answering on the port (e.g. a long-lived one you started yourself) it is used
and never stopped by us.

Scores are raw logits: relevant items land around -4..+5, unrelated ones
around -10. A model that returns one constant score for every document is
treated as unavailable (Akari found GGUF conversions that do this).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "gpustack/bge-reranker-v2-m3-GGUF:Q4_K_M"
DEFAULT_PORT = 8091          # Akari's own reranker uses 8081; keep clear of it
BATCH = 16                   # documents per /rerank request
DOC_CHARS = 600


class RerankerUnavailable(RuntimeError):
    pass


def _executable() -> str:
    for candidate in (shutil.which("llama-server"), "/opt/homebrew/bin/llama-server",
                      "/usr/local/bin/llama-server"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RerankerUnavailable("llama-server not found (brew install llama.cpp)")


class Reranker:
    def __init__(self, model: str = DEFAULT_MODEL, port: int = DEFAULT_PORT,
                 log_path: str | Path = "data/reranker.log", idle_seconds: int = 120) -> None:
        self.model = model
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.log_path = Path(log_path)
        self.idle_seconds = max(15, idle_seconds)
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._last_used = 0.0
        self._reaper: threading.Thread | None = None
        self._closed = False

    # -- lifecycle ----------------------------------------------------------

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=2) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def ensure(self) -> None:
        with self._lock:
            if self._closed:
                raise RerankerUnavailable("reranker closed")
            self._last_used = time.monotonic()
            if self._process and self._process.poll() is None:
                return
            if self._healthy():
                return  # someone else's server; use it, never stop it
            command = [_executable(), "-hf", self.model, "--host", "127.0.0.1",
                       "--port", str(self.port), "--reranking", "--ctx-size", "8192",
                       "--no-webui", "--log-disable"]
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("ab") as log:
                self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                                 stdout=log, stderr=log, start_new_session=True)
            process = self._process
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap_loop,
                                                name="reranker-reaper", daemon=True)
                self._reaper.start()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RerankerUnavailable(
                    f"llama-server exited with status {process.returncode}; see {self.log_path}")
            if self._healthy():
                return
            time.sleep(0.3)
        self.stop()
        raise RerankerUnavailable("llama-server did not become ready in 180 s")

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(2)
            with self._lock:
                if self._closed:
                    return
                idle = (self._process is not None
                        and time.monotonic() - self._last_used >= self.idle_seconds)
            if idle:
                self.stop()

    # -- scoring --------------------------------------------------------------

    def scores(self, query: str, documents: list[str]) -> list[float]:
        """One relevance score per document (same order). Raises RerankerUnavailable."""
        if not documents:
            return []
        self.ensure()
        out: list[float | None] = [None] * len(documents)
        for start in range(0, len(documents), BATCH):
            chunk = [d[:DOC_CHARS] for d in documents[start:start + BATCH]]
            body = json.dumps({"model": self.model, "query": query, "documents": chunk}).encode()
            request = urllib.request.Request(self.base + "/rerank", data=body,
                                             headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = json.loads(response.read())
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                raise RerankerUnavailable(f"rerank request failed: {exc}") from exc
            for result in payload.get("results") or payload.get("data") or []:
                index = int(result.get("index", -1))
                if 0 <= index < len(chunk):
                    out[start + index] = float(result.get("relevance_score", result.get("score", 0.0)))
            with self._lock:
                self._last_used = time.monotonic()
        if any(score is None for score in out):
            raise RerankerUnavailable("reranker returned an incomplete result")
        values = [float(score) for score in out]  # type: ignore[arg-type]
        if len(values) > 1 and max(values) - min(values) < 1e-9:
            raise RerankerUnavailable("reranker returned a constant score")
        return values
