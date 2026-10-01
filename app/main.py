"""FastAPI 应用：样本交接链核验 HTTP API。"""
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .db import db_session, init_db, row_to_dict
from .dossiers import get_dossier, submit_dossier, verify_dossier
from .schemas import ApproveIn, CorrectionIn, DossierIn, EventIn, SampleIn, TransferBatchIn
from .service import (
    DomainError,
    approve_storage,
    create_event,
    evaluate,
    evaluation_summary,
    get_sample_or_404,
    get_transfer_batch,
    list_corrections,
    register_sample,
    submit_correction,
    submit_transfer_batch,
)
from .versions import get_version, latest_version_no, list_versions, parse_version

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if config.SEED_ON_START:
        # 仅当库中尚无样本时写入演示数据，重启不重复
        from .seed import seed_demo

        with db_session() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
        if count == 0:
            seed_demo()
    yield


app = FastAPI(
    title="样本交接链核验服务",
    description="环境检测机构样本保管链（Chain of Custody）登记、事件核验、入库核定与版本化复核",
    version="1.4.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


def _summary(result: dict[str, Any], sample: dict[str, Any], conn=None) -> dict[str, Any]:
    body = evaluation_summary(result, sample)
    if conn is not None:
        # 当前最新版本号（版本 0 起；便于质控复核定位）
        body["current_version"] = latest_version_no(conn, sample["id"])
    return body


# --------------------------------------------------------------------------- 基本信息

@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "样本交接链核验服务",
        "docs": "/docs",
        "health": "/health",
        "api_prefix": API_PREFIX,
        "status_values": {"valid": "有效", "pending": "待补证", "invalid": "无效"},
    }


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- 样本登记

@app.post(f"{API_PREFIX}/samples", tags=["samples"], status_code=201)
def register_sample_api(data: SampleIn) -> JSONResponse:
    with db_session(immediate=True) as conn:
        sample, idempotent = register_sample(conn, data)
        result = evaluate(conn, sample)
        body = {
            "sample": {k: sample[k] for k in ("sample_code", "collected_at", "collector", "seal_no", "created_at")},
            "evaluation": _summary(result, dict(sample), conn),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples", tags=["samples"])
def list_samples_api() -> dict[str, Any]:
    with db_session() as conn:
        samples = conn.execute("SELECT * FROM sample ORDER BY id").fetchall()
        items = [_summary(evaluate(conn, s), dict(s), conn) for s in samples]
    return {"total": len(items), "items": items}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}", tags=["samples"])
def get_sample_api(sample_code: str) -> dict[str, Any]:
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        result = evaluate(conn, sample)
        return {"sample": row_to_dict(sample), "evaluation": _summary(result, dict(sample), conn)}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/chain", tags=["samples"])
def get_chain_api(sample_code: str) -> dict[str, Any]:
    """完整证据链：按时间排列的事件、当前保管人、结论与原因。"""
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        result = evaluate(conn, sample)
        return {
            "sample": {k: sample[k] for k in ("sample_code", "collected_at", "collector", "seal_no")},
            "status": result["status"],
            "reasons": result["reasons"],
            "current_custodian": result["current_custodian"],
            "pending_transfer": result["pending_transfer"],
            "has_storage": result["has_storage"],
            "approved_storage_at": sample["approved_storage_at"],
            "timeline": result["timeline"],
            "corrections": result["corrections"],
            "superseded_events": result["superseded_events"],
            "current_version": latest_version_no(conn, sample["id"]),
        }


# --------------------------------------------------------------------------- 保管链版本复核

@app.get(f"{API_PREFIX}/samples/{{sample_code}}/versions", tags=["versions"])
def list_versions_api(sample_code: str) -> dict[str, Any]:
    """版本索引：列出该样本全部版本（版本号、提交时间、触发类型、当时结论）。

    版本按提交先后排列；核定后旧版仍可查询。
    """
    with db_session() as conn:
        return list_versions(conn, sample_code)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/versions/latest", tags=["versions"])
def get_latest_version_api(sample_code: str) -> dict[str, Any]:
    """当前最新版本的完整保管链快照（便捷端点，等价于按 current_version 取版本）。"""
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        version = latest_version_no(conn, sample["id"])
        return {"version_snapshot": get_version(conn, sample_code, version)}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/versions/{{version_raw}}", tags=["versions"])
def get_version_api(sample_code: str, version_raw: str) -> dict[str, Any]:
    """指定版本复核：返回该版本提交时点的核验结论、原因、当前保管人、
    有效事件时间线、纠错记录与核定信息（不可变快照）。

    未知样本 → 404 sample_not_found；不存在的版本 → 404 version_not_found。
    """
    version = parse_version(version_raw)
    with db_session() as conn:
        return {"version_snapshot": get_version(conn, sample_code, version)}


# --------------------------------------------------------------------------- 事件上报

@app.post(f"{API_PREFIX}/events", tags=["events"], status_code=201)
def create_event_api(data: EventIn) -> JSONResponse:
    """移交 / 签收 / 封签检查 / 入库 统一上报入口。

    同 event_id 同内容重试 → 200 幂等返回；同 event_id 异内容 → 409 冲突。
    """
    with db_session(immediate=True) as conn:
        row, idempotent = create_event(conn, data)
        sample = conn.execute("SELECT * FROM sample WHERE id = ?", (row["sample_id"],)).fetchone()
        result = evaluate(conn, sample)
        body = {
            "event": {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "sample_code": data.sample_code,
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
                "recorded_at": row["created_at"],
            },
            "evaluation": _summary(result, dict(sample), conn),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


# --------------------------------------------------------------------------- 批量移交（同一交接车次多份样本）

@app.post(f"{API_PREFIX}/transfer-batches", tags=["transfer-batches"], status_code=201)
def submit_transfer_batch_api(data: TransferBatchIn) -> JSONResponse:
    """同一交接车次多份样本一次提交，各份作为既有 transfer 事件原子写入。

    * 整批失败不留痕：批内样本/事件号重复、样本不存在/已核定、任一事件号已占用
      → 事务回滚，不创建批次与任何事件（409/422 batch_rejected，逐项给出）；
    * 同批次号同内容重试 → 200 幂等（样本此后已核定也成立）；异内容 → 409 batch_conflict；
    * 并发提交由 BEGIN IMMEDIATE 串行化，按已提交顺序判定。
    """
    with db_session(immediate=True) as conn:
        batch, idempotent = submit_transfer_batch(conn, data)
    return JSONResponse(status_code=200 if idempotent else 201,
                        content={"batch": batch, "idempotent": idempotent})


@app.get(f"{API_PREFIX}/transfer-batches/{{batch_id}}", tags=["transfer-batches"])
def get_transfer_batch_api(batch_id: str) -> dict[str, Any]:
    """批次查询：逐份样本的当前核验结论及原事件是否仍有效（纠错后反映有效链）。"""
    with db_session() as conn:
        return {"batch": get_transfer_batch(conn, batch_id)}


# --------------------------------------------------------------------------- 事件纠错

@app.post(f"{API_PREFIX}/corrections", tags=["corrections"], status_code=201)
def submit_correction_api(data: CorrectionIn) -> JSONResponse:
    """核定前事件纠错：作废（void）或替换（replace）。

    * 仅核定入库前、且目标为当前有效版本时可提交；不得跨样本；
    * 同 correction_id 同内容重试 → 200 幂等；异内容 → 409 correction_conflict；
    * replace 须给完整新事件内容与未占用新事件号；
    * 并发针对同一有效版本仅一次成功，失败不改变链；
    * 响应 evaluation 为纠错后仅按有效版本重算的结论/原因/当前保管人。
    """
    with db_session(immediate=True) as conn:
        body, idempotent = submit_correction(conn, data)
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/corrections", tags=["corrections"])
def list_corrections_api(sample_code: str) -> dict[str, Any]:
    """样本的纠错审计记录（原事件与纠错关系永久留痕）。"""
    with db_session() as conn:
        items = list_corrections(conn, sample_code)
    return {"sample_code": sample_code, "total": len(items), "items": items}


# --------------------------------------------------------------------------- 入库核定

@app.post(f"{API_PREFIX}/samples/{{sample_code}}/approve", tags=["approval"])
def approve_api(sample_code: str, data: ApproveIn) -> dict[str, Any]:
    with db_session(immediate=True) as conn:
        result = approve_storage(conn, sample_code, data)
        sample = get_sample_or_404(conn, sample_code)
        return {"evaluation": _summary(result, dict(sample), conn), "approval": result["approval"]}


# --------------------------------------------------------------------------- 案卷封存（同一抽检多份样本）

@app.post(f"{API_PREFIX}/dossiers", tags=["dossiers"], status_code=201)
def submit_dossier_api(data: DossierIn) -> JSONResponse:
    """同一抽检的多份样本保管链封存为案卷。

    * 至少两份不重复样本；可逐份指定非负历史版本，未指定者取封存事务中的最新版本
      （整卷选取在同一事务的一致视图中完成）；
    * 任一样本/版本不存在、编号重复 → 整卷失败且不留案卷；
    * 案卷永久保存所选版本完整快照与按样本编号排序的清单，并生成可复算 SHA-256 摘要；
    * 同案卷号同内容重试 → 200 幂等返回原卷（即使最新版本已变化）；异内容 → 409 dossier_conflict；
    * 并发提交由 BEGIN IMMEDIATE 串行化，遵循已提交结果。
    """
    with db_session(immediate=True) as conn:
        dossier, idempotent = submit_dossier(conn, data)
    return JSONResponse(status_code=200 if idempotent else 201,
                        content={"dossier": dossier, "idempotent": idempotent})


@app.get(f"{API_PREFIX}/dossiers/{{dossier_id}}", tags=["dossiers"])
def get_dossier_api(dossier_id: str) -> dict[str, Any]:
    """案卷查询：按样本编号排序的清单、各份所选版本完整快照与封存摘要。"""
    with db_session() as conn:
        return {"dossier": get_dossier(conn, dossier_id)}


@app.post(f"{API_PREFIX}/dossiers/{{dossier_id}}/verify", tags=["dossiers"])
def verify_dossier_api(dossier_id: str) -> dict[str, Any]:
    """案卷核验：按封存内容重算 SHA-256 摘要并返回一致与否（只读，不静默改写）。"""
    with db_session() as conn:
        return verify_dossier(conn, dossier_id)
