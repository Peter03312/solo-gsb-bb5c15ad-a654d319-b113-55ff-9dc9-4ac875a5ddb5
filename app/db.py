"""SQLite 连接、建表初始化与底层读写工具。"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_code         TEXT NOT NULL UNIQUE,            -- 样本编号（业务唯一）
    collected_at        TEXT NOT NULL,                   -- 采集时间（UTC ISO8601）
    collector           TEXT NOT NULL,                   -- 采集人（初始保管人）
    seal_no             TEXT NOT NULL,                   -- 登记封签号
    content_hash        TEXT NOT NULL,                   -- 登记内容指纹（幂等/冲突判定）
    approved_storage_at TEXT,                            -- 入库核定时间；非空即历史锁定
    approved_by         TEXT,                            -- 核定操作人
    approve_note        TEXT,                            -- 核定备注
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custody_event (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id          INTEGER NOT NULL REFERENCES sample(id),
    event_id           TEXT NOT NULL UNIQUE,             -- 全局唯一事件号
    event_type         TEXT NOT NULL CHECK (event_type IN ('transfer','receive','seal_check','storage')),
    occurred_at        TEXT NOT NULL,                    -- 事件发生时间（UTC ISO8601）
    payload_json       TEXT NOT NULL,                    -- 类型相关字段
    content_hash       TEXT NOT NULL,                    -- 事件内容指纹（同事件号重试比对）
    created_at         TEXT NOT NULL,
    superseded_by      TEXT,                             -- 被哪条纠错作废/替换（NULL=当前有效版本）
    replaces_event_id  TEXT,                             -- 替换产生的新事件：指向被替换的原事件号
    batch_id           TEXT                              -- 批量移交提交时所属批次号（单事件上报为 NULL）
);

CREATE TABLE IF NOT EXISTS event_correction (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    correction_id     TEXT NOT NULL UNIQUE,              -- 全局唯一纠错号
    sample_id         INTEGER NOT NULL REFERENCES sample(id),
    target_event_id   TEXT NOT NULL,                     -- 被纠错的事件号（提交时为当前有效版本）
    action            TEXT NOT NULL CHECK (action IN ('void','replace')),
    operator          TEXT NOT NULL,                     -- 纠错操作人
    reason            TEXT NOT NULL,                     -- 纠错原因
    new_event_id      TEXT,                              -- replace 时的新事件号（void 为 NULL）
    new_event_row_id  INTEGER REFERENCES custody_event(id), -- replace 时新事件行
    request_hash      TEXT NOT NULL,                     -- 纠错请求指纹（同纠错号幂等/冲突判定）
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_batch (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL UNIQUE,                -- 全局唯一批次号（同一交接车次一次提交）
    occurred_at     TEXT NOT NULL,                       -- 统一移交时间（UTC ISO8601）
    from_custodian  TEXT NOT NULL,                       -- 交出人
    to_custodian    TEXT NOT NULL,                       -- 接收人
    item_count      INTEGER NOT NULL,                    -- 批内样本份数
    request_hash    TEXT NOT NULL,                       -- 整批内容指纹（同批次号幂等/冲突判定）
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_batch_item (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_pk     INTEGER NOT NULL REFERENCES transfer_batch(id),
    sample_id    INTEGER NOT NULL REFERENCES sample(id),
    sample_code  TEXT NOT NULL,                          -- 提交时样本编号（批次原始归属留痕）
    transfer_no  TEXT NOT NULL,                          -- 该份样本移交单号
    event_id     TEXT NOT NULL,                          -- 该份样本对应的既有 transfer 事件号
    event_row_id INTEGER NOT NULL REFERENCES custody_event(id),
    item_index   INTEGER NOT NULL                       -- 提交顺序（0 起）
);

-- 样本保管链版本（质控复核用）：版本 0 为登记/升级基线快照，此后每次真正提交
-- （单事件、批量移交中该样本事件、作废/替换纠错、入库核定）追加一个不可变快照行。
-- 版本按提交先后（id/created_at）排列，补录事件的较早发生时间不改写旧版。
CREATE TABLE IF NOT EXISTS sample_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id     INTEGER NOT NULL REFERENCES sample(id),
    version       INTEGER NOT NULL,                     -- 样本内版本号，从 0 起严格递增
    change_type   TEXT NOT NULL CHECK (change_type IN
                    ('register','event','batch_transfer','correction','approval','baseline')),
    change_ref    TEXT,                                 -- 触发版本的业务号（事件号/批次号/纠错号；register/baseline 为 NULL）
    snapshot_json TEXT NOT NULL,                        -- 提交时点完整核验快照（结论/原因/保管人/时间线/纠错/核定）
    created_at    TEXT NOT NULL,
    UNIQUE (sample_id, version)
);

-- 案卷封存（质控）：同一抽检的多份样本保管链版本一次封存为不可变案卷。
-- 案卷永久保存所选版本的完整快照与按样本编号排序的清单，并持有可复算的
-- SHA-256 摘要；后续补录/纠错/核定不改变旧案卷。
CREATE TABLE IF NOT EXISTS dossier (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_id    TEXT NOT NULL UNIQUE,                -- 全局唯一案卷号
    item_count    INTEGER NOT NULL,                    -- 卷内样本份数（>=2）
    digest        TEXT NOT NULL,                       -- 封存内容 SHA-256 摘要（可复算）
    request_hash  TEXT NOT NULL,                       -- 封存请求指纹（同案卷号幂等/冲突判定）
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dossier_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_pk    INTEGER NOT NULL REFERENCES dossier(id),
    sample_id     INTEGER NOT NULL REFERENCES sample(id),
    sample_code   TEXT NOT NULL,                       -- 封存时样本编号（清单留痕）
    version       INTEGER NOT NULL,                    -- 封存时选取的版本号（非负）
    item_index    INTEGER NOT NULL,                    -- 按样本编号排序后的序号（0 起）
    snapshot_json TEXT NOT NULL,                       -- 所选版本完整快照（永久保存）
    UNIQUE (dossier_pk, sample_code)
);

CREATE INDEX IF NOT EXISTS idx_event_sample_time ON custody_event(sample_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_version_sample ON sample_version(sample_id, version);
"""

# 依赖迁移新列的索引（须在旧库补列迁移完成后再创建）
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_event_effective ON custody_event(sample_id, superseded_by, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_correction_sample ON event_correction(sample_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_batch ON custody_event(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_item_batch ON transfer_batch_item(batch_pk, item_index)",
    "CREATE INDEX IF NOT EXISTS idx_dossier_item_dossier ON dossier_item(dossier_pk, item_index)",
]

# 旧库增量补列
_MIGRATION_COLUMNS = {
    "custody_event": [
        ("superseded_by", "TEXT"),
        ("replaces_event_id", "TEXT"),
        ("batch_id", "TEXT"),
    ],
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=config.DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发，遇到锁立即报错由服务层转 409
    conn.execute("PRAGMA journal_mode=WAL");
    conn.execute("PRAGMA foreign_keys=ON");
    conn.execute("PRAGMA busy_timeout=%d" % int(config.DB_TIMEOUT * 1000))
    return conn


@contextmanager
def db_session(commit: bool = True, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """事务上下文。

    immediate=True 时以 BEGIN IMMEDIATE 开启，事务一开始即持有写锁，
    使所有写操作（事件上报 / 纠错 / 核定）在 SQLite 层严格串行：
    纠错与核定并发时后到者必然看到先到者已提交的最终状态。
    """
    conn = get_connection()
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """进程启动时建表（幂等），并对旧库执行增量迁移。"""
    with db_session() as conn:
        conn.executescript(SCHEMA)
        for table, columns in _MIGRATION_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for stmt in POST_MIGRATION_INDEXES:
            conn.execute(stmt)
    # 升级前已有样本：以升级时状态补建版本 0 基线（不推断更早历史）。
    # 惰性导入避免 db ↔ service 的模块级循环依赖。
    from .versions import ensure_version_baselines

    with db_session(immediate=True) as conn:
        ensure_version_baselines(conn)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
