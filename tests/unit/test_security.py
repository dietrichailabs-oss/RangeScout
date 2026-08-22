import unittest

from app.security.secrets import redact_secrets


class TestSecurity(unittest.TestCase):
    def test_redact_secrets_hides_potential_tokens(self) -> None:
        payload = "apiKey='abcd1234567890ABCDEFGH'"
        self.assertIn("[REDACTED]", redact_secrets(payload))

    def test_redact_secrets_covers_provider_headers_and_query_tokens(self) -> None:
        samples = (
            "X-Finnhub-Token: finnhub_test_value_123456",
            "APCA-API-KEY-ID: alpaca_key_value_123456",
            "APCA-API-SECRET-KEY=alpaca_secret_value_123456",
            "https://example.invalid/?token=finnhub-query-value-123456",
        )
        for sample in samples:
            redacted = redact_secrets(sample)
            self.assertIn("[REDACTED]", redacted)
            self.assertNotIn("123456", redacted)


if __name__ == "__main__":
    unittest.main()
