"""批次放行平台的 HTTP/JSON 边界（仅依赖 Python 标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation.errors import DomainError, ValidationError

from .service import BatchReleaseService
from .storage import ReleaseDatabase


def _receipt_status(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def route(service: BatchReleaseService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到放行平台服务。"""

    headers = headers or {}
    body = dict(body or {})
    # 操作者身份只以 X-Actor-Id 头为准；请求体里即便带 actor_id 也忽略，避免重复关键字。
    body.pop("actor_id", None)
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    parts = [segment for segment in parsed.path.split("/") if segment]
    foundation = service.foundation
    try:
        if method == "GET" and not parts:
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        # ------------------------------------------------- 复用基础服务登记能力
        if method == "POST" and parts == ["organizations"]:
            return _receipt_status(foundation.register_organization(actor_id=actor_id, **body))
        if method == "POST" and parts == ["actors"]:
            return _receipt_status(foundation.register_actor(actor_id=actor_id, **body))
        if method == "POST" and parts == ["sites"]:
            return _receipt_status(foundation.register_site(actor_id=actor_id, **body))

        # ------------------------------------------------------------- 标准/批号
        if method == "POST" and parts == ["brand-standards"]:
            return _receipt_status(service.register_brand_standard(actor_id=actor_id, **body))
        if method == "POST" and parts == ["lots"]:
            return _receipt_status(service.register_lot(actor_id=actor_id, **body))
        if method == "POST" and parts == ["calibrations"]:
            return _receipt_status(service.register_calibration(actor_id=actor_id, **body))
        if method == "POST" and len(parts) == 3 and parts[0] == "calibrations" and parts[2] == "revoke":
            return _receipt_status(service.revoke_calibration(
                actor_id=actor_id, calibration_id=parts[1], **body))

        # ---------------------------------------------------------------- 批次
        if method == "GET" and parts == ["batches"]:
            return 200, {"items": service.list_batches(query.get("site_id", [None])[0])}
        if method == "POST" and parts == ["batches", "import"]:
            return _receipt_status(service.import_batch(actor_id=actor_id, **body))
        if method == "POST" and parts == ["batches", "merge"]:
            return _receipt_status(service.merge_batch(actor_id=actor_id, **body))
        if method == "POST" and parts == ["batches", "rework"]:
            return _receipt_status(service.rework_batch(actor_id=actor_id, **body))
        if method == "GET" and len(parts) == 2 and parts[0] == "batches":
            return 200, service.get_batch(parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "lineage":
            return 200, service.get_lineage(parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "explain":
            return 200, service.explain_batch(parts[1])
        if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "lab-results":
            return _receipt_status(service.record_lab_result(
                actor_id=actor_id, batch_id=parts[1], **body))
        if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "split":
            return _receipt_status(service.split_batch(
                actor_id=actor_id, batch_id=parts[1], **body))

        # ------------------------------------------------------------ 偏差/限制
        if method == "POST" and parts == ["deviations"]:
            return _receipt_status(service.open_deviation(actor_id=actor_id, **body))
        if method == "POST" and len(parts) == 3 and parts[0] == "deviations" and parts[2] == "disposition":
            return _receipt_status(service.disposition_deviation(
                actor_id=actor_id, deviation_id=parts[1], **body))
        if method == "POST" and len(parts) == 3 and parts[0] == "deviations" and parts[2] == "close":
            return _receipt_status(service.close_deviation(
                actor_id=actor_id, deviation_id=parts[1], **body))
        if method == "POST" and parts == ["restrictions"]:
            return _receipt_status(service.impose_restriction(actor_id=actor_id, **body))
        if method == "POST" and len(parts) == 3 and parts[0] == "restrictions" and parts[2] == "release":
            return _receipt_status(service.release_restriction(
                actor_id=actor_id, restriction_id=parts[1], **body))

        # ----------------------------------------------------------- 决定/复核
        if method == "POST" and parts == ["decisions"]:
            return _receipt_status(service.decide(actor_id=actor_id, **body))
        if method == "POST" and parts == ["reviews"]:
            return _receipt_status(service.open_review(actor_id=actor_id, **body))
        if method == "GET" and parts == ["reviews"]:
            return 200, {"items": service.list_reviews(query.get("status", [None])[0])}
        if method == "POST" and len(parts) == 3 and parts[0] == "reviews" and parts[2] == "complete":
            return _receipt_status(service.complete_review(
                actor_id=actor_id, task_id=parts[1], **body))
        if method == "POST" and len(parts) == 3 and parts[0] == "reviews" and parts[2] == "cancel":
            return _receipt_status(service.cancel_review(
                actor_id=actor_id, task_id=parts[1], **body))
        if method == "POST" and parts == ["shipments"]:
            return _receipt_status(service.record_shipment(actor_id=actor_id, **body))

        if method == "GET" and parts == ["audit-events"]:
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: BatchReleaseService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动跨工厂啤酒批次放行平台")
    parser.add_argument("--database", default="beer_release.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = ReleaseDatabase(args.database)
    Handler.service = BatchReleaseService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
