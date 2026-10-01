"""跨工厂批次放行平台的 HTTP/JSON 路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import DomainError, ValidationError
from .service import ReleaseService


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "y")


def route_release(service: ReleaseService, method: str, path: str,
                  body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
    """返回响应；若路径不属于放行域则返回 None。"""

    actor_id = headers.get("X-Actor-Id", "") or str(body.pop("actor_id", "") or "")
    body.pop("actor_id", None)
    parsed = urlparse(path)
    segments = [s for s in parsed.path.split("/") if s]
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    try:
        if method == "POST":
            if parsed.path == "/brand-standards":
                receipt = service.register_brand_standard(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/lines":
                receipt = service.register_line(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/material-lots":
                receipt = service.register_material_lot(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/material-lots/quarantine":
                receipt = service.set_material_quarantine(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/calibrations":
                receipt = service.register_calibration(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/batches":
                receipt = service.register_batch(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/lab-results":
                receipt = service.record_lab_result(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/reviews/complete":
                receipt = service.complete_review(actor_id=actor_id, **body)
                return 200 if receipt.get("replayed") else 201, receipt
            if parsed.path == "/restrictions":
                receipt = service.raise_restriction(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/restrictions/lift":
                receipt = service.lift_restriction(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/decisions":
                receipt = service.create_decision(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt
            if parsed.path == "/shipments":
                receipt = service.register_shipment(actor_id=actor_id, **body)
                return 200 if receipt["replayed"] else 201, receipt

        if method == "GET":
            if parsed.path == "/brand-standards":
                if "standard_id" not in query:
                    raise ValidationError("standard_id 不能为空")
                version = int(query["version"]) if query.get("version") else None
                return 200, service.get_brand_standard(query["standard_id"], version)
            if parsed.path == "/material-lots":
                if "material_lot_id" not in query:
                    raise ValidationError("material_lot_id 不能为空")
                return 200, service.get_material_lot(query["material_lot_id"])
            if len(segments) == 2 and segments[0] == "batches":
                return 200, service.get_batch(segments[1])
            if len(segments) == 3 and segments[0] == "batches":
                batch_id = segments[1]
                sub = segments[2]
                if sub == "lineage":
                    return 200, service.lineage(batch_id)
                if sub == "status":
                    return 200, service.effective_status(batch_id)
                if sub == "explain":
                    return 200, service.explain(batch_id)
            if parsed.path == "/lab-results":
                if "batch_id" not in query:
                    raise ValidationError("batch_id 不能为空")
                return 200, {"items": service.list_lab_results(query["batch_id"])}
            if parsed.path == "/reviews":
                if "batch_id" in query:
                    return 200, {"items": service.list_reviews(query["batch_id"])}
                return 200, {"items": service.list_open_reviews(query.get("site_id"))}
            if parsed.path == "/restrictions":
                if "batch_id" not in query:
                    raise ValidationError("batch_id 不能为空")
                return 200, {"items": service.list_restrictions(
                    query["batch_id"], active_only=_truthy(query.get("active_only")))}
            if parsed.path == "/decisions":
                if "batch_id" not in query:
                    raise ValidationError("batch_id 不能为空")
                return 200, {"items": service.list_decisions(query["batch_id"])}
            if parsed.path == "/shipments":
                if "batch_id" not in query:
                    raise ValidationError("batch_id 不能为空")
                return 200, {"items": service.list_shipments(query["batch_id"])}
            if parsed.path == "/pending-batches":
                return 200, {"items": service.pending_batches(query.get("site_id"))}
        return None
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
