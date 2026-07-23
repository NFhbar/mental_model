import time
import unittest

from fastapi import HTTPException
from fastapi.testclient import TestClient

import server


class ServerSecurityTest(unittest.TestCase):
    def setUp(self):
        self.password = server.APP_PASSWORD
        self.allow_local_repos = server.ALLOW_LOCAL_REPOS
        server.APP_PASSWORD = "test-password"
        server.auth_tokens.clear()
        server.auth_failures.clear()
        server.ask_requests.clear()

    def tearDown(self):
        server.APP_PASSWORD = self.password
        server.ALLOW_LOCAL_REPOS = self.allow_local_repos
        server.auth_tokens.clear()
        server.auth_failures.clear()
        server.ask_requests.clear()

    def test_expired_auth_token_is_rejected_and_removed(self):
        server.auth_tokens["expired"] = time.monotonic() - 1
        self.assertFalse(server.token_is_valid("expired"))
        self.assertNotIn("expired", server.auth_tokens)

    def test_valid_auth_token_is_accepted(self):
        server.auth_tokens["valid"] = time.monotonic() + 60
        self.assertTrue(server.token_is_valid("valid"))

    def test_rate_limiter_returns_retry_after(self):
        buckets = {}
        server.enforce_rate_limit(buckets, "client", 2, 60, "limited")
        server.enforce_rate_limit(buckets, "client", 2, 60, "limited")
        with self.assertRaises(HTTPException) as raised:
            server.enforce_rate_limit(buckets, "client", 2, 60, "limited")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertIn("Retry-After", raised.exception.headers)

    def test_authentication_failures_are_throttled(self):
        client = TestClient(server.app)
        for _ in range(server.AUTH_FAILURE_LIMIT):
            response = client.post("/api/auth", json={"password": "wrong"})
            self.assertEqual(response.status_code, 401)
        response = client.post("/api/auth", json={"password": "wrong"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_repository_endpoint_rejects_non_github_remote(self):
        server.ALLOW_LOCAL_REPOS = False
        server.auth_tokens["valid"] = time.monotonic() + 60
        client = TestClient(server.app)
        response = client.post(
            "/api/repo",
            json={"source": "http://169.254.169.254/internal"},
            headers={"X-Auth-Token": "valid"},
        )
        self.assertEqual(response.status_code, 400)

    def test_request_models_enforce_size_limits(self):
        client = TestClient(server.app)
        response = client.post("/api/auth", json={"password": "x" * 513})
        self.assertEqual(response.status_code, 422)
        response = client.post(
            "/api/ask",
            json={"session_id": "x" * 32, "question": "x" * 4001},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
