#!/usr/bin/env python3
"""
Serve the test index locally with CORS enabled.

This allows testing the IndexViewer without uploading to the test bucket.

Usage:
    python scripts/serve_test_index.py
    
Then update IndexViewer.test.tsx to use: http://localhost:8001/index-v2.json
"""

import http.server
import socketserver
import os

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
    # Change to output directory
    os.chdir("output")

    with socketserver.TCPServer(("", PORT), CORSRequestHandler) as httpd:
        print(f"🌐 Serving test index at http://localhost:{PORT}")
        print(f"📍 Root index: http://localhost:{PORT}/index-v2.json")
        print("")
        print("Press Ctrl+C to stop")
        httpd.serve_forever()
