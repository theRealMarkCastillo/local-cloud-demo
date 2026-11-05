"""Reconfigure this local demo only; no tunnel or cloud resource is created."""
from pathlib import Path
import json
import subprocess
import sys
from urllib.parse import urlparse

url = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else ""
parts = urlparse(url)
if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query or parts.fragment or parts.username or parts.password:
    raise SystemExit("Provide an origin only, e.g. https://your-demo.ngrok.app")
if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
    raise SystemExit("Public demos require HTTPS")
p = Path(__file__).resolve().parents[1]
def run(*args, capture=False):
    return subprocess.run(args, cwd=p, check=True, text=True, capture_output=capture)
f = p / ".env"
lines = f.read_text().splitlines() if f.exists() else (p / ".env.example").read_text().splitlines()
lines = [line for line in lines if not line.startswith("PUBLIC_URL=")]
f.write_text("\n".join(lines) + "\nPUBLIC_URL=" + url + "\n")
f.chmod(0o600)
run("docker", "compose", "up", "-d", "--wait", "--wait-timeout", "180")
cli = ("docker", "compose", "exec", "-T", "keycloak", "/opt/keycloak/bin/kcadm.sh")
# Password expansion happens only inside the local container, never in host command text.
run("docker", "compose", "exec", "-T", "keycloak", "bash", "-ec",
    'umask 077; /opt/keycloak/bin/kcadm.sh config credentials --config /tmp/workbench-admin.config --server http://localhost:8080/identity --realm master --user admin --password "$KC_BOOTSTRAP_ADMIN_PASSWORD"')
try:
    result = run(*cli, "get", "clients", "-r", "workbench", "-q", "clientId=workbench", "--fields", "id", "--config", "/tmp/workbench-admin.config", capture=True)
    clients = json.loads(result.stdout)
    if len(clients) != 1:
        raise SystemExit("Expected exactly one local workbench client")
    run(*cli, "update", "clients/" + clients[0]["id"], "-r", "workbench",
        "-s", "redirectUris=" + json.dumps([url + "/auth/callback"]),
        "-s", "webOrigins=" + json.dumps([url]), "--config", "/tmp/workbench-admin.config")
finally:
    run("docker", "compose", "exec", "-T", "keycloak", "rm", "-f", "/tmp/workbench-admin.config")
run("docker", "compose", "exec", "-T", "redis", "redis-cli", "EVAL",
    "local ks=redis.call('keys','session:*'); local fs=redis.call('keys','session-family:*'); for _,k in ipairs(fs) do table.insert(ks,k) end; for _,k in ipairs(ks) do redis.call('del',k) end; return #ks", "0")
print("Local origin and Keycloak callback updated:", url)
