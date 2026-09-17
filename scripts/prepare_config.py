"""Create secrets locally without printing them. Never overwrite user configuration."""

import os
import secrets
from pathlib import Path

p = Path("deployment/.env")
if not p.exists():
    password = secrets.token_hex(24)
    token = secrets.token_hex(32)
    p.write_text(
        Path("deployment/.env.example")
        .read_text()
        .replace("replace-with-random-password", password)
        .replace("replace-with-random-token", token)
    )
    p.chmod(0o600)
values = dict(
    line.split("=", 1) for line in p.read_text().splitlines() if "=" in line and not line.startswith("#")
)
# The host model process gets only model configuration and bridge authentication, never exchange/Bot keys.
bridge = Path("deployment/bridge.env")
bridge.write_text(
    "\n".join(
        k + "=" + values[k]
        for k in ("API_TOKEN", "AI_MODEL", "AI_REASONING_EFFORT", "AI_TIMEOUT_SECONDS", "CODEX_BIN")
    )
    + "\n"
)
bridge.chmod(0o600)
for name in ("runtime/analysis/telegram", "runtime/trading"):
    Path(name).mkdir(parents=True, exist_ok=True)
    os.chmod(name, 0o700)
print("Local configuration ready; secret values not displayed.")
