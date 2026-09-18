import unittest
from unittest.mock import patch

from config import AgentSettings, ServerSettings


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "ENTRA_TENANT_ID": "11111111-1111-1111-1111-111111111111",
            "ENTRA_AGENT_ID": "33333333-3333-3333-3333-333333333333",
            "MCP_API_CLIENT_ID": "44444444-4444-4444-4444-444444444444",
        }
        self.enterContext(patch("config.load_dotenv"))

    def test_server_needs_no_secret_or_model_key(self):
        with patch.dict("os.environ", self.environment, clear=True):
            settings = ServerSettings.from_env()
            self.assertEqual(settings.url, "http://127.0.0.1:8000/mcp")
            with self.assertRaises(ValueError):
                AgentSettings.from_env()

    def test_reject_nonloopback_invalid_port_and_invalid_ids(self):
        for overrides in [{"MCP_HOST": "0.0.0.0"}, {"MCP_PORT": "0"},
                          {"MCP_PORT": "abc"}, {"ENTRA_TENANT_ID": "common"},
                          {"ENTRA_AGENT_ID": ""}]:
            with self.subTest(overrides=overrides), patch.dict(
                "os.environ", self.environment | overrides, clear=True
            ):
                with self.assertRaises(ValueError):
                    ServerSettings.from_env()