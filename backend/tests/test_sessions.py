"""Deterministic concurrency checks using synthetic keys in the local Redis service."""
import asyncio
import json
import secrets
import unittest
import httpx
from fastapi import FastAPI, Request
from app import settings as settings
from app.services import redis
from app.session import CREATE, ServerSessionMiddleware, encode


@unittest.skipUnless(settings.ENVIRONMENT == 'local', 'Local synthetic Redis tests only')
class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sid, self.family = secrets.token_hex(32), secrets.token_hex(32)
        self.key, self.family_key = 'session:' + self.sid, 'session-family:' + self.family
        redis.eval(CREATE, 2, self.key, self.family_key, encode({'user': {'id': 'synthetic'}, 'csrf': 'test'}, self.family), 60)
        self.started, self.release = asyncio.Event(), asyncio.Event()
        app = FastAPI()
        app.add_middleware(ServerSessionMiddleware)

        @app.get('/hold')
        async def hold(request: Request, operation: str = 'read'):
            self.started.set()
            await self.release.wait()
            if operation == 'change': request.session['late'] = True
            elif operation == 'rotate':
                request.session['rotated'] = True
                request.scope['rotate_session'] = True
            elif operation == 'logout': request.session.clear()
            return {'ok': True}

        @app.post('/logout')
        async def logout(request: Request):
            request.session.clear()
            return {'ok': True}

        @app.post('/change')
        async def change(request: Request):
            request.session['newer'] = True
            return {'ok': True}

        @app.post('/rotate')
        async def rotate(request: Request):
            request.session['rotated'] = True
            request.scope['rotate_session'] = True
            return {'ok': True}

        @app.post('/nested')
        async def nested(request: Request):
            request.session['user']['name'] = 'Updated'
            return {'ok': True}

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test.local')
        self.headers = {'cookie': settings.COOKIE_NAME + '=' + self.sid}

    async def asyncTearDown(self):
        self.release.set()
        current = redis.get(self.family_key)
        if current: redis.delete(current)
        redis.delete(self.key, self.family_key)
        await self.client.aclose()

    async def pending(self, operation):
        task = asyncio.create_task(self.client.get('/hold', params={'operation': operation}, headers=self.headers))
        await asyncio.wait_for(self.started.wait(), 5)
        return task

    async def test_nested_mutation_is_saved_without_reissuing_cookie(self):
        response = await self.client.post('/nested', headers=self.headers)
        self.assertNotIn('set-cookie', response.headers)
        self.assertEqual(json.loads(redis.get(self.key))['data']['user']['name'], 'Updated')

    async def test_read_cannot_restore_logout_or_emit_old_cookie(self):
        task = await self.pending('read')
        await self.client.post('/logout', headers=self.headers)
        self.release.set()
        response = await task
        self.assertIsNone(redis.get(self.key))
        self.assertIsNone(redis.get(self.family_key))
        self.assertNotIn('set-cookie', response.headers)

    async def test_late_mutation_cannot_restore_logout(self):
        task = await self.pending('change')
        await self.client.post('/logout', headers=self.headers)
        self.release.set()
        await task
        self.assertIsNone(redis.get(self.key))
        self.assertIsNone(redis.get(self.family_key))

    async def test_read_and_stale_mutation_do_not_clobber_newer_state(self):
        for operation in ('read', 'change'):
            self.started, self.release = asyncio.Event(), asyncio.Event()
            task = await self.pending(operation)
            response = await self.client.post('/change', headers=self.headers)
            self.assertNotIn('set-cookie', response.headers)
            self.release.set()
            await task
            data = json.loads(redis.get(self.key))['data']
            self.assertTrue(data['newer'])
            self.assertNotIn('late', data)
            raw = json.loads(redis.get(self.key));raw['data'].pop('newer', None)
            redis.set(self.key, json.dumps(raw, sort_keys=True), ex=60)

    async def test_rotation_after_logout_cannot_create_authenticated_session(self):
        task = await self.pending('rotate')
        await self.client.post('/logout', headers=self.headers)
        self.release.set()
        response = await task
        self.assertNotIn('set-cookie', response.headers)
        self.assertIsNone(redis.get(self.family_key))

    async def test_logout_revokes_a_rotation_that_completed_while_it_was_in_flight(self):
        task = await self.pending('logout')
        rotated = await self.client.post('/rotate', headers=self.headers)
        new_sid = rotated.cookies.get(settings.COOKIE_NAME)
        self.assertIsNotNone(new_sid)
        self.assertNotEqual(new_sid, self.sid)
        self.assertIsNotNone(redis.get('session:' + new_sid))
        self.release.set()
        await task
        self.assertIsNone(redis.get('session:' + new_sid))
        self.assertIsNone(redis.get(self.family_key))

    async def test_read_after_rotation_does_not_restore_old_id_or_cookie(self):
        task = await self.pending('read')
        rotated = await self.client.post('/rotate', headers=self.headers)
        new_sid = rotated.cookies.get(settings.COOKIE_NAME)
        self.release.set()
        response = await task
        self.assertNotIn('set-cookie', response.headers)
        self.assertIsNone(redis.get(self.key))
        self.assertEqual(redis.get(self.family_key), 'session:' + new_sid)


if __name__ == '__main__': unittest.main()
