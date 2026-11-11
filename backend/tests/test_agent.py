"""In-stack sample agent: acts as `workbench-agent` for the signed-in user, only through the gateway."""
import base64
import json
import secrets
import unittest
from urllib.parse import parse_qs
import httpx
from app import agent, main
from app import settings as s
from app.services import redis
from app.session import CREATE, encode
from app.user_tokens import SignInRequired

NOT_CONNECTED = {"error": "account_not_connected", "connect_url": "http://localhost:8080/connections.html?service=upstream-demo"}


class Gateway:
    """Mock Keycloak token endpoint, gateway routes and credentials service."""

    def __init__(self, exchange_status=200, jira_connected=True, reporter="ok"):
        self.exchange_status, self.jira_connected, self.requests = exchange_status, jira_connected, []
        self.reporter = reporter  # "ok", "failed" (a failed A2A task) or an HTTP status the gateway answers with

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == s.TOKEN_URL:
            if self.exchange_status != 200:
                return httpx.Response(self.exchange_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "gateway-token"})
        if url.startswith(s.CREDENTIALS_INTERNAL_URL):
            return httpx.Response(204)
        assert request.headers["authorization"] == "Bearer gateway-token", "the agent must only present the exchanged token"
        if url.endswith("/gateway/llm/v1/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "stub reply"}}], "usage": {"total_tokens": 500}})
        if url.endswith("/gateway/a2a/reporter"):
            return self.reporter_reply()
        body = json.loads(request.content)
        if url.endswith("/gateway/mcp/jira") and not self.jira_connected:
            return httpx.Response(403, json=NOT_CONNECTED)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "x"}}},
                                  headers={"Mcp-Session-Id": "session-1"})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["mcp-session-id"] == "session-1"
        content = {"jobs": [{"id": "1"}, {"id": "2"}]} if url.endswith("/workbench") else {"upstream_user": "demo-upstream"}
        # agentgateway answers tool calls as an event stream.
        event = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": content, "isError": False}})
        return httpx.Response(200, text=f"event: message\ndata: {event}\n\n", headers={"Content-Type": "text/event-stream"})

    def reporter_reply(self) -> httpx.Response:
        if self.reporter == "timeout":
            raise httpx.ReadTimeout("simulated")
        if isinstance(self.reporter, int):
            return httpx.Response(self.reporter, text="authorization failed")
        if self.reporter == "failed":
            status = {"state": "TASK_STATE_FAILED", "message": {"role": "ROLE_AGENT", "parts": [{"text": "not allowed to read jobs"}]}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {"task": {"id": "t", "status": status}}})
        task = {"id": "t", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [{"text": "2 jobs: 2 complete"}]}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {"task": task}})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_token_exchanges_as_workbench_agent(self):
        gateway = Gateway()
        async with gateway.client() as http:
            self.assertEqual(await agent.gateway_token("user-token", http), "gateway-token")
        request = gateway.requests[0]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.assertEqual(form["grant_type"], "urn:ietf:params:oauth:grant-type:token-exchange")
        self.assertEqual(form["subject_token"], "user-token")
        self.assertNotIn("scope", form)
        user, _, _ = base64.b64decode(request.headers["authorization"].split()[1]).decode().partition(":")
        self.assertEqual(user, "workbench-agent")

    async def test_rejected_exchange_requires_sign_in(self):
        async with Gateway(exchange_status=400).client() as http:
            with self.assertRaises(SignInRequired):
                await agent.gateway_token("user-token", http)

    async def test_run_reports_each_step(self):
        async with Gateway().client() as http:
            result = await agent.run("user-token", http)
        self.assertEqual(result["steps"], [
            {"step": "llm", "status": 200, "detail": "stub reply"},
            {"step": "list_jobs", "status": 200, "detail": "2 jobs"},
            {"step": "jira", "status": 200, "detail": "demo-upstream"},
            {"step": "reporter", "status": 200, "detail": "2 jobs: 2 complete"}])

    async def test_reporter_failed_task_is_a_step_result(self):
        async with Gateway(reporter="failed").client() as http:
            result = await agent.run("user-token", http)
        self.assertEqual(result["steps"][3], {"step": "reporter", "status": 502, "detail": "not allowed to read jobs"})
        self.assertEqual([step["status"] for step in result["steps"][:3]], [200, 200, 200])

    async def test_reporter_timeout_is_a_step_result(self):
        # The earlier steps' results must survive a reporter that does not answer in time.
        async with Gateway(reporter="timeout").client() as http:
            result = await agent.run("user-token", http)
        self.assertEqual(result["steps"][3], {"step": "reporter", "status": 502, "detail": "failed"})
        self.assertEqual([step["status"] for step in result["steps"][:3]], [200, 200, 200])

    async def test_reporter_forbidden_is_a_step_result(self):
        async with Gateway(reporter=403).client() as http:
            result = await agent.run("user-token", http)
        self.assertEqual(result["steps"][3], {"step": "reporter", "status": 403, "detail": "failed"})

    async def test_reporter_call_presents_exchanged_token_and_traceparent(self):
        gateway = Gateway()
        async with gateway.client() as http:
            await agent.run("user-token", http)
        request = gateway.requests[-1]
        self.assertTrue(str(request.url).endswith("/gateway/a2a/reporter"))
        self.assertEqual(request.headers["authorization"], "Bearer gateway-token")
        self.assertEqual(request.headers["a2a-version"], "1.0")
        self.assertRegex(request.headers["traceparent"], r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")
        sent = json.loads(request.content)
        self.assertEqual(sent["method"], "SendMessage")
        self.assertEqual(sent["params"]["message"]["role"], "ROLE_USER")

    async def test_jira_not_connected_is_a_step_result(self):
        async with Gateway(jira_connected=False).client() as http:
            result = await agent.run("user-token", http)
        self.assertEqual(result["steps"][2], {"step": "jira", "status": 403, "detail": "account_not_connected",
                                              "connect_url": NOT_CONNECTED["connect_url"]})
        self.assertEqual(result["steps"][1]["status"], 200)

    async def test_evict_presents_the_exchanged_token_to_credentials(self):
        gateway = Gateway()
        async with gateway.client() as http:
            await agent.evict_credentials("user-token", "upstream-demo", http)
        last = gateway.requests[-1]
        self.assertEqual(str(last.url), s.CREDENTIALS_INTERNAL_URL + "/evict/upstream-demo")
        self.assertEqual(last.headers["authorization"], "Bearer gateway-token")


@unittest.skipUnless(s.ENVIRONMENT == "local", "Local synthetic Redis tests only")
class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_endpoint_requires_sign_in_and_csrf(self):
        sid, family = secrets.token_hex(32), secrets.token_hex(32)
        redis.eval(CREATE, 2, "session:" + sid, "session-family:" + family,
                   encode({"user": {"id": "synthetic"}, "csrf": "csrf-value"}, family), 60)
        self.addCleanup(redis.delete, "session:" + sid, "session-family:" + family)
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url=s.PUBLIC_URL) as anonymous:
            self.assertEqual((await anonymous.post("/api/agent/run")).status_code, 401)
        async with httpx.AsyncClient(transport=transport, base_url=s.PUBLIC_URL, cookies={s.COOKIE_NAME: sid}) as http:
            self.assertEqual((await http.post("/api/agent/run")).status_code, 403)
            # Signed in with CSRF but no stored Keycloak tokens: sign in again rather than a server error.
            ok = await http.post("/api/agent/run", headers={"Origin": s.PUBLIC_URL, "X-CSRF-Token": "csrf-value"})
            self.assertEqual(ok.status_code, 401)


if __name__ == "__main__":
    unittest.main()
