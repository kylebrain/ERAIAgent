"""
Serve web/ on http://localhost:8000 for local development.

Python's stdlib http.server reads MIME types from the OS registry on Windows,
where .js is often mapped to text/plain. Browsers reject ES module scripts
with the wrong MIME type, so we force the JS/CSS types here.

Usage:
    python scripts/serve_web.py [--port 8000]
"""
import argparse
import functools
import http.server
import mimetypes
import socketserver
from pathlib import Path

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent / "web"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))

    with socketserver.ThreadingTCPServer(("", args.port), handler) as httpd:
        httpd.allow_reuse_address = True
        print(f"Serving {root} at http://localhost:{args.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down")


if __name__ == "__main__":
    main()
