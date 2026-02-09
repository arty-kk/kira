import sys
import unittest

from starlette.requests import Request

for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        sys.modules.pop(name, None)

from app.api import conversation
from app.config import settings


def _make_request(client_host, headers=None) -> Request:
    if headers is None:
        headers = {}
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 1234) if client_host is not None else None,
    }
    return Request(scope)


class RateLimitIpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._trusted_backup = getattr(settings, "TRUSTED_PROXY_IPS", [])

    def tearDown(self) -> None:
        settings.TRUSTED_PROXY_IPS = self._trusted_backup

    def test_ignores_forwarded_for_when_proxy_not_trusted(self) -> None:
        settings.TRUSTED_PROXY_IPS = []
        request = _make_request(
            "10.0.0.1",
            headers={"X-Forwarded-For": "203.0.113.10", "X-Real-IP": "203.0.113.11"},
        )
        ip = conversation._resolve_rate_limit_ip(request)
        self.assertEqual(ip, "10.0.0.1")

    def test_uses_forwarded_for_when_proxy_trusted(self) -> None:
        settings.TRUSTED_PROXY_IPS = ["10.0.0.0/8"]
        request = _make_request(
            "10.0.0.9",
            headers={"X-Forwarded-For": "203.0.113.10", "X-Real-IP": "203.0.113.11"},
        )
        ip = conversation._resolve_rate_limit_ip(request)
        self.assertEqual(ip, "203.0.113.10")

    def test_uses_real_ip_when_forwarded_for_missing(self) -> None:
        settings.TRUSTED_PROXY_IPS = ["127.0.0.1"]
        request = _make_request("127.0.0.1", headers={"X-Real-IP": "203.0.113.12"})
        ip = conversation._resolve_rate_limit_ip(request)
        self.assertEqual(ip, "203.0.113.12")


if __name__ == "__main__":
    unittest.main()
