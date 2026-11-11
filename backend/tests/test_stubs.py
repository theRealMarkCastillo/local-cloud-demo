"""Local stand-ins behind the gateway: fake LLM, header echo and a Jira-like MCP server for upstream tokens."""
import time
import unittest
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from app import stubs
from app.mcp_auth import KeycloakTokenVerifier

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
UPSTREAM_ISS = stubs.UPSTREAM_ISSUER
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "MCP-Protocol-Version": "2025-11-25"}


class StubJwks:
    def get_signing_key_from_jwt(self, token):
        return jwt.PyJWK.from_dict(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1"})


def token(iss=UPSTREAM_ISS, aud="jira-stub", **extra):
    now = int(time.time())
    value = {"iss": iss, "aud": aud, "sub": "upstream-1", "preferred_username": "demo-upstream",
             "iat": now, "exp": now + 300} | extra
    return jwt.encode(value, KEY, algorithm="RS256", headers={"kid": "k1"})


def rpc(method, params=None):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def stub_app():
    return stubs.create_app(stubs.UpstreamVerifier(
        KeycloakTokenVerifier(UPSTREAM_ISS, stubs.AUDIENCE, None, StubJwks())))


class StubTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, path, **kwargs):
        # A fresh app per request: the MCP session manager can only be started once per server.
        app = stub_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stubs:8000") as http:
                return await http.post(path, **kwargs)

    async def test_llm_reports_500_tokens(self):
        response = await self.request("/v1/chat/completions", headers={"Authorization": "Bearer provider-key-123456"},
                                        json={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]})
        body = response.json()
        self.assertEqual(body["usage"]["total_tokens"], 500)
        self.assertEqual(body["choices"][0]["message"]["content"], "stub reply auth=Bearer provider")

    async def test_echo_returns_headers(self):
        response = await self.request("/echo/any/path", headers={"Authorization": "Bearer shared", "X-Other": "1"}, json={})
        self.assertEqual(response.json()["headers"]["authorization"], "Bearer shared")
        self.assertEqual(response.json()["path"], "/echo/any/path")

    async def test_jira_rejects_missing_token(self):
        self.assertEqual((await self.request("/jira/mcp", headers=HEADERS, json=rpc("tools/list"))).status_code, 401)

    async def test_jira_rejects_workbench_realm_token(self):
        wrong = token(iss=UPSTREAM_ISS.replace("upstream-demo", "workbench"))
        response = await self.request("/jira/mcp", headers=HEADERS | {"Authorization": "Bearer " + wrong}, json=rpc("tools/list"))
        self.assertEqual(response.status_code, 401)

    async def test_jira_rejects_wrong_audience(self):
        response = await self.request("/jira/mcp", headers=HEADERS | {"Authorization": "Bearer " + token(aud="gateway")},
                                        json=rpc("tools/list"))
        self.assertEqual(response.status_code, 401)

    async def test_jira_whoami_returns_upstream_user(self):
        response = await self.request("/jira/mcp", headers=HEADERS | {"Authorization": "Bearer " + token()},
                                        json=rpc("tools/call", {"name": "whoami", "arguments": {}}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["structuredContent"], {"upstream_user": "demo-upstream"})


if __name__ == "__main__":
    unittest.main()
