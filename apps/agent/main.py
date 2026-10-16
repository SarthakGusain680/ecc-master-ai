"""
ECC Master AI — Modal Agentic Engine
ReAct loop + LLM Router + ECC Doctrine + Sandboxed Tools + Supabase Realtime logs
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import modal
from openai import OpenAI
from pydantic import BaseModel, ValidationError
from supabase import create_client as supabase_create

# ---------------- Modal Infrastructure ----------------
image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "openai", "pydantic", "supabase", "requests"
)

app = modal.App("ecc-master-ai", image=image)

workspaces_vol = modal.Volume.from_name("ecc-workspaces", create_if_missing=True)
secrets = [modal.Secret.from_name("ecc-llm-keys")]

WORKSPACE_ROOT = "/workspaces"
MAX_STEPS = 5            # Circuit breaker: never loop forever
COMMAND_TIMEOUT = 30     # Seconds per sandbox command


def _sb():
    return supabase_create(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )


def log(sb, job_id: str, log_type: str, content: dict):
    """Stream a thought/tool/result to Supabase -> frontend sees it live."""
    sb.table("job_logs").insert(
        {"job_id": job_id, "log_type": log_type, "content": content}
    ).execute()


# ---------------- LLM Router (Free-Tier Fallback Chain) ----------------
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


def call_llm(messages: list) -> tuple[str, str]:
    """Try each provider in order. On rate-limit/outage, fall through."""
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
        except Exception as e:  # 429, 5xx, timeout -> next provider
            last_err = e
            continue
    raise RuntimeError(f"All LLM providers failed. Last error: {last_err}")


# ---------------- ECC Doctrine + Instinct Injection ----------------
def build_system_prompt(sb) -> str:
    query = sb.table("instincts").select("trigger_pattern, learned_rule, confidence")
    query = query.gte("confidence", 0.7).order("confidence", desc=True).limit(6)
    instincts = query.execute()

    instinct_block = ""
    for i in instincts.data:
        instinct_block += "- WHEN " + i['trigger_pattern'] + " -> " + i['learned_rule'] + "\n"
    
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