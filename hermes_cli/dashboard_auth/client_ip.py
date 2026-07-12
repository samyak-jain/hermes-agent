"""Trusted client-IP resolution for dashboard-auth audit events."""

from __future__ import annotations

from fastapi import Request

from hermes_cli.dashboard_auth.registry import list_request_auth_providers


def client_ip(request: Request) -> str:
    """Return the audit IP without trusting unconfigured proxy headers.

    A request-auth provider may name a client-IP header that its upstream edge
    owns and sanitizes. The header is trusted only while that provider is
    registered; ordinary basic/OIDC deployments continue to prefer their
    proxy-managed X-Forwarded-For chain.
    """
    for provider in list_request_auth_providers():
        header = str(getattr(provider, "trusted_client_ip_header", "") or "")
        if header:
            value = request.headers.get(header, "")
            if value:
                return value.strip()

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
