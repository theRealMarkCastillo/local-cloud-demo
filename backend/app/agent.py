"""Sample in-stack agent. It acts for the signed-in user as `workbench-agent` and reaches everything through the gateway."""
import secrets
import httpx
from app import settings as s
from app.gateway_client import call_tool, exchange, payload
from app.user_tokens import SignInRequired


async def gateway_token(user_access_token: str, http: httpx.AsyncClient) -> str:
    """Exchange the user's session token for one addressed to the gateway, issued to this agent's client."""
    response = await exchange(http, "workbench-agent", s.required("WORKBENCH_AGENT_SECRET"), user_access_token)
    if response.status_code in (400, 401):
        raise SignInRequired()  # the user's session at the identity provider has ended
    response.raise_for_status()
    return response.json()["access_token"]


def tool_step(step: str, status: int, body: dict, describe) -> dict:
    result = body.get("result", {}) if isinstance(body, dict) else {}
    if status == 200 and "structuredContent" in result and not result.get("isError"):
        return {"step": step, "status": 200, "detail": describe(result["structuredContent"])}
    if isinstance(body, dict) and body.get("error") == "account_not_connected":
        return {"step": step, "status": status, "detail": "account_not_connected", "connect_url": body.get("connect_url", "")}
    return {"step": step, "status": status if status != 200 else 502, "detail": "failed"}


async def ask_reporter(http: httpx.AsyncClient, token: str) -> dict:
    """Agent to agent: ask the reporter, through the gateway, for a report on the user's jobs."""
    # A fresh trace id lets the gateway's two audit lines (this hop and the reporter's own call) be joined.
    traceparent = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    response = await http.post(
        s.GATEWAY_INTERNAL_URL + "/gateway/a2a/reporter",
        headers={"Authorization": "Bearer " + token, "A2A-Version": "1.0", "traceparent": traceparent},
        json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": {
            "role": "ROLE_USER", "messageId": secrets.token_hex(8), "parts": [{"text": "Report on my jobs"}]}}})
    task = (payload(response).get("result") or {}).get("task") or {} if response.status_code == 200 else {}
    if not task:
        return {"step": "reporter", "status": response.status_code if response.status_code != 200 else 502, "detail": "failed"}

    def first_text(parts) -> str:
        return next((str(part["text"]) for part in parts or [] if isinstance(part, dict) and "text" in part), "")

    status = task.get("status") or {}
    if status.get("state") == "TASK_STATE_COMPLETED":
        report = next((first_text(artifact.get("parts")) for artifact in task.get("artifacts") or []), "")
        return {"step": "reporter", "status": 200, "detail": report}
    return {"step": "reporter", "status": 502, "detail": first_text((status.get("message") or {}).get("parts")) or "failed"}


async def run(user_access_token: str, http: httpx.AsyncClient) -> dict:
    """A fixed four-step script: ask the LLM, list the user's jobs, ask the SaaS stand-in who we are, ask the reporter agent."""
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
    steps.append(await ask_reporter(http, token))
    return {"steps": steps}


async def evict_credentials(user_access_token: str, alias: str, http: httpx.AsyncClient) -> None:
    """Make a disconnect take effect at once: the credentials service caches upstream tokens briefly."""
    token = await gateway_token(user_access_token, http)
    await http.post(f"{s.CREDENTIALS_INTERNAL_URL}/evict/{alias}", headers={"Authorization": "Bearer " + token})
