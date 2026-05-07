#!/usr/bin/env bash
# scripts/start_mcp_servers.sh
#
# Start all MiroFlow MCP tool servers as independent long-lived HTTP services
# on localhost.  Run this once before starting the web app with
# --config-name agent_web_demo_remote.
#
# Usage:
#   bash scripts/start_mcp_servers.sh          # start all servers
#   bash scripts/start_mcp_servers.sh stop      # stop all servers
#   bash scripts/start_mcp_servers.sh status    # show server status
#
# Ports (can be overridden via environment variables):
#   TOOL_SERPER_SEARCH_PORT  (default: 8001)
#   TOOL_CODE_SANDBOX_PORT   (default: 8002)
#   TOOL_JINA_SCRAPE_PORT    (default: 8003)
#   TOOL_READING_PORT        (default: 8004)
#
# Log files are written to logs/mcp_servers/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${REPO_ROOT}/logs/mcp_servers"
PID_DIR="${REPO_ROOT}/logs/mcp_servers/pids"

SERPER_PORT="${TOOL_SERPER_SEARCH_PORT:-8001}"
# SANDBOX_PORT="${TOOL_CODE_SANDBOX_PORT:-8002}"
JINA_PORT="${TOOL_JINA_SCRAPE_PORT:-8003}"
READING_PORT="${TOOL_READING_PORT:-8004}"

mkdir -p "${LOG_DIR}" "${PID_DIR}"

# Load .env if present
if [ -f "${REPO_ROOT}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env"
    set +a
fi

# ─── helpers ─────────────────────────────────────────────────────────────────

start_server() {
    local name="$1"
    local module="$2"
    local port="$3"
    local pid_file="${PID_DIR}/${name}.pid"
    local log_file="${LOG_DIR}/${name}.log"

    if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
        echo "[SKIP]  ${name} already running (pid $(cat "${pid_file}"))"
        return
    fi

    echo "[START] ${name} on port ${port} ..."
    uv run python -m "${module}" --transport http --port "${port}" --path /mcp \
        >> "${log_file}" 2>&1 &
    echo $! > "${pid_file}"
    echo "[OK]    ${name} started (pid $!, log: ${log_file})"
}

stop_server() {
    local name="$1"
    local pid_file="${PID_DIR}/${name}.pid"

    if [ ! -f "${pid_file}" ]; then
        echo "[SKIP]  ${name}: no pid file found"
        return
    fi

    local pid
    pid="$(cat "${pid_file}")"

    if kill -0 "${pid}" 2>/dev/null; then
        echo "[STOP]  ${name} (pid ${pid}) ..."
        kill "${pid}"
        rm -f "${pid_file}"
        echo "[OK]    ${name} stopped"
    else
        echo "[SKIP]  ${name}: process ${pid} not running, cleaning up pid file"
        rm -f "${pid_file}"
    fi
}

status_server() {
    local name="$1"
    local port="$2"
    local pid_file="${PID_DIR}/${name}.pid"

    if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
        echo "[UP]    ${name}  pid=$(cat "${pid_file}")  port=${port}"
    else
        echo "[DOWN]  ${name}  port=${port}"
    fi
}

wait_for_port() {
    local name="$1"
    local port="$2"
    local retries=180
    echo -n "[WAIT]  ${name} (port ${port}) "
    while ! python3 - <<EOF 2>/dev/null
import urllib.request, sys
try:
    urllib.request.urlopen("http://localhost:${port}/mcp", timeout=3)
    sys.exit(0)
except Exception:
    sys.exit(1)
EOF
    do
        echo -n "."
        sleep 1
        retries=$((retries - 1))
        if [ "${retries}" -le 0 ]; then
            echo " TIMEOUT"
            return 1
        fi
    done
    echo " READY"
}

# tmp
# stop_server "mcp-jina-scrape"
# start_server "mcp-jina-scrape" \
#             "miroflow.tool.mcp_servers.jina_scrape"   "${JINA_PORT}"

stop_server "mcp-serper-search"
start_server "mcp-serper-search" \
            "miroflow.tool.mcp_servers.serper_search" "${SERPER_PORT}"
exit 0

# ─── commands ─────────────────────────────────────────────────────────────────

cmd="${1:-start}"

case "${cmd}" in
    start)
        cd "${REPO_ROOT}"
        start_server "mcp-serper-search" \
            "miroflow.tool.mcp_servers.serper_search" "${SERPER_PORT}"
        # start_server "mcp-code-sandbox" \
        #     "miroflow.tool.mcp_servers.code_sandbox"  "${SANDBOX_PORT}"
        start_server "mcp-jina-scrape" \
            "miroflow.tool.mcp_servers.jina_scrape"   "${JINA_PORT}"
        start_server "mcp-reading" \
            "miroflow.tool.mcp_servers.reading_mcp_server" "${READING_PORT}"

        echo ""
        echo "Waiting for servers to become ready..."
        wait_for_port "mcp-serper-search" "${SERPER_PORT}"
        # wait_for_port "mcp-code-sandbox"  "${SANDBOX_PORT}"
        wait_for_port "mcp-jina-scrape"   "${JINA_PORT}"
        wait_for_port "mcp-reading"       "${READING_PORT}"

        echo ""
        echo "All MCP servers are running.  Start the web app with:"
        echo "  python web_app/main.py --config-name agent_web_demo_remote"
        ;;

    stop)
        stop_server "mcp-serper-search"
        # stop_server "mcp-code-sandbox"
        stop_server "mcp-jina-scrape"
        stop_server "mcp-reading"
        echo "All MCP servers stopped."
        ;;

    status)
        status_server "mcp-serper-search" "${SERPER_PORT}"
        # status_server "mcp-code-sandbox"  "${SANDBOX_PORT}"
        status_server "mcp-jina-scrape"   "${JINA_PORT}"
        status_server "mcp-reading"       "${READING_PORT}"
        ;;

    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
