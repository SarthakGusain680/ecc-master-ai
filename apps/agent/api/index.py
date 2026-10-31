import hmac
import json
import os
import re
import subprocess

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
from supabase import create_client

app = FastAPI()

# Browser origins allowed to call this API directly (comma separated in Vercel env var).
# If your UI calls through the Next.js /api/chat proxy, this is not even needed.
origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

sb = create_client(
    os.environ.get("SUPABASE_URL", "https://dummy.supabase.co"),
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "dummy"),
)

PROVIDERS = [
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

# Three backticks, built in code so the file never contains a literal fence
FENCE = "`" * 3


def require_key(x_api_key: str = Header(default="")):
    """Blocks every request that does not send the secret x-api-key header."""
    expected = os.environ.get("ECC_API_KEY", "")
    if not expected or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def call_llm(messages):
    for p in PROVIDERS:
        key = os.environ.get(p["key_env"])
        if not key:
            continue
        try:
            client = OpenAI(base_url=p["base_url"], api_key=key)
            resp = client.chat.completions.create(
                model=p["model"],
                messages=messages,
                temperature=0.2,
                max_tokens=1024,
            )
            return p["name"], resp.choices[0].message.content or ""
        except Exception:
            continue
    return "error", "All LLM providers failed."


def build_system_prompt():
    try:
        instincts = (
            sb.table("instincts")
            .select("trigger_pattern, learned_rule")
            .gte("confidence", 0.7)
            .limit(3)
            .execute()
            .data
        )
    except Exception:
        instincts = []

    if instincts:
        instinct_block = "\n".join(
            f"- WHEN {i['trigger_pattern']} -> {i['learned_rule']}" for i in instincts
        )
    else:
        instinct_block = "- (none learned yet)"

    example = '{"tool": "write_file", "input": {"path": "file.py", "content": "..."}}'
    return (
        "You are ECC-Master. Follow ECC Doctrine: Research first, use TDD, "
        "secure code, be concise.\n"
        "LEARNED INSTINCTS:\n"
        f"{instinct_block}\n"
        "TOOLS: To use a tool, output ONLY a fenced JSON block like this:\n"
        f"{FENCE}json\n{example}\n{FENCE}\n"
        'Or use "run_command" with {"command": "..."} to test your code. '
        "If you are done, reply with normal Markdown."
    )


class JobPayload(BaseModel):
    prompt: str
    # Previous chat turns, sent by the UI: [{"role": "user"|"assistant", "content": "..."}]
    history: list[dict] = []


def clean_history(history):
    out = []
    for m in history[-10:]:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            out.append({"role": role, "content": content[:4000]})
    return out


@app.get("/")
@app.get("/health")
def health():
    return {"status": "healthy", "service": "vercel-fastapi"}


@app.post("/start_job", dependencies=[Depends(require_key)])
def start_job(payload: JobPayload):
    res = sb.table("agent_jobs").insert({"user_id": "vercel-dev", "status": "running"}).execute()
    job_id = res.data[0]["id"]

    messages = [{"role": "system", "content": build_system_prompt()}]
    messages += clean_history(payload.history)
    messages.append({"role": "user", "content": payload.prompt})

    pattern = re.compile(FENCE + r"json\s*(\{.*?\})\s*" + FENCE, re.DOTALL)
    steps = []
    answer = ""

    for step in range(3):
        provider, content = call_llm(messages)
        answer = content
        sb.table("job_logs").insert(
            {"job_id": job_id, "log_type": "thought", "content": {"step": step, "text": content[:1000]}}
        ).execute()

        m = pattern.search(content or "")
        if not m:
            sb.table("job_logs").insert(
                {"job_id": job_id, "log_type": "final_answer", "content": {"text": content}}
            ).execute()
            break

        try:
            tool_call = json.loads(m.group(1))
            sb.table("job_logs").insert(
                {"job_id": job_id, "log_type": "tool_call", "content": tool_call}
            ).execute()

            if tool_call["tool"] == "write_file":
                safe_name = os.path.basename(tool_call["input"]["path"])
                path = f"/tmp/{job_id}_{safe_name}"
                with open(path, "w") as f:
                    f.write(tool_call["input"]["content"])
                result = {"written": path}
            elif tool_call["tool"] == "run_command":
                proc = subprocess.run(
                    tool_call["input"]["command"],
                    shell=True,
                    cwd="/tmp",
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                result = {"stdout": proc.stdout, "stderr": proc.stderr}
            else:
                result = {"error": "Unknown tool"}

            steps.append({"tool": tool_call.get("tool"), "input": tool_call.get("input"), "result": result})
            sb.table("job_logs").insert(
                {"job_id": job_id, "log_type": "tool_result", "content": result}
            ).execute()
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {"role": "user", "content": f"TOOL RESULT: {json.dumps(result)}. Give final answer."}
            )
        except Exception:
            break

    sb.table("agent_jobs").update({"status": "completed"}).eq("id", job_id).execute()
    return {"job_id": job_id, "status": "completed", "answer": answer, "steps": steps}


@app.get("/get_job", dependencies=[Depends(require_key)])
def get_job(job_id: str):
    job = sb.table("agent_jobs").select("*").eq("id", job_id).execute().data
    logs = (
        sb.table("job_logs")
        .select("*")
        .eq("job_id", job_id)
        .order("created_at")
        .execute()
        .data
    )
    return {"job": job, "logs": logs}