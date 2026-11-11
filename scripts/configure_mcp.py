"""Apply the MCP client scope and public PKCE client to the local realm; idempotent, users preserved."""
import json
import os
import subprocess
from pathlib import Path
from keycloak_admin import admin

ROOT = Path(__file__).resolve().parents[1]
SCOPE, CLIENT, MAPPER = "mcp:tools", "workbench-mcp", "mcp-audience"


def public_url():
    # Same precedence as Compose: the shell environment wins over .env.
    value = os.environ.get("PUBLIC_URL", "")
    env = ROOT / ".env"
    if not value and env.exists():
        lines = env.read_text().splitlines()
        value = next((line.split("=", 1)[1] for line in lines if line.startswith("PUBLIC_URL=")), "")
    value = value.strip().strip("\"'")
    return (value or "http://localhost:8080").rstrip("/")


def main():
    resource = public_url() + "/mcp"
    mapper = {"name": MAPPER, "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
              "config": {"included.custom.audience": resource, "access.token.claim": "true",
                         "id.token.claim": "false", "introspection.token.claim": "true"}}
    client = {"clientId": CLIENT, "name": "Workbench MCP clients", "enabled": True, "protocol": "openid-connect",
              "publicClient": True, "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
              "implicitFlowEnabled": False, "serviceAccountsEnabled": False,
              "redirectUris": ["http://127.0.0.1/*", "http://localhost/*"], "webOrigins": [],
              "attributes": {"pkce.code.challenge.method": "S256"}}
    created = []
    with admin() as run:
        def get(path, *args):
            return json.loads(run("get", path, "-r", "workbench", *args, capture_output=True).stdout)

        scopes = [item for item in get("client-scopes", "--fields", "id,name") if item["name"] == SCOPE]
        if scopes:
            scope_id = scopes[0]["id"]
        else:
            scope_id = run("create", "client-scopes", "-r", "workbench", "-i", "-f", "-", capture_output=True, input=json.dumps(
                {"name": SCOPE, "protocol": "openid-connect", "description": "Call Workbench MCP tools",
                 "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "true"}})).stdout.strip()
            created.append("scope")
        mappers = [item for item in get(f"client-scopes/{scope_id}/protocol-mappers/models") if item["name"] == MAPPER]
        if mappers:
            run("update", f"client-scopes/{scope_id}/protocol-mappers/models/{mappers[0]['id']}", "-r", "workbench",
                "-f", "-", input=json.dumps(mapper | {"id": mappers[0]["id"]}), stdout=subprocess.DEVNULL)
        else:
            run("create", f"client-scopes/{scope_id}/protocol-mappers/models", "-r", "workbench", "-f", "-",
                input=json.dumps(mapper), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            created.append("audience mapper")
        clients = get("clients", "-q", "clientId=" + CLIENT, "--fields", "id")
        if clients:
            client_id = clients[0]["id"]
            run("update", "clients/" + client_id, "-r", "workbench", "-f", "-", input=json.dumps(client))
        else:
            client_id = run("create", "clients", "-r", "workbench", "-i", "-f", "-", capture_output=True,
                            input=json.dumps(client)).stdout.strip()
            created.append("client")
        optional = {item["name"] for item in get(f"clients/{client_id}/optional-client-scopes")}
        if SCOPE not in optional:
            run("update", f"clients/{client_id}/optional-client-scopes/{scope_id}", "-r", "workbench", "-b", "{}")
            created.append("optional scope link")
    print(f"Configured local MCP scope and client for {resource}; created: {', '.join(created) or 'nothing (already present)'}")


if __name__ == "__main__":
    main()
