"""Safe error responses: no tracebacks, no internals, ever."""

from __future__ import annotations

from .http import Request, Response, json_response

MESSAGES = {
    400: ("Bad request", "The request could not be processed. Please go back and try again."),
    401: ("Sign-in required", "Please sign in to continue."),
    403: ("Not allowed", "You are not allowed to do that, or your page expired. Reload the page and try again."),
    404: ("Page not found", "The page you are looking for does not exist."),
    405: ("Method not allowed", "That action is not supported here."),
    409: ("Conflict", "This action conflicts with the current state and was not performed."),
    413: ("Request too large", "The submitted data was too large."),
    429: ("Too many attempts", "Too many attempts. Please wait a while before trying again."),
    500: ("Something went wrong", "An internal error occurred. It has been logged."),
    502: ("Service problem", "An upstream service gave an invalid answer. Please try again later."),
    503: ("Service unavailable", "The service is temporarily unavailable. Please try again later."),
}


class HttpError(Exception):
    def __init__(self, status: int, retry_after: int | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(str(status))


def error_response(request: Request | None, status: int, *, reference: str = "", retry_after: int | None = None) -> Response:
    title, message = MESSAGES.get(status, MESSAGES[500])
    if request is not None and (request.wants_json or request.path.startswith("/api/")):
        resp = json_response({"error": title, "message": message, **({"reference": reference} if reference else {})}, status)
    elif request is not None and request.is_htmx:
        body = request.ctx.templates.get_template("partials/error.html").render(
            title=title, message=message, reference=reference).encode("utf-8")
        resp = Response(status, body).header("HX-Retarget", "#flash").header("HX-Reswap", "innerHTML")
    else:
        ctx = getattr(request, "ctx", None)
        from .context import make_template_env

        env = ctx.templates if ctx is not None else make_template_env()
        body = env.get_template("error.html").render(
            status=status, title=title, message=message, reference=reference, user=getattr(request, "user", None),
        ).encode("utf-8")
        resp = Response(status, body)
    if retry_after:
        resp.header("Retry-After", str(retry_after))
    return resp
