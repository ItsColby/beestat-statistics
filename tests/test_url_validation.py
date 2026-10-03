"""Tests for the Beestat credential-transport URL boundary."""

from __future__ import annotations

import unittest

from custom_components.beestat_statistics import url_validation


class UrlValidationTest(unittest.TestCase):
    """Require a bounded HTTPS API origin before credentials can be used."""

    def test_normalizes_valid_https_url(self) -> None:
        self.assertEqual(
            url_validation.normalize_api_base(" https://api.example.test/v1/ "),
            "https://api.example.test/v1/",
        )

    def test_rejects_insecure_or_ambiguous_urls(self) -> None:
        for value in (
            "http://api.example.test/",
            "https://user@example.test/",
            "https://api.example.test/#fragment",
            "https://api.example.test/#",
            "https://api.example.test/?mode=test",
            "https://api.example.test/?",
            "https:///missing-host",
            "https://api.example.test\\ambiguous",
            "https://api%2f.example.test/",
            "https://api.example.test:0/",
            "https://api.example.test/has space",
            "https://api.example.test/line\nbreak",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                url_validation.normalize_api_base(value)

    def test_rejects_control_characters_before_url_parser_normalization(self) -> None:
        for character in ("\x00", "\x01", "\x1b", "\x7f"):
            for value in (
                f"{character}https://api.example.test/",
                f"https://api.example.test/{character}",
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    url_validation.normalize_api_base(value)


if __name__ == "__main__":
    unittest.main()
