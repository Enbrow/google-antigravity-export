"""Regression tests for local API discovery after Antigravity 2.28.1."""

import json
import unittest
from unittest.mock import patch

from antigravity_export.discover import _probe_port


class _Response:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, n):
        return self._body[:n]


class PortProbeTests(unittest.TestCase):
    def _check(self, payload, expected, status=200):
        with patch(
            "antigravity_export.discover.urllib.request.urlopen",
            return_value=_Response(payload, status=status),
        ):
            self.assertEqual(_probe_port(62000, "test-csrf"), expected)

    def test_empty_index_is_valid(self):
        self._check(b"{}", True)

    def test_populated_index_is_valid(self):
        self._check(json.dumps({"trajectorySummaries": {"some-id": {"summary": "Example"}}}).encode(), True)

    def test_other_json_object_is_not_valid(self):
        self._check(b'{"error":"not the conversation API"}', False)

    def test_non_json_is_not_valid(self):
        self._check(b"Not Found", False)

    def test_non_success_http_status_is_not_valid(self):
        self._check(b"{}", False, status=204)


if __name__ == "__main__":
    unittest.main()
