"""Desired Keycloak state for the outbound gateway: governance, audiences and the upstream provider."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import configure_gateway

SECRETS = {"gateway": "g", "credentials": "c", "workbench_agent": "w", "broker": "b"}
PUBLIC = "https://demo.example"


def desired(public=PUBLIC):
    return configure_gateway.desired(public, SECRETS)


def client(state, client_id):
    return next(c for c in state["clients"] if c["clientId"] == client_id)


class DesiredTests(unittest.TestCase):
    def test_agent_clients_have_no_full_scope(self):
        state = desired()
        names = [c["clientId"] for c in state["clients"]
                 if c["clientId"].startswith("agent-") or c["clientId"] in ("workbench-agent", "workbench")]
        self.assertEqual(sorted(names), ["agent-cli", "agent-cli-writer", "workbench", "workbench-agent"])
        for name in names:
            self.assertIs(client(state, name)["fullScopeAllowed"], False, name)

    def test_only_credentials_client_maps_read_token(self):
        mappings = desired()["scope_mappings"]
        holders = [name for name, roles in mappings.items() if "broker/read-token" in roles]
        self.assertEqual(holders, ["gateway-credentials"])

    def test_agent_audiences_include_gateway_and_credentials(self):
        state = desired()
        for name in ("agent-cli", "agent-cli-writer", "workbench-agent"):
            audiences = {m["config"].get("included.client.audience") for m in client(state, name)["protocolMappers"]}
            self.assertLessEqual({"gateway", "gateway-credentials"}, audiences, name)

    def test_workbench_client_adds_workbench_agent_audience(self):
        audiences = {m["config"].get("included.client.audience") for m in client(desired(), "workbench")["protocolMappers"]}
        self.assertIn("workbench-agent", audiences)

    def test_workbench_redirects_include_link_callback(self):
        self.assertEqual(client(desired(), "workbench")["redirectUris"],
                         [PUBLIC + "/auth/callback", PUBLIC + "/auth/link-callback"])

    def test_agent_scope_mappings_match_spec(self):
        mappings = {k: set(v) for k, v in desired()["scope_mappings"].items()}
        self.assertEqual(mappings["agent-cli"], {"llm:use", "workbench:read", "jira:use", "echo:use"})
        self.assertEqual(mappings["agent-cli-writer"], {"workbench:read", "workbench:write"})
        self.assertEqual(mappings["workbench-agent"], {"llm:use", "workbench:read", "jira:use"})

    def test_groups_split_jira(self):
        groups = desired()["groups"]
        self.assertNotIn("jira:use", groups["gateway-users"])
        self.assertIn("broker/read-token", groups["gateway-users"])
        self.assertEqual(groups["gateway-jira"], ["jira:use"])

    def test_idp_urls_follow_public_url(self):
        idp = desired()["idp"]
        config = idp["config"]
        self.assertTrue(config["authorizationUrl"].startswith(PUBLIC + "/identity/realms/upstream-demo/"))
        for key in ("tokenUrl", "jwksUrl", "userInfoUrl"):
            self.assertTrue(config[key].startswith("http://localhost:8080/identity/realms/upstream-demo/"), key)
        self.assertIs(idp["linkOnly"], True)
        self.assertIs(idp["storeToken"], True)
        self.assertEqual(config["defaultScope"], "openid jira.read")


class EnvTests(unittest.TestCase):
    def env_file(self, text):
        folder = Path(tempfile.mkdtemp())
        (folder / ".env").write_text(text)
        return patch.object(configure_gateway, "ROOT", folder)

    def test_env_prefers_environment_then_env_file(self):
        with self.env_file("X_SECRET=from-file\nY_SECRET='quoted'\n"):
            with patch.dict(os.environ, {"X_SECRET": "from-env"}, clear=True):
                self.assertEqual(configure_gateway.env("X_SECRET", "d"), "from-env")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(configure_gateway.env("X_SECRET", "d"), "from-file")
                self.assertEqual(configure_gateway.env("Y_SECRET", "d"), "quoted")
                self.assertEqual(configure_gateway.env("MISSING", "d"), "d")


if __name__ == "__main__":
    unittest.main()
