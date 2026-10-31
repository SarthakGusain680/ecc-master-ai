import json
import os
import re
import subprocess

from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from supabase import create_client

app = FastAPI()

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


@app.get("/")
@app.get("/health")
def health():
    return {"status": "healthy", "service": "vercel-fastapi"}


@app.post("/start_job")
def start_job(payload: JobPayload):
    res = sb.table("agent_jobs").insert({"user_id": "vercel-dev", "status": "running"}).execute()
    job_id = res.data[0]["id"]

    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": payload.prompt},
    ]

    pattern = re.compile(FENCE + r"json\s*(\{.*?\})\s*" + FENCE, re.DOTALL)

    for step in range(3):
        provider, content = call_llm(messages)
        sb.table("job_logs").insert(
            {"job_id": job_id, "log_type": "thought", "content": {"step": step, "text": content[:1000]}}
        ).execute()

        m = pattern.search(content or "")
        if not m:
            sb.table("job_logs").insert(
                {"job_id": job_id, "log_type": "final_answer", "content": {"text": content}}
            ).execute()
            sb.table("agent_jobs").update({"status": "completed"}).eq("id", job_id).execute()
            return {"job_id": job_id, "status": "completed"}

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
    return {"job_id": job_id, "status": "completed"}


@app.get("/get_job")
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
