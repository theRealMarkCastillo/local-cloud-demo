"""Reporter agent: an A2A server that reads the calling user's jobs through the gateway, never with the token it was given."""
import asyncio
import base64
import json
import os
import time
import unittest
from urllib.parse import parse_qs
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from app import reporter
from app import settings as s
from app.mcp_auth import KeycloakTokenVerifier

os.environ.setdefault("REPORTER_AGENT_SECRET", "test-reporter-secret")
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS = "http://localhost:8080/identity/realms/workbench"
TRACE = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


class StubJwks:
    def get_signing_key_from_jwt(self, token):
        return jwt.PyJWK.from_dict(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1"})


def token(sub="demo", aud="reporter-agent", **extra):
    now = int(time.time())
    return jwt.encode({"iss": ISS, "aud": aud, "sub": sub, "iat": now, "exp": now + 300} | extra, KEY,
                      algorithm="RS256", headers={"kid": "k1"})


class Gateway:
    """Mock Keycloak token endpoint and the gateway's workbench MCP route. Job counts differ per user."""

    def __init__(self, exchange_status=200, tool_status=200, jobs=None, fail=None):
        self.exchange_status, self.tool_status, self.fail = exchange_status, tool_status, fail
        self.jobs = jobs or {"demo": [{"status": "complete"}, {"status": "complete"}]}
        self.requests = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise self.fail("simulated", request=request)
        if str(request.url) == s.TOKEN_URL:
            if self.exchange_status != 200:
                return httpx.Response(self.exchange_status, json={"error": "access_denied"})
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            sub = jwt.decode(form["subject_token"], options={"verify_signature": False})["sub"]
            await asyncio.sleep(0.01)  # let concurrent requests interleave
            return httpx.Response(200, json={"access_token": "exchanged-for-" + sub})
        assert str(request.url) == s.GATEWAY_INTERNAL_URL + "/gateway/mcp/workbench"
        sub = request.headers["authorization"].removeprefix("Bearer exchanged-for-")
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"Mcp-Session-Id": "s-" + sub})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if self.tool_status != 200:
            return httpx.Response(self.tool_status, text="authorization failed")
        await asyncio.sleep(0.01)
        result = {"structuredContent": {"jobs": self.jobs.get(sub, [])}, "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": result})


def build(gateway):
    verifier = KeycloakTokenVerifier(ISS, reporter.AUDIENCE, None, StubJwks())
    app = reporter.create_app(verifier=verifier,
                              http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(gateway.handler)))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://reporter")


def message(text="report"):
    return {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {
        "message": {"role": "ROLE_USER", "messageId": "m1", "parts": [{"text": text}]}}}


async def send(http, bearer=None, **headers):
    headers = {"A2A-Version": "1.0"} | headers
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    return await http.post("/", json=message(), headers=headers)


def outcome(response):
    """(state, text) of the task in a SendMessage response."""
    task = response.json()["result"]["task"]
    if task["status"]["state"] == "TASK_STATE_COMPLETED":
        return "completed", task["artifacts"][0]["parts"][0]["text"]
    return task["status"]["state"], task["status"]["message"]["parts"][0]["text"]


class SummariseTests(unittest.TestCase):
    def test_summarise_counts_by_status(self):
        jobs = [{"status": "complete"}, {"status": "queued"}, {"status": "complete"}]
        self.assertEqual(reporter.summarise(jobs), "3 jobs: 2 complete, 1 queued")

    def test_summarise_zero_and_one(self):
        self.assertEqual(reporter.summarise([]), "0 jobs")
        self.assertEqual(reporter.summarise([{"status": "queued"}]), "1 job: 1 queued")


class ReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_missing_wrong_audience_and_expired_token(self):
        gateway = Gateway()
        async with build(gateway) as http:
            self.assertEqual((await send(http)).status_code, 401)
            self.assertEqual((await send(http, token(aud="http://localhost:8080/gateway"))).status_code, 401)
            self.assertEqual((await send(http, token(exp=int(time.time()) - 600))).status_code, 401)
            self.assertEqual((await http.get("/.well-known/agent-card.json")).status_code, 401)
        self.assertEqual(gateway.requests, [])

    async def test_health_needs_no_token(self):
        async with build(Gateway()) as http:
            self.assertEqual((await http.get("/health")).status_code, 200)

    async def test_agent_card_names_the_skill(self):
        async with build(Gateway()) as http:
            card = (await http.get("/.well-known/agent-card.json", headers={"Authorization": "Bearer " + token()})).json()
        self.assertEqual(card["name"], "Workbench reporter")
        self.assertEqual([skill["id"] for skill in card["skills"]], ["job_report"])

    async def test_job_report_exchanges_then_lists_jobs(self):
        gateway, incoming = Gateway(), token()
        async with build(gateway) as http:
            response = await send(http, incoming)
        self.assertEqual(outcome(response), ("completed", "2 jobs: 2 complete"))
        exchange = gateway.requests[0]
        form = {k: v[0] for k, v in parse_qs(exchange.content.decode()).items()}
        self.assertEqual(form["subject_token"], incoming)
        basic = base64.b64encode(("reporter-agent:" + os.environ["REPORTER_AGENT_SECRET"]).encode()).decode()
        self.assertEqual(exchange.headers["authorization"], "Basic " + basic)
        self.assertNotIn("scope", form)
        for request in gateway.requests[1:]:
            self.assertEqual(request.headers["authorization"], "Bearer exchanged-for-demo")
        self.assertEqual(len(gateway.requests), 4)

    async def test_exchange_refused_is_a_failed_task(self):
        async with build(Gateway(exchange_status=400)) as http:
            response = await send(http, token())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(outcome(response), ("TASK_STATE_FAILED", "could not act for this user"))

    async def test_denied_tool_is_a_failed_task(self):
        async with build(Gateway(tool_status=403)) as http:
            response = await send(http, token())
        self.assertEqual(outcome(response), ("TASK_STATE_FAILED", "not allowed to read jobs"))

    async def test_unreachable_gateway_is_a_failed_task(self):
        async with build(Gateway(fail=httpx.ConnectTimeout)) as http:
            response = await send(http, token())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(outcome(response), ("TASK_STATE_FAILED", "could not act for this user"))

    async def test_traceparent_forwarded_only_when_well_formed(self):
        gateway = Gateway()
        async with build(gateway) as http:
            await send(http, token(), traceparent=TRACE)
            self.assertEqual([r.headers.get("traceparent") for r in gateway.requests[1:]], [TRACE] * 3)
            gateway.requests.clear()
            response = await send(http, token(), traceparent="garbage")
            self.assertEqual(outcome(response)[0], "completed")
            self.assertEqual([r.headers.get("traceparent") for r in gateway.requests[1:]], [None] * 3)

    async def test_concurrent_users_get_their_own_reports(self):
        gateway = Gateway(jobs={"demo": [{"status": "queued"}], "other": [{"status": "complete"}] * 3})
        expected = {"demo": "1 job: 1 queued", "other": "3 jobs: 3 complete"}
        users = ["demo", "other"] * 10
        async with build(gateway) as http:
            responses = await asyncio.gather(*(send(http, token(sub=user)) for user in users))
        self.assertEqual([outcome(r) for r in responses], [("completed", expected[user]) for user in users])

    async def test_tokens_never_logged(self):
        incoming = token()
        with self.assertLogs("reporter", level="INFO") as logs:
            for gateway in (Gateway(), Gateway(exchange_status=400), Gateway(tool_status=403)):
                async with build(gateway) as http:
                    await send(http, incoming, **{"x-calling-agent": "workbench-agent"})
        text = "\n".join(logs.output)
        self.assertNotIn(incoming, text)
        self.assertNotIn("exchanged-for-demo", text)
        self.assertEqual(text.count("sub=demo"), 3)
        self.assertEqual(text.count("caller=workbench-agent"), 3)
        for wanted in ("outcome=ok", "outcome=exchange_refused", "outcome=denied"):
            self.assertIn(wanted, text)


if __name__ == "__main__":
    unittest.main()
