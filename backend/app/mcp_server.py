"""MCP resource server: the signed-in user's jobs and reports, authorized by Keycloak tokens for this resource."""
import json
import uuid
from typing import Annotated, Any
from urllib.parse import urlparse
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from app import jobs, mcp_auth
from app import settings as s
from app.identity import owner_id

server = MCPServer(
    name="workbench",
    instructions="Submit text-analysis jobs and read their JSON reports for the signed-in user.",
    token_verifier=mcp_auth.from_settings(),
    auth=AuthSettings(issuer_url=s.required("OIDC_ISSUER_URL"), resource_server_url=s.MCP_RESOURCE_URL,
                      required_scopes=[s.MCP_SCOPE], validate_token_resource=True),
)


def caller() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("Not authenticated")
    return owner_id(s.OIDC_ISSUER_URL, token.subject)


def as_text(row: dict) -> dict:
    return {key: (None if value is None else value if isinstance(value, (int, str)) else str(value))
            for key, value in row.items()}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
def submit_job(title: Annotated[str, Field(min_length=1, max_length=120)],
               text: Annotated[str, Field(min_length=1, max_length=20000)]) -> dict[str, Any]:
    """Queue a text-analysis job. Returns the job id; poll list_jobs until its status is complete."""
    return {"id": jobs.create_job(caller(), title, text)}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_jobs() -> dict[str, Any]:
    """List the caller's 50 most recent jobs, newest first."""
    return {"jobs": [as_text(row) for row in jobs.list_jobs(caller())]}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_report(job_id: str) -> dict[str, Any]:
    """Return the JSON report for one of the caller's completed jobs."""
    owner = caller()
    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        raise ToolError("Report not found")
    data = jobs.read_report(owner, parsed)
    if data is None:
        raise ToolError("Report not found")
    return json.loads(data)


@server.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok"})


public = urlparse(s.PUBLIC_URL)
app = server.streamable_http_app(
    stateless_http=True,
    json_response=True,
    max_request_body_size=65536,
    # "mcp" is the Compose-internal name the outbound gateway calls; it is not reachable from a browser.
    transport_security=TransportSecuritySettings(allowed_hosts=[public.netloc, "mcp", "mcp:8000"], allowed_origins=[s.PUBLIC_URL]),
)
