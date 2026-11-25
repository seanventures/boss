"""Serve one generated HTML artifact without exposing its repository."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
from urllib.parse import unquote, urlsplit


def report_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Report path must be relative and stay inside the app directory")
    target = (root / candidate).resolve()
    if not target.is_relative_to(root.resolve()) or target.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Report preview requires an HTML file inside the app directory")
    return target


def serve(value: str, prepare: str = "") -> None:
    root = Path.cwd()
    report_path(root, value)
    if prepare:
        try:
            subprocess.run(prepare, shell=True, check=True, timeout=120)
        except subprocess.CalledProcessError as error:
            raise SystemExit(f"boss.preview.report: prepare failed (exit {error.returncode}); "
                             "fix its command so it writes the report in a fresh clone") from None
        except subprocess.TimeoutExpired:
            raise SystemExit("boss.preview.report: prepare ran past 120 s; make it generate "
                             "the report from small checked-in demo inputs") from None
    target = report_path(root, value)
    if not target.is_file():
        raise ValueError("Report artifact is missing; configure boss.preview.report prepare to generate it")
    content = target.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = unquote(urlsplit(self.path).path)
            if route not in {"/", "/index.html", "/" + target.name}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

    ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--prepare", default="")
    arguments = parser.parse_args()
    serve(arguments.path, arguments.prepare)
