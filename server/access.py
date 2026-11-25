"""Who may change things: only a browser on the station computer itself.

Everyone else, on the network or through a tunnel, can only look.
"""

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

FORWARDING_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "via",
    "tailscale-funnel-request",
    "tailscale-user-login",
    "cf-connecting-ip",
)


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def is_local(request: Request) -> bool:
    """True for a direct request from this computer.

    A tunnel also connects from this computer, so its headers and the address
    the visitor asked for must not point elsewhere.
    """
    if any(name in request.headers for name in FORWARDING_HEADERS):
        return False
    if request.client is None or not _is_loopback(request.client.host):
        return False
    try:
        host = urlsplit("//" + request.headers.get("host", "")).hostname
    except ValueError:
        return False
    return _is_loopback(host)


def local_only(request: Request) -> None:
    if not is_local(request):
        raise HTTPException(403, "only possible on the station computer")
