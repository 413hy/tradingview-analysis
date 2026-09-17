"""Run a local project command with deployment config without printing secrets."""

import os
import sys
from pathlib import Path
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
os.chdir(root)
env = dict(os.environ)
env.update({k: v for k, v in dotenv_values(root / "deployment/.env").items() if v is not None})
env["PYTHONPATH"] = ":".join(str(root / p) for p in ("shared/src", "signal-system/src", "trading-system/src"))
if len(sys.argv) < 2:
    raise SystemExit("Usage: .venv/bin/python scripts/run.py COMMAND [ARGS]")
os.execvpe(sys.argv[1], sys.argv[1:], env)
