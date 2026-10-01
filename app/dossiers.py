"""案卷封存（质控）：同一抽检的多份样本保管链版本一次封存为不可变案卷。

规则（与 README 一致）：
  * 案卷号全局唯一；卷内至少两份不重复样本；可逐份指定非负历史版本，
    未指定者取封存事务中的最新版本，整卷选取在同一事务（同一数据库一致视图）中完成；
  * 任一样本或版本不存在、编号重复 → 整卷失败且不留案卷（事务回滚）；
  * 案卷永久保存所选版本的完整快照与按样本编号排序的清单，
    并对封存内容生成可复算的 SHA-256 摘要（算法见 compute_digest 注释）；
  * 后续补录、纠错、核定不改变旧案卷（快照随案卷落库即冻结）；
  * 同案卷号同一请求重试 → 幂等返回原卷（即使最新版本已变化）；异请求 → 409；
    并发提交以 BEGIN IMMEDIATE 串行化，遵循已提交结果；
  * 核验重算摘要并返回一致与否，不静默改写。
"""
import json
import sqlite3
from typing import Any

from .db import utcnow_iso
from .schemas import DossierIn
from .service import DomainError, _fingerprint, get_sample_or_404
from .versions import latest_version_no


def compute_digest(dossier_id: str, items: list[dict[str, Any]]) -> str:
    """案卷封存内容的 SHA-256 摘要（可复算）。

    规范结构：{"dossier_id": 案卷号,
               "items": [{"sample_code", "version", "snapshot"}, ... 按样本编号升序]}
    序列化：JSON（ensure_ascii=False，键排序，紧凑分隔符）后取 SHA-256 十六进制。
    任何人持有案卷号、清单与快照即可按同一算法独立复算核验。
    """
    canonical = {
        "dossier_id": dossier_id,
        "items": [
            {"sample_code": it["sample_code"], "version": it["version"], "snapshot": it["snapshot"]}
            for it in sorted(items, key=lambda x: x["sample_code"])
        ],
    }
    return _fingerprint(canonical)


def _request_fingerprint(data: DossierIn) -> str:
    """封存请求指纹（同案卷号幂等/冲突判定）。

    对 items 提交顺序不敏感（按样本编号归一化排序）；未指定版本记为 null——
    指纹不含解析后的版本号，因此同一请求重试即使最新版本已变化仍命中原卷。
    """
    items = sorted(
        ({"sample_code": it.sample_code, "version": it.version} for it in data.items),
        key=lambda x: x["sample_code"],
    )
    return _fingerprint({"dossier_id": data.dossier_id, "items": items})


def _resolve_snapshot(conn: sqlite3.Connection, sample: sqlite3.Row, version: int | None) -> tuple[int, str]:
    """在封存事务内解析某份样本应封存的 (版本号, 快照 JSON)。

    未指定版本取当前最新版本；指定版本必须存在。样本登记/迁移后必有版本 0，
    故最新版本必然存在；指定版本不存在 → 404 version_not_found（附当前最新版本号）。
    """
    if version is None:
        version = latest_version_no(conn, sample["id"])
    row = conn.execute(
        "SELECT snapshot_json FROM sample_version WHERE sample_id = ? AND version = ?",
        (sample["id"], version),
    ).fetchone()
    if row is None:
        current = latest_version_no(conn, sample["id"])
        raise DomainError(
            404,
            "version_not_found",
            f"样本 {sample['sample_code']} 不存在版本 {version}（当前最新版本为 {current}），整卷失败",
            {"sample_code": sample["sample_code"], "version": version, "current_version": current},
        )
    return version, row["snapshot_json"]


def _dossier_view(conn: sqlite3.Connection, dossier: sqlite3.Row) -> dict[str, Any]:
    """案卷对外视图：案卷号、摘要、创建时间与按样本编号排序的清单（含完整快照）。"""
    rows = conn.execute(
        "SELECT * FROM dossier_item WHERE dossier_pk = ? ORDER BY item_index ASC",
        (dossier["id"],),
    ).fetchall()
    return {
        "dossier_id": dossier["dossier_id"],
        "item_count": dossier["item_count"],
        "digest": dossier["digest"],
        "created_at": dossier["created_at"],
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


def submit_dossier(conn: sqlite3.Connection, data: DossierIn) -> tuple[dict[str, Any], bool]:
    """封存案卷。返回 (案卷视图, 是否幂等重试)。

    整卷语义：
      * 同案卷号同内容重试 → 幂等返回原卷（即使最新版本已变化）；异内容 → 409；
      * 编号重复（422）/ 样本不存在（404）/ 版本不存在（404）→ 整卷失败，事务回滚不留案卷；
      * 全部校验与选取在同一 IMMEDIATE 事务中完成（同一数据库一致视图），
        并发提交按已提交结果判定（案卷号唯一键兜底）。
    """
    request_hash = _request_fingerprint(data)

    # 1) 同案卷号：同内容幂等 / 异内容冲突。优先于一切业务校验——
    #    同一请求重试即使最新版本已变化也返回原卷。
    existing = conn.execute(
        "SELECT * FROM dossier WHERE dossier_id = ?", (data.dossier_id,)
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise DomainError(
                409,
                "dossier_conflict",
                f"案卷号 {data.dossier_id} 已使用但提交内容不一致（同案卷号异内容冲突）",
                {"dossier_id": data.dossier_id,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        return _dossier_view(conn, existing), True

    # 2) 整卷预校验：编号重复 → 422（此刻尚未写入任何行，失败不留案卷）。
    seen: dict[str, int] = {}
    duplicates: list[dict[str, Any]] = []
    for idx, it in enumerate(data.items):
        if it.sample_code in seen:
            duplicates.append({
                "index": idx, "sample_code": it.sample_code,
                "code": "duplicate_sample_in_dossier",
                "message": f"卷内样本编号重复：{it.sample_code}"
                           f"（第 {seen[it.sample_code] + 1} 份与第 {idx + 1} 份）",
            })
        else:
            seen[it.sample_code] = idx
    if duplicates:
        raise DomainError(
            422,
            "dossier_rejected",
            f"案卷 {data.dossier_id} 卷内样本编号重复，整卷失败且不留下案卷",
            {"dossier_id": data.dossier_id, "items": duplicates},
        )

    # 3) 逐份解析版本并取出该版本完整快照（同一事务内的一致视图）。
    #    任一样本/版本不存在 → 抛错回滚，整卷失败不留案卷。
    resolved: list[dict[str, Any]] = []
    for it in data.items:
        sample = get_sample_or_404(conn, it.sample_code)
        version, snapshot_json = _resolve_snapshot(conn, sample, it.version)
        resolved.append({
            "sample_id": sample["id"],
            "sample_code": it.sample_code,
            "version": version,
            "snapshot_json": snapshot_json,
            "snapshot": json.loads(snapshot_json),
        })

    # 4) 清单按样本编号排序；对封存内容生成可复算摘要；案卷头与明细一次落库。
    resolved.sort(key=lambda x: x["sample_code"])
    digest = compute_digest(data.dossier_id, resolved)
    created_at = utcnow_iso()
    try:
        dc = conn.execute(
            "INSERT INTO dossier (dossier_id, item_count, digest, request_hash, created_at)"
            " VALUES (?,?,?,?,?)",
            (data.dossier_id, len(resolved), digest, request_hash, created_at),
        )
    except sqlite3.IntegrityError:
        # 并发下唯一键竞争：重新读取后按幂等/冲突处理（遵循已提交结果）
        existing = conn.execute(
            "SELECT * FROM dossier WHERE dossier_id = ?", (data.dossier_id,)
        ).fetchone()
        if existing is not None and existing["request_hash"] == request_hash:
            return _dossier_view(conn, existing), True
        raise DomainError(409, "dossier_conflict", f"案卷号 {data.dossier_id} 并发写入且内容冲突")
    dossier_pk = dc.lastrowid
    for idx, it in enumerate(resolved):
        conn.execute(
            "INSERT INTO dossier_item (dossier_pk, sample_id, sample_code, version, item_index, snapshot_json)"
            " VALUES (?,?,?,?,?,?)",
            (dossier_pk, it["sample_id"], it["sample_code"], it["version"], idx, it["snapshot_json"]),
        )

    dossier = conn.execute("SELECT * FROM dossier WHERE id = ?", (dossier_pk,)).fetchone()
    return _dossier_view(conn, dossier), False


def get_dossier(conn: sqlite3.Connection, dossier_id: str) -> dict[str, Any]:
    """案卷查询：清单（按样本编号排序）、各份完整快照与封存摘要。"""
    dossier = conn.execute(
        "SELECT * FROM dossier WHERE dossier_id = ?", (dossier_id,)
    ).fetchone()
    if dossier is None:
        raise DomainError(404, "dossier_not_found", f"案卷不存在: {dossier_id}")
    return _dossier_view(conn, dossier)


def verify_dossier(conn: sqlite3.Connection, dossier_id: str) -> dict[str, Any]:
    """案卷核验：按封存内容重算 SHA-256 摘要并与留存摘要比对。

    只读操作——返回一致与否及两侧摘要，不静默改写任何存储内容。
    """
    dossier = conn.execute(
        "SELECT * FROM dossier WHERE dossier_id = ?", (dossier_id,)
    ).fetchone()
    if dossier is None:
        raise DomainError(404, "dossier_not_found", f"案卷不存在: {dossier_id}")
    rows = conn.execute(
        "SELECT sample_code, version, snapshot_json FROM dossier_item"
        " WHERE dossier_pk = ? ORDER BY item_index ASC",
        (dossier["id"],),
    ).fetchall()
    items = [
        {"sample_code": r["sample_code"], "version": r["version"],
         "snapshot": json.loads(r["snapshot_json"])}
        for r in rows
    ]
    recomputed = compute_digest(dossier["dossier_id"], items)
    return {
        "dossier_id": dossier["dossier_id"],
        "stored_digest": dossier["digest"],
        "recomputed_digest": recomputed,
        "match": recomputed == dossier["digest"],
    }
