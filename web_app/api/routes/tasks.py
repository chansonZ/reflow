# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Task management endpoints."""
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.session_manager import SessionManager
from ...core.task_executor import TaskExecutor
from ...models.task import (
    FileInfo,
    Message,
    TaskCreate,
    TaskListResponse,
    TaskResponse,
    TaskStatusUpdate,
    TrajectoryEvent, #z
)
from ..dependencies import get_session_manager, get_task_executor

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def filter_and_clean_messages(messages):
    """
    过滤和清洗消息列表
    
    Args:
        messages: 包含消息的字典列表
    
    Returns:
        清洗后的消息列表
    """
    filtered_messages = []

    # 定义需要截断的用户任务描述
    user_task_prefix = "Your task is to comprehensively address the question by actively collecting detailed information from the web, and generating a thorough, transparent report"

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")

        # 过滤条件1: 删除 role 为 "system" 的项目
        if role == "system":
            continue

        # 过滤条件2: 删除 role 为 "assistant" 且 content 包含 "<use_mcp_tool>" 的项目
        if role == "assistant" and "<use_mcp_tool>" in content:
            continue

        # 清洗条件: role 为 "user" 且 content 中包含特定任务描述
        if role == "user" and user_task_prefix in content:
            # 找到任务描述的位置并截断
            split_index = content.find(user_task_prefix)
            if split_index != -1:
                # 保留任务描述之前的内容
                cleaned_content = content[:split_index].rstrip()
                message["content"] = cleaned_content

        # 添加到过滤后的消息列表
        filtered_messages.append(message)

    return filtered_messages


@router.post("", response_model=TaskResponse)
async def create_task(
    task: TaskCreate,
    session_manager: SessionManager = Depends(get_session_manager),
    task_executor: TaskExecutor = Depends(get_task_executor),
) -> TaskResponse:
    """Create and start a new task."""
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    #z
    os.environ["TASK_ID"] = str(task_id)

    # Get file info if provided
    file_info = None
    if task.file_id:
        # Look up the uploaded file
        from ...core.config import config

        upload_dir = config.uploads_dir / task.file_id
        if upload_dir.exists():
            files = list(upload_dir.iterdir())
            if files:
                file_path = files[0]
                ext = file_path.suffix.lower()
                from .uploads import FILE_TYPE_MAP

                file_info = FileInfo(
                    file_id=task.file_id,
                    file_name=file_path.name,
                    file_type=FILE_TYPE_MAP.get(ext, "File"),
                    absolute_file_path=str(file_path.absolute()),
                )

    # Create session
    task_response = session_manager.create_task(
        task_id=task_id,
        task_description=task.task_description,
        config_path=task.config_path,
        file_info=file_info,
    )

    # Submit for background execution
    task_executor.submit_task(
        task_id=task_id,
        task_description=task.task_description,
        config_path=task.config_path,
        file_info=file_info,
    )

    return task_response


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session_manager: SessionManager = Depends(get_session_manager),
) -> TaskListResponse:
    """List all tasks with pagination."""
    tasks, total = session_manager.list_tasks(page, page_size)
    return TaskListResponse(
        tasks=tasks,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
    task_executor: TaskExecutor = Depends(get_task_executor),
) -> TaskResponse:
    """Get task by ID with current progress."""
    task = session_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # If running, get progress from executor
    # if task.status == "running":
    # Only merge live progress when the task is genuinely still running in the
    # executor.  Without this guard a page refresh that arrives just after the
    # task writes "completed" to disk (but before _running_tasks is cleaned up,
    # or while the session file transiently still says "running") would
    # overwrite final_answer / messages / status with empty progress data.
    if task.status == "running" and task_executor.is_task_running(task_id):
        progress = task_executor.get_task_progress(task_id)
        task = session_manager.update_task(task_id, progress)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

    return task


@router.get("/{task_id}/status", response_model=TaskStatusUpdate)
async def get_task_status(
    task_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
    task_executor: TaskExecutor = Depends(get_task_executor),
) -> TaskStatusUpdate:
    """Lightweight status endpoint for polling."""
    task = session_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    progress: dict = {}
    stored_messages: list = []
    stored_trajectory: list = [] #z

    if task.status == "running" and task_executor.is_task_running(task_id):
        progress_messages = progress.get("messages", [])
        progress_trajectory = progress.get("trajectory", [])

        if progress_messages:
            # Persist latest messages/trajectory to session file so they survive
            # across polls even if the in-memory tracer is unavailable later.
            session_manager.update_task(
                task_id,
                {
                    "current_turn": progress.get("current_turn", 0),
                    "step_count": progress.get("step_count", 0),
                    "messages": progress_messages,
                    "trajectory": progress_trajectory,
                },
            )
            stored_messages = progress_messages
            stored_trajectory = progress_trajectory
        else:
            # Tracer returned empty (not ready yet or transient failure).
            # Fall back to whatever was previously persisted to the session file.
            session_manager.update_task(
                task_id,
                {
                    "current_turn": progress.get("current_turn", 0),
                    "step_count": progress.get("step_count", 0),
                },
            )
            session_data = session_manager._read_session(task_id)
            if session_data:
                stored_messages = session_data.get("messages", [])
                stored_trajectory = session_data.get("trajectory", [])
    else:
        # For completed/failed/cancelled tasks, get stored messages from session
        session_data = session_manager._read_session(task_id)
        if session_data:
            stored_messages = session_data.get("messages", [])
            stored_trajectory = session_data.get("trajectory", []) #z

    # Convert messages to Message objects
    raw_messages = stored_messages
    messages = [Message(**m) for m in raw_messages if isinstance(m, dict)]

    # Build trajectory list
    raw_trajectory = stored_trajectory
    trajectory = [TrajectoryEvent(**e) for e in raw_trajectory if isinstance(e, dict)]

    return TaskStatusUpdate(
        id=task.id,
        status=task.status,
        current_turn=progress.get("current_turn", task.current_turn),
        step_count=progress.get("step_count", task.step_count),
        recent_logs=progress.get("recent_logs", []),
        messages=messages,
        final_answer=task.final_answer,
        summary=task.summary,
        error_message=task.error_message,
        trajectory=trajectory,#z
    )


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
    task_executor: TaskExecutor = Depends(get_task_executor),
) -> dict[str, str]:
    """Delete a task. Cancels if running."""
    task = session_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Cancel if running
    if task.status == "running":
        task_executor.cancel_task(task_id)

    # Delete session
    session_manager.delete_task(task_id)

    return {"message": "Task deleted", "id": task_id}

#z 无stop cancel任务
@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
    task_executor: TaskExecutor = Depends(get_task_executor),
) -> dict[str, str]:
    """Cancel a running task without deleting it."""
    task = session_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Task cannot be cancelled (status: {task.status})",
        )
    task_executor.cancel_task(task_id)
    return {"message": "Task cancellation requested", "id": task_id}
        