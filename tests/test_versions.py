"""端到端自测：样本保管链版本复核（质控版本化）。

覆盖：
  * 新登记样本版本 0；单事件每次真正提交 +1，同号同内容重试/冲突失败不产生版本；
  * 版本按提交先后记录，补录较早发生时间不改写旧版；快照不可变；
  * 批量移交各样本版本随事件一同提交/回滚；批次幂等重试不产生版本；
  * 作废/替换纠错、入库核定各 +1；幂等与失败（含核定被拒、冻结）不产生版本；
  * 核定后旧版仍可查；未知样本/不存在版本/非法版本号明确报错；/latest 便捷端点；
  * 升级前已有样本以升级时状态补建版本 0（旧库迁移）；
  * 各写入/查询响应附 current_version，既有响应结构保持兼容。

运行：python -m tests.test_versions
"""
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_versions.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.db import db_session, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan（启动钩子）
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="张三", seal=None, when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when,
        "collector": collector, "seal_no": seal or f"SEAL-{code}"})


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


def ver(code, v):
    return client.get(f"{API}/samples/{code}/versions/{v}").json()["version_snapshot"]


def ver_resp(code, v):
    return client.get(f"{API}/samples/{code}/versions/{v}")


def latest(code):
    return client.get(f"{API}/samples/{code}/versions/latest").json()["version_snapshot"]


def cur(code):
    return versions(code)["current_version"]


def complete_valid(code, seal=None):
    """登记并补齐 移交→签收→检查→入库 的有效链；返回各步后的版本号列表。"""
    seal = seal or f"SEAL-{code}"
    reg(code, seal=seal)
    vs = [cur(code)]
    steps = [
        {"event_id": f"{code}-T1", "event_type": "transfer", "sample_code": code,
         "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": f"TR-{code}",
         "from_custodian": "张三", "to_custodian": "李四"},
        {"event_id": f"{code}-R1", "event_type": "receive", "sample_code": code,
         "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": f"TR-{code}",
         "receiver": "李四", "seal_no": seal},
        {"event_id": f"{code}-C1", "event_type": "seal_check", "sample_code": code,
         "occurred_at": "2026-09-30T04:30:00Z", "checked_seal_no": seal, "seal_intact": True},
        {"event_id": f"{code}-S1", "event_type": "storage", "sample_code": code,
         "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"},
    ]
    for s in steps:
        ev(s)
        vs.append(cur(code))
    return vs


# ---------------------------------------------------------------- 1. 登记即版本 0
print("1) 新登记样本从版本 0 开始")
r = reg("V1")
check("登记 201 且响应附 current_version=0",
      r.status_code == 201 and r.json()["evaluation"]["current_version"] == 0, r.text)
v0 = ver("V1", 0)
check("v0 change_type=register", v0["change_type"] == "register")
check("v0 change_ref 为空", v0["change_ref"] is None)
check("v0 committed_at 存在", bool(v0["committed_at"]))
check("v0 时间线为空、事件计数 0", v0["event_count"] == 0 and v0["timeline"] == [])
check("v0 初始保管人=采集人", v0["current_custodian"] == "张三")
check("v0 无核定信息", v0["approval"] is None and v0["approved_storage_at"] is None)
check("v0 含样本登记信息", v0["sample"]["sample_code"] == "V1" and v0["sample"]["seal_no"] == "SEAL-V1")
check("登记幂等重试 200 不产生版本",
      reg("V1").status_code == 200 and cur("V1") == 0)

# ---------------------------------------------------------------- 2. 每次提交 +1 / 重试不 +1
print("2) 单事件真正提交各 +1，同号同内容重试不产生版本")
vs = complete_valid("V2")
check("版本序列 0,1,2,3,4", vs == [0, 1, 2, 3, 4], str(vs))
v4 = ver("V2", 4)
check("v4 结论 valid、保管人实验室", v4["status"] == "valid" and v4["current_custodian"] == "实验室")
check("v4 时间线 4 条", len(v4["timeline"]) == 4)
check("版本总数 5（0..4）", versions("V2")["total"] == 5)
# 同内容重试
r = ev({"event_id": "V2-T1", "event_type": "transfer", "sample_code": "V2",
        "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-V2",
        "from_custodian": "张三", "to_custodian": "李四"})
check("同事件号同内容重试 200", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("重试后版本仍为 4", cur("V2") == 4)
# 异内容冲突失败 → 不产生版本
r = ev({"event_id": "V2-T1", "event_type": "transfer", "sample_code": "V2",
        "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-V2",
        "from_custodian": "张三", "to_custodian": "李四"})
check("同事件号异内容 409", r.status_code == 409, r.text)
check("冲突失败不产生版本", cur("V2") == 4)

# ---------------------------------------------------------------- 3. 快照不可变 + 补录不改旧版
print("3) 快照不可变；补录较早发生时间的事件不改写旧版（按提交先后）")
reg("V3")
ev({"event_id": "V3-LATE", "event_type": "storage", "sample_code": "V3",
    "occurred_at": "2026-09-30T09:00:00Z", "custodian": "实验室"})
check("第一次提交后 v1", cur("V3") == 1)
v1 = ver("V3", 1)
check("v1 仅含先提交的事件", [e["event_id"] for e in v1["timeline"]] == ["V3-LATE"], str(v1["timeline"]))
# 补录一个发生时间更早的移交（乱序补录）
ev({"event_id": "V3-EARLY", "event_type": "transfer", "sample_code": "V3",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-V3",
    "from_custodian": "张三", "to_custodian": "李四"})
check("补录后 v2", cur("V3") == 2)
v1_again = ver("V3", 1)
check("v1 快照不被改写（仍仅 1 条）", [e["event_id"] for e in v1_again["timeline"]] == ["V3-LATE"])
check("v1 结论字段也冻结", v1_again["event_count"] == 1 and v1_again["status"] == v1["status"])
v2 = ver("V3", 2)
check("v2 时间线按发生时间重排（早事件在前）",
      [e["event_id"] for e in v2["timeline"]] == ["V3-EARLY", "V3-LATE"], str(v2["timeline"]))
check("版本按提交先后（与发生时间无关）",
      [i["version"] for i in versions("V3")["items"]] == [0, 1, 2])
# 版本索引摘要字段
idx = {i["version"]: i for i in versions("V3")["items"]}
check("索引含提交时间/触发类型/结论/保管人",
      idx[1]["change_type"] == "event" and idx[1]["change_ref"] == "V3-LATE"
      and idx[1]["status"] == v1["status"] and idx[1]["current_custodian"] == "张三"
      and bool(idx[1]["committed_at"]))

# ---------------------------------------------------------------- 4. 批量移交版本
print("4) 批量移交：各样本版本随事件一同提交，整批失败全部不产生版本")
for i, bc in enumerate(("V4-A", "V4-B", "V4-C")):
    reg(bc, seal=f"SEAL-{bc}")
reg("V4-UNRELATED")
body = {
    "batch_id": "V4-BATCH", "occurred_at": "2026-09-30T02:00:00Z",
    "from_custodian": "张三", "to_custodian": "李四",
    "items": [
        {"sample_code": "V4-A", "transfer_no": "TR-A", "event_id": "V4-EA"},
        {"sample_code": "V4-B", "transfer_no": "TR-B", "event_id": "V4-EB"},
        {"sample_code": "V4-C", "transfer_no": "TR-C", "event_id": "V4-EC"},
    ],
}
r = client.post(f"{API}/transfer-batches", json=body)
check("批量提交 201", r.status_code == 201, r.text)
for bc in ("V4-A", "V4-B", "V4-C"):
    snap = ver(bc, 1)
    check(f"{bc} v1 为批次事件", snap["change_type"] == "batch_transfer" and snap["change_ref"] == "V4-BATCH",
          str(snap["change_type"]))
    check(f"{bc} v1 时间线为批内 transfer", [e["event_id"] for e in snap["timeline"]] == [f"V4-E{bc[-1]}"])
check("批外样本版本不受影响", cur("V4-UNRELATED") == 0)
# 批次条目摘要附 current_version
check("批次查询逐项 current_version=1",
      all(it["evaluation"]["current_version"] == 1 for it in r.json()["batch"]["items"]), r.text)
# 同号同内容重试 → 幂等，不产生版本
body2 = dict(body, items=list(reversed(body["items"])))
r2 = client.post(f"{API}/transfer-batches", json=body2)
check("批次同号换序重试 200 幂等", r2.status_code == 200 and r2.json()["idempotent"] is True, r2.text)
check("幂等重试后各样本仍 v1", all(cur(bc) == 1 for bc in ("V4-A", "V4-B", "V4-C")))
# 整批失败（批内样本重复 + 事件号占用）→ 任何样本都不产生版本
bad = {
    "batch_id": "V4-BAD", "occurred_at": "2026-09-30T03:00:00Z",
    "from_custodian": "张三", "to_custodian": "王五",
    "items": [
        {"sample_code": "V4-A", "transfer_no": "TR-X", "event_id": "V4-NEW-1"},
        {"sample_code": "V4-A", "transfer_no": "TR-Y", "event_id": "V4-EA"},
    ],
}
rb = client.post(f"{API}/transfer-batches", json=bad)
check("整批失败 409 batch_rejected", rb.status_code == 409
      and rb.json()["error"]["code"] == "batch_rejected", rb.text)
check("整批失败不留版本（V4-A 仍 v1；新事件号对应无版本）",
      cur("V4-A") == 1 and cur("V4-B") == 1)
# 失败不占用批次号，修正后可复用且产生版本
bad["items"] = [{"sample_code": "V4-UNRELATED", "transfer_no": "TR-U", "event_id": "V4-NEW-1"}]
rok = client.post(f"{API}/transfer-batches", json=bad)
check("修正后复用批次号成功 201", rok.status_code == 201, rok.text)
check("复用成功 → 该样本 v1", cur("V4-UNRELATED") == 1)

# ---------------------------------------------------------------- 5. 纠错版本
print("5) 作废/替换纠错各 +1；幂等与失败不产生版本")
complete_valid("V5", seal="SEAL-V5")
check("纠错前 v4", cur("V5") == 4)
fix_void = {"correction_id": "V5-FIX1", "sample_code": "V5", "target_event_id": "V5-C1",
            "action": "replace", "operator": "质控员", "reason": "检查记录更正",
            "new_event": {"event_id": "V5-C2", "event_type": "seal_check",
                          "occurred_at": "2026-09-30T04:30:00Z",
                          "checked_seal_no": "SEAL-V5", "seal_intact": True}}
r = client.post(f"{API}/corrections", json=fix_void)
check("替换纠错 201", r.status_code == 201, r.text)
check("纠错响应 evaluation.current_version=5",
      r.json()["evaluation"]["current_version"] == 5, r.text)
check("纠错后 v5", cur("V5") == 5)
v5snap = ver("V5", 5)
check("v5 change_type=correction/change_ref=纠错号",
      v5snap["change_type"] == "correction" and v5snap["change_ref"] == "V5-FIX1")
check("v5 有效时间线含新事件 V5-C2", any(e["event_id"] == "V5-C2" for e in v5snap["timeline"]))
check("v5 被替换记录 V5-C1 进入 superseded_events",
      [e["event_id"] for e in v5snap["superseded_events"]] == ["V5-C1"],
      str(v5snap["superseded_events"]))
check("v5 纠错留痕含该条", [c["correction_id"] for c in v5snap["corrections"]] == ["V5-FIX1"])
v4snap = ver("V5", 4)
check("v4 快照仍是纠错前（C1 有效、无留痕）",
      any(e["event_id"] == "V5-C1" for e in v4snap["timeline"])
      and v4snap["superseded_events"] == [] and v4snap["corrections"] == [])
# 同纠错号同内容重试 → 200，不产生版本
r = client.post(f"{API}/corrections", json=fix_void)
check("纠错幂等重试 200", r.status_code == 200 and r.json()["evaluation"]["current_version"] == 5, r.text)
check("幂等重试后仍 v5", cur("V5") == 5)
# 针对已失效事件再纠错 → 失败不产生版本
r = client.post(f"{API}/corrections", json={
    "correction_id": "V5-FIX2", "sample_code": "V5", "target_event_id": "V5-C1",
    "action": "void", "operator": "质控员", "reason": "对已替换事件再纠错"})
check("对非有效版本纠错 409 event_not_effective",
      r.status_code == 409 and r.json()["error"]["code"] == "event_not_effective", r.text)
check("失败不产生版本", cur("V5") == 5)
# 作废成功 +1
r = client.post(f"{API}/corrections", json={
    "correction_id": "V5-FIX3", "sample_code": "V5", "target_event_id": "V5-C2",
    "action": "void", "operator": "质控员", "reason": "检查记录作废"})
check("作废 201 且版本 +1", r.status_code == 201 and cur("V5") == 6, r.text)
check("v6 change_ref=V5-FIX3", ver("V5", 6)["change_ref"] == "V5-FIX3")

# ---------------------------------------------------------------- 6. 核定版本与冻结
print("6) 入库核定 +1；核定被拒/重复核定不产生版本；核定后旧版仍可查")
complete_valid("V6", seal="SEAL-V6")
# 非 valid 核定被拒 → 不产生版本（用一个 pending 样本）
reg("V6P")
rp = client.post(f"{API}/samples/V6P/approve", json={"approver": "主管"})
check("pending 核定被拒 422", rp.status_code == 422 and rp.json()["error"]["code"] == "approval_rejected", rp.text)
check("核定被拒不产生版本", cur("V6P") == 0)
r = client.post(f"{API}/samples/V6/approve", json={"approver": "质量主管", "note": "准予入库"})
check("valid 核定 200 且 current_version=5",
      r.status_code == 200 and r.json()["evaluation"]["current_version"] == 5, r.text)
check("核定后 v5", cur("V6") == 5)
v6a = ver("V6", 5)
check("v5 change_type=approval", v6a["change_type"] == "approval")
check("v5 快照含核定信息",
      v6a["approval"] == {"approved_at": v6a["approved_storage_at"],
                          "approver": "质量主管", "note": "准予入库"}
      and v6a["approved_storage_at"] is not None, str(v6a["approval"]))
check("核定前 v4 快照无核定信息", ver("V6", 4)["approval"] is None)
# 重复核定幂等
r = client.post(f"{API}/samples/V6/approve", json={"approver": "质量主管", "note": "准予入库"})
check("重复核定 200 幂等", r.status_code == 200)
check("重复核定不产生版本", cur("V6") == 5)
# 核定后写事件/纠错被冻结 → 不产生版本
r = ev({"event_id": "V6-LATE", "event_type": "seal_check", "sample_code": "V6",
        "occurred_at": "2026-09-30T08:00:00Z", "checked_seal_no": "SEAL-V6"})
check("核定后写事件 409", r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)
r = client.post(f"{API}/corrections", json={
    "correction_id": "V6-FIXL", "sample_code": "V6", "target_event_id": "V6-T1",
    "action": "void", "operator": "质控员", "reason": "冻结后试探"})
check("核定后纠错 409", r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)
check("冻结后版本仍为 5", cur("V6") == 5)
# 旧版全部仍可查
for v in range(6):
    check(f"核定后版本 {v} 仍可查", ver_resp("V6", v).status_code == 200)

# ---------------------------------------------------------------- 7. 查询端点与错误
print("7) 版本查询端点：索引 / latest / 指定版本；未知与非法输入报错")
check("latest 等于当前最新版本快照",
      latest("V6")["version"] == 5 and latest("V6")["change_type"] == "approval")
check("latest 与按号查询一致", latest("V3")["timeline"] == ver("V3", 2)["timeline"])
r = client.get(f"{API}/samples/UNKNOWN-XX/versions")
check("未知样本版本索引 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
r = client.get(f"{API}/samples/UNKNOWN-XX/versions/0")
check("未知样本指定版本 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
r = ver_resp("V6", 99)
check("不存在版本 404 version_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "version_not_found", r.text)
check("错误详情带当前最新版本号", r.json()["error"]["details"]["current_version"] == 5, r.text)
r = ver_resp("V6", "x")
check("非数字版本号 404 version_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "version_not_found", r.text)
r = ver_resp("V6", "-1")
check("负数版本号 404 version_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "version_not_found", r.text)

# ---------------------------------------------------------------- 8. 快照完整性
print("8) 版本快照字段完整（原因/在途移交/是否入库等）")
reg("V8")
ev({"event_id": "V8-T1", "event_type": "transfer", "sample_code": "V8",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-V8",
    "from_custodian": "张三", "to_custodian": "李四"})
s = ver("V8", 1)
check("快照含 pending 原因", s["status"] == "pending" and any("缺少签收" in x for x in s["reasons"]), str(s["reasons"]))
check("快照含在途移交", s["pending_transfer"] and s["pending_transfer"]["transfer_no"] == "TR-V8")
check("快照 has_storage=False", s["has_storage"] is False)
check("快照无 superseded/corrections", s["superseded_events"] == [] and s["corrections"] == [])
check("chain 与样本摘要响应附 current_version",
      client.get(f"{API}/samples/V8/chain").json()["current_version"] == 1
      and client.get(f"{API}/samples/V8").json()["evaluation"]["current_version"] == 1
      and any(x["sample_code"] == "V8" and x["current_version"] == 1
              for x in client.get(f"{API}/samples").json()["items"]))

# ---------------------------------------------------------------- 9. 旧库迁移基线
print("9) 升级前已有样本：迁移时以当时状态补建版本 0，不推断更早历史")
old_dir = tempfile.mkdtemp()
old_db = os.path.join(old_dir, "legacy.db")
con = sqlite3.connect(old_db)
con.executescript(
    """
CREATE TABLE sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sample_code TEXT NOT NULL UNIQUE,
    collected_at TEXT NOT NULL, collector TEXT NOT NULL, seal_no TEXT NOT NULL,
    content_hash TEXT NOT NULL, approved_storage_at TEXT, approved_by TEXT,
    approve_note TEXT, created_at TEXT NOT NULL);
CREATE TABLE custody_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL, created_at TEXT NOT NULL, superseded_by TEXT,
    replaces_event_id TEXT, batch_id TEXT);
CREATE TABLE event_correction (
    id INTEGER PRIMARY KEY AUTOINCREMENT, correction_id TEXT NOT NULL UNIQUE, sample_id INTEGER NOT NULL,
    target_event_id TEXT NOT NULL, action TEXT NOT NULL, operator TEXT NOT NULL, reason TEXT NOT NULL,
    new_event_id TEXT, new_event_row_id INTEGER, request_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE transfer_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL,
    from_custodian TEXT NOT NULL, to_custodian TEXT NOT NULL, item_count INTEGER NOT NULL,
    request_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE transfer_batch_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_pk INTEGER NOT NULL, sample_id INTEGER NOT NULL,
    sample_code TEXT NOT NULL, transfer_no TEXT NOT NULL, event_id TEXT NOT NULL,
    event_row_id INTEGER NOT NULL, item_index INTEGER NOT NULL);
INSERT INTO sample VALUES
 (1,'OLD-A','2026-01-01T00:00:00Z','甲','SL-A','h',NULL,NULL,NULL,'2026-01-01T00:00:00Z'),
 (2,'OLD-B','2026-01-01T00:00:00Z','乙','SL-B','h','2026-03-01T00:00:00Z','主管','ok','2026-01-01T00:00:00Z');
"""
)
con.commit()
con.close()

orig_db_path = config.DB_PATH
config.DB_PATH = type(config.DB_PATH)(old_db)
try:
    init_db()  # 迁移：建 sample_version 并补基线
    with db_session() as old_conn:
        from app.versions import get_version as gv, list_versions as lv, ensure_version_baselines
        for code in ("OLD-A", "OLD-B"):
            info = lv(old_conn, code)
            check(f"{code} 仅补建 1 个版本", info["total"] == 1 and info["current_version"] == 0, str(info))
            snap = gv(old_conn, code, 0)
            check(f"{code} v0 change_type=baseline", snap["change_type"] == "baseline")
        check("OLD-B 基线快照保留升级时核定信息",
              gv(old_conn, "OLD-B", 0)["approved_by"] == "主管")
        # 幂等：再次补建不产生重复基线
        created = ensure_version_baselines(old_conn)
        check("基线补建幂等（第二次 0 条）", created == 0, str(created))
finally:
    config.DB_PATH = orig_db_path

# 迁移后在旧库上继续提交，版本从 1 递增（旧库写路径独立验证服务层）
config.DB_PATH = type(config.DB_PATH)(old_db)
try:
    from app.schemas import EventIn
    from app.service import create_event, get_sample_or_404
    from app.versions import list_versions as lv2
    with db_session(immediate=True) as conn:
        srow = get_sample_or_404(conn, "OLD-A")
        _, idem = create_event(conn, EventIn(
            event_id="OLD-A-E1", event_type="transfer", sample_code="OLD-A",
            occurred_at="2026-02-01T00:00:00Z", transfer_no="TR-OLD",
            from_custodian="甲", to_custodian="丙"))
        check("旧库迁移后新事件正常写入", not idem)
        check("旧库版本从 1 继续（基线 0 保留）",
              [i["version"] for i in lv2(conn, "OLD-A")["items"]] == [0, 1])
finally:
    config.DB_PATH = orig_db_path

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
