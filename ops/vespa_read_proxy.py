"""Loopback-published, read-only bridge to the existing Marqo Vespa network."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if parsed.path != "/search/":
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        yql = params.get("yql", [""])[0]
        schemas = os.environ["SOURCE_SCHEMA"].split(",")
        if not yql.startswith("select ") or not any(
            f" from {schema} where true order by marqo__id asc" in yql for schema in schemas
        ):
            self.send_error(403)
            return
        try:
            with urllib.request.urlopen(os.environ["VESPA_URL"] + self.path, timeout=90) as response:
                data = response.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        except (OSError, urllib.error.HTTPError):
            self.send_error(502)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
