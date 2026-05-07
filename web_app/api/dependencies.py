# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""FastAPI dependencies for dependency injection."""

from ..core.agent_pool import AgentPool
from ..core.config import AppConfig, config
from ..core.session_manager import SessionManager
from ..core.task_executor import TaskExecutor

# Global instances (created once at startup)
_session_manager: SessionManager | None = None
_task_executor: TaskExecutor | None = None
_agent_pool: AgentPool | None = None


def get_config() -> AppConfig:
    """Get application configuration."""
    return config


def get_session_manager() -> SessionManager:
    """Get session manager instance."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(config.sessions_dir)
    return _session_manager


def get_task_executor() -> TaskExecutor:
    """Get task executor instance."""
    global _task_executor
    if _task_executor is None:
        _task_executor = TaskExecutor(config, get_session_manager())
    return _task_executor


def get_agent_pool() -> AgentPool | None:
    """Get the agent pool instance (may be None before warmup completes)."""
    return _agent_pool

def init_dependencies() -> None:
    """Initialize all dependencies at startup."""
    # global _session_manager, _task_executor
    global _session_manager, _task_executor, _agent_pool
    _session_manager = SessionManager(config.sessions_dir)
    _task_executor = TaskExecutor(config, _session_manager)

    # Create the pool; warmup is triggered separately (in lifespan) so that
    # the app can start serving health-check requests while agents are built.
    _agent_pool = AgentPool(
        config_path=config.default_config,
        project_root=config.project_root,
        pool_size=config.agent_pool_size,
        max_overflow=config.agent_pool_max_overflow,
    )
    _task_executor.agent_pool = _agent_pool