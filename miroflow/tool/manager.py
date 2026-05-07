# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool manager module - manages and executes MCP tool calls

Note: Tools are dynamically discovered through the MCP protocol, not the registry
"""

import asyncio
import contextlib
import functools
from typing import Any, Awaitable, Callable, TypeVar,Optional, List

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from miroflow.logging.task_tracer import get_tracer
from .mcp_servers.browser_session import PlaywrightSession
from miroflow.utils.tool_utils import format_tool_result
from miroflow.logging.decorators import span
from .factory import get_mcp_server_configs_from_tool_cfg_paths

logger = get_tracer()

R = TypeVar("R")


def update_server_params_with_context_var(
    server_params: StdioServerParameters,
) -> StdioServerParameters:
    """
    Update the server params with the context var.
    """
    from miroflow.logging.task_tracer import get_current_task_context_var

    task_context_var = get_current_task_context_var()
    if task_context_var is not None:
        server_params.env["TASK_ID"] = task_context_var.task_id
        #z 
        server_params.env["TASK_ATTEMPT_ID"] = str(task_context_var.attempt_id)
        server_params.env["TASK_RETRY_ID"] = str(task_context_var.retry_id)
        server_params.env["TASK_CONTEXT_KEY"] = str(task_context_var)
    server_params.env["TASK_LOG_DIR"] = str(logger.log_path)
    return server_params


def with_timeout(timeout_s: float = 300.0):
    """
    Decorator: wraps any *async* function in asyncio.wait_for().
    Usage:
        @with_timeout(20)
        async def create_message_foo(...): ...
    """

    def decorator(
        func: Callable[..., Awaitable[R]],
    ) -> Callable[..., Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> R:
            return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout_s)

        return wrapper

    return decorator


class ToolManager:
    def __init__(
        self, cfg: Optional[List[str]] = None, server_configs=None, tool_blacklist=None
    ):
        """
        Initialize ToolManager.
        :param cfg: List of tool configuration file paths. If provided, will be used to generate server_configs.
        :param server_configs: List returned by create_server_parameters(). Used only if cfg is not provided (for backward compatibility).
        :param tool_blacklist: Optional set of (server_name, tool_name) tuples to blacklist.
        """
        # If cfg is provided, use it to generate server_configs
        if cfg is not None:
            server_configs = get_mcp_server_configs_from_tool_cfg_paths(cfg)
        elif server_configs is None:
            server_configs = []

        self.server_configs = server_configs
        self.server_dict = {
            config["name"]: config["params"] for config in server_configs
        }
        self.browser_session = None
        self.tool_blacklist = tool_blacklist if tool_blacklist else set()
        self._persistent_sessions: dict[str, ClientSession] = {} #z
        self._exit_stack: contextlib.AsyncExitStack | None = None #z

        logger.info(
            f"ToolManager initialized, loaded servers: {list(self.server_dict.keys())}"
        )
        if self.tool_blacklist:
            logger.info(f"Tool blacklist configured: {self.tool_blacklist}")


    #z start
    async def start_all_servers(self) -> None:
        """Start all configured MCP servers and create persistent sessions.

        Each server process is launched once and its ClientSession is kept alive
        for the lifetime of the agent run so that subsequent tool calls and tool
        definition queries can reuse the connection without the per-call overhead
        of spawning a new subprocess.

        The playwright server is intentionally excluded because PlaywrightSession
        already manages its own persistent connection lifecycle.
        Supports three transport modes (determined by the type of server_params):
          * StdioServerParameters  → stdio subprocess (local)
          * dict {"url": ..., "transport": "streamable-http"} → Streamable HTTP
          * dict {"url": ..., "transport": "sse"} → legacy SSE
          * str (legacy) → treated as SSE URL for backward compatibility
        """
        if self._exit_stack is not None:
            # Already started; nothing to do
            return

        self._exit_stack = contextlib.AsyncExitStack()
        await self._exit_stack.__aenter__()

        for config in self.server_configs:
            server_name = config["name"]
            server_params = config["params"]

            # PlaywrightSession handles its own lifecycle; skip here
            if server_name == "playwright":
                continue

            try:
                if isinstance(server_params, StdioServerParameters):
                    read, write = await self._exit_stack.enter_async_context(
                        stdio_client(
                            update_server_params_with_context_var(server_params)
                        )
                    )
                elif isinstance(server_params, dict) and "url" in server_params:
                    transport = server_params.get("transport", "streamable-http")
                    url = server_params["url"]
                    if transport == "sse":
                        read, write = await self._exit_stack.enter_async_context(
                            sse_client(url)
                        )
                    else:
                        # Default: Streamable HTTP (FastMCP 2.x+ / MCP spec v0.3+)
                        read, write, _ = await self._exit_stack.enter_async_context(
                            streamablehttp_client(url)
                        )
                elif isinstance(server_params, str) and server_params.startswith(
                    ("http://", "https://")
                ):
                    # Legacy: bare URL string → treat as SSE for backward compat
                    read, write = await self._exit_stack.enter_async_context(
                        sse_client(server_params)
                    )
                else:
                    logger.error(
                        f"Unknown server params type for '{server_name}': {type(server_params)}, skipping persistent session"
                    )
                    continue

                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write, sampling_callback=None)
                )
                await session.initialize()
                if isinstance(server_params, dict) and "url" in server_params:
                    # streamable-http 协议不适合持久 session，跳过
                    logger.info(f"HTTP server '{server_name}': skipping persistent session (connect per call)")
                else:
                    self._persistent_sessions[server_name] = session
                    logger.info(
                        f"Persistent MCP session started for server '{server_name}',session={session}"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to start persistent session for server '{server_name}': {e}"
                )

    async def stop_all_servers(self) -> None:
        """Stop all persistent MCP server sessions and release resources.

        Closes every context manager registered in the exit stack (ClientSessions
        and their underlying stdio/SSE transports) and also closes the playwright
        browser session if one was opened.
        """
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.error(f"Error while stopping MCP servers: {e}")
            finally:
                self._exit_stack = None
                self._persistent_sessions.clear()
                logger.info("All persistent MCP server sessions stopped")

        if self.browser_session is not None:
            try:
                await self.browser_session.close()
            except Exception as e:
                logger.error(f"Error while closing browser session: {e}")
            finally:
                self.browser_session = None

    #z end
    
    def _is_huggingface_dataset_or_space_url(self, url):
        """
        Check if the URL is a Hugging Face dataset or space URL.
        :param url: The URL to check
        :return: True if it's a HuggingFace dataset or space URL, False otherwise
        """
        if not url:
            return False
        return "huggingface.co/datasets" in url or "huggingface.co/spaces" in url

    def _should_block_hf_scraping(self, tool_name, arguments):
        """
        Check if we should block scraping of Hugging Face datasets/spaces.
        :param tool_name: The name of the tool being called
        :param arguments: The arguments passed to the tool
        :return: True if scraping should be blocked, False otherwise
        """
        return (
            tool_name == "scrape_and_extract_info" #z 原来的"scrape" 
            and arguments.get("url")
            and self._is_huggingface_dataset_or_space_url(arguments["url"])
        )

    def get_server_params(self, server_name):
        """Get parameters for specified server"""
        return self.server_dict.get(server_name)

    async def _find_servers_with_tool(self, tool_name):
        """
        Find servers containing the specified tool name among all servers
        :param tool_name: Tool name to search for
        :return: List of server names containing the tool
        """
        servers_with_tool = []

        for config in self.server_configs:
            server_name = config["name"]
            server_params = config["params"]

            try:
                if isinstance(server_params, StdioServerParameters):
                    async with stdio_client(
                        update_server_params_with_context_var(server_params)
                    ) as (read, write):
                        async with ClientSession(
                            read, write, sampling_callback=None
                        ) as session:
                            await session.initialize()
                            tools_response = await session.list_tools()
                            # Follow the same blacklist logic as get_all_tool_definitions
                            for tool in tools_response.tools:
                                if (server_name, tool.name) in self.tool_blacklist:
                                    continue
                                if tool.name == tool_name:
                                    servers_with_tool.append(server_name)
                                    break
                elif isinstance(server_params, dict) and "url" in server_params:
                    transport = server_params.get("transport", "streamable-http")
                    url = server_params["url"]
                    if transport == "sse":
                        async with sse_client(url) as (read, write):
                            async with ClientSession(
                                read, write, sampling_callback=None
                            ) as session:
                                await session.initialize()
                                tools_response = await session.list_tools()
                                for tool in tools_response.tools:
                                    if (server_name, tool.name) in self.tool_blacklist:
                                        continue
                                    if tool.name == tool_name:
                                        servers_with_tool.append(server_name)
                                        break
                    else:
                        async with streamablehttp_client(url) as (read, write, _):
                            async with ClientSession(
                                read, write, sampling_callback=None
                            ) as session:
                                await session.initialize()
                                tools_response = await session.list_tools()
                                for tool in tools_response.tools:
                                    if (server_name, tool.name) in self.tool_blacklist:
                                        continue
                                    if tool.name == tool_name:
                                        servers_with_tool.append(server_name)
                                        break
                elif isinstance(server_params, str) and server_params.startswith(
                    ("http://", "https://")
                ):
                    # SSE endpoint
                    # Legacy: bare URL string → SSE for backward compatibility
                    async with sse_client(server_params) as (read, write):
                        async with ClientSession(
                            read, write, sampling_callback=None
                        ) as session:
                            await session.initialize()
                            tools_response = await session.list_tools()
                            for tool in tools_response.tools:
                                # Consistent with get_all_tool_definitions: SSE part has no blacklist processing
                                # Can add specific tool filtering logic here (if needed)
                                # if server_name == "tool-excel" and tool.name not in ["get_workbook_metadata", "read_data_from_excel"]:
                                #     continue
                                if tool.name == tool_name:
                                    servers_with_tool.append(server_name)
                                    break
                else:
                    logger.error(
                        f"Error: Unknown parameter type for server '{server_name}': {type(server_params)}"
                    )
                    # For unknown types, we skip rather than throw an exception, because this is a search function
                    continue
            except Exception as e:
                logger.error(
                    f"Error: Cannot connect or get tools from server '{server_name}' to find '{tool_name}': {e}"
                )
                continue

        return servers_with_tool

    async def get_all_tool_definitions(self) -> list[dict]:
        """
        Connect to all configured servers and get their tool definitions.
        Returns a list suitable for passing to Prompt generators.
        Reuses persistent sessions started by start_all_servers() when available.
        """
        all_servers_for_prompt = []
        # Handle remote server tools
        for config in self.server_configs:
            server_name = config["name"]
            server_params = config["params"]
            one_server_for_prompt = {"name": server_name, "tools": []}
            logger.info(f"Getting tool definitions for server '{server_name}'...")

            try:
                # if isinstance(server_params, StdioServerParameters):
                if server_name in self._persistent_sessions:
                    logger.info(f"#z 使用已经持久化的mcp: '{server_name}'...")
                    session = self._persistent_sessions[server_name]
                    tools_response = await session.list_tools()
                    for tool in tools_response.tools:
                        if (server_name, tool.name) in self.tool_blacklist:
                            logger.info(
                                f"Tool '{tool.name}' in server '{server_name}' is blacklisted, skipping."
                            )
                            continue
                        one_server_for_prompt["tools"].append(
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "schema": tool.inputSchema,
                            }
                        )
                elif isinstance(server_params, StdioServerParameters):
                    async with stdio_client(
                        update_server_params_with_context_var(server_params)
                    ) as (read, write):
                        async with ClientSession(
                            read, write, sampling_callback=None
                        ) as session:
                            await session.initialize()
                            tools_response = await session.list_tools()
                            # black list some tools
                            for tool in tools_response.tools:
                                if (server_name, tool.name) in self.tool_blacklist:
                                    logger.info(
                                        f"Tool '{tool.name}' in server '{server_name}' is blacklisted, skipping."
                                    )
                                    continue
                                one_server_for_prompt["tools"].append(
                                    {
                                        "name": tool.name,
                                        "description": tool.description,
                                        "schema": tool.inputSchema,
                                    }
                                )
                elif isinstance(server_params, dict) and "url" in server_params:
                    transport = server_params.get("transport", "streamable-http")
                    url = server_params["url"]
                    if transport == "sse":
                        async with sse_client(url) as (read, write):
                            async with ClientSession(
                                read, write, sampling_callback=None
                            ) as session:
                                await session.initialize()
                                tools_response = await session.list_tools()
                                for tool in tools_response.tools:
                                    if (server_name, tool.name) in self.tool_blacklist:
                                        continue
                                    one_server_for_prompt["tools"].append(
                                        {
                                            "name": tool.name,
                                            "description": tool.description,
                                            "schema": tool.inputSchema,
                                        }
                                    )
                    else:
                        async with streamablehttp_client(url) as (read, write, _):
                            async with ClientSession(
                                read, write, sampling_callback=None
                            ) as session:
                                await session.initialize()
                                tools_response = await session.list_tools()
                                for tool in tools_response.tools:
                                    if (server_name, tool.name) in self.tool_blacklist:
                                        continue
                                    one_server_for_prompt["tools"].append(
                                        {
                                            "name": tool.name,
                                            "description": tool.description,
                                            "schema": tool.inputSchema,
                                        }
                                    )
                elif isinstance(server_params, str) and server_params.startswith(
                    ("http://", "https://")
                ):
                    # Legacy: bare URL string → SSE for backward compatibility
                    async with sse_client(server_params) as (read, write):
                        async with ClientSession(
                            read, write, sampling_callback=None
                        ) as session:
                            await session.initialize()
                            tools_response = await session.list_tools()
                            for tool in tools_response.tools:
                                # Can add specific tool filtering logic here (if needed)
                                # if server_name == "tool-excel" and tool.name not in ["get_workbook_metadata", "read_data_from_excel"]:
                                #     continue
                                one_server_for_prompt["tools"].append(
                                    {
                                        "name": tool.name,
                                        "description": tool.description,
                                        "schema": tool.inputSchema,
                                    }
                                )
                else:
                    logger.error(
                        f"Error: Unknown parameter type for server '{server_name}': {type(server_params)}"
                    )
                    raise TypeError(
                        f"Unknown server params type for {server_name}: {type(server_params)}"
                    )

                logger.info(
                    f"Successfully obtained {len(one_server_for_prompt['tools'])} tool definitions for server '{server_name}':{one_server_for_prompt['tools']}"
                )
                all_servers_for_prompt.append(one_server_for_prompt)

            except Exception as e:
                logger.error(
                    f"Error: Cannot connect or get tools from server '{server_name}': {e}"
                )
                # Still add server entry, but mark tool list as empty or containing error information
                one_server_for_prompt["tools"] = [
                    {"error": f"Failed to fetch tools: {e}"}
                ]
                all_servers_for_prompt.append(one_server_for_prompt)

        return all_servers_for_prompt
    #z start
    def _extract_tool_result_content(self, tool_result, tool_name: str) -> str:
        """
        Safely extract text content from an MCP tool result, preserving the
        original format.  Logs a warning when the result is empty.

        :param tool_result: The raw tool result object returned by session.call_tool()
        :param tool_name: Tool name (used for log/fallback messages)
        :return: Extracted text content string
        """
        if tool_result.content and len(tool_result.content) > 0:
            text_content = tool_result.content[-1].text
            if text_content is not None and text_content.strip():
                return text_content  # Preserve original format!
            return f"Tool '{tool_name}' completed but returned empty text - this may be expected or indicate an issue"

        logger.error(
            f"Tool '{tool_name}' returned empty content, tool_result.content: {tool_result.content}"
        )
        return f"Tool '{tool_name}' completed but returned no content - this may be expected or indicate an issue"
    #z end
    
    @span()
    @with_timeout(300) #z 原来是900 秒
    async def execute_tool_call(self, server_name, tool_name, arguments) -> Any:
        """
        Execute a single tool call.
        :param server_name: Server name
        :param tool_name: Tool name
        :param arguments: Tool arguments dictionary
        :return: Dictionary containing result or error
        """

        # Original remote server call logic
        server_params = self.get_server_params(server_name)
        if not server_params:
            logger.error(
                f"Error: Attempting to call server '{server_name}' that was not found"
            )

            # Try to find the tool in all available servers
            suggested_servers = await self._find_servers_with_tool(tool_name)

            error_message = f"Server '{server_name}' not found."

            if len(suggested_servers) == 1:
                # Auto-correction: only one server contains the tool, try to auto-correct and execute
                correct_server = suggested_servers[0]
                logger.info(
                    f"Auto-correction: Server '{server_name}' not found, but found tool '{tool_name}' in '{correct_server}', trying to auto-correct and execute"
                )

                try:
                    # Recursive call, using the correct server name
                    corrected_result = await self.execute_tool_call(
                        correct_server, tool_name, arguments
                    )

                    # If auto-correction is successful, add a note in the result
                    if "result" in corrected_result:
                        # 提取原始结果
                        # original_result = corrected_result["result"]
                        # Add auto-correction note in the result, including the reason for the correction
                        correction_note = f"[Auto-corrected: Server '{server_name}' not found, but tool '{tool_name}' was found only in server '{correct_server}', so automatically used '{correct_server}' instead] "
                        corrected_result["result"] = correction_note + str(
                            corrected_result["result"]
                        )
                        print(f'correction_note = {correction_note}')
                        return corrected_result
                    elif "error" in corrected_result:
                        # If there is an error after auto-correction, add a note in the error message
                        correction_note = f"[Auto-corrected: Server '{server_name}' not found, but tool '{tool_name}' was found only in server '{correct_server}', attempted auto-correction but still failed] "
                        corrected_result["error"] = correction_note + str(
                            corrected_result["error"]
                        )
                        print(f'correction_note = {correction_note}')
                        return corrected_result

                except Exception as auto_correct_error:
                    logger.error(f"Auto-correction failed: {auto_correct_error}")
                    error_message += f" Found tool '{tool_name}' in server '{correct_server}' and attempted auto-correction, but it failed: {str(auto_correct_error)}"

            elif len(suggested_servers) > 1:
                error_message += f" However, found tool '{tool_name}' in these servers: {', '.join(suggested_servers)}. You may want to use one of these servers instead."
            else:
                error_message += (
                    " It is possible that the server_name and tool_name were confused or mixed up. "
                    "You should try again and carefully check the server name and tool name provided in the system prompt."
                )

            return {
                "server_name": server_name,
                "tool_name": tool_name,
                "error": error_message,
            }

        logger.info(
            f"Connecting to server '{server_name}' to call tool '{tool_name}'...call arguments: '{arguments}'..."
        )

        if server_name == "playwright":
            try:
                if self.browser_session is None:
                    self.browser_session = PlaywrightSession(server_params)
                    await self.browser_session.connect()
                tool_result = await self.browser_session.call_tool(
                    tool_name, arguments=arguments
                )

                # Check if result is empty and provide better feedback
                if tool_result is None or tool_result == "":
                    logger.error(
                        f"Tool '{tool_name}' returned empty result, this may be normal (such as delete operations) or the tool execution may have issues"
                    )
                    return {
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "result": f"Tool '{tool_name}' returned empty result - this may be expected (e.g., delete operations) or indicate an issue with tool execution",
                    }

                return {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "result": tool_result,
                }
            except Exception as e:
                return {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error": f"Tool call failed: {str(e)}",
                }
        else:
            try:
                result_content = None
                # Reuse persistent session if available
                if server_name in self._persistent_sessions:
                    try:
                        tool_result = await self._persistent_sessions[
                            server_name
                        ].call_tool(tool_name, arguments=arguments)
                        result_content = self._extract_tool_result_content(
                            tool_result, tool_name
                        )
                        if self._should_block_hf_scraping(tool_name, arguments):
                            result_content = "You are trying to scrape a Hugging Face dataset for answers, please do not use the scrape tool for this purpose."
                    except Exception as tool_error:
                        logger.error(f"Tool execution error: {tool_error}")
                        return {
                            "server_name": server_name,
                            "tool_name": tool_name,
                            "error": f"Tool execution failed: {str(tool_error)}",
                        }
                elif isinstance(server_params, StdioServerParameters):
                    async with stdio_client(
                        update_server_params_with_context_var(server_params)
                    ) as (read, write):
                        async with ClientSession(
                            read, write, sampling_callback=None
                        ) as session:
                            await session.initialize()
                            try:
                                tool_result = await session.call_tool(
                                    tool_name, arguments=arguments
                                )
                                result_content = self._extract_tool_result_content(
                                    tool_result, tool_name
                                )
                                if self._should_block_hf_scraping(tool_name, arguments):
                                    result_content = "You are trying to scrape a Hugging Face dataset for answers, please do not use the scrape tool for this purpose."
                            except Exception as tool_error:
                                logger.error(f"Tool execution error: {tool_error}")
                                return {
                                    "server_name": server_name,
                                    "tool_name": tool_name,
                                    "error": f"Tool execution failed: {str(tool_error)}",
                                }
                elif isinstance(server_params, dict) and "url" in server_params:
                    transport = server_params.get("transport", "streamable-http")
                    url = server_params["url"]
                    if transport == "sse":
                        ctx_mgr = sse_client(url)
                        async with ctx_mgr as (read, write):
                            async with ClientSession(
                                read, write, sampling_callback=None
                            ) as session:
                                await session.initialize()
                                try:
                                    tool_result = await session.call_tool(
                                        tool_name, arguments=arguments
                                    )
                                    result_content = self._extract_tool_result_content(
                                        tool_result, tool_name
                                    )
                                    if self._should_block_hf_scraping(tool_name, arguments):
                                        result_content = "You are trying to scrape a Hugging Face dataset for answers, please do not use the scrape tool for this purpose."
                                except Exception as tool_error:
                                    logger.error(f"Tool execution error: {tool_error}")
                                    return {
                                        "server_name": server_name,
                                        "tool_name": tool_name,
                                        "error": f"Tool execution failed: {str(tool_error)}",
                                    }
                    else:
                        async with streamablehttp_client(url) as (read, write, _):
                            async with ClientSession(
                                read, write, sampling_callback=None
                            ) as session:
                                await session.initialize()
                                try:
                                    tool_result = await session.call_tool(
                                        tool_name, arguments=arguments
                                    )
                                    result_content = self._extract_tool_result_content(
                                        tool_result, tool_name
                                    )
                                    if self._should_block_hf_scraping(tool_name, arguments):
                                        result_content = "You are trying to scrape a Hugging Face dataset for answers, please do not use the scrape tool for this purpose."
                                except Exception as tool_error:
                                    logger.error(f"Tool execution error: {tool_error}")
                                    return {
                                        "server_name": server_name,
                                        "tool_name": tool_name,
                                        "error": f"Tool execution failed: {str(tool_error)}",
                                    }
                elif isinstance(server_params, str) and server_params.startswith(
                    ("http://", "https://")
                ):
                    # Legacy: bare URL string → SSE for backward compatibility
                    async with sse_client(server_params) as (read, write):
                        async with ClientSession(
                            read, write, sampling_callback=None
                        ) as session:
                            await session.initialize()
                            try:
                                tool_result = await session.call_tool(
                                    tool_name, arguments=arguments
                                )
                                
                                result_content = self._extract_tool_result_content(
                                    tool_result, tool_name
                                )

                                # post hoc check for browsing agent reading answers from hf datsets
                                if self._should_block_hf_scraping(tool_name, arguments):
                                    result_content = "You are trying to scrape a Hugging Face dataset for answers, please do not use the scrape tool for this purpose."
                            except Exception as tool_error:
                                logger.error(f"Tool execution error: {tool_error}")
                                return {
                                    "server_name": server_name,
                                    "tool_name": tool_name,
                                    "error": f"Tool execution failed: {str(tool_error)}",
                                }
                else:
                    raise TypeError(
                        f"Unknown server params type for {server_name}: {type(server_params)}"
                    )

                logger.info(
                    f"Tool '{tool_name}' (server: '{server_name}') called successfully."
                )

                if (
                    isinstance(result_content, str)
                    and "Unknown tool:" in result_content
                ):
                    suggested_servers = await self._find_servers_with_tool(tool_name)
                    if len(suggested_servers) == 1:
                        logger.info(
                            f"Auto-correction: Tool '{tool_name}' not found in '{server_name}', trying '{suggested_servers[0]}'"
                        )
                        return await self.execute_tool_call(
                            suggested_servers[0], tool_name, arguments
                        )

                return {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "result": result_content,  # Return extracted text content
                }

            except Exception as outer_e:  # Rename this to outer_e to avoid shadowing
                logger.error(
                    f"Error: Failed to call tool '{tool_name}' (server: '{server_name}'): {outer_e}"
                )
                # import traceback
                # traceback.print_exc() # Print detailed stack trace for debugging

                # Store the original error message for later use
                error_message = str(outer_e)

                if (
                    tool_name == "scrape"
                    and "unhandled errors" in error_message
                    and "url" in arguments
                    and arguments["url"] is not None
                ):
                    try:
                        logger.info("Attempting to use MarkItDown for fallback...")
                        from markitdown import MarkItDown

                        md = MarkItDown(
                            docintel_endpoint="<document_intelligence_endpoint>"
                        )
                        result = md.convert(arguments["url"])
                        logger.info("Successfully used MarkItDown")
                        return {
                            "server_name": server_name,
                            "tool_name": tool_name,
                            "result": result.text_content,  # Return extracted text content
                        }
                    except (
                        Exception
                    ) as inner_e:  # Use a different name to avoid shadowing
                        # Log the inner exception if needed
                        logger.error(f"Fallback also failed: {inner_e}")
                        # No need for pass here as we'll continue to the return statement

                # Always use the outer exception for the final error response
                return {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error": f"Tool call failed: {error_message}",
                }

    async def execute_tool_calls_batch(
        self, tool_calls: tuple, max_tool_calls: int = 10
    ) -> tuple[list[tuple[str, dict]], bool]:
        """
        Execute a batch of tool calls.
        :param tool_calls: Tuple of tool calls
        :param max_tool_calls: Maximum number of tool calls to execute
        :return: Tuple of tool call results and whether the tool calls exceeded the limit
        """
        if len(tool_calls) > max_tool_calls:
            tool_calls = tool_calls[:max_tool_calls]
            exceeded = True
        else:
            exceeded = False

        results = []
        for tool_call in tool_calls:
            call_id = tool_call["id"]
            server_name = tool_call["server_name"]
            tool_name = tool_call["tool_name"]
            arguments = tool_call["arguments"]
            try:
                result = await self.execute_tool_call(
                    server_name=server_name, tool_name=tool_name, arguments=arguments
                )
            except Exception as e:
                # Catch all exceptions (including TimeoutError) and convert to error result
                # This allows the agent to continue processing instead of failing the task
                logger.error(
                    f"Tool '{tool_name}' (server: '{server_name}') "
                    f"execution failed: {e}"
                )
                result = {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error": f"Tool call failed: {str(e)}",
                }
            results.append((call_id, result))

        return results, exceeded

    def format_tool_results(self, results):
        ret = []
        for call_id, result in results:
            ret.append((call_id, format_tool_result(result)))
        return ret
