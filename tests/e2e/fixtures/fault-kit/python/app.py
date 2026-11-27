"""A tiny recipe counter: GET / answers a page, GET /api/count answers JSON."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from recipes import count_recipes

RECIPES = ["Soup", "Bread", "Salad"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/count"):
            body = json.dumps({"count": count_recipes(RECIPES)}).encode()
            kind = "application/json"
        else:
            body = f"<h1>Recipe box</h1><p>{count_recipes(RECIPES)} recipes</p>".encode()
            kind = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Recipe box on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
