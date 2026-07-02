"""SQLite (aiosqlite) 초기화 + 마이그레이션.

backend_plan §8.3 권위. WAL 모드 + PRAGMA user_version 기반 마이그레이션.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.utils.log import get_logger

log = get_logger("db")


SCHEMA_V2_MIGRATIONS = [
    "ALTER TABLE screening_results ADD COLUMN smina_affinity_kcal_mol REAL",
    "ALTER TABLE screening_results ADD COLUMN smina_intramolecular_kcal_mol REAL",
    """CREATE INDEX IF NOT EXISTS idx_results_job_smina
        ON screening_results(job_id, smina_affinity_kcal_mol ASC)""",
]

SCHEMA_V3_MIGRATIONS = [
    "ALTER TABLE screening_results ADD COLUMN af3_ranking_score REAL",
    """CREATE INDEX IF NOT EXISTS idx_results_job_ranking
        ON screening_results(job_id, af3_ranking_score DESC)""",
]

# smina --minimize 재구성: minimized affinity provenance 컬럼 1개 추가.
# canonical 최종 점수는 기존 smina_affinity_kcal_mol(V2) 재사용 — 신규 추가 금지.
SCHEMA_V4_MIGRATIONS = [
    "ALTER TABLE screening_results ADD COLUMN smina_minimized_affinity_kcal_mol REAL",
]


SCHEMA_V1 = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    model TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    msg_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    role TEXT NOT NULL,        -- user | assistant | system
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS libraries (
    library_id TEXT PRIMARY KEY,
    session_id TEXT,
    filename TEXT,
    n_entries INTEGER, n_unique INTEGER, n_duplicates INTEGER,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screening_jobs (
    job_id TEXT PRIMARY KEY,
    session_id TEXT,
    library_id TEXT REFERENCES libraries(library_id),
    target_json TEXT,
    config_json TEXT,
    status TEXT,           -- queued|running|done|error|cancelled
    stage TEXT,
    started_at TEXT, ended_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS screening_results (
    job_id TEXT NOT NULL REFERENCES screening_jobs(job_id),
    ligand_id TEXT NOT NULL,
    af3_iptm REAL, af3_mean_pae REAL, af3_mean_plddt REAL,
    af3_folder TEXT,
    om_e_interaction REAL, om_e_complex REAL, om_min_steps INTEGER,
    composite_score REAL,
    rank INTEGER,
    payload_json TEXT,
    PRIMARY KEY (job_id, ligand_id)
);

CREATE INDEX IF NOT EXISTS idx_results_job_score
    ON screening_results(job_id, composite_score DESC);

CREATE INDEX IF NOT EXISTS idx_jobs_session
    ON screening_jobs(session_id, started_at DESC);
"""


async def init_database(db_path: Path) -> None:
    """SQLite DB 초기화 + 스키마 마이그레이션."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        # 현재 user_version 확인
        async with db.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
            current_version = row[0] if row else 0

        if current_version < 1:
            log.info("db.migration.applying", target_version=1, db_path=str(db_path))
            await db.executescript(SCHEMA_V1)
            await db.execute("PRAGMA user_version = 1")
            await db.commit()
            log.info("db.migration.done", version=1)

        if current_version < 2:
            log.info("db.migration.applying", target_version=2, db_path=str(db_path))
            for stmt in SCHEMA_V2_MIGRATIONS:
                try:
                    await db.execute(stmt)
                except Exception as exc:
                    # ALTER TABLE ADD COLUMN は既存カラムがあれば無視する
                    if "duplicate column" in str(exc).lower():
                        log.debug("db.migration.column_exists", stmt=stmt[:60])
                    else:
                        raise
            await db.execute("PRAGMA user_version = 2")
            await db.commit()
            log.info("db.migration.done", version=2)

        if current_version < 3:
            log.info("db.migration.applying", target_version=3, db_path=str(db_path))
            for stmt in SCHEMA_V3_MIGRATIONS:
                try:
                    await db.execute(stmt)
                except Exception as exc:
                    if "duplicate column" in str(exc).lower():
                        log.debug("db.migration.column_exists", stmt=stmt[:60])
                    else:
                        raise
            await db.execute("PRAGMA user_version = 3")
            await db.commit()
            log.info("db.migration.done", version=3)

        if current_version < 4:
            log.info("db.migration.applying", target_version=4, db_path=str(db_path))
            for stmt in SCHEMA_V4_MIGRATIONS:
                try:
                    await db.execute(stmt)
                except Exception as exc:
                    if "duplicate column" in str(exc).lower():
                        log.debug("db.migration.column_exists", stmt=stmt[:60])
                    else:
                        raise
            await db.execute("PRAGMA user_version = 4")
            await db.commit()
            log.info("db.migration.done", version=4)

        if current_version >= 4:
            log.info("db.ready", current_version=current_version, db_path=str(db_path))


async def get_db(db_path: Path) -> aiosqlite.Connection:
    """단일 connection 획득 (caller가 async with 사용)."""
    return await aiosqlite.connect(db_path)
