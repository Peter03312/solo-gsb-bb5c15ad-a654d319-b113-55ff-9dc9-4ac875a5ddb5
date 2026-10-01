"""端到端自测：案卷封存（同一抽检多份样本保管链版本归档）。

覆盖：
  * 基本封存：至少两份样本、清单按样本编号排序、快照与版本端点一致、摘要生成；
  * 逐份指定非负历史版本 / 未指定取封存事务中最新版本；
  * 摘要可复算：外部按公开口径复算一致；verify 端点重算一致；
  * 同案卷号同请求重试幂等（即使最新版本变化仍返回原卷；items 换序不敏感）；
  * 同案卷号异请求 409 case_conflict；
  * 整卷失败：编号重复 / 样本不存在 / 版本不存在 → 422 case_rejected 且不留案卷，
    案卷号修正后可复用；少于两份 / 负版本等载荷校验 422；
  * 不可变：封存后补录、纠错、核定不改写旧案卷；封存本身不产生样本新版本；
  * 核验：重算摘要返回一致与否，被篡改时如实报不一致且不静默改写；
  * 并发：同号同内容一建一幂等；同号异内容一建一冲突；
  * 未知案卷查询 / 核验 404 case_not_found。

运行：python -m tests.test_casefiles
"""
import hashlib
import json
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_casefiles.db")
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


def transfer(code, eid, no=None, frm="张三", to="李四", when="2026-09-30T02:00:00Z"):
    return ev({"event_id": eid, "event_type": "transfer", "sample_code": code,
               "occurred_at": when, "transfer_no": no or f"TR-{code}",
               "from_custodian": frm, "to_custodian": to})


def cur(code):
    return client.get(f"{API}/samples/{code}/versions").json()["current_version"]


def ver(code, v):
    return client.get(f"{API}/samples/{code}/versions/{v}").json()["version_snapshot"]


def seal(body):
    return client.post(f"{API}/case-files", json=body)


def get_case(cid):
    return client.get(f"{API}/case-files/{cid}")


def verify(cid):
    return client.get(f"{API}/case-files/{cid}/verify")


def recompute_digest(case_id, items):
    """按 README 公开口径外部复算封存摘要。"""
    canonical = {"case_id": case_id, "items": [
        {"sample_code": it["sample_code"], "version": it["version"], "snapshot": it["snapshot"]}
        for it in sorted(items, key=lambda x: x["sample_code"])]}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------- 1. 基本封存：清单排序 / 快照 / 摘要
print("1) 基本封存：指定版本与取最新混合，清单按样本编号排序，快照与版本端点一致")
for c in ("C1-A", "C1-B", "C1-C"):
    reg(c)
transfer("C1-A", "C1-A-E1")
transfer("C1-A", "C1-A-E2", no="TR-C1-A-2", when="2026-09-30T03:00:00Z")   # C1-A → v2
transfer("C1-B", "C1-B-E1")                                                # C1-B → v1
# C1-C 无事件 → 仅 v0
body1 = {"case_id": "CASE-1", "items": [
    {"sample_code": "C1-C"},                    # 未指定 → 最新 v0
    {"sample_code": "C1-A", "version": 1},      # 指定历史版本（非最新 v2）
    {"sample_code": "C1-B"},                    # 未指定 → 最新 v1
]}
r = seal(body1)
check("封存 201 且 idempotent=false", r.status_code == 201 and r.json()["idempotent"] is False, r.text)
cf = r.json()["case_file"]
check("案卷头字段齐全（案卷号/份数/摘要/封存时间）",
      cf["case_id"] == "CASE-1" and cf["item_count"] == 3
      and len(cf["digest"]) == 64 and bool(cf["created_at"]), str(cf.keys()))
check("清单按样本编号排序（与提交顺序无关）",
      [(i["index"], i["sample_code"]) for i in cf["items"]]
      == [(0, "C1-A"), (1, "C1-B"), (2, "C1-C")], str(cf["items"]))
check("指定版本生效（C1-A 封存 v1 而非最新 v2）",
      [i["version"] for i in cf["items"] if i["sample_code"] == "C1-A"] == [1])
check("未指定取封存时点最新版本（C1-B→v1，C1-C→v0）",
      {i["sample_code"]: i["version"] for i in cf["items"] if i["sample_code"] != "C1-A"}
      == {"C1-B": 1, "C1-C": 0})
check("各份快照与版本复核端点一致",
      all(i["snapshot"] == ver(i["sample_code"], i["version"]) for i in cf["items"]))
check("清单含完整快照（结论/保管人/时间线等版本快照字段）",
      all(k in cf["items"][0]["snapshot"] for k in
          ("status", "reasons", "current_custodian", "timeline", "corrections",
           "superseded_events", "approval", "committed_at", "change_type")))

# ---------------------------------------------------------------- 2. 摘要可复算 + verify 一致
print("2) 摘要可复算：外部复算一致；verify 端点重算一致")
check("外部按公开口径复算 == 封存摘要", recompute_digest("CASE-1", cf["items"]) == cf["digest"])
r = verify("CASE-1")
check("verify 200 且一致", r.status_code == 200 and r.json()["consistent"] is True, r.text)
check("verify 返回封存摘要与重算摘要且相等",
      r.json()["stored_digest"] == cf["digest"] and r.json()["recomputed_digest"] == cf["digest"])

# ---------------------------------------------------------------- 3. 幂等重试：最新版本变化仍返回原卷
print("3) 同案卷号同请求重试：即使最新版本变化也返回原卷（items 换序不敏感）")
transfer("C1-B", "C1-B-E2", no="TR-C1-B-2", when="2026-09-30T04:00:00Z")   # C1-B 推进到 v2
check("C1-B 最新版本已推进到 v2", cur("C1-B") == 2)
body1_retry = {"case_id": "CASE-1", "items": [
    {"sample_code": "C1-B"}, {"sample_code": "C1-A", "version": 1}, {"sample_code": "C1-C"}]}
r = seal(body1_retry)
check("同请求重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
cf_retry = r.json()["case_file"]
check("重试返回原卷（C1-B 仍为 v1 而非最新 v2）",
      [i["version"] for i in cf_retry["items"] if i["sample_code"] == "C1-B"] == [1])
check("重试摘要与快照完全不变",
      cf_retry["digest"] == cf["digest"] and cf_retry["items"] == cf["items"])
check("库中仍只有一个 CASE-1 案卷",
      db_scalar("SELECT COUNT(*) FROM case_file WHERE case_id='CASE-1'") == 1)

# ---------------------------------------------------------------- 4. 异请求冲突
print("4) 同案卷号异请求 → 409 case_conflict")
r = seal({"case_id": "CASE-1", "items": [
    {"sample_code": "C1-A", "version": 2}, {"sample_code": "C1-B"}, {"sample_code": "C1-C"}]})
check("指定版本不同 → 409", r.status_code == 409 and r.json()["error"]["code"] == "case_conflict", r.text)
r = seal({"case_id": "CASE-1", "items": [{"sample_code": "C1-A"}, {"sample_code": "C1-B"}]})
check("份数/成员不同 → 409", r.status_code == 409 and r.json()["error"]["code"] == "case_conflict", r.text)
r = seal({"case_id": "CASE-1", "items": [
    {"sample_code": "C1-A", "version": 1}, {"sample_code": "C1-B", "version": 1}, {"sample_code": "C1-C"}]})
check("缺省改显式（同值版本）也属异请求 → 409",
      r.status_code == 409 and r.json()["error"]["code"] == "case_conflict", r.text)
check("冲突后原卷仍可查且不变", get_case("CASE-1").json()["case_file"]["digest"] == cf["digest"])

# ---------------------------------------------------------------- 5. 整卷失败：不留案卷，案卷号可复用
print("5) 整卷失败：编号重复 / 样本不存在 / 版本不存在 → 422 且不留案卷")
r = seal({"case_id": "CASE-DUP", "items": [
    {"sample_code": "C1-A"}, {"sample_code": "C1-B"}, {"sample_code": "C1-A", "version": 0}]})
check("卷内编号重复 422 case_rejected",
      r.status_code == 422 and r.json()["error"]["code"] == "case_rejected", r.text)
check("逐项给出 duplicate_sample_in_case",
      r.json()["error"]["details"]["items"][0]["code"] == "duplicate_sample_in_case"
      and r.json()["error"]["details"]["items"][0]["sample_code"] == "C1-A",
      str(r.json()["error"]["details"]))
r = seal({"case_id": "CASE-NOSAMPLE", "items": [
    {"sample_code": "C1-A"}, {"sample_code": "NO-SUCH"}]})
check("样本不存在 422 且逐项 sample_not_found",
      r.status_code == 422
      and r.json()["error"]["details"]["items"][0]["code"] == "sample_not_found", r.text)
r = seal({"case_id": "CASE-NOVER", "items": [
    {"sample_code": "C1-A", "version": 99}, {"sample_code": "C1-B"}]})
check("版本不存在 422 且逐项 version_not_found（附当前最新版本定位）",
      r.status_code == 422
      and r.json()["error"]["details"]["items"][0]["code"] == "version_not_found"
      and "当前最新版本" in r.json()["error"]["details"]["items"][0]["message"], r.text)
for cid in ("CASE-DUP", "CASE-NOSAMPLE", "CASE-NOVER"):
    rr = get_case(cid)
    check(f"失败案卷 {cid} 不留痕（404 case_not_found）",
          rr.status_code == 404 and rr.json()["error"]["code"] == "case_not_found", rr.text)
check("失败不留案卷头/明细",
      db_scalar("SELECT COUNT(*) FROM case_file WHERE case_id IN ('CASE-DUP','CASE-NOSAMPLE','CASE-NOVER')") == 0
      and db_scalar("SELECT COUNT(*) FROM case_file_item") == 3)  # 仅 CASE-1 的 3 份
r = seal({"case_id": "CASE-DUP", "items": [{"sample_code": "C1-A"}, {"sample_code": "C1-B"}]})
check("失败案卷号修正后可复用（201）", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 6. 载荷校验
print("6) 载荷校验：少于两份 / 负版本 / 多余字段 → 422")
r = seal({"case_id": "CASE-ONE", "items": [{"sample_code": "C1-A"}]})
check("少于两份 422", r.status_code == 422, r.text)
r = seal({"case_id": "CASE-NEG", "items": [
    {"sample_code": "C1-A", "version": -1}, {"sample_code": "C1-B"}]})
check("负版本 422", r.status_code == 422, r.text)
r = seal({"case_id": "CASE-EXTRA", "items": [
    {"sample_code": "C1-A"}, {"sample_code": "C1-B"}], "foo": 1})
check("多余字段 422", r.status_code == 422, r.text)
check("空案卷号 422", seal({"case_id": "", "items": [
    {"sample_code": "C1-A"}, {"sample_code": "C1-B"}]}).status_code == 422)
check("校验失败不留案卷", get_case("CASE-ONE").status_code == 404
      and get_case("CASE-NEG").status_code == 404 and get_case("CASE-EXTRA").status_code == 404)

# ---------------------------------------------------------------- 7. 不可变：后续补录/纠错/核定不改写旧案卷
print("7) 封存后补录、纠错、核定不改变旧案卷；封存本身不产生样本新版本")
# 7.1 封存只读取档，不产生样本新版本
reg("C7-A"); reg("C7-B")
transfer("C7-A", "C7-A-E1")
before = {"C7-A": cur("C7-A"), "C7-B": cur("C7-B")}
r = seal({"case_id": "CASE-7", "items": [{"sample_code": "C7-A"}, {"sample_code": "C7-B"}]})
check("封存 201", r.status_code == 201, r.text)
check("封存不产生样本新版本",
      cur("C7-A") == before["C7-A"] and cur("C7-B") == before["C7-B"])
cf7 = r.json()["case_file"]

# 7.2 补录事件（C7-A 推进版本）
transfer("C7-A", "C7-A-E2", no="TR-C7-A-2", when="2026-09-30T03:00:00Z")
# 7.3 纠错（作废 C7-A 的移交）
r = client.post(f"{API}/corrections", json={
    "correction_id": "C7-FIX1", "sample_code": "C7-A", "target_event_id": "C7-A-E1",
    "action": "void", "operator": "质控员", "reason": "误报作废"})
check("纠错成功", r.status_code == 201, r.text)
# 7.4 核定（另建 valid 样本封存后核定）
reg("C7-C", seal="SEAL-C7-C")
ev({"event_id": "C7-C-T1", "event_type": "transfer", "sample_code": "C7-C",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-C7-C",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "C7-C-R1", "event_type": "receive", "sample_code": "C7-C",
    "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": "TR-C7-C",
    "receiver": "李四", "seal_no": "SEAL-C7-C"})
ev({"event_id": "C7-C-S1", "event_type": "storage", "sample_code": "C7-C",
    "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"})
r = seal({"case_id": "CASE-7B", "items": [{"sample_code": "C7-C"}, {"sample_code": "C7-B"}]})
check("第二卷封存 201", r.status_code == 201, r.text)
cf7b = r.json()["case_file"]
r = client.post(f"{API}/samples/C7-C/approve", json={"approver": "质量主管"})
check("封存后核定成功", r.status_code == 200, r.text)

check("CASE-7 案卷头与摘要不因补录/纠错改变",
      get_case("CASE-7").json()["case_file"]["digest"] == cf7["digest"])
check("CASE-7 各份快照逐字节不变",
      get_case("CASE-7").json()["case_file"]["items"] == cf7["items"])
check("CASE-7B 摘要不因核定改变",
      get_case("CASE-7B").json()["case_file"]["digest"] == cf7b["digest"])
check("CASE-7B 中 C7-C 快照仍是核定前版本（approval 为 null）",
      [i for i in get_case("CASE-7B").json()["case_file"]["items"]
       if i["sample_code"] == "C7-C"][0]["snapshot"]["approval"] is None)
check("两卷核验仍一致",
      verify("CASE-7").json()["consistent"] is True
      and verify("CASE-7B").json()["consistent"] is True)

# ---------------------------------------------------------------- 8. 核验：篡改如实报不一致，不静默改写
print("8) 核验重算：内容被篡改时返回不一致，且不静默改写案卷")
with db_session(immediate=True) as conn:
    row = conn.execute("SELECT id, snapshot_json FROM case_file_item"
                       " WHERE sample_code='C7-B' AND case_pk=(SELECT id FROM case_file WHERE case_id='CASE-7')").fetchone()
    tampered = json.loads(row["snapshot_json"])
    tampered["status"] = "valid"  # 篡改封存快照
    conn.execute("UPDATE case_file_item SET snapshot_json=? WHERE id=?",
                 (json.dumps(tampered, ensure_ascii=False), row["id"]))
r = verify("CASE-7")
check("篡改后 verify 返回不一致", r.status_code == 200 and r.json()["consistent"] is False, r.text)
check("verify 暴露重算摘要与封存摘要不同",
      r.json()["recomputed_digest"] != r.json()["stored_digest"]
      and r.json()["stored_digest"] == cf7["digest"])
check("不静默改写：库中封存摘要仍原值",
      db_scalar("SELECT digest FROM case_file WHERE case_id='CASE-7'") == cf7["digest"])
check("不静默改写：查询仍返回被篡改内容（如实呈现，不回滚不修复）",
      [i for i in get_case("CASE-7").json()["case_file"]["items"]
       if i["sample_code"] == "C7-B"][0]["snapshot"]["status"] == "valid")
# 恢复篡改，避免影响后续用例
with db_session(immediate=True) as conn:
    orig = [i for i in cf7["items"] if i["sample_code"] == "C7-B"][0]["snapshot"]
    conn.execute("UPDATE case_file_item SET snapshot_json=?"
                 " WHERE case_pk=(SELECT id FROM case_file WHERE case_id='CASE-7') AND sample_code='C7-B'",
                 (json.dumps(orig, ensure_ascii=False, separators=(",", ":")),))
check("恢复后核验重新一致", verify("CASE-7").json()["consistent"] is True)

# ---------------------------------------------------------------- 9. 并发
print("9) 并发：同号同内容一建一幂等；同号异内容一建一冲突")
for c in ("C9-A", "C9-B", "C9-C"):
    reg(c)
body9 = {"case_id": "CASE-CONC", "items": [{"sample_code": "C9-A"}, {"sample_code": "C9-B"}]}
results: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_case(tag, body):
    barrier.wait()
    results[tag] = seal(body).status_code


t1 = threading.Thread(target=post_case, args=("A", body9))
t2 = threading.Thread(target=post_case, args=("B", body9))
t1.start(); t2.start(); t1.join(); t2.join()
check("同号同内容并发：一次 201 一次 200", sorted(results.values()) == [200, 201], str(results))
check("并发后仅一个案卷、两份明细",
      db_scalar("SELECT COUNT(*) FROM case_file WHERE case_id='CASE-CONC'") == 1
      and db_scalar("SELECT COUNT(*) FROM case_file_item WHERE case_pk="
                    "(SELECT id FROM case_file WHERE case_id='CASE-CONC')") == 2)

body9b = {"case_id": "CASE-CONC2", "items": [{"sample_code": "C9-A"}, {"sample_code": "C9-B"}]}
body9c = {"case_id": "CASE-CONC2", "items": [{"sample_code": "C9-A"}, {"sample_code": "C9-C"}]}
results2: dict[str, int] = {}
barrier2 = threading.Barrier(2)


def post_case2(tag, body):
    barrier2.wait()
    results2[tag] = seal(body).status_code


t1 = threading.Thread(target=post_case2, args=("A", body9b))
t2 = threading.Thread(target=post_case2, args=("B", body9c))
t1.start(); t2.start(); t1.join(); t2.join()
check("同号异内容并发：一次 201 一次 409", sorted(results2.values()) == [201, 409], str(results2))
winner_items = body9b if results2["A"] == 201 else body9c
check("已提交结果成立（胜方内容可查且摘要一致）",
      get_case("CASE-CONC2").status_code == 200
      and sorted(i["sample_code"] for i in get_case("CASE-CONC2").json()["case_file"]["items"])
      == sorted(i["sample_code"] for i in winner_items["items"]))

# ---------------------------------------------------------------- 10. 查询与核验的错误
print("10) 未知案卷查询/核验 → 404 case_not_found")
r = get_case("NO-SUCH-CASE")
check("查询未知案卷 404", r.status_code == 404 and r.json()["error"]["code"] == "case_not_found", r.text)
r = verify("NO-SUCH-CASE")
check("核验未知案卷 404", r.status_code == 404 and r.json()["error"]["code"] == "case_not_found", r.text)

# ---------------------------------------------------------------- 11. 既有接口行为保留
print("11) 既有版本与写入接口行为不受案卷功能影响")
check("样本版本索引正常", client.get(f"{API}/samples/C1-A/versions").status_code == 200)
check("指定版本快照正常", client.get(f"{API}/samples/C1-A/versions/1").status_code == 200)
r = ev({"event_id": "C1-A-E1", "event_type": "transfer", "sample_code": "C1-A",
        "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-C1-A",
        "from_custodian": "张三", "to_custodian": "李四"})
check("事件同号同内容重试仍 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("样本列表/单样本/证据链正常",
      client.get(f"{API}/samples").status_code == 200
      and client.get(f"{API}/samples/C1-A").status_code == 200
      and client.get(f"{API}/samples/C1-A/chain").status_code == 200)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
