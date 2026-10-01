"""跨工厂批次放行域服务。

在基础服务（组织、人员、站点、幂等收据、哈希审计链）之上实现：

- 品牌标准版本登记与放行时点快照；
- 产线、原料/包装批号（含隔离标记）、设备校准登记；
- 生产批次登记与拆分/合并/返工谱系；
- 实验室结果导入（重复导入不改变任何状态）；
- 生产/实验室/放行三段复核（持久化，服务重启后可继续）；
- 可按整批/包装/销售区域挂接与解除的偏差限制（四眼原则+处置证据）；
- 仅追加的放行/冻结/召回决定台账，出库时固化决定快照；
- 任一批次的全量放行链还原（explain）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor
from ..storage import Database

WRITE_ROLES = ("admin", "operator", "reviewer", "quality_lead")
STANDARD_ROLES = ("admin", "reviewer", "quality_lead")
DECISION_ROLES = ("admin", "quality_lead")
LIFT_ROLES = ("admin", "reviewer", "quality_lead")


def _parse_dt(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 8601 时间字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed


def _norm_dt(value: Any, field: str) -> str:
    """解析后统一转为 UTC 的 Z 文本，保证字典序与时间序一致。"""

    parsed = _parse_dt(value, field)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field} 必须是非空数组")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{field} 中的元素必须是非空字符串")
        item = item.strip()
        if item in result:
            raise ValidationError(f"{field} 不能包含重复值 {item}")
        result.append(item)
    return result


class ReleaseService:
    """协调跨工厂批次放行的全部业务规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str, actor: Actor):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            stored_response = json.loads(row["response_json"])
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True, **stored_response}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    def _evidence(self, connection, ref: str, field: str = "evidence_ref") -> str:
        if not isinstance(ref, str) or not ref.strip():
            raise ValidationError(f"{field} 不能为空")
        ref = ref.strip()
        checks = {
            "lab_result": "SELECT 1 FROM rl_lab_results WHERE result_id=?",
            "calibration": "SELECT 1 FROM rl_calibrations WHERE calibration_id=?",
            "domain_record": "SELECT 1 FROM domain_records WHERE record_id=?",
            "decision": "SELECT 1 FROM rl_decisions WHERE decision_id=?",
            "material_lot": "SELECT 1 FROM rl_material_lots WHERE material_lot_id=?",
        }
        if ":" not in ref:
            raise ValidationError(f"{field} 必须使用 类型:标识 的形式引用处置证据")
        kind, ident = ref.split(":", 1)
        if kind not in checks:
            raise ValidationError(f"{field} 的证据类型 {kind} 不受支持")
        if connection.execute(checks[kind], (ident,)).fetchone() is None:
            raise NotFoundError(f"{field} 引用的证据不存在: {ref}")
        return ref

    # ---------------------------------------------------------- 品牌标准版本

    def register_brand_standard(self, *, request_id: str, actor_id: str, standard_id: str,
                                version: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(standard_id, str) or not standard_id.strip():
            raise ValidationError("standard_id 不能为空")
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        tests = payload.get("tests")
        if not isinstance(tests, dict) or not tests:
            raise ValidationError("品牌标准必须包含非空 tests 字典")
        for code, spec in tests.items():
            if not isinstance(code, str) or not isinstance(spec, dict):
                raise ValidationError("tests 的键值结构无效")
            if not isinstance(spec.get("method_code"), str) or not spec["method_code"].strip():
                raise ValidationError(f"检验项目 {code} 缺少 method_code")
            for bound in ("min", "max"):
                if bound in spec and not isinstance(spec[bound], (int, float)):
                    raise ValidationError(f"检验项目 {code} 的 {bound} 必须是数值")
        if not isinstance(version, int) or version < 1:
            raise ValidationError("version 必须是不小于 1 的整数")
        body = {"actor_id": actor_id, "standard_id": standard_id, "version": version, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *STANDARD_ROLES)
            payload_json = canonical_json(payload)
            payload_hash = digest(payload)

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT MAX(version) AS v FROM rl_brand_standards WHERE standard_id=?",
                    (standard_id,),
                ).fetchone()["v"]
                if latest is not None and version != latest + 1:
                    raise ConflictError("品牌标准版本必须按顺序递增，不能补插或覆盖历史版本")
                connection.execute(
                    "INSERT INTO rl_brand_standards(standard_id,version,payload_json,payload_hash,"
                    "registered_by,created_at) VALUES(?,?,?,?,?,?)",
                    (standard_id, version, payload_json, payload_hash, actor_id, self._now()),
                )
                if latest is not None:
                    connection.execute(
                        "UPDATE rl_brand_standards SET superseded_at=? "
                        "WHERE standard_id=? AND version=?",
                        (self._now(), standard_id, latest),
                    )
                self._audit(connection, actor_id=actor_id, action="release.standard_registered",
                            resource_type="brand_standard", resource_id=f"{standard_id}:{version}",
                            detail={"standard_id": standard_id, "version": version,
                                    "payload_hash": payload_hash, "supersedes": latest})
                response = {"standard_id": standard_id, "version": version,
                            "payload_hash": payload_hash, "supersedes": latest}
                return "brand_standard", f"{standard_id}:{version}", response

            return self._idempotent(connection, request_id=request_id,
                                    action="release.register_brand_standard",
                                    payload=body, create=create)

    def get_brand_standard(self, standard_id: str, version: int | None = None) -> dict[str, Any]:
        connection = self.database.connection
        if version is None:
            row = connection.execute(
                "SELECT * FROM rl_brand_standards WHERE standard_id=? ORDER BY version DESC LIMIT 1",
                (standard_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM rl_brand_standards WHERE standard_id=? AND version=?",
                (standard_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError("品牌标准或版本不存在")
        return {"standard_id": row["standard_id"], "version": row["version"],
                "payload": json.loads(row["payload_json"]), "payload_hash": row["payload_hash"],
                "registered_by": row["registered_by"], "created_at": row["created_at"],
                "superseded_at": row["superseded_at"]}

    # ---------------------------------------------------------------- 产线

    def register_line(self, *, request_id: str, actor_id: str, line_id: str,
                      site_id: str, name: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "line_id": line_id, "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._site(connection, site_id, actor)
            if not isinstance(name, str) or not name.strip():
                raise ValidationError("name 不能为空")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO rl_lines(line_id,site_id,name,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (line_id, site_id, name.strip(), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("产线编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="release.line_registered",
                            resource_type="line", resource_id=line_id,
                            detail={"site_id": site_id, "name": name.strip()})
                return "line", line_id, {"line_id": line_id, "site_id": site_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.register_line", payload=body, create=create)

    # ------------------------------------------------- 原料/包装批号与隔离

    def register_material_lot(self, *, request_id: str, actor_id: str, material_lot_id: str,
                              site_id: str, kind: str, material_code: str,
                              attributes: dict[str, Any], quarantined: bool = False,
                              import_key: str | None = None) -> dict[str, Any]:
        if kind not in ("raw_material", "packaging"):
            raise ValidationError("kind 必须是 raw_material 或 packaging")
        if not isinstance(material_code, str) or not material_code.strip():
            raise ValidationError("material_code 不能为空")
        if not isinstance(attributes, dict):
            raise ValidationError("attributes 必须是对象")
        body = {"actor_id": actor_id, "material_lot_id": material_lot_id, "site_id": site_id,
                "kind": kind, "material_code": material_code, "attributes": attributes,
                "import_key": import_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._site(connection, site_id, actor)
            payload = {"kind": kind, "material_code": material_code.strip(), "attributes": attributes}
            payload_json = canonical_json(payload)
            payload_hash = digest(payload)
            existing = connection.execute(
                "SELECT * FROM rl_material_lots WHERE material_lot_id=?", (material_lot_id,)
            ).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                if existing:
                    if existing["payload_hash"] != payload_hash:
                        raise ConflictError("同一物料批号已经登记不同内容，且历史批号不可改写")
                    if existing["site_id"] != site_id:
                        raise ConflictError("物料批号已属于其他场所")
                    # 关键规则：重复导入只能确认既有记录，绝不改变隔离状态。
                    if import_key:
                        connection.execute(
                            "INSERT OR IGNORE INTO rl_material_imports(import_key,material_lot_id,payload_hash) "
                            "VALUES(?,?,?)",
                            (import_key, material_lot_id, payload_hash),
                        )
                    return ("material_lot", material_lot_id,
                            {"material_lot_id": material_lot_id, "quarantined": bool(existing["quarantined"]),
                             "replayed_existing": True})
                try:
                    connection.execute(
                        "INSERT INTO rl_material_lots(material_lot_id,site_id,kind,material_code,"
                        "payload_json,payload_hash,quarantined,registered_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (material_lot_id, site_id, kind, material_code.strip(), payload_json,
                         payload_hash, 1 if quarantined else 0, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("物料批号已经存在") from exc
                if import_key:
                    connection.execute(
                        "INSERT INTO rl_material_imports(import_key,material_lot_id,payload_hash) "
                        "VALUES(?,?,?)",
                        (import_key, material_lot_id, payload_hash),
                    )
                self._audit(connection, actor_id=actor_id, action="release.material_lot_registered",
                            resource_type="material_lot", resource_id=material_lot_id,
                            detail={"site_id": site_id, "kind": kind,
                                    "material_code": material_code.strip(),
                                    "quarantined": bool(quarantined), "import_key": import_key,
                                    "payload_hash": payload_hash})
                return ("material_lot", material_lot_id,
                        {"material_lot_id": material_lot_id, "quarantined": bool(quarantined)})

            return self._idempotent(connection, request_id=request_id,
                                    action="release.register_material_lot",
                                    payload=body, create=create)

    def set_material_quarantine(self, *, request_id: str, actor_id: str, material_lot_id: str,
                                quarantined: bool, reason: str,
                                evidence_ref: str | None = None) -> dict[str, Any]:
        body = {"actor_id": actor_id, "material_lot_id": material_lot_id,
                "quarantined": quarantined, "reason": reason, "evidence_ref": evidence_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer", "quality_lead")
            row = connection.execute(
                "SELECT * FROM rl_material_lots WHERE material_lot_id=?", (material_lot_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("物料批号不存在")
            if not isinstance(reason, str) or not reason.strip():
                raise ValidationError("reason 不能为空")
            resolved_ref = self._evidence(connection, evidence_ref) if evidence_ref else None

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE rl_material_lots SET quarantined=? WHERE material_lot_id=?",
                    (1 if quarantined else 0, material_lot_id),
                )
                self._audit(connection, actor_id=actor_id,
                            action="release.material_quarantine_changed",
                            resource_type="material_lot", resource_id=material_lot_id,
                            detail={"quarantined": bool(quarantined), "reason": reason.strip(),
                                    "evidence_ref": resolved_ref,
                                    "previous": bool(row["quarantined"])})
                return ("material_lot", material_lot_id,
                        {"material_lot_id": material_lot_id, "quarantined": bool(quarantined)})

            return self._idempotent(connection, request_id=request_id,
                                    action="release.set_material_quarantine",
                                    payload=body, create=create)

    def get_material_lot(self, material_lot_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM rl_material_lots WHERE material_lot_id=?", (material_lot_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("物料批号不存在")
        payload = json.loads(row["payload_json"])
        return {"material_lot_id": row["material_lot_id"], "site_id": row["site_id"],
                "kind": row["kind"], "material_code": row["material_code"],
                "attributes": payload["attributes"], "quarantined": bool(row["quarantined"]),
                "registered_by": row["registered_by"], "created_at": row["created_at"]}

    # ---------------------------------------------------------------- 校准

    def register_calibration(self, *, request_id: str, actor_id: str, calibration_id: str,
                             site_id: str, equipment_code: str, valid_from: str, valid_until: str,
                             payload: dict[str, Any] | None = None) -> dict[str, Any]:
        start = _parse_dt(valid_from, "valid_from")
        end = _parse_dt(valid_until, "valid_until")
        if start >= end:
            raise ValidationError("valid_from 必须早于 valid_until")
        if not isinstance(equipment_code, str) or not equipment_code.strip():
            raise ValidationError("equipment_code 不能为空")
        if payload is not None and not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        start_s = _norm_dt(valid_from, "valid_from")
        end_s = _norm_dt(valid_until, "valid_until")
        body = {"actor_id": actor_id, "calibration_id": calibration_id, "site_id": site_id,
                "equipment_code": equipment_code, "valid_from": start_s,
                "valid_until": end_s, "payload": payload or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._site(connection, site_id, actor)
            stored = canonical_json({"valid_from": start_s, "valid_until": end_s,
                                     "equipment_code": equipment_code.strip(), "payload": payload or {}})
            stored_hash = digest(stored)
            existing = connection.execute(
                "SELECT payload_hash FROM rl_calibrations WHERE calibration_id=?", (calibration_id,)
            ).fetchone()
            def create() -> tuple[str, str, dict[str, Any]]:
                if existing:
                    if existing["payload_hash"] != stored_hash:
                        raise ConflictError("校准编号已经登记为不同内容")
                    return "calibration", calibration_id, {"calibration_id": calibration_id,
                                                           "replayed_existing": True}
                try:
                    connection.execute(
                        "INSERT INTO rl_calibrations(calibration_id,site_id,equipment_code,"
                        "valid_from,valid_until,payload_json,payload_hash,registered_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (calibration_id, site_id, equipment_code.strip(), start_s,
                         end_s, canonical_json(payload or {}), stored_hash,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("校准编号已经存在") from exc
                self._audit(connection, actor_id=actor_id,
                            action="release.calibration_registered",
                            resource_type="calibration", resource_id=calibration_id,
                            detail={"site_id": site_id, "equipment_code": equipment_code.strip(),
                                    "valid_from": start_s, "valid_until": end_s})
                return "calibration", calibration_id, {"calibration_id": calibration_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.register_calibration",
                                    payload=body, create=create)

    # ---------------------------------------------------------- 批次与谱系

    def register_batch(self, *, request_id: str, actor_id: str, batch_id: str, site_id: str,
                       line_id: str, product_code: str, standard_id: str, standard_version: int,
                       production_start: str, production_end: str, package_codes: list[str],
                       region_codes: list[str], quantity: int,
                       material_lot_ids: list[str] | None = None,
                       parent_batch_ids: list[str] | None = None,
                       relation: str = "original", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        start = _parse_dt(production_start, "production_start")
        end = _parse_dt(production_end, "production_end")
        if start >= end:
            raise ValidationError("production_start 必须早于 production_end")
        start_s = _norm_dt(production_start, "production_start")
        end_s = _norm_dt(production_end, "production_end")
        packages = _string_list(package_codes, "package_codes")
        regions = _string_list(region_codes, "region_codes")
        if relation not in ("original", "split", "merge", "rework"):
            raise ValidationError("relation 必须是 original/split/merge/rework")
        parent_ids: list[str] = []
        for parent in parent_batch_ids or []:
            if not isinstance(parent, str) or not parent.strip():
                raise ValidationError("parent_batch_ids 中的元素必须是非空字符串")
            parent = parent.strip()
            if parent in parent_ids:
                raise ValidationError(f"parent_batch_ids 不能包含重复值 {parent}")
            parent_ids.append(parent)
        material_ids: list[str] = []
        for material in material_lot_ids or []:
            if not isinstance(material, str) or not material.strip():
                raise ValidationError("material_lot_ids 中的元素必须是非空字符串")
            material = material.strip()
            if material in material_ids:
                raise ValidationError(f"material_lot_ids 不能包含重复值 {material}")
            material_ids.append(material)
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValidationError("quantity 必须是正整数")
        if relation != "original" and not parent_ids:
            raise ValidationError(f"{relation} 批次必须引用父批次")
        if relation == "merge" and len(parent_ids) < 2:
            raise ValidationError("合并批次至少需要两个父批次")
        body = {"actor_id": actor_id, "batch_id": batch_id, "site_id": site_id, "line_id": line_id,
                "product_code": product_code, "standard_id": standard_id,
                "standard_version": standard_version, "production_start": start_s,
                "production_end": end_s, "package_codes": packages,
                "region_codes": regions, "quantity": quantity, "material_lot_ids": material_ids,
                "parent_batch_ids": parent_ids, "relation": relation, "payload": payload or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._site(connection, site_id, actor)
            line = connection.execute("SELECT * FROM rl_lines WHERE line_id=?", (line_id,)).fetchone()
            if line is None or line["site_id"] != site_id:
                raise NotFoundError("产线不存在或不属于该场所")
            standard = connection.execute(
                "SELECT * FROM rl_brand_standards WHERE standard_id=? AND version=?",
                (standard_id, standard_version),
            ).fetchone()
            if standard is None:
                raise NotFoundError("引用的品牌标准版本不存在")
            products = json.loads(standard["payload_json"]).get("products")
            if isinstance(products, list) and products and product_code not in products:
                raise ValidationError("产品不在该品牌标准版本的适用范围内")
            for parent_id in parent_ids:
                if connection.execute("SELECT 1 FROM rl_batches WHERE batch_id=?",
                                      (parent_id,)).fetchone() is None:
                    raise NotFoundError(f"父批次不存在: {parent_id}")
            # 谱系批次自动继承父批次的物料批号，加上新投入的物料。
            inherited = [r["batch_id"] for r in connection.execute(
                "SELECT material_lot_id AS batch_id FROM rl_batch_materials WHERE batch_id IN (%s)"
                % ",".join("?" * len(parent_ids)),
                parent_ids,
            ).fetchall()] if parent_ids else []
            all_materials: list[str] = []
            for mid in inherited + material_ids:
                if mid not in all_materials:
                    all_materials.append(mid)
            if not all_materials:
                raise ValidationError("批次必须关联至少一个原料或包装批号")
            for mid in all_materials:
                mrow = connection.execute(
                    "SELECT site_id FROM rl_material_lots WHERE material_lot_id=?", (mid,)
                ).fetchone()
                if mrow is None:
                    raise NotFoundError(f"物料批号不存在: {mid}")
            stored_payload = canonical_json(payload or {})

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute(
                        "SELECT 1 FROM rl_batches WHERE batch_id=?", (batch_id,)).fetchone():
                    raise ConflictError("批次编号已经存在")
                connection.execute(
                    "INSERT INTO rl_batches(batch_id,site_id,line_id,product_code,standard_id,"
                    "standard_version,production_start,production_end,package_codes_json,"
                    "region_codes_json,quantity,parent_relation,payload_json,registered_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, site_id, line_id, product_code, standard_id, standard_version,
                     start_s, end_s, canonical_json(packages),
                     canonical_json(regions), quantity, relation, stored_payload,
                     actor_id, self._now()),
                )
                for parent_id in parent_ids:
                    connection.execute(
                        "INSERT INTO rl_batch_parents(batch_id,parent_batch_id) VALUES(?,?)",
                        (batch_id, parent_id),
                    )
                for mid in all_materials:
                    connection.execute(
                        "INSERT INTO rl_batch_materials(batch_id,material_lot_id) VALUES(?,?)",
                        (batch_id, mid),
                    )
                # 三段复核随批次建档即处于未结状态，并随数据库持久化。
                for stage in ("production", "laboratory", "release"):
                    connection.execute(
                        "INSERT INTO rl_reviews(review_id,batch_id,stage,status,opened_by,opened_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, batch_id, stage, "open", actor_id, self._now()),
                    )
                self._audit(connection, actor_id=actor_id, action="release.batch_registered",
                            resource_type="batch", resource_id=batch_id,
                            detail={"site_id": site_id, "line_id": line_id,
                                    "product_code": product_code,
                                    "standard": f"{standard_id}:{standard_version}",
                                    "relation": relation, "parent_batch_ids": parent_ids,
                                    "material_lot_ids": all_materials,
                                    "production_start": start_s,
                                    "production_end": end_s})
                return "batch", batch_id, {"batch_id": batch_id, "relation": relation,
                                           "parent_batch_ids": parent_ids,
                                           "material_lot_ids": all_materials}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.register_batch", payload=body, create=create)

    def _batch(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM rl_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = self._batch(connection, batch_id)
        parents = [r["parent_batch_id"] for r in connection.execute(
            "SELECT parent_batch_id FROM rl_batch_parents WHERE batch_id=? ORDER BY parent_batch_id",
            (batch_id,))]
        children = [r["batch_id"] for r in connection.execute(
            "SELECT batch_id FROM rl_batch_parents WHERE parent_batch_id=? ORDER BY batch_id",
            (batch_id,))]
        materials = [r["material_lot_id"] for r in connection.execute(
            "SELECT material_lot_id FROM rl_batch_materials WHERE batch_id=? ORDER BY material_lot_id",
            (batch_id,))]
        return {"batch_id": row["batch_id"], "site_id": row["site_id"], "line_id": row["line_id"],
                "product_code": row["product_code"], "standard_id": row["standard_id"],
                "standard_version": row["standard_version"],
                "production_start": row["production_start"], "production_end": row["production_end"],
                "package_codes": json.loads(row["package_codes_json"]),
                "region_codes": json.loads(row["region_codes_json"]),
                "quantity": row["quantity"], "relation": row["parent_relation"],
                "parent_batch_ids": parents, "child_batch_ids": children,
                "material_lot_ids": materials, "payload": json.loads(row["payload_json"]),
                "registered_by": row["registered_by"], "created_at": row["created_at"]}

    def lineage(self, batch_id: str) -> dict[str, Any]:
        """返回批次的祖先与后代谱系（递归）。"""
        connection = self.database.connection
        if connection.execute("SELECT 1 FROM rl_batches WHERE batch_id=?", (batch_id,)).fetchone() is None:
            raise NotFoundError("批次不存在")

        def walk(recursive_sql: str) -> list[dict[str, str]]:
            rows = connection.execute(recursive_sql, (batch_id,)).fetchall()
            return [{"batch_id": r["batch_id"], "related_batch_id": r["related_batch_id"],
                     "relation": r["relation"], "depth": r["depth"]} for r in rows]

        ancestors = walk(
            "WITH RECURSIVE tree(batch_id, related_batch_id, relation, depth) AS ("
            "SELECT batch_id, parent_batch_id, 'parent', 1 FROM rl_batch_parents WHERE batch_id=? "
            "UNION ALL "
            "SELECT t.related_batch_id, p.parent_batch_id, 'parent', t.depth+1 "
            "FROM tree t JOIN rl_batch_parents p ON p.batch_id=t.related_batch_id"
            ") SELECT * FROM tree"
        )
        descendants = walk(
            "WITH RECURSIVE tree(batch_id, related_batch_id, relation, depth) AS ("
            "SELECT parent_batch_id, batch_id, 'child', 1 FROM rl_batch_parents WHERE parent_batch_id=? "
            "UNION ALL "
            "SELECT t.related_batch_id, c.batch_id, 'child', t.depth+1 "
            "FROM tree t JOIN rl_batch_parents c ON c.parent_batch_id=t.related_batch_id"
            ") SELECT * FROM tree"
        )
        return {"batch_id": batch_id, "ancestors": ancestors, "descendants": descendants}

    # -------------------------------------------------------------- 实验室

    def record_lab_result(self, *, request_id: str, actor_id: str, batch_id: str, test_code: str,
                          outcome: str, method_code: str, tested_at: str, tested_by: str,
                          measured_value: str | None = None, import_key: str | None = None) -> dict[str, Any]:
        if outcome not in ("pass", "fail"):
            raise ValidationError("outcome 必须是 pass 或 fail")
        if not all(isinstance(v, str) and v.strip() for v in (test_code, method_code, tested_by)):
            raise ValidationError("test_code/method_code/tested_by 必须是非空字符串")
        tested = _parse_dt(tested_at, "tested_at")
        tested_s = _norm_dt(tested_at, "tested_at")
        value = str(measured_value).strip() if measured_value is not None else None
        body = {"actor_id": actor_id, "batch_id": batch_id, "test_code": test_code,
                "outcome": outcome, "method_code": method_code, "tested_at": tested_s,
                "tested_by": tested_by, "measured_value": value, "import_key": import_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            batch = self._batch(connection, batch_id)
            standard = connection.execute(
                "SELECT payload_json FROM rl_brand_standards WHERE standard_id=? AND version=?",
                (batch["standard_id"], batch["standard_version"]),
            ).fetchone()
            specs = json.loads(standard["payload_json"]).get("tests", {})
            spec = specs.get(test_code)
            if spec is not None and spec.get("method_code") != method_code:
                raise ValidationError(f"检验项目 {test_code} 的方法与品牌标准不一致")
            # 数值型结果必须与标准限值判定一致，防止实验室结论与实测值互相矛盾。
            if spec is not None and value is not None:
                try:
                    numeric = float(value)
                except ValueError:
                    numeric = None
                if numeric is not None:
                    expected = True
                    if "min" in spec and numeric < spec["min"]:
                        expected = False
                    if "max" in spec and numeric > spec["max"]:
                        expected = False
                    if expected != (outcome == "pass"):
                        raise ValidationError(f"检验项目 {test_code} 的实测值与 pass/fail 结论矛盾")
            existing = connection.execute(
                "SELECT * FROM rl_lab_results WHERE batch_id=? AND test_code=? AND tested_by=? "
                "AND tested_at=? AND method_code=?",
                (batch_id, test_code, tested_by, tested_s, method_code),
            ).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                if existing:
                    if existing["outcome"] != outcome or existing["measured_value"] != value:
                        raise ConflictError("同一次检验已经登记不同结果，检验记录不可改写")
                    return "lab_result", existing["result_id"], {"result_id": existing["result_id"],
                                                                 "replayed_existing": True}
                result_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rl_lab_results(result_id,batch_id,test_code,outcome,measured_value,"
                    "method_code,tested_at,tested_by,payload_json,import_key,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (result_id, batch_id, test_code, outcome, value, method_code,
                     tested_s, tested_by, canonical_json({"value": value}),
                     import_key, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="release.lab_result_recorded",
                            resource_type="lab_result", resource_id=result_id,
                            detail={"batch_id": batch_id, "test_code": test_code,
                                    "outcome": outcome, "method_code": method_code,
                                    "tested_at": tested_s, "tested_by": tested_by,
                                    "import_key": import_key})
                return "lab_result", result_id, {"result_id": result_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.record_lab_result", payload=body, create=create)

    def list_lab_results(self, batch_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._batch(connection, batch_id)
        rows = connection.execute(
            "SELECT * FROM rl_lab_results WHERE batch_id=? ORDER BY tested_at, result_id",
            (batch_id,),
        ).fetchall()
        return [{"result_id": r["result_id"], "batch_id": r["batch_id"],
                 "test_code": r["test_code"], "outcome": r["outcome"],
                 "measured_value": r["measured_value"], "method_code": r["method_code"],
                 "tested_at": r["tested_at"], "tested_by": r["tested_by"],
                 "import_key": r["import_key"], "created_at": r["created_at"]} for r in rows]

    def _latest_lab_outcomes(self, connection, batch_id: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM rl_lab_results WHERE batch_id=? ORDER BY tested_at, result_id",
            (batch_id,),
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            latest[r["test_code"]] = {"outcome": r["outcome"], "method_code": r["method_code"],
                                      "tested_at": r["tested_at"], "result_id": r["result_id"]}
        return latest

    # ---------------------------------------------------------------- 复核

    def complete_review(self, *, request_id: str, actor_id: str, batch_id: str,
                        stage: str, notes: str = "") -> dict[str, Any]:
        if stage not in ("production", "laboratory", "release"):
            raise ValidationError("stage 必须是 production/laboratory/release")
        body = {"actor_id": actor_id, "batch_id": batch_id, "stage": stage, "notes": notes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._batch(connection, batch_id)
            if stage == "production":
                self._require(actor, "admin", "operator", "quality_lead")
            else:
                self._require(actor, "admin", "reviewer", "quality_lead")
            review = connection.execute(
                "SELECT * FROM rl_reviews WHERE batch_id=? AND stage=?", (batch_id, stage)
            ).fetchone()
            if review is None:
                raise NotFoundError("复核记录不存在")
            if review["status"] == "completed":
                return {"request_id": request_id, "resource_type": "review",
                        "resource_id": review["review_id"], "replayed": True,
                        "already_completed": True}
            if stage == "laboratory":
                latest = self._latest_lab_outcomes(connection, batch_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if stage == "laboratory":
                    if not latest:
                        raise ConflictError("尚无实验室结果，不能结束实验室复核")
                    failing = [code for code, item in latest.items() if item["outcome"] == "fail"]
                    if failing:
                        raise ConflictError(f"检验项目仍为不合格，不能结束实验室复核: {sorted(failing)}")
                connection.execute(
                    "UPDATE rl_reviews SET status='completed', completed_by=?, completed_at=?, notes=? "
                    "WHERE review_id=?",
                    (actor_id, self._now(), notes.strip(), review["review_id"]),
                )
                self._audit(connection, actor_id=actor_id, action="release.review_completed",
                            resource_type="review", resource_id=review["review_id"],
                            detail={"batch_id": batch_id, "stage": stage, "notes": notes.strip()})
                return "review", review["review_id"], {"review_id": review["review_id"],
                                                       "batch_id": batch_id, "stage": stage}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.complete_review", payload=body, create=create)

    def list_reviews(self, batch_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._batch(connection, batch_id)
        rows = connection.execute(
            "SELECT * FROM rl_reviews WHERE batch_id=? ORDER BY stage", (batch_id,)
        ).fetchall()
        return [self._review_dict(r) for r in rows]

    @staticmethod
    def _review_dict(row) -> dict[str, Any]:
        return {"review_id": row["review_id"], "batch_id": row["batch_id"], "stage": row["stage"],
                "status": row["status"], "opened_by": row["opened_by"], "opened_at": row["opened_at"],
                "completed_by": row["completed_by"], "completed_at": row["completed_at"],
                "notes": row["notes"]}

    def list_open_reviews(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """列出所有未结复核，服务重启后用它继续未完成工作。"""
        connection = self.database.connection
        sql = ("SELECT r.* FROM rl_reviews r JOIN rl_batches b ON b.batch_id=r.batch_id "
               "WHERE r.status='open'")
        parameters: list[Any] = []
        if site_id:
            sql += " AND b.site_id=?"
            parameters.append(site_id)
        sql += " ORDER BY r.opened_at, r.batch_id, r.stage"
        return [self._review_dict(r) for r in connection.execute(sql, parameters).fetchall()]

    # ----------------------------------------------------------- 偏差限制

    def raise_restriction(self, *, request_id: str, actor_id: str, batch_id: str, scope_type: str,
                          scope_values: list[str] | None, reason: str,
                          evidence_ref: str) -> dict[str, Any]:
        if scope_type not in ("batch", "package", "region"):
            raise ValidationError("scope_type 必须是 batch/package/region")
        values = _string_list(scope_values or [], "scope_values") if scope_type != "batch" else []
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason 不能为空")
        body = {"actor_id": actor_id, "batch_id": batch_id, "scope_type": scope_type,
                "scope_values": values, "reason": reason, "evidence_ref": evidence_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer", "quality_lead")
            batch = self._batch(connection, batch_id)
            allowed = json.loads(batch["package_codes_json" if scope_type == "package"
                                       else "region_codes_json"]) if scope_type != "batch" else []
            unknown = sorted(set(values) - set(allowed))
            if unknown:
                raise ValidationError(f"限制范围超出批次现有取值: {unknown}")
            self._evidence(connection, evidence_ref)

            def create() -> tuple[str, str, dict[str, Any]]:
                duplicate = connection.execute(
                    "SELECT 1 FROM rl_restrictions WHERE batch_id=? AND scope_type=? AND status='active' "
                    "AND scope_values_json=? AND reason=?",
                    (batch_id, scope_type, canonical_json(values), reason.strip()),
                ).fetchone()
                if duplicate:
                    raise ConflictError("相同范围与原因的偏差限制已经处于生效中")
                restriction_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rl_restrictions(restriction_id,batch_id,scope_type,scope_values_json,"
                    "reason,evidence_ref,raised_by,raised_at,status) VALUES(?,?,?,?,?,?,?,?, 'active')",
                    (restriction_id, batch_id, scope_type, canonical_json(values),
                     reason.strip(), evidence_ref, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="release.restriction_raised",
                            resource_type="restriction", resource_id=restriction_id,
                            detail={"batch_id": batch_id, "scope_type": scope_type,
                                    "scope_values": values, "reason": reason.strip(),
                                    "evidence_ref": evidence_ref})
                return "restriction", restriction_id, {"restriction_id": restriction_id,
                                                       "batch_id": batch_id, "status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.raise_restriction", payload=body, create=create)

    def lift_restriction(self, *, request_id: str, actor_id: str, restriction_id: str,
                         evidence_ref: str, review_id: str | None = None,
                         notes: str = "") -> dict[str, Any]:
        body = {"actor_id": actor_id, "restriction_id": restriction_id,
                "evidence_ref": evidence_ref, "review_id": review_id, "notes": notes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *LIFT_ROLES)
            row = connection.execute(
                "SELECT * FROM rl_restrictions WHERE restriction_id=?", (restriction_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("偏差限制不存在")
            resolved_ref = self._evidence(connection, evidence_ref)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "lifted":
                    raise ConflictError("偏差限制已经解除，不能重复解除")
                # 四眼原则：解除人不得是挂接人。
                if row["raised_by"] == actor_id:
                    raise PermissionDenied("解除限制必须由挂接人之外的另一名授权者执行")
                if review_id is not None:
                    review = connection.execute(
                        "SELECT * FROM rl_reviews WHERE review_id=?", (review_id,)
                    ).fetchone()
                    if review is None:
                        raise NotFoundError("引用的复核记录不存在")
                    if review["status"] != "completed":
                        raise ConflictError("引用的复核尚未完成")
                connection.execute(
                    "UPDATE rl_restrictions SET status='lifted', lifted_by=?, lifted_at=?, "
                    "lift_evidence_ref=?, lift_review_id=?, lift_notes=? WHERE restriction_id=?",
                    (actor_id, self._now(), resolved_ref, review_id, notes.strip(), restriction_id),
                )
                self._audit(connection, actor_id=actor_id, action="release.restriction_lifted",
                            resource_type="restriction", resource_id=restriction_id,
                            detail={"batch_id": row["batch_id"], "raised_by": row["raised_by"],
                                    "lifted_by": actor_id, "evidence_ref": resolved_ref,
                                    "review_id": review_id, "notes": notes.strip()})
                return "restriction", restriction_id, {"restriction_id": restriction_id,
                                                       "status": "lifted"}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.lift_restriction", payload=body, create=create)

    def list_restrictions(self, batch_id: str, active_only: bool = False) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._batch(connection, batch_id)
        sql = "SELECT * FROM rl_restrictions WHERE batch_id=?"
        if active_only:
            sql += " AND status='active'"
        sql += " ORDER BY raised_at, restriction_id"
        return [self._restriction_dict(r) for r in connection.execute(sql, (batch_id,)).fetchall()]

    @staticmethod
    def _restriction_dict(r) -> dict[str, Any]:
        return {"restriction_id": r["restriction_id"], "batch_id": r["batch_id"],
                "scope_type": r["scope_type"], "scope_values": json.loads(r["scope_values_json"]),
                "reason": r["reason"], "evidence_ref": r["evidence_ref"],
                "raised_by": r["raised_by"], "raised_at": r["raised_at"], "status": r["status"],
                "lifted_by": r["lifted_by"], "lifted_at": r["lifted_at"],
                "lift_evidence_ref": r["lift_evidence_ref"], "lift_review_id": r["lift_review_id"],
                "lift_notes": r["lift_notes"]}

    def _active_restrictions(self, connection, batch_id: str) -> list:
        return connection.execute(
            "SELECT * FROM rl_restrictions WHERE batch_id=? AND status='active'",
            (batch_id,),
        ).fetchall()

    @staticmethod
    def _restriction_covers(restriction, package: str, region: str) -> bool:
        if restriction["scope_type"] == "batch":
            return True
        values = json.loads(restriction["scope_values_json"])
        if restriction["scope_type"] == "package":
            return package in values
        return region in values

    # ----------------------------------------------------------- 放行决定

    def _release_blockers(self, connection, batch, scope_packages: list[str],
                          scope_regions: list[str]) -> list[str]:
        """计算在给定范围放行尚缺的条件。"""
        batch_id = batch["batch_id"]
        blockers: list[str] = []
        reviews = {r["stage"]: r for r in connection.execute(
            "SELECT * FROM rl_reviews WHERE batch_id=?", (batch_id,))}
        for stage in ("production", "laboratory"):
            if reviews[stage]["status"] != "completed":
                blockers.append(f"review:{stage}_open")
        standard = connection.execute(
            "SELECT * FROM rl_brand_standards WHERE standard_id=? AND version=?",
            (batch["standard_id"], batch["standard_version"]),
        ).fetchone()
        spec_payload = json.loads(standard["payload_json"])
        latest = self._latest_lab_outcomes(connection, batch_id)
        for code, spec in spec_payload.get("tests", {}).items():
            item = latest.get(code)
            if item is None:
                blockers.append(f"lab_missing:{code}")
            elif item["outcome"] != "pass":
                blockers.append(f"lab_failed:{code}")
            elif item["method_code"] != spec["method_code"]:
                blockers.append(f"lab_method_mismatch:{code}")
        for code, item in latest.items():
            if item["outcome"] == "fail" and f"lab_failed:{code}" not in blockers:
                blockers.append(f"lab_failed:{code}")
        required_equipment: list[str] = []
        for equipment in spec_payload.get("equipment", []):
            if isinstance(equipment, str):
                required_equipment.append(equipment)
            elif isinstance(equipment, dict) and equipment.get("site_id") == batch["site_id"]:
                code = equipment.get("equipment_code")
                if isinstance(code, str) and code.strip():
                    required_equipment.append(code.strip())
        for equipment in required_equipment:
            calibration = connection.execute(
                "SELECT 1 FROM rl_calibrations WHERE site_id=? AND equipment_code=? "
                "AND valid_from<=? AND valid_until>=? LIMIT 1",
                (batch["site_id"], equipment, batch["production_start"], batch["production_end"]),
            ).fetchone()
            if calibration is None:
                blockers.append(f"calibration_missing:{equipment}")
        quarantined = [r["material_lot_id"] for r in connection.execute(
            "SELECT m.material_lot_id FROM rl_material_lots m "
            "JOIN rl_batch_materials bm ON bm.material_lot_id=m.material_lot_id "
            "WHERE bm.batch_id=? AND m.quarantined=1",
            (batch_id,),
        ).fetchall()]
        if quarantined:
            blockers.append("materials_quarantined:" + ",".join(sorted(quarantined)))
        for restriction in self._active_restrictions(connection, batch_id):
            values = json.loads(restriction["scope_values_json"])
            hit = False
            if restriction["scope_type"] == "batch":
                hit = True
            elif restriction["scope_type"] == "package":
                hit = bool(set(values) & set(scope_packages))
            else:
                hit = bool(set(values) & set(scope_regions))
            if hit:
                blockers.append(f"active_restriction:{restriction['restriction_id']}")
        return blockers

    def _scope_values(self, batch, scope_type: str, scope_values: list[str] | None) -> list[str]:
        if scope_type == "batch":
            return []
        column = "package_codes_json" if scope_type == "package" else "region_codes_json"
        allowed = json.loads(batch[column])
        values = _string_list(scope_values or [], "scope_values")
        unknown = sorted(set(values) - set(allowed))
        if unknown:
            raise ValidationError(f"决定范围超出批次现有取值: {unknown}")
        return values

    def _covering_decisions(self, connection, batch_id: str) -> list:
        return connection.execute(
            "SELECT * FROM rl_decisions WHERE batch_id=? ORDER BY decided_at, rowid",
            (batch_id,),
        ).fetchall()

    def _decision_covers(self, decision_row, package: str, region: str) -> bool:
        if decision_row["scope_type"] == "batch":
            return True
        values = json.loads(decision_row["scope_values_json"])
        if decision_row["scope_type"] == "package":
            return package in values
        return region in values

    def _prior_matrix(self, decisions, packages: list[str], regions: list[str]) -> dict[str, str]:
        matrix: dict[str, str] = {}
        for package in packages:
            for region in regions:
                state = "held"
                for decision in decisions:
                    if self._decision_covers(decision, package, region):
                        state = decision["decision"]
                matrix[f"{package}|{region}"] = state
        return matrix

    def create_decision(self, *, request_id: str, actor_id: str, batch_id: str, decision: str,
                        scope_type: str = "batch", scope_values: list[str] | None = None,
                        reason: str, evidence_ref: str) -> dict[str, Any]:
        if decision not in ("release", "freeze", "recall"):
            raise ValidationError("decision 必须是 release/freeze/recall")
        if scope_type not in ("batch", "package", "region"):
            raise ValidationError("scope_type 必须是 batch/package/region")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason 不能为空")
        body = {"actor_id": actor_id, "batch_id": batch_id, "decision": decision,
                "scope_type": scope_type, "scope_values": scope_values or [],
                "reason": reason, "evidence_ref": evidence_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DECISION_ROLES)
            batch = self._batch(connection, batch_id)
            values = self._scope_values(batch, scope_type, scope_values)
            packages = json.loads(batch["package_codes_json"])
            regions = json.loads(batch["region_codes_json"])
            scope_packages = values if scope_type == "package" else packages
            scope_regions = values if scope_type == "region" else regions
            self._evidence(connection, evidence_ref)
            decisions = self._covering_decisions(connection, batch_id)
            prior_matrix = self._prior_matrix(decisions, packages, regions)
            scope_json = canonical_json(values)

            def create() -> tuple[str, str, dict[str, Any]]:
                affected_cells = {f"{p}|{r}": prior_matrix[f"{p}|{r}"]
                                  for p in scope_packages for r in scope_regions}
                current_states = set(affected_cells.values())
                if decision == "release":
                    blockers = self._release_blockers(connection, batch, scope_packages, scope_regions)
                    if blockers:
                        raise ConflictError("放行条件未满足: " + "; ".join(sorted(blockers)))
                    if current_states == {"release"}:
                        raise ConflictError("该范围已经处于放行状态，重复放行不会产生新决定")
                elif decision == "recall":
                    shipped = connection.execute(
                        "SELECT COUNT(*) AS c FROM rl_shipments WHERE batch_id=?", (batch_id,)
                    ).fetchone()["c"]
                    if current_states == {"held"} and not shipped:
                        raise ConflictError("尚未放行或出库的范围应使用冻结而非召回")
                previous = None
                for candidate in reversed(decisions):
                    if candidate["scope_type"] == scope_type and \
                            candidate["scope_values_json"] == scope_json:
                        previous = candidate["decision_id"]
                        break
                standard = connection.execute(
                    "SELECT * FROM rl_brand_standards WHERE standard_id=? AND version=?",
                    (batch["standard_id"], batch["standard_version"]),
                ).fetchone()
                evidence_snapshot = self._evidence_snapshot(connection, batch)
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rl_decisions(decision_id,batch_id,decision,scope_type,"
                    "scope_values_json,reason,evidence_ref,decided_by,decided_at,"
                    "standard_snapshot_json,evidence_snapshot_json,supersedes_decision_id,"
                    "prior_state_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, batch_id, decision, scope_type, scope_json, reason.strip(),
                     evidence_ref, actor_id, self._now(), standard["payload_json"],
                     canonical_json(evidence_snapshot), previous,
                     canonical_json(affected_cells)),
                )
                if decision == "release":
                    # 放行决定同时收尾放行复核，保留决定人与时间。
                    connection.execute(
                        "UPDATE rl_reviews SET status='completed', completed_by=?, completed_at=?, "
                        "notes=COALESCE(NULLIF(notes,''),?) WHERE batch_id=? AND stage='release' "
                        "AND status='open'",
                        (actor_id, self._now(), f"随放行决定 {decision_id} 完成", batch_id),
                    )
                self._audit(connection, actor_id=actor_id, action="release.decision_created",
                            resource_type="decision", resource_id=decision_id,
                            detail={"batch_id": batch_id, "decision": decision,
                                    "scope_type": scope_type, "scope_values": values,
                                    "reason": reason.strip(), "evidence_ref": evidence_ref,
                                    "supersedes": previous,
                                    "prior_states": affected_cells})
                return "decision", decision_id, {"decision_id": decision_id, "batch_id": batch_id,
                                                 "decision": decision, "scope_type": scope_type,
                                                 "scope_values": values}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.create_decision", payload=body, create=create)

    def _evidence_snapshot(self, connection, batch) -> dict[str, Any]:
        """组装放行时点的完整证据快照（随决定永久保存）。"""
        batch_id = batch["batch_id"]
        reviews = [self._review_dict(r) for r in connection.execute(
            "SELECT * FROM rl_reviews WHERE batch_id=? ORDER BY stage", (batch_id,))]
        lab = [{"result_id": r["result_id"], "test_code": r["test_code"], "outcome": r["outcome"],
                "measured_value": r["measured_value"], "method_code": r["method_code"],
                "tested_at": r["tested_at"], "tested_by": r["tested_by"]}
               for r in connection.execute(
                   "SELECT * FROM rl_lab_results WHERE batch_id=? ORDER BY tested_at, result_id",
                   (batch_id,))]
        materials = [{"material_lot_id": r["material_lot_id"], "kind": r["kind"],
                      "material_code": r["material_code"],
                      "quarantined": bool(r["quarantined"])}
                     for r in connection.execute(
                         "SELECT m.* FROM rl_material_lots m JOIN rl_batch_materials bm "
                         "ON bm.material_lot_id=m.material_lot_id WHERE bm.batch_id=?",
                         (batch_id,))]
        calibrations = [{"calibration_id": r["calibration_id"],
                         "equipment_code": r["equipment_code"],
                         "valid_from": r["valid_from"], "valid_until": r["valid_until"]}
                        for r in connection.execute(
                            "SELECT c.* FROM rl_calibrations c WHERE c.site_id=?",
                            (batch["site_id"],))]
        restrictions = [self._restriction_dict(r) for r in self._active_restrictions(connection, batch_id)]
        parents = [{"batch_id": r["parent_batch_id"],
                    "relation": connection.execute(
                        "SELECT parent_relation FROM rl_batches WHERE batch_id=?",
                        (r["parent_batch_id"],)).fetchone()["parent_relation"]}
                   for r in connection.execute(
                       "SELECT parent_batch_id FROM rl_batch_parents WHERE batch_id=? "
                       "ORDER BY parent_batch_id", (batch_id,))]
        return {"reviews": reviews, "lab_results": lab, "materials": materials,
                "calibrations": calibrations, "active_restrictions": restrictions,
                "parents": parents}

    def list_decisions(self, batch_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._batch(connection, batch_id)
        rows = connection.execute(
            "SELECT decision_id,batch_id,decision,scope_type,scope_values_json,reason,evidence_ref,"
            "decided_by,decided_at,supersedes_decision_id,prior_state_json "
            "FROM rl_decisions WHERE batch_id=? ORDER BY decided_at, rowid",
            (batch_id,),
        ).fetchall()
        return [{"decision_id": r["decision_id"], "batch_id": r["batch_id"],
                 "decision": r["decision"], "scope_type": r["scope_type"],
                 "scope_values": json.loads(r["scope_values_json"]), "reason": r["reason"],
                 "evidence_ref": r["evidence_ref"], "decided_by": r["decided_by"],
                 "decided_at": r["decided_at"], "supersedes_decision_id": r["supersedes_decision_id"],
                 "prior_state": json.loads(r["prior_state_json"])} for r in rows]

    def effective_status(self, batch_id: str) -> dict[str, Any]:
        """计算批次当前按包装×区域的有效状态（默认 held，不可销售）。"""
        connection = self.database.connection
        batch = self._batch(connection, batch_id)
        packages = json.loads(batch["package_codes_json"])
        regions = json.loads(batch["region_codes_json"])
        decisions = self._covering_decisions(connection, batch_id)
        matrix = self._prior_matrix(decisions, packages, regions)
        active_restrictions = self._active_restrictions(connection, batch_id)
        cells = []
        for package in packages:
            for region in regions:
                restricted = any(self._restriction_covers(r, package, region)
                                 for r in active_restrictions)
                state = matrix[f"{package}|{region}"]
                cells.append({"package_code": package, "region_code": region, "state": state,
                              # 可销售必须同时满足：已放行且没有生效中的偏差限制。
                              "saleable": state == "release" and not restricted,
                              "restricted": restricted})
        blocked_cells = [f"{c['package_code']}|{c['region_code']}" for c in cells
                         if c["restricted"]]
        return {"batch_id": batch_id, "cells": cells,
                "active_restrictions": [self._restriction_dict(r) for r in active_restrictions],
                "restriction_blocked_cells": blocked_cells}

    # ---------------------------------------------------------------- 出库

    def register_shipment(self, *, request_id: str, actor_id: str, shipment_id: str,
                          batch_id: str, package_code: str, region_code: str, quantity: int,
                          shipped_at: str) -> dict[str, Any]:
        shipped = _parse_dt(shipped_at, "shipped_at")
        shipped_s = _norm_dt(shipped_at, "shipped_at")
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValidationError("quantity 必须是正整数")
        body = {"actor_id": actor_id, "shipment_id": shipment_id, "batch_id": batch_id,
                "package_code": package_code, "region_code": region_code,
                "quantity": quantity, "shipped_at": shipped_s}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch(connection, batch_id)
            packages = json.loads(batch["package_codes_json"])
            regions = json.loads(batch["region_codes_json"])
            if package_code not in packages:
                raise ValidationError("包装代码不属于该批次")
            if region_code not in regions:
                raise ValidationError("销售区域不属于该批次")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM rl_shipments WHERE shipment_id=?",
                                      (shipment_id,)).fetchone():
                    raise ConflictError("出库单号已经存在")
                decisions = self._covering_decisions(connection, batch_id)
                matrix = self._prior_matrix(decisions, packages, regions)
                state = matrix[f"{package_code}|{region_code}"]
                if state != "release":
                    raise ConflictError(f"该包装/区域当前状态为 {state}，不能出库")
                covering = [d for d in reversed(decisions)
                            if self._decision_covers(d, package_code, region_code)]
                releasing = next(d for d in covering if d["decision"] == "release")
                for restriction in self._active_restrictions(connection, batch_id):
                    if self._restriction_covers(restriction, package_code, region_code):
                        raise ConflictError("该包装/区域存在生效中的偏差限制，不能出库")
                snapshot = {"decision_id": releasing["decision_id"],
                            "decision": releasing["decision"],
                            "scope_type": releasing["scope_type"],
                            "scope_values": json.loads(releasing["scope_values_json"]),
                            "decided_by": releasing["decided_by"],
                            "decided_at": releasing["decided_at"],
                            "reason": releasing["reason"],
                            "evidence_ref": releasing["evidence_ref"],
                            "standard_id": batch["standard_id"],
                            "standard_version": batch["standard_version"]}
                connection.execute(
                    "INSERT INTO rl_shipments(shipment_id,batch_id,package_code,region_code,"
                    "quantity,shipped_at,registered_by,decision_id,decision_snapshot_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (shipment_id, batch_id, package_code, region_code, quantity,
                     shipped_s, actor_id, releasing["decision_id"],
                     canonical_json(snapshot), self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="release.shipment_registered",
                            resource_type="shipment", resource_id=shipment_id,
                            detail={"batch_id": batch_id, "package_code": package_code,
                                    "region_code": region_code, "quantity": quantity,
                                    "shipped_at": shipped_s,
                                    "decision_id": releasing["decision_id"]})
                return "shipment", shipment_id, {"shipment_id": shipment_id,
                                                 "decision_id": releasing["decision_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="release.register_shipment", payload=body, create=create)

    def list_shipments(self, batch_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._batch(connection, batch_id)
        rows = connection.execute(
            "SELECT * FROM rl_shipments WHERE batch_id=? ORDER BY shipped_at, shipment_id",
            (batch_id,),
        ).fetchall()
        return [{"shipment_id": r["shipment_id"], "batch_id": r["batch_id"],
                 "package_code": r["package_code"], "region_code": r["region_code"],
                 "quantity": r["quantity"], "shipped_at": r["shipped_at"],
                 "registered_by": r["registered_by"], "decision_id": r["decision_id"],
                 "decision_snapshot": json.loads(r["decision_snapshot_json"]),
                 "created_at": r["created_at"]} for r in rows]

    # ------------------------------------------------------------ 全链还原

    def explain(self, batch_id: str) -> dict[str, Any]:
        """还原任一批次为何放行、冻结或召回的完整证据链。"""
        connection = self.database.connection
        batch_dict = self.get_batch(batch_id)
        standard = self.get_brand_standard(batch_dict["standard_id"],
                                           batch_dict["standard_version"])
        reviews = self.list_reviews(batch_id)
        lab = self.list_lab_results(batch_id)
        restrictions = self.list_restrictions(batch_id)
        decisions = self.list_decisions(batch_id)
        shipments = self.list_shipments(batch_id)
        effective = self.effective_status(batch_id)
        materials = [self.get_material_lot(mid) for mid in batch_dict["material_lot_ids"]]
        calibrations = [{"calibration_id": r["calibration_id"], "site_id": r["site_id"],
                         "equipment_code": r["equipment_code"], "valid_from": r["valid_from"],
                         "valid_until": r["valid_until"]}
                        for r in connection.execute(
                            "SELECT * FROM rl_calibrations WHERE site_id=? ORDER BY equipment_code",
                            (batch_dict["site_id"],))]
        decision_rows = self._covering_decisions(connection, batch_id)
        decision_snapshots = {r["decision_id"]: {
            "standard_snapshot": json.loads(r["standard_snapshot_json"]),
            "evidence_snapshot": json.loads(r["evidence_snapshot_json"]),
        } for r in decision_rows}
        events = [{"sequence": r["sequence"], "event_id": r["event_id"], "actor_id": r["actor_id"],
                   "action": r["action"], "resource_type": r["resource_type"],
                   "resource_id": r["resource_id"], "detail": json.loads(r["detail_json"]),
                   "occurred_at": r["occurred_at"]}
                  for r in connection.execute(
                      "SELECT * FROM audit_events WHERE resource_id=? "
                      "OR detail_json LIKE ? ORDER BY sequence",
                      (batch_id, f'%"batch_id":"{batch_id}"%'))]
        return {"batch": batch_dict, "lineage": self.lineage(batch_id),
                "brand_standard": {"standard_id": standard["standard_id"],
                                   "version": standard["version"],
                                   "payload": standard["payload"],
                                   "payload_hash": standard["payload_hash"],
                                   "registered_at": standard["created_at"]},
                "materials": materials, "calibrations": calibrations,
                "lab_results": lab, "reviews": reviews, "restrictions": restrictions,
                "decisions": decisions, "decision_snapshots": decision_snapshots,
                "shipments": shipments, "effective_status": effective,
                "audit_events": events}

    def pending_batches(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """列出仍有未结复核的批次，供重启后继续工作。"""
        connection = self.database.connection
        sql = ("SELECT b.batch_id, b.site_id, b.product_code, "
                "GROUP_CONCAT(r.stage) AS open_stages, MIN(r.opened_at) AS oldest "
                "FROM rl_batches b JOIN rl_reviews r ON r.batch_id=b.batch_id "
                "WHERE r.status='open'")
        parameters: list[Any] = []
        if site_id:
            sql += " AND b.site_id=?"
            parameters.append(site_id)
        sql += " GROUP BY b.batch_id ORDER BY oldest"
        result = []
        for row in connection.execute(sql, parameters):
            stages = sorted(row["open_stages"].split(","))
            result.append({"batch_id": row["batch_id"], "site_id": row["site_id"],
                           "product_code": row["product_code"], "open_stages": stages,
                           "oldest_open_at": row["oldest"]})
        return result
