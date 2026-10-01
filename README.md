# 跨工厂啤酒批次放行平台

本项目在酒类经营基础服务（经营主体、操作者、场所、结构化参考资料、角色权限、
请求幂等、SQLite 事务、哈希串联审计）之上，为高端啤酒品质团队提供覆盖
**广州 / 嘉善 / 厦门**等多个工厂的批次放行可信链。运行时仅使用 Python 标准库和 SQLite。

## 解决的问题

同一品牌规范在不同产线的取样、复检、偏差处置记录难以拼成可信放行链，市场催货时
容易把尚未完成复核的批次当成可销售库存。平台把以下证据关联到同一条链：

- **品牌标准版本**：标准按 `(standard_id, version)` 不可变登记；批次在导入时钉住
  具体版本，实验室结果按钉住版本评价，后续新版本**不会倒改**既有批次。
- **原料与包装批号、生产时段、设备校准、实验室结果**：全部作为放行证据，
  校准有效期必须覆盖整个生产时段。
- **有权人员决定**：放行/冻结/召回/处置只追加、不更新；谁在何时基于什么证据决定
  全部进入哈希审计链。

## 关键规则

- **谱系**：拆分（split）、合并（merge）、返工（rework）以谱系边表达，子批次继承
  母批次批号/校准证据；放行复核会递归检查祖先批次上的活动限制与未处置偏差。
- **重复导入幂等**：同导入键同内容原样重放，不新增决定、不改变隔离状态；
  同键不同内容冲突拒绝。实验室结果同样按样品编号幂等。
- **部分范围偏差**：偏差/限制/决定携带 `packaging` 与 `regions` 范围，只影响重叠的
  包装与销售区域；`explain` 按包装×区域单元重放时间线，给出 `released/frozen/
  recalled/disposed/mixed` 的当前有效状态，避免把冻结区域当可售库存。
- **四眼解除**：解除隔离限制必须由施加人/处置人之外的另一名授权者执行，并引用
  处置证据；关联偏差未完成处置或证据缺失时不能解除。
- **历史锁定**：决定只追加；出库（shipment）后该放行决定标记为历史锁定，后续规则、
  标准版本或校准变化都不改变它；召回通过追加 `recall` 决定表达。
- **重启续办**：未结复核任务持久化，服务重启后可列出并继续办理。
- **可还原**：质量负责人通过 API 或离线测试，用 `GET /batches/{id}/explain`
  还原任一批次为何放行、冻结或召回，返回完整证据、决定时间线与审计链校验结果。

## 目录

- `src/beverage_ops_foundation/`：基础服务（共享表、权限、幂等、审计链）。
- `src/beer_release/`：批次放行平台：
  - `storage.py`：放行平台表结构（与基础库共用同一数据库与审计链）；
  - `models.py`：数据对象；
  - `service.py`：标准版本、批号、校准、批次、实验室、谱系、偏差、限制、决定、
    复核、出库与证据还原；
  - `api.py`：HTTP/JSON 边界；
  - `acceptance.py`：跨三厂离线端到端验收。
- `tests/`：门禁、标准版本、部分偏差、四眼解除、谱系、幂等、重启持久化、
  HTTP 路由与端到端验收测试。

## 环境

- Linux，Python 3.11+，仅标准库与 SQLite。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beer_release.acceptance
```

成功时输出一行 `status` 为 `ok` 的 JSON（含三厂场景与审计链事件数），退出码 0。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beer_release.api --database beer_release.sqlite3 \
  --host 127.0.0.1 --port 8080
```

写入接口通过 `X-Actor-Id` 标识操作者，请求体携带业务 `request_id` 实现幂等。
主要端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/brand-standards` | 登记不可变品牌标准版本 |
| POST | `/lots`、`/calibrations` | 登记原料/包装批号、设备校准 |
| POST | `/calibrations/{id}/revoke` | 吊销校准（只影响今后判定） |
| POST | `/batches/import` | 导入批次（钉住标准版本，幂等） |
| POST | `/batches/{id}/lab-results` | 登记实验室结果（按钉住版本评价） |
| POST | `/batches/{id}/split`、`/batches/merge`、`/batches/rework` | 谱系操作 |
| GET | `/batches/{id}/lineage`、`/batches/{id}/explain` | 谱系与放行链还原 |
| POST | `/deviations`、`/deviations/{id}/disposition`、`/deviations/{id}/close` | 偏差生命周期 |
| POST | `/restrictions`、`/restrictions/{id}/release` | 施加隔离 / 四眼解除 |
| POST | `/decisions` | release/freeze/recall/dispose（追加） |
| POST | `/reviews`、`/reviews/{id}/complete`、`/reviews/{id}/cancel` | 复核（可重启续办） |
| POST | `/shipments` | 出库（锁定对应历史放行决定） |
| GET | `/batches`、`/reviews`、`/audit-events` | 查询 |
| GET | `/` | 健康检查与审计链状态 |
