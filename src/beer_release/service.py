"""跨工厂批次放行平台的领域服务。

设计要点：

* 所有写入沿用基础库的请求幂等回执与同一条哈希审计链；
* 品牌标准按 ``(standard_id, version)`` 不可变登记，批次在导入时钉住版本，
  后续新版本不会倒改既有批次的评价与决定；
* 拆分、合并、返工通过谱系边表达，放行复核会递归继承祖先批次上的限制与偏差；
* 偏差与限制携带包装/销售区域范围，只影响重叠部分；
* 解除限制必须由另一名授权者引用处置证据（四眼原则）；
* 放行/冻结/召回/处置决定只追加、不更新；出库后历史决定保持锁定；
* 未结复核任务持久化，服务重启后可继续办理。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Callable

from beverage_ops_foundation.audit import append_event, canonical_json, digest
from beverage_ops_foundation.clock import Clock, SystemClock
from beverage_ops_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from beverage_ops_foundation.models import Actor
from beverage_ops_foundation.service import DomainService

from .models import OperationReceipt

IDENTIFIER = re.compile(r"^\w[\w.:-]{1,63}$", re.UNICODE)
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
LOT_KINDS = frozenset({"material", "packaging"})
DECISIONS = frozenset({"release", "freeze", "recall", "dispose"})
DECISION_STATUS = {"release": "released", "freeze": "frozen", "recall": "recalled", "dispose": "disposed"}


def normalize_scope(value: Any) -> dict[str, list[str]]:
    """把范围归一成排序后的包装/区域列表；缺省表示全覆盖。"""

    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValidationError("scope 必须是对象")
    packaging = value.get("packaging", [])
    regions = value.get("regions", [])
    if packaging is None:
        packaging = []
    if regions is None:
        regions = []
    if not isinstance(packaging, list) or not isinstance(regions, list):
        raise ValidationError("scope.packaging 与 scope.regions 必须是列表")

    def clean(items: list[Any], field: str) -> list[str]:
        cleaned: set[str] = set()
        for item in items:
            text = str(item).strip()
            if not text or len(text) > 80:
                raise ValidationError(f"scope.{field} 条目不能为空且不能超过 80 个字符")
            cleaned.add(text)
        return sorted(cleaned)

    return {"packaging": clean(packaging, "packaging"), "regions": clean(regions, "regions")}


def scope_overlaps(left: dict[str, list[str]], right: dict[str, list[str]]) -> bool:
    """判断两个范围是否在包装与销售区域两个维度上同时重叠。"""

    def dimension_overlaps(a: list[str], b: list[str]) -> bool:
        return not a or not b or bool(set(a) & set(b))

    return (dimension_overlaps(left["packaging"], right["packaging"])
            and dimension_overlaps(left["regions"], right["regions"]))


def _dimension_covers(values: list[str], cell_value: str | None) -> bool:
    """范围某一维度是否覆盖某个投影单元。

    范围列表为空表示覆盖全部（含通配单元）；非空时只覆盖列表中的具体单元，
    不覆盖代表"其余全部"的 ``None`` 通配单元。
    """

    if not values:
        return True
    return cell_value is not None and cell_value in values


def scope_covers_cell(scope: dict[str, list[str]], packaging: str | None,
                      region: str | None) -> bool:
    """判断决定/限制范围是否覆盖某个包装×区域单元。"""

    return (_dimension_covers(scope["packaging"], packaging)
            and _dimension_covers(scope["regions"], region))


class BatchReleaseService:
    """协调放行平台的权限、幂等、事务、谱系与审计规则。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        # 复用基础服务完成组织/人员/场所登记与审计查询（同一连接、同一审计链）。
        self.foundation = DomainService(database, self.clock)

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 500) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    @staticmethod
    def _to_utc_z(parsed: datetime) -> str:
        from datetime import timezone
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _ts(self, value: Any, field: str) -> str:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是带时区的 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return self._to_utc_z(parsed)

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]],
                    related_ids: list[str] | None = None) -> OperationReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return OperationReceipt(request_id, row["resource_type"], row["resource_id"], True,
                                    sorted({rid for rid in (related_ids or []) if rid}))
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return OperationReceipt(request_id, resource_type, resource_id, False,
                                sorted({rid for rid in (related_ids or []) if rid}))

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _batch_row(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def _standard(self, connection, standard_id: str, version: str):
        row = connection.execute(
            "SELECT * FROM brand_standards WHERE standard_id=? AND version=?", (standard_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("品牌标准版本不存在，批次必须钉住已登记版本")
        return row

    # ------------------------------------------------------------- 品牌标准版本

    def register_brand_standard(self, *, request_id: str, actor_id: str, standard_id: str,
                                version: str, brand: str, spec: dict[str, Any]) -> OperationReceipt:
        if not isinstance(spec, dict) or not spec:
            raise ValidationError("spec 必须是非空对象")
        limits = spec.get("limits", {})
        if limits is None:
            limits = {}
        if not isinstance(limits, dict):
            raise ValidationError("spec.limits 必须是对象")
        for name, rule in limits.items():
            if not isinstance(rule, dict):
                raise ValidationError(f"spec.limits.{name} 必须是对象")
            lower = rule.get("min")
            upper = rule.get("max")
            for bound, value in (("min", lower), ("max", upper)):
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValidationError(f"spec.limits.{name}.{bound} 必须是数值")
            if lower is not None and upper is not None and lower > upper:
                raise ValidationError(f"spec.limits.{name} 的 min 不能大于 max")
        payload = {"actor_id": actor_id, "standard_id": standard_id, "version": version,
                   "brand": brand, "spec": spec}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            standard_id = self._id(standard_id, "standard_id")
            version = self._id(version, "version")
            brand = self._text(brand, "brand")
            spec_json = canonical_json(spec)
            spec_hash = digest(spec)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT spec_hash FROM brand_standards WHERE standard_id=? AND version=?",
                    (standard_id, version),
                ).fetchone()
                if existing:
                    if existing["spec_hash"] != spec_hash:
                        raise ConflictError("品牌标准版本不可变：同编号版本已登记不同内容")
                    return "brand_standard", f"{standard_id}:{version}", {"replayed": True}
                connection.execute(
                    "INSERT INTO brand_standards(standard_id,version,brand,spec_json,spec_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (standard_id, version, brand, spec_json, spec_hash, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="brand_standard.registered",
                            resource_type="brand_standard", resource_id=f"{standard_id}:{version}",
                            detail={"standard_id": standard_id, "version": version, "brand": brand,
                                    "spec_hash": spec_hash})
                return "brand_standard", f"{standard_id}:{version}", {"spec_hash": spec_hash}

            return self._idempotent(connection, request_id=request_id, action="register_brand_standard",
                                    payload=payload, create=create)

    # ------------------------------------------------------------- 批号与校准

    def register_lot(self, *, request_id: str, actor_id: str, lot_id: str, site_id: str,
                     lot_kind: str, name: str, supplier: str | None = None,
                     payload: dict[str, Any] | None = None) -> OperationReceipt:
        if lot_kind not in LOT_KINDS:
            raise ValidationError("lot_kind 必须是 material 或 packaging")
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        body = {"actor_id": actor_id, "lot_id": lot_id, "site_id": site_id, "lot_kind": lot_kind,
                "name": name, "supplier": supplier, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所登记批号")
            lot_id = self._id(lot_id, "lot_id")
            name = self._text(name, "name")
            supplier = self._text(supplier, "supplier", 200) if supplier else None

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO lots(lot_id,site_id,lot_kind,name,supplier,payload_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (lot_id, site_id, lot_kind, name, supplier, canonical_json(payload), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批号已存在或场所无效") from exc
                self._audit(connection, actor_id=actor_id, action="lot.registered",
                            resource_type="lot", resource_id=lot_id,
                            detail={"site_id": site_id, "lot_kind": lot_kind, "name": name})
                return "lot", lot_id, {"lot_id": lot_id}

            return self._idempotent(connection, request_id=request_id, action="register_lot",
                                    payload=body, create=create)

    def register_calibration(self, *, request_id: str, actor_id: str, calibration_id: str,
                             site_id: str, equipment_id: str, calibrated_at: str,
                             valid_from: str, valid_until: str) -> OperationReceipt:
        calibrated_at = self._ts(calibrated_at, "calibrated_at")
        valid_from = self._ts(valid_from, "valid_from")
        valid_until = self._ts(valid_until, "valid_until")
        if not valid_from <= valid_until:
            raise ValidationError("校准有效期起点不能晚于终点")
        body = {"actor_id": actor_id, "calibration_id": calibration_id, "site_id": site_id,
                "equipment_id": equipment_id, "calibrated_at": calibrated_at,
                "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所登记校准")
            calibration_id = self._id(calibration_id, "calibration_id")
            equipment_id = self._id(equipment_id, "equipment_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO calibrations(calibration_id,site_id,equipment_id,calibrated_at,"
                        "valid_from,valid_until,status,created_by,created_at) VALUES(?,?,?,?,?,?,'valid',?,?)",
                        (calibration_id, site_id, equipment_id, calibrated_at, valid_from, valid_until,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("校准记录已存在或场所无效") from exc
                self._audit(connection, actor_id=actor_id, action="calibration.registered",
                            resource_type="calibration", resource_id=calibration_id,
                            detail={"site_id": site_id, "equipment_id": equipment_id,
                                    "valid_from": valid_from, "valid_until": valid_until})
                return "calibration", calibration_id, {"calibration_id": calibration_id}

            return self._idempotent(connection, request_id=request_id, action="register_calibration",
                                    payload=body, create=create)

    def revoke_calibration(self, *, request_id: str, actor_id: str,
                           calibration_id: str, reason: str) -> OperationReceipt:
        reason = self._text(reason, "reason")
        body = {"actor_id": actor_id, "calibration_id": calibration_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            row = connection.execute("SELECT * FROM calibrations WHERE calibration_id=?",
                                     (calibration_id,)).fetchone()
            if row is None:
                raise NotFoundError("校准记录不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 仅对今后的判定失效；已记录的放行决定保存了当时快照，不会被倒改。
                connection.execute("UPDATE calibrations SET status='revoked' WHERE calibration_id=? AND status='valid'",
                                   (calibration_id,))
                self._audit(connection, actor_id=actor_id, action="calibration.revoked",
                            resource_type="calibration", resource_id=calibration_id,
                            detail={"reason": reason})
                return "calibration", calibration_id, {"calibration_id": calibration_id, "status": "revoked"}

            return self._idempotent(connection, request_id=request_id, action="revoke_calibration",
                                    payload=body, create=create)

    # ----------------------------------------------------------------- 批次导入

    def import_batch(self, *, request_id: str, actor_id: str, site_id: str, import_key: str,
                     brand: str, standard_id: str, standard_version: str,
                     production_start: str, production_end: str, quantity: float,
                     unit: str, material_lot_ids: list[str] | None = None,
                     packaging_lot_ids: list[str] | None = None,
                     calibration_ids: list[str] | None = None, batch_id: str | None = None,
                     scope: dict[str, Any] | None = None) -> OperationReceipt:
        production_start = self._ts(production_start, "production_start")
        production_end = self._ts(production_end, "production_end")
        if not production_start < production_end:
            raise ValidationError("生产时段起点必须早于终点")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
            raise ValidationError("quantity 必须是正数")
        unit = self._text(unit, "unit", 20)
        material_lot_ids = list(material_lot_ids or [])
        packaging_lot_ids = list(packaging_lot_ids or [])
        calibration_ids = list(calibration_ids or [])
        scope_json = canonical_json(normalize_scope(scope))
        body = {"actor_id": actor_id, "site_id": site_id, "import_key": import_key, "brand": brand,
                "standard_id": standard_id, "standard_version": standard_version,
                "production_start": production_start, "production_end": production_end,
                "quantity": quantity, "unit": unit, "material_lot_ids": sorted(material_lot_ids),
                "packaging_lot_ids": sorted(packaging_lot_ids),
                "calibration_ids": sorted(calibration_ids), "scope": scope_json}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所导入批次")
            import_key = self._id(import_key, "import_key")
            brand = self._text(brand, "brand")
            standard_id = self._id(standard_id, "standard_id")
            standard_version = self._id(standard_version, "standard_version")
            self._standard(connection, standard_id, standard_version)

            def lots_of(ids: list[str], expected_kind: str) -> list[str]:
                resolved: list[str] = []
                for lot_id in {self._id(value, "lot_id") for value in ids}:
                    lot = connection.execute("SELECT * FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
                    if lot is None:
                        raise NotFoundError(f"批号 {lot_id} 不存在")
                    if lot["site_id"] != site_id:
                        raise ValidationError(f"批号 {lot_id} 不属于当前场所")
                    if lot["lot_kind"] != expected_kind:
                        raise ValidationError(f"批号 {lot_id} 不是{('原料' if expected_kind == 'material' else '包装')}批号")
                    resolved.append(lot_id)
                return sorted(resolved)

            materials = lots_of(material_lot_ids, "material")
            packaging = lots_of(packaging_lot_ids, "packaging")
            calibrations: list[str] = []
            for calibration_id in {self._id(value, "calibration_id") for value in calibration_ids}:
                cal = connection.execute("SELECT * FROM calibrations WHERE calibration_id=?",
                                         (calibration_id,)).fetchone()
                if cal is None:
                    raise NotFoundError(f"校准记录 {calibration_id} 不存在")
                if cal["site_id"] != site_id:
                    raise ValidationError(f"校准记录 {calibration_id} 不属于当前场所")
                calibrations.append(calibration_id)
            calibrations.sort()
            resolved_batch_id = self._id(batch_id, "batch_id") if batch_id else uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                # 重复导入：内容一致则原样返回，绝不触碰隔离/偏差/决定状态。
                existing = connection.execute(
                    "SELECT * FROM batches WHERE site_id=? AND import_key=?", (site_id, import_key)
                ).fetchone()
                if existing:
                    existing_inputs = [r["lot_id"] for r in connection.execute(
                        "SELECT lot_id FROM batch_inputs WHERE batch_id=? ORDER BY lot_id",
                        (existing["batch_id"],)).fetchall()]
                    existing_materials = [lid for lid in existing_inputs
                                          if self._lot_kind(connection, lid) == "material"]
                    existing_packaging = [lid for lid in existing_inputs
                                          if self._lot_kind(connection, lid) == "packaging"]
                    existing_calibrations = [r["calibration_id"] for r in connection.execute(
                        "SELECT calibration_id FROM batch_calibrations WHERE batch_id=? ORDER BY calibration_id",
                        (existing["batch_id"],)).fetchall()]
                    fingerprint = digest({
                        "brand": brand, "standard_id": standard_id, "standard_version": standard_version,
                        "production_start": production_start, "production_end": production_end,
                        "quantity": quantity, "unit": unit,
                        "materials": materials, "packaging": packaging, "calibrations": calibrations,
                    })
                    stored = digest({
                        "brand": existing["brand"], "standard_id": existing["standard_id"],
                        "standard_version": existing["standard_version"],
                        "production_start": existing["production_start"],
                        "production_end": existing["production_end"],
                        "quantity": existing["quantity"], "unit": existing["unit"],
                        "materials": existing_materials, "packaging": existing_packaging,
                        "calibrations": existing_calibrations,
                    })
                    if fingerprint != stored:
                        raise ConflictError("同一导入键已登记不同内容；重复导入不得改变既有批次")
                    return "batch", existing["batch_id"], {"batch_id": existing["batch_id"], "replayed": True}
                try:
                    connection.execute(
                        "INSERT INTO batches(batch_id,site_id,brand,standard_id,standard_version,"
                        "production_start,production_end,quantity,unit,status,import_key,scope_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'registered', ?,?,?,?)",
                        (resolved_batch_id, site_id, brand, standard_id, standard_version,
                         production_start, production_end, float(quantity), unit, import_key, scope_json,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号冲突") from exc
                for lot_id in materials + packaging:
                    connection.execute("INSERT INTO batch_inputs(batch_id,lot_id) VALUES(?,?)",
                                       (resolved_batch_id, lot_id))
                for calibration_id in calibrations:
                    connection.execute("INSERT INTO batch_calibrations(batch_id,calibration_id) VALUES(?,?)",
                                       (resolved_batch_id, calibration_id))
                self._audit(connection, actor_id=actor_id, action="batch.imported",
                            resource_type="batch", resource_id=resolved_batch_id,
                            detail={"site_id": site_id, "import_key": import_key, "brand": brand,
                                    "standard_id": standard_id, "standard_version": standard_version,
                                    "materials": materials, "packaging": packaging,
                                    "calibrations": calibrations})
                return "batch", resolved_batch_id, {"batch_id": resolved_batch_id}

            return self._idempotent(connection, request_id=request_id, action="import_batch",
                                    payload=body, create=create, related_ids=[resolved_batch_id])

    @staticmethod
    def _lot_kind(connection, lot_id: str) -> str:
        row = connection.execute("SELECT lot_kind FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
        return row["lot_kind"] if row else ""

    @staticmethod
    def _inherit_evidence(connection, child_batch_id: str, parent_batch_ids: list[str]) -> None:
        """让拆分/合并/返工子批次继承母批次的批号与校准证据。

        批号与校准只增不删：子批次因此能独立重建完整证据链，同时谱系边保留来源关系。
        """

        for parent_id in dict.fromkeys(parent_batch_ids):
            connection.execute(
                "INSERT OR IGNORE INTO batch_inputs(batch_id,lot_id) "
                "SELECT ?, lot_id FROM batch_inputs WHERE batch_id=?",
                (child_batch_id, parent_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO batch_calibrations(batch_id,calibration_id) "
                "SELECT ?, calibration_id FROM batch_calibrations WHERE batch_id=?",
                (child_batch_id, parent_id),
            )

    # -------------------------------------------------------------- 实验室结果

    def record_lab_result(self, *, request_id: str, actor_id: str, batch_id: str,
                          sample_code: str, sampled_at: str, tests: dict[str, float],
                          result_id: str | None = None) -> OperationReceipt:
        sample_code = self._id(sample_code, "sample_code")
        sampled_at = self._ts(sampled_at, "sampled_at")
        if not isinstance(tests, dict) or not tests:
            raise ValidationError("tests 必须是非空对象")
        for name, value in tests.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"tests.{name} 必须是数值")
        body = {"actor_id": actor_id, "batch_id": batch_id, "sample_code": sample_code,
                "sampled_at": sampled_at, "tests": tests}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            batch = self._batch_row(connection, batch_id)
            standard = self._standard(connection, batch["standard_id"], batch["standard_version"])
            spec = json.loads(standard["spec_json"])
            checks = self._evaluate(spec, tests)
            conforms = all(item["ok"] for item in checks.values())
            evaluation = {
                "standard_id": batch["standard_id"], "standard_version": batch["standard_version"],
                "spec_hash": standard["spec_hash"], "checks": checks, "conforms": conforms,
            }
            resolved_id = self._id(result_id, "result_id") if result_id else uuid.uuid4().hex
            tests_hash = digest(tests)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT result_id, tests_json, conforms FROM lab_results WHERE batch_id=? AND sample_code=?",
                    (batch_id, sample_code),
                ).fetchone()
                if existing:
                    if digest(json.loads(existing["tests_json"])) != tests_hash:
                        raise ConflictError("同样品编号已登记不同结果；重复导入不得改变隔离状态")
                    return "lab_result", existing["result_id"], \
                        {"sample_code": sample_code, "replayed": True}
                try:
                    connection.execute(
                        "INSERT INTO lab_results(result_id,batch_id,sample_code,sampled_at,tests_json,"
                        "conforms,evaluation_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (resolved_id, batch_id, sample_code, sampled_at, canonical_json(tests),
                         1 if conforms else 0, canonical_json(evaluation), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("实验室结果冲突") from exc
                # 只追加证据，不自动放行也不自动改变任何限制状态。
                self._audit(connection, actor_id=actor_id, action="lab_result.recorded",
                            resource_type="lab_result", resource_id=resolved_id,
                            detail={"batch_id": batch_id, "sample_code": sample_code,
                                    "conforms": conforms, "standard_version": batch["standard_version"]})
                return "lab_result", resolved_id, {"result_id": resolved_id, "conforms": conforms}

            return self._idempotent(connection, request_id=request_id, action="record_lab_result",
                                    payload=body, create=create, related_ids=[resolved_id])

    @staticmethod
    def _evaluate(spec: dict[str, Any], tests: dict[str, float]) -> dict[str, dict[str, Any]]:
        checks: dict[str, dict[str, Any]] = {}
        for name, rule in (spec.get("limits") or {}).items():
            value = tests.get(name)
            lower = rule.get("min")
            upper = rule.get("max")
            ok = value is not None and not isinstance(value, bool)
            if ok and lower is not None:
                ok = value >= lower
            if ok and upper is not None:
                ok = value <= upper
            checks[name] = {"value": value, "ok": bool(ok), "min": lower, "max": upper}
        return checks

    # --------------------------------------------------------------------- 谱系

    def split_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                    children: list[dict[str, Any]]) -> OperationReceipt:
        if not children:
            raise ValidationError("children 不能为空")
        normalized_children: list[dict[str, Any]] = []
        total = 0.0
        for index, child in enumerate(children):
            quantity = child.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
                raise ValidationError(f"children[{index}].quantity 必须是正数")
            normalized_children.append({
                "batch_id": self._id(child["batch_id"], "batch_id") if child.get("batch_id") else uuid.uuid4().hex,
                "quantity": float(quantity),
                "unit": self._text(child.get("unit") or "", "unit", 20) if child.get("unit") else None,
                "scope": canonical_json(normalize_scope(child.get("scope"))),
            })
            total += float(quantity)
        body = {"actor_id": actor_id, "batch_id": batch_id,
                "children": [{"batch_id": c["batch_id"], "quantity": c["quantity"], "scope": c["scope"]}
                             for c in normalized_children]}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            parent = self._batch_row(connection, batch_id)
            if total > parent["quantity"] + 1e-9:
                raise ValidationError("拆分数量之和不能超过母批次数量")
            for child in normalized_children:
                if connection.execute("SELECT 1 FROM batches WHERE batch_id=?", (child["batch_id"],)).fetchone():
                    raise ConflictError(f"子批次 {child['batch_id']} 已存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                first = normalized_children[0]["batch_id"]
                for child in normalized_children:
                    connection.execute(
                        "INSERT INTO batches(batch_id,site_id,brand,standard_id,standard_version,"
                        "production_start,production_end,quantity,unit,status,import_key,scope_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'registered', ?,?,?,?)",
                        (child["batch_id"], parent["site_id"], parent["brand"], parent["standard_id"],
                         parent["standard_version"], parent["production_start"], parent["production_end"],
                         child["quantity"], child["unit"] or parent["unit"],
                         f"split:{parent['batch_id']}:{child['batch_id']}", child["scope"],
                         actor_id, self._now()),
                    )
                    link_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO lineage_links(link_id,parent_batch_id,child_batch_id,relation,"
                        "quantity,detail_json,created_by,created_at) VALUES(?,?,?, 'split', ?,?,?,?)",
                        (link_id, parent["batch_id"], child["batch_id"], child["quantity"],
                         child["scope"], actor_id, self._now()),
                    )
                    self._inherit_evidence(connection, child["batch_id"], [parent["batch_id"]])
                    self._audit(connection, actor_id=actor_id, action="lineage.split",
                                resource_type="batch", resource_id=child["batch_id"],
                                detail={"parent_batch_id": parent["batch_id"], "quantity": child["quantity"]})
                return "batch", first, {"child_batch_ids": [c["batch_id"] for c in normalized_children]}

            return self._idempotent(
                connection, request_id=request_id, action="split_batch", payload=body, create=create,
                related_ids=[c["batch_id"] for c in normalized_children])

    def merge_batch(self, *, request_id: str, actor_id: str, parent_batch_ids: list[str],
                    quantity: float, unit: str | None = None, child_batch_id: str | None = None,
                    scope: dict[str, Any] | None = None) -> OperationReceipt:
        parent_batch_ids = sorted({self._id(value, "parent_batch_id") for value in (parent_batch_ids or [])})
        if len(parent_batch_ids) < 2:
            raise ValidationError("合并至少需要两个母批次")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
            raise ValidationError("quantity 必须是正数")
        scope_json = canonical_json(normalize_scope(scope))
        child_id = self._id(child_batch_id, "child_batch_id") if child_batch_id else uuid.uuid4().hex
        body = {"actor_id": actor_id, "parent_batch_ids": parent_batch_ids, "quantity": quantity,
                "child_batch_id": child_id, "scope": scope_json}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            parents = [self._batch_row(connection, pid) for pid in parent_batch_ids]
            brand = parents[0]["brand"]
            standard = (parents[0]["standard_id"], parents[0]["standard_version"])
            for parent in parents[1:]:
                if (parent["brand"], parent["standard_id"], parent["standard_version"]) != (brand, *standard):
                    raise ValidationError("只能合并且品牌与钉住标准版本一致的批次")
            if connection.execute("SELECT 1 FROM batches WHERE batch_id=?", (child_id,)).fetchone():
                raise ConflictError(f"子批次 {child_id} 已存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO batches(batch_id,site_id,brand,standard_id,standard_version,"
                    "production_start,production_end,quantity,unit,status,import_key,scope_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'registered', ?,?,?,?)",
                    (child_id, parents[0]["site_id"], brand, standard[0], standard[1],
                     min(p["production_start"] for p in parents), max(p["production_end"] for p in parents),
                     float(quantity), unit or parents[0]["unit"],
                     f"merge:{':'.join(parent_batch_ids)}:{child_id}", scope_json, actor_id, self._now()),
                )
                for parent in parents:
                    link_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO lineage_links(link_id,parent_batch_id,child_batch_id,relation,"
                        "quantity,detail_json,created_by,created_at) VALUES(?,?,?, 'merge', ?,?,?,?)",
                        (link_id, parent["batch_id"], child_id, None,
                         canonical_json({"parent_quantity": parent["quantity"]}), actor_id, self._now()),
                    )
                self._inherit_evidence(connection, child_id, parent_batch_ids)
                self._audit(connection, actor_id=actor_id, action="lineage.merge",
                            resource_type="batch", resource_id=child_id,
                            detail={"parent_batch_ids": parent_batch_ids, "quantity": quantity})
                return "batch", child_id, {"child_batch_id": child_id}

            return self._idempotent(connection, request_id=request_id, action="merge_batch",
                                    payload=body, create=create, related_ids=[child_id])

    def rework_batch(self, *, request_id: str, actor_id: str, source_batch_id: str,
                     quantity: float | None = None, derived_batch_id: str | None = None,
                     scope: dict[str, Any] | None = None, note: str | None = None) -> OperationReceipt:
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                                     or quantity <= 0):
            raise ValidationError("quantity 必须是正数")
        scope_json = canonical_json(normalize_scope(scope))
        note = self._text(note, "note", 500) if note else None
        derived_id = self._id(derived_batch_id, "derived_batch_id") if derived_batch_id else uuid.uuid4().hex
        body = {"actor_id": actor_id, "source_batch_id": source_batch_id, "quantity": quantity,
                "derived_batch_id": derived_id, "scope": scope_json, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            source = self._batch_row(connection, source_batch_id)
            if quantity is not None and quantity > source["quantity"] + 1e-9:
                raise ValidationError("返工数量不能超过来源批次数量")
            derived_quantity = float(quantity if quantity is not None else source["quantity"])
            if connection.execute("SELECT 1 FROM batches WHERE batch_id=?", (derived_id,)).fetchone():
                raise ConflictError(f"返工批次 {derived_id} 已存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO batches(batch_id,site_id,brand,standard_id,standard_version,"
                    "production_start,production_end,quantity,unit,status,import_key,scope_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'registered', ?,?,?,?)",
                    (derived_id, source["site_id"], source["brand"], source["standard_id"],
                     source["standard_version"], source["production_start"], source["production_end"],
                     derived_quantity, source["unit"], f"rework:{source['batch_id']}:{derived_id}",
                     scope_json, actor_id, self._now()),
                )
                link_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO lineage_links(link_id,parent_batch_id,child_batch_id,relation,"
                    "quantity,detail_json,created_by,created_at) VALUES(?,?,?, 'rework', ?,?,?,?)",
                    (link_id, source["batch_id"], derived_id, derived_quantity,
                     canonical_json({"note": note}), actor_id, self._now()),
                )
                self._inherit_evidence(connection, derived_id, [source["batch_id"]])
                self._audit(connection, actor_id=actor_id, action="lineage.rework",
                            resource_type="batch", resource_id=derived_id,
                            detail={"source_batch_id": source_batch_id, "note": note})
                return "batch", derived_id, {"derived_batch_id": derived_id}

            return self._idempotent(connection, request_id=request_id, action="rework_batch",
                                    payload=body, create=create, related_ids=[derived_id])

    # ------------------------------------------------------------- 偏差与限制

    def open_deviation(self, *, request_id: str, actor_id: str, batch_id: str,
                       scope: dict[str, Any], description: str, severity: str,
                       deviation_id: str | None = None) -> OperationReceipt:
        if severity not in SEVERITIES:
            raise ValidationError("severity 必须是 low/medium/high/critical")
        scope_norm = normalize_scope(scope)
        description = self._text(description, "description")
        deviation_id = self._id(deviation_id, "deviation_id") if deviation_id else uuid.uuid4().hex
        body = {"actor_id": actor_id, "batch_id": batch_id, "scope": scope_norm,
                "description": description, "severity": severity, "deviation_id": deviation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._batch_row(connection, batch_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO deviations(deviation_id,batch_id,scope_json,description,severity,status,"
                    "opened_by,opened_at,disposition_evidence_json) VALUES(?,?,?,?,?, 'open', ?,?, '[]')",
                    (deviation_id, batch_id, canonical_json(scope_norm), description, severity,
                     actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="deviation.opened",
                            resource_type="deviation", resource_id=deviation_id,
                            detail={"batch_id": batch_id, "scope": scope_norm, "severity": severity})
                return "deviation", deviation_id, {"deviation_id": deviation_id}

            return self._idempotent(connection, request_id=request_id, action="open_deviation",
                                    payload=body, create=create, related_ids=[deviation_id])

    def disposition_deviation(self, *, request_id: str, actor_id: str, deviation_id: str,
                              disposition_summary: str, evidence: list[str]) -> OperationReceipt:
        disposition_summary = self._text(disposition_summary, "disposition_summary")
        references = self._evidence(evidence)
        body = {"actor_id": actor_id, "deviation_id": deviation_id,
                "disposition_summary": disposition_summary, "evidence": references}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            deviation = self._deviation(connection, deviation_id)
            if deviation["status"] != "open":
                raise ConflictError("偏差已处置，处置记录不可改写")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE deviations SET status='dispositioned', disposition_summary=?, "
                    "disposition_evidence_json=?, dispositioned_by=?, dispositioned_at=? WHERE deviation_id=?",
                    (disposition_summary, canonical_json(references), actor_id, self._now(), deviation_id),
                )
                self._audit(connection, actor_id=actor_id, action="deviation.dispositioned",
                            resource_type="deviation", resource_id=deviation_id,
                            detail={"batch_id": deviation["batch_id"], "evidence": references,
                                    "disposition_summary": disposition_summary})
                return "deviation", deviation_id, {"deviation_id": deviation_id, "status": "dispositioned"}

            return self._idempotent(connection, request_id=request_id, action="disposition_deviation",
                                    payload=body, create=create)

    def close_deviation(self, *, request_id: str, actor_id: str, deviation_id: str) -> OperationReceipt:
        body = {"actor_id": actor_id, "deviation_id": deviation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            deviation = self._deviation(connection, deviation_id)
            if deviation["status"] == "closed":
                raise ConflictError("偏差已经关闭")
            if deviation["status"] != "dispositioned":
                raise ConflictError("偏差必须先完成处置才能关闭")
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM restrictions WHERE deviation_id=? AND status='active'",
                (deviation_id,)).fetchone()["count"]
            if active:
                raise ConflictError("仍有关联限制未解除，不能关闭偏差")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE deviations SET status='closed', closed_by=?, closed_at=? WHERE deviation_id=?",
                    (actor_id, self._now(), deviation_id),
                )
                self._audit(connection, actor_id=actor_id, action="deviation.closed",
                            resource_type="deviation", resource_id=deviation_id,
                            detail={"batch_id": deviation["batch_id"]})
                return "deviation", deviation_id, {"deviation_id": deviation_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id, action="close_deviation",
                                    payload=body, create=create)

    def impose_restriction(self, *, request_id: str, actor_id: str, batch_id: str, reason: str,
                           scope: dict[str, Any] | None = None,
                           deviation_id: str | None = None) -> OperationReceipt:
        scope_norm = normalize_scope(scope)
        reason = self._text(reason, "reason", 300)
        # 由请求编号确定性派生：同一 request_id 重放必须命中同一条限制，
        # 而服务端随机 ID 会让幂等载荷每次不同。
        restriction_id = uuid.uuid5(uuid.NAMESPACE_URL, f"restriction:{request_id}").hex
        body = {"actor_id": actor_id, "batch_id": batch_id, "reason": reason, "scope": scope_norm,
                "deviation_id": deviation_id, "restriction_id": restriction_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._batch_row(connection, batch_id)
            if deviation_id:
                deviation = self._deviation(connection, deviation_id)
                if deviation["batch_id"] != batch_id:
                    raise ValidationError("限制关联的偏差不属于该批次")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO restrictions(restriction_id,batch_id,deviation_id,scope_json,reason,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?, 'active', ?,?)",
                    (restriction_id, batch_id, deviation_id, canonical_json(scope_norm), reason,
                     actor_id, self._now()),
                )
                # 活动隔离限制使批次进入冻结粗粒度状态；是否可售以 explain 的单元投影为准。
                connection.execute("UPDATE batches SET status='frozen' WHERE batch_id=? AND status!='recalled'",
                                   (batch_id,))
                self._audit(connection, actor_id=actor_id, action="restriction.imposed",
                            resource_type="restriction", resource_id=restriction_id,
                            detail={"batch_id": batch_id, "deviation_id": deviation_id,
                                    "scope": scope_norm, "reason": reason})
                return "restriction", restriction_id, {"restriction_id": restriction_id}

            return self._idempotent(connection, request_id=request_id, action="impose_restriction",
                                    payload=body, create=create, related_ids=[restriction_id])

    def release_restriction(self, *, request_id: str, actor_id: str, restriction_id: str,
                            evidence: list[str], note: str) -> OperationReceipt:
        references = self._evidence(evidence)
        note = self._text(note, "note")
        body = {"actor_id": actor_id, "restriction_id": restriction_id, "evidence": references, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            # 解除限制属于质量放行职责，仅管理员/复核人可执行。
            self._require(actor, "admin", "reviewer")
            row = connection.execute("SELECT * FROM restrictions WHERE restriction_id=?",
                                     (restriction_id,)).fetchone()
            if row is None:
                raise NotFoundError("限制不存在")
            if row["status"] != "active":
                raise ConflictError("限制已经解除")
            if row["created_by"] == actor_id:
                raise PermissionDenied("解除限制必须由另一名授权者执行（四眼原则）")
            if row["deviation_id"]:
                deviation = self._deviation(connection, row["deviation_id"])
                if deviation["status"] == "open":
                    raise ConflictError("关联偏差尚未完成处置，不能解除限制")
                if deviation["dispositioned_by"] == actor_id:
                    raise PermissionDenied("处置人与解除限制人不能是同一人（四眼原则）")
                if not deviation["disposition_evidence_json"] or \
                        json.loads(deviation["disposition_evidence_json"]) == []:
                    raise ConflictError("处置证据缺失，不能解除限制")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE restrictions SET status='released', released_by=?, released_at=?, "
                    "release_note=?, release_evidence_json=? WHERE restriction_id=?",
                    (actor_id, self._now(), note, canonical_json(references), restriction_id),
                )
                self._audit(connection, actor_id=actor_id, action="restriction.released",
                            resource_type="restriction", resource_id=restriction_id,
                            detail={"batch_id": row["batch_id"], "evidence": references, "note": note,
                                    "imposed_by": row["created_by"]})
                # 不自动改写批次状态；是否重新放行必须另行走放行决定。
                return "restriction", restriction_id, {"restriction_id": restriction_id, "status": "released"}

            return self._idempotent(connection, request_id=request_id, action="release_restriction",
                                    payload=body, create=create)

    @staticmethod
    def _evidence(evidence: Any) -> list[str]:
        if not isinstance(evidence, list) or not evidence:
            raise ValidationError("evidence 必须是非空引用列表")
        references: list[str] = []
        for item in evidence:
            text = str(item).strip()
            if not text or len(text) > 200:
                raise ValidationError("evidence 条目不能为空且不能超过 200 个字符")
            references.append(text)
        if len(set(references)) != len(references):
            raise ValidationError("evidence 不能重复")
        return references

    def _deviation(self, connection, deviation_id: str):
        row = connection.execute("SELECT * FROM deviations WHERE deviation_id=?", (deviation_id,)).fetchone()
        if row is None:
            raise NotFoundError("偏差不存在")
        return row

    # --------------------------------------------------------------------- 决定

    def decide(self, *, request_id: str, actor_id: str, batch_id: str, decision: str,
               rationale: str, scope: dict[str, Any] | None = None) -> OperationReceipt:
        if decision not in DECISIONS:
            raise ValidationError("decision 必须是 release/freeze/recall/dispose")
        rationale = self._text(rationale, "rationale")
        scope_norm = normalize_scope(scope)
        body = {"actor_id": actor_id, "batch_id": batch_id, "decision": decision,
                "rationale": rationale, "scope": scope_norm}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            batch = self._batch_row(connection, batch_id)
            evidence = self._build_evidence(connection, batch, scope_norm)
            if decision == "release":
                self._require(actor, "admin", "reviewer")
                blockers = evidence["blockers"]
                if blockers:
                    raise ConflictError("批次尚不满足放行条件：" + "；".join(blockers))
            elif decision == "freeze":
                self._require(actor, "admin", "reviewer", "operator")
                evidence["blockers"] = []
            elif decision == "recall":
                self._require(actor, "admin", "reviewer")
                if not evidence["prior_release_decisions"]:
                    raise ConflictError("没有可召回的历史放行决定")
                evidence["recall_targets"] = evidence["prior_release_decisions"]
            else:  # dispose
                self._require(actor, "admin", "reviewer")
            decision_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO decisions(decision_id,batch_id,decision,scope_json,standard_id,"
                    "standard_version,evidence_json,rationale,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, batch_id, decision, canonical_json(scope_norm),
                     batch["standard_id"], batch["standard_version"], canonical_json(evidence), rationale,
                     actor_id, self._now()),
                )
                new_status = DECISION_STATUS[decision]
                # 仅维护当前粗粒度状态；完整可解释状态由追加的决定时间线重建。
                connection.execute("UPDATE batches SET status=? WHERE batch_id=?", (new_status, batch_id))
                if decision == "freeze":
                    # 冻结同时落一条活动限制；若范围对应唯一一个未关闭偏差则关联，
                    # 解除时必须先由另一名授权者完成处置并引用证据。
                    restriction_id = uuid.uuid4().hex
                    freeze_deviation_id = self._matching_open_deviation(connection, batch_id, scope_norm)
                    connection.execute(
                        "INSERT INTO restrictions(restriction_id,batch_id,deviation_id,scope_json,reason,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                        (restriction_id, batch_id, freeze_deviation_id, canonical_json(scope_norm),
                         f"freeze_decision:{decision_id}", actor_id, self._now()),
                    )
                    evidence["freeze_restriction_id"] = restriction_id
                    evidence["freeze_deviation_id"] = freeze_deviation_id
                    connection.execute("UPDATE decisions SET evidence_json=? WHERE decision_id=?",
                                       (canonical_json(evidence), decision_id))
                self._audit(connection, actor_id=actor_id, action=f"decision.{decision}",
                            resource_type="decision", resource_id=decision_id,
                            detail={"batch_id": batch_id, "scope": scope_norm,
                                    "standard_version": batch["standard_version"],
                                    "blockers": evidence.get("blockers", [])})
                return "decision", decision_id, {"decision_id": decision_id, "decision": decision}

            receipt = self._idempotent(connection, request_id=request_id, action="decide",
                                       payload=body, create=create, related_ids=[decision_id])
            return receipt

    def _ancestor_ids(self, connection, batch_id: str) -> list[str]:
        rows = connection.execute(
            "WITH RECURSIVE ancestors(ancestor_id) AS ("
            "SELECT parent_batch_id FROM lineage_links WHERE child_batch_id=? "
            "UNION ALL "
            "SELECT l.parent_batch_id FROM lineage_links l "
            "JOIN ancestors a ON l.child_batch_id = a.ancestor_id"
            ") SELECT DISTINCT ancestor_id FROM ancestors",
            (batch_id,),
        ).fetchall()
        return [row["ancestor_id"] for row in rows]

    def _matching_open_deviation(self, connection, batch_id: str,
                                 scope: dict[str, list[str]]) -> str | None:
        """返回与冻结范围重叠且唯一的未关闭偏差，供冻结限制关联四眼流程。"""

        rows = connection.execute(
            "SELECT deviation_id, scope_json FROM deviations WHERE batch_id=? AND status!='closed'",
            (batch_id,)).fetchall()
        matches = [r["deviation_id"] for r in rows
                   if scope_overlaps(scope, json.loads(r["scope_json"]))]
        return matches[0] if len(matches) == 1 else None

    def _build_evidence(self, connection, batch, scope: dict[str, list[str]]) -> dict[str, Any]:
        import json
        batch_id = batch["batch_id"]
        family = set(self._ancestor_ids(connection, batch_id))
        family.add(batch_id)
        standard = self._standard(connection, batch["standard_id"], batch["standard_version"])

        input_rows = connection.execute(
            "SELECT i.lot_id, l.lot_kind, l.name, l.supplier FROM batch_inputs i "
            "JOIN lots l ON l.lot_id = i.lot_id WHERE i.batch_id=? ORDER BY i.lot_id", (batch_id,)
        ).fetchall()
        inputs = [{"lot_id": r["lot_id"], "lot_kind": r["lot_kind"], "name": r["name"],
                   "supplier": r["supplier"]} for r in input_rows]

        calibration_rows = connection.execute(
            "SELECT c.* FROM batch_calibrations bc JOIN calibrations c ON c.calibration_id = bc.calibration_id "
            "WHERE bc.batch_id=? ORDER BY c.calibration_id", (batch_id,)).fetchall()
        calibrations = []
        for row in calibration_rows:
            covers = (row["status"] == "valid"
                      and row["valid_from"] <= batch["production_start"]
                      and row["valid_until"] >= batch["production_end"])
            calibrations.append({"calibration_id": row["calibration_id"], "equipment_id": row["equipment_id"],
                                 "status": row["status"], "valid_from": row["valid_from"],
                                 "valid_until": row["valid_until"], "covers_production_window": covers})
        calibration_ok = bool(calibrations) and all(item["covers_production_window"] for item in calibrations)

        lab_rows = connection.execute(
            "SELECT * FROM lab_results WHERE batch_id=? ORDER BY sampled_at, sample_code", (batch_id,)
        ).fetchall()
        labs = []
        lab_ok = bool(lab_rows)
        for row in lab_rows:
            conforms = bool(row["conforms"])
            lab_ok = lab_ok and conforms
            labs.append({"result_id": row["result_id"], "sample_code": row["sample_code"],
                         "sampled_at": row["sampled_at"], "conforms": conforms,
                         "evaluation": json.loads(row["evaluation_json"])})

        blockers: list[str] = []
        if not [i for i in inputs if i["lot_kind"] == "material"]:
            blockers.append("缺少原料批号证据")
        if not [i for i in inputs if i["lot_kind"] == "packaging"]:
            blockers.append("缺少包装批号证据")
        if not calibration_ok:
            if not calibrations:
                blockers.append("缺少设备校准证据")
            else:
                blockers.append("存在未覆盖整个生产时段或已失效的设备校准")
        if not labs:
            blockers.append("缺少实验室结果")
        elif not lab_ok:
            blockers.append("存在不合格实验室结果")

        restriction_rows = connection.execute(
            "SELECT * FROM restrictions WHERE batch_id IN (%s) AND status='active' ORDER BY created_at, rowid"
            % ",".join("?" * len(family)), tuple(sorted(family))).fetchall()
        active_restrictions = []
        for row in restriction_rows:
            item_scope = json.loads(row["scope_json"])
            overlap = scope_overlaps(scope, item_scope)
            item = {"restriction_id": row["restriction_id"], "batch_id": row["batch_id"],
                    "scope": item_scope, "reason": row["reason"], "created_by": row["created_by"],
                    "created_at": row["created_at"], "overlaps_requested_scope": overlap,
                    "inherited": row["batch_id"] != batch_id}
            active_restrictions.append(item)
            if overlap:
                source = "（继承自祖先批次）" if item["inherited"] else ""
                blockers.append(f"存在活动限制 {row['restriction_id']}{source}：{row['reason']}")

        deviation_rows = connection.execute(
            "SELECT * FROM deviations WHERE batch_id IN (%s) AND status='open' ORDER BY opened_at, deviation_id"
            % ",".join("?" * len(family)), tuple(sorted(family))).fetchall()
        open_deviations = []
        for row in deviation_rows:
            item_scope = json.loads(row["scope_json"])
            overlap = scope_overlaps(scope, item_scope)
            item = {"deviation_id": row["deviation_id"], "batch_id": row["batch_id"],
                    "scope": item_scope, "severity": row["severity"], "description": row["description"],
                    "opened_by": row["opened_by"], "opened_at": row["opened_at"],
                    "overlaps_requested_scope": overlap, "inherited": row["batch_id"] != batch_id}
            open_deviations.append(item)
            if overlap:
                source = "（继承自祖先批次）" if item["inherited"] else ""
                blockers.append(f"存在未处置偏差 {row['deviation_id']}{source}")

        prior_releases = [{"decision_id": r["decision_id"], "scope": json.loads(r["scope_json"]),
                           "decided_at": r["decided_at"], "decided_by": r["decided_by"]}
                          for r in connection.execute(
                              "SELECT * FROM decisions WHERE batch_id=? AND decision='release' "
                              "ORDER BY decided_at, rowid", (batch_id,)).fetchall()
                          if scope_overlaps(scope, json.loads(r["scope_json"]))]

        return {
            "computed_at": self._now(),
            "brand_standard": {"standard_id": batch["standard_id"], "version": batch["standard_version"],
                               "spec_hash": standard["spec_hash"]},
            "inputs": inputs,
            "calibrations": calibrations,
            "calibrations_cover_window": calibration_ok,
            "lab_results": labs,
            "lab_conforms": lab_ok,
            "active_restrictions": active_restrictions,
            "open_deviations": open_deviations,
            "prior_release_decisions": prior_releases,
            "blockers": blockers,
        }

    # --------------------------------------------------------------------- 复核

    def open_review(self, *, request_id: str, actor_id: str, batch_id: str, kind: str,
                    note: str | None = None, deviation_id: str | None = None,
                    task_id: str | None = None) -> OperationReceipt:
        if kind not in ("release_review", "deviation_review"):
            raise ValidationError("kind 必须是 release_review 或 deviation_review")
        note = self._text(note, "note") if note else None
        task_id = self._id(task_id, "task_id") if task_id else uuid.uuid4().hex
        body = {"actor_id": actor_id, "batch_id": batch_id, "kind": kind, "note": note,
                "deviation_id": deviation_id, "task_id": task_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._batch_row(connection, batch_id)
            if kind == "deviation_review":
                if not deviation_id:
                    raise ValidationError("偏差复核必须提供 deviation_id")
                deviation = self._deviation(connection, deviation_id)
                if deviation["batch_id"] != batch_id:
                    raise ValidationError("偏差不属于该批次")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO review_tasks(task_id,batch_id,kind,deviation_id,status,note,opened_by,opened_at) "
                    "VALUES(?,?,?,?, 'open', ?,?,?)",
                    (task_id, batch_id, kind, deviation_id, note, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="review.opened",
                            resource_type="review_task", resource_id=task_id,
                            detail={"batch_id": batch_id, "kind": kind, "deviation_id": deviation_id})
                return "review_task", task_id, {"task_id": task_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id, action="open_review",
                                    payload=body, create=create, related_ids=[task_id])

    def complete_review(self, *, request_id: str, actor_id: str, task_id: str,
                        decision: str, rationale: str,
                        scope: dict[str, Any] | None = None) -> OperationReceipt:
        if decision not in DECISIONS:
            raise ValidationError("decision 必须是 release/freeze/recall/dispose")
        rationale = self._text(rationale, "rationale")
        scope_norm = normalize_scope(scope)
        body = {"actor_id": actor_id, "task_id": task_id, "decision": decision,
                "rationale": rationale, "scope": scope_norm}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("复核任务不存在")
            if task["status"] != "open":
                raise ConflictError("复核任务已经办结")
            batch = self._batch_row(connection, task["batch_id"])
            evidence = self._build_evidence(connection, batch, scope_norm)
            if decision == "release":
                self._require(actor, "admin", "reviewer")
                if task["opened_by"] == actor_id:
                    raise PermissionDenied("放行复核必须由开单人之外的授权者办结（四眼原则）")
                if evidence["blockers"]:
                    raise ConflictError("批次尚不满足放行条件：" + "；".join(evidence["blockers"]))
            elif decision == "freeze":
                self._require(actor, "admin", "reviewer", "operator")
                evidence["blockers"] = []
            elif decision == "recall":
                self._require(actor, "admin", "reviewer")
                if not evidence["prior_release_decisions"]:
                    raise ConflictError("没有可召回的历史放行决定")
            else:
                self._require(actor, "admin", "reviewer")
            decision_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO decisions(decision_id,batch_id,decision,scope_json,standard_id,"
                    "standard_version,evidence_json,rationale,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, task["batch_id"], decision, canonical_json(scope_norm),
                     batch["standard_id"], batch["standard_version"], canonical_json(evidence), rationale,
                     actor_id, self._now()),
                )
                connection.execute("UPDATE batches SET status=? WHERE batch_id=?",
                                   (DECISION_STATUS[decision], task["batch_id"]))
                if decision == "freeze":
                    restriction_id = uuid.uuid4().hex
                    freeze_deviation_id = self._matching_open_deviation(connection, task["batch_id"], scope_norm)
                    connection.execute(
                        "INSERT INTO restrictions(restriction_id,batch_id,deviation_id,scope_json,reason,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                        (restriction_id, task["batch_id"], freeze_deviation_id, canonical_json(scope_norm),
                         f"freeze_decision:{decision_id}", actor_id, self._now()),
                    )
                connection.execute(
                    "UPDATE review_tasks SET status='completed', completed_by=?, completed_at=?, decision_id=? "
                    "WHERE task_id=?",
                    (actor_id, self._now(), decision_id, task_id),
                )
                self._audit(connection, actor_id=actor_id, action="review.completed",
                            resource_type="review_task", resource_id=task_id,
                            detail={"batch_id": task["batch_id"], "decision": decision,
                                    "decision_id": decision_id})
                return "decision", decision_id, {"decision_id": decision_id, "task_id": task_id}

            return self._idempotent(connection, request_id=request_id, action="complete_review",
                                    payload=body, create=create, related_ids=[decision_id, task_id])

    def cancel_review(self, *, request_id: str, actor_id: str, task_id: str,
                      note: str) -> OperationReceipt:
        note = self._text(note, "note")
        body = {"actor_id": actor_id, "task_id": task_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("复核任务不存在")
            if task["status"] != "open":
                raise ConflictError("复核任务已经办结")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE review_tasks SET status='cancelled', completed_by=?, completed_at=? "
                                   "WHERE task_id=?", (actor_id, self._now(), task_id))
                self._audit(connection, actor_id=actor_id, action="review.cancelled",
                            resource_type="review_task", resource_id=task_id,
                            detail={"batch_id": task["batch_id"], "note": note})
                return "review_task", task_id, {"task_id": task_id, "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id, action="cancel_review",
                                    payload=body, create=create)

    def list_reviews(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in ("open", "completed", "cancelled"):
            raise ValidationError("status 必须是 open/completed/cancelled")
        query = "SELECT * FROM review_tasks"
        parameters: list[Any] = []
        if status:
            query += " WHERE status=?"
            parameters.append(status)
        query += " ORDER BY opened_at, task_id"
        return [self._review_dict(row) for row in self.database.connection.execute(query, parameters)]

    @staticmethod
    def _review_dict(row) -> dict[str, Any]:
        return {"task_id": row["task_id"], "batch_id": row["batch_id"], "kind": row["kind"],
                "deviation_id": row["deviation_id"], "status": row["status"], "note": row["note"],
                "opened_by": row["opened_by"], "opened_at": row["opened_at"],
                "completed_by": row["completed_by"], "completed_at": row["completed_at"],
                "decision_id": row["decision_id"]}

    # --------------------------------------------------------------------- 出库

    def record_shipment(self, *, request_id: str, actor_id: str, batch_id: str, decision_id: str,
                        quantity: float, scope: dict[str, Any] | None = None,
                        shipment_id: str | None = None, shipped_at: str | None = None) -> OperationReceipt:
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
            raise ValidationError("quantity 必须是正数")
        shipped_at = self._ts(shipped_at, "shipped_at") if shipped_at else self._now()
        shipment_id = self._id(shipment_id, "shipment_id") if shipment_id else uuid.uuid4().hex
        scope_norm = normalize_scope(scope)
        body = {"actor_id": actor_id, "batch_id": batch_id, "decision_id": decision_id,
                "quantity": quantity, "shipped_at": shipped_at, "shipment_id": shipment_id,
                "scope": scope_norm}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch_row(connection, batch_id)
            decision = connection.execute("SELECT * FROM decisions WHERE decision_id=?",
                                          (decision_id,)).fetchone()
            if decision is None:
                raise NotFoundError("放行决定不存在")
            if decision["batch_id"] != batch_id or decision["decision"] != "release":
                raise ValidationError("出库必须引用该批次自身的放行决定")
            if shipped_at < decision["decided_at"]:
                raise ValidationError("出库时间不能早于放行决定时间")
            if not scope_overlaps(scope_norm, json.loads(decision["scope_json"])):
                raise ValidationError("出库范围不在放行决定覆盖范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO shipments(shipment_id,batch_id,decision_id,quantity,scope_json,shipped_at,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (shipment_id, batch_id, decision_id, float(quantity), canonical_json(scope_norm),
                     shipped_at, actor_id, self._now()),
                )
                # 决策行不做任何更新：出库后该放行决定成为锁定的历史事实。
                self._audit(connection, actor_id=actor_id, action="shipment.recorded",
                            resource_type="shipment", resource_id=shipment_id,
                            detail={"batch_id": batch_id, "decision_id": decision_id, "quantity": quantity})
                return "shipment", shipment_id, {"shipment_id": shipment_id}

            return self._idempotent(connection, request_id=request_id, action="record_shipment",
                                    payload=body, create=create, related_ids=[shipment_id])

    # --------------------------------------------------------------------- 读模

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        import json
        row = self._batch_row(self.database.connection, batch_id)
        site = self.database.connection.execute("SELECT * FROM sites WHERE site_id=?", (row["site_id"],)).fetchone()
        return {"batch_id": row["batch_id"], "site_id": row["site_id"],
                "site_name": site["name"] if site else None, "brand": row["brand"],
                "standard_id": row["standard_id"], "standard_version": row["standard_version"],
                "production_start": row["production_start"], "production_end": row["production_end"],
                "quantity": row["quantity"], "unit": row["unit"], "status": row["status"],
                "import_key": row["import_key"], "scope": json.loads(row["scope_json"] or "{}"),
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def list_batches(self, site_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM batches"
        parameters: list[Any] = []
        if site_id:
            query += " WHERE site_id=?"
            parameters.append(site_id)
        query += " ORDER BY created_at, batch_id"
        return [self.get_batch(row["batch_id"]) for row in self.database.connection.execute(query, parameters)]

    def get_lineage(self, batch_id: str) -> dict[str, Any]:
        self._batch_row(self.database.connection, batch_id)
        connection = self.database.connection
        parents = []
        for row in connection.execute(
            "WITH RECURSIVE up(link_id,parent_batch_id,child_batch_id,relation,quantity,detail_json,depth) AS ("
            "SELECT link_id,parent_batch_id,child_batch_id,relation,quantity,detail_json,1 FROM lineage_links "
            "WHERE child_batch_id=? "
            "UNION ALL "
            "SELECT l.link_id,l.parent_batch_id,l.child_batch_id,l.relation,l.quantity,l.detail_json,u.depth+1 "
            "FROM lineage_links l JOIN up u ON l.child_batch_id=u.parent_batch_id"
            ") SELECT * FROM up ORDER BY depth, link_id", (batch_id,)).fetchall():
            parents.append({"link_id": row["link_id"], "parent_batch_id": row["parent_batch_id"],
                            "child_batch_id": row["child_batch_id"], "relation": row["relation"],
                            "quantity": row["quantity"], "detail": json.loads(row["detail_json"]),
                            "depth": row["depth"]})
        children = []
        for row in connection.execute("SELECT * FROM lineage_links WHERE parent_batch_id=? ORDER BY created_at, link_id",
                                     (batch_id,)).fetchall():
            children.append({"link_id": row["link_id"], "child_batch_id": row["child_batch_id"],
                             "relation": row["relation"], "quantity": row["quantity"]})
        return {"batch_id": batch_id, "ancestors": parents, "direct_children": children}

    def explain_batch(self, batch_id: str) -> dict[str, Any]:
        """重建批次为何被放行、冻结或召回的完整证据链。"""

        import json
        from beverage_ops_foundation.audit import verify_chain
        connection = self.database.connection
        batch = self.get_batch(batch_id)
        standard = self._standard(connection, batch["standard_id"], batch["standard_version"])

        lots = []
        for row in connection.execute(
                "SELECT l.* FROM batch_inputs i JOIN lots l ON l.lot_id=i.lot_id WHERE i.batch_id=? ORDER BY l.lot_id",
                (batch_id,)).fetchall():
            lots.append({"lot_id": row["lot_id"], "lot_kind": row["lot_kind"], "name": row["name"],
                         "supplier": row["supplier"], "payload": json.loads(row["payload_json"])})

        calibrations = []
        for row in connection.execute(
                "SELECT c.* FROM batch_calibrations bc JOIN calibrations c ON c.calibration_id=bc.calibration_id "
                "WHERE bc.batch_id=? ORDER BY c.calibration_id", (batch_id,)).fetchall():
            calibrations.append({"calibration_id": row["calibration_id"], "equipment_id": row["equipment_id"],
                                 "status": row["status"], "calibrated_at": row["calibrated_at"],
                                 "valid_from": row["valid_from"], "valid_until": row["valid_until"],
                                 "covers_production_window": (
                                     row["status"] == "valid"
                                     and row["valid_from"] <= batch["production_start"]
                                     and row["valid_until"] >= batch["production_end"])})

        lab_results = []
        for row in connection.execute("SELECT * FROM lab_results WHERE batch_id=? ORDER BY sampled_at, sample_code",
                                      (batch_id,)).fetchall():
            lab_results.append({"result_id": row["result_id"], "sample_code": row["sample_code"],
                                "sampled_at": row["sampled_at"], "tests": json.loads(row["tests_json"]),
                                "conforms": bool(row["conforms"]),
                                "evaluation": json.loads(row["evaluation_json"])})

        deviations = []
        ancestor_ids = set(self._ancestor_ids(connection, batch_id))
        family = sorted(ancestor_ids | {batch_id})
        for row in connection.execute(
                "SELECT * FROM deviations WHERE batch_id IN (%s) ORDER BY opened_at, deviation_id"
                % ",".join("?" * len(family)), family).fetchall():
            deviations.append({"deviation_id": row["deviation_id"], "batch_id": row["batch_id"],
                               "inherited": row["batch_id"] != batch_id,
                               "scope": json.loads(row["scope_json"]), "description": row["description"],
                               "severity": row["severity"], "status": row["status"],
                               "opened_by": row["opened_by"], "opened_at": row["opened_at"],
                               "disposition_summary": row["disposition_summary"],
                               "disposition_evidence": json.loads(row["disposition_evidence_json"] or "[]"),
                               "dispositioned_by": row["dispositioned_by"],
                               "dispositioned_at": row["dispositioned_at"],
                               "closed_by": row["closed_by"], "closed_at": row["closed_at"]})

        restrictions = []
        for row in connection.execute(
                "SELECT * FROM restrictions WHERE batch_id IN (%s) ORDER BY created_at, rowid"
                % ",".join("?" * len(family)), family).fetchall():
            restrictions.append({"restriction_id": row["restriction_id"], "batch_id": row["batch_id"],
                                 "inherited": row["batch_id"] != batch_id,
                                 "deviation_id": row["deviation_id"],
                                 "scope": json.loads(row["scope_json"]), "reason": row["reason"],
                                 "status": row["status"], "created_by": row["created_by"],
                                 "created_at": row["created_at"], "released_by": row["released_by"],
                                 "released_at": row["released_at"], "release_note": row["release_note"],
                                 "release_evidence": json.loads(row["release_evidence_json"] or "[]")})

        decisions = []
        shipment_rows = connection.execute("SELECT * FROM shipments WHERE batch_id=?", (batch_id,)).fetchall()
        shipments_by_decision: dict[str, list[dict[str, Any]]] = {}
        for shipment in shipment_rows:
            shipments_by_decision.setdefault(shipment["decision_id"], []).append(
                {"shipment_id": shipment["shipment_id"], "quantity": shipment["quantity"],
                 "scope": json.loads(shipment["scope_json"]), "shipped_at": shipment["shipped_at"]})
        for row in connection.execute("SELECT * FROM decisions WHERE batch_id=? ORDER BY decided_at, rowid",
                                      (batch_id,)).fetchall():
            decisions.append({
                "decision_id": row["decision_id"], "decision": row["decision"],
                "scope": json.loads(row["scope_json"]),
                "standard_id": row["standard_id"], "standard_version": row["standard_version"],
                "evidence": json.loads(row["evidence_json"]), "rationale": row["rationale"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "shipments": shipments_by_decision.get(row["decision_id"], []),
                "historical_locked": bool(shipments_by_decision.get(row["decision_id"])),
            })

        reviews = [self._review_dict(row) for row in connection.execute(
            "SELECT * FROM review_tasks WHERE batch_id=? ORDER BY opened_at, task_id", (batch_id,)).fetchall()]

        effective = self._project_state(batch, decisions, restrictions)
        valid, audit_count = verify_chain(connection)
        return {
            "batch": batch,
            "brand_standard": {"standard_id": standard["standard_id"], "version": standard["version"],
                               "brand": standard["brand"], "spec": json.loads(standard["spec_json"]),
                               "spec_hash": standard["spec_hash"], "immutable": True},
            "inputs": lots,
            "calibrations": calibrations,
            "lab_results": lab_results,
            "lineage": {"ancestor_ids": sorted(ancestor_ids), **self.get_lineage(batch_id)},
            "deviations": deviations,
            "restrictions": restrictions,
            "decisions": decisions,
            "review_tasks": reviews,
            "effective_state": effective,
            "replay_notice": "决定仅追加、不更新；历史放行决定在出库后锁定，新标准版本不倒改既有评价。",
            "audit": {"valid": valid, "events": audit_count},
            "computed_at": self._now(),
        }

    def _project_state(self, batch: dict[str, Any], decisions: list[dict[str, Any]],
                       restrictions: list[dict[str, Any]]) -> dict[str, Any]:
        """按包装 × 销售区域单元重放时间线，得到当前有效状态。

        每个维度既枚举范围内出现过的具体值，也保留一个 ``None`` 通配单元表示
        "其余全部"，因此"只冻结某区域、其余仍放行"会被投影成 ``mixed``。
        """

        packaging_universe: set[str] = set()
        region_universe: set[str] = set()
        for collection in (decisions, restrictions):
            for item in collection:
                packaging_universe.update(item["scope"]["packaging"])
                region_universe.update(item["scope"]["regions"])
        pkg_dim = sorted(packaging_universe) + [None]
        reg_dim = sorted(region_universe) + [None]
        cells = []
        for packaging in pkg_dim:
            for region in reg_dim:
                cell_scope = {"packaging": [packaging] if packaging else [],
                              "regions": [region] if region else []}
                overlapping_decisions = [d for d in decisions
                                         if scope_covers_cell(d["scope"], packaging, region)]
                last_decision = overlapping_decisions[-1]["decision"] if overlapping_decisions else None
                held = any(r["status"] == "active" and scope_covers_cell(r["scope"], packaging, region)
                           for r in restrictions)
                if last_decision == "recall":
                    state = "recalled"
                elif held or last_decision == "freeze":
                    state = "frozen"
                elif last_decision == "dispose":
                    state = "disposed"
                elif last_decision == "release":
                    state = "released"
                else:
                    state = "registered"
                cells.append({"packaging": packaging, "region": region, "state": state})
        states = {cell["state"] for cell in cells}
        overall = next(iter(states)) if len(states) == 1 else "mixed"
        saleable = [cell for cell in cells if cell["state"] == "released"]
        return {"overall": overall, "cells": cells, "saleable_cells": saleable,
                "headline_status": batch["status"]}

    # --------------------------------------------------------------------- 审计

    def verify_audit(self) -> tuple[bool, int]:
        from beverage_ops_foundation.audit import verify_chain
        return verify_chain(self.database.connection)

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        return self.foundation.audit_events(after_sequence)
