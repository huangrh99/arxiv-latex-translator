"""HTTP server and browser entry point."""

from __future__ import annotations

import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .pipeline import JobManager


STATIC_DIR = Path(__file__).parent / "static"


class TranslatorHandler(BaseHTTPRequestHandler):
    manager: JobManager

    def _json(self, value: object, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, path: Path, content_type: str | None = None, allowed_root: Path | None = None) -> None:
        try:
            resolved = path.resolve()
            base = (allowed_root or self.manager.base_dir).resolve()
            if base not in resolved.parents:
                self._json({"error": "文件路径无效"}, HTTPStatus.NOT_FOUND)
                return
        except OSError:
            self._json({"error": "文件路径无效"}, HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self._json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8", STATIC_DIR)
            return
        if path == "/healthz":
            self._json({"status": "ok"})
            return
        if path == "/api/status":
            self._json({"codex_cli": self.manager.codex_status()})
            return
        if path == "/compare.html":
            self._send_file(STATIC_DIR / "compare.html", "text/html; charset=utf-8", STATIC_DIR)
            return
        if path == "/api/jobs":
            query = parse_qs(parsed.query).get("query", [""])[0]
            jobs = self.manager.list(query)
            self._json({"jobs": [job.to_dict() for job in jobs], "count": len(jobs)})
            return
        parts = path.split("/")
        if len(parts) == 4 and parts[1:3] == ["api", "jobs"]:
            job = self.manager.get(parts[3])
            if job is None:
                self._json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(job.to_dict())
            return
        if len(parts) == 5 and parts[1:3] == ["api", "jobs"]:
            job = self.manager.get(parts[3])
            if job is None:
                self._json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            files = {"pdf": (job.pdf_path, "application/pdf"), "source": (job.source_path, "application/zip"), "log": (job.log_path, "text/plain; charset=utf-8")}
            item = files.get(parts[4])
            if item is None or item[0] is None:
                self._json({"error": "文件尚未生成"}, HTTPStatus.NOT_FOUND)
                return
            self._send_file(Path(item[0]), item[1])
            return
        self._json({"error": "路径不存在"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json({"error": "Content-Length 无效"}, HTTPStatus.BAD_REQUEST)
            return
        if length > 64 * 1024:
            self._json({"error": "请求过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"error": "请求必须是 JSON"}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/jobs":
            if not isinstance(payload, dict):
                self._json({"error": "请求体必须是 JSON 对象"}, HTTPStatus.BAD_REQUEST)
                return
            source = payload.get("source")
            if not isinstance(source, str) or not source.strip():
                self._json({"error": "缺少 source"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                job = self.manager.create(source, force=bool(payload.get("force", False)))
            except (ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(job.to_dict(), HTTPStatus.ACCEPTED)
            return

        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "retry":
            try:
                job = self.manager.retry(parts[3])
            except KeyError:
                self._json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            except RuntimeError as exc:
                self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            self._json(job.to_dict(), HTTPStatus.ACCEPTED)
            return
        self._json({"error": "路径不存在"}, HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str | None = None, port: int | None = None, manager: JobManager | None = None) -> ThreadingHTTPServer:
    selected_manager = manager or JobManager()
    selected_host = host or os.environ.get("ARXIV_HOST", "127.0.0.1")
    selected_port = int(os.environ.get("ARXIV_PORT", "8000")) if port is None else port
    handler = type("BoundTranslatorHandler", (TranslatorHandler,), {"manager": selected_manager})
    return ThreadingHTTPServer((selected_host, selected_port), handler)


def main() -> None:
    server = create_server()
    print(f"ArXiv LaTeX Translator listening on http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
