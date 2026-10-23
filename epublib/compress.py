"""Gzip middleware that only touches text-like responses.

Starlette's stock `GZipMiddleware` compresses every response over the size
threshold, which is wrong for this app: `.epub` / `.m4b` / cover downloads are
already compressed (or huge, range-requested media), and gzipping a 206 partial
response corrupts the byte range. So we reuse Starlette's responder but flag
anything that isn't JSON / HTML / CSS / JS / SVG as excluded, which makes it
pass through untouched.
"""

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_COMPRESSIBLE = ("application/json", "application/javascript", "image/svg+xml", "text/")


class _TextOnlyResponder(GZipResponder):
    async def send_with_compression(self, message: Message) -> None:
        await super().send_with_compression(message)
        if message["type"] == "http.response.start":
            ctype = Headers(raw=message["headers"]).get("content-type", "")
            if not ctype.startswith(_COMPRESSIBLE):
                self.content_type_is_excluded = True


class TextGZipMiddleware(GZipMiddleware):
    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        super().__init__(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            await _TextOnlyResponder(self.app, self.minimum_size, compresslevel=self.compresslevel)(
                scope, receive, send
            )
            return
        # No gzip accepted (or not HTTP): the stock middleware passes through.
        await super().__call__(scope, receive, send)
