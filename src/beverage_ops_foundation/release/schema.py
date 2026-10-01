"""跨工厂批次放行平台的 SQLite 表结构。

表均为只追加或带版本的台账式设计：批次限制、复核、决定永不物理删除，
历史决定引用的规则与证据快照也一并留存，后续规则变更无法倒改。
"""

from __future__ import annotations

RELEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rl_brand_standards (
    standard_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    PRIMARY KEY (standard_id, version)
);
CREATE TABLE IF NOT EXISTS rl_lines (
    line_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rl_material_lots (
    material_lot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('raw_material','packaging')),
    material_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    quarantined INTEGER NOT NULL CHECK(quarantined IN (0,1)),
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rl_material_imports (
    import_key TEXT NOT NULL,
    material_lot_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (import_key, material_lot_id)
);
CREATE TABLE IF NOT EXISTS rl_calibrations (
    calibration_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    equipment_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rl_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    product_code TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    standard_version INTEGER NOT NULL,
    production_start TEXT NOT NULL,
    production_end TEXT NOT NULL,
    package_codes_json TEXT NOT NULL,
    region_codes_json TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    parent_relation TEXT NOT NULL CHECK(parent_relation IN ('original','split','merge','rework')),
    payload_json TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rl_batch_parents (
    batch_id TEXT NOT NULL,
    parent_batch_id TEXT NOT NULL,
    PRIMARY KEY (batch_id, parent_batch_id)
);
CREATE TABLE IF NOT EXISTS rl_batch_materials (
    batch_id TEXT NOT NULL,
    material_lot_id TEXT NOT NULL,
    PRIMARY KEY (batch_id, material_lot_id)
);
CREATE TABLE IF NOT EXISTS rl_lab_results (
    result_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    test_code TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('pass','fail')),
    measured_value TEXT,
    method_code TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    tested_by TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    import_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, test_code, tested_by, tested_at, method_code)
);
CREATE TABLE IF NOT EXISTS rl_reviews (
    review_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('production','laboratory','release')),
    status TEXT NOT NULL CHECK(status IN ('open','completed')),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    completed_by TEXT,
    completed_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(batch_id, stage)
);
CREATE TABLE IF NOT EXISTS rl_restrictions (
    restriction_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('batch','package','region')),
    scope_values_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','lifted')),
    lifted_by TEXT,
    lifted_at TEXT,
    lift_evidence_ref TEXT,
    lift_review_id TEXT,
    lift_notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rl_decisions (
    decision_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('release','freeze','recall')),
    scope_type TEXT NOT NULL CHECK(scope_type IN ('batch','package','region')),
    scope_values_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    standard_snapshot_json TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    supersedes_decision_id TEXT,
    prior_state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rl_shipments (
    shipment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    package_code TEXT NOT NULL,
    region_code TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    shipped_at TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    decision_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
