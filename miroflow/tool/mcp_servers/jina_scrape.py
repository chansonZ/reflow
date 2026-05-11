# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0
import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastmcp import FastMCP

from .utils.html_extractor import extract_webpage_content,truncate_content_for_model

# Configure logging
logger = logging.getLogger("miroflow")
#z 在 .env 中
SUMMARY_LLM_BASE_URL = os.environ.get("SUMMARY_LLM_BASE_URL")
SUMMARY_LLM_MODEL_NAME = os.environ.get("SUMMARY_LLM_MODEL_NAME")
SUMMARY_LLM_API_KEY = os.environ.get("SUMMARY_LLM_API_KEY")
SUMMARY_LLM_MAX_LEN = int(os.environ.get("SUMMARY_LLM_MAX_LEN",10240))
logger.info(f'SUMMARY_LLM_MAX_LEN = {SUMMARY_LLM_MAX_LEN}')

JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
JINA_BASE_URL = os.environ.get("JINA_BASE_URL", "https://r.jina.ai")

# Initialize FastMCP server
mcp = FastMCP("tool-jina-scrape")

# Module-level shared httpx client for connection pooling
_httpx_client: httpx.AsyncClient | None = None

# In-process cache for scrape/reachability state
_scrape_cache: dict[str, dict[str, Any]] = {}
_reachability_cache: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


SCRAPE_CACHE_ENABLED = _env_bool("SCRAPE_CACHE_ENABLED", True)
SCRAPE_CACHE_TTL_SECONDS = int(os.getenv("SCRAPE_CACHE_TTL_SECONDS", "1800"))
SCRAPE_CACHE_DIR = Path(
    os.getenv(
        "SCRAPE_CACHE_DIR",
        str(Path(os.getenv("TASK_LOG_DIR", "./logs/mcp_servers")) / "scrape_cache"),
    )
)
REACHABILITY_CHECK_ENABLED = _env_bool("REACHABILITY_CHECK_ENABLED", True)
REACHABILITY_TIMEOUT_SECONDS = float(os.getenv("REACHABILITY_TIMEOUT_SECONDS", "3.0"))
REACHABILITY_CACHE_TTL_SECONDS = int(os.getenv("REACHABILITY_CACHE_TTL_SECONDS", "300"))

def _get_httpx_client() -> httpx.AsyncClient:
    """Get or create a shared httpx.AsyncClient for Serper API requests."""
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(timeout=60.0)
    return _httpx_client


def _normalize_url_for_cache(url: str) -> str:
    if not url:
        return ""
    raw_url = url.strip()
    if not raw_url:
        return ""
    if not raw_url.lower().startswith(("http://", "https://")):
        return raw_url
    parts = urlsplit(raw_url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _stable_headers_for_key(custom_headers: Dict[str, str] | None) -> str:
    if not custom_headers:
        return ""
    pairs = []
    for key, value in custom_headers.items():
        if key.lower() == "authorization":
            continue
        pairs.append((key.lower(), str(value)))
    pairs.sort()
    return json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))


def _build_scrape_cache_key(
    url: str, custom_headers: Dict[str, str] | None, max_chars: int = 102400 * 4
) -> str:
    normalized_url = _normalize_url_for_cache(url)
    payload = f"{normalized_url}|{_stable_headers_for_key(custom_headers)}|{max_chars}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_file_cache(cache_key: str) -> Dict[str, Any] | None:
    if not SCRAPE_CACHE_ENABLED:
        return None
    cache_path = SCRAPE_CACHE_DIR / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    expires_at = cached.get("expires_at", 0)
    if expires_at <= time.time():
        return None
    result = cached.get("result")
    return result if isinstance(result, dict) else None


def _write_file_cache(cache_key: str, result: Dict[str, Any]) -> None:
    if not SCRAPE_CACHE_ENABLED:
        return
    try:
        SCRAPE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = SCRAPE_CACHE_DIR / f"{cache_key}.json"
        cache_path.write_text(
            json.dumps(
                {
                    "expires_at": time.time() + SCRAPE_CACHE_TTL_SECONDS,
                    "result": result,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Scrape cache write failed: {e}")


def _get_scrape_cache(cache_key: str) -> Dict[str, Any] | None:
    if not SCRAPE_CACHE_ENABLED:
        return None
    now = time.time()
    in_mem = _scrape_cache.get(cache_key)
    if in_mem and in_mem.get("expires_at", 0) > now:
        return in_mem.get("result")
    file_hit = _read_file_cache(cache_key)
    if file_hit is not None:
        _scrape_cache[cache_key] = {
            "expires_at": now + min(SCRAPE_CACHE_TTL_SECONDS, 120),
            "result": file_hit,
        }
        return file_hit
    return None


def _set_scrape_cache(cache_key: str, result: Dict[str, Any]) -> None:
    if not SCRAPE_CACHE_ENABLED:
        return
    if not result.get("success"):
        return
    _scrape_cache[cache_key] = {
        "expires_at": time.time() + SCRAPE_CACHE_TTL_SECONDS,
        "result": result,
    }
    _write_file_cache(cache_key, result)


async def _precheck_url_reachability(
    url: str, custom_headers: Dict[str, str] | None = None
) -> tuple[bool, str]:
    if not REACHABILITY_CHECK_ENABLED:
        return True, ""
    normalized_url = _normalize_url_for_cache(url)
    if not normalized_url.lower().startswith(("http://", "https://")):
        return True, ""

    now = time.time()
    cached = _reachability_cache.get(normalized_url)
    if cached and cached.get("expires_at", 0) > now:
        return bool(cached.get("reachable")), str(cached.get("error", ""))

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; miroflow-reachability-check/1.0)"
    }
    if custom_headers:
        headers.update(custom_headers)

    client = _get_httpx_client()
    timeout = httpx.Timeout(
        REACHABILITY_TIMEOUT_SECONDS,
        connect=min(REACHABILITY_TIMEOUT_SECONDS, 2.0),
        read=REACHABILITY_TIMEOUT_SECONDS,
    )
    try:
        response = await client.head(
            normalized_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
        if response.status_code in {405, 501}:
            response = await client.get(
                normalized_url,
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
            )
        reachable = True
        error = ""
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        reachable = False
        error = f"reachability precheck failed: {str(e)}"
    except Exception:
        # Unknown precheck errors should not block scraping.
        reachable = True
        error = ""

    _reachability_cache[normalized_url] = {
        "reachable": reachable,
        "error": error,
        "expires_at": now + REACHABILITY_CACHE_TTL_SECONDS,
    }
    return reachable, error


async def _scrape_url_fastest(
    url: str, custom_headers: Dict[str, str] | None = None
) -> Dict[str, Any]:
    cache_key = _build_scrape_cache_key(url, custom_headers)
    cached_result = _get_scrape_cache(cache_key)
    if cached_result is not None:
        logger.info(f"Scrape cache hit for url={url}")
        return cached_result

    tasks = {
        "jina": asyncio.create_task(scrape_url_with_jina(url, custom_headers)),
        "python": asyncio.create_task(scrape_url_with_python(url, custom_headers)),
    }
    task_name_by_obj = {task: name for name, task in tasks.items()}
    pending = set(tasks.values())
    failures: list[str] = []

    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                source = task_name_by_obj.get(task, "unknown")
                try:
                    result = task.result()
                except Exception as e:
                    failures.append(f"{source}: {str(e)}")
                    continue

                if result.get("success"):
                    logger.info(f"Scrape race winner: {source}, url={url}")
                    for wait_task in pending:
                        wait_task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    _set_scrape_cache(cache_key, result)
                    return result

                failures.append(f"{source}: {result.get('error', 'unknown error')}")
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return {
        "success": False,
        "content": "",
        "error": "Scraping failed (race mode): " + " | ".join(failures),
        "line_count": 0,
        "char_count": 0,
        "last_char_line": 0,
        "all_content_displayed": False,
    }


async def _scrape_and_extract_single(
    url: str, info_to_extract: str, custom_headers: Dict[str, str] = None
) -> Dict[str, Any]:
    """
    Internal helper: scrape a single URL and extract information via LLM.
    Returns a plain dict (not JSON string).
    """
    logger.info(f'#z _scrape_and_extract_single url={url}')
    if _is_huggingface_dataset_or_space_url(url):
        return {
            "success": False,
            "url": url,
            "extracted_info": "",
            "error": "You are trying to scrape a Hugging Face dataset for answers, please do not use the scrape tool for this purpose.",
            "scrape_stats": {},
            "model_used": "",
            "tokens_used": 0,
        }

    reachable, reachability_error = await _precheck_url_reachability(url, custom_headers)
    if not reachable:
        logger.warning(f"URL unreachable, skip scraping: url={url}, reason={reachability_error}")
        return {
            "success": False,
            "url": url,
            "extracted_info": "",
            "error": f"URL unreachable, skipped quickly: {reachability_error}",
            "scrape_stats": {},
            "model_used": SUMMARY_LLM_MODEL_NAME,
            "tokens_used": 0,
        }

    # Race Jina and Python in parallel, use the first successful result.
    scrape_result = await _scrape_url_fastest(url, custom_headers)
    if not scrape_result["success"]:
        logger.error(
            f"Jina Scrape and Extract Info: race scraping failed: {scrape_result['error']}"
        )
        return {
            "success": False,
            "url": url,
            "extracted_info": "",
            "error": scrape_result["error"],
            "scrape_stats": {},
            "model_used": SUMMARY_LLM_MODEL_NAME,
            "tokens_used": 0,
        }

    # Then, summarize the content
    extracted_result = await extract_info_with_llm(
        url=url,
        content=scrape_result["content"],
        info_to_extract=info_to_extract,
        model=SUMMARY_LLM_MODEL_NAME,
        max_tokens=8192,
    )

    return {
        "success": extracted_result["success"],
        "url": url,
        "extracted_info": extracted_result["extracted_info"],
        "error": extracted_result["error"],
        "scrape_stats": {
            "line_count": scrape_result["line_count"],
            "char_count": scrape_result["char_count"],
            "last_char_line": scrape_result["last_char_line"],
            "all_content_displayed": scrape_result["all_content_displayed"],
        },
        "model_used": extracted_result["model_used"],
        "tokens_used": extracted_result["tokens_used"],
    }


@mcp.tool()
async def scrape_and_extract_info(
    url: str, info_to_extract: str, custom_headers: Dict[str, str] = None
):
    """
    Scrape content from a URL, including web pages, PDFs, code files, and other supported resources, and extract meaningful information using an LLM.
    If you need to extract information from a PDF, please use this tool.

    Args:
        url (str): The URL to scrape content from. Supports various types of URLs such as web pages, PDFs, raw text/code files (e.g., GitHub, Gist), and similar sources.
        info_to_extract (str): The specific types of information to extract (usually a question)
        custom_headers (Dict[str, str]): Additional headers to include in the scraping request

    Returns:
        Dict[str, Any]: A dictionary containing:
            - success (bool): Whether the operation was successful
            - url (str): The original URL
            - extracted_info (str): The extracted information
            - error (str): Error message if the operation failed
            - scrape_stats (Dict): Statistics about the scraped content
            - model_used (str): The model used for summarization
            - tokens_used (int): Number of tokens used (if available)
    """
    logger.info(f'#z 进入scrape_and_extract_info url={url},info_to_extract={info_to_extract}')
    result = await _scrape_and_extract_single(url, info_to_extract, custom_headers)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def scrape_and_extract_info_multi(
    urls: list, info_to_extract: str, custom_headers: Dict[str, str] = None
):
    """
    Concurrently scrape content from multiple URLs and extract meaningful information using an LLM.
    All URLs are fetched in parallel, which reduces total wait time compared to sequential scraping.
    Use this tool when you have identified several candidate URLs and want to process them together
    in a single tool call, keeping the ReAct reasoning loop clean.

    Args:
        urls (list): A list of URLs to scrape. Each URL supports web pages, PDFs, raw text/code
                     files (e.g., GitHub, Gist), and similar sources.
        info_to_extract (str): The specific information to extract from each page (usually a question).
                               The same extraction query is applied to every URL.
        custom_headers (Dict[str, str]): Optional additional HTTP headers for all scraping requests.

    Returns:
        JSON string with:
            - results (list): Per-URL result objects, each containing:
                - success (bool): Whether this URL was scraped and extracted successfully
                - url (str): The URL
                - extracted_info (str): Extracted information (empty string on failure)
                - error (str): Error message if the URL failed
                - scrape_stats (Dict): Scraping statistics
                - model_used (str): Summarization model name
                - tokens_used (int): LLM tokens consumed
            - summary (Dict):
                - total (int): Total number of URLs attempted
                - succeeded (int): Number of successful extractions
                - failed (int): Number of failed extractions
                - succeeded_urls (list): URLs that succeeded
                - failed_urls (list): URLs that failed
    """
    logger.info(f'#z 进入scrape_and_extract_info_multi urls={urls}, info_to_extract={info_to_extract}')

    if not urls:
        return json.dumps(
            {
                "results": [],
                "summary": {
                    "total": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "succeeded_urls": [],
                    "failed_urls": [],
                },
            },
            ensure_ascii=False,
        )

    # Launch all URL scraping tasks concurrently
    tasks = [
        _scrape_and_extract_single(url, info_to_extract, custom_headers)
        for url in urls
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    succeeded_urls = []
    failed_urls = []

    for url, raw in zip(urls, raw_results):
        if isinstance(raw, Exception):
            # Unexpected exception from gather — treat as failure
            logger.error(f'#z scrape_and_extract_info_multi: exception for url={url}: {raw}')
            result = {
                "success": False,
                "url": url,
                "extracted_info": "",
                "error": f"Unexpected error: {str(raw)}",
                "scrape_stats": {},
                "model_used": SUMMARY_LLM_MODEL_NAME,
                "tokens_used": 0,
            }
        else:
            result = raw

        results.append(result)
        if result.get("success"):
            succeeded_urls.append(url)
        else:
            failed_urls.append(url)

    return json.dumps(
        {
            "results": results,
            "summary": {
                "total": len(urls),
                "succeeded": len(succeeded_urls),
                "failed": len(failed_urls),
                "succeeded_urls": succeeded_urls,
                "failed_urls": failed_urls,
            },
        },
        ensure_ascii=False,
    )


def _is_huggingface_dataset_or_space_url(url):
    """
    Check if the URL is a HuggingFace dataset or space URL.
    :param url: The URL to check
    :return: True if it's a HuggingFace dataset or space URL, False otherwise
    """
    if not url:
        return False
    return "huggingface.co/datasets" in url or "huggingface.co/spaces" in url


async def scrape_url_with_jina(
    url: str, custom_headers: Dict[str, str] = None, max_chars: int = 102400 * 4
) -> Dict[str, Any]:
    """
    Scrape content from a URL and save to a temporary file. Need to read the content from the temporary file.


    Args:
        url (str): The URL to scrape content from
        custom_headers (Dict[str, str]): Additional headers to include in the request
        max_chars (int): Maximum number of characters to reserve for the scraped content

    Returns:
        Dict[str, Any]: A dictionary containing:
            - success (bool): Whether the operation was successful
            - filename (str): Absolute path to the temporary file containing the scraped content
            - content (str): The scraped content of the first 40k characters
            - error (str): Error message if the operation failed
            - line_count (int): Number of lines in the scraped content
            - char_count (int): Number of characters in the scraped content
            - last_char_line (int): Line number where the last displayed character is located
            - all_content_displayed (bool): Signal indicating if all content was displayed (True if content <= 40k chars)
    """

    # Validate input
    if not url or not url.strip():
        return {
            "success": False,
            "filename": "",
            "content": "",
            "error": "URL cannot be empty",
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    # Get API key from environment
    if not JINA_API_KEY:
        return {
            "success": False,
            "filename": "",
            "content": "",
            "error": "JINA_API_KEY environment variable is not set",
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    # Avoid duplicate Jina URL prefix
    if url.startswith("https://r.jina.ai/") and url.count("http") >= 2:
        url = url[len("https://r.jina.ai/") :]

    # Construct the Jina.ai API URL
    jina_url = f"{JINA_BASE_URL}/{url}"
    # print(f'jina_url = {jina_url}')

    try:
        # Prepare headers
        headers = {
            "Authorization": f"Bearer {JINA_API_KEY}",
        }

        # Add custom headers if provided
        if custom_headers:
            headers.update(custom_headers)

        # Retry configuration
        retry_delays = [1, 2, 4]#[1, 2, 4, 8]
        client = _get_httpx_client()
        
        for attempt, delay in enumerate(retry_delays, 1):
            try:
                # Make the request using httpx library
                
                # async with httpx.AsyncClient() as client:
                response = await client.get(
                        jina_url,
                        headers=headers,
                        timeout=httpx.Timeout(30), #直接设置总超时30 #httpx.Timeout(None, connect=20, read=60), #connect=20：连接超时 20 秒,读取超时 60 秒
                        follow_redirects=True,  # Follow redirects (equivalent to curl -L)
                    )

                # Check if request was successful
                response.raise_for_status()
                break  # Success, exit retry loop

            except httpx.ConnectTimeout as e:
                # connection timeout, retry
                if attempt < len(retry_delays):
                    logger.info(
                        f"Jina Scrape: Connection timeout, {delay}s before next attempt (attempt {attempt + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Jina Scrape: Connection retry attempts exhausted, url: {url}"
                    )
                    raise e

            except httpx.ConnectError as e:
                # connection error, retry
                if attempt < len(retry_delays):
                    logger.info(
                        f"Jina Scrape: Connection error: {e}, {delay}s before next attempt"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Jina Scrape: Connection retry attempts exhausted, url: {url}"
                    )
                    raise e

            except httpx.ReadTimeout as e:
                # read timeout, retry
                if attempt < len(retry_delays):
                    logger.info(
                        f"Jina Scrape: Read timeout, {delay}s before next attempt (attempt {attempt + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Jina Scrape: Read timeout retry attempts exhausted, url: {url}"
                    )
                    raise e

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code

                # Retryable: 5xx (server errors) + specific 4xx (408, 409, 425, 429)
                should_retry = status_code >= 500 or status_code in [408, 409, 425, 429]

                if should_retry and attempt < len(retry_delays):
                    logger.info(
                        f"Jina Scrape: HTTP {status_code} (retryable), retry in {delay}s, url: {url}"
                    )
                    await asyncio.sleep(delay)
                    continue
                elif should_retry:
                    logger.error(
                        f"Jina Scrape: HTTP {status_code} retry exhausted, url: {url}"
                    )
                    raise e
                else:
                    logger.error(
                        f"Jina Scrape: HTTP {status_code} (non-retryable), url: {url}"
                    )
                    raise e

            except httpx.RequestError as e:
                if attempt < len(retry_delays):
                    logger.info(
                        f"Jina Scrape: Unknown request exception: {e}, url: {url}, {delay}s before next attempt (attempt {attempt + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Jina Scrape: Unknown request exception retry attempts exhausted, url: {url}"
                    )
                    raise e

    except Exception as e:
        error_msg = f"Jina Scrape: Unexpected error occurred: {str(e)}"
        logger.error(error_msg)
        return {
            "success": False,
            "filename": "",
            "content": "",
            "error": error_msg,
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    # Get the scraped content
    content = response.text

    if not content:
        return {
            "success": False,
            "filename": "",
            "content": "",
            "error": "No content returned from Jina.ai API",
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    # handle insufficient balance error
    try:
        content_dict = json.loads(content)
    except json.JSONDecodeError:
        content_dict = None
    if (
        isinstance(content_dict, dict)
        and content_dict.get("name") == "InsufficientBalanceError"
    ):
        return {
            "success": False,
            "filename": "",
            "content": "",
            "error": "Insufficient balance",
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    # Get content statistics
    total_char_count = len(content)
    total_line_count = content.count("\n") + 1 if content else 0

    # Extract first max_chars characters
    displayed_content = content[:max_chars]
    all_content_displayed = total_char_count <= max_chars

    # Calculate the line number of the last character displayed
    if displayed_content:
        # Count newlines up to the last displayed character
        last_char_line = displayed_content.count("\n") + 1
    else:
        last_char_line = 0

    return {
        "success": True,
        "content": displayed_content,
        "error": "",
        "line_count": total_line_count,
        "char_count": total_char_count,
        "last_char_line": last_char_line,
        "all_content_displayed": all_content_displayed,
    }


async def scrape_url_with_python(
    url: str, custom_headers: Dict[str, str] = None, max_chars: int = 102400 * 4
) -> Dict[str, Any]:
    """
    Fallback scraping method using Python's httpx library directly.

    Args:
        url (str): The URL to scrape content from
        custom_headers (Dict[str, str]): Additional headers to include in the request
        max_chars (int): Maximum number of characters to reserve for the scraped content

    Returns:
        Dict[str, Any]: A dictionary containing:
            - success (bool): Whether the operation was successful
            - content (str): The scraped content
            - error (str): Error message if the operation failed
            - line_count (int): Number of lines in the scraped content
            - char_count (int): Number of characters in the scraped content
            - last_char_line (int): Line number where the last displayed character is located
            - all_content_displayed (bool): Signal indicating if all content was displayed
    """
    # Validate input
    if not url or not url.strip():
        return {
            "success": False,
            "content": "",
            "error": "URL cannot be empty",
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    try:
        # Prepare headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        # Add custom headers if provided
        if custom_headers:
            headers.update(custom_headers)

        # Retry configuration
        retry_delays = [1, 2, 4]
        client = _get_httpx_client()
        for attempt, delay in enumerate(retry_delays, 1):
            try:
                # Make the request using httpx library
                # async with httpx.AsyncClient() as client:
                response = await client.get(
                        url,
                        headers=headers,
                        timeout=httpx.Timeout(None, connect=20, read=60),
                        follow_redirects=True,
                    )

                # Check if request was successful
                response.raise_for_status()
                break  # Success, exit retry loop

            except httpx.ConnectTimeout as e:
                if attempt < len(retry_delays):
                    logger.info(
                        f"Python Scrape: Connection timeout, {delay}s before next attempt (attempt {attempt + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Python Scrape: Connection retry attempts exhausted, url: {url}"
                    )
                    raise e

            except httpx.ConnectError as e:
                if attempt < len(retry_delays):
                    logger.info(
                        f"Python Scrape: Connection error: {e}, {delay}s before next attempt"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Python Scrape: Connection retry attempts exhausted, url: {url}"
                    )
                    raise e

            except httpx.ReadTimeout as e:
                if attempt < len(retry_delays):
                    logger.info(
                        f"Python Scrape: Read timeout, {delay}s before next attempt (attempt {attempt + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Python Scrape: Read timeout retry attempts exhausted, url: {url}"
                    )
                    raise e

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code

                # Retryable: 5xx (server errors) + specific 4xx (408, 409, 425, 429)
                should_retry = status_code >= 500 or status_code in [408, 409, 425, 429]

                if should_retry and attempt < len(retry_delays):
                    logger.info(
                        f"Python Scrape: HTTP {status_code} (retryable), retry in {delay}s, url: {url}"
                    )
                    await asyncio.sleep(delay)
                    continue
                elif should_retry:
                    logger.error(
                        f"Python Scrape: HTTP {status_code} retry exhausted, url: {url}"
                    )
                    raise e
                else:
                    logger.error(
                        f"Python Scrape: HTTP {status_code} (non-retryable), url: {url}"
                    )
                    raise e

            except httpx.RequestError as e:
                if attempt < len(retry_delays):
                    logger.info(
                        f"Python Scrape: Unknown request exception: {e}, url: {url}, {delay}s before next attempt (attempt {attempt + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        f"Python Scrape: Unknown request exception retry attempts exhausted, url: {url}"
                    )
                    raise e

    except Exception as e:
        error_msg = f"Python Scrape: Unexpected error occurred: {str(e)}"
        logger.error(error_msg)
        return {
            "success": False,
            "content": "",
            "error": error_msg,
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    # Get the scraped content
    content = response.text

    if not content:
        return {
            "success": False,
            "content": "",
            "error": "No content returned from URL",
            "line_count": 0,
            "char_count": 0,
            "last_char_line": 0,
            "all_content_displayed": False,
        }

    # Get content statistics
    total_char_count = len(content)
    total_line_count = content.count("\n") + 1 if content else 0

    # Extract first max_chars characters
    displayed_content = content[:max_chars]
    all_content_displayed = total_char_count <= max_chars

    # Calculate the line number of the last character displayed
    if displayed_content:
        last_char_line = displayed_content.count("\n") + 1
    else:
        last_char_line = 0

    return {
        "success": True,
        "content": displayed_content,
        "error": "",
        "line_count": total_line_count,
        "char_count": total_char_count,
        "last_char_line": last_char_line,
        "all_content_displayed": all_content_displayed,
    }


EXTRACT_INFO_PROMPT = """You are given a piece of content and the requirement of information to extract. Your task is to extract the information specifically requested. Be precise and focus exclusively on the requested information.

INFORMATION TO EXTRACT:
{}

INSTRUCTIONS:
1. Extract the information relevant to the focus above.
2. If the exact information is not found, extract the most closely related details.
3. Be specific and include exact details when available.
4. Clearly organize the extracted information for easy understanding.
5. Do not include general summaries or unrelated content.

CONTENT TO ANALYZE:
{}

EXTRACTED INFORMATION:"""


def get_prompt_with_truncation(
    info_to_extract: str, content: str, truncate_last_num_chars: int = -1, model_max_len: int = -1
) -> str:
    oririn_len = len(content)
    trunc_flag = "[...truncated]"
    if truncate_last_num_chars > 0:
        logger.info(f'#z 原始truncated')
        content = content[:-truncate_last_num_chars] + trunc_flag
        
    if model_max_len > 0:
        logger.info(f'#z 加固后truncated')
        content = truncate_content_for_model(content, info_to_extract, model_max_len - 128, EXTRACT_INFO_PROMPT) #z 128 安全设置
        if len(content) > len(trunc_flag):
            content = content[:len(content) - len(trunc_flag) ] + trunc_flag
        else:
            pass # 无需操作
    if len(content)  < oririn_len:
        logger.info(f'#z 原文长度超过模型处理长度({model_max_len})，已裁剪')
             
    # Prepare the prompt
    prompt = EXTRACT_INFO_PROMPT.format(info_to_extract, content)
    return prompt


async def extract_info_with_llm(
    url: str,
    content: str,
    info_to_extract: str,
    model: str = "LLM",
    max_tokens: int = 4096,
) -> Dict[str, Any]:
    """
    Summarize content using an LLM API.

    Args:
        content (str): The content to summarize
        info_to_extract (str): The specific types of information to extract (usually a question) #z 比如 提取关于中国债券市场人工智能应用的核心挑战、受益环节和潜在风险的信息
        model (str): The model to use for summarization
        max_tokens (int): Maximum tokens for the response

    Returns:
        Dict[str, Any]: A dictionary containing:
            - success (bool): Whether the operation was successful
            - extracted_info (str): The extracted information
            - error (str): Error message if the operation failed
            - model_used (str): The model used for summarization
            - tokens_used (int): Number of tokens used (if available)
    """

    # Validate input
    if not content or not content.strip():
        return {
            "success": False,
            "extracted_info": "",
            "error": "Content cannot be empty",
            "model_used": model,
            "tokens_used": 0,
        }

    # content 清洗:已经出现上游传入的有很多垃圾html标签
    if '<html>' in content or '<!DOCTYPE html>' in content:
        logger.info(f'#z 包含html标签，进行清洗')
        content = extract_webpage_content(content, is_url=False)
    
    prompt = get_prompt_with_truncation(info_to_extract, content, model_max_len = SUMMARY_LLM_MAX_LEN)
    # print('#z:extract_info_with_llm model name:{model}')
    # Prepare the payload
    if "gpt" in model:
        payload = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }
        # Add cost-saving parameters for GPT-5 models
        if "gpt-5" in model.lower() or "gpt5" in model.lower():
            payload["service_tier"] = "flex"
            payload["reasoning_effort"] = "minimal"
    else:
        if "Qwen3" in model:
            prompt = '/no_think\n'  + prompt
        
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "temperature": 1.0,
            # "top_p": 0.8,
            # "top_k": 20,
        }

    # Validate LLM endpoint configuration early for clearer errors
    if not SUMMARY_LLM_BASE_URL or not SUMMARY_LLM_BASE_URL.strip():
        return {
            "success": False,
            "extracted_info": "",
            "error": "SUMMARY_LLM_BASE_URL environment variable is not set",
            "model_used": model,
            "tokens_used": 0,
        }

    # Build the complete API endpoint URL
    # If SUMMARY_LLM_BASE_URL doesn't already include /chat/completions, append it
    api_url = SUMMARY_LLM_BASE_URL.strip()
    if "/chat/completions" not in api_url:
        # Ensure proper URL formatting
        if api_url.endswith("/"):
            api_url = api_url.rstrip("/")
        api_url = f"{api_url}/chat/completions"

    # Prepare headers (add Authorization if API key is available)
    headers = {"Content-Type": "application/json"}
    if SUMMARY_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {SUMMARY_LLM_API_KEY}"

    try:
        # Retry configuration
        connect_retry_delays = [1, 2, 4, 8]
        client = _get_httpx_client()
        for attempt, delay in enumerate(connect_retry_delays, 1):
            try:
                # Make the API request using httpx
                # async with httpx.AsyncClient() as client:
                response = await client.post(
                        api_url,
                        headers=headers,
                        json=payload,
                        timeout=httpx.Timeout(None, connect=30, read=300),
                    )
                if response.text and len(response.text) >= 50:
                    tail_50 = response.text[-50:]
                    repeat_count = response.text.count(tail_50)
                    if repeat_count > 5:
                        logger.info("Repeat detected in extract_info_with_llm")
                        continue

                # Check if the request was successful
                if (
                    "Requested token count exceeds the model's maximum context length"
                    in response.text
                    or "longer than the model's context length" in response.text
                ):
                    prompt = get_prompt_with_truncation(
                        info_to_extract,
                        content,
                        truncate_last_num_chars=40960 * attempt,model_max_len = SUMMARY_LLM_MAX_LEN
                    )  # remove 40k * num_attempts chars from the end of the content
                    payload["messages"][0]["content"] = prompt
                    continue  # no need to raise error here, just try again

                response.raise_for_status()
                break  # Success, exit retry loop

            except httpx.ConnectTimeout as e:
                # connection timeout, retry
                if attempt < len(connect_retry_delays):
                    logger.info(
                        f"Jina Scrape and Extract Info: Connection timeout, {delay}s before next attempt (attempt {attempt + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Jina Scrape and Extract Info: Connection retry attempts exhausted"
                    )
                    raise e

            except httpx.ConnectError as e:
                # connection error, retry
                if attempt < len(connect_retry_delays):
                    logger.info(
                        f"Jina Scrape and Extract Info: Connection error: {e}, {delay}s before next attempt"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Jina Scrape and Extract Info: Connection retry attempts exhausted"
                    )
                    raise e

            except httpx.ReadTimeout as e:
                # read timeout, LLM API is too slow, no need to retry
                if attempt < len(connect_retry_delays):
                    logger.info(
                        f"Jina Scrape and Extract Info: LLM API attempt {attempt} read timeout"
                    )
                    continue
                else:
                    logger.error(
                        f"Jina Scrape and Extract Info: LLM API read timeout retry attempts exhausted, please check the request complexity, information to extract: {info_to_extract}, length of content: {len(content)}, url: {url}"
                    )
                    raise e

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code

                # Special case: GPT-5 service_tier parameter compatibility issue
                if (
                    "gpt-5" in model.lower() or "gpt5" in model.lower()
                ) and "service_tier" in payload:
                    logger.info(
                        "Extract Info: GPT-5 service_tier error, removing and retrying"
                    )
                    payload.pop("service_tier", None)
                    if attempt < len(connect_retry_delays):
                        await asyncio.sleep(delay)
                        continue

                # Retryable: 5xx (server errors) + specific 4xx (408, 409, 425, 429)
                should_retry = status_code >= 500 or status_code in [408, 409, 425, 429]

                if should_retry and attempt < len(connect_retry_delays):
                    logger.info(
                        f"Extract Info: HTTP {status_code} (retryable), retry in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                elif should_retry:
                    logger.error(f"Extract Info: HTTP {status_code} retry exhausted")
                    raise e
                else:
                    logger.error(f"Extract Info: HTTP {status_code} (non-retryable)")
                    raise httpx.HTTPStatusError(
                        f"response.text: {response.text}",
                        request=e.request,
                        response=e.response,
                    ) from e

            except httpx.RequestError as e:
                logger.error(
                    f"Jina Scrape and Extract Info: Unknown request exception: {e}"
                )
                raise e

    except Exception as e:
        error_msg = f"Jina Scrape and Extract Info: Unexpected error during LLM API call: {str(e)}"
        logger.error(error_msg)
        return {
            "success": False,
            "extracted_info": "",
            "error": error_msg,
            "model_used": model,
            "tokens_used": 0,
        }

    # Parse the response
    try:
        response_data = response.json()
        logger.info(f'#z mcp工具调用-摘要模型返回:\n{response_data}\n')

    except json.JSONDecodeError as e:
        error_msg = (
            f"Jina Scrape and Extract Info: Failed to parse LLM API response: {str(e)}"
        )
        logger.error(error_msg)
        logger.error(f"Raw response: {response.text}")
        return {
            "success": False,
            "extracted_info": "",
            "error": error_msg,
            "model_used": model,
            "tokens_used": 0,
        }

    # Extract summary from response
    if "choices" in response_data and len(response_data["choices"]) > 0:
        try:
            summary = response_data["choices"][0]["message"]["content"]
        except Exception as e:
            error_msg = f"Jina Scrape and Extract Info: Failed to get summary from LLM API response: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "extracted_info": "",
                "error": error_msg,
                "model_used": model,
                "tokens_used": 0,
            }

        # Extract token usage if available
        tokens_used = 0
        if "usage" in response_data:
            tokens_used = response_data["usage"].get("total_tokens", 0)

        return {
            "success": True,
            "extracted_info": summary, #z: Extracted Information:\n\n1. AI Market and Industrial Policy Goals (2025-2030):\n\n2. No Specific Quantitative Forecasts (2025–2028)...
            "error": "",
            "model_used": model,
            "tokens_used": tokens_used,
        }
    elif "error" in response_data:
        error_msg = (
            f"Jina Scrape and Extract Info: LLM API error: {response_data['error']}"
        )
        logger.error(error_msg)
        return {
            "success": False,
            "extracted_info": "",
            "error": error_msg,
            "model_used": model,
            "tokens_used": 0,
        }
    else:
        error_msg = f"Jina Scrape and Extract Info: No valid response from LLM API, response data: {response_data}"
        logger.error(error_msg)
        return {
            "success": False,
            "extracted_info": "",
            "error": error_msg,
            "model_used": model,
            "tokens_used": 0,
        }


if __name__ == "__main__":
    # Example usage and testing

    # Run the MCP server
    # mcp.run(transport="stdio", show_banner=False)

    parser = argparse.ArgumentParser(description="Jina Scrape MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport method: 'stdio' or 'http' (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8003,
        help="Port to use when running with HTTP transport (default: 8003)",
    )
    parser.add_argument(
        "--path",
        type=str,
        default="/mcp",
        help="URL path to use when running with HTTP transport (default: /mcp)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(
            transport="streamable-http",
            port=args.port,
            path=args.path,
            show_banner=False,
        )
