"""Base HTTP client with retry logic and rate limiting."""

import time
from typing import Any

import httpx


class RateLimitError(Exception):
    """Raised when API rate limit is hit."""
    pass


class APIError(Exception):
    """Raised when API returns an error."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BaseAPIClient:
    """Base class for API clients with retry and rate limiting support."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        """Lazy initialization of HTTP client."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=self._get_default_headers(),
            )
        return self._client

    def _get_default_headers(self) -> dict[str, str]:
        """Override in subclass to add default headers."""
        return {
            "Accept": "application/json",
            "User-Agent": "SolanaWalletTracker/0.1.0",
        }

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        """Process response and handle errors."""
        if response.status_code == 429:
            raise RateLimitError("Rate limit exceeded")

        if response.status_code >= 400:
            try:
                error_data = response.json()
                message = error_data.get("message", error_data.get("error", str(error_data)))
            except Exception:
                message = response.text or f"HTTP {response.status_code}"
            raise APIError(message, response.status_code)

        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request with retry logic."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        last_exception: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                response = self.client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    headers=headers,
                )
                return self._handle_response(response)

            except RateLimitError:
                # Exponential backoff for rate limits
                wait_time = self.retry_delay * (2 ** attempt)
                time.sleep(wait_time)
                last_exception = RateLimitError("Rate limit exceeded after retries")

            except httpx.TimeoutException:
                last_exception = APIError("Request timed out", None)
                time.sleep(self.retry_delay)

            except httpx.RequestError as e:
                last_exception = APIError(f"Request failed: {e}", None)
                time.sleep(self.retry_delay)

        if last_exception:
            raise last_exception
        raise APIError("Request failed after retries")

    def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make GET request."""
        return self._request("GET", endpoint, params=params, headers=headers)

    def post(
        self,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make POST request."""
        return self._request("POST", endpoint, params=params, json_data=json_data, headers=headers)

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
