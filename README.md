# 跨工厂批次放行平台

本项目面向高端啤酒事业群同时管理广州、嘉善、厦门等多个工厂的场景，在基础经营服务之上提供一条**可还原、不可倒改**的批次放行链：把品牌标准版本、原料与包装批号、生产时段、实验室结果、设备校准与有权人员的决定关联起来，使“尚未完成复核的批次”绝不会被当作可销售库存。

## 目录

- `src/beverage_ops_foundation/`：基础域（组织/人员/站点、幂等、SQLite 事务、哈希审计链）。
- `src/beverage_ops_foundation/release/`：批次放行域
  - `schema.py`：放行域表结构（标准版本、物料、校准、批次谱系、检验、复核、限制、决定、出库）。
  - `service.py`：全部放行业务规则。
  - `api.py`：放行域 HTTP/JSON 路由。
  - `acceptance.py`：三厂完整故事的离线验收。
- `tests/`：基础规则、放行规则、HTTP 路由与端到端验收测试。

## 核心规则

- **品牌标准版本化**：版本必须顺序递增，历史版本不可覆盖；每个放行/冻结/召回决定都固化当时的标准全文与证据快照。
- **批次谱系**：拆分（split）、合并（merge，至少两个父批次）、返工（rework）必须引用父批次并自动继承物料批号；谱系可双向递归查询。返工批次必须重新检验、重新复核后才能放行。
- **重复导入幂等**：物料批号、实验室结果、校准的重复导入只回放既有收据；同号不同内容被拒绝，**重复导入绝不会清除隔离/不合格状态**。
- **三段复核**：生产、实验室、放行复核随批次建档即持久化为未结状态；实验室复核在有缺检或不合格结果时不能收尾。服务重启后用 `GET /pending-batches` / `GET /reviews` 继续未结复核。
- **范围化偏差**：限制可挂在整批、特定包装或特定销售区域；解除限制必须由挂接人**之外的另一名授权者**执行，并引用真实存在的处置证据（检验、校准、决定等）。
- **仅追加决定台账**：放行/冻结/召回只追加不改写。状态按“包装×区域”矩阵计算，默认 `held`（不可销售）；放行要求复核完成、标准检验项目齐备且合格、方法一致、设备校准覆盖整个生产时段、关联物料未隔离、范围内无生效限制。
- **历史不可倒改**：已出库记录固化所引用的放行决定快照（含标准版本、决定人、证据）；标准发布新版本或批次后续被冻结/召回，都不改变历史决定与出库快照。
- **出库守门**：只有对应包装/区域当前为放行且无生效限制才能出库。
- **全链还原**：`GET /batches/{id}/explain` 返回批次、谱系、标准、物料、校准、检验、复核、限制、决定（含快照）、出库、审计事件与当前有效状态。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

基础服务验收：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

跨工厂批次放行平台验收（三厂、返工、四眼解除、重复导入、重启续审、版本快照、召回）：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.release.acceptance
```

成功时输出一行 `status` 为 `ok` 的 JSON（含 27 项检查结果）并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

写入接口通过 `X-Actor-Id` 标识操作者，请求体使用 `request_id` 保证幂等；服务重启后 SQLite 中的业务状态、未结复核、决定台账与审计链继续保留。

### 放行域主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/brand-standards` | 登记品牌标准版本（顺序递增） |
| POST | `/lines` | 登记产线 |
| POST | `/material-lots` | 登记原料/包装批号（可带 `quarantined`、`import_key`） |
| POST | `/material-lots/quarantine` | 改变物料隔离状态（需证据） |
| POST | `/calibrations` | 登记设备校准有效期 |
| POST | `/batches` | 登记批次（`relation` + `parent_batch_ids` 表达谱系） |
| POST | `/lab-results` | 导入实验室结果（实测值与结论一致性校验） |
| POST | `/reviews/complete` | 完成生产/实验室复核 |
| POST | `/restrictions` | 挂接范围化偏差限制 |
| POST | `/restrictions/lift` | 四眼解除限制（引用处置证据/复核） |
| POST | `/decisions` | 作出 release/freeze/recall 决定 |
| POST | `/shipments` | 出库（固化放行决定快照） |
| GET | `/batches/{id}` | 批次详情（含谱系与物料） |
| GET | `/batches/{id}/lineage` | 递归祖先/后代谱系 |
| GET | `/batches/{id}/status` | 包装×区域有效状态矩阵与可销售性 |
| GET | `/batches/{id}/explain` | 完整放行链还原 |
| GET | `/reviews` | 未结复核清单（可按 `site_id` 过滤，重启后续审） |
| GET | `/pending-batches` | 仍有未结复核的批次 |
| GET | `/decisions?batch_id=` | 仅追加的决定台账 |
| GET | `/shipments?batch_id=` | 出库记录与决定快照 |

角色：`quality_lead`（质量负责人）可登记标准、管理隔离/限制、作出放行决定；`reviewer` 负责实验室复核并可参与四眼解除；`operator` 负责产线、物料、检验录入与出库；`auditor` 只读审计。
