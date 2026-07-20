from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

from app.models import Watch


@dataclass(frozen=True)
class PlatformUrlPolicy:
    platform: str
    expected_hosts: tuple[str, ...]
    search_host: str
    search_path: str

    def validate(self, url: str) -> str:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{self.platform}: URL must use http or https")
        host = parsed.netloc.casefold().split("@")[-1].split(":")[0]
        if not any(host == expected or host.endswith(f".{expected}") for expected in self.expected_hosts):
            expected = ", ".join(self.expected_hosts)
            raise ValueError(f"{self.platform}: URL host must be one of {expected}")
        return url.strip()

    def discovery_url(self, watch: Watch) -> str:
        query = quote_plus(
            " ".join([watch.movie_name, watch.city, watch.language, watch.format]).strip()
        )
        return f"https://{self.search_host}{self.search_path}?q={query}&date={watch.show_date.isoformat()}"


SENSITIVE_QUERY_NAMES = {
    "access_token",
    "auth",
    "authorization",
    "cookie",
    "key",
    "password",
    "session",
    "sessionid",
    "signature",
    "token",
}


def sanitize_url(url: str) -> str:
    """Remove credential-like query parameters before diagnostics are persisted or shown."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in SENSITIVE_QUERY_NAMES
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


BOOKMYSHOW_URLS = PlatformUrlPolicy(
    platform="BookMyShow",
    expected_hosts=("bookmyshow.com",),
    search_host="in.bookmyshow.com",
    search_path="/explore/search",
)

PVRINOX_URLS = PlatformUrlPolicy(
    platform="PVR INOX",
    expected_hosts=("pvrcinemas.com", "pvrinox.com"),
    search_host="www.pvrcinemas.com",
    search_path="/search",
)
