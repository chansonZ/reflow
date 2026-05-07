export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface FileInfo {
  file_id: string;
  file_name: string;
  file_type: string;
  absolute_file_path: string;
}

export interface Task {
  id: string;
  task_description: string;
  config_path: string;
  status: TaskStatus;
  created_at: string;
  updated_at: string;
  current_turn: number;
  max_turns: number;
  step_count: number;
  final_answer: string | null;
  summary: string | null;
  error_message: string | null;
  file_info: FileInfo | null;
  log_path: string | null;
}

export interface TaskCreate {
  task_description: string;
  config_path: string;
  file_id?: string;
}

export interface Message {
  role: string;
  content: string;
}

export interface SearchResult {
  title?: string | null;
  url: string;
  snippet?: string | null;
  favicon?: string | null;
}

export type TrajectoryEventType = 'search' | 'read' | 'reasoning' | 'tool_call';

export interface TrajectoryEvent {
  id: string;
  type: TrajectoryEventType;
  parent_id?: string | null;
  // search
  query?: string | null;
  results?: SearchResult[];
  results_count?: number;
  // read
  url?: string | null;
  // reasoning
  text?: string | null;
  // generic tool_call
  tool_name?: string | null;
  args?: Record<string, unknown> | null;
  status?: 'started' | 'completed' | 'error' | null;
  result?: string | null;
  error?: string | null;
}

export interface TaskStatusUpdate {
  id: string;
  status: TaskStatus;
  current_turn: number;
  step_count: number;
  recent_logs: unknown[];
  messages: Message[];
  final_answer: string | null;
  summary: string | null;
  error_message: string | null;
  trajectory?: TrajectoryEvent[];
}

export interface TaskListResponse {
  tasks: Task[];
  total: number;
  page: number;
  page_size: number;
}

export interface ConfigListResponse {
  configs: string[];
  default: string;
}

export interface UploadResponse {
  file_id: string;
  file_name: string;
  file_type: string;
  absolute_file_path: string;
}
