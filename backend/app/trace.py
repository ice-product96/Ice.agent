"""Decision trace id shared across one agent turn (judgments, events, logs)."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator
from uuid import uuid4

_current_trace: ContextVar[str | None] = ContextVar("ice_decision_trace", default=None)


def new_trace_id() -> str:
    return uuid4().hex


def current_trace_id() -> str | None:
    return _current_trace.get()


def current_trace_id_default(_context: object = None) -> str | None:
    """SQLAlchemy column default: stamp rows with the active decision trace."""
    return _current_trace.get()


def set_trace_id(trace_id: str | None) -> Token[str | None]:
    return _current_trace.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _current_trace.reset(token)


@contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    value = trace_id or new_trace_id()
    token = _current_trace.set(value)
    try:
        yield value
    finally:
        _current_trace.reset(token)
