# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""File-based session management for tasks."""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models.task import FileInfo, TaskResponse, TaskStatus

def _serialise_without_ts(d: dict) -> str:
    """Return a stable JSON string for *d* excluding the ``updated_at`` key.

    Used by :meth:`SessionManager.update_task` to detect whether a write is
    actually needed (skip-if-unchanged optimisation).
    """
    return json.dumps(
        {k: v for k, v in d.items() if k != "updated_at"},
        sort_keys=True,
        default=str,
    )
    
#z 每次 /api/tasks?page=1&page_size=50 HTTP/1.1" 200 都会读取task文件
class SessionManager:
    """Manages task sessions stored as JSON files."""

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _get_session_path(self, task_id: str) -> Path:
        """Get path to session file for a task."""
        return self.sessions_dir / f"{task_id}.json"

    def _read_session(self, task_id: str) -> dict[str, Any] | None:
        """Read session data from file."""
        path = self._get_session_path(task_id)
        if not path.exists():
            return None
        # print(f'_read_session: {path}')
        with self._lock:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return None
    #z 多人?? FileNotFoundError: [Errno 2] No such file or directory: '/home/arch/chan_workspace/miroflow/web_app/sessions/task_bed15968e256.tmp' -> '/home/arch/chan_workspace/miroflow/web_app/sessions/task_bed15968e256.json' 
    #z 每次前端查询都会更新写文件，只有一个字段有变化：updated_at，其他都没有变化
    def _write_session00(self, task_id: str, data: dict[str, Any]) -> None:
        """Write session data to file atomically."""
        path = self._get_session_path(task_id)
        temp_path = path.with_suffix(".tmp")
        try:
            with self._lock:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str,ensure_ascii=False,) #  ensure_ascii=False,  #z 关键：禁止 ASCII 转义

                os.replace(temp_path, path)
                # print(f'_write_session: 完成{path}')
                # print(f'内容：\n{data}\n')
        except Exception as e:
            # 记录错误并清理临时文件
            print(f"[ERROR] Failed to write session {task_id}: {e}")
            if temp_path.exists():
                temp_path.unlink()
            raise
    def _write_session01(self, task_id: str, data: dict[str, Any]) -> None:
        """Write session data to file atomically only if non-updated_at fields have changed."""
        path = self._get_session_path(task_id)
        temp_path = path.with_suffix(".tmp")
        
        # 如果文件存在，检查是否有实质性变化（排除 updated_at）
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                
                # 排除 updated_at 字段后比较
                existing_filtered = {k: v for k, v in existing_data.items() if k != "updated_at"}
                new_filtered = {k: v for k, v in data.items() if k != "updated_at"}
                
                # 使用 json.dumps 进行深度比较（处理嵌套结构）
                if json.dumps(existing_filtered, sort_keys=True) == json.dumps(new_filtered, sort_keys=True):
                    return  # 没有实质性变化，跳过写入
            except (json.JSONDecodeError, IOError):
                # 文件损坏或不存在，继续写入
                pass
        
        # 执行写入操作
        try:
            with self._lock:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str, ensure_ascii=False)
                
                os.replace(temp_path, path)
                # print(f'_write_session: 完成{path}')
                print(f'task status 内容更新：\n{data}\n')
        except Exception as e:
            print(f"[ERROR] Failed to write session {task_id}: {e}")
            if temp_path.exists():
                temp_path.unlink()
            raise
    def _write_session(self, task_id: str, data: dict[str, Any]) -> None:
        """Write session data to file atomically.

        Must be called while ``self._lock`` is already held by the caller.
        """
        path = self._get_session_path(task_id)
        temp_path = path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
            os.replace(temp_path, path)
            print(f'task status 内容更新：\n{data}\n')
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise        

    def create_task(
        self,
        task_id: str,
        task_description: str,
        config_path: str,
        file_info: FileInfo | None = None,
        log_path: str | None = None,
        max_turns: int = 0,
    ) -> TaskResponse:
        """Create a new task session."""
        now = datetime.now()#datetime.utcnow() #z
        session = {
            "id": task_id,
            "task_description": task_description,
            "config_path": config_path,
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "current_turn": 0,
            "max_turns": max_turns,
            "step_count": 0,
            "final_answer": None,
            "summary": None,
            "error_message": None,
            "file_info": file_info.model_dump() if file_info else None,
            "log_path": log_path,
        }
        
        if self.task_exists(task_id):
            raise ValueError(f"Task ID {task_id} already exists")
        with self._lock:
            self._write_session(task_id, session)
        return TaskResponse(**session)

    def get_task(self, task_id: str) -> TaskResponse | None:
        """Get task by ID."""
        session = self._read_session(task_id)
        if session is None:
            return None
        return TaskResponse(**session)

    def list_tasks(
        self,
        page: int = 1,
        page_size: int = 20,
        status: TaskStatus | None = None,
    ) -> tuple[list[TaskResponse], int]:
        """List all tasks with pagination."""
        tasks = []
        for path in self.sessions_dir.glob("*.json"):
            session = self._read_session(path.stem)
            if session:
                if status is None or session.get("status") == status:
                    tasks.append(TaskResponse(**session))

        # Sort by created_at descending (newest first)
        tasks.sort(key=lambda t: t.created_at, reverse=True)

        # Paginate
        total = len(tasks)
        start = (page - 1) * page_size
        end = start + page_size
        return tasks[start:end], total

    def update_task(self, task_id: str, updates: dict[str, Any]) -> TaskResponse | None:
        """Update task session with new values (atomic read-modify-write).

        The entire read → merge → write cycle is performed under a single lock
        acquisition so concurrent calls cannot interleave and lose each other's
        updates.  Writes are skipped when the merged data is identical to the
        current file contents (ignoring the ``updated_at`` timestamp) to avoid
        unnecessary I/O.
        """
        path = self._get_session_path(task_id)
        with self._lock:
            if not path.exists():
                return None

            # Inline read (no nested _read_session call – this thread already
            # holds self._lock which is not reentrant).
            try:
                with open(path, encoding="utf-8") as f:
                    session = json.load(f)
            except (json.JSONDecodeError, IOError):
                return None

            # Compute the merged state before touching timestamps.
            merged = {**session, **updates}

            # Skip writing when there are no substantive changes (the only
            # difference would be an updated timestamp, which is not meaningful).
            if _serialise_without_ts(merged) == _serialise_without_ts(session):
                return TaskResponse(**session)

            merged["updated_at"] = datetime.now().isoformat()
            self._write_session(task_id, merged)

        return TaskResponse(**merged)
        
        # """Update task session with new values."""
        # session = self._read_session(task_id)
        # if session is None:
        #     return None

        # session.update(updates)
        # session["updated_at"] = datetime.now().isoformat()#datetime.utcnow().isoformat()
        # self._write_session(task_id, session)
        # return TaskResponse(**session)

    def delete_task(self, task_id: str) -> bool:
        """Delete task session file."""
        path = self._get_session_path(task_id)
        if path.exists():
            with self._lock:
                path.unlink()
            return True
        return False

    def task_exists(self, task_id: str) -> bool:
        """Check if task exists."""
        return self._get_session_path(task_id).exists()
#z 无stop任务