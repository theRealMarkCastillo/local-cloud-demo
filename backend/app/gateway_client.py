"""What every in-stack agent needs to act for a user through the gateway: a token exchange and an MCP tool call."""
import json
import httpx
from app import settings as s

EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
PROTOCOL = "2025-11-25"
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTOCOL}


async def exchange(http: httpx.AsyncClient, client_id: str, secret: str, subject_token: str) -> httpx.Response:
    """Ask Keycloak for a token for the same user, issued to `client_id`. The caller judges the status."""
    return await http.post(s.TOKEN_URL, auth=(client_id, secret), data={
        "grant_type": EXCHANGE, "subject_token": subject_token, "subject_token_type": ACCESS_TOKEN,
        "requested_token_type": ACCESS_TOKEN})


def payload(response: httpx.Response):
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = events[-1] if events else ""
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        return {}


async def call_tool(http: httpx.AsyncClient, token: str, path: str, name: str, *, client_name: str = "workbench-agent",
                    extra_headers: dict[str, str] | None = None) -> tuple[int, dict]:
    """One MCP tool call through the gateway: initialize, then tools/call. Returns (status, body)."""
    url = s.GATEWAY_INTERNAL_URL + path
    headers = MCP_HEADERS | {"Authorization": "Bearer " + token} | (extra_headers or {})
    started = await http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": client_name, "version": "1"}}})
    if started.status_code != 200:
        return started.status_code, payload(started)
    session = started.headers.get("mcp-session-id")
    if session:
        headers = headers | {"Mcp-Session-Id": session}
    await http.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    called = await http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                         "params": {"name": name, "arguments": {}}})
    return called.status_code, payload(called)
