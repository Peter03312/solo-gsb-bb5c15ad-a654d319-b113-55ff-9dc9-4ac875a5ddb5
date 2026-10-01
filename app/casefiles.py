"""案卷封存：同一抽检多份样本保管链版本的不可变归档（质控）。

封存规则（与 README 一致）：
  * 案卷号全局唯一；一次封存至少两份不重复的样本编号；
  * 可逐份指定非负历史版本，未指定者取封存事务中该样本的最新版本；
  * 整卷选取处于同一 IMMEDIATE 写事务内——所有样本与版本读取来自同一数据库
    一致视图，选取期间不会有其他写入穿插；
  * 任一样本或版本不存在、编号重复 → 整卷失败，事务回滚，不留案卷；
  * 案卷永久保存所选版本的完整快照（自 sample_version 复制固化）与按样本编号
    排序的清单，并对封存内容生成可复算的 SHA-256 摘要；
  * 后续补录、纠错、核定只产生样本新版本，不改写旧案卷；
  * 同案卷号同一请求重试 → 幂等返回原卷（即使最新版本已变化）；异请求 → 409；
  * 并发提交由 BEGIN IMMEDIATE 串行化，按已提交结果判定；
  * 核验时重算摘要并返回一致与否，不静默改写案卷。

摘要可复算口径（与 verify 端点、外部复算一致）：
  digest = SHA-256( UTF-8( json.dumps(
      {"case_id": 案卷号,
       "items": [{"sample_code": 样本编号, "version": 版本号, "snapshot": 完整快照},
                  ... 按样本编号升序]},
      ensure_ascii=False, sort_keys=True, separators=(",", ":")) ) ) 的十六进制小写
"""
import hashlib
import json
import sqlite3
from typing import Any

from .db import utcnow_iso
from .schemas import CaseFileIn
from .service import DomainError


# --------------------------------------------------------------------------- 指纹与摘要

def _case_fingerprint(data: CaseFileIn) -> str:
    """请求内容指纹（同案卷号幂等/冲突判定）。

    对 items 提交顺序不敏感（按样本编号归一化排序）；version 缺省以 null 参与，
    指纹只覆盖请求本身而非解析后的版本——因此同请求重试即使最新版本已变化，
    仍命中幂等并返回原卷。
    """
    items = sorted(
        ({"sample_code": it.sample_code, "version": it.version} for it in data.items),
        key=lambda x: x["sample_code"],
    )
    canonical = {"case_id": data.case_id, "items": items}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _case_digest(case_id: str, items: list[dict[str, Any]]) -> str:
    """封存内容摘要：对案卷号 + 按样本编号排序的（编号/版本/完整快照）做 SHA-256。

    纯函数：同一批封存内容任何时候复算结果一致，供核验端点与外部复算比对。
    """
    canonical = {
        "case_id": case_id,
        "items": [
            {"sample_code": it["sample_code"], "version": it["version"], "snapshot": it["snapshot"]}
            for it in sorted(items, key=lambda x: x["sample_code"])
        ],
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 查询视图

def _get_case_row(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM case_file WHERE case_id = ?", (case_id,)).fetchone()


def _case_file_view(conn: sqlite3.Connection, case_row: sqlite3.Row) -> dict[str, Any]:
    """案卷查询视图：清单（按样本编号排序）+ 各份完整快照 + 摘要。"""
    rows = conn.execute(
        "SELECT * FROM case_file_item WHERE case_pk = ? ORDER BY item_index ASC",
        (case_row["id"],),
    ).fetchall()
    return {
        "case_id": case_row["case_id"],
        "item_count": case_row["item_count"],
        "digest": case_row["digest"],
        "created_at": case_row["created_at"],
        "items": [
            {
                "index": r["item_index"],
                "sample_code": r["sample_code"],
                "version": r["version"],
                "snapshot": json.loads(r["snapshot_json"]),
            }
            for r in rows
        ],
    }


def get_case_file(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    """案卷查询：返回按样本编号排序的清单、各份完整快照与封存摘要。"""
    row = _get_case_row(conn, case_id)
    if row is None:
        raise DomainError(404, "case_not_found", f"案卷不存在: {case_id}")
    return _case_file_view(conn, row)


def verify_case_file(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    """案卷核验：重算封存内容摘要并与封存摘要比对，返回一致与否。

    只读比对，不静默改写案卷；任何不一致都以 consistent=false 如实返回。
    """
    row = _get_case_row(conn, case_id)
    if row is None:
        raise DomainError(404, "case_not_found", f"案卷不存在: {case_id}")
    view = _case_file_view(conn, row)
    recomputed = _case_digest(case_id, view["items"])
    return {
        "case_id": case_id,
        "item_count": row["item_count"],
        "stored_digest": row["digest"],
        "recomputed_digest": recomputed,
        "consistent": recomputed == row["digest"],
    }


# --------------------------------------------------------------------------- 封存提交

def submit_case_file(conn: sqlite3.Connection, data: CaseFileIn) -> tuple[dict[str, Any], bool]:
    """封存同一抽检的多份样本保管链版本为案卷。返回 (案卷查询视图, 是否幂等重试)。

    整卷语义：
      * 任一样本不存在、指定版本不存在、卷内样本编号重复 → 整卷失败，
        事务回滚，不留下案卷（422 case_rejected，逐项给出原因）；
      * 同案卷号同内容重试 → 幂等返回原卷（即使样本最新版本已变化）；
      * 同案卷号异内容 → 409 case_conflict；
      * 并发提交以 BEGIN IMMEDIATE 持锁顺序串行判定（案卷号唯一键兜底）；
      * 封存为只读取档：不产生样本新版本，不改写任何既有记录。
    """
    request_hash = _case_fingerprint(data)

    # 1) 同案卷号：同内容幂等 / 异内容冲突。优先于一切业务校验，
    #    与既有事件/批次接口一致——同请求重试永远返回原卷。
    existing = _get_case_row(conn, data.case_id)
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise DomainError(
                409,
                "case_conflict",
                f"案卷号 {data.case_id} 已使用但提交内容不一致（同案卷号异请求冲突）",
                {"case_id": data.case_id,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        return _case_file_view(conn, existing), True

    # 2) 整卷预校验与版本解析：在同一事务一致视图内逐份核对。
    #    任一份不通过即整卷失败（此刻尚未写入任何行），不留案卷。
    invalid_items: list[dict[str, Any]] = []
    seen_samples: dict[str, int] = {}
    resolved: list[dict[str, Any]] = []  # 通过校验的份：样本行 + 解析版本 + 快照

    for idx, it in enumerate(data.items):
        if it.sample_code in seen_samples:
            invalid_items.append({
                "index": idx, "sample_code": it.sample_code, "version": it.version,
                "code": "duplicate_sample_in_case",
                "message": f"卷内样本编号重复：{it.sample_code}"
                           f"（第 {seen_samples[it.sample_code] + 1} 份与第 {idx + 1} 份）",
            })
            continue
        seen_samples[it.sample_code] = idx

        sample = conn.execute(
            "SELECT * FROM sample WHERE sample_code = ?", (it.sample_code,)
        ).fetchone()
        if sample is None:
            invalid_items.append({
                "index": idx, "sample_code": it.sample_code, "version": it.version,
                "code": "sample_not_found",
                "message": f"样本不存在: {it.sample_code}",
            })
            continue

        if it.version is None:
            # 未指定版本：取封存事务（同一一致视图）中该样本的最新版本
            ver_row = conn.execute(
                "SELECT * FROM sample_version WHERE sample_id = ?"
                " ORDER BY version DESC LIMIT 1",
                (sample["id"],),
            ).fetchone()
        else:
            ver_row = conn.execute(
                "SELECT * FROM sample_version WHERE sample_id = ? AND version = ?",
                (sample["id"], it.version),
            ).fetchone()
        if ver_row is None:
            current = conn.execute(
                "SELECT COALESCE(MAX(version), -1) AS max_version FROM sample_version WHERE sample_id = ?",
                (sample["id"],),
            ).fetchone()["max_version"]
            invalid_items.append({
                "index": idx, "sample_code": it.sample_code, "version": it.version,
                "code": "version_not_found",
                "message": f"样本 {it.sample_code} 不存在版本 {it.version}"
                           f"（当前最新版本为 {current}）",
            })
            continue

        resolved.append({
            "sample": sample,
            "version": ver_row["version"],
            "snapshot": json.loads(ver_row["snapshot_json"]),
        })

    if invalid_items:
        raise DomainError(
            422,
            "case_rejected",
            f"案卷 {data.case_id} 存在不合法项（卷内编号重复/样本不存在/版本不存在），"
            "整卷失败且不留下案卷",
            {"case_id": data.case_id, "items": invalid_items},
        )

    # 3) 全部通过：清单按样本编号排序，计算封存摘要，案卷头与明细同事务落库。
    #    案卷号唯一键兜底并发；任一插入失败整体回滚，不留案卷与明细。
    resolved.sort(key=lambda x: x["sample"]["sample_code"])
    seal_items = [
        {"sample_code": r["sample"]["sample_code"], "version": r["version"], "snapshot": r["snapshot"]}
        for r in resolved
    ]
    digest = _case_digest(data.case_id, seal_items)
    created_at = utcnow_iso()

    try:
        cc = conn.execute(
            "INSERT INTO case_file (case_id, item_count, digest, request_hash, created_at)"
            " VALUES (?,?,?,?,?)",
            (data.case_id, len(seal_items), digest, request_hash, created_at),
        )
    except sqlite3.IntegrityError:
        # 并发下案卷号唯一键竞争：重读后按幂等/冲突处理
        existing = _get_case_row(conn, data.case_id)
        if existing is not None and existing["request_hash"] == request_hash:
            return _case_file_view(conn, existing), True
        raise DomainError(409, "case_conflict", f"案卷号 {data.case_id} 并发写入且内容冲突")

    case_pk = cc.lastrowid
    for index, r in enumerate(resolved):
        conn.execute(
            "INSERT INTO case_file_item"
            " (case_pk, sample_id, sample_code, version, item_index, snapshot_json)"
            " VALUES (?,?,?,?,?,?)",
            (case_pk, r["sample"]["id"], r["sample"]["sample_code"], r["version"],
             index, json.dumps(r["snapshot"], ensure_ascii=False, separators=(",", ":"))),
        )

    case_row = conn.execute("SELECT * FROM case_file WHERE id = ?", (case_pk,)).fetchone()
    return _case_file_view(conn, case_row), False
