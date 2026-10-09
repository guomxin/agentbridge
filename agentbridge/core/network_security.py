from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


INSECURE_PRIVATE_HTTP_WARNING = (
    "WARNING: insecure private HTTP mode is enabled. OA credentials, trusted "
    "form values, and MCP bearer tokens can cross the network without TLS. "
    "Use only on a controlled private test network and never expose these "
    "ports publicly."
)

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def validate_insecure_private_http_endpoint(
    *,
    host: str,
    port: int,
    public_base_url: str,
    service_name: str,
) -> None:
    try:
        bind_address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            f"{service_name} insecure HTTP host must be a literal private IP address"
        ) from exc
    if not any(bind_address in network for network in _PRIVATE_NETWORKS):
        raise ValueError(
            f"{service_name} insecure HTTP host must be an RFC 1918 or IPv6 ULA address"
        )

    parsed = urlparse(public_base_url)
    if parsed.scheme.lower() != "http" or not parsed.netloc:
        raise ValueError(f"{service_name} insecure private endpoint must use HTTP")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{service_name} insecure private public URL is invalid")
    try:
        url_host = parsed.hostname
        url_port = parsed.port or 80
        url_address = ipaddress.ip_address(url_host or "")
    except ValueError as exc:
        raise ValueError(
            f"{service_name} insecure private public URL must use a literal IP address"
        ) from exc
    if url_address != bind_address:
        raise ValueError(
            f"{service_name} insecure private public URL must match the bind IP"
        )
    if url_port != port:
        raise ValueError(
            f"{service_name} insecure private public URL must match the bind port"
        )
