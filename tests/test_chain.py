"""端到端自测：通过 FastAPI TestClient 核验全部业务规则。

运行：python -m tests.test_chain
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_chain.db")
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


def reg(code, collector="采集员甲", seal="SEAL-001", when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal
    })


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


# ---------------------------------------------------------------- 1. 登记与初始待补证
print("1) 样本登记，无事件时为待补证（缺入库/链路）")
r = reg("S-001")
check("登记返回 201", r.status_code == 201, r.text)
c = chain("S-001")
check("无事件结论 pending", c["status"] == "pending", str(c))
check("初始保管人=采集人", c["current_custodian"] == "采集员甲")
check("登记重复提交幂等 200", reg("S-001").status_code == 200)
r = client.post(f"{API}/samples", json={
    "sample_code": "S-001", "collected_at": "2026-09-30T02:00:00Z", "collector": "采集员甲", "seal_no": "SEAL-001"
})
check("同编号异内容冲突 409", r.status_code == 409, r.text)

# ---------------------------------------------------------------- 2. 完整有效链
print("2) 移交→签收→封签检查→入库，结论有效")
reg("S-VALID", collector="张三", seal="SEAL-V")
ev({"event_id": "EV-T1", "event_type": "transfer", "sample_code": "S-VALID",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-1",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "EV-R1", "event_type": "receive", "sample_code": "S-VALID",
    "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": "TR-1",
    "receiver": "李四", "seal_no": "SEAL-V"})
ev({"event_id": "EV-C1", "event_type": "seal_check", "sample_code": "S-VALID",
    "occurred_at": "2026-09-30T04:30:00Z", "checked_seal_no": "SEAL-V", "seal_intact": True})
r = ev({"event_id": "EV-S1", "event_type": "storage", "sample_code": "S-VALID",
        "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室", "storage_location": "冷库A"})
c = chain("S-VALID")
check("结论 valid", c["status"] == "valid", str(c["reasons"]))
check("当前保管人=实验室", c["current_custodian"] == "实验室")
check("时间线 4 条且按时间排列",
      [e["occurred_at"] for e in c["timeline"]] == sorted(e["occurred_at"] for e in c["timeline"]) and len(c["timeline"]) == 4)

# ---------------------------------------------------------------- 3. 幂等与冲突
print("3) 事件号同内容重试幂等、异内容冲突")
same = ev({"event_id": "EV-T1", "event_type": "transfer", "sample_code": "S-VALID",
           "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-1",
           "from_custodian": "张三", "to_custodian": "李四"})
check("同内容重试 200 且 idempotent", same.status_code == 200 and same.json()["idempotent"] is True, same.text)
diff = ev({"event_id": "EV-T1", "event_type": "transfer", "sample_code": "S-VALID",
           "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-1",
           "from_custodian": "张三", "to_custodian": "李四"})
check("异内容 409 event_conflict", diff.status_code == 409 and diff.json()["error"]["code"] == "event_conflict", diff.text)

# ---------------------------------------------------------------- 4. 移交须当前保管人发起
print("4) 非当前保管人发起移交 → 无效")
reg("S-BADFROM", collector="张三", seal="SEAL-B1")
ev({"event_id": "BF-T1", "event_type": "transfer", "sample_code": "S-BADFROM",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-B1",
    "from_custodian": "王五", "to_custodian": "李四"})
c = chain("S-BADFROM")
check("结论 invalid", c["status"] == "invalid", str(c))
check("原因含非当前保管人", any("不是当前保管人" in x for x in c["reasons"]), str(c["reasons"]))
check("保管人仍为张三", c["current_custodian"] == "张三")

# ---------------------------------------------------------------- 5. 在途不得再次移交
print("5) 移交至签收期间再次移交 → 无效")
reg("S-RETR", collector="张三", seal="SEAL-RT")
ev({"event_id": "RT-T1", "event_type": "transfer", "sample_code": "S-RETR",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-RT1",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "RT-T2", "event_type": "transfer", "sample_code": "S-RETR",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-RT2",
    "from_custodian": "张三", "to_custodian": "赵六"})
c = chain("S-RETR")
check("结论 invalid", c["status"] == "invalid", str(c))
check("原因含再次移交", any("再次移交" in x for x in c["reasons"]), str(c["reasons"]))
check("在途单仍为 TR-RT1", c["pending_transfer"] and c["pending_transfer"]["transfer_no"] == "TR-RT1", str(c["pending_transfer"]))

# ---------------------------------------------------------------- 6. 签收三要素 + 时间
print("6) 签收匹配移交单号/接收人/封签号且晚于移交")
reg("S-RC", collector="张三", seal="SEAL-RC")
ev({"event_id": "RC-T1", "event_type": "transfer", "sample_code": "S-RC",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-RC",
    "from_custodian": "张三", "to_custodian": "李四"})
# 封签不符
ev({"event_id": "RC-R1", "event_type": "receive", "sample_code": "S-RC",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-RC",
    "receiver": "李四", "seal_no": "WRONG-SEAL"})
c = chain("S-RC")
check("封签不符 invalid", c["status"] == "invalid" and any("封签号不符" in x for x in c["reasons"]), str(c["reasons"]))
# 补录正确签收后仍然无效（无效优先，错误签收记录不可被抵消）
ev({"event_id": "RC-R2", "event_type": "receive", "sample_code": "S-RC",
    "occurred_at": "2026-09-30T03:30:00Z", "transfer_no": "TR-RC",
    "receiver": "李四", "seal_no": "SEAL-RC"})
c = chain("S-RC")
check("错误签收不可被补录抵消，仍 invalid", c["status"] == "invalid", str(c))

# 接收人不匹配
reg("S-RC2", collector="张三", seal="SEAL-RC2")
ev({"event_id": "RC2-T1", "event_type": "transfer", "sample_code": "S-RC2",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-RC2",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "RC2-R1", "event_type": "receive", "sample_code": "S-RC2",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-RC2",
    "receiver": "赵六", "seal_no": "SEAL-RC2"})
c = chain("S-RC2")
check("接收人不匹配 invalid", any("接收人不匹配" in x for x in c["reasons"]), str(c["reasons"]))

# 签收早于移交
reg("S-RC3", collector="张三", seal="SEAL-RC3")
ev({"event_id": "RC3-R1", "event_type": "receive", "sample_code": "S-RC3",
    "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-RC3",
    "receiver": "李四", "seal_no": "SEAL-RC3"})
ev({"event_id": "RC3-T1", "event_type": "transfer", "sample_code": "S-RC3",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-RC3",
    "from_custodian": "张三", "to_custodian": "李四"})
c = chain("S-RC3")
check("签收不晚于移交 invalid", any("不晚于移交时间" in x for x in c["reasons"]), str(c["reasons"]))

# ---------------------------------------------------------------- 7. 重复签收
print("7) 重复签收 → 无效")
reg("S-DUP", collector="张三", seal="SEAL-D")
ev({"event_id": "DUP-T1", "event_type": "transfer", "sample_code": "S-DUP",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-D",
    "from_custodian": "张三", "to_custodian": "李四"})
for eid, t in [("DUP-R1", "2026-09-30T03:00:00Z"), ("DUP-R2", "2026-09-30T04:00:00Z")]:
    ev({"event_id": eid, "event_type": "receive", "sample_code": "S-DUP",
        "occurred_at": t, "transfer_no": "TR-D", "receiver": "李四", "seal_no": "SEAL-D"})
c = chain("S-DUP")
check("重复签收 invalid", any("重复签收" in x for x in c["reasons"]), str(c["reasons"]))

# ---------------------------------------------------------------- 8. 待补证两情形
print("8) 缺签收 / 缺入库 → 待补证")
reg("S-P1", collector="张三", seal="SEAL-P1")
ev({"event_id": "P1-T1", "event_type": "transfer", "sample_code": "S-P1",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-P1",
    "from_custodian": "张三", "to_custodian": "李四"})
c = chain("S-P1")
check("缺签收缺入库 pending", c["status"] == "pending", str(c))
check("原因包含缺签收与缺入库",
      any("缺少签收" in x for x in c["reasons"]) and any("缺少实验室入库" in x for x in c["reasons"]), str(c["reasons"]))
check("待签收期间保管人仍为张三", c["current_custodian"] == "张三")

reg("S-P2", collector="张三", seal="SEAL-P2")
ev({"event_id": "P2-T1", "event_type": "transfer", "sample_code": "S-P2",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-P2",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "P2-R1", "event_type": "receive", "sample_code": "S-P2",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-P2",
    "receiver": "李四", "seal_no": "SEAL-P2"})
c = chain("S-P2")
check("仅缺入库 pending", c["status"] == "pending" and any("缺少实验室入库" in x for x in c["reasons"]), str(c))
check("当前保管人已变为李四", c["current_custodian"] == "李四")

# ---------------------------------------------------------------- 9. 封签检查
print("9) 封签检查不符/破损 → 无效且无效优先")
reg("S-CK", collector="张三", seal="SEAL-CK")
ev({"event_id": "CK-C1", "event_type": "seal_check", "sample_code": "S-CK",
    "occurred_at": "2026-09-30T02:00:00Z", "checked_seal_no": "FAKE", "seal_intact": True})
c = chain("S-CK")
check("封签号不符 invalid", any("封签检查封签号不符" in x for x in c["reasons"]), str(c["reasons"]))
ev({"event_id": "CK-C2", "event_type": "seal_check", "sample_code": "S-CK",
    "occurred_at": "2026-09-30T03:00:00Z", "checked_seal_no": "SEAL-CK", "seal_intact": False})
c = chain("S-CK")
check("封签破损 invalid 且优先于待补证", c["status"] == "invalid" and any("破损" in x for x in c["reasons"]), str(c))

# ---------------------------------------------------------------- 10. 乱序补录重算
print("10) 乱序补录按发生时间重算")
reg("S-OOO", collector="张三", seal="SEAL-OOO")
# 先上报入库和签收（移交还未补录）→ 此刻应 pending/invalid
ev({"event_id": "OOO-S1", "event_type": "storage", "sample_code": "S-OOO",
    "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"})
c = chain("S-OOO")
check("缺移交签收链时 pending", c["status"] == "pending", str(c))
ev({"event_id": "OOO-R1", "event_type": "receive", "sample_code": "S-OOO",
    "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": "TR-OOO",
    "receiver": "李四", "seal_no": "SEAL-OOO"})
c = chain("S-OOO")
check("先签收后移交暂 invalid（无在途移交）", c["status"] == "invalid", str(c))
ev({"event_id": "OOO-T1", "event_type": "transfer", "sample_code": "S-OOO",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-OOO",
    "from_custodian": "张三", "to_custodian": "李四"})
c = chain("S-OOO")
check("补录移交后重算为 valid", c["status"] == "valid", str(c["reasons"]))

# ---------------------------------------------------------------- 11. 核定入库与冻结
print("11) 仅有效链可核定，核定后历史冻结")
r = client.post(f"{API}/samples/S-P2/approve", json={"approver": "质量主管"})
check("待补证链核定被拒 422", r.status_code == 422, r.text)
r = client.post(f"{API}/samples/S-VALID/approve", json={"approver": "质量主管", "note": "准予入库"})
check("有效链核定 200", r.status_code == 200, r.text)
check("核定后标记 approved_storage_at", chain("S-VALID")["approved_storage_at"] is not None)
r = ev({"event_id": "EV-LATE", "event_type": "seal_check", "sample_code": "S-VALID",
        "occurred_at": "2026-09-30T06:00:00Z", "checked_seal_no": "SEAL-V"})
check("核定后追加事件 409 冻结", r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)
r = client.post(f"{API}/samples/S-VALID/approve", json={"approver": "质量主管"})
check("重复核定幂等 200", r.status_code == 200, r.text)

# ---------------------------------------------------------------- 12. 事件早于采集
print("12) 事件早于采集时间 → 无效")
reg("S-EARLY", collector="张三", seal="SEAL-E", when="2026-09-30T10:00:00Z")
ev({"event_id": "EARLY-T1", "event_type": "transfer", "sample_code": "S-EARLY",
    "occurred_at": "2026-09-30T09:00:00Z", "transfer_no": "TR-E",
    "from_custodian": "张三", "to_custodian": "李四"})
c = chain("S-EARLY")
check("早于采集 invalid", any("早于样本采集时间" in x for x in c["reasons"]), str(c["reasons"]))

# ---------------------------------------------------------------- 13. 列表/404/必填校验
print("13) 列表、404、字段校验")
r = client.get(f"{API}/samples")
check("样本列表包含全部样本", r.json()["total"] >= 12, str(r.json()["total"]))
check("未知样本 404", client.get(f"{API}/samples/NOPE").status_code == 404)
r = ev({"event_id": "BAD-1", "event_type": "transfer", "sample_code": "S-001",
        "occurred_at": "2026-09-30T02:00:00Z", "from_custodian": "张三"})
check("transfer 缺必填字段 422", r.status_code == 422, r.text)
r = ev({"event_id": "BAD-2", "event_type": "unknown", "sample_code": "S-001",
        "occurred_at": "2026-09-30T02:00:00Z"})
check("非法事件类型 422", r.status_code == 422)

# ---------------------------------------------------------------- 14. 无效优先于待补证
print("14) 无效 + 待补证并存时无效优先")
reg("S-PRIO", collector="张三", seal="SEAL-PR")
ev({"event_id": "PRIO-T1", "event_type": "transfer", "sample_code": "S-PRIO",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-PR",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "PRIO-T2", "event_type": "transfer", "sample_code": "S-PRIO",
    "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-PR2",
    "from_custodian": "张三", "to_custodian": "赵六"})
c = chain("S-PRIO")
check("结论 invalid 且原因附待补证说明",
      c["status"] == "invalid" and any(x.startswith("另有待补证") for x in c["reasons"]), str(c["reasons"]))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
