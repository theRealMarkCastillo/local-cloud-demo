"""The user's own Keycloak tokens: kept server-side, refreshed near expiry, dropped when refresh fails."""
import unittest
from app import user_tokens
from app.user_tokens import SignInRequired


def issued(access="a1", refresh="r1", expires_in=300):
    return {"access_token": access, "refresh_token": refresh, "expires_in": expires_in, "id_token": "ignored"}


class UserTokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session, self.calls = {"user": {"id": "u"}}, []

    async def refresh(self, refresh_token):
        self.calls.append(refresh_token)
        return issued("a2", "r2")

    async def failing(self, refresh_token):
        raise RuntimeError("invalid_grant")

    def test_save_records_expiry_and_only_what_is_needed(self):
        user_tokens.save(self.session, issued(), now=1000.0)
        self.assertEqual(self.session["tokens"], {"access": "a1", "refresh": "r1", "expires_at": 1300.0})

    async def test_access_token_fresh_returns_stored(self):
        user_tokens.save(self.session, issued(), now=1000.0)
        self.assertEqual(await user_tokens.access_token(self.session, self.refresh, now=1269.0), "a1")
        self.assertEqual(self.calls, [])

    async def test_access_token_refreshes_near_expiry(self):
        user_tokens.save(self.session, issued(), now=1000.0)
        self.assertEqual(await user_tokens.access_token(self.session, self.refresh, now=1271.0), "a2")
        self.assertEqual(self.calls, ["r1"])
        self.assertEqual(self.session["tokens"]["refresh"], "r2")

    async def test_refresh_failure_clears_and_raises(self):
        user_tokens.save(self.session, issued(), now=1000.0)
        with self.assertRaises(SignInRequired):
            await user_tokens.access_token(self.session, self.failing, now=2000.0)
        self.assertNotIn("tokens", self.session)

    async def test_missing_tokens_require_sign_in(self):
        with self.assertRaises(SignInRequired):
            await user_tokens.access_token(self.session, self.refresh, now=1000.0)


if __name__ == "__main__":
    unittest.main()
