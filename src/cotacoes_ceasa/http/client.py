from dataclasses import dataclass, field
from http.client import RemoteDisconnected
from time import monotonic, sleep

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class HttpClient:
    """Cliente HTTP simples com timeout e intervalo minimo entre requisicoes."""

    timeout_seconds: int = 30
    request_delay_seconds: float = 2.0
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "User-Agent": (
                "cotacoes-ceasa-scraper/0.1 "
                "(coleta academica de dados publicos)"
            )
        }
    )
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def get_text(self, url: str, encoding: str = "utf-8") -> str:
        """Baixa uma URL e retorna o corpo da resposta como texto."""
        body = self.get_bytes(url)

        return body.decode(encoding, errors="replace")

    def get_bytes(self, url: str) -> bytes:
        """Baixa uma URL e retorna o corpo da resposta como bytes."""
        for attempt in range(2):
            self._wait_before_request()
            request = Request(url, headers=self.headers)

            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return response.read()
            except HTTPError as error:
                raise RuntimeError(f"Erro HTTP ao baixar {url}: {error.code}") from error
            except RemoteDisconnected as error:
                if attempt == 1:
                    raise RuntimeError(
                        f"Conexao encerrada pela fonte ao baixar {url}."
                    ) from error
            except URLError as error:
                raise RuntimeError(
                    f"Erro de conexao ao baixar {url}: {error.reason}"
                ) from error

        raise RuntimeError(f"Nao foi possivel baixar {url}.")

    def _wait_before_request(self) -> None:
        if self._last_request_at == 0:
            self._last_request_at = monotonic()
            return

        elapsed_seconds = monotonic() - self._last_request_at
        remaining_seconds = self.request_delay_seconds - elapsed_seconds

        if remaining_seconds > 0:
            sleep(remaining_seconds)

        self._last_request_at = monotonic()
