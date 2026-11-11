"""Owner identity and owner-scoped job access shared by the browser API and MCP service."""
import hashlib
import unittest
import uuid
from app import settings
from app.identity import owner_id
from app import jobs


class OwnerIdTests(unittest.TestCase):
    def test_owner_id_matches_previous_formula(self):
        expected = hashlib.sha256(b"https://i.example/realms/r|abc").hexdigest()
        self.assertEqual(owner_id("https://i.example/realms/r", "abc"), expected)

    def test_owner_id_differs_by_issuer(self):
        self.assertNotEqual(owner_id("https://a.example/realms/r", "abc"),
                            owner_id("https://b.example/realms/r", "abc"))


@unittest.skipUnless(settings.ENVIRONMENT == "local", "Local synthetic database tests only")
class JobAccessTests(unittest.TestCase):
    def test_read_report_unknown_job_returns_none(self):
        self.assertIsNone(jobs.read_report("synthetic-owner", uuid.uuid4()))


if __name__ == "__main__":
    unittest.main()
