"""Backward-compatible entry point for the pre-shared memory embedding worker.

New code uses mcp_server.embedding_worker through embedding_manager so memory and
skills share one worker process, cache, model, and idle timeout.
"""
from .embedding_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
