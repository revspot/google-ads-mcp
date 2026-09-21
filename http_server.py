"""HTTP entrypoint for the Google Ads MCP server.

Run: uvicorn http_server:app --host 0.0.0.0 --port 5008

Upstream ships a stdio server: one process per user, one global Google Ads client, and
credentials read from the environment at import. Served over HTTP for many revspot
clients none of that holds, so this module adds the three things that do:

  * a tenant per request, taken from headers (several accepted spellings each, see
    HEADERS_CLIENT_KEY) and bound to a ContextVar, so the 139 tools
    can keep calling get_client() with no argument and still get the right account
    (see utils/auth_manager.py and utils/request_context.py);
  * the /google-ads prefix the public vhost serves this on, stripped here rather than in
    nginx, so the public URL is spelled out in the repo and not in one box's config —
    the same shape revspot-mcp's agent/server.py uses;
  * a /health route that says which build and MCC this container is, so an instance can
    always be asked what it is.

Auth is NOT done here. nginx gates /google-ads/ with auth_request against
mcp-auth-service, so reaching this app at all means the caller held the shared harness
key. The headers below carry *which tenant* to act as, not whether the caller may.

Env: GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET (required — app-level OAuth client,
the same one revspot-backend uses, which is what carries the Cloud project's API access
level), GOOGLE_ADS_LOGIN_CUSTOMER_ID (optional MCC), GOOGLE_ADS_DEVELOPER_TOKEN
(optional, ignored by the API since 2026-09-09). Refresh tokens never live here.
"""

import json
import logging
import os

from mcp.server.transport_security import TransportSecuritySettings

# Importing this registers all 139 tools on the FastMCP object and is not cheap.
from google_ads_mcp import mcp
from utils.request_context import Tenant, tenant_scope

logger = logging.getLogger("google_ads_mcp.http")

# The public prefix on mcp.revspot.ai. nginx passes the path through unchanged.
PATH_PREFIX = "/google-ads"

# Which revspot client this request acts as. The harness reads both from the DB it
# shares with revspot-backend and sends them on every request.
#
# Several spellings per field, tried in order, first one present wins. Not indecision:
# custom headers have to be approved before a client may send them, so the names that
# survive that process are not always the names this server would have chosen, and a
# rename is not a thing either side can do unilaterally. Accepting the alternates costs
# a dict lookup and saves a deployment from turning on a header nobody can change.
HEADERS_CLIENT_KEY = (
    "x-revspot-client-id",
    "x-workspace-id",
    # Approved with the transposition; kept until we know which spelling is real.
    "x-worskpace-id",
)

# Every one of these must carry a REFRESH token, whatever the name says.
# initialize_oauth builds Credentials(token=None, refresh_token=...) and mints its own
# access token, so a real OAuth access token put here fails to refresh — and would be
# an hour from expiry anyway, while the client built from it is cached for the life of
# the process. x-access-token is an alias for the field, not a change of what goes in it.
HEADERS_REFRESH_TOKEN = (
    "x-google-ads-refresh-token",
    "x-access-token",
)

# Optional per-tenant override for clients that sit under a different manager account.
HEADERS_LOGIN_CUSTOMER_ID = (
    "x-google-ads-login-customer-id",
    "x-login-customer-id",
)


def _first_header(headers: dict, names) -> str:
    """The value of the first of `names` present and non-empty, else ""."""
    for name in names:
        value = headers.get(name, "").strip()
        if value:
            return value
    return ""

# Stateless: every POST stands alone, with no session carried between requests. That is
# the honest model here — the credential arrives per request, so a session that outlived
# one request would be a session whose tenant is a guess.
mcp.settings.stateless_http = True
mcp.settings.streamable_http_path = "/mcp"

# FastMCP turns on DNS-rebinding protection by itself when the bind host looks local, and
# allows only localhost (server.py:191). Behind nginx the Host header is the public name,
# so without this every proxied request is answered "Invalid Host header" with a 421.
# Kept on rather than switched off: it costs one setting, and it is the check that stops
# a browser on someone's machine from driving this server through a hostile page.
_allowed_hosts = [
    h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "mcp.revspot.ai").split(",")
    if h.strip()
]
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    # The loopback entries keep the container reachable on its own port for debugging,
    # which is how /mcp is exercised without going through nginx.
    allowed_hosts=_allowed_hosts + ["127.0.0.1:*", "localhost:*", "127.0.0.1", "localhost"],
    # A harness sends no Origin at all, and an absent Origin passes. These are here for
    # the browser case only.
    allowed_origins=[f"https://{h}" for h in _allowed_hosts],
)


def _json_response(status: int, body: dict):
    """A tiny ASGI responder, so this module needs nothing from Starlette's internals."""
    payload = json.dumps(body).encode()

    async def respond(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": payload})

    return respond


class StripPrefix:
    """Serve /google-ads/... as if it had arrived without the prefix.

    An alias, not a mount: the app sees the rewritten path with no root_path, so
    /google-ads/mcp is indistinguishable from /mcp. Keeps the server reachable directly
    on the container port for debugging, and keeps nginx free of rewriting.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(PATH_PREFIX + "/"):
            trimmed = scope["path"][len(PATH_PREFIX):]
            scope = dict(scope, path=trimmed, raw_path=trimmed.encode())
        await self.app(scope, receive, send)


class Health:
    """Answer /health without involving the MCP machinery."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/health":
            login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or None
            await _json_response(200, {
                "status": "ok",
                "server": "google-ads-mcp",
                # Says what this container is configured as, without leaking a secret:
                # whether it has an OAuth client at all, and which MCC it logs in under.
                "oauth_client_configured": bool(os.getenv("GOOGLE_ADS_CLIENT_ID")),
                "login_customer_id": login_customer_id,
                "tools": len(await mcp.list_tools()),
            })(scope, receive, send)
            return
        await self.app(scope, receive, send)


class TenantFromHeaders:
    """Bind the request's tenant for the duration of the request.

    Refuses a tool call that arrives without a tenant rather than falling back to any
    client that happens to be initialised: a silent fallback is how one revspot client
    ends up reading — or mutating — another's campaigns.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        client_key = _first_header(headers, HEADERS_CLIENT_KEY)
        refresh_token = _first_header(headers, HEADERS_REFRESH_TOKEN)

        if not (client_key and refresh_token):
            await _json_response(400, {
                "error": "missing tenant headers",
                # Names every accepted spelling: a caller that guessed the wrong one
                # should be able to see the right one in the rejection.
                "detail": (
                    "one of " + " / ".join(HEADERS_CLIENT_KEY) + " and one of "
                    + " / ".join(HEADERS_REFRESH_TOKEN) + " are required on every request"
                ),
            })(scope, receive, send)
            return

        tenant = Tenant(
            client_key=client_key,
            refresh_token=refresh_token,
            login_customer_id=(
                _first_header(headers, HEADERS_LOGIN_CUSTOMER_ID) or None
            ),
        )
        # The token itself is never logged — only which client it belongs to.
        logger.debug("request for tenant %s", client_key)
        with tenant_scope(tenant):
            await self.app(scope, receive, send)


# google_ads_initialize belongs to the stdio server, where the model asks its user for
# credentials once and the process serves only them. Here credentials arrive per request
# from the harness, so the tool has nothing to do — and what it does do is write a client
# into the auth manager's shared cache under the key "default", which is state every
# request can see. Removed rather than left as a trap: a model that finds it will ask a
# user for a developer token this deployment does not want anyone to type.
_REMOVED_TOOLS = ["google_ads_initialize"]
for _name in _REMOVED_TOOLS:
    try:
        mcp._tool_manager.remove_tool(_name)
        logger.info("removed stdio-only tool: %s", _name)
    except Exception as exc:  # pragma: no cover - upstream may rename it
        logger.warning("could not remove tool %s: %s", _name, exc)


# Outermost first: strip the public prefix, answer /health before anything needs a
# tenant, then bind the tenant for the MCP app underneath.
app = StripPrefix(Health(TenantFromHeaders(mcp.streamable_http_app())))
