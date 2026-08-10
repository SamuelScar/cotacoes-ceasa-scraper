import unittest

from cotacoes_ceasa.http.client import HttpClient


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class _SequencedOpener:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.open_count = 0

    def open(self, request: object, timeout: int) -> _FakeResponse:
        del request, timeout
        self.open_count += 1

        if not self.responses:
            raise AssertionError("Resposta HTTP nao configurada para o teste.")

        return _FakeResponse(self.responses.pop(0))


class HttpClientCacheTest(unittest.TestCase):
    def test_force_refresh_replaces_cached_response(self) -> None:
        opener = _SequencedOpener([b"indisponivel", b"disponivel"])
        client = HttpClient(
            request_delay_seconds=0,
            request_jitter_seconds=0,
        )
        client._opener = opener

        self.assertEqual("indisponivel", client.get_text("https://example.test"))
        self.assertEqual("indisponivel", client.get_text("https://example.test"))
        self.assertEqual(
            "disponivel",
            client.get_text("https://example.test", force_refresh=True),
        )
        self.assertEqual("disponivel", client.get_text("https://example.test"))
        self.assertEqual(2, opener.open_count)

    def test_force_refresh_replaces_cached_post_response(self) -> None:
        opener = _SequencedOpener([b"indisponivel", b"disponivel"])
        client = HttpClient(
            request_delay_seconds=0,
            request_jitter_seconds=0,
        )
        client._opener = opener
        payload = {"categoria": "FRUTAS"}

        self.assertEqual(
            "indisponivel",
            client.post_form("https://example.test", payload),
        )
        self.assertEqual(
            "indisponivel",
            client.post_form("https://example.test", payload),
        )
        self.assertEqual(
            "disponivel",
            client.post_form(
                "https://example.test",
                payload,
                force_refresh=True,
            ),
        )
        self.assertEqual(
            "disponivel",
            client.post_form("https://example.test", payload),
        )
        self.assertEqual(2, opener.open_count)


if __name__ == "__main__":
    unittest.main()
