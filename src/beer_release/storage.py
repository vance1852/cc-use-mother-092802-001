"""批次放行平台的 SQLite 表结构与连接管理。

基础库 :mod:`beverage_ops_foundation.storage` 提供 organizations、actors、
sites、audit_events 等共享表；这里只追加放行平台自身的表，两者共用同一数据库
与同一条哈希审计链。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from beverage_ops_foundation.storage import SCHEMA as FOUNDATION_SCHEMA

RELEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_standards (
    standard_id TEXT NOT NULL,
    version TEXT NOT NULL,
    brand TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (standard_id, version)
);
CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    lot_kind TEXT NOT NULL CHECK(lot_kind IN ('material','packaging')),
    name TEXT NOT NULL,
    supplier TEXT,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations (
    calibration_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    equipment_id TEXT NOT NULL,
    calibrated_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid','revoked')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    brand TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    standard_version TEXT NOT NULL,
    production_start TEXT NOT NULL,
    production_end TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('registered','released','frozen','recalled','disposed')),
    import_key TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, import_key)
);
CREATE TABLE IF NOT EXISTS batch_inputs (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    lot_id TEXT NOT NULL REFERENCES lots(lot_id),
    PRIMARY KEY (batch_id, lot_id)
);
CREATE TABLE IF NOT EXISTS batch_calibrations (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    calibration_id TEXT NOT NULL REFERENCES calibrations(calibration_id),
    PRIMARY KEY (batch_id, calibration_id)
);
CREATE TABLE IF NOT EXISTS lab_results (
    result_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    sample_code TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    tests_json TEXT NOT NULL,
    conforms INTEGER NOT NULL CHECK(conforms IN (0,1)),
    evaluation_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, sample_code)
);
CREATE TABLE IF NOT EXISTS lineage_links (
    link_id TEXT PRIMARY KEY,
    parent_batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    child_batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    relation TEXT NOT NULL CHECK(relation IN ('split','merge','rework')),
    quantity REAL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deviations (
    deviation_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    scope_json TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','dispositioned','closed')),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    disposition_summary TEXT,
    disposition_evidence_json TEXT NOT NULL DEFAULT '[]',
    dispositioned_by TEXT,
    dispositioned_at TEXT,
    closed_by TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS restrictions (
    restriction_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    deviation_id TEXT REFERENCES deviations(deviation_id),
    scope_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT,
    release_note TEXT,
    release_evidence_json TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    decision TEXT NOT NULL CHECK(decision IN ('release','freeze','recall','dispose')),
    scope_json TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    standard_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    shipment_id TEXT
);
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    kind TEXT NOT NULL CHECK(kind IN ('release_review','deviation_review')),
    deviation_id TEXT REFERENCES deviations(deviation_id),
    status TEXT NOT NULL CHECK(status IN ('open','completed','cancelled')),
    note TEXT,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    completed_by TEXT,
    completed_at TEXT,
    decision_id TEXT
);
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    scope_json TEXT NOT NULL,
    shipped_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_batch ON decisions(batch_id, decided_at, decision_id);
CREATE INDEX IF NOT EXISTS idx_links_parent ON lineage_links(parent_batch_id);
CREATE INDEX IF NOT EXISTS idx_links_child ON lineage_links(child_batch_id);
CREATE INDEX IF NOT EXISTS idx_restrictions_batch ON restrictions(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON review_tasks(status);
"""


class ReleaseDatabase:
    """管理同时包含基础表与放行平台表的 SQLite 数据库。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(FOUNDATION_SCHEMA)
        self.connection.executescript(RELEASE_SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
