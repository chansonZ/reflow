# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0
import argparse
import json
import logging
import os
from typing import Any, Dict

import httpx
from fastmcp import FastMCP
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .utils.web_url_state import (
    add_search_round,
    decide_url_status,
    load_url_state,
    normalize_url,
    save_url_state,
    upsert_url_record,
)
# Temporarily disabled: Sogou search functionality
# from tencentcloud.common import credential
# from tencentcloud.common.common_client import CommonClient
# from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
#     TencentCloudSDKException,
# )
# from tencentcloud.common.profile.client_profile import ClientProfile
# from tencentcloud.common.profile.http_profile import HttpProfile

from .utils.url_unquote import decode_http_urls_in_dict

# Configure logging
logger = logging.getLogger("miroflow")

SERPER_BASE_URL = os.getenv("SERPER_BASE_URL", "https://google.serper.dev")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")

# Temporarily disabled: Sogou search functionality
# TENCENTCLOUD_SECRET_ID = os.getenv("TENCENTCLOUD_SECRET_ID", "")
# TENCENTCLOUD_SECRET_KEY = os.getenv("TENCENTCLOUD_SECRET_KEY", "")

# Initialize FastMCP server
mcp = FastMCP("tool-serper-search")


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type(
        (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError)
    ),
)
async def make_serper_request(
    payload: Dict[str, Any], headers: Dict[str, str]
) -> httpx.Response:
    """Make HTTP request to Serper API with retry logic."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SERPER_BASE_URL}/search",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return response


def _is_huggingface_dataset_or_space_url(url):
    """
    Check if the URL is a HuggingFace dataset or space URL.
    :param url: The URL to check
    :return: True if it's a HuggingFace dataset or space URL, False otherwise
    """
    if not url:
        return False
    return "huggingface.co/datasets" in url or "huggingface.co/spaces" in url


@mcp.tool()
async def google_search(
    q: str,
    gl: str = "us",
    hl: str = "en",
    location: str = None,
    num: int = None, #5, #原来None
    tbs: str = None,
    page: int = None,
    autocorrect: bool = None,
):
    """
    Tool to perform web searches via Serper API and retrieve rich results.

    It is able to retrieve organic search results, people also ask,
    related searches, and knowledge graph.

    Args:
        q: Search query string
        gl: Optional region code for search results in ISO 3166-1 alpha-2 format (e.g., 'us')
        hl: Optional language code for search results in ISO 639-1 format (e.g., 'en')
        location: Optional location for search results (e.g., 'SoHo, New York, United States', 'California, United States')
        num: Number of results to return (default: 10)
        tbs: Time-based search filter ('qdr:h' for past hour, 'qdr:d' for past day, 'qdr:w' for past week, 'qdr:m' for past month, 'qdr:y' for past year)
        page: Page number of results to return (default: 1)
        autocorrect: Whether to autocorrect spelling in query

    Returns:
        Dictionary containing search results and metadata.
    """
    # Check for API key
    if not SERPER_API_KEY:
        return json.dumps(
            {
                "success": False,
                "error": "SERPER_API_KEY environment variable not set",
                "results": [],
            },
            ensure_ascii=False,
        )

    # Validate required parameter
    if not q or not q.strip():
        return json.dumps(
            {
                "success": False,
                "error": "Search query 'q' is required and cannot be empty",
                "results": [],
            },
            ensure_ascii=False,
        )

    try:
        # Helper function to perform a single search
        async def perform_search(search_query: str) -> tuple[list, dict]:
            """Perform a search and return organic results and search parameters."""
            # Build payload with all supported parameters
            payload: dict[str, Any] = {
                "q": search_query.strip(),
                "gl": gl,
                "hl": hl,
            }

            # Add optional parameters if provided
            if location:
                payload["location"] = location
            if num is not None:
                payload["num"] = num
            else:
                payload["num"] = 10  # Default
            if tbs:
                payload["tbs"] = tbs
            if page is not None:
                payload["page"] = page
            if autocorrect is not None:
                payload["autocorrect"] = autocorrect

            # Set up headers
            headers = {
                "X-API-KEY": SERPER_API_KEY,
                "Content-Type": "application/json",
            }

            # Make the API request
            response = await make_serper_request(payload, headers)
            data = response.json()

            # filter out HuggingFace dataset or space urls
            organic_results = []
            if "organic" in data:
                for item in data["organic"]:
                    if _is_huggingface_dataset_or_space_url(item.get("link", "")):
                        continue
                    organic_results.append(item)

            return organic_results, data.get("searchParameters", {})

        # Perform initial search
        original_query = q.strip()
        organic_results, search_params = await perform_search(original_query)

        # If no results and query contains quotes, retry without quotes
        if not organic_results and '"' in original_query:
            # Remove all types of quotes
            query_without_quotes = original_query.replace('"', "").strip()
            if query_without_quotes:  # Make sure we still have a valid query
                organic_results, search_params = await perform_search(
                    query_without_quotes
                )

        # Build comprehensive response
        state = load_url_state()
        enriched_results = []
        eligible_urls = []
        duplicate_urls = []
        blacklisted_urls = []
        for index, item in enumerate(organic_results, start=1):
            raw_url = item.get("link", "")
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            decision = decide_url_status(
                state=state,
                url=raw_url,
                title=title,
                snippet=snippet,
            )
            enriched_item = dict(item)
            enriched_item["canonical_url"] = decision.canonical_url
            enriched_item["clean_status"] = decision.status
            enriched_item["clean_reason"] = decision.reason
            if decision.matched_url:
                enriched_item["matched_url"] = decision.matched_url
            if decision.similarity_score:
                enriched_item["similarity_score"] = round(
                    decision.similarity_score, 4
                )

            if decision.status in {"eligible", "duplicate_exact", "duplicate_similar"}:
                upsert_url_record(
                    state=state,
                    canonical_url=decision.canonical_url or normalize_url(raw_url),
                    title=title,
                    snippet=snippet,
                    source_query=original_query,
                    source_rank=index,
                    metadata={
                        "position": item.get("position"),
                        "displayed_link": item.get("displayedLink"),
                        "date": item.get("date"),
                    },
                )

            if decision.status == "eligible":
                eligible_urls.append(decision.canonical_url)
            elif decision.status in {"duplicate_exact", "duplicate_similar"}:
                duplicate_urls.append(
                    {
                        "url": decision.canonical_url,
                        "matched_url": decision.matched_url,
                        "status": decision.status,
                    }
                )
            elif decision.status == "blacklisted":
                blacklisted_urls.append(decision.canonical_url)

            enriched_results.append(enriched_item)

        add_search_round(
            state=state,
            query=original_query,
            search_params=search_params,
            results=enriched_results,
        )
        state_path = save_url_state(state)
        print(f'#z: state_path= {state_path}')

        response_data = {
            "organic": enriched_results,
            "searchParameters": search_params,
            "url_cleaning": {
                "state_file": str(state_path),
                "total_results": len(enriched_results),
                "eligible_urls": eligible_urls,
                "duplicate_urls": duplicate_urls,
                "blacklisted_urls": blacklisted_urls,
            },
        }
        response_data = decode_http_urls_in_dict(response_data)
        print(f'#z mcp工具调用-google_search返回:\n{response_data}\n')
        return json.dumps(response_data, ensure_ascii=False)

    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
                "results": [],
            },
            ensure_ascii=False,
        )


if __name__ == "__main__":
    # mcp.run(show_banner=False)
    parser = argparse.ArgumentParser(description="Serper Search MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport method: 'stdio' or 'http' (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port to use when running with HTTP transport (default: 8001)",
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
