"""样本保管链版本（质控复核）。

版本规则（与 README 一致）：
  * 新登记样本从版本 0 开始；升级前已有样本以升级时状态补建版本 0 基线，不推断更早历史；
  * 此后每次真正提交——单事件、批量移交中的该样本事件、作废/替换纠错、入库核定——
    各使受影响样本版本递增一次；同号同内容重试与失败操作不产生版本；
  * 版本按提交先后记录（补录事件的发生时间较早也不改写旧版），快照一经写入不可变；
  * 同一批次各样本的版本与其事件在同一事务中一同提交或一同回滚；
  * 核定后的旧版仍可查询。
"""
import json
import sqlite3
from typing import Any

from .db import utcnow_iso
from .service import DomainError, evaluate

# 触发新版本的写入类型（与 sample_version.change_type 约束一致）
CHANGE_REGISTER = "register"
CHANGE_EVENT = "event"
CHANGE_BATCH = "batch_transfer"
CHANGE_CORRECTION = "correction"
CHANGE_APPROVAL = "approval"
CHANGE_BASELINE = "baseline"


def _build_snapshot(
    sample: sqlite3.Row,
    result: dict[str, Any],
    version: int,
    change_type: str,
    change_ref: str | None,
    committed_at: str,
) -> dict[str, Any]:
    """组装提交时点的完整核验快照（结构与 /chain 响应同源，附加版本信封）。"""
    return {
        "sample": {
            "sample_code": sample["sample_code"],
            "collected_at": sample["collected_at"],
            "collector": sample["collector"],
            "seal_no": sample["seal_no"],
        },
        "version": version,
        "change_type": change_type,
        "change_ref": change_ref,
        "committed_at": committed_at,
        "status": result["status"],
        "reasons": result["reasons"],
        "current_custodian": result["current_custodian"],
        "pending_transfer": result["pending_transfer"],
        "has_storage": result["has_storage"],
        "event_count": result["event_count"],
        "timeline": result["timeline"],
        "corrections": result["corrections"],
        "superseded_events": result["superseded_events"],
        "approved_storage_at": sample["approved_storage_at"],
        "approved_by": sample["approved_by"],
        "approval": (
            {
                "approved_at": sample["approved_storage_at"],
                "approver": sample["approved_by"],
                "note": sample["approve_note"],
            }
            if sample["approved_storage_at"]
            else None
        ),
    }


def record_version(
    conn: sqlite3.Connection,
    sample: sqlite3.Row,
    change_type: str,
    change_ref: str | None = None,
    result: dict[str, Any] | None = None,
) -> int:
    """按当前库内状态追加一个不可变版本快照，返回新版本号。

    必须在调用方的写事务内执行（批量场景随事件一同提交/回滚）。
    result 可传入已重算好的 evaluate 结果以避免重复计算；否则内部重算。
    """
    # 调用方持有的样本行可能是更新前的（如核定），按主键重读最新行
    fresh = conn.execute("SELECT * FROM sample WHERE id = ?", (sample["id"],)).fetchone()
    if result is None:
        result = evaluate(conn, fresh)

    row = conn.execute(
        "SELECT COALESCE(MAX(version), -1) AS max_version FROM sample_version WHERE sample_id = ?",
        (fresh["id"],),
    ).fetchone()
    version = row["max_version"] + 1

    committed_at = utcnow_iso()
    snapshot = _build_snapshot(fresh, result, version, change_type, change_ref, committed_at)
    conn.execute(
        "INSERT INTO sample_version"
        " (sample_id, version, change_type, change_ref, snapshot_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            fresh["id"],
            version,
            change_type,
            change_ref,
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            committed_at,
        ),
    )
    return version


def ensure_version_baselines(conn: sqlite3.Connection) -> int:
    """为尚无任何版本的样本补建版本 0 基线（升级迁移用）。

    升级前已有样本以升级时状态作为版本 0，不推断更早历史；幂等。
    """
    created = 0
    sample_ids = [r["id"] for r in conn.execute("SELECT id FROM sample ORDER BY id ASC").fetchall()]
    for sample_id in sample_ids:
        exists = conn.execute(
            "SELECT 1 FROM sample_version WHERE sample_id = ? LIMIT 1", (sample_id,)
        ).fetchone()
        if exists is not None:
            continue
        sample = conn.execute("SELECT * FROM sample WHERE id = ?", (sample_id,)).fetchone()
        record_version(conn, sample, CHANGE_BASELINE)
        created += 1
    return created


def latest_version_no(conn: sqlite3.Connection, sample_id: int) -> int:
    """样本当前最新版本号（登记/基线后至少为 0）。"""
    row = conn.execute(
        "SELECT MAX(version) AS max_version FROM sample_version WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    return 0 if row["max_version"] is None else row["max_version"]


def _version_summary(row: sqlite3.Row) -> dict[str, Any]:
    snap = json.loads(row["snapshot_json"])
    return {
        "version": row["version"],
        "change_type": row["change_type"],
        "change_ref": row["change_ref"],
        "committed_at": row["created_at"],
        "status": snap["status"],
        "reasons": snap["reasons"],
        "current_custodian": snap["current_custodian"],
        "has_storage": snap["has_storage"],
        "event_count": snap["event_count"],
        "approved_storage_at": snap["approved_storage_at"],
    }


def list_versions(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """列出版本索引（按版本号升序），供质控定位复核版本。"""
    from .service import get_sample_or_404

    sample = get_sample_or_404(conn, sample_code)
    rows = conn.execute(
        "SELECT * FROM sample_version WHERE sample_id = ? ORDER BY version ASC",
        (sample["id"],),
    ).fetchall()
    items = [_version_summary(r) for r in rows]
    return {
        "sample_code": sample_code,
        "current_version": items[-1]["version"] if items else 0,
        "total": len(items),
        "items": items,
    }


def get_version(conn: sqlite3.Connection, sample_code: str, version: int) -> dict[str, Any]:
    """取指定版本的完整不可变快照；样本不存在 / 版本不存在明确报错。"""
    from .service import get_sample_or_404

    sample = get_sample_or_404(conn, sample_code)
    row = conn.execute(
        "SELECT * FROM sample_version WHERE sample_id = ? AND version = ?",
        (sample["id"], version),
    ).fetchone()
    if row is None:
        current = latest_version_no(conn, sample["id"])
        raise DomainError(
            404,
            "version_not_found",
            f"样本 {sample_code} 不存在版本 {version}（当前最新版本为 {current}）",
            {"sample_code": sample_code, "version": version, "current_version": current},
        )
    return json.loads(row["snapshot_json"])


def parse_version(raw: str) -> int:
    """路径参数版本号解析：非整数/负数统一视为不存在的版本。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise DomainError(404, "version_not_found", f"版本号必须是非负整数: {raw!r}")
    if v < 0:
        raise DomainError(404, "version_not_found", f"版本号不存在（不允许为负）: {v}")
    return v
