"""Bound the new import request before multipart parsing can spool it to disk."""

from fastapi import HTTPException
from starlette.responses import JSONResponse


class ImportBodyLimitMiddleware:
    # 25 MiB chunks + 100 MiB PDF + bounded multipart/form-field overhead.
    max_body_bytes = 125 * 1024 * 1024 + 64 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        root = scope.get("root_path", "").rstrip("/")
        if root and path.startswith(root + "/"):
            path = path[len(root):]
        if scope.get("type") != "http" or scope.get("method") != "POST" or path != "/documents/import-chunks":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length is not None:
            try:
                size = int(length)
                if size < 0:
                    raise ValueError()
            except ValueError:
                return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
            if size > self.max_body_bytes:
                return await JSONResponse({"detail": "Import request exceeds the body limit"}, status_code=413)(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    raise HTTPException(413, "Import request exceeds the body limit")
            return message

        return await self.app(scope, bounded_receive, send)
