import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from miroflow.logging.task_tracer import utc_iso

TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "spm",
    "ref",
    "ref_src",
}


# def utc_iso() -> str:
#     return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
#         "+00:00", "Z"
#     )


def _safe_task_suffix(task_id="") -> str:
    key = os.getenv("TASK_CONTEXT_KEY")
    if key:
        return key

    if not task_id:
        task_id = os.getenv("TASK_ID", "local")
    attempt = os.getenv("TASK_ATTEMPT_ID", "0")
    retry = os.getenv("TASK_RETRY_ID", "0")
    return f"{task_id}_attempt_{attempt}_retry_{retry}"


def _state_file_path(task_id: str ="") -> Path:
    base_dir = Path(os.getenv("TASK_LOG_DIR", "./logs/mcp_servers"))
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"{_safe_task_suffix(task_id)}_web_url_state.json"


def load_url_state(task_id: str ="") -> dict[str, Any]:
    path = _state_file_path(task_id)
    if not path.exists():
        return {
            "updated_at": utc_iso(),
            "search_rounds": [],
            "url_records": {},
            "blacklist": {},
        }

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "updated_at": utc_iso(),
            "search_rounds": [],
            "url_records": {},
            "blacklist": {},
        }


def save_url_state(state: dict[str, Any], task_id: str = '') -> Path:
    path = _state_file_path(task_id)
    state["updated_at"] = utc_iso()
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)
    return path


def normalize_url(url: str) -> str:
    '''
    统一 scheme 和 netloc 为小写
    移除默认端口（:80 for http, :443 for https）
    路径处理：合并多余斜杠、移除末尾斜杠（除非是根路径）
    移除追踪参数：过滤掉 utm_*、gclid、fbclid 等常见营销/追踪参数
    对剩余参数排序后重新拼接
    
    https://example.com/path/?utm_source=twitter&id=123
    → https://example.com/path?id=123
    '''
    if not url:
        return ""

    raw_url = url.strip()
    if not raw_url:
        return ""
    if not re.match(r"^https?://", raw_url, flags=re.IGNORECASE):
        return raw_url

    parts = urlsplit(raw_url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_QUERY_KEYS or lowered.startswith("utm_"):
            continue
        query_pairs.append((key, value))
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path or "/", query, ""))


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    lowered = text.lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def make_similarity_signature(
    normalized_url: str,
    title: str = "",
    snippet: str = "",
) -> str:
    '''
    将URL、标题、摘要转换为可比较的文本指纹。
    '''
    url_text = normalized_url
    if normalized_url.startswith(("http://", "https://")):
        parts = urlsplit(normalized_url)
        path_tokens = [token for token in parts.path.split("/") if token]
        url_text = " ".join([parts.netloc, *path_tokens])
    text = " ".join(
        part for part in [url_text, _normalize_text(title), _normalize_text(snippet)] if part
    )
    return _normalize_text(text)


def compute_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(a=left, b=right).ratio()


@dataclass
class UrlDecision:
    canonical_url: str
    status: str
    reason: str
    matched_url: str | None = None
    similarity_score: float = 0.0


def decide_url_status(
    *,
    state: dict[str, Any],
    url: str,
    title: str = "",
    snippet: str = "",
    similar_threshold: float = 0.92,
    treat_seen_as_duplicate: bool = True,
) -> UrlDecision:
    canonical_url = normalize_url(url)
    if not canonical_url:
        return UrlDecision("", "invalid", "empty_or_invalid_url")

    blacklist = state.get("blacklist", {})
    if canonical_url in blacklist:
        return UrlDecision(
            canonical_url,
            "blacklisted",
            blacklist[canonical_url].get("reason", "blacklisted"),
            matched_url=canonical_url,
        )

    records = state.get("url_records", {})
    if canonical_url in records and treat_seen_as_duplicate:
        return UrlDecision(
            canonical_url,
            "duplicate_exact",
            "already_seen",
            matched_url=canonical_url,
        )

    current_signature = make_similarity_signature(canonical_url, title, snippet)
    best_match = None
    best_score = 0.0
    for existing_url, record in records.items():
        existing_signature = record.get("similarity_signature", "")
        if not treat_seen_as_duplicate and not record.get("last_scrape_success"):
            continue
        score = compute_similarity(current_signature, existing_signature)
        if score > best_score:
            best_score = score
            best_match = existing_url

    if best_match and best_score >= similar_threshold:
        return UrlDecision(
            canonical_url,
            "duplicate_similar",
            "similar_to_existing_url",
            matched_url=best_match,
            similarity_score=best_score,
        )

    return UrlDecision(canonical_url, "eligible", "new_url")


def upsert_url_record(
    *,
    state: dict[str, Any],
    canonical_url: str,
    title: str = "",
    snippet: str = "",
    source_query: str = "",
    source_rank: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    records = state.setdefault("url_records", {})
    now = utc_iso()
    record = records.get(canonical_url, {})
    record.update(
        {
            "canonical_url": canonical_url,
            "title": title,
            "snippet": snippet,
            "source_query": source_query,
            "source_rank": source_rank,
            "last_seen_at": now,
            "similarity_signature": make_similarity_signature(
                canonical_url, title, snippet
            ),
        }
    )
    if "first_seen_at" not in record:
        record["first_seen_at"] = now
    if metadata:
        record["metadata"] = metadata
    records[canonical_url] = record


def add_search_round(
    *,
    state: dict[str, Any],
    query: str,
    search_params: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    round_info = {
        "query": query,
        "search_params": search_params,
        "recorded_at": utc_iso(),
        "results": results,
    }
    state.setdefault("search_rounds", []).append(round_info)
    return round_info


def add_to_blacklist(
    state: dict[str, Any],
    canonical_url: str,
    *,
    reason: str,
    error: str = "",
) -> None:
    blacklist = state.setdefault("blacklist", {})
    blacklist[canonical_url] = {
        "reason": reason,
        "error": error,
        "blacklisted_at": utc_iso(),
    }
