"""零依赖（仅标准库）的 HTTP API 调用示例：完整演示登记→移交→签收→检查→入库→查询→核定。

使用前：
    pip install 无额外要求（urllib 为 Python 标准库）
    先启动服务：docker compose up -d   或   python -m uvicorn app.main:app --port 8080
运行：
    python examples/call_api.py
"""
import json
import os
import urllib.error
import urllib.request
import uuid

BASE = os.getenv("API_BASE", "http://127.0.0.1:8080/api/v1")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def show(title: str, status: int, resp: dict, *keys: str) -> None:
    print(f"\n--- {title} [HTTP {status}] ---")
    out = resp
    for k in keys:
        out = out.get(k, {}) if isinstance(out, dict) else {}
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    # 每次运行使用独立编号，脚本可任意重复执行
    run_id = uuid.uuid4().hex[:8]
    code = f"API-DEMO-{run_id}"

    # 1) 样本登记：采集时间、采集人（初始保管人）、封签号
    s, r = call("POST", "/samples", {
        "sample_code": code,
        "collected_at": "2026-09-30T01:00:00Z",
        "collector": "王采集",
        "seal_no": "SEAL-DEMO-1",
    })
    show("样本登记", s, r, "evaluation")

    # 2) 移交：必须由当前保管人发起，指定移交单号与接收人
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("移交（签收前=待补证）", s, r, "evaluation")

    # 3) 签收：单号/接收人/封签号三匹配，且晚于移交
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-R1-{run_id}", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T04:00:00Z",
        "transfer_no": "TR-DEMO-1", "receiver": "李运输", "seal_no": "SEAL-DEMO-1",
    })
    show("签收", s, r, "evaluation")

    # 4) 封签检查
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-C1-{run_id}", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T04:30:00Z",
        "checked_seal_no": "SEAL-DEMO-1", "seal_intact": True,
    })
    show("封签检查", s, r, "evaluation")

    # 5) 实验室入库（缺此节点结论为待补证）
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-S1-{run_id}", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-09-30T05:00:00Z",
        "custodian": "实验室", "storage_location": "冷库A-01",
    })
    show("实验室入库（此时有效）", s, r, "evaluation")

    # 6) 幂等重试：同事件号同内容 → 200
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("同事件号同内容重试（幂等）", s, r, "idempotent")

    # 7) 冲突：同事件号异内容 → 409
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:30:00Z",  # 时间被改动
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("同事件号异内容（冲突）", s, r)

    # 8) 查询完整证据链（按发生时间排列）
    s, r = call("GET", f"/samples/{code}/chain")
    print(f"\n--- 证据链 [HTTP {s}] ---")
    print("结论:", r["status"], "| 当前保管人:", r["current_custodian"])
    for e in r["timeline"]:
        print(" ", e["occurred_at"], e["event_type"], e["event_id"])

    # 8.1) 纠错（替换）：用新事件号替换封签检查，结论按仅有效版本重算
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-C-{run_id}",
        "sample_code": code,
        "target_event_id": f"API-EVT-C1-{run_id}",
        "action": "replace",
        "operator": "质控员",
        "reason": "示例：封签检查记录需补备注，整体替换",
        "new_event": {
            "event_id": f"API-EVT-C2-{run_id}",
            "event_type": "seal_check",
            "occurred_at": "2026-09-30T04:30:00Z",
            "checked_seal_no": "SEAL-DEMO-1",
            "seal_intact": True,
            "note": "纠错替换：复核无误",
        },
    })
    show("纠错-替换（同纠错号同内容重试幂等）", s, r, "correction")
    print("  替换后结论:", r["evaluation"]["status"], "| 保管人:", r["evaluation"]["current_custodian"])

    # 8.2) 同纠错号同内容重试 → 200 幂等；异内容 → 409（此处仅演示幂等）
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-C-{run_id}",
        "sample_code": code,
        "target_event_id": f"API-EVT-C1-{run_id}",
        "action": "replace",
        "operator": "质控员",
        "reason": "示例：封签检查记录需补备注，整体替换",
        "new_event": {
            "event_id": f"API-EVT-C2-{run_id}",
            "event_type": "seal_check",
            "occurred_at": "2026-09-30T04:30:00Z",
            "checked_seal_no": "SEAL-DEMO-1",
            "seal_intact": True,
            "note": "纠错替换：复核无误",
        },
    })
    show("同纠错号同内容重试", s, r, "correction")

    # 8.3) 链查询可追溯被替换记录与纠错关系
    s, r = call("GET", f"/samples/{code}/chain")
    print(f"\n--- 纠错后证据链 [HTTP {s}] ---")
    print("有效时间线:", [e["event_id"] for e in r["timeline"]])
    print("被替换记录:", [e["event_id"] for e in r["superseded_events"]])
    print("纠错留痕:", [(c["correction_id"], c["action"], c["new_event_id"]) for c in r["corrections"]])

    # 9) 核定入库（仅 valid 可核定；核定后冻结，纠错也一并拒绝）
    s, r = call("POST", f"/samples/{code}/approve", {"approver": "质量主管", "note": "准予入库"})
    show("核定入库", s, r, "approval")

    # 9.1) 核定后纠错 → 409 history_locked
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-LATE-{run_id}",
        "sample_code": code,
        "target_event_id": f"API-EVT-T1-{run_id}",
        "action": "void",
        "operator": "质控员",
        "reason": "核定后试探作废",
    })
    show("核定后纠错（拒绝）", s, r)

    # 10) 核定后再写事件 → 409 history_locked
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-LATE-{run_id}", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T06:00:00Z", "checked_seal_no": "SEAL-DEMO-1",
    })
    show("核定后改写（拒绝）", s, r)

    # 11) 作废演示（独立样本）：作废后核验只纳入有效版本，结论即时重算
    code2 = f"API-VOID-{run_id}"
    call("POST", "/samples", {
        "sample_code": code2,
        "collected_at": "2026-09-30T01:00:00Z",
        "collector": "王采集",
        "seal_no": "SEAL-DEMO-2",
    })
    call("POST", "/events", {
        "event_id": f"API-EVT2-T1-{run_id}", "event_type": "transfer", "sample_code": code2,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-2", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    call("POST", "/events", {
        "event_id": f"API-EVT2-R1-{run_id}", "event_type": "receive", "sample_code": code2,
        "occurred_at": "2026-09-30T03:00:00Z",
        "transfer_no": "TR-DEMO-2", "receiver": "李运输", "seal_no": "WRONG-SEAL",
    })
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-V-{run_id}",
        "sample_code": code2,
        "target_event_id": f"API-EVT2-R1-{run_id}",
        "action": "void",
        "operator": "质控员",
        "reason": "误报，作废封签号错误的签收",
    })
    show("纠错-作废（原记录留痕、核验剔除）", s, r, "evaluation", "status")
    s, r = call("GET", f"/samples/{code2}/corrections")
    show("纠错审计查询", s, r)

    # 12) 批量移交：同一交接车次多份样本一次提交
    run_b = uuid.uuid4().hex[:8]
    b_codes = [f"API-BATCH-{run_b}-{i}" for i in (1, 2, 3)]
    for i, bc in enumerate(b_codes):
        call("POST", "/samples", {
            "sample_code": bc,
            "collected_at": "2026-09-30T01:00:00Z",
            "collector": "王采集",
            "seal_no": f"SEAL-BATCH-{run_b}-{i}",
        })

    batch_body = {
        "batch_id": f"API-BATCH-{run_b}",
        "occurred_at": "2026-09-30T02:00:00Z",
        "from_custodian": "王采集",
        "to_custodian": "李运输",
        "items": [
            {"sample_code": b_codes[0], "transfer_no": f"TR-B-{run_b}-1",
             "event_id": f"API-BEVT-{run_b}-1"},
            {"sample_code": b_codes[1], "transfer_no": f"TR-B-{run_b}-2",
             "event_id": f"API-BEVT-{run_b}-2"},
            {"sample_code": b_codes[2], "transfer_no": f"TR-B-{run_b}-3",
             "event_id": f"API-BEVT-{run_b}-3"},
        ],
    }
    s, r = call("POST", "/transfer-batches", batch_body)
    show("批量移交提交（201，逐份核验结论）", s, r, "batch", "item_count")
    for it in r.get("batch", {}).get("items", []):
        print(f"    {it['sample_code']}: {it['evaluation']['status']}"
              f"（原事件有效={it['event_effective']}）")

    # 12.1) 同批次号同内容重试 → 200 幂等（顺序变化也识别为同内容）
    batch_body["items"] = list(reversed(batch_body["items"]))
    s, r = call("POST", "/transfer-batches", batch_body)
    show("同批次号同内容重试（200 幂等）", s, r, "idempotent")

    # 12.2) 同批次号异内容 → 409 batch_conflict
    bad = json.loads(json.dumps(batch_body, ensure_ascii=False))
    bad["items"][0]["transfer_no"] = "TR-CHANGED"
    s, r = call("POST", "/transfer-batches", bad)
    show("同批次号异内容（409 batch_conflict）", s, r)

    # 12.3) 整批失败示例：批内样本重复 / 事件号已占用 → 不留批次与事件
    bad2 = {
        "batch_id": f"API-BATCH-BAD-{run_b}",
        "occurred_at": "2026-09-30T02:00:00Z",
        "from_custodian": "王采集", "to_custodian": "李运输",
        "items": [
            {"sample_code": b_codes[0], "transfer_no": "TR-X1",
             "event_id": f"API-BEVT-{run_b}-9"},
            {"sample_code": b_codes[0], "transfer_no": "TR-X2",
             "event_id": f"API-BEVT-{run_b}-1"},  # 批内样本重复 + 事件号已占用
        ],
    }
    s, r = call("POST", "/transfer-batches", bad2)
    show("整批失败（409/422 batch_rejected，逐项给出）", s, r)

    # 12.4) 批次查询：逐份当前结论与原事件是否仍有效
    s, r = call("GET", f"/transfer-batches/API-BATCH-{run_b}")
    show("批次查询", s, r, "batch", "item_count")

    # 12.5) 作废其中一份移交后再查：只改有效链与查询结果，批次原始归属仍在
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-B-{run_b}",
        "sample_code": b_codes[1],
        "target_event_id": f"API-BEVT-{run_b}-2",
        "action": "void",
        "operator": "质控员",
        "reason": "该份误上车次，作废移交",
    })
    show("作废批内一份移交", s, r, "evaluation", "status")
    s, r = call("GET", f"/transfer-batches/API-BATCH-{run_b}")
    print(f"\n--- 纠错后批次查询 [HTTP {s}] ---")
    for it in r.get("batch", {}).get("items", []):
        print(f"    {it['sample_code']}: 原事件 {it['event_id']} 有效={it['event_effective']}"
              f" 当前有效事件={it['current_event_id']} 结论={it['evaluation']['status']}")

    # 13) 保管链版本复核（质控）：按样本编号 + 版本号查询当时的结论、原因、保管人与时间线
    print("\n=== 保管链版本复核 ===")
    s, r = call("GET", f"/samples/{code}/versions")
    print(f"--- 版本索引 [HTTP {s}] 当前最新版本: {r['current_version']}（共 {r['total']} 版）---")
    for it in r["items"]:
        print(f"    v{it['version']} [{it['change_type']}] {it['committed_at']}"
              f" 结论={it['status']} 保管人={it['current_custodian']} 触发号={it['change_ref']}")

    # 指定历史版本：复核核定（入库）当时的完整快照
    s, r = call("GET", f"/samples/{code}/versions/0")
    v0 = r["version_snapshot"]
    print(f"\n--- 版本 0（登记基线）[HTTP {s}] ---")
    print("结论:", v0["status"], "| 时间线事件数:", len(v0["timeline"]), "| 核定:", v0["approval"])

    # latest 便捷端点
    s, r = call("GET", f"/samples/{code}/versions/latest")
    latest = r["version_snapshot"]
    print(f"--- 当前最新版本 [HTTP {s}] v{latest['version']} [{latest['change_type']}] ---")
    print("结论:", latest["status"], "| 保管人:", latest["current_custodian"], "| 核定信息:", latest["approval"])
    print("当时有效时间线:", [(e["event_type"], e["event_id"]) for e in latest["timeline"]])
    print("纠错留痕:", [(c["correction_id"], c["action"]) for c in latest["corrections"]])
    print("作废/替换留痕:", [(e["event_id"], e["superseded_by"]) for e in latest["superseded_events"]])

    # 未知样本 / 不存在的版本明确报错
    s, r = call("GET", "/samples/NO-SUCH-SAMPLE/versions/0")
    print("\n未知样本查询:", s, r.get("error", {}).get("code"))
    s, r = call("GET", f"/samples/{code}/versions/9999")
    print("不存在的版本:", s, r.get("error", {}).get("code"),
          "| 当前最新版本:", r.get("error", {}).get("details", {}).get("current_version"))

    # 14) 案卷封存：同一抽检的多份样本保管链版本一次封存为不可变案卷
    print("\n=== 案卷封存 ===")
    # 14.1) 封存：code 指定历史版本 0（登记基线），code2 缺省取封存事务中的最新版本
    dossier_id = f"API-DOSSIER-{run_id}"
    dossier_body = {
        "dossier_id": dossier_id,
        "items": [
            {"sample_code": code2},                 # 未指定版本 → 取最新
            {"sample_code": code, "version": 0},    # 指定非负历史版本
        ],
    }
    s, r = call("POST", "/dossiers", dossier_body)
    d = r.get("dossier", {})
    print(f"--- 案卷封存 [HTTP {s}] 案卷号={d.get('dossier_id')} 份数={d.get('item_count')} ---")
    print("摘要:", d.get("digest"))
    for it in d.get("items", []):
        print(f"    [{it['index']}] {it['sample_code']} @ v{it['version']}"
              f" 结论={it['snapshot']['status']} 保管人={it['snapshot']['current_custodian']}")

    # 14.2) 查询案卷：清单（按样本编号排序）、各份完整快照与摘要
    s, r = call("GET", f"/dossiers/{dossier_id}")
    print(f"\n--- 案卷查询 [HTTP {s}] ---")
    print("清单:", [(it["sample_code"], it["version"]) for it in r["dossier"]["items"]])

    # 14.3) 核验：重算摘要并返回一致与否（只读，不改写）
    s, r = call("POST", f"/dossiers/{dossier_id}/verify")
    print(f"--- 案卷核验 [HTTP {s}] 一致={r.get('match')} ---")
    print("留存摘要:", r.get("stored_digest"))
    print("重算摘要:", r.get("recomputed_digest"))

    # 14.4) 同案卷号同一请求重试：即使样本已有新版本，仍返回原卷
    call("POST", "/events", {
        "event_id": f"API-EVT2-T2-{run_id}", "event_type": "transfer", "sample_code": code2,
        "occurred_at": "2026-09-30T06:00:00Z",
        "transfer_no": "TR-DEMO-2B", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    s, r = call("POST", "/dossiers", dossier_body)
    print(f"\n--- 同案卷号同请求重试（样本已新增版本）[HTTP {s}] 幂等={r.get('idempotent')} ---")
    print("清单仍封存原版本:", [(it["sample_code"], it["version"]) for it in r["dossier"]["items"]])

    # 14.5) 同案卷号异内容 → 409 dossier_conflict
    s, r = call("POST", "/dossiers", {
        "dossier_id": dossier_id,
        "items": [{"sample_code": code, "version": 1}, {"sample_code": code2}],
    })
    print("同案卷号异内容:", s, r.get("error", {}).get("code"))

    # 14.6) 整卷失败：编号重复 / 版本不存在 → 不留案卷
    s, r = call("POST", "/dossiers", {
        "dossier_id": f"API-DOSSIER-BAD-{run_id}",
        "items": [{"sample_code": code}, {"sample_code": code}],
    })
    print("编号重复整卷失败:", s, r.get("error", {}).get("code"))
    s, r = call("POST", "/dossiers", {
        "dossier_id": f"API-DOSSIER-BAD2-{run_id}",
        "items": [{"sample_code": code, "version": 9999}, {"sample_code": code2}],
    })
    print("版本不存在整卷失败:", s, r.get("error", {}).get("code"),
          "| 当前最新版本:", r.get("error", {}).get("details", {}).get("current_version"))
    s, r = call("GET", f"/dossiers/API-DOSSIER-BAD-{run_id}")
    print("失败不留案卷（查询 404）:", s, r.get("error", {}).get("code"))


if __name__ == "__main__":
    main()
