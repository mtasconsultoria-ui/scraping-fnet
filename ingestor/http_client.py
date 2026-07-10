from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "mtas-scraping-fnet/0.1 (rotina de ingestao de dados publicos CVM)"
TIMEOUT = 180.0
RETRIES = 4


def fetch_bytes(url: str) -> bytes:
    """GET com retry exponencial (2s, 4s, 8s, 16s) para instabilidade dos portais."""
    last_error: Exception | None = None
    for attempt in range(RETRIES + 1):
        if attempt:
            delay = 2**attempt
            log.warning("retry %s/%s em %ss para %s", attempt, RETRIES, delay, url)
            time.sleep(delay)
        try:
            response = httpx.get(
                url,
                timeout=TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:  # inclui timeouts e 5xx
            last_error = exc
    raise RuntimeError(f"download falhou após {RETRIES} tentativas: {url}") from last_error
