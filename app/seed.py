"""一键初始化演示数据：有效 / 待补证 / 无效 各一条，事件按乱序顺序写入以演示自动重算。

直接运行：python -m app.seed
容器启动时由入口脚本在空库情况下自动调用。
"""
from datetime import datetime, timedelta, timezone

from .db import db_session, init_db
from .schemas import EventIn, SampleIn
from .service import create_event, register_sample


def _iso(dt: datetime) -> datetime:
    return dt


def seed_demo() -> dict[str, int]:
    init_db()
    t0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    samples = [
        SampleIn(sample_code="DEMO-VALID-001", collected_at=_iso(t0 - timedelta(hours=6)),
                 collector="王采集", seal_no="SEAL-V1"),
        SampleIn(sample_code="DEMO-PENDING-002", collected_at=_iso(t0 - timedelta(hours=4)),
                 collector="赵采集", seal_no="SEAL-P2"),
        SampleIn(sample_code="DEMO-INVALID-003", collected_at=_iso(t0 - timedelta(hours=5)),
                 collector="孙采集", seal_no="SEAL-I3"),
    ]

    # (sample_index, EventIn)；VALID 与 INVALID 故意乱序（先写签收/入库再写移交），展示按发生时间重算
    events: list[tuple[int, EventIn]] = [
        # VALID-001：完整有效链（乱序写入：入库 → 检查 → 移交 → 签收）
        (0, EventIn(event_id="DEMO-E-V-S1", event_type="storage", sample_code="DEMO-VALID-001",
                    occurred_at=_iso(t0 - timedelta(minutes=70)), custodian="实验室",
                    storage_location="冷库A-01", note="入库登记")),
        (0, EventIn(event_id="DEMO-E-V-C1", event_type="seal_check", sample_code="DEMO-VALID-001",
                    occurred_at=_iso(t0 - timedelta(minutes=80)), checked_seal_no="SEAL-V1", seal_intact=True,
                    note="入库前封签检查")),
        (0, EventIn(event_id="DEMO-E-V-T1", event_type="transfer", sample_code="DEMO-VALID-001",
                    occurred_at=_iso(t0 - timedelta(hours=5)), transfer_no="TR-V1",
                    from_custodian="王采集", to_custodian="李运输", note="现场移交运输")),
        (0, EventIn(event_id="DEMO-E-V-R1", event_type="receive", sample_code="DEMO-VALID-001",
                    occurred_at=_iso(t0 - timedelta(minutes=90)), transfer_no="TR-V1",
                    receiver="李运输", seal_no="SEAL-V1", note="实验室交接区签收")),
        # PENDING-002：已移交未签收、未入库 → 待补证
        (1, EventIn(event_id="DEMO-E-P-T1", event_type="transfer", sample_code="DEMO-PENDING-002",
                    occurred_at=_iso(t0 - timedelta(hours=2)), transfer_no="TR-P2",
                    from_custodian="赵采集", to_custodian="钱接收", note="运输途中，待签收")),
        # INVALID-003：封签检查封签号不符 → 无效（即使有入库，无效优先）
        (2, EventIn(event_id="DEMO-E-I-T1", event_type="transfer", sample_code="DEMO-INVALID-003",
                    occurred_at=_iso(t0 - timedelta(hours=4)), transfer_no="TR-I3",
                    from_custodian="孙采集", to_custodian="周检测")),
        (2, EventIn(event_id="DEMO-E-I-R1", event_type="receive", sample_code="DEMO-INVALID-003",
                    occurred_at=_iso(t0 - timedelta(hours=3)), transfer_no="TR-I3",
                    receiver="周检测", seal_no="SEAL-I3")),
        (2, EventIn(event_id="DEMO-E-I-C1", event_type="seal_check", sample_code="DEMO-INVALID-003",
                    occurred_at=_iso(t0 - timedelta(minutes=100)), checked_seal_no="SEAL-TAMPERED",
                    seal_intact=False, note="检查发现封签被更换")),
        (2, EventIn(event_id="DEMO-E-I-S1", event_type="storage", sample_code="DEMO-INVALID-003",
                    occurred_at=_iso(t0 - timedelta(minutes=95)), custodian="实验室",
                    storage_location="冷库B-02")),
    ]

    inserted_samples = 0
    inserted_events = 0
    with db_session(immediate=True) as conn:
        for s in samples:
            _, idempotent = register_sample(conn, s)
            inserted_samples += int(not idempotent)
        for idx, e in events:
            _, idempotent = create_event(conn, e)
            inserted_events += int(not idempotent)

    print(f"[seed] 样本新增 {inserted_samples} 条，事件新增 {inserted_events} 条")
    if inserted_samples:
        print("[seed] 演示编号: DEMO-VALID-001(有效) / DEMO-PENDING-002(待补证) / DEMO-INVALID-003(无效)")
    return {"samples": inserted_samples, "events": inserted_events}


if __name__ == "__main__":
    seed_demo()
