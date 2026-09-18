import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from starlette.testclient import TestClient

from config import ServerSettings
from entra_token_verifier import EntraTokenVerifier
from mcp_server import create_server


SETTINGS = ServerSettings(
    "11111111-1111-1111-1111-111111111111",
    "33333333-3333-3333-3333-333333333333",
    "44444444-4444-4444-4444-444444444444",
)


class ProtectedServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(
            cls.private_key.public_key()
        ))

    def setUp(self):
        self.verifier = EntraTokenVerifier(SETTINGS)
        self.verifier._keys = Mock(spec=jwt.PyJWKClient)
        self.verifier._keys.get_signing_key_from_jwt.return_value = self.public_key
        server = create_server(SETTINGS, self.verifier)
        self.client = self.enterContext(TestClient(
            server.streamable_http_app(stateless_http=True, json_response=True),
            base_url="http://127.0.0.1:8000",
        ))

    def token(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": SETTINGS.issuer, "aud": SETTINGS.api_id, "tid": SETTINGS.tenant_id,
            "oid": SETTINGS.agent_id, "azp": SETTINGS.agent_id, "idtyp": "app", "ver": "2.0",
            "iat": now, "nbf": now - 1, "exp": now + 300, "roles": [SETTINGS.role],
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "test"})

    def request(self, token=None, method="tools/list", params=None):
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-11-25"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
        })

    def test_missing_token_challenge_and_metadata(self):
        response = self.request()
        self.assertEqual(response.status_code, 401)
        challenge = response.headers["WWW-Authenticate"]
        self.assertIn("resource_metadata=", challenge)
        metadata_url = challenge.split('resource_metadata="')[1].split('"')[0]
        metadata = self.client.get(metadata_url)
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json()["resource"], SETTINGS.url)
        self.assertEqual(metadata.json()["authorization_servers"], [SETTINGS.issuer])

    def test_authorized_list_and_call(self):
        response = self.request(self.token())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["tools"][0]["name"], "get_portfolio_summary")
        response = self.request(self.token(), "tools/call", {
            "name": "get_portfolio_summary", "arguments": {"account_id": "DEMO-001"},
        })
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError", False))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["total_value_usd"], 20000)

    def test_missing_role_is_forbidden(self):
        self.assertEqual(self.request(self.token(roles=[])).status_code, 403)

    def test_invalid_claims_are_unauthorized(self):
        for overrides in [
            {"aud": "https://graph.microsoft.com"}, {"iss": "https://example.org"},
            {"tid": "another-tenant"}, {"oid": "another-agent"}, {"azp": "blueprint"},
            {"idtyp": "user"}, {"ver": "1.0"}, {"scp": "Portfolio.Read"},
            {"exp": int(time.time()) - 60}, {"nbf": int(time.time()) + 300},
            {"roles": "Portfolio.Read"},
        ]:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.request(self.token(**overrides)).status_code, 401)

    def test_bad_signature_and_unknown_key(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode({"aud": SETTINGS.api_id}, other_key, algorithm="RS256")
        self.assertEqual(self.request(forged).status_code, 401)
        self.verifier._keys.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError("unknown kid")
        self.assertEqual(self.request(self.token()).status_code, 401)

    def test_no_tool_execution_on_denied_request(self):
        calls = []
        server = create_server(SETTINGS, self.verifier)

        @server.tool()
        def probe() -> str:
            calls.append("called")
            return "ok"

        with TestClient(server.streamable_http_app(stateless_http=True, json_response=True),
                        base_url="http://127.0.0.1:8000") as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "probe", "arguments": {}}}
            headers = {"Accept": "application/json, text/event-stream",
                       "MCP-Protocol-Version": "2025-11-25"}
            self.assertEqual(client.post("/mcp", json=body, headers=headers).status_code, 401)
            self.assertEqual(calls, [])
            headers["Authorization"] = f"Bearer {self.token()}"
            self.assertEqual(client.post("/mcp", json=body, headers=headers).status_code, 200)
            self.assertEqual(calls, ["called"])

    def test_jwks_rotation_refreshes_unknown_key(self):
        first_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        old_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(first_key.public_key()))
        old_jwk["kid"] = "old"
        new_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        new_jwk["kid"] = "test"
        keys = jwt.PyJWKClient("https://login.microsoftonline.com/test/keys")
        with patch.object(keys, "fetch_data", side_effect=[
            {"keys": [old_jwk]}, {"keys": [new_jwk]},
        ]) as fetch:
            self.verifier._keys = keys
            self.assertEqual(self.request(self.token()).status_code, 200)
            self.assertEqual(fetch.call_count, 2)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_is_tenant_pinned_and_cached(self):
        verifier = EntraTokenVerifier(SETTINGS)
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json={
            "issuer": SETTINGS.issuer,
            "jwks_uri": f"https://login.microsoftonline.com/{SETTINGS.tenant_id}/discovery/v2.0/keys",
        }, request=httpx.Request("GET", "https://login.microsoftonline.com"))
        with patch("entra_token_verifier.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            keys = await verifier._discover()
            self.assertIs(await verifier._discover(), keys)
        client.get.assert_awaited_once_with(f"{SETTINGS.issuer}/.well-known/openid-configuration")

    async def test_untrusted_discovery_fails_closed(self):
        for metadata in [
            {"issuer": "https://example.org", "jwks_uri": "https://example.org/keys"},
            {"issuer": SETTINGS.issuer, "jwks_uri": "http://login.microsoftonline.com/keys"},
            {"issuer": SETTINGS.issuer, "jwks_uri": "https://example.org/keys"},
            {"issuer": SETTINGS.issuer, "jwks_uri": 42},
        ]:
            client = AsyncMock()
            client.get.return_value = httpx.Response(200, json=metadata,
                                                    request=httpx.Request("GET", SETTINGS.issuer))
            with self.subTest(metadata=metadata), patch("entra_token_verifier.httpx.AsyncClient") as factory:
                factory.return_value.__aenter__.return_value = client
                self.assertIsNone(await EntraTokenVerifier(SETTINGS).verify_token("invalid"))


if __name__ == "__main__":
    unittest.main()