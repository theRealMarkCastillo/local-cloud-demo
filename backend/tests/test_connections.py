"""Connections API: CSRF-protected, sign-in required, and logout drops the stored Keycloak tokens."""
import json
import secrets
import unittest
import httpx
from app import connections, main
from app import settings as s
from app.services import redis
from app.session import CREATE, encode


class LinkParamsTests(unittest.TestCase):
    def test_link_params_sets_kc_action(self):
        self.assertEqual(connections.link_params("upstream-demo"), {"kc_action": "idp_link:upstream-demo"})

    def test_link_params_unknown_alias_raises(self):
        with self.assertRaises(KeyError):
            connections.link_params("anything-else")

    def test_statuses_from_linked_accounts(self):
        linked = [{"providerAlias": "upstream-demo", "connected": True}, {"providerAlias": "other", "connected": True}]
        self.assertEqual(connections.statuses(linked), [{"alias": "upstream-demo", "connected": True}])
        self.assertEqual(connections.statuses([]), [{"alias": "upstream-demo", "connected": False}])


@unittest.skipUnless(s.ENVIRONMENT == "local", "Local synthetic Redis tests only")
class EndpointTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # No lifespan in these tests: register a synthetic provider so OAuth state checks can run offline.
        if "identity" not in main.oauth._clients:
            main.oauth.register("identity", client_id="synthetic", client_secret="synthetic",
                                authorize_url="http://id.invalid/auth", access_token_url="http://id.invalid/token",
                                client_kwargs={"scope": "openid", "code_challenge_method": "S256"})

    async def asyncSetUp(self):
        self.sid, family = secrets.token_hex(32), secrets.token_hex(32)
        self.key = "session:" + self.sid
        data = {"user": {"id": "synthetic", "name": "Synthetic"}, "csrf": "csrf-value",
                "tokens": {"access": "synthetic-access", "refresh": "synthetic-refresh", "expires_at": 9999999999.0}}
        redis.eval(CREATE, 2, self.key, "session-family:" + family, encode(data, family), 60)
        self.addAsyncCleanup(self.cleanup, family)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url=s.PUBLIC_URL,
                                      cookies={s.COOKIE_NAME: self.sid})
        self.csrf = {"Origin": s.PUBLIC_URL, "X-CSRF-Token": "csrf-value"}

    async def cleanup(self, family):
        await self.http.aclose()
        redis.delete(self.key, "session-family:" + family)

    async def test_connections_require_sign_in(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url=s.PUBLIC_URL) as anonymous:
            self.assertEqual((await anonymous.get("/api/connections")).status_code, 401)

    async def test_connect_requires_csrf(self):
        self.assertEqual((await self.http.post("/api/connections/upstream-demo/connect")).status_code, 403)
        wrong_origin = self.csrf | {"Origin": "https://evil.example"}
        self.assertEqual((await self.http.post("/api/connections/upstream-demo/connect", headers=wrong_origin)).status_code, 403)

    async def test_connect_returns_link_start_redirect(self):
        response = await self.http.post("/api/connections/upstream-demo/connect", headers=self.csrf)
        self.assertEqual(response.json(), {"redirect": s.PUBLIC_URL + "/auth/link-start/upstream-demo"})

    async def test_disconnect_requires_csrf(self):
        self.assertEqual((await self.http.delete("/api/connections/upstream-demo")).status_code, 403)

    async def test_unknown_alias_404(self):
        self.assertEqual((await self.http.post("/api/connections/nope/connect", headers=self.csrf)).status_code, 404)
        self.assertEqual((await self.http.delete("/api/connections/nope", headers=self.csrf)).status_code, 404)
        self.assertEqual((await self.http.get("/auth/link-start/nope")).status_code, 404)

    async def test_link_callback_rejects_bad_state(self):
        response = await self.http.get("/auth/link-callback?state=forged&code=abc&kc_action_status=success")
        self.assertEqual(response.status_code, 400)

    async def test_logout_clears_tokens(self):
        self.assertIn("synthetic-refresh", redis.get(self.key))
        self.assertEqual((await self.http.post("/auth/logout", headers=self.csrf)).status_code, 200)
        self.assertIsNone(redis.get(self.key))
        leftovers = [k for k in redis.scan_iter("session:*") if "synthetic-refresh" in (redis.get(k) or "")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
