# ECC Master AI - Agent Engine (Hugging Face Spaces edition)
import io
import json
import os
import re
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from openai import OpenAI
from pydantic import BaseModel, ValidationError
from supabase import create_client as supabase_create

api = FastAPI()

WORKSPACE_ROOT = "/tmp/workspaces"
MAX_STEPS = 5
COMMAND_TIMEOUT = 30
JOB_API_KEY = os.environ.get("JOB_API_KEY", "dev-key")

os.makedirs(WORKSPACE_ROOT, exist_ok=True)


def _sb():
    return supabase_create(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )


def log(sb, job_id, log_type, content):
    # Stream a thought/tool/result to Supabase -> frontend sees it live
    sb.table("job_logs").insert(
        {"job_id": job_id, "log_type": log_type, "content": content}
    ).execute()


PROVIDERS = [
    {
        "name": "nemotron",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_env": "NVIDIA_API_KEY",
        "model": "nvidia/llama-3.1-nemotron-70b-instruct",
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
    },
    {
        "name": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.0-flash",
    },
]


def call_llm(messages):
    last_err = None
    for p in PROVIDERS:
        key = os.environ.get(p["key_env"])
        if not key:
            continue
        try:
            client = OpenAI(base_url=p["base_url"], api_key=key)
            resp = client.chat.completions.create(
                model=p["model"], messages=messages, temperature=0.2
            )
            return p["name"], resp.choices[0].message.content
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError("All LLM providers failed: " + str(last_err))


def build_system_prompt(sb):
    query = sb.table("instincts").select("trigger_pattern, learned_rule, confidence")
    query = query.gte("confidence", 0.7).order("confidence", desc=True).limit(6)
    instincts = query.execute()

    instinct_block = ""
    for i in instincts.data:
        instinct_block += "- WHEN " + i["trigger_pattern"] + " -> " + i["learned_rule"] + "\n"

    if not instinct_block:
        instinct_block = "- (none learned yet)\n"

    base_prompt = (
        "You are ECC-Master, an elite AI software engineer running inside the ECC harness.\n\n"
        "## ECC DOCTRINE (always follow)\n"
        "1. RESEARCH FIRST: inspect files before editing.\n"
        "2. TDD CYCLE: RED -> GREEN -> IMPROVE.\n"
        "3. SECURITY: never hardcode secrets.\n"
        "4. TOKEN DISCIPLINE: be concise.\n"
        "5. VERIFICATION: run a command to prove it works.\n\n"
        "## LEARNED INSTINCTS\n"
    )

    tools_prompt = (
        "## TOOLS\n"
        "Output ONLY a fenced JSON block for a tool call:\n"
        "```json\n"
        '{"tool": "read_file | write_file | list_files | run_command", "input": {...}}\n'
        "```\n"
        "When COMPLETE, reply with normal Markdown and NO JSON block."
    )

    return base_prompt + instinct_block + tools_prompt


class ToolCall(BaseModel):
    tool: str
    input: dict


FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_tool_call(text):
    m = FENCE_RE.search(text or "")
    if not m:
        return None
    try:
        return ToolCall.model_validate(json.loads(m.group(1)))
    except (json.JSONDecodeError, ValidationError):
        return "INVALID"


DANGEROUS = [
    r"rm\s+-rf\s+/", r"sudo", r"mkfs", r":\(\)\s*\{", r"dd\s+if=",
    r"curl[^\n]*\|\s*(ba)?sh", r"wget[^\n]*\|\s*(ba)?sh", r"shutdown", r"reboot",
]


def _safe_path(workspace, rel):
    full = os.path.realpath(os.path.join(workspace, rel))
    if not full.startswith(os.path.realpath(workspace)):
        raise ValueError("Path escape blocked by AgentShield")
    return full


def execute_tool(tc, workspace):
    try:
        if tc.tool == "list_files":
            out = []
            for root, _, files in os.walk(workspace):
                for f in files:
                    out.append(os.path.relpath(os.path.join(root, f), workspace))
            return {"files": out[:200]}

        if tc.tool == "read_file":
            with open(_safe_path(workspace, tc.input["path"])) as fh:
                return {"content": fh.read()[:8000]}

        if tc.tool == "write_file":
            path = _safe_path(workspace, tc.input["path"])
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(tc.input["content"])
            return {"written": tc.input["path"]}

        if tc.tool == "run_command":
            cmd = tc.input["command"]
            for pat in DANGEROUS:
                if re.search(pat, cmd):
                    return {"blocked": "AgentShield rejected: " + pat}
            proc = subprocess.run(
                cmd, shell=True, cwd=workspace, capture_output=True,
                text=True, timeout=COMMAND_TIMEOUT,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": workspace},
            )
            return {"stdout": proc.stdout[:4000], "stderr": proc.stderr[:2000],
                    "exit_code": proc.returncode}

        return {"error": "Unknown tool: " + tc.tool}
    except Exception as e:
        return {"error": str(e)}


def save_workspace(sb, job_id, workspace):
    # Zip the workspace and persist it in Supabase Storage (free 1GB)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(workspace):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, workspace))
    try:
        sb.storage.create_bucket("workspaces")
    except Exception:
        pass
    sb.storage.from_("workspaces").upload(
        job_id + ".zip", buf.getvalue(),
        file_options={"content-type": "application/zip"},
    )


def restore_workspace(sb, old_job_id, workspace):
    try:
        data = sb.storage.from_("workspaces").download(old_job_id + ".zip")
    except Exception:
        return
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(workspace)


def run_agent_loop(job_id, user_prompt, resume_job_id=None):
    sb = _sb()
    sb.table("agent_jobs").update({"status": "running"}).eq("id", job_id).execute()

    workspace = os.path.join(WORKSPACE_ROOT, job_id)
    os.makedirs(workspace, exist_ok=True)
    if resume_job_id:
        restore_workspace(sb, resume_job_id, workspace)

    messages = [
        {"role": "system", "content": build_system_prompt(sb)},
        {"role": "user", "content": user_prompt},
    ]

    for step in range(MAX_STEPS):
        provider, content = call_llm(messages)
        log(sb, job_id, "thought", {"step": step, "provider": provider,
                                    "text": (content or "")[:2000]})

        tc = parse_tool_call(content)

        if tc == "INVALID":
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                "Your JSON tool call was malformed. Output ONLY a valid fenced JSON block."})
            continue

        if tc is None:
            log(sb, job_id, "final_answer", {"text": content})
            sb.table("agent_jobs").update({
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()
            save_workspace(sb, job_id, workspace)
            return content

        result = execute_tool(tc, workspace)
        log(sb, job_id, "tool_call", {"step": step, "tool": tc.tool, "input": tc.input})
        log(sb, job_id, "tool_result", {"step": step, "result": result})

        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
            "TOOL RESULT (" + tc.tool + "):\n" + json.dumps(result) + "\n"
            "If verification passed, give your final Markdown answer. Otherwise continue."})

    log(sb, job_id, "error", {"text": "Max steps reached."})
    sb.table("agent_jobs").update({"status": "failed"}).eq("id", job_id).execute()
    save_workspace(sb, job_id, workspace)
    return "Circuit breaker: max steps reached before completion."


def _auth(x_api_key):
    if x_api_key != JOB_API_KEY:
        raise HTTPException(status_code=401, detail="invalid key")


@api.get("/health")
def health():
    return {"status": "healthy", "service": "hf-space-agent", "timestamp": time.time()}


@api.post("/start_job")
def start_job(payload: dict, x_api_key: str = Header(default=None)):
    _auth(x_api_key)
    sb = _sb()
    job_id = sb.table("agent_jobs").insert(
        {"user_id": "web", "status": "pending"}
    ).execute().data[0]["id"]
    threading.Thread(
        target=run_agent_loop,
        args=(job_id, payload.get("prompt", ""), payload.get("resume_job_id")),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@api.get("/get_job")
def get_job(job_id: str, x_api_key: str = Header(default=None)):
    _auth(x_api_key)
    sb = _sb()
    job = sb.table("agent_jobs").select("*").eq("id", job_id).execute().data
    logs = sb.table("job_logs").select("*").eq("job_id", job_id) \
             .order("created_at").execute().data
    return {"job": job, "logs": logs}