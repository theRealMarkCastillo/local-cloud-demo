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

def client():
    # Browsers treat localhost as trustworthy for Secure cookies; urllib does not.
    # This script is restricted to localhost above, so permit its HTTP cookies.
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=TLS), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(secure_protocols=("https", "wss", "http")))))

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
