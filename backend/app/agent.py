"""Sample in-stack agent. It acts for the signed-in user as `workbench-agent` and reaches everything through the gateway."""
import json
import httpx
from app import settings as s
from app.user_tokens import SignInRequired

EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
PROTOCOL = "2025-11-25"
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTOCOL}


async def gateway_token(user_access_token: str, http: httpx.AsyncClient) -> str:
    """Exchange the user's session token for one addressed to the gateway, issued to this agent's client."""
    response = await http.post(s.TOKEN_URL, auth=("workbench-agent", s.required("WORKBENCH_AGENT_SECRET")), data={
        "grant_type": EXCHANGE, "subject_token": user_access_token, "subject_token_type": ACCESS_TOKEN,
        "requested_token_type": ACCESS_TOKEN})
    if response.status_code in (400, 401):
        raise SignInRequired()  # the user's session at the identity provider has ended
    response.raise_for_status()
    return response.json()["access_token"]


def payload(response: httpx.Response):
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = events[-1] if events else ""
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        return {}


async def call_tool(http: httpx.AsyncClient, token: str, path: str, name: str) -> tuple[int, dict]:
    """One MCP tool call through the gateway: initialize, then tools/call. Returns (status, body)."""
    url = s.GATEWAY_INTERNAL_URL + path
    headers = MCP_HEADERS | {"Authorization": "Bearer " + token}
    started = await http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "workbench-agent", "version": "1"}}})
    if started.status_code != 200:
        return started.status_code, payload(started)
    session = started.headers.get("mcp-session-id")
    if session:
        headers = headers | {"Mcp-Session-Id": session}
    await http.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    called = await http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                         "params": {"name": name, "arguments": {}}})
    return called.status_code, payload(called)


def tool_step(step: str, status: int, body: dict, describe) -> dict:
    result = body.get("result", {}) if isinstance(body, dict) else {}
    if status == 200 and "structuredContent" in result and not result.get("isError"):
        return {"step": step, "status": 200, "detail": describe(result["structuredContent"])}
    if isinstance(body, dict) and body.get("error") == "account_not_connected":
        return {"step": step, "status": status, "detail": "account_not_connected", "connect_url": body.get("connect_url", "")}
    return {"step": step, "status": status if status != 200 else 502, "detail": "failed"}


async def run(user_access_token: str, http: httpx.AsyncClient) -> dict:
    """A fixed three-step script: ask the LLM, list the user's jobs, ask the SaaS stand-in who we are."""
    token = await gateway_token(user_access_token, http)
    chat = await http.post(s.GATEWAY_INTERNAL_URL + "/gateway/llm/v1/chat/completions",
                           headers={"Authorization": "Bearer " + token},
                           json={"model": "fake-model", "messages": [{"role": "user", "content": "Summarise my jobs"}]})
    reply = payload(chat).get("choices", [{}])[0].get("message", {}).get("content", "") if chat.status_code == 200 else ""
    steps = [{"step": "llm", "status": chat.status_code,
              "detail": reply if chat.status_code == 200 else ("budget used up" if chat.status_code == 429 else "failed")}]
    status, body = await call_tool(http, token, "/gateway/mcp/workbench", "list_jobs")
    steps.append(tool_step("list_jobs", status, body, lambda content: f"{len(content.get('jobs', []))} jobs"))
    status, body = await call_tool(http, token, "/gateway/mcp/jira", "whoami")
    steps.append(tool_step("jira", status, body, lambda content: str(content.get("upstream_user", ""))))
    return {"steps": steps}


async def evict_credentials(user_access_token: str, alias: str, http: httpx.AsyncClient) -> None:
    """Make a disconnect take effect at once: the credentials service caches upstream tokens briefly."""
    token = await gateway_token(user_access_token, http)
    await http.post(f"{s.CREDENTIALS_INTERNAL_URL}/evict/{alias}", headers={"Authorization": "Bearer " + token})
