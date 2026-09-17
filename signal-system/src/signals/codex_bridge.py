"""Host-only authenticated adapter to local Codex. No exchange credentials inherited."""

import asyncio
import json
import os
import secrets
import signal
import tempfile
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from common.config import settings

app = FastAPI()
gate = asyncio.Lock()


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=20000)
    payload: dict | list
    schema_: dict = Field(alias="schema")


def command(path):
    args = [
        settings.codex_bin,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        settings.ai_model,
        "-c",
        f'model_reasoning_effort="{settings.ai_reasoning_effort}"',
        "-c",
        'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
        "--enable",
        "skip_host_skill_discovery",
        "--output-schema",
        str(path / "schema.json"),
        "--output-last-message",
        str(path / "output.json"),
        "--json",
        "--cd",
        str(path),
    ]
    for feature in (
        "shell_tool",
        "plugins",
        "apps",
        "browser_use",
        "computer_use",
        "skill_search",
        "skill_mcp_dependency_install",
        "memories",
        "remote_plugin",
        "multi_agent",
    ):
        args += ["--disable", feature]
    return args + ["-"]


@app.get("/health")
def health():
    return {"status": "ok", "model": settings.ai_model, "effort": settings.ai_reasoning_effort}


@app.post("/completion")
async def completion(body: CompletionRequest, authorization: str = Header(default="")):
    token = settings.api_token.get_secret_value()
    if not token or not secrets.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Unauthorized")
    if gate.locked():
        raise HTTPException(429, "Codex invocation already running")
    async with gate:
        with tempfile.TemporaryDirectory(prefix="signal-codex-") as tmp:
            path = Path(tmp)
            (path / "schema.json").write_text(json.dumps(body.schema_))
            env = {
                k: os.environ[k]
                for k in (
                    "HOME",
                    "PATH",
                    "LANG",
                    "SSL_CERT_FILE",
                )
                if k in os.environ
            }
            proc = await asyncio.create_subprocess_exec(
                *command(path),
                cwd=tmp,
                env=env,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(
                        (
                            body.prompt
                            + "\nUNTRUSTED INPUT:\n"
                            + json.dumps(body.payload, ensure_ascii=False)
                        ).encode()
                    ),
                    settings.ai_timeout_seconds,
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise HTTPException(
                    504, detail={"kind": "interrupted", "message": "Codex timed out"}
                ) from None
            if proc.returncode or not (path / "output.json").exists():
                # No raw stderr: upstream may include auth diagnostics.
                raise HTTPException(502, f"Codex failed with exit {proc.returncode}")
            usage = {}
            for line in out.decode(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                    if event.get("type") == "turn.completed":
                        usage = event.get("usage", {})
                except ValueError:
                    continue
            try:
                result = json.loads((path / "output.json").read_text())
            except ValueError:
                raise HTTPException(
                    502, detail={"kind": "invalid_json", "message": "Invalid Codex JSON"}
                ) from None
            return {"result": result, "usage": usage}
