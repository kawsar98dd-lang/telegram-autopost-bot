"""ASGI adapter: raw ASGI scope -> neutral Request -> pipeline -> raw ASGI response."""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

from .http import Request, Response, parse_cookies
from .multipart import MultipartError, parse_multipart

log = logging.getLogger(__name__)
MAX_BODY = 32 * 1024  # ordinary forms; routes that accept an upload declare a bigger limit (Route.max_body)


class BodyTooLarge(Exception):
    pass


async def _read_body(receive, limit: int = MAX_BODY) -> bytes:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge()
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def client_ip(scope, headers: dict[str, str], trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64] or "0.0.0.0"
    client = scope.get("client")
    return client[0] if client else "0.0.0.0"


class ASGIAdapter:
    """Wraps ``pipeline`` (an object with ``async handle(request)`` and ``error(request, status)``)."""

    def __init__(self, pipeline, trust_proxy) -> None:
        self.pipeline = pipeline
        self._trust_proxy = trust_proxy  # callable -> bool (settings exist only after start-up)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request = Request(
            method=scope["method"].upper(),
            path=scope.get("path", "/") or "/",
            query=dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)),
            headers=headers,
            cookies=parse_cookies(headers.get("cookie", "")),
            client_ip=client_ip(scope, headers, self._trust_proxy()),
        )
        try:
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                limit = MAX_BODY
                limit_for = getattr(self.pipeline, "body_limit", None)
                if limit_for is not None:
                    limit = limit_for(request)
                declared = headers.get("content-length", "")
                if declared.isdigit() and int(declared) > limit:
                    raise BodyTooLarge()
                body = await _read_body(receive, limit)
                content_type = headers.get("content-type", "")
                if content_type.lower().startswith("multipart/form-data"):
                    request.form, request.files = parse_multipart(content_type, body)
                elif content_type.startswith("application/x-www-form-urlencoded"):
                    parsed: dict[str, str] = {}
                    for k, v in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True):
                        parsed.setdefault(k, v)
                    request.form = parsed
            response = await self.pipeline.handle(request)
        except BodyTooLarge:
            response = self.pipeline.error(request, 413)
        except MultipartError:
            response = self.pipeline.error(request, 400)
        except Exception:  # last resort; the pipeline normally handles its own errors
            log.exception("unhandled error in ASGI adapter")
            response = self.pipeline.error(request, 500)
        await self._send(send, request, response)

    @staticmethod
    async def _send(send, request: Request, response: Response) -> None:
        body = b"" if request.method == "HEAD" else response.body
        headers = [(b"content-type", response.content_type.encode("latin-1")),
                   (b"content-length", str(len(response.body)).encode("ascii"))]
        headers += [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in response.headers]
        await send({"type": "http.response.start", "status": response.status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
