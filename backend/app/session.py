"""Opaque browser cookie; all session/OIDC state lives in Redis, never in JS."""
import json
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from app.services import redis
from app import settings as s

class ServerSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        sid = request.cookies.get(s.COOKIE_NAME, "")
        valid = len(sid) == 64 and all(c in "0123456789abcdef" for c in sid)
        raw = await run_in_threadpool(redis.get, f"session:{sid}") if valid else None
        original = json.loads(raw) if raw else {}
        request.scope["session"] = original.copy()
        response = await call_next(request)
        current = request.scope["session"]
        rotate = request.scope.get("rotate_session", False)
        if rotate or not current:
            if raw:
                await run_in_threadpool(redis.delete, f"session:{sid}")
            sid = ""
        if current:
            if not sid or not raw:
                sid = secrets.token_hex(32)
            await run_in_threadpool(redis.setex, f"session:{sid}", s.SESSION_TTL, json.dumps(current))
            response.set_cookie(s.COOKIE_NAME, sid, max_age=s.SESSION_TTL, httponly=True,
                                secure=s.COOKIE_SECURE, samesite="lax", path="/")
        elif raw:
            response.delete_cookie(s.COOKIE_NAME, path="/", secure=s.COOKIE_SECURE,
                                   httponly=True, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        return response
