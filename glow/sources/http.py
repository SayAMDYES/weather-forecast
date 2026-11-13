import logging
import time

import requests

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = "glow-forecast/0.1 (personal non-commercial afterglow forecast)"


def get(url, retries=3, timeout=60, headers=None, params=None):
    """GET with exponential backoff; 404 is returned immediately since it means 'not published yet'."""
    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            resp = _session.get(url, timeout=timeout, headers=headers, params=params)
            if resp.status_code == 404 or resp.ok:
                return resp
            log.warning("HTTP %s from %s (attempt %d/%d)", resp.status_code, url, attempt, retries)
            if resp.status_code == 429 and attempt < retries:
                # Rate limited: honour Retry-After, otherwise wait out most of a minute window
                retry_after = resp.headers.get("Retry-After", "")
                time.sleep(float(retry_after) if retry_after.isdigit() else 30.0 * attempt)
                continue
        except requests.RequestException as e:
            log.warning("%s for %s (attempt %d/%d)", e, url, attempt, retries)
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"failed after {retries} attempts: {url}")


def exists(url, timeout=30):
    try:
        return _session.head(url, timeout=timeout).status_code == 200
    except requests.RequestException:
        return False


def status(url, timeout=30, retries=3):
    """HTTP status of a HEAD request, retried on network errors; None if it never got an answer."""
    for attempt in range(retries):
        try:
            return _session.head(url, timeout=timeout).status_code
        except requests.RequestException as e:
            log.warning("%s for HEAD %s (attempt %d/%d)", e, url, attempt + 1, retries)
            time.sleep(2 * (attempt + 1))
    return None
