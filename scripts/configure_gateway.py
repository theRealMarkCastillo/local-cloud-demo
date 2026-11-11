"""Apply the outbound-gateway clients, roles, groups and upstream-demo provider to the local realm; idempotent."""
import json
import os
from pathlib import Path
from keycloak_admin import admin

ROOT = Path(__file__).resolve().parents[1]
REALM = "workbench"
UPSTREAM = "upstream-demo"
INTERNAL = "http://localhost:8080/identity/realms/" + UPSTREAM + "/protocol/openid-connect"
ROLES = ["llm:use", "workbench:read", "workbench:write", "jira:use", "echo:use"]
LOOPBACK = ["http://127.0.0.1/*", "http://localhost/*"]
ACCOUNT_ROLES = ["account/manage-account", "account/manage-account-links", "account/view-profile"]


def env(name, default=""):
    # Same precedence as Compose: the shell environment wins over .env.
    value = os.environ.get(name, "")
    path = ROOT / ".env"
    if not value and path.exists():
        lines = path.read_text().splitlines()
        value = next((line.split("=", 1)[1] for line in lines if line.startswith(name + "=")), "")
    return value.strip().strip("\"'") or default


def public_url():
    return env("PUBLIC_URL", "http://localhost:8080").rstrip("/")


def secrets():
    return {"gateway": env("GATEWAY_CLIENT_SECRET", "local-only-gateway-secret"),
            "credentials": env("CREDENTIALS_CLIENT_SECRET", "local-only-credentials-secret"),
            "workbench_agent": env("WORKBENCH_AGENT_SECRET", "local-only-agent-secret"),
            "broker": env("UPSTREAM_BROKER_SECRET", "local-only-broker-secret")}


def audience(name, client_id):
    return {"name": name, "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": client_id, "access.token.claim": "true",
                       "id.token.claim": "false", "introspection.token.claim": "true"}}


def agent_mappers(public):
    # The client-id audiences let `gateway` and `gateway-credentials` exchange the token. The gateway itself
    # accepts only the resource audience: Keycloak adds client-id audiences to any token carrying that
    # client's roles, so a full-scope client would otherwise pass as an agent.
    resource = {"name": "gateway-resource-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                "config": {"included.custom.audience": public + "/gateway", "access.token.claim": "true",
                           "id.token.claim": "false", "introspection.token.claim": "true"}}
    return [audience("gateway-audience", "gateway"), audience("credentials-audience", "gateway-credentials"), resource]


def confidential(client_id, secret, **extra):
    return {"clientId": client_id, "enabled": True, "protocol": "openid-connect", "publicClient": False, "secret": secret,
            "standardFlowEnabled": False, "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
            "serviceAccountsEnabled": False, "fullScopeAllowed": False,
            "attributes": {"standard.token.exchange.enabled": "true"}} | extra


def public_agent(client_id, name, public):
    return {"clientId": client_id, "name": name, "enabled": True, "protocol": "openid-connect", "publicClient": True,
            "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
            "serviceAccountsEnabled": False, "fullScopeAllowed": False, "redirectUris": LOOPBACK, "webOrigins": [],
            "attributes": {"pkce.code.challenge.method": "S256"}, "protocolMappers": agent_mappers(public)}


def desired(public, secret):
    clients = [
        confidential("gateway", secret["gateway"], name="Outbound gateway"),
        confidential("gateway-credentials", secret["credentials"], name="Gateway credentials service"),
        confidential("workbench-agent", secret["workbench_agent"], name="Workbench in-stack agent",
                     protocolMappers=agent_mappers(public)),
        public_agent("agent-cli", "Sample external agent", public),
        public_agent("agent-cli-writer", "Sample external agent that may write", public),
        {"clientId": "workbench", "fullScopeAllowed": False,
         "redirectUris": [public + "/auth/callback", public + "/auth/link-callback"],
         "protocolMappers": [audience("workbench-agent-audience", "workbench-agent")]},
    ]
    scope_mappings = {
        "agent-cli": ["llm:use", "workbench:read", "jira:use", "echo:use"],
        "agent-cli-writer": ["workbench:read", "workbench:write"],
        "workbench-agent": ["llm:use", "workbench:read", "jira:use"],
        "gateway-credentials": ["broker/read-token"],
        # Session tokens may call the account API (list/unlink connections) but never read stored tokens.
        "workbench": list(ACCOUNT_ROLES),
    }
    groups = {"gateway-users": ["llm:use", "workbench:read", "workbench:write", "echo:use", "broker/read-token"],
              "gateway-jira": ["jira:use"]}
    idp = {"alias": UPSTREAM, "displayName": "Upstream demo", "providerId": "oidc", "enabled": True,
           "storeToken": True, "linkOnly": True, "trustEmail": False, "addReadTokenRoleOnCreate": False,
           "config": {"authorizationUrl": f"{public}/identity/realms/{UPSTREAM}/protocol/openid-connect/auth",
                      "tokenUrl": INTERNAL + "/token", "userInfoUrl": INTERNAL + "/userinfo",
                      "jwksUrl": INTERNAL + "/certs", "issuer": f"{public}/identity/realms/{UPSTREAM}",
                      "useJwksUrl": "true", "validateSignature": "true", "clientId": "workbench-broker",
                      "clientSecret": secret["broker"], "clientAuthMethod": "client_secret_post",
                      "defaultScope": "openid jira.read", "syncMode": "IMPORT",
                      "pkceEnabled": "true", "pkceMethod": "S256"}}
    return {"clients": clients, "roles": list(ROLES), "groups": groups, "scope_mappings": scope_mappings, "idp": idp}


def main():
    public, secret = public_url(), secrets()
    state = desired(public, secret)
    created = []
    with admin() as run:
        def get(path, *args, realm=REALM):
            return json.loads(run("get", path, "-r", realm, *args, capture_output=True).stdout)

        def put(method, path, body, *args, realm=REALM):
            return run(method, path, "-r", realm, *args, "-f", "-", input=json.dumps(body),
                       capture_output=True).stdout.strip()

        def client_id(name, realm=REALM):
            found = [c for c in get("clients", "-q", "clientId=" + name, "--fields", "id,clientId", realm=realm)
                     if c["clientId"] == name]
            return found[0]["id"] if found else None

        def role_rep(client, name):
            return get(f"clients/{client_id(client)}/roles/{name}")

        ids = {}
        for rep in state["clients"]:
            body = {k: v for k, v in rep.items() if k != "protocolMappers"}
            existing = client_id(rep["clientId"])
            if existing:
                put("update", "clients/" + existing, body)
                ids[rep["clientId"]] = existing
            else:
                ids[rep["clientId"]] = put("create", "clients", body, "-i")
                created.append("client " + rep["clientId"])
            have = {m["name"]: m["id"] for m in get(f"clients/{ids[rep['clientId']]}/protocol-mappers/models")}
            for mapper in rep.get("protocolMappers", []):
                path = f"clients/{ids[rep['clientId']]}/protocol-mappers/models"
                if mapper["name"] in have:
                    put("update", f"{path}/{have[mapper['name']]}", mapper | {"id": have[mapper["name"]]})
                else:
                    put("create", path, mapper)
                    created.append("mapper " + mapper["name"])
        # The gateway may request the MCP scope when exchanging tokens for the MCP resource.
        scopes = [s for s in get("client-scopes", "--fields", "id,name") if s["name"] == "mcp:tools"]
        if scopes:
            optional = {s["name"] for s in get(f"clients/{ids['gateway']}/optional-client-scopes")}
            if "mcp:tools" not in optional:
                run("update", f"clients/{ids['gateway']}/optional-client-scopes/{scopes[0]['id']}", "-r", REALM, "-b", "{}")
                created.append("gateway optional scope")
        have_roles = {r["name"] for r in get(f"clients/{ids['gateway']}/roles")}
        for role in state["roles"]:
            if role not in have_roles:
                run("create", f"clients/{ids['gateway']}/roles", "-r", REALM, "-s", "name=" + role)
                created.append("role " + role)

        def resolve(role):
            client, _, name = role.rpartition("/")
            return (client or "gateway"), name

        for name, roles in state["scope_mappings"].items():
            for target in {resolve(r)[0] for r in roles}:
                reps = [role_rep(*resolve(r)) for r in roles if resolve(r)[0] == target]
                put("create", f"clients/{ids[name]}/scope-mappings/clients/{client_id(target)}", reps)
        existing_groups = {g["name"]: g["id"] for g in get("groups", "--fields", "id,name")}
        for group, roles in state["groups"].items():
            if group not in existing_groups:
                existing_groups[group] = run("create", "groups", "-r", REALM, "-i", "-s", "name=" + group,
                                             capture_output=True).stdout.strip()
                created.append("group " + group)
            for role in roles:
                client, name = resolve(role)
                run("add-roles", "-r", REALM, "--gname", group, "--cclientid", client, "--rolename", name)
        members = {"demo": ["gateway-users", "gateway-jira"], "demo-other": ["gateway-users"]}
        for username, groups in members.items():
            users = get("users", "-q", "username=" + username, "--fields", "id,username")
            user = next(u["id"] for u in users if u["username"] == username)
            for group in groups:
                run("update", f"users/{user}/groups/{existing_groups[group]}", "-r", REALM, "-n",
                    "-s", f"realm={REALM}", "-s", f"userId={user}", "-s", f"groupId={existing_groups[group]}")
        run("add-roles", "-r", REALM, "--rname", "default-roles-" + REALM, "--cclientid", "account",
            "--rolename", "manage-account-links")
        # Upstream stand-in realm: scope, redirect URI and the link-only provider.
        broker = client_id("workbench-broker", UPSTREAM)
        if not broker:
            raise SystemExit("Realm upstream-demo is missing; recreate Keycloak so it imports identity/upstream-demo-realm.json")
        put("update", "clients/" + broker, {"redirectUris": [f"{public}/identity/realms/{REALM}/broker/{UPSTREAM}/endpoint*"]},
            realm=UPSTREAM)
        up_scopes = [s for s in get("client-scopes", "--fields", "id,name", realm=UPSTREAM) if s["name"] == "jira.read"]
        if up_scopes:
            scope_id = up_scopes[0]["id"]
        else:
            scope_id = run("create", "client-scopes", "-r", UPSTREAM, "-i", "-s", "name=jira.read", "-s", "protocol=openid-connect",
                           "-s", "attributes.\"include.in.token.scope\"=true", capture_output=True).stdout.strip()
            created.append("upstream scope")
        run("update", f"clients/{broker}/optional-client-scopes/{scope_id}", "-r", UPSTREAM, "-b", "{}")
        idps = [i for i in get("identity-provider/instances") if i["alias"] == UPSTREAM]
        if idps:
            put("update", "identity-provider/instances/" + UPSTREAM, state["idp"])
        else:
            put("create", "identity-provider/instances", state["idp"])
            created.append("identity provider")
    print(f"Configured the outbound gateway for {public}; created: {', '.join(created) or 'nothing (already present)'}")


if __name__ == "__main__":
    main()
