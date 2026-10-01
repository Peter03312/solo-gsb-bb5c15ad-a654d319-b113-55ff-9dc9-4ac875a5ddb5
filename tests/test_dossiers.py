"""端到端自测：案卷封存（同一抽检多份样本保管链版本封存）。

覆盖：
  * 基本封存：至少两份不重复样本、逐份指定/缺省版本、清单按样本编号排序、
    快照与版本复核一致、SHA-256 摘要可独立复算；
  * 整卷失败：编号重复 / 样本不存在 / 版本不存在 / 份数不足 / 负数版本 → 不留案卷；
  * 幂等与冲突：同案卷号同内容重试（含换序）幂等且最新版本变化后仍返回原卷；异内容 409；
  * 封存后补录 / 纠错 / 核定不改变旧案卷；
  * 核验：重算摘要一致与否；库内容被篡改时报告不一致且不静默改写；
  * 并发：同号同内容一建一幂等；同号异内容一建一冲突，遵循已提交结果；
  * 既有版本与写入接口行为不受影响。

运行：python -m tests.test_dossiers
"""
import hashlib
import json
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_dossiers.db")
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


def complete_valid(code, seal=None):
    """登记并补齐 移交→签收→检查→入库 的有效链（版本推进到 4）。"""
    seal = seal or f"SEAL-{code}"
    reg(code, seal=seal)
    for payload in [
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
    ]:
        ev(payload)


def cur(code):
    return client.get(f"{API}/samples/{code}/versions").json()["current_version"]


def ver(code, v):
    return client.get(f"{API}/samples/{code}/versions/{v}").json()["version_snapshot"]


def dossier(body):
    return client.post(f"{API}/dossiers", json=body)


def get_dossier(did):
    return client.get(f"{API}/dossiers/{did}")


def verify(did):
    return client.post(f"{API}/dossiers/{did}/verify")


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def recompute(dossier_id, items):
    """按 README 文档化算法独立复算案卷摘要（与实现解耦）。"""
    canonical = {
        "dossier_id": dossier_id,
        "items": [
            {"sample_code": it["sample_code"], "version": it["version"], "snapshot": it["snapshot"]}
            for it in sorted(items, key=lambda x: x["sample_code"])
        ],
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 1. 基本封存
print("1) 基本封存：指定/缺省版本、清单排序、快照一致、摘要可复算")
complete_valid("D1-B")                      # 版本推进到 4
complete_valid("D1-A")
v_a, v_b = cur("D1-A"), cur("D1-B")
check("封存前两样本最新版本均为 4", v_a == 4 and v_b == 4, f"{v_a},{v_b}")
# items 故意乱序提交；D1-A 指定历史版本 2，D1-B 缺省取最新
r = dossier({"dossier_id": "DOSSIER-1", "items": [
    {"sample_code": "D1-B"},
    {"sample_code": "D1-A", "version": 2},
]})
check("封存 201", r.status_code == 201, r.text)
d = r.json()["dossier"]
check("idempotent=false", r.json()["idempotent"] is False)
check("案卷号/份数/创建时间", d["dossier_id"] == "DOSSIER-1" and d["item_count"] == 2 and bool(d["created_at"]))
check("摘要为 64 位十六进制", len(d["digest"]) == 64 and all(c in "0123456789abcdef" for c in d["digest"]))
check("清单按样本编号排序（与提交顺序无关）",
      [it["sample_code"] for it in d["items"]] == ["D1-A", "D1-B"], str(d["items"]))
check("清单序号 0..n-1", [it["index"] for it in d["items"]] == [0, 1])
check("指定版本封存为 v2，缺省取最新 v4",
      d["items"][0]["version"] == 2 and d["items"][1]["version"] == 4, str(d["items"]))
check("快照与版本复核接口一致（D1-A@v2）", d["items"][0]["snapshot"] == ver("D1-A", 2))
check("快照与版本复核接口一致（D1-B@v4）", d["items"][1]["snapshot"] == ver("D1-B", 4))
check("摘要可按文档算法独立复算", recompute("DOSSIER-1", d["items"]) == d["digest"])
# GET 查询返回清单、快照与摘要
r = get_dossier("DOSSIER-1")
check("查询 200 且内容与封存响应一致", r.status_code == 200 and r.json()["dossier"] == d, r.text)

# ---------------------------------------------------------------- 2. 整卷失败不留案卷
print("2) 整卷失败：编号重复 / 样本不存在 / 版本不存在 / 份数不足 / 负数版本")
reg("D2-A"); reg("D2-B")
r = dossier({"dossier_id": "DOSSIER-DUP", "items": [
    {"sample_code": "D2-A"}, {"sample_code": "D2-A", "version": 0}]})
check("编号重复 422 dossier_rejected",
      r.status_code == 422 and r.json()["error"]["code"] == "dossier_rejected", r.text)
check("重复明细标注 duplicate_sample_in_dossier",
      r.json()["error"]["details"]["items"][0]["code"] == "duplicate_sample_in_dossier", r.text)
check("编号重复不留案卷", get_dossier("DOSSIER-DUP").status_code == 404)

r = dossier({"dossier_id": "DOSSIER-NOSAMPLE", "items": [
    {"sample_code": "D2-A"}, {"sample_code": "D2-GHOST"}]})
check("样本不存在 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
check("样本不存在不留案卷", get_dossier("DOSSIER-NOSAMPLE").status_code == 404)

r = dossier({"dossier_id": "DOSSIER-NOVER", "items": [
    {"sample_code": "D2-A"}, {"sample_code": "D2-B", "version": 9}]})
check("版本不存在 404 version_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "version_not_found", r.text)
check("错误详情带当前最新版本号",
      r.json()["error"]["details"]["current_version"] == cur("D2-B"), r.text)
check("版本不存在不留案卷", get_dossier("DOSSIER-NOVER").status_code == 404)

r = dossier({"dossier_id": "DOSSIER-ONE", "items": [{"sample_code": "D2-A"}]})
check("不足两份 422（字段校验）", r.status_code == 422, r.text)
check("份数不足不留案卷", get_dossier("DOSSIER-ONE").status_code == 404)

r = dossier({"dossier_id": "DOSSIER-NEG", "items": [
    {"sample_code": "D2-A", "version": -1}, {"sample_code": "D2-B"}]})
check("负数版本 422（字段校验）", r.status_code == 422, r.text)
check("负数版本不留案卷", get_dossier("DOSSIER-NEG").status_code == 404)
check("失败案卷号均不占用（可复用）",
      dossier({"dossier_id": "DOSSIER-DUP", "items": [
          {"sample_code": "D2-A"}, {"sample_code": "D2-B"}]}).status_code == 201)

# ---------------------------------------------------------------- 3. 幂等与冲突
print("3) 幂等：同号同内容（含换序）重试返回原卷，最新版本变化不影响；异内容 409")
complete_valid("D3-A")                      # v4
complete_valid("D3-B")                      # v4
body3 = {"dossier_id": "DOSSIER-3", "items": [{"sample_code": "D3-A"}, {"sample_code": "D3-B"}]}
r1 = dossier(body3)
check("首次封存 201", r1.status_code == 201, r1.text)
d1 = r1.json()["dossier"]
check("缺省版本取封存时最新（均为 4）",
      [it["version"] for it in d1["items"]] == [4, 4], str(d1["items"]))
# 换序同内容重试 → 幂等
r2 = dossier({"dossier_id": "DOSSIER-3", "items": list(reversed(body3["items"]))})
check("换序同内容重试 200 幂等", r2.status_code == 200 and r2.json()["idempotent"] is True, r2.text)
check("幂等返回原卷（digest 一致）", r2.json()["dossier"] == d1)
# 样本新增版本（补录事件 → v5）
ev({"event_id": "D3-A-T2", "event_type": "transfer", "sample_code": "D3-A",
    "occurred_at": "2026-09-30T06:00:00Z", "transfer_no": "TR-D3-A-2",
    "from_custodian": "实验室", "to_custodian": "王五"})
check("补录后 D3-A 最新版本推进到 5", cur("D3-A") == 5)
# 同一请求重试：即使最新版本已变化，仍返回原卷
r3 = dossier(body3)
check("最新版本变化后同请求重试仍 200 幂等", r3.status_code == 200 and r3.json()["idempotent"] is True, r3.text)
check("返回原卷（版本仍 4，digest 不变）",
      r3.json()["dossier"] == d1 and r3.json()["dossier"]["items"][0]["version"] == 4)
check("库中仅一个 DOSSIER-3", db_scalar("SELECT COUNT(*) FROM dossier WHERE dossier_id='DOSSIER-3'") == 1)
# 异内容冲突：指定版本 vs 缺省
r = dossier({"dossier_id": "DOSSIER-3", "items": [
    {"sample_code": "D3-A", "version": 5}, {"sample_code": "D3-B"}]})
check("同案卷号异内容 409 dossier_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "dossier_conflict", r.text)
# 异内容冲突：样本集合不同
r = dossier({"dossier_id": "DOSSIER-3", "items": [
    {"sample_code": "D3-A"}, {"sample_code": "D2-A"}]})
check("样本集合不同亦 409", r.status_code == 409 and r.json()["error"]["code"] == "dossier_conflict", r.text)
check("冲突后原卷不变", get_dossier("DOSSIER-3").json()["dossier"] == d1)

# ---------------------------------------------------------------- 4. 封存后写入不改变旧案卷
print("4) 后续补录 / 纠错 / 核定不改变旧案卷")
complete_valid("D4-A")                      # v4
complete_valid("D4-B")                      # v4
r = dossier({"dossier_id": "DOSSIER-4", "items": [
    {"sample_code": "D4-A", "version": 3}, {"sample_code": "D4-B"}]})
check("封存 201", r.status_code == 201, r.text)
d4 = r.json()["dossier"]
# 补录事件（D4-A → v5）
ev({"event_id": "D4-A-T2", "event_type": "transfer", "sample_code": "D4-A",
    "occurred_at": "2026-09-30T06:00:00Z", "transfer_no": "TR-D4-A-2",
    "from_custodian": "实验室", "to_custodian": "王五"})
# 纠错替换（D4-B → v5）
r = client.post(f"{API}/corrections", json={
    "correction_id": "D4-FIX1", "sample_code": "D4-B", "target_event_id": "D4-B-C1",
    "action": "replace", "operator": "质控员", "reason": "检查记录更正",
    "new_event": {"event_id": "D4-B-C2", "event_type": "seal_check",
                  "occurred_at": "2026-09-30T04:30:00Z",
                  "checked_seal_no": "SEAL-D4-B", "seal_intact": True}})
check("纠错替换成功", r.status_code == 201, r.text)
# 入库核定（D4-B → v6）
r = client.post(f"{API}/samples/D4-B/approve", json={"approver": "质量主管", "note": "准予入库"})
check("核定成功", r.status_code == 200, r.text)
check("样本版本已推进（5/6）", cur("D4-A") == 5 and cur("D4-B") == 6, f"{cur('D4-A')},{cur('D4-B')}")
r = get_dossier("DOSSIER-4")
check("旧案卷内容完全不变", r.json()["dossier"] == d4)
check("案卷中 D4-B 仍封存 v4（非最新 v6）",
      [it["version"] for it in r.json()["dossier"]["items"]] == [3, 4])
rv = verify("DOSSIER-4")
check("核验一致 match=true", rv.status_code == 200 and rv.json()["match"] is True, rv.text)
check("核验返回双侧摘要", rv.json()["stored_digest"] == d4["digest"]
      and rv.json()["recomputed_digest"] == d4["digest"])

# ---------------------------------------------------------------- 5. 核验：篡改报告不一致且不静默改写
print("5) 核验：内容被篡改时报告不一致，且不静默改写")
complete_valid("D5-A"); complete_valid("D5-B")
r = dossier({"dossier_id": "DOSSIER-5", "items": [{"sample_code": "D5-A"}, {"sample_code": "D5-B"}]})
d5 = r.json()["dossier"]
check("封存 201", r.status_code == 201, r.text)
# 直接改库模拟篡改：替换卷内一份快照
with db_session(immediate=True) as conn:
    conn.execute(
        "UPDATE dossier_item SET snapshot_json = json_set(snapshot_json, '$.status', 'invalid')"
        " WHERE dossier_pk = (SELECT id FROM dossier WHERE dossier_id = 'DOSSIER-5')"
        " AND sample_code = 'D5-A'")
rv = verify("DOSSIER-5")
check("篡改后核验 match=false", rv.status_code == 200 and rv.json()["match"] is False, rv.text)
check("两侧摘要均返回且不同",
      rv.json()["stored_digest"] == d5["digest"]
      and rv.json()["recomputed_digest"] != d5["digest"], rv.text)
check("留存摘要不被改写", get_dossier("DOSSIER-5").json()["dossier"]["digest"] == d5["digest"])
rv2 = verify("DOSSIER-5")
check("再次核验仍不一致（核验不修复存储）", rv2.json()["match"] is False)
# 恢复后核验重新一致
with db_session(immediate=True) as conn:
    conn.execute(
        "UPDATE dossier_item SET snapshot_json = ?"
        " WHERE dossier_pk = (SELECT id FROM dossier WHERE dossier_id = 'DOSSIER-5')"
        " AND sample_code = 'D5-A'",
        (json.dumps(ver("D5-A", 4), ensure_ascii=False, separators=(",", ":")),))
check("恢复原始快照后核验重新一致", verify("DOSSIER-5").json()["match"] is True)

# ---------------------------------------------------------------- 6. 查询与核验的错误
print("6) 不存在案卷的查询/核验报错")
r = get_dossier("DOSSIER-GHOST")
check("查询不存在案卷 404 dossier_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "dossier_not_found", r.text)
r = verify("DOSSIER-GHOST")
check("核验不存在案卷 404 dossier_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "dossier_not_found", r.text)

# ---------------------------------------------------------------- 7. 并发
print("7) 并发：同号同内容一建一幂等；同号异内容一建一冲突，遵循已提交结果")
complete_valid("D7-A"); complete_valid("D7-B"); complete_valid("D7-C")

# 7.1 同案卷号同内容并发
body7 = {"dossier_id": "DOSSIER-CONC", "items": [{"sample_code": "D7-A"}, {"sample_code": "D7-B"}]}
results: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_same(tag):
    barrier.wait()
    results[tag] = dossier(body7).status_code


t1 = threading.Thread(target=post_same, args=("A",))
t2 = threading.Thread(target=post_same, args=("B",))
t1.start(); t2.start(); t1.join(); t2.join()
check("同号同内容并发：一次 201 一次 200", sorted(results.values()) == [200, 201], str(results))
check("并发后仅一个案卷、两份明细",
      db_scalar("SELECT COUNT(*) FROM dossier WHERE dossier_id='DOSSIER-CONC'") == 1
      and db_scalar("SELECT COUNT(*) FROM dossier_item di JOIN dossier d ON di.dossier_pk = d.id"
                    " WHERE d.dossier_id='DOSSIER-CONC'") == 2)

# 7.2 同案卷号异内容并发：先提交者生效，后到者 409
body7a = {"dossier_id": "DOSSIER-CONC2", "items": [{"sample_code": "D7-A"}, {"sample_code": "D7-B"}]}
body7b = {"dossier_id": "DOSSIER-CONC2", "items": [{"sample_code": "D7-A"}, {"sample_code": "D7-C"}]}
results2: dict[str, tuple[int, dict]] = {}
barrier2 = threading.Barrier(2)


def post_diff(tag, body):
    barrier2.wait()
    r = dossier(body)
    results2[tag] = (r.status_code, r.json())


t1 = threading.Thread(target=post_diff, args=("A", body7a))
t2 = threading.Thread(target=post_diff, args=("B", body7b))
t1.start(); t2.start(); t1.join(); t2.join()
codes = sorted(v[0] for v in results2.values())
check("同号异内容并发：一次 201 一次 409", codes == [201, 409], str(results2))
loser = next(v[1] for v in results2.values() if v[0] == 409)
check("落败方为 dossier_conflict", loser.get("error", {}).get("code") == "dossier_conflict", str(loser))
winner_body = body7a if results2["A"][0] == 201 else body7b
stored = get_dossier("DOSSIER-CONC2").json()["dossier"]
check("库中案卷遵循已提交（获胜方）结果",
      [it["sample_code"] for it in stored["items"]]
      == sorted(it["sample_code"] for it in winner_body["items"]), str(stored["items"]))

# ---------------------------------------------------------------- 8. 既有接口行为不受影响
print("8) 既有版本与写入接口行为保持")
check("版本复核仍正常（D4-B 最新 v6）", ver("D4-B", 6)["change_type"] == "approval")
check("样本列表正常", any(x["sample_code"] == "D4-A" for x in client.get(f"{API}/samples").json()["items"]))
r = ev({"event_id": "D8-T1", "event_type": "transfer", "sample_code": "D2-A",
        "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-D2-A",
        "from_custodian": "张三", "to_custodian": "李四"})
check("事件写入 201 且版本递增", r.status_code == 201 and r.json()["evaluation"]["current_version"] == 1, r.text)
check("健康检查可用", client.get("/health").json()["status"] == "ok")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
