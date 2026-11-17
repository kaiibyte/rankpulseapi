
"""Cloudflare Python Workers entry point (see wrangler.jsonc)."""

import js  # pyright: ignore[reportMissingImports]
from workers import Response, WorkerEntrypoint, asgi

from app.core.config import ALTERNATIVE_ENDPOINT_URL
from app.core.fallback import (
    EDGE_BROWSER_CACHE_CONTROL,
    EDGE_CACHE_SECONDS,
    FALLBACK_STATUSES,
    backup_url,
    edge_cacheable,
    redirect_headers,
)
from app.main import app
from app.utils.client_ip import reset_edge_geo, set_edge_geo
from app.web.warm import warm

warm(app)


def _edge_geo(request) -> dict[str, str | None] | None:
    cf = getattr(request, "cf", None)
    if not cf:
        return None

    country = getattr(cf, "country", None)
    if not country:
        return None

    return {
        "city": getattr(cf, "city", None) or None,
        "state": getattr(cf, "region", None) or None,
        "country": str(country).lower(),
    }


def _is_rankpulse_request(request) -> bool:
    path = str(request.url).split("?", 1)[0]
    return path.endswith("/api/rankpulse") or "/api/rankpulse/" in path


def _rankpulse_error(request) -> Response:
    origin = request.headers.get("Origin", "")

    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
    }

    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"

    return Response(
        '{"status":"error","code":"WORKER_ERROR",'
        '"message":"The RankPulse backend encountered an internal error."}',
        status=500,
        headers=headers,
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = str(request.url)
        rankpulse_request = _is_rankpulse_request(request)

        cacheable = edge_cacheable(str(request.method), url)

        if cacheable:
            hit = await _edge_match(url)
            if hit:
                return _edge_answer(hit)

        token = set_edge_geo(_edge_geo(request))

        try:
            response = await asgi.fetch(app, request, self.env, self.ctx)

        except Exception:
            if rankpulse_request:
                return _rankpulse_error(request)

            fallback = _to_backup(request, "exception")

            if fallback is None:
                raise

            return fallback

        finally:
            reset_edge_geo(token)

        # Do not redirect RankPulse errors. Return the original response
        # so its actual error can be inspected.
        if (
            not rankpulse_request
            and response.status in FALLBACK_STATUSES
        ):
            return _to_backup(
                request,
                f"status-{response.status}",
            ) or response

        if cacheable and response.status == 200:
            _edge_store(self.ctx, url, response)

        return response


async def _edge_match(url: str):
    """Return a cached response, or None if the cache cannot be read."""
    try:
        return await js.caches.default.match(url)
    except Exception:
        return None


def _edge_answer(hit):
    """Rebuild a cached response with the correct browser cache headers."""
    try:
        answer = js.Response.new(hit.body, hit)
        answer.headers.set("Cache-Control", EDGE_BROWSER_CACHE_CONTROL)
        return answer
    except Exception:
        return hit


def _edge_store(ctx, url: str, response) -> None:
    """Store a successful cacheable response."""
    try:
        copy = js.Response.new(response.clone().body, response)
        copy.headers.set(
            "Cache-Control",
            f"public, max-age={EDGE_CACHE_SECONDS}",
        )
        copy.headers.set("X-Rone-Edge-Cache", "HIT")
        ctx.waitUntil(js.caches.default.put(url, copy))
    except Exception:
        pass


def _to_backup(request, reason: str) -> Response | None:
    location = backup_url(
        str(request.url),
        ALTERNATIVE_ENDPOINT_URL,
    )

    if location is None:
        return None

    return Response(
        "",
        status=307,
        headers=redirect_headers(location, reason),
    )
