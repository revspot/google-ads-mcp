"""Per-request tenant credentials, carried in a ContextVar.

Upstream runs one process per user over stdio, so a single global Google Ads client is
enough for it. Served over HTTP for many revspot clients it is not: the harness calls on
behalf of a different client on every request, and the credential has to travel with the
request rather than sit in a module global.

Only the refresh token varies per tenant. The OAuth client id/secret, the developer token
and the login customer id are app-level settings shared by every client — the same values
revspot-backend holds — so they stay in the environment and only this one string moves.

A ContextVar and not a thread-local: the server is asyncio, many requests share a thread,
and anyio copies the context into the worker thread it runs sync tool functions in.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple, Optional


class Tenant(NamedTuple):
    """Who a single request is acting as."""

    # revspot client id. Doubles as the cache key for that tenant's GoogleAdsClient.
    client_key: str
    refresh_token: str
    # Overrides the app-level MCC for tenants that sit under a different manager.
    login_customer_id: Optional[str] = None


_current: ContextVar[Optional[Tenant]] = ContextVar("current_tenant", default=None)


def get_tenant() -> Optional[Tenant]:
    """The tenant this request acts as, or None outside a tenant-scoped request."""
    return _current.get()


@contextmanager
def tenant_scope(tenant: Tenant):
    """Bind `tenant` for the duration of one request, then restore what was there.

    Reset rather than set-to-None on the way out: a ContextVar left set would otherwise
    leak one tenant's credentials into whatever ran next on the same context.
    """
    token = _current.set(tenant)
    try:
        yield tenant
    finally:
        _current.reset(token)
