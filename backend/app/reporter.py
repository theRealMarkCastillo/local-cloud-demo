"""Reporter agent: a second in-stack agent, reached agent-to-agent (A2A) through the gateway.

It reports on the calling user's jobs. It never forwards the token it was given: it exchanges that token for
one of its own, limited to what `reporter-agent` may do for the user, and calls the workbench MCP server
through the gateway with it."""
import asyncio
import logging
import re
from collections import Counter
from contextlib import asynccontextmanager
import httpx
from a2a.helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette
from starlette.authentication import SimpleUser
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from app import mcp_auth
from app import settings as s
from app.gateway_client import call_tool, exchange

log = logging.getLogger("reporter")
AUDIENCE = s.REPORTER_CLIENT_ID
BUDGET_SECONDS = 20  # for the whole report; the calling agent waits 30 s
LIST_LIMIT = 50  # list_jobs returns at most this many, newest first
TRACEPARENT = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
CARD = AgentCard(
    name="Workbench reporter", description="Reports on the signed-in user's jobs.", version="1",
    # Agents reach this one through the gateway, which also rewrites this URL when it serves the card.
    supported_interfaces=[AgentInterface(url=s.PUBLIC_URL + "/gateway/a2a/reporter", protocol_binding="JSONRPC",
                                         protocol_version="1.0")],
    capabilities=AgentCapabilities(streaming=False), default_input_modes=["text/plain"], default_output_modes=["text/plain"],
    skills=[AgentSkill(id="job_report", name="Job report", description="Summarise the signed-in user's jobs.", tags=["jobs"])])


class ReportError(Exception):
    """The report could not be produced; `message` is safe to show to the calling agent."""

    def __init__(self, message: str, outcome: str):
        super().__init__(message)
        self.message, self.outcome = message, outcome


def summarise(jobs: list[dict]) -> str:
    if not jobs:
        return "0 jobs"
    counts = Counter(str(job.get("status", "unknown")) for job in jobs)
    noun = "job" if len(jobs) == 1 else "jobs"
    latest = "latest " if len(jobs) >= LIST_LIMIT else ""  # a full page is not a total
    return f"{latest}{len(jobs)} {noun}: " + ", ".join(f"{counts[status]} {status}" for status in sorted(counts))


async def job_report(token: str, traceparent: str | None, http: httpx.AsyncClient) -> str:
    """Exchange the incoming token for the reporter's own gateway token, list the user's jobs and summarise them."""
    extra = {"traceparent": traceparent} if traceparent and TRACEPARENT.fullmatch(traceparent) else None
    try:
        async with asyncio.timeout(BUDGET_SECONDS):
            exchanged = await exchange(http, AUDIENCE, s.required("REPORTER_AGENT_SECRET"), token)
            if exchanged.status_code != 200:
                log.warning("reporter token exchange returned %s", exchanged.status_code)
                raise ReportError("could not act for this user", "exchange_refused")
            status, body = await call_tool(http, exchanged.json()["access_token"], "/gateway/mcp/workbench", "list_jobs",
                                           client_name=AUDIENCE, extra_headers=extra)
    except (httpx.HTTPError, TimeoutError) as error:
        log.warning("reporter could not reach Keycloak or the gateway: %s", type(error).__name__)
        raise ReportError("could not act for this user", "unreachable") from None
    result = body.get("result") if isinstance(body, dict) else None
    if status == 200 and isinstance(result, dict) and "structuredContent" in result and not result.get("isError"):
        return summarise(result["structuredContent"].get("jobs", []))
    refused = status in (401, 403) or (status == 200 and isinstance(body, dict) and ("error" in body or result))
    if refused:
        raise ReportError("not allowed to read jobs", "denied")
    # Anything else (5xx, rate limit, an empty answer) is an outage, not a permission problem.
    log.warning("reporter list_jobs call returned %s", status)
    raise ReportError("could not act for this user", "unreachable")


class Reporter(AgentExecutor):
    def __init__(self, http_factory):
        self.http_factory = http_factory

    async def execute(self, context, event_queue):
        state = context.call_context.state
        headers = state.get("headers", {})
        sub = str((state.get("auth") or {}).get("sub", ""))
        # Set by the gateway from the caller's validated token; a client cannot choose it. Kept log-safe anyway.
        caller = re.sub(r"[^A-Za-z0-9._:-]", "", headers.get("x-calling-agent", ""))[:64]
        if context.current_task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            async with self.http_factory() as http:
                report = await job_report(headers.get("authorization", "").partition(" ")[2], headers.get("traceparent"), http)
        except ReportError as error:
            log.info("reporter sub=%s caller=%s outcome=%s", sub, caller, error.outcome)
            await updater.failed(updater.new_agent_message([new_text_part(error.message)]))
            return
        log.info("reporter sub=%s caller=%s outcome=ok", sub, caller)
        await updater.add_artifact([new_text_part(report)], name="job_report")
        await updater.complete()

    async def cancel(self, context, event_queue):
        raise NotImplementedError("job_report finishes in one call; there is nothing to cancel")


class RequireToken:
    """Every path except /health needs a Keycloak token addressed to this agent."""

    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/health":
            return await self.app(scope, receive, send)
        scheme, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
        claims = await self.state["verifier"].claims(token) if scheme.lower() == "bearer" and token else None
        if claims is None:
            return await JSONResponse({"error": "invalid_token"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        # Tasks are stored per user: the A2A handler scopes them by this user name.
        scope["auth"], scope["user"] = claims, SimpleUser(str(claims["sub"]))
        await self.app(scope, receive, send)


async def health(request):
    return JSONResponse({"status": "ok"})


def create_app(verifier: mcp_auth.KeycloakTokenVerifier | None = None, http_factory=None):
    state = {"verifier": verifier}
    handler = DefaultRequestHandler(agent_executor=Reporter(http_factory or (lambda: httpx.AsyncClient(timeout=15))),
                                    task_store=InMemoryTaskStore(), agent_card=CARD)

    @asynccontextmanager
    async def lifespan(app):
        if not log.handlers:  # uvicorn configures only its own loggers; without this the audit lines are dropped
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
            log.propagate = False
        if state["verifier"] is None:
            state["verifier"] = mcp_auth.from_settings(AUDIENCE, None)
        yield

    routes = [Route("/health", health)] + create_agent_card_routes(CARD) + create_jsonrpc_routes(handler, "/")
    return RequireToken(Starlette(routes=routes, lifespan=lifespan), state)


app = create_app()
