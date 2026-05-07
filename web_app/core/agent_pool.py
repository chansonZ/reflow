# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""
Agent pool for the MiroFlow web application.

Maintains a fixed-size pool of pre-built agents so that each submitted
query does not need to call build_agent_from_config from scratch.

Usage::

    pool = AgentPool(
        config_path="config/agent_web_demo.yaml",
        project_root=Path("/path/to/project"),
        pool_size=10,
    )
    pool.warmup()               # call once at startup

    agent, is_overflow = pool.acquire()
    try:
        result = await agent.run(ctx)
    finally:
        pool.release(agent, is_overflow)

Configuration (environment variables):
    AGENT_POOL_SIZE            – number of agents to pre-build (default: 10)
    AGENT_POOL_MAX_OVERFLOW    – max additional overflow agents (-1 = unlimited)
    AGENT_POOL_STRATEGY        – overflow strategy: "overflow_create" (default),
                                 "block", or "reject"
"""

import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Tuple

logger = logging.getLogger(__name__)


class AgentPool:
    """Thread-safe pool of pre-built agents.

    Agents are expensive to construct (LLM clients, tool initialisation, …).
    This pool pre-builds *pool_size* agents at startup so that subsequent
    task submissions can acquire one instantly instead of paying the build cost
    on every request.

    If the pool is exhausted an *overflow* agent is created on demand and
    discarded after the task completes so that the pool size stays stable.

    Because tasks run in background threads (each with their own asyncio event
    loop via :func:`asyncio.run`) the pool uses :class:`queue.Queue` which is
    inherently thread-safe.
    """

    def __init__(
        self,
        config_path: str,
        project_root: Path,
        pool_size: int = 10,
        max_overflow: int = -1,
    ) -> None:
        """
        Args:
            config_path:   Path to the agent YAML config relative to
                           *project_root* (e.g. ``"config/agent_web_demo.yaml"``).
            project_root:  Absolute path to the MiroFlow project root.
            pool_size:     Number of agents to pre-build during warmup.
            max_overflow:  Maximum number of overflow agents that may be
                           created concurrently.  ``-1`` means unlimited.
        """
        self._config_path = config_path
        self._project_root = project_root
        self._pool_size = pool_size
        self._max_overflow = max_overflow

        # thread-safe FIFO pool
        self._pool: queue.Queue = queue.Queue(maxsize=pool_size)

        # stats & overflow tracking
        self._stats_lock = threading.Lock()
        self._overflow_active = 0
        self._overflow_total = 0
        self._total_acquired = 0
        self._total_released = 0
        self._total_build_time_s = 0.0
        self._total_builds = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_project_on_path(self) -> None:
        """Add *project_root* to ``sys.path`` and set it as cwd."""
        project_root_str = str(self._project_root)
        if project_root_str not in sys.path:
            sys.path.insert(0, project_root_str)
        os.chdir(self._project_root)

    def _build_one_agent(self) -> Tuple[Any, float]:
        """Build a single agent from *config_path*.

        Returns:
            Tuple of ``(agent, elapsed_seconds)``.
        """
        self._ensure_project_on_path()

        # Deferred imports to avoid circular import issues at module load time
        from config import load_config  # type: ignore[import]
        from miroflow.agents import build_agent_from_config  # type: ignore[import]

        cfg = load_config(self._config_path)
        t0 = time.monotonic()
        agent = build_agent_from_config(cfg=cfg)
        elapsed = time.monotonic() - t0

        with self._stats_lock:
            self._total_build_time_s += elapsed
            self._total_builds += 1

        return agent, elapsed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def warmup(self) -> int:
        """Pre-build agents and fill the pool.

        Called once during application startup.  Builds *pool_size* agents
        sequentially and deposits them into the pool.

        Returns:
            Number of agents successfully created.
        """
        built = 0
        logger.info(
            "Agent pool: warming up %d agents (config=%s)",
            self._pool_size,
            self._config_path,
        )

        for i in range(self._pool_size):
            try:
                agent, elapsed = self._build_one_agent()
                self._pool.put_nowait(agent)
                built += 1
                logger.info(
                    "Agent pool: built agent %d/%d in %.2fs",
                    i + 1,
                    self._pool_size,
                    elapsed,
                )
            except Exception:
                logger.exception(
                    "Agent pool: failed to build agent %d/%d (config=%s)",
                    i + 1,
                    self._pool_size,
                    self._config_path,
                )

        logger.info(
            "Agent pool: ready — %d/%d agents available (config=%s)",
            built,
            self._pool_size,
            self._config_path,
        )
        return built

    def acquire(self) -> Tuple[Any, bool]:
        """Acquire an agent.

        Tries to return a pre-built agent from the pool first.  If the pool
        is empty, an *overflow* agent is built on demand.

        Returns:
            ``(agent, is_overflow)`` — ``is_overflow`` is ``True`` when the
            agent was created on demand and must NOT be returned to the pool.

        Raises:
            RuntimeError: If ``max_overflow`` is set and has been reached.
        """
        # --- try pool first ---
        try:
            agent = self._pool.get_nowait()
            with self._stats_lock:
                self._total_acquired += 1
            logger.debug(
                "Agent pool: acquired from pool (%d remaining)",
                self._pool.qsize(),
            )
            return agent, False
        except queue.Empty:
            pass

        # --- overflow path ---
        with self._stats_lock:
            if self._max_overflow != -1 and self._overflow_active >= self._max_overflow:
                raise RuntimeError(
                    f"Agent pool exhausted: pool_size={self._pool_size}, "
                    f"max_overflow={self._max_overflow} already in use"
                )
            self._overflow_active += 1
            self._overflow_total += 1
            overflow_num = self._overflow_total

        logger.info(
            "Agent pool: pool empty — creating overflow agent #%d "
            "(active_overflow=%d, config=%s)",
            overflow_num,
            self._overflow_active,
            self._config_path,
        )

        try:
            agent, elapsed = self._build_one_agent()
        except Exception:
            with self._stats_lock:
                self._overflow_active -= 1
            raise

        with self._stats_lock:
            self._total_acquired += 1

        logger.info(
            "Agent pool: overflow agent #%d built in %.2fs",
            overflow_num,
            elapsed,
        )
        return agent, True

    def release(self, agent: Any, is_overflow: bool) -> None:
        """Release an agent back to the pool.

        Overflow agents are discarded.  Pool agents are returned to the pool
        if there is space; otherwise they are discarded (this can happen if
        the pool was over-filled somehow).

        Args:
            agent:       The agent to release.
            is_overflow: Whether the agent was an overflow agent.
        """
        with self._stats_lock:
            self._total_released += 1
            if is_overflow:
                self._overflow_active -= 1

        if is_overflow:
            logger.debug("Agent pool: discarding overflow agent")
            return

        try:
            self._pool.put_nowait(agent)
            logger.debug(
                "Agent pool: returned to pool (%d available)",
                self._pool.qsize(),
            )
        except queue.Full:
            logger.debug("Agent pool: pool full, discarding extra agent")

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def available(self) -> int:
        """Current number of idle agents in the pool."""
        return self._pool.qsize()

    @property
    def stats(self) -> dict:
        """Snapshot of pool metrics for monitoring / health endpoints."""
        with self._stats_lock:
            builds = self._total_builds
            build_time = self._total_build_time_s
            return {
                "config_path": self._config_path,
                "pool_size": self._pool_size,
                "available": self._pool.qsize(),
                "overflow_active": self._overflow_active,
                "overflow_total": self._overflow_total,
                "total_acquired": self._total_acquired,
                "total_released": self._total_released,
                "avg_build_time_s": round(build_time / builds, 3) if builds else 0.0,
            }