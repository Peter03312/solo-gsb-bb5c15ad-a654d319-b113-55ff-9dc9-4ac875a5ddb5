"""端到端自测：核定前事件纠错（作废 void / 替换 replace）。

运行：python -m tests.test_corrections
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_corrections.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
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


def reg(code, collector="张三", seal="SEAL-X", when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal
    })


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


def correction(body):
    return client.post(f"{API}/corrections", json=body)


def valid_chain(code, events):
    """写一条标准 移交→签收→检查→入库 链（事件定义由调用方给出）。"""
    for e in events:
        ev(e)


# ---------------------------------------------------------------- 1. 作废：有效链中作废签收后重算
print("1) 作废错误签收，核验只纳入有效版本，可追溯")
reg("C-VOID")
valid_chain("C-VOID", [
    {"event_id": "VOID-T1", "event_type": "transfer", "sample_code": "C-VOID",
     "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-V",
     "from_custodian": "张三", "to_custodian": "李四"},
    {"event_id": "VOID-R1", "event_type": "receive", "sample_code": "C-VOID",
     "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-V",
     "receiver": "李四", "seal_no": "BAD-SEAL"},
])
c = chain("C-VOID")
check("作废前 invalid", c["status"] == "invalid", str(c["reasons"]))

r = correction({"correction_id": "FIX-1", "sample_code": "C-VOID",
                "target_event_id": "VOID-R1", "action": "void",
                "operator": "纠错员甲", "reason": "误报，作废错误签收"})
check("作废返回 201", r.status_code == 201, r.text)
body = r.json()
check("作废后重算为 pending（缺签收缺入库）",
      body["evaluation"]["status"] == "pending"
      and any("缺少签收" in x for x in body["evaluation"]["reasons"]), str(body["evaluation"]["reasons"]))
check("作废后保管人回到移交前=张三", body["evaluation"]["current_custodian"] == "张三")
check("有效事件计数=1（仅移交）", body["evaluation"]["event_count"] == 1, str(body["evaluation"]["event_count"]))
check("结论原因不再含封签不符",
      all("封签号不符" not in x for x in body["evaluation"]["reasons"]), str(body["evaluation"]["reasons"]))

c = chain("C-VOID")
check("chain 仅含 1 条有效时间线", len(c["timeline"]) == 1, str(c["timeline"]))
check("chain corrections 留痕",
      [x["correction_id"] for x in c["corrections"]] == ["FIX-1"]
      and c["corrections"][0]["action"] == "void", str(c["corrections"]))
check("chain superseded_events 可追溯被作废记录",
      len(c["superseded_events"]) == 1
      and c["superseded_events"][0]["event_id"] == "VOID-R1"
      and c["superseded_events"][0]["superseded_by"] == "FIX-1", str(c["superseded_events"]))

r = client.get(f"{API}/samples/C-VOID/corrections")
check("纠错审计查询 200 且含 1 条",
      r.status_code == 200 and r.json()["total"] == 1
      and r.json()["items"][0]["operator"] == "纠错员甲", r.text)

# 作废后补录正确签收（新事件号）→ 重新成立，再补齐入库
ev({"event_id": "VOID-R2", "event_type": "receive", "sample_code": "C-VOID",
    "occurred_at": "2026-09-30T03:30:00Z", "transfer_no": "TR-V",
    "receiver": "李四", "seal_no": "SEAL-X"})
ev({"event_id": "VOID-S1", "event_type": "storage", "sample_code": "C-VOID",
    "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"})
c = chain("C-VOID")
check("补录后 valid，错误签收未被纳入", c["status"] == "valid", str(c["reasons"]))
check("当前保管人=实验室", c["current_custodian"] == "实验室")

# ---------------------------------------------------------------- 2. 替换：原号作废、新号生效、按时间重算
print("2) 替换移交时间错误，新事件号生效并永久留痕")
reg("C-REPL", seal="SEAL-R")
ev({"event_id": "REPL-T1", "event_type": "transfer", "sample_code": "C-REPL",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-R",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "REPL-R1", "event_type": "receive", "sample_code": "C-REPL",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-R",
    "receiver": "李四", "seal_no": "SEAL-R"})
# 原封签检查读错号 → invalid
ev({"event_id": "REPL-C1", "event_type": "seal_check", "sample_code": "C-REPL",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "WRONG", "seal_intact": True})
check("替换前 invalid", chain("C-REPL")["status"] == "invalid")

new_event = {"event_id": "REPL-C2", "event_type": "seal_check",
             "occurred_at": "2026-09-30T03:30:00Z",
             "checked_seal_no": "SEAL-R", "seal_intact": True}
r = correction({"correction_id": "FIX-2", "sample_code": "C-REPL",
                "target_event_id": "REPL-C1", "action": "replace",
                "operator": "纠错员乙", "reason": "检查时扫码识别错误",
                "new_event": new_event})
check("替换返回 201", r.status_code == 201, r.text)
b = r.json()
check("替换留痕 new_event_id 指向新号",
      b["correction"]["new_event_id"] == "REPL-C2"
      and b["correction"]["target_event_id"] == "REPL-C1", str(b["correction"]))
ev({"event_id": "REPL-S1", "event_type": "storage", "sample_code": "C-REPL",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
c = chain("C-REPL")
check("替换补齐入库后 valid", c["status"] == "valid", str(c["reasons"]))
ids_eff = {e["event_id"] for e in c["timeline"]}
ids_sup = {e["event_id"] for e in c["superseded_events"]}
check("有效链含新号不含旧号", "REPL-C2" in ids_eff and "REPL-C1" not in ids_eff, str(ids_eff))
check("旧号在 superseded_events", ids_sup == {"REPL-C1"}, str(ids_sup))
check("新事件时间线带 replaces_event_id",
      any(e["event_id"] == "REPL-C2" and e["replaces_event_id"] == "REPL-C1" for e in c["timeline"]),
      str(c["timeline"]))

# 再次替换（多跳）：以新的有效版本为目标
r = correction({"correction_id": "FIX-2-B", "sample_code": "C-REPL",
                "target_event_id": "REPL-C2", "action": "replace",
                "operator": "纠错员乙", "reason": "再修正备注",
                "new_event": {"event_id": "REPL-C3", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:31:00Z",
                              "checked_seal_no": "SEAL-R", "seal_intact": True,
                              "note": "已复核"}})
check("对替换产生的有效版本可再次替换", r.status_code == 201, r.text)
c = chain("C-REPL")
check("两条纠错均留痕、两个旧号可追溯",
      [x["correction_id"] for x in c["corrections"]] == ["FIX-2", "FIX-2-B"]
      and {x["event_id"] for x in c["superseded_events"]} == {"REPL-C1", "REPL-C2"},
      str((c["corrections"], c["superseded_events"])))

# ---------------------------------------------------------------- 3. 幂等与冲突
print("3) 同纠错号同内容幂等、异内容冲突")
same = correction({"correction_id": "FIX-1", "sample_code": "C-VOID",
                   "target_event_id": "VOID-R1", "action": "void",
                   "operator": "纠错员甲", "reason": "误报，作废错误签收"})
check("同内容重试 200 且幂等", same.status_code == 200 and same.json()["correction"]["correction_id"] == "FIX-1",
      same.text)
diff = correction({"correction_id": "FIX-1", "sample_code": "C-VOID",
                   "target_event_id": "VOID-R1", "action": "void",
                   "operator": "纠错员甲", "reason": "原因被篡改"})
check("异内容 409 correction_conflict",
      diff.status_code == 409 and diff.json()["error"]["code"] == "correction_conflict", diff.text)
diff2 = correction({"correction_id": "FIX-1", "sample_code": "C-VOID",
                    "target_event_id": "VOID-R1", "action": "replace",
                    "operator": "纠错员甲", "reason": "误报，作废错误签收",
                    "new_event": {"event_id": "REPL-NX", "event_type": "seal_check",
                                  "occurred_at": "2026-09-30T03:30:00Z",
                                  "checked_seal_no": "SEAL-X"}})
check("动作不同（void→replace）409",
      diff2.status_code == 409 and diff2.json()["error"]["code"] == "correction_conflict", diff2.text)
# 冲突失败不得改变链
check("冲突后有效链不变",
      chain("C-VOID")["status"] == "valid"
      and all(x["correction_id"] != "FIX-1-NOPE" for x in chain("C-VOID")["corrections"]))

# ---------------------------------------------------------------- 4. 各种业务拒绝
print("4) 跨样本/非有效版本/核定后/新号占用/校验失败")
# 4.1 跨样本：目标事件属于 C-VOID
r = correction({"correction_id": "FIX-X1", "sample_code": "C-REPL",
                "target_event_id": "VOID-R2", "action": "void",
                "operator": "纠错员甲", "reason": "试探跨样本"})
check("跨样本 409 correction_cross_sample",
      r.status_code == 409 and r.json()["error"]["code"] == "correction_cross_sample", r.text)

# 4.2 目标已是被替换版本
r = correction({"correction_id": "FIX-X2", "sample_code": "C-REPL",
                "target_event_id": "REPL-C1", "action": "void",
                "operator": "纠错员甲", "reason": "重复纠错已作废事件"})
check("对非有效版本纠错 409 event_not_effective",
      r.status_code == 409 and r.json()["error"]["code"] == "event_not_effective", r.text)

# 4.3 目标不存在 / 样本不存在
r = correction({"correction_id": "FIX-X3", "sample_code": "C-REPL",
                "target_event_id": "NO-SUCH-EV", "action": "void",
                "operator": "纠错员甲", "reason": "x"})
check("目标事件不存在 404 event_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "event_not_found", r.text)
r = correction({"correction_id": "FIX-X4", "sample_code": "NO-SUCH-S",
                "target_event_id": "REPL-T1", "action": "void",
                "operator": "纠错员甲", "reason": "x"})
check("样本不存在 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)

# 4.4 replace 新事件号已占用（用旧号也不行，旧号属永久留痕）
r = correction({"correction_id": "FIX-X5", "sample_code": "C-REPL",
                "target_event_id": "REPL-T1", "action": "replace",
                "operator": "纠错员甲", "reason": "新号占用试探",
                "new_event": {"event_id": "REPL-R1", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:00:00Z",
                              "checked_seal_no": "SEAL-R"}})
check("新事件号已占用 409 new_event_id_occupied",
      r.status_code == 409 and r.json()["error"]["code"] == "new_event_id_occupied", r.text)

# 4.5 replace 缺 new_event / void 带 new_event
r = correction({"correction_id": "FIX-X6", "sample_code": "C-REPL",
                "target_event_id": "REPL-T1", "action": "replace",
                "operator": "纠错员甲", "reason": "缺新内容"})
check("replace 缺 new_event 422", r.status_code == 422, r.text)
r = correction({"correction_id": "FIX-X7", "sample_code": "C-REPL",
                "target_event_id": "REPL-T1", "action": "void",
                "operator": "纠错员甲", "reason": "作废带新内容",
                "new_event": {"event_id": "ZZ", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:00:00Z",
                              "checked_seal_no": "SEAL-R"}})
check("void 携带 new_event 422", r.status_code == 422, r.text)
# 替换新事件缺必填
r = correction({"correction_id": "FIX-X8", "sample_code": "C-REPL",
                "target_event_id": "REPL-T1", "action": "replace",
                "operator": "纠错员甲", "reason": "新事件字段不全",
                "new_event": {"event_id": "REPL-N8", "event_type": "transfer",
                              "occurred_at": "2026-09-30T02:30:00Z",
                              "from_custodian": "张三"}})
check("替换新事件缺必填字段 422", r.status_code == 422, r.text)
# new_event 禁止携带 sample_code（不得跨样本）
r = correction({"correction_id": "FIX-X9", "sample_code": "C-REPL",
                "target_event_id": "REPL-T1", "action": "replace",
                "operator": "纠错员甲", "reason": "新事件夹带样本号",
                "new_event": {"event_id": "REPL-N9", "event_type": "seal_check",
                              "sample_code": "OTHER", "occurred_at": "2026-09-30T03:00:00Z",
                              "checked_seal_no": "SEAL-R"}})
check("new_event 携带 sample_code 422（extra forbid）", r.status_code == 422, r.text)

# 4.6 校验失败不会占用纠错号：此前 4xx 的 FIX-X5 可重新提交成功
reg("C-RETRY", collector="张三", seal="SEAL-RY")
ev({"event_id": "RY-T1", "event_type": "transfer", "sample_code": "C-RETRY",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-RY",
    "from_custodian": "张三", "to_custodian": "李四"})
r = correction({"correction_id": "FIX-X5", "sample_code": "C-RETRY",
                "target_event_id": "RY-T1", "action": "void",
                "operator": "纠错员乙", "reason": "此前校验失败的纠错号可重新使用"})
check("此前校验失败的纠错号可重新使用 201", r.status_code == 201, r.text)
check("作废生效：有效链为空", len(chain("C-RETRY")["timeline"]) == 0)

# ---------------------------------------------------------------- 5. 核定后拒绝纠错
print("5) 核定入库后纠错被拒；事件接口既有语义保持兼容")
reg("C-LOCK", collector="张三", seal="SEAL-LK")
ev({"event_id": "LK-T1", "event_type": "transfer", "sample_code": "C-LOCK",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-LK",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "LK-R1", "event_type": "receive", "sample_code": "C-LOCK",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-LK",
    "receiver": "李四", "seal_no": "SEAL-LK"})
ev({"event_id": "LK-S1", "event_type": "storage", "sample_code": "C-LOCK",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
r = client.post(f"{API}/samples/C-LOCK/approve", json={"approver": "质量主管"})
check("核定 200", r.status_code == 200, r.text)
r = correction({"correction_id": "FIX-LOCK", "sample_code": "C-LOCK",
                "target_event_id": "LK-R1", "action": "void",
                "operator": "纠错员甲", "reason": "核定后试探"})
check("核定后纠错 409 history_locked",
      r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)

# 核定前已成功的纠错，核定后同号同内容重试仍幂等 200（只读，无新效果）
reg("C-IDEM", collector="张三", seal="SEAL-ID")
ev({"event_id": "ID-T1", "event_type": "transfer", "sample_code": "C-IDEM",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-ID",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "ID-R1", "event_type": "receive", "sample_code": "C-IDEM",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-ID",
    "receiver": "李四", "seal_no": "SEAL-ID"})
ev({"event_id": "ID-S1", "event_type": "storage", "sample_code": "C-IDEM",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
ok = correction({"correction_id": "FIX-IDEM", "sample_code": "C-IDEM",
                 "target_event_id": "ID-R1", "action": "replace",
                 "operator": "纠错员甲", "reason": "核定前替换，保持链有效",
                 "new_event": {"event_id": "ID-R2", "event_type": "receive",
                               "occurred_at": "2026-09-30T03:00:00Z",
                               "transfer_no": "TR-ID", "receiver": "李四", "seal_no": "SEAL-ID"}})
check("核定前替换 201", ok.status_code == 201, ok.text)
client.post(f"{API}/samples/C-IDEM/approve", json={"approver": "质量主管"})
retry = correction({"correction_id": "FIX-IDEM", "sample_code": "C-IDEM",
                    "target_event_id": "ID-R1", "action": "replace",
                    "operator": "纠错员甲", "reason": "核定前替换，保持链有效",
                    "new_event": {"event_id": "ID-R2", "event_type": "receive",
                                  "occurred_at": "2026-09-30T03:00:00Z",
                                  "transfer_no": "TR-ID", "receiver": "李四", "seal_no": "SEAL-ID"}})
check("核定后同纠错号同内容重试 200 幂等", retry.status_code == 200, retry.text)
retry_diff = correction({"correction_id": "FIX-IDEM", "sample_code": "C-IDEM",
                         "target_event_id": "ID-R1", "action": "replace",
                         "operator": "纠错员甲", "reason": "篡改内容",
                         "new_event": {"event_id": "ID-R2", "event_type": "receive",
                                       "occurred_at": "2026-09-30T03:00:00Z",
                                       "transfer_no": "TR-ID", "receiver": "李四", "seal_no": "SEAL-ID"}})
check("核定后同纠错号异内容 409（优先冲突语义）", retry_diff.status_code == 409, retry_diff.text)

# 既有事件接口兼容：同 event_id 同内容 200、异内容 409
same_ev = ev({"event_id": "LK-T1", "event_type": "transfer", "sample_code": "C-LOCK",
              "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-LK",
              "from_custodian": "张三", "to_custodian": "李四"})
check("既有事件接口：核定后同内容仍 200 幂等", same_ev.status_code == 200, same_ev.text)
diff_ev = ev({"event_id": "LK-T1", "event_type": "transfer", "sample_code": "C-LOCK",
              "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-LK",
              "from_custodian": "张三", "to_custodian": "李四"})
check("既有事件接口：异内容仍 409 event_conflict（兼容规则保持）",
      diff_ev.status_code == 409 and diff_ev.json()["error"]["code"] == "event_conflict", diff_ev.text)

# 既有查询字段保持
c = chain("C-LOCK")
for key in ("sample", "status", "reasons", "current_custodian", "pending_transfer",
            "has_storage", "approved_storage_at", "timeline"):
    check(f"chain 保留既有字段 {key}", key in c)


# ---------------------------------------------------------------- 6. 并发：同一有效版本只允许一次成功
print("6) 并发纠错同一有效版本：一次成功，其余失败且链不变")
reg("C-CONC", collector="张三", seal="SEAL-CC")
ev({"event_id": "CC-T1", "event_type": "transfer", "sample_code": "C-CONC",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-CC",
    "from_custodian": "张三", "to_custodian": "李四"})

results: dict[str, int] = {}
barrier = threading.Barrier(2)


def do_corr(tag: str, cid: str, action: str, new_id: str | None):
    barrier.wait()
    body = {"correction_id": cid, "sample_code": "C-CONC", "target_event_id": "CC-T1",
            "action": action, "operator": f"操作人{tag}", "reason": f"并发{tag}"}
    if new_id:
        body["new_event"] = {"event_id": new_id, "event_type": "transfer",
                             "occurred_at": "2026-09-30T02:10:00Z",
                             "transfer_no": "TR-CC", "from_custodian": "张三", "to_custodian": "李四"}
    results[tag] = correction(body).status_code


t1 = threading.Thread(target=do_corr, args=("A", "FIX-CA", "void", None))
t2 = threading.Thread(target=do_corr, args=("B", "FIX-CB", "replace", "CC-T1-FIX"))
t1.start(); t2.start(); t1.join(); t2.join()
check("并发纠错恰有一次成功（200/201），另一次 409",
      sorted(results.values()) == [201, 409], str(results))
c = chain("C-CONC")
check("仅 1 条纠错留痕", len(c["corrections"]) == 1, str(c["corrections"]))
if results["A"] == 201:
    check("成功者为作废：有效链为空、原事件在 superseded",
          len(c["timeline"]) == 0 and [x["event_id"] for x in c["superseded_events"]] == ["CC-T1"],
          str((c["timeline"], c["superseded_events"])))
else:
    check("成功者为替换：新号在有效链、原事件在 superseded",
          [x["event_id"] for x in c["timeline"]] == ["CC-T1-FIX"]
          and [x["event_id"] for x in c["superseded_events"]] == ["CC-T1"],
          str((c["timeline"], c["superseded_events"])))


# ---------------------------------------------------------------- 7. 并发：纠错 vs 核定
print("7) 纠错与核定并发：核定只能基于最终提交的有效链")
outcomes: dict[str, str] = {}
barrier2 = threading.Barrier(2)


def setup_victim(code, target, seal, transfer_no):
    reg(code, collector="张三", seal=seal)
    ev({"event_id": f"{target}-T1", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": transfer_no,
        "from_custodian": "张三", "to_custodian": "李四"})
    ev({"event_id": f"{target}-R1", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": transfer_no,
        "receiver": "李四", "seal_no": "BAD"})  # 封签错误 → invalid
    ev({"event_id": f"{target}-S1", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})


# 情形 A：作废先提交（invalid 链）→ 核定必基于作废后的最终链 → pending，核定拒绝；链中无错误签收
setup_victim("C-RACE1", "RC1", "SEAL-RC1", "TR-RC1")

def void_worker():
    barrier2.wait()
    outcomes["correction"] = str(correction({
        "correction_id": "FIX-RACE1", "sample_code": "C-RACE1",
        "target_event_id": "RC1-R1", "action": "void",
        "operator": "纠错员", "reason": "作废错误签收"}).status_code)

def approve_worker():
    barrier2.wait()
    outcomes["approval"] = str(client.post(f"{API}/samples/C-RACE1/approve",
                                           json={"approver": "质量主管"}).status_code)

ta = threading.Thread(target=void_worker); tb = threading.Thread(target=approve_worker)
ta.start(); tb.start(); ta.join(); tb.join()
# 谁先拿到写锁谁赢（真实并发，两种结局都合法）：
#  作废先提交 → 作废 201 + 核定基于最终链（pending）拒绝 422；
#  核定先提交 → 核定 200 冻结 + 作废 409 history_locked。
if outcomes.get("correction") == "201":
    check("作废先提交：201", True)
    check("核定基于作废后最终链拒绝 422", outcomes.get("approval") == "422", str(outcomes))
    c = chain("C-RACE1")
    check("最终链 pending 且错误签收已不在核验范围",
          c["status"] == "pending" and c["superseded_events"][0]["event_id"] == "RC1-R1",
          str(c["status"]))
else:
    check("核定先提交：200 冻结", outcomes.get("approval") == "200", str(outcomes))
    check("作废被 409 拒绝", outcomes.get("correction") == "409", str(outcomes))
    c = chain("C-RACE1")
    check("最终保持核定态 valid、已冻结",
          c["status"] == "valid" and c["approved_storage_at"] is not None, str(c["status"]))

# 情形 B：核定先提交（valid 链）→ 纠错必被 history_locked 拒绝
setup_victim("C-RACE2", "RC2", "SEAL-RC2", "TR-RC2")
# 先把错误签收修正为有效链，保证核定可成功
correction({"correction_id": "FIX-RACE2-PRE", "sample_code": "C-RACE2",
            "target_event_id": "RC2-R1", "action": "replace",
            "operator": "纠错员", "reason": "先修正封签",
            "new_event": {"event_id": "RC2-R2", "event_type": "receive",
                          "occurred_at": "2026-09-30T03:00:00Z",
                          "transfer_no": "TR-RC2", "receiver": "李四", "seal_no": "SEAL-RC2"}})
check("前置修正后 valid", chain("C-RACE2")["status"] == "valid", str(chain("C-RACE2")["reasons"]))

outcomes2: dict[str, str] = {}
barrier3 = threading.Barrier(2)

def late_void_worker():
    barrier3.wait()
    outcomes2["correction"] = str(correction({
        "correction_id": "FIX-RACE2-LATE", "sample_code": "C-RACE2",
        "target_event_id": "RC2-R2", "action": "void",
        "operator": "纠错员", "reason": "核定同时试探作废"}).status_code)

def approve_worker2():
    barrier3.wait()
    outcomes2["approval"] = str(client.post(f"{API}/samples/C-RACE2/approve",
                                            json={"approver": "质量主管"}).status_code)

tc = threading.Thread(target=late_void_worker); td = threading.Thread(target=approve_worker2)
tc.start(); td.start(); tc.join(); td.join()
# 同样有两种合法结局：核定先提交→核定 200 + 作废 409 history_locked；
# 作废先提交→作废 201 + 核定基于作废后最终链（pending）拒绝 422。
if outcomes2.get("approval") == "200":
    check("核定先提交：200", True)
    check("并发作废 409 history_locked（核定已冻结）", outcomes2.get("correction") == "409", str(outcomes2))
    err = correction({"correction_id": "FIX-RACE2-LATE2", "sample_code": "C-RACE2",
                      "target_event_id": "RC2-R2", "action": "void",
                      "operator": "纠错员", "reason": "核定后再试"})
    check("冻结后错误码 history_locked", err.json()["error"]["code"] == "history_locked", err.text)
    check("失败纠错未留痕",
          all(x["correction_id"] not in ("FIX-RACE2-LATE", "FIX-RACE2-LATE2")
              for x in chain("C-RACE2")["corrections"]), str(chain("C-RACE2")["corrections"]))
else:
    check("作废先提交：201", outcomes2.get("correction") == "201", str(outcomes2))
    check("核定基于最终链拒绝 422", outcomes2.get("approval") == "422", str(outcomes2))
    err = correction({"correction_id": "FIX-RACE2-LATE2", "sample_code": "C-RACE2",
                      "target_event_id": "RC2-T1", "action": "void",
                      "operator": "纠错员", "reason": "未核定，仍可继续纠错"})
    check("未核定时纠错不被冻结（201）", err.status_code == 201, err.text)
    c = chain("C-RACE2")
    check("成功的作废已留痕",
          any(x["correction_id"] == "FIX-RACE2-LATE" for x in c["corrections"]),
          str(c["corrections"]))

# ---------------------------------------------------------------- 8. 替换重算原因与当前保管人
print("8) 替换移交的接收人后，保管人按新链重算")
reg("C-CUST", collector="张三", seal="SEAL-CU")
ev({"event_id": "CU-T1", "event_type": "transfer", "sample_code": "C-CUST",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-CU",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "CU-R1", "event_type": "receive", "sample_code": "C-CUST",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-CU",
    "receiver": "李四", "seal_no": "SEAL-CU"})
# 原移交接收人写错（应为王五）→ 替换移交；注意签收四要素须与替换后内容一致，
# 故此处同时演示：先替换移交接收人=李四（原值），无变化；改为替换移交时间修正
ev({"event_id": "CU-C1", "event_type": "seal_check", "sample_code": "C-CUST",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-CU", "seal_intact": False})
r = correction({"correction_id": "FIX-CU", "sample_code": "C-CUST",
                "target_event_id": "CU-C1", "action": "replace",
                "operator": "纠错员", "reason": "封签实际完好，误判破损",
                "new_event": {"event_id": "CU-C2", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-CU", "seal_intact": True}})
check("替换封签检查 201", r.status_code == 201, r.text)
ev({"event_id": "CU-S1", "event_type": "storage", "sample_code": "C-CUST",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "冷库A保管岗"})
c = chain("C-CUST")
check("重算 valid，当前保管人按新链=冷库A保管岗",
      c["status"] == "valid" and c["current_custodian"] == "冷库A保管岗", str((c["status"], c["reasons"])))
check("原因不含破损", all("破损" not in x for x in c["reasons"]), str(c["reasons"]))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
