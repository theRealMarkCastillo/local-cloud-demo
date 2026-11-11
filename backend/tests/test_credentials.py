"""Credentials ext-authz service: stored upstream tokens reach only their own user, and never the logs."""
import asyncio
import time
import unittest
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from app.credentials import Broker, NotConnected, TokenCache, Unavailable, create_app
from app.mcp_auth import KeycloakTokenVerifier

ISS = "https://id.example/realms/workbench"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TOKEN_URL = "http://kc/token"
BROKER_URL = "http://kc/broker/{alias}/token"
PUBLIC = "http://localhost:8080"


class StubJwks:
    def get_signing_key_from_jwt(self, token):
        return jwt.PyJWK.from_dict(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1"})


def token(sub="user-1", aud=("gateway", "gateway-credentials"), roles=("jira:use",), **overrides):
    now = int(time.time())
    value = {"iss": ISS, "aud": list(aud), "sub": sub, "azp": "agent-cli", "iat": now, "exp": now + 300,
             "resource_access": {"gateway": {"roles": list(roles)}}}
    value.update(overrides)
    return jwt.encode(value, KEY, algorithm="RS256", headers={"kid": "k1"})


class Keycloak:
    """Mock token + broker endpoints. Upstream tokens are derived from the exchanged user's subject."""

    def __init__(self, broker_status=200, fail=None, expires_in=300):
        self.broker_status, self.fail, self.expires_in = broker_status, fail, expires_in
        self.exchanges, self.reads = 0, 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise self.fail
        if str(request.url) == TOKEN_URL:
            self.exchanges += 1
            form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
            sub = jwt.decode(form["subject_token"], options={"verify_signature": False})["sub"]
            await asyncio.sleep(0.01)  # let concurrent requests interleave
            return httpx.Response(200, json={"access_token": "exchanged-for-" + sub})
        self.reads += 1
        if self.broker_status != 200:
            return httpx.Response(self.broker_status, json={"errorMessage": "not associated"})
        sub = request.headers["authorization"].removeprefix("Bearer exchanged-for-")
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"access_token": "upstream-access-" + sub, "refresh_token": "upstream-refresh-" + sub,
                                         "expires_in": self.expires_in})


def build(keycloak, cache=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(keycloak.handler))
    broker = Broker(http, TOKEN_URL, BROKER_URL, "gateway-credentials", "secret")
    verifier = KeycloakTokenVerifier(ISS, "gateway", None, StubJwks())
    return create_app(verifier=verifier, broker=broker, cache=cache or TokenCache(), public_url=PUBLIC)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://credentials")


def bearer(value):
    return {"Authorization": "Bearer " + value}


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.cache = TokenCache(clock=lambda: self.now)

    def test_cache_hit_within_ttl(self):
        self.cache.put("u1", "a", "t", self.now + 300)
        self.now += 59
        self.assertEqual(self.cache.get("u1", "a"), "t")

    def test_cache_ttl_capped_at_60s(self):
        self.cache.put("u1", "a", "t", self.now + 3600)
        self.now += 61
        self.assertIsNone(self.cache.get("u1", "a"))

    def test_cache_expires_30s_before_upstream(self):
        self.cache.put("u1", "a", "t", self.now + 50)
        self.now += 19
        self.assertEqual(self.cache.get("u1", "a"), "t")
        self.now += 2
        self.assertIsNone(self.cache.get("u1", "a"))

    def test_cache_isolated_by_user_and_alias(self):
        self.cache.put("u1", "a", "t", self.now + 300)
        self.assertIsNone(self.cache.get("u2", "a"))
        self.assertIsNone(self.cache.get("u1", "b"))


class CheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_returns_authorization_header(self):
        async with client(build(Keycloak())) as c:
            response = await c.get("/check/upstream-demo", headers=bearer(token()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["authorization"], "Bearer upstream-access-user-1")

    async def test_check_accepts_any_method(self):
        async with client(build(Keycloak())) as c:
            self.assertEqual((await c.post("/check/upstream-demo", headers=bearer(token()))).status_code, 200)

    async def test_check_uses_cache_second_time(self):
        keycloak = Keycloak()
        async with client(build(keycloak)) as c:
            await c.get("/check/upstream-demo", headers=bearer(token()))
            await c.get("/check/upstream-demo", headers=bearer(token()))
        self.assertEqual((keycloak.exchanges, keycloak.reads), (1, 1))

    async def test_check_rejects_missing_and_wrong_audience(self):
        async with client(build(Keycloak())) as c:
            self.assertEqual((await c.get("/check/upstream-demo")).status_code, 401)
            response = await c.get("/check/upstream-demo", headers=bearer(token(aud=("http://localhost:8080/mcp",))))
        self.assertEqual(response.status_code, 401)

    async def test_check_requires_jira_role(self):
        async with client(build(Keycloak())) as c:
            response = await c.get("/check/upstream-demo", headers=bearer(token(roles=("llm:use",))))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "forbidden", "required_role": "jira:use"})

    async def test_unknown_alias_is_404(self):
        async with client(build(Keycloak())) as c:
            self.assertEqual((await c.get("/check/other", headers=bearer(token()))).status_code, 404)

    async def test_not_linked_maps_to_account_not_connected(self):
        async with client(build(Keycloak(broker_status=400))) as c:
            response = await c.get("/check/upstream-demo", headers=bearer(token()))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "account_not_connected",
                                           "connect_url": PUBLIC + "/connections.html?service=upstream-demo"})

    async def test_keycloak_down_is_503(self):
        async with client(build(Keycloak(fail=httpx.ConnectError("down")))) as c:
            self.assertEqual((await c.get("/check/upstream-demo", headers=bearer(token()))).status_code, 503)

    async def test_broker_server_error_is_503(self):
        async with client(build(Keycloak(broker_status=500))) as c:
            self.assertEqual((await c.get("/check/upstream-demo", headers=bearer(token()))).status_code, 503)

    async def test_concurrent_users_never_share_tokens(self):
        async with client(build(Keycloak())) as c:
            users = [f"user-{n % 2}" for n in range(20)]
            responses = await asyncio.gather(*(c.get("/check/upstream-demo", headers=bearer(token(sub=u))) for u in users))
        for user, response in zip(users, responses):
            self.assertEqual(response.headers["authorization"], "Bearer upstream-access-" + user)

    async def test_tokens_not_logged(self):
        with self.assertLogs("app.credentials", level="DEBUG") as logs:
            async with client(build(Keycloak())) as c:
                await c.get("/check/upstream-demo", headers=bearer(token()))
            async with client(build(Keycloak(broker_status=500))) as c:
                await c.get("/check/upstream-demo", headers=bearer(token()))
        text = "\n".join(logs.output)
        self.assertIn("user-1", text)
        for secret in ("upstream-access", "upstream-refresh", "exchanged-for", "eyJ"):
            self.assertNotIn(secret, text)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_token_returns_token_and_expiry(self):
        keycloak = Keycloak(expires_in=120)
        broker = Broker(httpx.AsyncClient(transport=httpx.MockTransport(keycloak.handler)), TOKEN_URL, BROKER_URL, "c", "s")
        value, expires_at = await broker.upstream_token(token(), "upstream-demo")
        self.assertEqual(value, "upstream-access-user-1")
        self.assertAlmostEqual(expires_at, time.time() + 120, delta=5)

    async def test_not_associated_raises_not_connected(self):
        broker = Broker(httpx.AsyncClient(transport=httpx.MockTransport(Keycloak(broker_status=400).handler)),
                        TOKEN_URL, BROKER_URL, "c", "s")
        with self.assertRaises(NotConnected):
            await broker.upstream_token(token(), "upstream-demo")

    async def test_network_error_raises_unavailable(self):
        broker = Broker(httpx.AsyncClient(transport=httpx.MockTransport(Keycloak(fail=httpx.ReadTimeout("slow")).handler)),
                        TOKEN_URL, BROKER_URL, "c", "s")
        with self.assertRaises(Unavailable):
            await broker.upstream_token(token(), "upstream-demo")


if __name__ == "__main__":
    unittest.main()
