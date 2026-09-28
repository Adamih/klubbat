"""HTTP fetching with timeouts, bounded retries, and modest concurrency.

Database writes never happen in this module; callers persist results afterwards.
"""

import logging
import time
from dataclasses import dataclass

import httpx
from django.conf import settings

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when a URL could not be fetched after bounded retries."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content: bytes


class Fetcher:
    """One HTTP client per ingestion run. Thread-safe for concurrent downloads."""

    def __init__(self):
        connect_timeout, read_timeout = settings.DOWNLOAD_TIMEOUT
        timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout)
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": settings.SOURCE_USER_AGENT,
                "Accept-Language": "sv",
            },
            follow_redirects=True,
        )

    def get(self, url: str) -> FetchResult:
        delay = settings.RETRY_BACKOFF_SECONDS
        last_error = None
        for attempt in range(1, settings.DOWNLOAD_RETRIES + 1):
            try:
                response = self._client.get(url)
                response.raise_for_status()
                return FetchResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content=response.content,
                )
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                status = getattr(exc, "response", None)
                status_code = status.status_code if status is not None else None
                # Client errors other than rate-limiting are not worth retrying.
                if status_code is not None and status_code not in (429, 500, 502, 503, 504):
                    break
                if attempt < settings.DOWNLOAD_RETRIES:
                    time.sleep(delay)
                    delay *= 2
        raise FetchError(f"GET {url} failed after retries: {last_error!r}")

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def looks_like_pdf(content: bytes) -> bool:
    """Validate that bytes are usable as a PDF (header may sit after a small preamble)."""
    if not content:
        return False
    return b"%PDF-" in content[:1024]
