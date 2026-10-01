# 样本交接链核验服务（Chain of Custody）

面向环境检测机构的样本保管链核验服务：登记样本采集信息，接收**移交 / 签收 / 封签检查 / 实验室入库**四类带唯一事件号的事件，自动按发生时间重算证据链，输出**当前保管人**与 **有效（valid）/ 待补证（pending）/ 无效（invalid）**结论及原因；同一交接车次的多份样本支持**批量移交一次提交**（全局唯一批次号、统一移交时间/交出人/接收人，整批原子、同号幂等）；核定入库前支持对当前有效事件提交**作废 / 替换纠错**（永久留痕、仅有效版本参与核验）；仅有效链可核定入库，核定后历史冻结；全部真正提交按先后形成**不可变版本**，质控可按样本编号 + 版本号复核任一历史版本的结论、原因、保管人、有效时间线、纠错与核定信息（版本 0 起，核定后旧版仍可查）；同一抽检的多份样本可**封存为案卷**（至少两份、可逐份指定历史版本、整卷原子、按样本编号排序清单 + 完整快照 + 可复算 SHA-256 摘要，封存后任何写入不改变旧案卷）。

技术栈：**Python 3.11 + FastAPI + SQLite（标准库 sqlite3）**，无外部服务依赖。

---

## 一、核验规则

| 场景 | 判定 |
|---|---|
| 样本登记 | 记录采集时间、采集人（=初始保管人）、封签号；样本编号唯一 |
| 移交（transfer） | 必须由**当前保管人**发起；**移交至签收期间不得再次移交**，否则无效 |
| 签收（receive） | 必须同时满足：匹配在途**移交单号**、**接收人**与移交单一致、**封签号**与登记一致、签收时间**晚于**移交时间 |
| 重复签收 | 同一移交单被签收两次 → 无效（无效事件不可被后续补录抵消） |
| 封签检查（seal_check） | 检查封签号与登记不符，或封签破损（`seal_intact=false`）→ 无效 |
| 缺签收 | 移交后没有合格签收 → **待补证** |
| 缺入库 | 没有实验室入库记录，或入库缺失前序移交-签收链 → **待补证** |
| 保管人断链 | 移交人不是当前保管人 / 签收无对应在途移交 → **无效** |
| 结论优先级 | **无效优先**：同时存在无效原因与待补证事项时，结论为无效（原因中附注待补证事项） |
| 乱序补录 | 事件可任意顺序上报，每次查询/核定都按 `occurred_at`（发生时间，非录入时间）整体重算 |
| 事件幂等 | 同一 `event_id` + 同内容（SHA-256 指纹）重试 → 幂等返回 HTTP 200；同 `event_id` 异内容 → HTTP 409 冲突（核定后亦保持此兼容行为） |
| 批量移交 | 同一交接车次多份样本一次提交：全局唯一**批次号** + 统一移交时间/交出人/接收人 + 每份的样本编号、移交单号、事件号；各份均作为既有 transfer 事件原子落库。批内样本编号/事件号重复、样本不存在或已核定、任一事件号已占用 → **整批失败**，不留下事件或批次。同批次号同内容重试幂等（200，样本事后已核定也成立，items 顺序与时区表示不敏感），异内容 → 409；并发提交按已提交顺序串行判定 |
| 批次查询 | 返回逐份样本的**当前核验结论**与**原事件是否仍有效**（作废/替换后反映有效链：`event_effective` / `current_event_id`）；批次头与明细的原始归属永久留痕，纠错不改变批次归属 |
| 核定前纠错 | 核定前可按样本编号 + 当前有效事件号提交**作废（void）/替换（replace）**，附全局唯一纠错号、操作人、原因；替换须给完整新事件内容与**未占用**的新事件号。原事件与纠错关系永久留痕，核验仅纳入有效版本并按发生时间重算结论/原因/当前保管人；被替换记录可经链查询追溯。纠错**不得跨样本**；同纠错号同内容重试幂等（200）、异内容冲突（409）；并发针对同一有效版本仅一次成功，失败不改链。核定入库后拒绝纠错 |
| 纠错与核定并发 | 写事务以 `BEGIN IMMEDIATE` 串行：先提交者生效，后到者看到其已提交结果——作废先提交则核定基于最终有效链（非 valid 则 422），核定先提交则纠错被 `history_locked` 拒绝 |
| 入库核定 | 仅 `valid` 可核定；核定后该样本登记与事件全部**拒绝改写**（HTTP 409），重复核定幂等 |
| 版本化复核 | 样本保管链按**提交先后**形成不可变版本：新登记样本从**版本 0**开始；升级前已有样本以**升级时状态**补建版本 0 基线，不推断更早历史。此后每次**真正提交**——单事件、批量移交中的该样本事件、作废/替换纠错、入库核定——各使受影响样本版本**递增一次**；同号同内容重试与失败操作**不产生版本**。补录事件的发生时间较早也**不改写旧版**；同批次各样本的版本与事件**一同提交或一同失败**。核定后旧版仍可查询 |
| 案卷封存 | 同一抽检的多份样本（**至少两份、编号不重复**）一次封存为案卷：全局唯一**案卷号**，可逐份指定**非负历史版本**，未指定者取**封存事务中的最新版本**，整卷选取来自**同一数据库一致视图**。任一样本/版本不存在、编号重复 → **整卷失败且不留案卷**。案卷永久保存所选版本完整快照与**按样本编号排序的清单**，并生成**可复算的 SHA-256 摘要**；后续补录、纠错、核定**不改变旧案卷**。同案卷号同内容重试幂等（200，即使最新版本已变化也返回原卷，items 顺序不敏感），异内容 → 409；并发提交按已提交结果判定 |

> 说明：任何事件发生时间早于样本采集时间，直接记为无效（采集前不可能发生保管动作）。

---

## 二、一键启动

### 方式 A：Docker Compose（推荐，自动初始化依赖、建表、写入演示数据）

```bash
docker compose up -d --build
docker compose logs -f          # 看到 uvicorn 启动日志即可
```

- 首次启动自动：安装 Python 依赖（镜像构建阶段）→ 建表 → 写入 3 条演示样本（有效 / 待补证 / 无效）。
- SQLite 数据持久化在宿主机 `./data/chain.db`，容器重建不丢数据；已有数据时演示数据自动跳过。
- 不需要演示数据：把 `docker-compose.yml` 中 `SEED_ON_START` 改为 `"false"`（只建空库）。

停止 / 清理：

```bash
docker compose down             # 停止（保留数据）
docker compose down && rm -f data/chain.db*   # 停止并清空数据
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m app.seed              # 可选：初始化演示数据（幂等，可重复执行）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 三、服务地址与端口

| 用途 | 地址 |
|---|---|
| 服务根地址 | http://localhost:8080/ |
| 健康检查 | http://localhost:8080/health |
| Swagger 交互文档（可直接调试） | http://localhost:8080/docs |
| OpenAPI 规范 | http://localhost:8080/openapi.json |
| 业务 API 前缀 | `/api/v1` |

快速验证：

```bash
curl http://localhost:8080/health
curl http://localhost:8080/api/v1/samples        # 查看 3 条演示样本的结论
```

演示样本：

| 样本编号 | 结论 | 说明 |
|---|---|---|
| `DEMO-VALID-001` | valid | 移交→签收→封签检查→入库完整（事件故意乱序写入，演示自动重算） |
| `DEMO-PENDING-002` | pending | 已移交未签收、未入库 |
| `DEMO-INVALID-003` | invalid | 封签检查封签号不符且破损（无效优先于入库事实） |

---

## 四、配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口 |
| `DB_PATH` | Docker 内 `/data/chain.db`；本地 `./data/chain.db` | SQLite 文件路径 |
| `DATA_DIR` | Docker 内 `/data`；本地 `./data` | 数据目录（仅在未显式设置 `DB_PATH` 时用于拼默认路径） |
| `SEED_ON_START` | `true` | 启动时空库是否自动写入演示数据 |
| `LOCK_AFTER_APPROVAL` | `true` | 核定入库后是否冻结历史（按业务要求应始终为 true） |
| `DB_TIMEOUT` | `10` | SQLite 写锁等待秒数 |

---

## 五、HTTP API

所有时间均使用 ISO 8601（建议 UTC，如 `2026-09-30T02:00:00Z`；带时区偏移亦可，内部统一转 UTC）。

### 5.1 样本登记

`POST /api/v1/samples`

```json
{
  "sample_code": "S-001",
  "collected_at": "2026-09-30T01:00:00Z",
  "collector": "王采集",
  "seal_no": "SEAL-001"
}
```

- `201` 新建；同编号同内容重试 `200`（`idempotent: true`）；同编号异内容 `409 sample_conflict`。
- 响应中 `evaluation` 即时给出当前核验结论。

### 5.2 事件上报（统一入口）

`POST /api/v1/events`

事件类型与各自必填字段：

| event_type | 必填字段 | 含义 |
|---|---|---|
| `transfer` | `transfer_no`、`from_custodian`、`to_custodian` | 移交单号、交出方（须当前保管人）、接收方 |
| `receive` | `transfer_no`、`receiver`、`seal_no` | 匹配单号、签收人（须等于接收方）、签收封签号 |
| `seal_check` | `checked_seal_no`（`seal_intact` 默认 true） | 检查读取的封签号、是否完好 |
| `storage` | 无（建议 `custodian`、`storage_location`） | 实验室入库，`custodian` 默认“实验室” |

公共字段：`event_id`（全局唯一）、`sample_code`、`occurred_at`、可选 `note`（备注参与幂等指纹）。

```bash
# 移交
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-T1","event_type":"transfer","sample_code":"S-001",
  "occurred_at":"2026-09-30T02:00:00Z",
  "transfer_no":"TR-1","from_custodian":"王采集","to_custodian":"李运输"}'

# 签收（单号/接收人/封签号三匹配，且晚于移交）
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-R1","event_type":"receive","sample_code":"S-001",
  "occurred_at":"2026-09-30T04:00:00Z",
  "transfer_no":"TR-1","receiver":"李运输","seal_no":"SEAL-001"}'

# 封签检查
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-C1","event_type":"seal_check","sample_code":"S-001",
  "occurred_at":"2026-09-30T04:30:00Z",
  "checked_seal_no":"SEAL-001","seal_intact":true}'

# 实验室入库
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-S1","event_type":"storage","sample_code":"S-001",
  "occurred_at":"2026-09-30T05:00:00Z",
  "custodian":"实验室","storage_location":"冷库A-01"}'
```

- 新事件 `201`；同 `event_id` 同内容重试 `200`（`idempotent: true`）；异内容 `409 event_conflict`。
- 响应中 `evaluation.status` / `reasons` / `current_custodian` 为写入后重算结果。

### 5.3 查询

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/samples` | 全部样本核验摘要（结论、原因、当前保管人） |
| `GET /api/v1/samples/{sample_code}` | 单样本登记信息 + 核验摘要 |
| `GET /api/v1/samples/{sample_code}/chain` | **完整证据链**：按发生时间排列的事件时间线、当前保管人、在途移交、结论与全部原因 |

```bash
curl http://localhost:8080/api/v1/samples/S-001/chain
```

`chain` 响应要点：

```json
{
  "sample": {"sample_code": "S-001", "collector": "王采集", "seal_no": "SEAL-001", "...": "..."},
  "status": "valid",
  "reasons": ["保管链完整：采集 王采集 至当前保管人 实验室，封签一致，已入库"],
  "current_custodian": "实验室",
  "pending_transfer": null,
  "has_storage": true,
  "timeline": [
    {"event_id": "EVT-T1", "event_type": "transfer", "occurred_at": "...", "recorded_at": "...",
     "payload": {"...": "..."}, "replaces_event_id": null}
  ],
  "corrections": [
    {"correction_id": "FIX-1", "target_event_id": "EVT-C1", "action": "replace",
     "operator": "质控员", "reason": "……", "new_event_id": "EVT-C2", "created_at": "..."}
  ],
  "superseded_events": [
    {"event_id": "EVT-C1", "event_type": "seal_check", "occurred_at": "...", "payload": {"...": "..."},
     "superseded_by": "FIX-1", "replaces_event_id": null}
  ]
}
```

- `timeline` 仅含**当前有效版本**事件（`replaces_event_id` 标记该事件是否由替换产生、指向原事件号）；
- `superseded_events` 为被作废/替换的原始事件，仅用于追溯，不参与核验；
- `corrections` 为该样本全部纠错留痕（永久保留，核定后仍可查）。

### 5.4 入库核定

`POST /api/v1/samples/{sample_code}/approve`

```bash
curl -X POST http://localhost:8080/api/v1/samples/S-001/approve \
  -H 'Content-Type: application/json' -d '{"approver":"质量主管","note":"准予入库"}'
```

- `valid` → `200` 核定成功并冻结；`pending`/`invalid` → `422 approval_rejected`（响应体带回完整核验结果）。
- 核定后再上报事件或改写登记 → `409 history_locked`；重复核定幂等返回。
- 核定与纠错并发时，核定只可能基于**最终已提交的有效链**判定。

### 5.5 事件纠错（核定前：作废 / 替换）

`POST /api/v1/corrections`

公共字段：`correction_id`（全局唯一纠错号）、`sample_code`、`target_event_id`（被纠错的**当前有效**事件号）、`action`（`void` / `replace`）、`operator`、`reason`；`replace` 时必须提供 `new_event`（字段集与 5.2 事件完全一致，但**不含 `sample_code`**——纠错不得跨样本）。

```bash
# 作废：错误签收作废，核验随即只按剩余有效版本重算
curl -X POST http://localhost:8080/api/v1/corrections -H 'Content-Type: application/json' -d '{
  "correction_id":"FIX-0001","sample_code":"S-001","target_event_id":"EVT-R1",
  "action":"void","operator":"质控员","reason":"封签号录入错误，作废重报"}'

# 替换：给出完整新事件内容与未占用的新事件号
curl -X POST http://localhost:8080/api/v1/corrections -H 'Content-Type: application/json' -d '{
  "correction_id":"FIX-0002","sample_code":"S-001","target_event_id":"EVT-C1",
  "action":"replace","operator":"质控员","reason":"检查时扫码识别错误，整体替换",
  "new_event":{
    "event_id":"EVT-C2","event_type":"seal_check",
    "occurred_at":"2026-09-30T04:30:00Z",
    "checked_seal_no":"SEAL-001","seal_intact":true}}'
```

响应（`201` 新纠错；`200` 同纠错号同内容幂等重试）：

```json
{
  "correction": {
    "correction_id": "FIX-0002", "target_event_id": "EVT-C1", "action": "replace",
    "operator": "质控员", "reason": "检查时扫码识别错误，整体替换",
    "new_event_id": "EVT-C2", "created_at": "2026-09-30T05:10:00Z"
  },
  "evaluation": {"status": "valid", "reasons": ["..."], "current_custodian": "实验室", "...": "..."}
}
```

规则与语义：

| 规则 | 行为 |
|---|---|
| 准入 | 仅核定入库前可纠错；目标必须属于该样本且仍是当前有效版本 |
| 作废 `void` | 原事件标记失效（行不删除），不得携带 `new_event` |
| 替换 `replace` | 原事件标记失效，写入完整新事件；新事件号必须全局未占用（旧号永久留痕不可复用） |
| 重算 | 仅有效版本按 `occurred_at` 重算结论、原因、当前保管人；可连续替换（替换产生的新版本仍可再被纠错） |
| 幂等/冲突 | 同 `correction_id` 同内容重试 → `200`；异内容（含动作不同）→ `409 correction_conflict` |
| 并发 | 针对同一有效版本仅一次成功：落败方 `409 event_not_effective` 且事务回滚，链不变 |
| 核定后 | `409 history_locked`（包括与核定并发落败的纠错） |

纠错审计查询：

```bash
curl http://localhost:8080/api/v1/samples/S-001/corrections
# {"sample_code":"S-001","total":2,"items":[{"correction_id":"FIX-0002", ...}]}
```

被替换记录通过 `GET /api/v1/samples/{sample_code}/chain` 的 `superseded_events` / `corrections` 字段追溯（见 5.3）。

### 5.6 批量移交（同一交接车次多份样本）

`POST /api/v1/transfer-batches`

同一交接车次可能涉及多份样本：一次提交**全局唯一批次号**、**统一移交时间**、**交出人**、**接收人**，以及**每份样本的编号、移交单号和事件号**；批内各份均作为既有 `transfer` 事件在同一事务中落库，逐样本独立参与保管链核验。

```bash
curl -X POST http://localhost:8080/api/v1/transfer-batches \
  -H 'Content-Type: application/json' -d '{
    "batch_id":"BATCH-20260930-01",
    "occurred_at":"2026-09-30T02:00:00Z",
    "from_custodian":"王采集",
    "to_custodian":"李运输",
    "items":[
      {"sample_code":"S-001","transfer_no":"TR-20260930-01-01","event_id":"EVT-B01"},
      {"sample_code":"S-002","transfer_no":"TR-20260930-01-02","event_id":"EVT-B02"},
      {"sample_code":"S-003","transfer_no":"TR-20260930-01-03","event_id":"EVT-B03"}
    ]
  }'
```

请求字段：

| 字段 | 说明 |
|---|---|
| `batch_id` | 全局唯一批次号；同号同内容重试幂等、异内容冲突 |
| `occurred_at` | 批内各份统一移交时间（ISO 8601，内部归一 UTC） |
| `from_custodian` / `to_custodian` | 统一交出人 / 接收人；每份 transfer 均须满足“交出人=该样本当前保管人”（不满足不影响落库，但该份核验为断链 invalid） |
| `items[]` | 至少一份；每份含 `sample_code`、`transfer_no`、`event_id`（事件号全局唯一，落库为既有 transfer 事件） |

响应（`201` 新建；`200` 同批次号同内容幂等重试，带 `idempotent`）：

```json
{
  "batch": {
    "batch_id": "BATCH-20260930-01",
    "occurred_at": "2026-09-30T02:00:00Z",
    "from_custodian": "王采集",
    "to_custodian": "李运输",
    "item_count": 3,
    "created_at": "2026-09-30T01:59:00Z",
    "items": [
      {
        "index": 0,
        "sample_code": "S-001",
        "transfer_no": "TR-20260930-01-01",
        "event_id": "EVT-B01",
        "event_effective": true,
        "superseded_by": null,
        "current_event_id": "EVT-B01",
        "evaluation": {"status": "pending", "reasons": ["..."], "current_custodian": "王采集", "...": "..."}
      }
    ]
  },
  "idempotent": false
}
```

规则与语义：

| 规则 | 行为 |
|---|---|
| 整批成功 | 批内每份成为该样本的既有 `transfer` 事件（时间线 `batch_id` 标记批次归属），响应逐项给出提交后核验摘要 |
| 整批失败 | 批内样本编号重复 / 事件号重复 / 样本不存在（`422`），样本已核定 / 任一事件号已占用（`409`）→ 事务回滚，**不留下任何事件或批次**；`error.details.items` 逐项给出 `index/sample_code/event_id/code/message`；冲突与不合法项并存时冲突优先（409） |
| 幂等/冲突 | 同 `batch_id` 同内容 → `200`（对 items 提交顺序与时区表示不敏感；**样本事后已核定仍幂等**）；异内容（批次头任一字段或任一份内容变化）→ `409 batch_conflict`。校验失败不占用批次号，修正后可复用 |
| 批次与核定并发 | 写事务 `BEGIN IMMEDIATE` 串行，按已提交顺序判定：批次先提交则核定基于最终有效链（新增在途移交通常转为 pending → `422`）；核定先提交则批次因样本已核定整体 `409` 回滚 |
| 与单事件接口 | 批量事件号走既有单事件接口重试：同内容 `200`、异内容 `409 event_conflict`；纠错、核定接口行为不变 |
| 失败批次号 | `GET` 不存在的批次 → `404 batch_not_found` |

批次查询：

`GET /api/v1/transfer-batches/{batch_id}`

```bash
curl http://localhost:8080/api/v1/transfer-batches/BATCH-20260930-01
```

返回批次头与逐份当前状态。后续纠错只改变**有效链与查询结果**，批次原始归属可追溯：

| 字段 | 含义 |
|---|---|
| `items[].evaluation` | 该份样本**当前**核验结论（status/reasons/current_custodian 等） |
| `items[].event_effective` | 批内**原**移交事件是否仍是当前有效版本（未被作废/替换） |
| `items[].superseded_by` | 原事件被哪条纠错作废/替换（未纠错为 `null`） |
| `items[].current_event_id` | 沿替换链走到的当前有效事件号；被作废且无替换后继为 `null` |
| 批次头 / 明细 | `batch_id`、统一移交时间、交出人、接收人、份数与每份原始编号/单号/事件号**永久留痕**，纠错不增减、不改变 |

被作废/替换的批内原事件同样出现在对应样本 chain 的 `timeline.batch_id`（有效时）或 `superseded_events.batch_id`（失效后），可双向追溯。

### 5.7 保管链版本复核（质控）

质控人员按**样本编号 + 版本号**复核某份样本在指定提交版本的保管链：当时的核验结论、原因、当前保管人、有效事件时间线、纠错记录、被作废/替换留痕与核定信息；并可随时取得当前最新版本号。版本快照在提交时点固化、不可变，核定后的旧版仍可查询。

版本号规则：

- **版本 0**：新登记样本的登记基线；升级前已有样本在启动迁移时以**升级时状态**补建一条 `baseline` 版本 0（不推断更早历史）。
- **每次真正提交 +1**：单事件上报、批量移交中该样本的那份事件、作废/替换纠错成功、入库核定成功，各使受影响样本递增一个版本。
- **不产生版本**：同号同内容的幂等重试（样本/事件/批次/纠错/核定）、以及任何失败操作（冲突 409、整批失败、核定被拒 422、字段校验 422 等）。
- **按提交先后**：版本顺序是写入提交顺序，与事件 `occurred_at` 无关——乱序补录的更早事件只进入新版本，**旧版快照不变**。
- **批次原子**：同一批次各样本的版本与其事件在同一事务中，整批成功则每份 +1，整批失败则一份都不增。

#### 版本索引

`GET /api/v1/samples/{sample_code}/versions`

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions
```

```json
{
  "sample_code": "S-001",
  "current_version": 6,
  "total": 7,
  "items": [
    {"version": 0, "change_type": "register", "change_ref": null,
     "committed_at": "2026-09-30T01:05:00Z", "status": "pending",
     "reasons": ["缺少实验室入库记录"], "current_custodian": "王采集",
     "has_storage": false, "event_count": 0, "approved_storage_at": null},
    {"version": 1, "change_type": "event", "change_ref": "EVT-T1", "...": "..."},
    {"version": 5, "change_type": "correction", "change_ref": "FIX-0002", "...": "..."},
    {"version": 6, "change_type": "approval", "change_ref": null,
     "committed_at": "2026-09-30T05:20:00Z", "status": "valid",
     "current_custodian": "实验室", "approved_storage_at": "2026-09-30T05:20:00Z"}
  ]
}
```

`items[].change_type` 取值：`register`（登记版本 0）/ `baseline`（升级基线版本 0）/ `event`（单事件）/ `batch_transfer`（批量移交中的该样本事件，`change_ref` 为批次号）/ `correction`（作废或替换纠错，`change_ref` 为纠错号）/ `approval`（入库核定）。

#### 指定版本完整快照

`GET /api/v1/samples/{sample_code}/versions/{version}`

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions/3
```

返回该版本**提交时点固化**的完整快照（结构与 5.3 的 `chain` 同源）：

```json
{
  "version_snapshot": {
    "sample": {"sample_code": "S-001", "collector": "王采集", "seal_no": "SEAL-001", "...": "..."},
    "version": 3,
    "change_type": "event",
    "change_ref": "EVT-R1",
    "committed_at": "2026-09-30T04:00:03Z",
    "status": "pending",
    "reasons": ["缺少实验室入库记录"],
    "current_custodian": "李运输",
    "pending_transfer": null,
    "has_storage": false,
    "event_count": 2,
    "timeline": [ {"event_id": "EVT-T1", "...": "..."}, {"event_id": "EVT-R1", "...": "..."} ],
    "corrections": [],
    "superseded_events": [],
    "approved_storage_at": null,
    "approved_by": null,
    "approval": null
  }
}
```

- `timeline` 为当时的**有效事件**时间线；`corrections` / `superseded_events` 为截至该版本的纠错与作废/替换留痕；
- `approval` 在核定版本及之后为 `{approved_at, approver, note}`，此前为 `null`；
- 快照一经写入不再随后续纠错/补录变化——对比不同版本即可复核保管链如何演变。

#### 当前最新版本

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions/latest
```

等价于先查索引取 `current_version` 再按号取快照。此外，样本列表/单样本摘要/证据链/事件与纠错写入响应/批次逐项 `evaluation` 中均附 `current_version` 字段（增量字段，不影响既有响应）。

#### 错误

| 情形 | HTTP | code |
|---|---|---|
| 样本编号未知（索引 / latest / 指定版本均是） | 404 | `sample_not_found` |
| 版本号不存在（含非整数、负数；详情带 `current_version` 便于定位） | 404 | `version_not_found` |

核定（冻结）只阻止新的写入，**不影响任何版本的只读查询**。

### 5.8 案卷封存（同一抽检多份样本）

质控将同一抽检的多份样本保管链封存为案卷：提交**全局唯一案卷号**与**至少两份不重复样本编号**，可逐份指定**非负历史版本**；未指定者取**封存事务中的最新版本**。整卷选取在同一事务（同一数据库一致视图）中完成；任一样本或版本不存在、编号重复 → **整卷失败且不留案卷**。案卷永久保存所选版本的完整快照与**按样本编号排序的清单**，并对封存内容生成**可复算的 SHA-256 摘要**；后续补录、纠错、核定均不改变旧案卷。

`POST /api/v1/dossiers`

```bash
curl -X POST http://localhost:8080/api/v1/dossiers \
  -H 'Content-Type: application/json' -d '{
    "dossier_id":"DOSSIER-20261001-01",
    "items":[
      {"sample_code":"S-001","version":3},
      {"sample_code":"S-002"}
    ]
  }'
```

请求字段：

| 字段 | 说明 |
|---|---|
| `dossier_id` | 全局唯一案卷号；同号同内容重试幂等、异内容冲突 |
| `items[]` | 至少两份；每份含 `sample_code` 与可选 `version`（非负整数，缺省取封存事务中最新版本） |

响应（`201` 新建；`200` 同案卷号同内容幂等重试，带 `idempotent`）：

```json
{
  "dossier": {
    "dossier_id": "DOSSIER-20261001-01",
    "item_count": 2,
    "digest": "9f2c…（64 位十六进制）",
    "created_at": "2026-10-01T02:00:00Z",
    "items": [
      {"index": 0, "sample_code": "S-001", "version": 3, "snapshot": {"version": 3, "status": "valid", "...": "..."}},
      {"index": 1, "sample_code": "S-002", "version": 6, "snapshot": {"version": 6, "status": "pending", "...": "..."}}
    ]
  },
  "idempotent": false
}
```

- `items` 无论提交顺序如何，清单恒**按样本编号升序**返回（`index` 0 起）；`snapshot` 即该样本该版本的完整保管链快照（结构同 5.7 指定版本快照）。
- 封存的是**版本快照副本**：此后样本补录、纠错、核定都不改变案卷内容。

规则与语义：

| 规则 | 行为 |
|---|---|
| 整卷成功 | 各份所选版本快照与清单一同事务落库；响应给出每份实际封存的版本号与完整快照 |
| 整卷失败 | 编号重复（`422 dossier_rejected`，明细 `duplicate_sample_in_dossier`）/ 样本不存在（`404 sample_not_found`）/ 版本不存在（`404 version_not_found`，详情附 `current_version`）→ 事务回滚，**不留下案卷**；失败不占用案卷号，修正后可复用 |
| 幂等/冲突 | 同 `dossier_id` 同内容 → `200`（对 items 提交顺序不敏感；**即使最新版本已变化也返回原卷**）；异内容（样本集合或任一份指定版本变化）→ `409 dossier_conflict` |
| 并发 | 写事务 `BEGIN IMMEDIATE` 串行：同号同内容一建一幂等；同号异内容先提交者生效、后到者 `409`，库中遵循已提交结果 |
| 份数/版本号校验 | 不足两份、版本号为负 → `422`（请求体字段校验） |

**摘要算法（可复算）**：对规范结构

```json
{"dossier_id": "案卷号",
 "items": [{"sample_code": "...", "version": 0, "snapshot": {...}}, "... 按样本编号升序"]}
```

以 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 序列化后取 **SHA-256 十六进制**。任何人持有案卷号、清单与快照即可独立复算比对。

案卷查询：

`GET /api/v1/dossiers/{dossier_id}` —— 返回清单、各份完整快照与摘要；不存在 → `404 dossier_not_found`。

案卷核验：

```bash
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-20261001-01/verify
```

```json
{"dossier_id": "DOSSIER-20261001-01",
 "stored_digest": "9f2c…", "recomputed_digest": "9f2c…", "match": true}
```

核验按库存封存内容**重算摘要**并与留存摘要比对，返回一致与否（`match`）及两侧摘要；核验为只读操作，**不静默改写**任何存储内容——内容被篡改时报告 `match: false`，留存摘要与快照保持原样。

### 错误响应统一格式

```json
{"error": {"code": "event_conflict", "message": "……", "details": {}}}
```

| HTTP | code | 触发 |
|---|---|---|
| 404 | `sample_not_found` / `event_not_found` | 样本不存在 / 纠错目标事件不存在 |
| 404 | `version_not_found` | 版本复核的版本号不存在（非整数/负数/超出现有版本；详情附 `current_version`） |
| 409 | `sample_conflict` / `event_conflict` | 同编号异内容 / 同事件号异内容 |
| 409 | `history_locked` | 已核定入库后改写历史或纠错 |
| 409 | `correction_conflict` | 同纠错号异内容（含动作 void↔replace 不一致） |
| 409 | `correction_cross_sample` | 纠错目标事件不属于提交的样本（禁止跨样本） |
| 409 | `event_not_effective` | 目标事件已被作废/替换，或并发下被抢先纠错（非当前有效版本） |
| 409 | `new_event_id_occupied` | 替换所用新事件号已被占用 |
| 404 | `batch_not_found` | 批次查询的批次号不存在 |
| 409 | `batch_conflict` | 同批次号异内容重试（含批次头或任一份样本内容变化） |
| 409/422 | `batch_rejected` | 整批失败：409=样本已核定（`sample_approved`）/事件号已占用（`event_id_occupied`）；422=批内样本重复（`duplicate_sample_in_batch`）/事件号重复（`duplicate_event_in_batch`）/样本不存在（`sample_not_found`）；逐项原因见 `details.items` |
| 422 | `approval_rejected` | 非有效链尝试核定（或 Pydantic 字段校验失败） |
| 404 | `dossier_not_found` | 案卷查询/核验的案卷号不存在 |
| 409 | `dossier_conflict` | 同案卷号异内容重试（样本集合或任一份指定版本变化） |
| 422 | `dossier_rejected` | 整卷失败：卷内样本编号重复（`duplicate_sample_in_dossier`，明细见 `details.items`）；样本/版本不存在分别按 `sample_not_found`/`version_not_found` 报 404，均不留案卷 |

---

## 六、Python 调用示例

仓库内置一个**仅用标准库**（无需安装 requests）的端到端示例脚本：

```bash
python examples/call_api.py
```

脚本依次演示：登记 → 移交 → 签收 → 封签检查 → 入库 → 幂等重试 → 冲突 → 查询证据链 → **纠错替换 / 同号幂等 / 链追溯 / 作废演示 / 纠错审计查询** → 核定 → 核定后改写与纠错被拒 → **批量移交提交 / 同号幂等 / 异内容冲突 / 整批失败 / 批次查询 / 批内作废后查询变化** → **保管链版本复核（版本索引、指定历史版本快照、当前最新版本、未知样本/版本报错）** → **案卷封存（指定/缺省版本封存、案卷查询、摘要核验、同号幂等重试、异内容冲突、整卷失败）**。也可直接在 http://localhost:8080/docs 的 Swagger UI 中点击调用。

版本复核也可用 curl 直接体验（完整脚本见下）：

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions            # 版本索引 + current_version
curl http://localhost:8080/api/v1/samples/S-001/versions/3          # 指定版本完整快照
curl http://localhost:8080/api/v1/samples/S-001/versions/latest     # 当前最新版本快照
```

案卷封存 curl 速览：

```bash
curl -X POST http://localhost:8080/api/v1/dossiers -H 'Content-Type: application/json' -d '{
  "dossier_id":"DOSSIER-001",
  "items":[{"sample_code":"S-001","version":3},{"sample_code":"S-002"}]}'   # 封存（S-002 取最新版本）
curl http://localhost:8080/api/v1/dossiers/DOSSIER-001                     # 查询清单/快照/摘要
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-001/verify      # 核验：重算摘要比对
```

---

## 七、自测

```bash
python -m tests.test_chain          # 既有规则：42 项断言
python -m tests.test_corrections    # 纠错：65 项断言（含并发）
python -m tests.test_batches        # 批量移交：83 项断言（含并发）
python -m tests.test_versions       # 版本复核：94 项断言（含旧库迁移基线）
python -m tests.test_dossiers       # 案卷封存：61 项断言（含并发与篡改核验）
```

- `test_chain` 覆盖 14 类场景：登记幂等/冲突、四类无效情形（非保管人移交、在途再移交、签收四要素不符、重复签收、封签不符/破损）、待补证两情形、乱序补录重算、核定准入与冻结、事件早于采集、无效优先等。
- `test_corrections` 覆盖：作废后仅有效版本重算（结论/原因/保管人）、替换生效与多跳替换、原事件与纠错关系留痕及链追溯、审计查询、同纠错号幂等/异内容冲突、跨样本拒绝、非有效版本拒绝、新事件号占用、replace/void 载荷校验、核定后拒绝纠错、同事件号 200/409 兼容、同有效版本并发仅一次成功、纠错与核定并发两种合法结局（可重复运行）。
- `test_batches` 覆盖：整批提交各份成为既有 transfer（逐份 pending/invalid 与在途信息、时间线批次归属）、整批失败四类原因（批内样本/事件号重复、样本不存在、已核定、事件号占用）且不留批次/事件/明细、冲突聚合与 409/422 优先、同批次号幂等（items 换序、时区归一、核定后仍 200）与异内容 409、批次查询随作废/替换更新（`event_effective`/`current_event_id`）与原始归属留痕、单事件接口对批量事件号 200/409、同批并发一建一幂等、异批事件号交叠一胜整批回滚、批次与核定并发按已提交顺序判定两种结局、载荷校验与失败批次号复用。
- `test_versions` 覆盖：登记即版本 0（快照字段）、单事件每次真正提交 +1 而同号重试/冲突失败不产生版本、快照不可变且补录较早事件不改旧版（版本按提交先后、时间线按发生时间重排）、批量移交各样本版本随事件同提交/整批失败全不增/幂等不增/失败批次号修正后复用、替换与作废纠错 +1 而幂等与对失效事件纠错不增、核定 +1 而核定被拒/重复核定/冻结后写入不增、核定后全部旧版仍可查、版本索引/`latest`/指定版本端点、未知样本与不存在/非数字/负数版本号报错、快照完整字段、旧库迁移为已有样本补建 `baseline` 版本 0 且幂等、迁移后版本从 1 继续。
- `test_dossiers` 覆盖：基本封存（指定/缺省版本、清单按样本编号排序、快照与版本复核一致、摘要可独立复算）、整卷失败五类（编号重复/样本不存在/版本不存在/份数不足/负数版本）且不留案卷与案卷号可复用、同号同内容（含换序）幂等且最新版本变化后仍返回原卷、异内容 409、封存后补录/纠错/核定不改变旧案卷、核验一致/篡改报不一致且不静默改写、不存在案卷查询与核验 404、同号同内容并发一建一幂等、同号异内容并发一建一冲突且遵循已提交结果、既有版本与写入接口行为保持。

---

## 八、目录结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由、异常映射、启动初始化
│   ├── service.py    # 核心领域逻辑：幂等指纹、保管链重算、纠错（作废/替换）、核定冻结、批量移交
│   ├── versions.py   # 保管链版本：版本快照记录、版本 0 基线迁移、版本索引/指定版本查询
│   ├── dossiers.py   # 案卷封存：整卷原子封存、按编号排序清单、SHA-256 摘要与核验、同号幂等/冲突
│   ├── schemas.py    # 请求模型：事件、纠错（NewEventIn/CorrectionIn）、批量移交（TransferBatchIn）、案卷（DossierIn）按类型必填校验
│   ├── db.py         # SQLite 连接、建表与旧库迁移（批次表、版本表与基线补建、案卷表）、IMMEDIATE 事务
│   ├── seed.py       # 演示数据初始化（幂等）
│   └── config.py     # 环境变量配置
├── examples/call_api.py   # 零依赖调用示例（含纠错、批量移交、版本复核与案卷封存全流程）
├── tests/test_chain.py    # 既有规则端到端自测（42 项断言）
├── tests/test_corrections.py  # 纠错端到端自测（65 项断言，含并发）
├── tests/test_batches.py  # 批量移交端到端自测（83 项断言，含并发）
├── tests/test_versions.py # 保管链版本复核端到端自测（94 项断言，含旧库迁移）
├── tests/test_dossiers.py # 案卷封存端到端自测（61 项断言，含并发与篡改核验）
├── entrypoint.sh     # 容器入口：建表 → 演示数据 → 启动
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
