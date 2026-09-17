"""Encrypted filesystem/storage is recommended; backup includes login sessions and keys."""

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
os.umask(0o077)
target = ROOT / "runtime/backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
target.mkdir(parents=True, mode=0o700)
try:
    with (target / "postgres.dump").open("wb") as output:
        subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                "deployment/.env",
                "-f",
                "deployment/compose.yml",
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "-U",
                "signals",
                "-d",
                "signals",
                "-Fc",
            ],
            stdout=output,
            check=True,
        )
    for area in ("analysis", "trading"):
        folder = ROOT / "runtime" / area
        for path in folder.glob("*.db"):
            dest = target / area / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source, sqlite3.connect(dest) as copy:
                source.backup(copy)
        # Telegram SQLite session may be open; use SQLite's consistent backup API.
        for path in (folder / "telegram").glob("*.session"):
            dest = target / area / "telegram" / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source, sqlite3.connect(dest) as copy:
                source.backup(copy)
    shutil.copy2(ROOT / "deployment/.env", target / "deployment.env")
    (target / "README.txt").write_text(
        "PostgreSQL dump + online SQLite snapshots; each database is individually consistent.\nContains secrets. TradingView browser profile is not copied while running; log in again after restore.\nRestore only with all workers/Bots stopped. Reconcile Demo account before resuming entries.\n"
    )
    print("备份完成（包含密钥，目录权限700）：", target.relative_to(ROOT))
except Exception as exc:
    print("备份未完成，请勿用于恢复。错误类型：", type(exc).__name__)
    raise SystemExit(1) from None
