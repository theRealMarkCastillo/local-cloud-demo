"""Opaque browser sessions with atomic Redis revocation, rotation and state updates."""
from copy import deepcopy
import json
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from app.services import redis
from app import settings as s

# The family points at its current session key. Logout revokes the family, so a
# concurrent login rotation cannot escape revocation by issuing another ID.
CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SET', KEYS[2], KEYS[1], 'EX', ARGV[2])
return 1
"""
REFRESH = """
if redis.call('GET', KEYS[2]) ~= KEYS[1] or redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return 1
"""
COMPARE_AND_SET = """
if redis.call('GET', KEYS[2]) ~= KEYS[1] or redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""
ROTATE = """
if redis.call('GET', KEYS[2]) ~= KEYS[1] or redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if not redis.call('SET', KEYS[3], ARGV[2], 'EX', ARGV[3], 'NX') then return 0 end
redis.call('SET', KEYS[2], KEYS[3], 'EX', ARGV[3])
redis.call('DEL', KEYS[1])
return 1
"""
REVOKE = """
local current = redis.call('GET', KEYS[2])
if current then redis.call('DEL', current) end
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""


def token(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def encode(data, family):
    return json.dumps({'data': data, 'family': family}, sort_keys=True)


class ServerSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        sid = request.cookies.get(s.COOKIE_NAME, '')
        key = f'session:{sid}'
        raw = await run_in_threadpool(redis.get, key) if token(sid) else None
        envelope = json.loads(raw) if raw else {}
        family = envelope.get('family')
        # Old-format sessions are intentionally not adopted into a new family.
        # They require a fresh sign-in rather than gaining an unrevocable alias.
        if not token(family) or not isinstance(envelope.get('data'), dict):
            raw = None
            original = {}
        else:
            original = envelope['data']
        request.scope['session'] = deepcopy(original)
        response = await call_next(request)
        current = request.scope['session']
        family_key = f'session-family:{family}'
        issue_cookie = False
        if not current:
            if raw:
                await run_in_threadpool(redis.eval, REVOKE, 2, key, family_key)
                response.delete_cookie(s.COOKIE_NAME, path='/', secure=s.COOKIE_SECURE,
                                       httponly=True, samesite='lax')
        elif not raw:
            sid, family = secrets.token_hex(32), secrets.token_hex(32)
            issue_cookie = await run_in_threadpool(
                redis.eval, CREATE, 2, f'session:{sid}', f'session-family:{family}',
                encode(current, family), s.SESSION_TTL)
        elif request.scope.get('rotate_session', False):
            new_sid = secrets.token_hex(32)
            issue_cookie = await run_in_threadpool(
                redis.eval, ROTATE, 3, key, family_key, f'session:{new_sid}', raw,
                encode(current, family), s.SESSION_TTL)
            sid = new_sid
        elif current != original:
            await run_in_threadpool(redis.eval, COMPARE_AND_SET, 2, key, family_key,
                                    raw, encode(current, family), s.SESSION_TTL)
        else:
            await run_in_threadpool(redis.eval, REFRESH, 2, key, family_key, s.SESSION_TTL)
        if issue_cookie:
            # A browser-session cookie avoids stale read responses refreshing or
            # replacing cookies. Redis controls the sliding idle timeout.
            response.set_cookie(s.COOKIE_NAME, sid, httponly=True, secure=s.COOKIE_SECURE,
                                samesite='lax', path='/')
        response.headers['Cache-Control'] = 'no-store'
        return response
