"""Another preview the evil app must not reach directly (tests/e2e/preview_sandbox)."""
import http.server
import os

http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), type("H", (http.server.BaseHTTPRequestHandler,), {
    "do_GET": lambda self: (self.send_response(200), self.end_headers(), self.wfile.write(b"victim")),
    "log_message": lambda self, *a: None})).serve_forever()
