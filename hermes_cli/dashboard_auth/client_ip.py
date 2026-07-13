"""Trusted client-IP resolution for dashboard-auth audit events."""

from __future__ import annotations

from fastapi import Request

from hermes_cli.dashboard_auth.registry import get_provider


def client_ip(request: Request) -> str:
    """Return the audit IP without trusting unconfigured proxy headers.

    A request-auth provider may name a client-IP header that its upstream edge
    owns and sanitizes. The header is trusted only after that provider has
    verified this specific request and attached its session; directly-reachable
    origins therefore cannot spoof failed-auth audit events with an edge-only
    header. Ordinary basic/OIDC deployments continue to prefer their
    proxy-managed X-Forwarded-For chain.
    """
    session = getattr(getattr(request, "state", None), "session", None)
    provider = get_provider(str(getattr(session, "provider", "") or ""))
    if provider is not None and getattr(provider, "supports_request_auth", False):
        header = str(getattr(provider, "trusted_client_ip_header", "") or "")
        if header:
            value = request.headers.get(header, "")
            if value:
                return value.strip()

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
