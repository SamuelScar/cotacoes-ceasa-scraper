from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import RemoteDisconnected
from http.cookiejar import CookieJar
from random import uniform
from time import monotonic, sleep

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class HttpSourceBlockedError(RuntimeError):
    """Indica que a fonte recusou ou limitou novas requisicoes."""


class HttpRequestError(RuntimeError):
    """Indica falha HTTP persistente que nao representa ausencia de cotacao."""


class HttpResourceNotFoundError(ValueError):
    """Indica que a fonte nao possui o recurso historico solicitado."""


@dataclass
class HttpClient:
    """Cliente HTTP conservador com cache, intervalo minimo e backoff."""

    timeout_seconds: int = 30
    request_delay_seconds: float = 2.0
    request_jitter_seconds: float = 0.5
    max_attempts: int = 4
    backoff_base_seconds: float = 2.0
    response_cache_size: int = 4096
    response_cache_max_bytes: int = 256 * 1024 * 1024
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "User-Agent": (
                "cotacoes-ceasa-scraper/0.1 "
                "(coleta academica de dados publicos)"
            )
        }
    )
    _last_request_at: float = field(default=0.0, init=False, repr=False)
    _cookie_jar: CookieJar = field(default_factory=CookieJar, init=False, repr=False)
    _opener: object = field(init=False, repr=False)
    _response_cache: OrderedDict[tuple[str, bytes | None], bytes] = field(
        default_factory=OrderedDict,
        init=False,
        repr=False,
    )
    _response_cache_bytes: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts deve ser maior que zero.")

        if self.request_delay_seconds < 0 or self.request_jitter_seconds < 0:
            raise ValueError("Intervalos entre requisicoes nao podem ser negativos.")

        if self.backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds nao pode ser negativo.")

        self._opener = build_opener(HTTPCookieProcessor(self._cookie_jar))

    def get_text(
        self,
        url: str,
        encoding: str = "utf-8",
        *,
        force_refresh: bool = False,
    ) -> str:
        """Baixa uma URL e retorna o corpo da resposta como texto."""
        body = self.get_bytes(url, force_refresh=force_refresh)

        return body.decode(encoding, errors="replace")

    def get_bytes(self, url: str, *, force_refresh: bool = False) -> bytes:
        """Baixa uma URL e retorna o corpo da resposta como bytes."""
        return self._request(url, force_refresh=force_refresh)

    def post_form(
        self,
        url: str,
        data: dict[str, str | list[str]],
        *,
        force_refresh: bool = False,
    ) -> str:
        """Envia um formulario e retorna o corpo da resposta como texto."""
        body = urlencode(data, doseq=True).encode("utf-8")

        return self._request(
            url,
            body,
            force_refresh=force_refresh,
        ).decode("utf-8", errors="replace")

    def _request(
        self,
        url: str,
        data: bytes | None = None,
        *,
        force_refresh: bool = False,
    ) -> bytes:
        cache_key = (url, data)
        cached_response = (
            None if force_refresh else self._get_cached_response(cache_key)
        )

        if cached_response is not None:
            return cached_response

        for attempt in range(self.max_attempts):
            self._wait_before_request()
            request = Request(url, data=data, headers=self.headers)

            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    self._cache_response(cache_key, body)

                    return body
            except HTTPError as error:
                if error.code == 403:
                    raise HttpSourceBlockedError(
                        f"Fonte recusou a requisicao ao baixar {url}: HTTP 403."
                    ) from error

                if error.code in {404, 410}:
                    raise HttpResourceNotFoundError(
                        f"Recurso historico nao encontrado ao baixar {url}: "
                        f"HTTP {error.code}."
                    ) from error

                if error.code in RETRYABLE_HTTP_STATUS_CODES:
                    if attempt == self.max_attempts - 1:
                        error_type = (
                            HttpSourceBlockedError
                            if error.code == 429
                            else HttpRequestError
                        )
                        raise error_type(
                            f"Erro HTTP persistente ao baixar {url}: {error.code}"
                        ) from error

                    retry_after = (
                        error.headers.get("Retry-After") if error.headers else None
                    )
                    self._wait_before_retry(attempt, retry_after)
                    continue

                raise HttpRequestError(
                    f"Erro HTTP ao baixar {url}: {error.code}"
                ) from error
            except RemoteDisconnected as error:
                if attempt == self.max_attempts - 1:
                    raise HttpRequestError(
                        f"Conexao encerrada pela fonte ao baixar {url}."
                    ) from error

                self._wait_before_retry(attempt)
            except URLError as error:
                if attempt == self.max_attempts - 1:
                    raise HttpRequestError(
                        f"Erro de conexao ao baixar {url}: {error.reason}"
                    ) from error

                self._wait_before_retry(attempt)
            except (ConnectionError, TimeoutError) as error:
                if attempt == self.max_attempts - 1:
                    raise HttpRequestError(
                        f"Conexao interrompida ao baixar {url}: {error}"
                    ) from error

                self._wait_before_retry(attempt)

        raise HttpRequestError(f"Nao foi possivel baixar {url}.")

    def _wait_before_request(self) -> None:
        if self._last_request_at == 0:
            self._last_request_at = monotonic()
            return

        elapsed_seconds = monotonic() - self._last_request_at
        delay_seconds = self.request_delay_seconds + uniform(
            0,
            self.request_jitter_seconds,
        )
        remaining_seconds = delay_seconds - elapsed_seconds

        if remaining_seconds > 0:
            sleep(remaining_seconds)

        self._last_request_at = monotonic()

    def _wait_before_retry(
        self,
        attempt: int,
        retry_after: str | None = None,
    ) -> None:
        retry_after_seconds = self._parse_retry_after(retry_after)
        backoff_seconds = self.backoff_base_seconds * (2**attempt)
        wait_seconds = max(retry_after_seconds, backoff_seconds) + uniform(0, 1)
        sleep(wait_seconds)

    def _parse_retry_after(self, value: str | None) -> float:
        if value is None:
            return 0

        try:
            return max(0, float(value))
        except ValueError:
            pass

        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 0

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)

        return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    def _get_cached_response(
        self,
        cache_key: tuple[str, bytes | None],
    ) -> bytes | None:
        if cache_key not in self._response_cache:
            return None

        response = self._response_cache.pop(cache_key)
        self._response_cache[cache_key] = response

        return response

    def _cache_response(
        self,
        cache_key: tuple[str, bytes | None],
        response: bytes,
    ) -> None:
        if (
            self.response_cache_size <= 0
            or self.response_cache_max_bytes <= 0
            or len(response) > self.response_cache_max_bytes
        ):
            return

        previous_response = self._response_cache.pop(cache_key, None)

        if previous_response is not None:
            self._response_cache_bytes -= len(previous_response)

        while self._response_cache and (
            len(self._response_cache) >= self.response_cache_size
            or self._response_cache_bytes + len(response)
            > self.response_cache_max_bytes
        ):
            _, removed_response = self._response_cache.popitem(last=False)
            self._response_cache_bytes -= len(removed_response)

        self._response_cache[cache_key] = response
        self._response_cache_bytes += len(response)
