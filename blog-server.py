#!/usr/bin/env python3
"""Local dev server for Creative DBB static site with blog save API."""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BLOG_FILE = ROOT / "blog.json"
SELECTED_WORK_FILE = ROOT / "selected-work.json"
IMAGES_DIR = ROOT / "images"
PORT = 8080
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class BlogHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if self.path == "/api/blog":
            self._save_blog(raw)
            return

        if self.path == "/api/selected-work":
            self._save_selected_work(raw)
            return

        if self.path == "/api/upload":
            self._upload_image(raw)
            return

        self.send_error(404, "Not found")

    def _save_blog(self, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if "posts" not in payload or not isinstance(payload["posts"], list):
                raise ValueError("Invalid blog data: missing posts array")
            BLOG_FILE.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _save_selected_work(self, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if "items" not in payload or not isinstance(payload["items"], list):
                raise ValueError("Invalid selected work data: missing items array")
            SELECTED_WORK_FILE.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _upload_image(self, raw: bytes) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json_error(400, "Expected multipart form data")
            return

        try:
            filename, file_bytes, mime_type = self._parse_multipart(raw, content_type)
        except ValueError as exc:
            self._json_error(400, str(exc))
            return

        if not filename or not file_bytes:
            self._json_error(400, "No image file provided")
            return

        ext = ALLOWED_IMAGE_TYPES.get(mime_type) or Path(filename).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            self._json_error(400, "Unsupported image type. Use JPG, PNG, WebP, or GIF.")
            return

        if ext == ".jpeg":
            ext = ".jpg"

        stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(filename).stem).strip("-").lower()
        if not stem:
            stem = "upload"

        IMAGES_DIR.mkdir(exist_ok=True)
        safe_name = f"{int(time.time())}-{stem}{ext}"
        target = IMAGES_DIR / safe_name
        target.write_bytes(file_bytes)

        rel_path = f"images/{safe_name}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"ok": True, "path": rel_path, "filename": safe_name}).encode()
        )

    def _json_error(self, code: int, message: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": message}).encode())

    @staticmethod
    def _parse_multipart(raw: bytes, content_type: str) -> tuple[str, bytes, str]:
        match = re.search(r"boundary=(.+)", content_type)
        if not match:
            raise ValueError("Missing multipart boundary")

        boundary = match.group(1).strip()
        if boundary.startswith('"') and boundary.endswith('"'):
            boundary = boundary[1:-1]

        delimiter = ("--" + boundary).encode()
        filename = ""
        file_bytes = b""
        mime_type = "application/octet-stream"

        for part in raw.split(delimiter):
            if b"Content-Disposition" not in part:
                continue

            header_block, _, body = part.partition(b"\r\n\r\n")
            if not body:
                continue

            body = body.rstrip(b"\r\n")
            if body.endswith(b"--"):
                body = body[:-2].rstrip(b"\r\n")

            headers = header_block.decode("utf-8", errors="ignore")
            if 'name="image"' not in headers:
                continue

            name_match = re.search(r'filename="([^"]*)"', headers)
            if name_match:
                filename = name_match.group(1)

            type_match = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.I)
            if type_match:
                mime_type = type_match.group(1).strip().lower()

            file_bytes = body
            break

        return filename, file_bytes, mime_type


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = HTTPServer(("127.0.0.1", port), BlogHandler)
    print(f"Serving {ROOT}")
    print(f"Open http://127.0.0.1:{port}/blog-admin.html")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()