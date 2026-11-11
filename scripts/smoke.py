"""Exercise real OIDC login and the full stack using only Python's stdlib."""
import http.cookiejar
import json
import os
import ssl
import subprocess
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

local_env = Path(__file__).resolve().parents[1] / ".env"
configured = "http://localhost:8080"
if local_env.exists():
    configured = next((line.split("=",1)[1].strip() for line in local_env.read_text().splitlines() if line.startswith("PUBLIC_URL=")), configured)
BASE = os.getenv("DEMO_BASE_URL", configured).rstrip("/")
host = urllib.parse.urlparse(BASE).hostname or ""
if host not in ("localhost", "127.0.0.1") and not host.endswith(".orb.local"):
    raise SystemExit("This synthetic smoke test is restricted to localhost or local OrbStack domains")
TLS = ssl.create_default_context(cafile=os.getenv("DEMO_CA_FILE"))
if host.endswith(".orb.local") and not os.getenv("DEMO_CA_FILE"):
    # Read the existing public CA certificate; never disable TLS verification.
    cert = subprocess.run(["security", "find-certificate", "-a", "-c", "OrbStack", "-p"], capture_output=True, text=True, check=True)
    if cert.stdout.strip():
        TLS.load_verify_locations(cadata=cert.stdout)

class LoginForm(HTMLParser):
    action = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            self.action = attrs["action"]

def cookies():
    # Browsers treat localhost as trustworthy for Secure cookies; urllib does not.
    # This script is restricted to localhost above, so permit its HTTP cookies.
    return urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(secure_protocols=("https", "wss", "http"))))

def client():
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=TLS), cookies())

def fetch(opener, path, method="GET", body=None, token=None, origin=BASE, expected=200):
    headers = {}
    if method != "GET":
        headers = {"Origin": origin, "Content-Type": "application/json"}
        if token is not None:
            headers["X-CSRF-Token"] = token
    data = json.dumps(body or {}).encode() if method != "GET" else None
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        response = opener.open(request, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    assert response.status == expected, f"{method} {path}: expected {expected}, got {response.status}"
    raw = response.read()
    return json.loads(raw) if raw else None

def login(opener, username="demo"):
    page = opener.open(BASE + "/auth/login", timeout=20)
    parser = LoginForm(); parser.feed(page.read().decode())
    assert parser.action, "OIDC login form not found"
    # Demo-only credentials. Override if changed in the local realm.
    password = os.getenv("DEMO_PASSWORD", "demo-password")
    data = urllib.parse.urlencode({"username": username, "password": password, "credentialId": ""}).encode()
    response = opener.open(parser.action, data=data, timeout=20)
    assert urllib.parse.urlparse(response.url).path == "/", "Login did not return to the app"
    return fetch(opener, "/api/me")

anon = client()
fetch(anon, "/health")
fetch(anon, "/api/jobs", expected=401)
fetch(anon, "/api/jobs", "POST", {"title": "blocked", "text": "blocked"}, expected=401)
print("PASS: readiness and unauthenticated access rejection")
authed = client(); me = login(authed); token = me["csrf"]
fetch(authed, "/api/jobs", "POST", {"title": "blocked", "text": "blocked"}, expected=403)
fetch(authed, "/api/jobs", "POST", {"title": "blocked", "text": "blocked"}, token, origin="https://wrong.example", expected=403)
print("PASS: OIDC authorization code login, CSRF and origin rejection")
job = fetch(authed, "/api/jobs", "POST", {"title": "Smoke test", "text": "portable portable worker"}, token, expected=201)
for attempt in range(30):
    row = next(item for item in fetch(authed, "/api/jobs")["jobs"] if item["id"] == job["id"])
    if row["status"] == "complete":
        break
    assert row["status"] != "failed", "Worker failed"
    time.sleep(1)
else:
    raise AssertionError("Worker did not complete within 30 seconds")
report = fetch(authed, f"/api/jobs/{job['id']}/report")
assert report["words"] == 3 and report["top_words"][0] == ["portable", 2]
print("PASS: Postgres job → worker → private MinIO object → authenticated download")
other = client(); login(other, "demo-other")
assert all(item["id"] != job["id"] for item in fetch(other, "/api/jobs")["jobs"])
fetch(other, f"/api/jobs/{job['id']}/report", expected=404)
fetch(other, "/auth/logout", "POST", token=fetch(other, "/api/me")["csrf"])
print("PASS: a second OIDC user cannot list or download another user's report")
fetch(authed, "/auth/logout", "POST", token=token)
fetch(authed, "/api/jobs", expected=401)
fetch(authed, f"/api/jobs/{job['id']}/report", expected=401)
print("PASS: application logout invalidates session")

# --- MCP resource server: discovery, PKCE login for a public client, tools, isolation ---
import base64
import hashlib
import secrets

MCP_URL = BASE + "/mcp"
METADATA_URL = BASE + "/.well-known/oauth-protected-resource/mcp"
MCP_REDIRECT = "http://127.0.0.1:53682/callback"  # loopback; never served, only captured
PROTOCOL = "2025-11-25"

class CapturedRedirect(Exception):
    def __init__(self, url):
        self.url = url

class StopAtLoopback(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith(MCP_REDIRECT):
            raise CapturedRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=20, context=TLS) as response:
        return json.loads(response.read())

def mcp_raw(token, method, params, origin=None, request_id=1):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTOCOL}
    if token:
        headers["Authorization"] = "Bearer " + token
    if origin:
        headers["Origin"] = origin
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
    request = urllib.request.Request(MCP_URL, data=body, headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=20, context=TLS)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    return response.status, response.headers, payload

def mcp_call(token, method, params, expected=200, origin=None):
    status, _, payload = mcp_raw(token, method, params, origin)
    assert status == expected, f"MCP {method}: expected {expected}, got {status}"
    if expected == 200:
        assert "error" not in payload, f"MCP {method} JSON-RPC error: {payload.get('error')}"
        return payload["result"]
    return payload

def tool(token, name, arguments):
    return mcp_call(token, "tools/call", {"name": name, "arguments": arguments})

def mcp_token(username, scope="openid mcp:tools"):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({"client_id": "workbench-mcp", "response_type": "code", "redirect_uri": MCP_REDIRECT,
                                    "scope": scope, "state": state, "code_challenge": challenge,
                                    "code_challenge_method": "S256", "resource": MCP_URL})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=TLS), StopAtLoopback(), cookies())
    try:
        page = opener.open(oidc["authorization_endpoint"] + "?" + query, timeout=20)
        parser = LoginForm(); parser.feed(page.read().decode())
        assert parser.action, "MCP login form not found"
        password = os.getenv("DEMO_PASSWORD", "demo-password")
        data = urllib.parse.urlencode({"username": username, "password": password, "credentialId": ""}).encode()
        opener.open(parser.action, data=data, timeout=20)
        raise AssertionError("Keycloak did not redirect to the loopback callback")
    except CapturedRedirect as captured:
        returned = urllib.parse.parse_qs(urllib.parse.urlparse(captured.url).query)
    assert returned.get("state") == [state], "OAuth state mismatch"
    assert "code" in returned, f"No authorization code: {returned.get('error')}"
    form = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": "workbench-mcp", "code": returned["code"][0],
                                   "redirect_uri": MCP_REDIRECT, "code_verifier": verifier}).encode()
    with urllib.request.urlopen(urllib.request.Request(oidc["token_endpoint"], data=form), timeout=20, context=TLS) as response:
        return json.loads(response.read())["access_token"]

status, headers, _ = mcp_raw(None, "tools/list", {})
assert status == 401, f"Unauthenticated MCP request: expected 401, got {status}"
assert f'resource_metadata="{METADATA_URL}"' in (headers.get("WWW-Authenticate") or ""), "401 lacks resource_metadata pointer"
print("PASS: MCP rejects unauthenticated requests and points to its resource metadata")
resource = get_json(METADATA_URL)
issuer = BASE + "/identity/realms/workbench"
assert resource["resource"] == MCP_URL and resource["authorization_servers"][0].rstrip("/") == issuer, resource
oidc = get_json(issuer + "/.well-known/openid-configuration")
assert "S256" in oidc.get("code_challenge_methods_supported", []), "Authorization server lacks PKCE S256"
print("PASS: protected-resource metadata → authorization server discovery")
demo_token = mcp_token("demo")
initialized = mcp_call(demo_token, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}})
assert initialized.get("serverInfo"), "initialize returned no serverInfo"
names = {item["name"] for item in mcp_call(demo_token, "tools/list", {})["tools"]}
assert names == {"submit_job", "list_jobs", "get_report"}, names
created = tool(demo_token, "submit_job", {"title": "MCP smoke", "text": "portable portable worker"})
assert not created.get("isError"), created
mcp_job = created["structuredContent"]["id"]
for attempt in range(30):
    listed = tool(demo_token, "list_jobs", {})["structuredContent"]["jobs"]
    row = next(item for item in listed if item["id"] == mcp_job)
    if row["status"] == "complete":
        break
    assert row["status"] != "failed", "Worker failed"
    time.sleep(1)
else:
    raise AssertionError("MCP job did not complete within 30 seconds")
mcp_report = tool(demo_token, "get_report", {"job_id": mcp_job})
assert not mcp_report.get("isError") and mcp_report["structuredContent"]["words"] == 3, mcp_report
print("PASS: PKCE login for workbench-mcp → MCP submit_job → worker → get_report")
other_token = mcp_token("demo-other")
assert all(item["id"] != mcp_job for item in tool(other_token, "list_jobs", {})["structuredContent"]["jobs"])
assert tool(other_token, "get_report", {"job_id": mcp_job}).get("isError") is True
assert tool(demo_token, "get_report", {"job_id": "not-a-uuid"}).get("isError") is True
print("PASS: MCP tools are owner-scoped; malformed ids are tool errors")
no_audience = mcp_token("demo", scope="openid")
mcp_call(no_audience, "tools/list", {}, expected=401)
mcp_call(demo_token, "tools/list", {}, expected=403, origin="https://evil.example")
print("PASS: tokens without the MCP audience and cross-site origins are rejected")
registration = urllib.request.Request(issuer + "/clients-registrations/openid-connect", method="POST",
    data=json.dumps({"redirect_uris": [MCP_REDIRECT], "token_endpoint_auth_method": "none"}).encode(),
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(registration, timeout=20, context=TLS)
    raise AssertionError("Anonymous dynamic client registration was accepted")
except urllib.error.HTTPError as error:
    assert error.code == 403, f"Anonymous registration: expected 403, got {error.code}"
print("PASS: anonymous dynamic client registration is rejected")
