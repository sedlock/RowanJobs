"""Destination policy.

Fetched pages are untrusted input. A link or a redirect in one of them must not
be able to make the collector reach into the local network, a link-local
address, or a cloud metadata service. Every URL -- initial or redirect target --
goes through :meth:`UrlPolicy.check` before a connection is opened.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")

# Well-known cloud instance metadata endpoints, blocked explicitly as well as
# by the private/link-local rules below.
METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",
    "fd00:ec2::254",
}


class DestinationError(RuntimeError):
    """A URL was refused before any connection was attempted."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


@dataclass(slots=True)
class UrlPolicy:
    """Allowlist plus address validation."""

    allowed_hosts: tuple[str, ...]
    allow_subdomains: bool = False
    resolve: bool = True

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if host in self.allowed_hosts:
            return True
        if self.allow_subdomains:
            return any(host.endswith("." + allowed) for allowed in self.allowed_hosts)
        return False

    def check(self, url: str) -> str:
        """Validate ``url`` and return it normalised, or raise DestinationError."""
        parts = urlsplit(url)
        if parts.scheme not in ALLOWED_SCHEMES:
            raise DestinationError(url, f"scheme {parts.scheme!r} not permitted")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise DestinationError(url, "no host")
        if host in METADATA_HOSTS:
            raise DestinationError(url, "metadata service address")
        if not self.host_allowed(host):
            raise DestinationError(url, "host is not in the collection allowlist")
        if parts.username or parts.password:
            raise DestinationError(url, "embedded credentials are not permitted")

        if self.resolve:
            self._check_addresses(url, host, parts.port)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))

    def _check_addresses(self, url: str, host: str, port: int | None) -> None:
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False

        if literal:
            if not _ip_is_public(ipaddress.ip_address(host)):
                raise DestinationError(url, "literal address is not a public address")
            return

        try:
            infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise DestinationError(url, f"DNS resolution failed ({exc})") from exc
        if not infos:
            raise DestinationError(url, "DNS returned no addresses")
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:  # pragma: no cover - defensive
                raise DestinationError(url, f"unparseable address {addr!r}") from None
            if not _ip_is_public(ip):
                raise DestinationError(url, f"resolves to non-public address {addr}")


def same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)
