import asyncio
import unittest
from urllib.parse import parse_qs

import httpx

from entra_agent_auth import (
    AgentCredentials,
    AgentIdentityTokenProvider,
    TokenAcquisitionError,
)


TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
AGENT = "33333333-3333-3333-3333-333333333333"
API = "44444444-4444-4444-4444-444444444444"


class TokenProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_cache_and_renewal(self):
        requests = []
        now = [100.0]

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={
                "access_token": f"token-{len(requests)}",
                "expires_in": 3600,
                "token_type": "Bearer",
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = AgentIdentityTokenProvider(
                AgentCredentials(TENANT, BLUEPRINT, AGENT, "private-secret"),
                f"api://{API}/.default", client, clock=lambda: now[0],
            )
            self.assertEqual(await provider.get_token(), "token-2")
            self.assertEqual(await provider.get_token(), "token-2")
            self.assertEqual(len(requests), 2)
            self.assertEqual(str(requests[0].url),
                             f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token")
            self.assertEqual(parse_qs(requests[0].content.decode()), {
                "client_id": [BLUEPRINT], "client_secret": ["private-secret"],
                "fmi_path": [AGENT], "scope": ["api://AzureADTokenExchange/.default"],
                "grant_type": ["client_credentials"],
            })
            self.assertEqual(parse_qs(requests[1].content.decode()), {
                "client_id": [AGENT], "client_assertion": ["token-1"],
                "client_assertion_type": ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"],
                "scope": [f"api://{API}/.default"], "grant_type": ["client_credentials"],
            })
            now[0] += 3540
            self.assertEqual(await provider.get_token(), "token-4")

    async def test_errors_do_not_disclose_response_secrets(self):
        def respond(request):
            return httpx.Response(400, json={
                "error": "invalid_client", "error_description": "private-secret token-value",
                "correlation_id": TENANT,
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            credentials = AgentCredentials(TENANT, BLUEPRINT, AGENT, "private-secret")
            provider = AgentIdentityTokenProvider(credentials, f"api://{API}/.default", client)
            with self.assertRaises(TokenAcquisitionError) as caught:
                await provider.get_token()
            self.assertIn("invalid_client", str(caught.exception))
            self.assertIn(TENANT, str(caught.exception))
            self.assertNotIn("private-secret", str(caught.exception))
            self.assertNotIn("token-value", str(caught.exception))
            self.assertNotIn("private-secret", repr(credentials))

    async def test_malformed_responses_fail_closed(self):
        payloads = [[], {}, {"access_token": "token", "expires_in": "NaN"},
                    {"access_token": "token", "expires_in": 0, "token_type": "Bearer"},
                    {"access_token": "token", "expires_in": 10, "token_type": "Basic"}]
        for payload in payloads:
            with self.subTest(payload=payload):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=payload)
                )) as client:
                    provider = AgentIdentityTokenProvider(
                        AgentCredentials(TENANT, BLUEPRINT, AGENT, "secret"),
                        f"api://{API}/.default", client,
                    )
                    with self.assertRaises(TokenAcquisitionError):
                        await provider.get_token()

    async def test_concurrent_calls_share_exchange(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={
                "access_token": "token", "expires_in": 3600, "token_type": "Bearer",
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = AgentIdentityTokenProvider(
                AgentCredentials(TENANT, BLUEPRINT, AGENT, "secret"),
                f"api://{API}/.default", client,
            )
            self.assertEqual(await asyncio.gather(
                provider.get_token(), provider.get_token(), provider.get_token()
            ), ["token"] * 3)
            self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main()