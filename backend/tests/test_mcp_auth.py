"""Bearer token validation for the MCP service, using synthetic local keys only."""
import time
import unittest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from app.mcp_auth import KeycloakTokenVerifier, jwks_client

ISS = "https://id.example/realms/workbench"
AUD = "http://localhost:8080/mcp"
SCOPE = "mcp:tools"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StubJwks:
    """Stands in for PyJWKClient: returns the test public key, optionally failing first."""

    def __init__(self, failures=0):
        self.failures, self.calls = failures, 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        if self.calls <= self.failures:
            raise jwt.PyJWKClientError("Unable to find a signing key that matches")
        return jwt.PyJWK.from_dict(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1"})


def claims(**overrides):
    now = int(time.time())
    value = {"iss": ISS, "aud": AUD, "sub": "user-1", "azp": "workbench-mcp",
             "scope": "openid mcp:tools", "iat": now, "exp": now + 300}
    value.update(overrides)
    return {k: v for k, v in value.items() if v is not None}


def token(key=KEY, **overrides):
    return jwt.encode(claims(**overrides), key, algorithm="RS256", headers={"kid": "k1"})


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    def verifier(self, jwks=None):
        self.jwks = jwks or StubJwks()
        return KeycloakTokenVerifier(ISS, AUD, SCOPE, self.jwks)

    async def test_valid_token_accepted(self):
        result = await self.verifier().verify_token(token())
        self.assertEqual(result.subject, "user-1")
        self.assertEqual(result.resource, AUD)
        self.assertEqual(result.client_id, "workbench-mcp")
        self.assertIn("mcp:tools", result.scopes)

    async def test_aud_list_containing_resource_accepted(self):
        self.assertIsNotNone(await self.verifier().verify_token(token(aud=[AUD, "account"])))

    async def test_wrong_audience_rejected(self):
        self.assertIsNone(await self.verifier().verify_token(token(aud="workbench")))

    async def test_wrong_issuer_rejected(self):
        self.assertIsNone(await self.verifier().verify_token(token(iss="https://evil.example/realms/workbench")))

    async def test_expired_rejected(self):
        now = int(time.time())
        self.assertIsNone(await self.verifier().verify_token(token(iat=now - 600, exp=now - 120)))

    async def test_missing_scope_rejected(self):
        self.assertIsNone(await self.verifier().verify_token(token(scope="openid profile")))

    async def test_missing_sub_rejected(self):
        self.assertIsNone(await self.verifier().verify_token(token(sub=None)))

    async def test_bad_signature_rejected(self):
        self.assertIsNone(await self.verifier().verify_token(token(key=OTHER_KEY)))

    async def test_alg_none_rejected(self):
        unsigned = jwt.encode(claims(), None, algorithm="none")
        self.assertIsNone(await self.verifier().verify_token(unsigned))

    async def test_garbage_token_rejected(self):
        self.assertIsNone(await self.verifier().verify_token("not-a-jwt"))

    async def test_unknown_kid_refetches_once(self):
        verifier = self.verifier(StubJwks(failures=1))
        self.assertIsNotNone(await verifier.verify_token(token()))
        self.assertEqual(self.jwks.calls, 2)

    async def test_repeated_key_failure_rejected(self):
        verifier = self.verifier(StubJwks(failures=5))
        self.assertIsNone(await verifier.verify_token(token()))
        self.assertEqual(self.jwks.calls, 2)


class JwksClientTests(unittest.TestCase):
    def test_signing_keys_are_not_cached_beyond_the_key_set(self):
        # A per-kid cache without expiry would keep trusting keys Keycloak has removed.
        client = jwks_client("http://keycloak.invalid/certs")
        self.assertFalse(hasattr(client.get_signing_key, "cache_info"))
        self.assertIsNotNone(client.jwk_set_cache)


if __name__ == "__main__":
    unittest.main()
