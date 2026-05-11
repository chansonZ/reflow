# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for task management."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

class SearchResult(BaseModel):
    """A single result returned by a search tool."""

    title: str | None = None
    url: str = ""
    snippet: str | None = None
    favicon: str | None = None


class TrajectoryEvent(BaseModel):
    """A structured event in the agent's thinking trajectory."""

    id: str
    type: Literal["search", "read", "reasoning", "tool_call"]
    parent_id: str | None = None

    # search
    query: str | None = None
    results: list[SearchResult] = Field(default_factory=list)
    results_count: int = 0

    # read
    url: str | None = None
    urls: list[str] | None = None  # for multi-URL read events (e.g. scrape_and_extract_info_multi)

    # reasoning
    text: str | None = None

    # generic tool_call fallback
    tool_name: str | None = None
    args: dict[str, Any] | None = None

class FileInfo(BaseModel):
    """File information for uploaded files."""

    file_id: str
    file_name: str
    file_type: str
    absolute_file_path: str


class TaskCreate(BaseModel):
    """Request model for creating a new task."""

    task_description: str = Field(
        ..., min_length=1, description="The task/question to process"
    )
    config_path: str = Field(
        default="config/agent_web_demo.yaml", description="Agent config path"
    )
    file_id: str | None = Field(default=None, description="Uploaded file ID")


class TaskResponse(BaseModel):
    """Response model for task data."""

    id: str
    task_description: str
    config_path: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime

    # Progress info
    current_turn: int = 0
    max_turns: int = 0
    step_count: int = 0

    # Results (populated when completed)
    final_answer: str | None = None
    summary: str | None = None
    error_message: str | None = None

    # File info
    file_info: FileInfo | None = None

    # Log path for debugging
    log_path: str | None = None


class TaskListResponse(BaseModel):
    """Response model for task list."""

    tasks: list[TaskResponse]
    total: int
    page: int
    page_size: int


class Message(BaseModel):
    """Model for LLM conversation message."""

    role: str
    content: str


class TaskStatusUpdate(BaseModel):
    """Model for polling status updates (lightweight)."""

    id: str
    status: TaskStatus
    current_turn: int = 0
    step_count: int = 0
    recent_logs: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    final_answer: str | None = None
    summary: str | None = None
    error_message: str | None = None
    trajectory: list[TrajectoryEvent] = Field(default_factory=list)


class UploadResponse(BaseModel):
    """Response model for file upload."""

    file_id: str
    file_name: str
    file_type: str
    absolute_file_path: str


class ConfigListResponse(BaseModel):
    """Response model for config list."""

    configs: list[str]
    default: str
