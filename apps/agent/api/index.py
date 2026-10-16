import json
import os
import re
import time
from datetime import datetime, timezone
import subprocess

from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from supabase import create_client

app = FastAPI()

# Supabase connection
sb = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
)

# LLM Providers (Optimized for speed)
PROVIDERS = [
    {"name": "groq", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "model": "llama-3.3-70b-versatile"},
    {"name": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY", "model": "gemini-2.0-flash"},
]

def call_llm(messages):
    for p in PROVIDERS:
        key = os.environ.get(p["key_env"])
        if not key: continue
        try:
            client = OpenAI(base_url=p["base_url"], api_key=key)
            resp = client.chat.completions.create(model=p["model"], messages=messages, temperature=0.2, max_tokens=1024)
            return p["name"], resp.choices[0].message.content
        except Exception:
            continue
    return "error", "All LLM providers failed."

def build_system_prompt():
    # Fetch top instincts
    instincts = sb.table("instincts").select("trigger_pattern, learned_rule").gte("confidence", 0.7).limit(3).execute().data
    instinct_block = "\n".join([f"- WHEN {i['trigger_pattern']} -> {i['learned_rule']}" for i in instincts]) if instincts else "- (none learned yet)"
    
    return f"""You are ECC-Master. Follow ECC Doctrine: Research first, use TDD, secure code, be concise.
LEARNED INSTINCTS:
{instinct_block}
TOOLS: Output ONLY fenced JSON for tools:
```json
{{"tool": "write_file", "input": {{"path": "file.py", "content": "..."}}}}