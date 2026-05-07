# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""MiroFlow Web API - FastAPI application entry point."""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Add parent directory for MiroFlow imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables
dotenv.load_dotenv()

from .api.dependencies import get_agent_pool, init_dependencies  # noqa: E402
from .api.routes import configs, health, tasks, uploads  # noqa: E402
from .core.config import config  # noqa: E402


# Configure uvicorn access logging to reduce verbosity
class AccessLogFilter(logging.Filter):
    """Filter to suppress frequent polling requests from access logs."""
    
    def filter(self, record):
        # Suppress status polling requests (they happen every 1-2 seconds)
        if "/api/tasks/" in record.getMessage() and "/status" in record.getMessage():
            return False
        # Suppress health check logs
        if "/api/health" in record.getMessage():
            return False
        return True


# Set up logging
logging.basicConfig(level=logging.INFO)
uvicorn_logger = logging.getLogger("uvicorn.access")
uvicorn_logger.addFilter(AccessLogFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    config.uploads_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    init_dependencies()
    
    # Warm up the agent pool in a background thread so that the app is
    # immediately ready to serve requests (health checks, etc.) while the
    # potentially slow agent-build process runs concurrently.
    pool = get_agent_pool()
    if pool is not None and config.agent_pool_size > 0:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, pool.warmup)
        print(
            f"Agent pool warmup started in background "
            f"(pool_size={config.agent_pool_size}, config={config.default_config})"
        )
    yield
    # Shutdown - cleanup if needed


app = FastAPI(
    title="MiroFlow API",
    description="REST API for MiroFlow AI Research Agent",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(health.router)
app.include_router(tasks.router)
app.include_router(configs.router)
app.include_router(uploads.router)

# Serve static files (built frontend) if directory exists
static_dir = Path(__file__).parent / "static"
if static_dir.exists() and any(static_dir.iterdir()):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


@app.get("/")
async def root():
    """Root endpoint - returns API info or redirects to frontend."""
    return {
        "name": "MiroFlow API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "web_app.main:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
        access_log=True,
    )