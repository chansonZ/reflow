# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""
Tool factory module - creates MCP server parameters from configuration

Note: Tools are dynamically discovered through the MCP protocol, not the registry
Two modes are supported for each tool:
  1. Stdio (default): launches a local subprocess per agent run.
     Config fields: tool_command, args, env
  2. Remote HTTP: connects to an already-running MCP HTTP service.
     Config fields: url, transport (optional, defaults to "streamable-http")
     Example:
       name: "tool-serper-search"
       url: "http://localhost:8001/mcp"
       transport: "streamable-http"   # or "sse" for legacy SSE endpoints
"""

import sys
from typing import List, Dict, Any, Optional

from mcp import StdioServerParameters
from omegaconf import OmegaConf


def get_mcp_server_configs_from_tool_cfg_paths(
    cfg_paths: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Create MCP server configurations from a list of tool config paths.

    Args:
        cfg_paths: List of tool configuration file paths. Returns empty list if None.

    Returns:
        List of MCP server configurations.  Each entry is a dict with:
          - "name": server name string
          - "params": one of
              * StdioServerParameters  – for subprocess-based (stdio) tools
              * dict {"url": str, "transport": str}  – for remote HTTP tools
    """
    if cfg_paths is None:
        return []

    configs = []

    # TODO: add support for SSE endpoint
    for config_path in cfg_paths:
        try:
            tool_cfg = OmegaConf.load(config_path)
            # Remote HTTP mode: config has a "url" field instead of "tool_command"
            if "url" in tool_cfg:
                transport = tool_cfg.get("transport", "streamable-http")
                configs.append(
                    {
                        "name": tool_cfg.get("name"),
                        "params": {
                            "url": str(tool_cfg["url"]),
                            "transport": str(transport),
                        },
                    }
                )
            else:
                # Stdio mode (default): launch a subprocess
                configs.append(
                    {
                        "name": tool_cfg.get("name"),
                        "params": StdioServerParameters(
                            command=sys.executable
                            if tool_cfg["tool_command"] == "python"
                            else tool_cfg["tool_command"],
                            args=tool_cfg.get("args", []),
                            env=tool_cfg.get("env", {}),
                        ),
                    }
                )
        except Exception as e:
            raise RuntimeError(
                f"Error creating MCP server parameters for tool {config_path}: {e}"
            )

    return configs
