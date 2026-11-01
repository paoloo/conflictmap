"""Vendored world-conflict intel pipeline (copied from the sibling Qt app's
intel/ package, 2026-11-01): gather (news RSS + UCDP CSV) -> candidate match
-> rerank (llama-server) -> summarize (Ollama) -> ranked run + world brief.

Imports are relative so this package is self-contained; the only external
dependency is `requests` (see requirements.txt). The models themselves are
launched on demand — see the README's "The models" table.
"""
