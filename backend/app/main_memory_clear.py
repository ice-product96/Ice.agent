"""Application entrypoint with agent memory-clear routes enabled."""

from .main import app
from .memory_routes import router as memory_router

app.include_router(memory_router)
