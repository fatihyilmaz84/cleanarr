from __future__ import annotations

from fastapi import Request


async def get_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session
