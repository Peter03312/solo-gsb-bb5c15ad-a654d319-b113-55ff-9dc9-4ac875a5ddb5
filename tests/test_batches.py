"""端到端自测：同一交接车次多份样本的批量移交（transfer batch）。

运行：python -m tests.test_batches
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_batches.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

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


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


def batch(body):
    return client.post(f"{API}/transfer-batches", json=body)


def get_batch(bid):
    return client.get(f"{API}/transfer-batches/{bid}")


def batch_body(bid="BATCH-1", when="2026-09-30T02:00:00Z", frm="张三", to="李四", items=None):
    return {
        "batch_id": bid, "occurred_at": when,
        "from_custodian": frm, "to_custodian": to,
        "items": items if items is not None else [
            {"sample_code": "B1", "transfer_no": "TR-A", "event_id": "B-EVT-1"},
            {"sample_code": "B2", "transfer_no": "TR-B", "event_id": "B-EVT-2"},
            {"sample_code": "B3", "transfer_no": "TR-C", "event_id": "B-EVT-3"},
        ],
    }


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------- 1. 正常整批提交
print("1) 同一车次多份样本整批提交：201，各份成为既有 transfer 事件")
for c in ("B1", "B2", "B3"):
    reg(c)
r = batch(batch_body())
check("提交 201", r.status_code == 201, r.text)
b = r.json()
check("顶层 idempotent=false", b["idempotent"] is False, r.text)
hb = b["batch"]
check("批次头回显（批次号/统一时间/交出人/接收人/份数）",
      hb["batch_id"] == "BATCH-1" and hb["occurred_at"] == "2026-09-30T02:00:00Z"
      and hb["from_custodian"] == "张三" and hb["to_custodian"] == "李四"
      and hb["item_count"] == 3, str(hb))
check("逐份顺序与字段回显",
      [(i["index"], i["sample_code"], i["transfer_no"], i["event_id"]) for i in hb["items"]]
      == [(0, "B1", "TR-A", "B-EVT-1"), (1, "B2", "TR-B", "B-EVT-2"),
          (2, "B3", "TR-C", "B-EVT-3")], str(hb["items"]))
check("原事件均有效、current_event_id 指向自身",
      all(i["event_effective"] is True and i["event_effective"]
          and i["current_event_id"] == i["event_id"] and i["superseded_by"] is None
          for i in hb["items"]), str(hb["items"]))
for i, code in enumerate(("B1", "B2", "B3")):
    ev_item = hb["items"][i]
    check(f"{code} 提交后 pending（已移交缺签收/入库），在途单号正确，保管人不变",
          ev_item["evaluation"]["status"] == "pending"
          and ev_item["evaluation"]["pending_transfer"]["transfer_no"]
          == ev_item["transfer_no"]
          and ev_item["evaluation"]["current_custodian"] == "张三",
          str(ev_item["evaluation"]))
# 各份移交确实作为既有 transfer 事件进入各样本链
for code, no, eid in (("B1", "TR-A", "B-EVT-1"), ("B2", "TR-B", "B-EVT-2"),
                      ("B3", "TR-C", "B-EVT-3")):
    c = chain(code)
    check(f"{code} 时间线为 1 条 transfer，携带 batch_id 归属",
          len(c["timeline"]) == 1 and c["timeline"][0]["event_type"] == "transfer"
          and c["timeline"][0]["event_id"] == eid
          and c["timeline"][0]["payload"]["transfer_no"] == no
          and c["timeline"][0]["batch_id"] == "BATCH-1", str(c["timeline"]))

# 批次查询接口
r = get_batch("BATCH-1")
check("批次查询 200 且与提交视图一致",
      r.status_code == 200 and r.json()["batch"]["item_count"] == 3
      and [i["event_id"] for i in r.json()["batch"]["items"]]
      == ["B-EVT-1", "B-EVT-2", "B-EVT-3"], r.text)

# ---------------------------------------------------------------- 2. 整批失败：不留批次与事件
print("2) 整批失败：批内重复/样本不存在 422，已核定/事件号占用 409，且不留痕")

# 2.1 批内样本编号重复
reg("D1"); reg("D2")
dup = batch_body("BATCH-DUP", items=[
    {"sample_code": "D1", "transfer_no": "TR-D1", "event_id": "DUP-1"},
    {"sample_code": "D1", "transfer_no": "TR-D2", "event_id": "DUP-2"},
])
r = batch(dup)
check("批内样本重复 422 batch_rejected",
      r.status_code == 422 and r.json()["error"]["code"] == "batch_rejected"
      and {x["code"] for x in r.json()["error"]["details"]["items"]} == {"duplicate_sample_in_batch"},
      r.text)
check("失败批次查询 404", get_batch("BATCH-DUP").status_code == 404)
check("失败未写入事件（D1 链为空）", len(chain("D1")["timeline"]) == 0)

# 2.2 批内事件号重复
r = batch(batch_body("BATCH-DUP2", items=[
    {"sample_code": "D1", "transfer_no": "TR-D1", "event_id": "DUP-E"},
    {"sample_code": "D2", "transfer_no": "TR-D2", "event_id": "DUP-E"},
]))
check("批内事件号重复 422",
      r.status_code == 422 and "duplicate_event_in_batch"
      in {x["code"] for x in r.json()["error"]["details"]["items"]}, r.text)
check("失败批次号未被占用（修正后可复用）", get_batch("BATCH-DUP2").status_code == 404)

# 2.3 样本不存在（整批回滚，其他合法份也不写入）
r = batch(batch_body("BATCH-NOPE", items=[
    {"sample_code": "D1", "transfer_no": "TR-OK", "event_id": "NOPE-1"},
    {"sample_code": "GHOST", "transfer_no": "TR-X", "event_id": "NOPE-2"},
]))
check("样本不存在 422 且逐项给出",
      r.status_code == 422
      and {x["code"] for x in r.json()["error"]["details"]["items"]} == {"sample_not_found"},
      r.text)
check("同批合法份未写入（整批失败）", len(chain("D1")["timeline"]) == 0)

# 2.4 任一事件号已被占用（占用方属于另一样本）→ 整批 409
r = batch(batch_body("BATCH-OCC", items=[
    {"sample_code": "D1", "transfer_no": "TR-O1", "event_id": "OCC-1"},
    {"sample_code": "D2", "transfer_no": "TR-O2", "event_id": "B-EVT-1"},  # 已被 BATCH-1 占用
]))
check("事件号已占用 409 batch_rejected/event_id_occupied",
      r.status_code == 409 and r.json()["error"]["code"] == "batch_rejected"
      and "event_id_occupied" in {x["code"] for x in r.json()["error"]["details"]["items"]},
      r.text)
check("占用冲突整批回滚（OCC-1 未写入）", len(chain("D1")["timeline"]) == 0)
check("占用冲突批次不留痕", get_batch("BATCH-OCC").status_code == 404)

# 2.5 样本已核定 → 409；混合错误时冲突优先且错误项聚合返回
reg("LOCK", seal="SEAL-LOCK")
ev({"event_id": "LK-E-T1", "event_type": "transfer", "sample_code": "LOCK",
    "occurred_at": "2026-09-30T01:30:00Z", "transfer_no": "TR-LK",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "LK-E-R1", "event_type": "receive", "sample_code": "LOCK",
    "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-LK",
    "receiver": "李四", "seal_no": "SEAL-LOCK"})
ev({"event_id": "LK-E-C1", "event_type": "seal_check", "sample_code": "LOCK",
    "occurred_at": "2026-09-30T03:00:00Z", "checked_seal_no": "SEAL-LOCK", "seal_intact": True})
ev({"event_id": "LK-E-S1", "event_type": "storage", "sample_code": "LOCK",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
ar = client.post(f"{API}/samples/LOCK/approve", json={"approver": "质量主管"})
check("前置样本核定成功", ar.status_code == 200, ar.text)
r = batch(batch_body("BATCH-LOCKED", when="2026-09-30T04:30:00Z",
                     frm="实验室", to="档案岗", items=[
    {"sample_code": "LOCK", "transfer_no": "TR-L2", "event_id": "LOCKED-1"},
    {"sample_code": "GHOST2", "transfer_no": "TR-G", "event_id": "LOCKED-2"},
]))
codes = {x["code"] for x in r.json().get("error", {}).get("details", {}).get("items", [])}
check("已核定 409，且同时聚合样本不存在（冲突优先 409）",
      r.status_code == 409 and r.json()["error"]["code"] == "batch_rejected"
      and codes == {"sample_approved", "sample_not_found"}, r.text)
check("核定样本未新增事件",
      len(chain("LOCK")["timeline"]) == 4 and get_batch("BATCH-LOCKED").status_code == 404,
      str(len(chain("LOCK")["timeline"])))

# 2.6 数据库层确认无残留
check("transfer_batch 仅 BATCH-1 一条",
      db_scalar("SELECT COUNT(*) FROM transfer_batch") == 1)
check("transfer_batch_item 仅 3 行",
      db_scalar("SELECT COUNT(*) FROM transfer_batch_item") == 3)
check("批量事件 batch_id 仅挂在 BATCH-1 的三份上",
      db_scalar("SELECT COUNT(*) FROM custody_event WHERE batch_id IS NOT NULL") == 3)

# ---------------------------------------------------------------- 3. 幂等与冲突
print("3) 同批次号同内容 200 幂等（顺序无关/时区归一/核定后仍幂等），异内容 409")
r = batch(batch_body())
check("同内容重试 200 且 idempotent=true",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["batch"]["item_count"] == 3, r.text)
check("重试不产生重复事件",
      db_scalar("SELECT COUNT(*) FROM custody_event WHERE batch_id='BATCH-1'") == 3)

# items 顺序不同 → 同内容幂等
reordered = batch_body(items=[
    {"sample_code": "B3", "transfer_no": "TR-C", "event_id": "B-EVT-3"},
    {"sample_code": "B1", "transfer_no": "TR-A", "event_id": "B-EVT-1"},
    {"sample_code": "B2", "transfer_no": "TR-B", "event_id": "B-EVT-2"},
])
check("items 换序仍识别为同内容 200", batch(reordered).status_code == 200)

# 同一时刻不同时区表示 → 归一化后同内容幂等（使用独立批次与样本）
for c in ("TZ1", "TZ2"):
    reg(c)
tz_body = batch_body("BATCH-TZ", when="2026-09-30T10:00:00+08:00", items=[
    {"sample_code": "TZ1", "transfer_no": "TR-TZ1", "event_id": "TZ-1"},
    {"sample_code": "TZ2", "transfer_no": "TR-TZ2", "event_id": "TZ-2"},
])
r1 = batch(tz_body)
check("带偏移时间首次提交 201", r1.status_code == 201, r1.text)
tz_body["occurred_at"] = "2026-09-30T02:00:00Z"
check("等效 UTC 时间重试 200（时间归一化进指纹）", batch(tz_body).status_code == 200)

# 异内容冲突（改移交单号）
diff = batch_body(items=[
    {"sample_code": "B1", "transfer_no": "TR-A-CHANGED", "event_id": "B-EVT-1"},
    {"sample_code": "B2", "transfer_no": "TR-B", "event_id": "B-EVT-2"},
    {"sample_code": "B3", "transfer_no": "TR-C", "event_id": "B-EVT-3"},
])
r = batch(diff)
check("同批次号异内容 409 batch_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "batch_conflict", r.text)
# 改统一移交时间 / 交出人 / 接收人 也属异内容
for mut in (
    lambda b: b.update(occurred_at="2026-09-30T02:30:00Z"),
    lambda b: b.update(from_custodian="王五"),
    lambda b: b.update(to_custodian="赵六"),
):
    b2 = batch_body()
    mut(b2)
    check("批次头任一字段变化均 409", batch(b2).status_code == 409)
check("冲突失败不留新批次/事件",
      db_scalar("SELECT COUNT(*) FROM transfer_batch") == 2
      and db_scalar("SELECT COUNT(*) FROM custody_event WHERE batch_id IS NOT NULL") == 5)

# 核定后同批同内容仍幂等 200；同号异内容仍 409（优先冲突语义）
for code, no in (("B1", "TR-A"),):
    ev({"event_id": f"{code}-R1", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": no,
        "receiver": "李四", "seal_no": f"SEAL-{code}"})
    ev({"event_id": f"{code}-C1", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": f"SEAL-{code}",
        "seal_intact": True})
    ev({"event_id": f"{code}-S1", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
check("B1 补齐后 valid", chain("B1")["status"] == "valid", str(chain("B1")["reasons"]))
check("B1 核定 200",
      client.post(f"{API}/samples/B1/approve", json={"approver": "质量主管"}).status_code == 200)
r = batch(batch_body())
check("核定后同批次号同内容重试仍 200 幂等", r.status_code == 200, r.text)
check("幂等响应带回核定后逐份结论（B1 已核定仍可查）",
      r.json()["batch"]["items"][0]["evaluation"]["status"] == "valid"
      and r.json()["batch"]["items"][0]["evaluation"]["approved_storage_at"] is not None,
      r.text)
r = batch(diff)
check("核定后同批次号异内容仍 409", r.status_code == 409, r.text)

# 既有单事件接口对批量事件号：同内容 200 / 异内容 409
same_single = ev({"event_id": "B-EVT-2", "event_type": "transfer", "sample_code": "B2",
                  "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-B",
                  "from_custodian": "张三", "to_custodian": "李四"})
check("单事件接口：批量事件号同内容 200 幂等", same_single.status_code == 200, same_single.text)
diff_single = ev({"event_id": "B-EVT-2", "event_type": "transfer", "sample_code": "B2",
                  "occurred_at": "2026-09-30T02:01:00Z", "transfer_no": "TR-B",
                  "from_custodian": "张三", "to_custodian": "李四"})
check("单事件接口：批量事件号异内容 409 event_conflict",
      diff_single.status_code == 409
      and diff_single.json()["error"]["code"] == "event_conflict", diff_single.text)

# ---------------------------------------------------------------- 4. 批次查询随纠错更新，原始归属可追溯
print("4) 后续作废/替换只改变有效链与批次查询，批次原始归属可追溯")
# 作废 B2 的原移交：event_effective=false、current_event_id=null
r = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-BATCH-1", "sample_code": "B2",
    "target_event_id": "B-EVT-2", "action": "void",
    "operator": "质控员", "reason": "车次交接后发现该份误发车次，作废移交"})
check("作废批量移交 201", r.status_code == 201, r.text)
b = get_batch("BATCH-1").json()["batch"]
it_b2 = next(i for i in b["items"] if i["sample_code"] == "B2")
it_b3 = next(i for i in b["items"] if i["sample_code"] == "B3")
check("查询反映：B2 原事件失效（event_effective=false, superseded_by 指向纠错）",
      it_b2["event_effective"] is False and it_b2["superseded_by"] == "FIX-BATCH-1"
      and it_b2["current_event_id"] is None, str(it_b2))
check("作废即时重算：B2 无有效事件、保管人回到采集人",
      it_b2["evaluation"]["event_count"] == 0
      and it_b2["evaluation"]["current_custodian"] == "张三", str(it_b2["evaluation"]))
check("B3 不受影响，仍有效/在途",
      it_b3["event_effective"] is True and it_b3["current_event_id"] == "B-EVT-3", str(it_b3))
check("批次头原始归属不变（批次号/份数/统一移交时间）",
      b["batch_id"] == "BATCH-1" and b["item_count"] == 3
      and b["occurred_at"] == "2026-09-30T02:00:00Z", str(b))
# 原事件仍可在链的 superseded_events 追溯，且带批次归属
c = chain("B2")
check("B2 chain 中被作废事件在 superseded_events 可追溯",
      [e["event_id"] for e in c["superseded_events"]] == ["B-EVT-2"]
      and c["superseded_events"][0]["batch_id"] == "BATCH-1", str(c["superseded_events"]))

# 替换 B3 的原移交：新事件进入有效链，查询 current_event_id 指向新版本
r = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-BATCH-2", "sample_code": "B3",
    "target_event_id": "B-EVT-3", "action": "replace",
    "operator": "质控员", "reason": "移交单号录错，整体替换",
    "new_event": {"event_id": "B-EVT-3-FIX", "event_type": "transfer",
                  "occurred_at": "2026-09-30T02:05:00Z",
                  "transfer_no": "TR-C-FIX", "from_custodian": "张三",
                  "to_custodian": "李四"}})
check("替换批量移交 201", r.status_code == 201, r.text)
b = get_batch("BATCH-1").json()["batch"]
it_b3 = next(i for i in b["items"] if i["sample_code"] == "B3")
check("查询反映：B3 原事件失效，current_event_id 指向替换新版本",
      it_b3["event_effective"] is False and it_b3["superseded_by"] == "FIX-BATCH-2"
      and it_b3["current_event_id"] == "B-EVT-3-FIX", str(it_b3))
check("B3 有效链为替换版本且新事件不挂批次（仅原事件归属 BATCH-1）",
      [e["event_id"] for e in chain("B3")["timeline"]] == ["B-EVT-3-FIX"]
      and chain("B3")["timeline"][0]["batch_id"] is None
      and chain("B3")["timeline"][0]["replaces_event_id"] == "B-EVT-3",
      str(chain("B3")["timeline"]))
# 批次明细行数不变（原始归属留痕）
check("批次明细仍为 3 行（原始份数不随纠错增减）",
      db_scalar("SELECT COUNT(*) FROM transfer_batch_item WHERE batch_pk="
                " (SELECT id FROM transfer_batch WHERE batch_id='BATCH-1')") == 3)

# ---------------------------------------------------------------- 5. 批量移交纳入既有核验规则
print("5) 批量移交即既有 transfer：受保管人/在途规则约束，逐样本独立")
reg("E1", collector="钱采集")
# from_custodian 不是当前保管人（应为 钱采集）→ 该样本结论 invalid，但提交本身成功
r = batch(batch_body("BATCH-RULE", items=[
    {"sample_code": "E1", "transfer_no": "TR-E1", "event_id": "RULE-1"}]))
check("非保管人移交：提交成功但核验 invalid（断链）",
      r.status_code == 201
      and r.json()["batch"]["items"][0]["evaluation"]["status"] == "invalid"
      and any("不是当前保管人" in x for x in
              r.json()["batch"]["items"][0]["evaluation"]["reasons"]), r.text)

reg("F1"); reg("F2")
r = batch(batch_body("BATCH-MIX", items=[
    {"sample_code": "F1", "transfer_no": "TR-F1", "event_id": "MIX-1"},
    {"sample_code": "F2", "transfer_no": "TR-F2", "event_id": "MIX-2"},
]))
check("同批次跨样本独立判定", r.status_code == 201)
# F1 再走单事件补一份在途移交 → 在途再移交 invalid；F2 保持 pending 不受影响
ev({"event_id": "MIX-1B", "event_type": "transfer", "sample_code": "F1",
    "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-F1-B",
    "from_custodian": "张三", "to_custodian": "李四"})
check("F1 在途再移交 invalid；F2 仍 pending",
      chain("F1")["status"] == "invalid" and chain("F2")["status"] == "pending")
b = get_batch("BATCH-MIX").json()["batch"]
check("批次查询逐份反映不同结论",
      {(i["sample_code"], i["evaluation"]["status"]) for i in b["items"]}
      == {("F1", "invalid"), ("F2", "pending")}, str(b["items"]))

# ---------------------------------------------------------------- 6. 并发
print("6) 并发：同批同内容一建一幂等；异批事件号交叠一胜整批回滚；批次与核定按顺序")

# 6.1 同批次号同内容并发
for c in ("G1", "G2", "G3"):
    reg(c)
body_g = batch_body("BATCH-CONC", items=[
    {"sample_code": "G1", "transfer_no": "TR-G1", "event_id": "CONC-1"},
    {"sample_code": "G2", "transfer_no": "TR-G2", "event_id": "CONC-2"},
    {"sample_code": "G3", "transfer_no": "TR-G3", "event_id": "CONC-3"},
])
results_g: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_g(tag):
    barrier.wait()
    results_g[tag] = batch(body_g).status_code


t1 = threading.Thread(target=post_g, args=("A",))
t2 = threading.Thread(target=post_g, args=("B",))
t1.start(); t2.start(); t1.join(); t2.join()
check("同批同内容并发：一次 201 一次 200",
      sorted(results_g.values()) == [200, 201], str(results_g))
check("并发后仅一个批次、三份事件",
      db_scalar("SELECT COUNT(*) FROM transfer_batch WHERE batch_id='BATCH-CONC'") == 1
      and db_scalar("SELECT COUNT(*) FROM custody_event WHERE batch_id='BATCH-CONC'") == 3)

# 6.2 不同批次事件号交叠：仅一个整批成功，落败方整批回滚
for c in ("H1", "H2", "H3", "H4"):
    reg(c)
body_ha = batch_body("BATCH-HA", items=[
    {"sample_code": "H1", "transfer_no": "TR-H1", "event_id": "SHARED-EVT"},
    {"sample_code": "H2", "transfer_no": "TR-H2", "event_id": "HA-ONLY"},
])
body_hb = batch_body("BATCH-HB", items=[
    {"sample_code": "H3", "transfer_no": "TR-H3", "event_id": "SHARED-EVT"},
    {"sample_code": "H4", "transfer_no": "TR-H4", "event_id": "HB-ONLY"},
])
results_h: dict[str, int] = {}
barrier2 = threading.Barrier(2)


def post_h(tag, body):
    barrier2.wait()
    results_h[tag] = batch(body).status_code


t1 = threading.Thread(target=post_h, args=("A", body_ha))
t2 = threading.Thread(target=post_h, args=("B", body_hb))
t1.start(); t2.start(); t1.join(); t2.join()
check("交叠批次：一个 201 一个 409",
      sorted(results_h.values()) == [201, 409], str(results_h))
winner = "A" if results_h["A"] == 201 else "B"
loser_only_sample, loser_only_evt = ("H2", "HA-ONLY") if winner == "B" else ("H4", "HB-ONLY")
check("落败批次整批回滚（其独有事件未落库）",
      len(chain(loser_only_sample)["timeline"]) == 0, loser_only_sample)
check("落败批次不留批次头",
      db_scalar("SELECT COUNT(*) FROM transfer_batch WHERE batch_id=?",
                ("BATCH-HA" if winner == "B" else "BATCH-HB",)) == 0)
check("获胜批次两份事件齐全",
      db_scalar("SELECT COUNT(*) FROM custody_event WHERE event_id IN ('SHARED-EVT',?)",
                ("HB-ONLY" if winner == "B" else "HA-ONLY",)) == 2)

# 6.3 批次提交 vs 入库核定：按已提交顺序判定（两种顺序各做一次确定性验证 + 一次真实并发）
def make_valid_sample(code, seal):
    reg(code, collector="张三", seal=seal)
    ev({"event_id": f"{code}-T1", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T01:30:00Z", "transfer_no": f"TR-{code}",
        "from_custodian": "张三", "to_custodian": "李四"})
    ev({"event_id": f"{code}-R1", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": f"TR-{code}",
        "receiver": "李四", "seal_no": seal})
    ev({"event_id": f"{code}-C1", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T03:00:00Z", "checked_seal_no": seal, "seal_intact": True})
    ev({"event_id": f"{code}-S1", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
    check(f"{code} 赛前 valid", chain(code)["status"] == "valid")


# 顺序 A（确定性）：valid → 批次先提交（新增在途移交）→ 核定基于最终链 pending 拒绝 422
make_valid_sample("SEQ-A", "SEAL-SA")
r = batch(batch_body("BATCH-SEQA", when="2026-09-30T05:00:00Z",
                     frm="实验室", to="档案岗",
                     items=[{"sample_code": "SEQ-A", "transfer_no": "TR-SA2",
                             "event_id": "SEQA-1"}]))
check("顺序A：批次先提交 201", r.status_code == 201, r.text)
r = client.post(f"{API}/samples/SEQ-A/approve", json={"approver": "质量主管"})
check("顺序A：核定基于批次后最终链（pending）拒绝 422",
      r.status_code == 422 and r.json()["error"]["code"] == "approval_rejected", r.text)
check("顺序A：批次查询在，样本未核定",
      get_batch("BATCH-SEQA").status_code == 200
      and chain("SEQ-A")["approved_storage_at"] is None)

# 顺序 B（确定性）：valid → 核定先提交 200 冻结 → 批次 409 sample_approved，批次不留痕
make_valid_sample("SEQ-B", "SEAL-SB")
check("顺序B：核定先提交 200",
      client.post(f"{API}/samples/SEQ-B/approve",
                  json={"approver": "质量主管"}).status_code == 200)
r = batch(batch_body("BATCH-SEQB", when="2026-09-30T05:00:00Z",
                     frm="实验室", to="档案岗",
                     items=[{"sample_code": "SEQ-B", "transfer_no": "TR-SB2",
                             "event_id": "SEQB-1"}]))
check("顺序B：核定后批量移交 409 batch_rejected/sample_approved",
      r.status_code == 409 and r.json()["error"]["code"] == "batch_rejected"
      and r.json()["error"]["details"]["items"][0]["code"] == "sample_approved", r.text)
check("顺序B：落败批次不留痕", get_batch("BATCH-SEQB").status_code == 404)

# 真实并发（两种结局均合法，按已提交顺序各自成立）
make_valid_sample("J1", "SEAL-J1")
race_batch = batch_body("BATCH-RACE", when="2026-09-30T05:00:00Z",
                        frm="实验室", to="档案岗",
                        items=[{"sample_code": "J1", "transfer_no": "TR-J2",
                                "event_id": "RACE-1"}])
outcomes: dict[str, int] = {}
barrier3 = threading.Barrier(2)


def race_batch_worker():
    barrier3.wait()
    outcomes["batch"] = batch(race_batch).status_code


def race_approve_worker():
    barrier3.wait()
    outcomes["approve"] = client.post(f"{API}/samples/J1/approve",
                                      json={"approver": "质量主管"}).status_code


t1 = threading.Thread(target=race_batch_worker)
t2 = threading.Thread(target=race_approve_worker)
t1.start(); t2.start(); t1.join(); t2.join()
if outcomes["batch"] == 201:
    check("批次先提交：201", True)
    check("核定基于提交后有效链（新增在途移交→pending）拒绝 422",
          outcomes["approve"] == 422, str(outcomes))
    check("批次查询可见该份",
          get_batch("BATCH-RACE").json()["batch"]["items"][0]["sample_code"] == "J1")
    check("J1 未核定（核定被拒）", chain("J1")["approved_storage_at"] is None)
else:
    check("核定先提交：200 冻结", outcomes["approve"] == 200, str(outcomes))
    check("批次被 409 拒绝（sample_approved）",
          outcomes["batch"] == 409, str(outcomes))
    check("落败批次不留痕", get_batch("BATCH-RACE").status_code == 404)
    check("J1 保持核定态", chain("J1")["approved_storage_at"] is not None)

# ---------------------------------------------------------------- 7. 其他
print("7) 批次不存在 404；载荷校验 422；失败后批次号可复用")
check("批次不存在 404 batch_not_found",
      get_batch("NO-SUCH-BATCH").status_code == 404
      and get_batch("NO-SUCH-BATCH").json()["error"]["code"] == "batch_not_found")

r = batch({"batch_id": "", "occurred_at": "2026-09-30T02:00:00Z",
           "from_custodian": "张三", "to_custodian": "李四", "items": []})
check("空批次号/空 items 422（Pydantic 校验）", r.status_code == 422, r.text)
r = batch({"batch_id": "BATCH-BAD", "occurred_at": "2026-09-30T02:00:00Z",
           "from_custodian": "张三", "to_custodian": "李四",
           "items": [{"sample_code": "B1", "transfer_no": "TR-x"}]})
check("item 缺 event_id 422", r.status_code == 422, r.text)
r = batch({"batch_id": "BATCH-BAD", "occurred_at": "2026-09-30T02:00:00Z",
           "from_custodian": "张三", "to_custodian": "李四",
           "items": [{"sample_code": "B1", "transfer_no": "TR-x",
                      "event_id": "X", "extra": 1}]})
check("item 多余字段 422（extra forbid）", r.status_code == 422, r.text)

# 此前校验/业务失败的批次号可重新提交成功
reg("K1")
r = batch(batch_body("BATCH-RETRY-ID", items=[
    {"sample_code": "K1", "transfer_no": "TR-K1", "event_id": "RETRY-ID-1"}]))
check("失败过的批次号可重新提交 201", r.status_code == 201, r.text)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
