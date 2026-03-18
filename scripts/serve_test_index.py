#!/usr/bin/env python3
"""
Serve the test index locally with CORS enabled.

This allows testing the IndexViewer without uploading to the test bucket.

Usage:
    python scripts/serve_test_index.py
    python scripts/serve_test_index.py --root /custom/path
    INDEX_OUTPUT_ROOT=/custom/path python scripts/serve_test_index.py
    
Then update IndexViewer.test.tsx to use: http://localhost:8001/index-v2.json
"""

import argparse
import http.server
import socketserver
import os
from pathlib import Path

PORT = 8001


class CORSRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Serve NGM index files locally with CORS"
    )
    parser.add_argument(
        "--root",
        default=os.getenv("INDEX_OUTPUT_ROOT"),
        help="Root directory to serve (default: INDEX_OUTPUT_ROOT, then local FILES_STORE, then 'output')",
    )
    args = parser.parse_args()

    root_arg = args.root or os.getenv("FILES_STORE") or "output"
    # FILES_STORE can be remote (e.g., s3://...), which is not serveable by this local HTTP server.
    if "://" in root_arg and not root_arg.startswith("file://"):
        root_arg = "output"
    if root_arg.startswith("file://"):
        root_arg = root_arg[len("file://") :]
    root = Path(root_arg).expanduser()

    # If root is relative, resolve it from current working directory
    if not root.is_absolute():
        root = Path.cwd() / root
    if not root.is_dir():
        raise SystemExit(f" Index root directory not found: {root}")

    os.chdir(root)

    with socketserver.TCPServer(("", PORT), CORSRequestHandler) as httpd:
        print(f"Serving test index at http://localhost:{PORT}")
        print(f"Root directory: {root.absolute()}")
        print(f"Root index: http://localhost:{PORT}/index-v2.json")
        print("")
        print("Press Ctrl+C to stop")
        httpd.serve_forever()
