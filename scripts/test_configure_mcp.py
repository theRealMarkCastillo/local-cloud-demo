"""Resolve PUBLIC_URL the way Compose does: environment first, then .env, quotes stripped."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import configure_mcp


class PublicUrlTests(unittest.TestCase):
    def env_file(self, text):
        folder = Path(tempfile.mkdtemp())
        (folder / ".env").write_text(text)
        return patch.object(configure_mcp, "ROOT", folder)

    def test_environment_overrides_env_file(self):
        with self.env_file("PUBLIC_URL=http://localhost:8080\n"), patch.dict(os.environ, {"PUBLIC_URL": "https://demo.example"}):
            self.assertEqual(configure_mcp.public_url(), "https://demo.example")

    def test_quotes_in_env_file_are_stripped(self):
        with self.env_file('PUBLIC_URL="https://demo.example/"\n'), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configure_mcp.public_url(), "https://demo.example")

    def test_default_without_configuration(self):
        with self.env_file(""), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configure_mcp.public_url(), "http://localhost:8080")


if __name__ == "__main__":
    unittest.main()
