"""Local LLM access through Ollama. Every AI feature (highlights, Q&A) uses
this one model, running on this PC."""

import json
from typing import Iterator

import httpx

from . import library

# Keep models loaded between requests: reloading after Ollama's default
# 5-minute idle unload has been seen to hang the first request.
KEEP_ALIVE = "30m"
TIMEOUT = httpx.Timeout(180, connect=10)  # one section or answer; retried once
THINK_TIMEOUT = httpx.Timeout(1200, connect=10)  # a thinking pass over a whole paper
# One context size for every request: Ollama reloads the model whenever it
# changes. 32k holds a whole paper's main text for the highlighter's reading
# pass (the longest so far is ~18k tokens); the model then uses ~10 GB.
NUM_CTX = 32768

DEFAULTS = {
    "model": "qwen3.5:9b-q8_0",
    # 127.0.0.1, not "localhost": Windows tries IPv6 (::1) first, Ollama only
    # listens on IPv4, and the fallback cost ~2 s on every request.
    "url": "http://127.0.0.1:11434",
}


def settings() -> dict:
    """settings.json may override the model or URL under "ai"."""
    ai = library.load_settings().get("ai", {})
    return {
        "model": ai.get("model") or ai.get("ollama_model") or DEFAULTS["model"],
        "url": (ai.get("url") or ai.get("ollama_url") or DEFAULTS["url"]).rstrip("/"),
    }


def model_name() -> str:
    return f"ollama:{settings()['model']}"


def status() -> dict:
    cfg = settings()
    out = {"model": cfg["model"], "ready": False, "message": ""}
    try:
        tags = httpx.get(f"{cfg['url']}/api/tags", timeout=3).json()
    except Exception:
        out["message"] = "Ollama is not running."
        return out
    names = {m["name"] for m in tags.get("models", [])}
    out["ready"] = cfg["model"] in names or f"{cfg['model']}:latest" in names
    if not out["ready"]:
        out["message"] = f"Model not downloaded. Run: ollama pull {cfg['model']}"
    return out


def chat(messages: list[dict], schema: dict | None = None, temperature: float = 0, max_tokens: int = 2048,
         think: bool = False) -> str:
    """One reply. think=True lets the model reason first (slower; max_tokens
    must leave room for the reasoning); only the final answer is returned."""
    cfg = settings()
    body = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "think": think,
        "keep_alive": KEEP_ALIVE,
        # num_predict caps the answer so a runaway generation can't stall a paper.
        "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": max_tokens},
    }
    if schema:
        body["format"] = schema
        # Qwen 3.5's default presence penalty (1.5) punishes the repeated keys of
        # a JSON list ("id", "category", ...) and can send it into endless output.
        body["options"]["presence_penalty"] = 0
    for attempt in range(2):
        try:
            r = httpx.post(f"{cfg['url']}/api/chat", json=body, timeout=THINK_TIMEOUT if think else TIMEOUT)
            break
        except httpx.ReadTimeout:
            if attempt:
                raise
    r.raise_for_status()
    return r.json()["message"]["content"]


def embed(texts: list[str], model: str) -> list[list[float]]:
    r = httpx.post(f"{settings()['url']}/api/embed", json={"model": model, "input": texts, "keep_alive": KEEP_ALIVE}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]


def stream_chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 1200) -> Iterator[str]:
    """Yield the answer as it is generated."""
    cfg = settings()
    body = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": max_tokens},
    }
    with httpx.stream("POST", f"{cfg['url']}/api/chat", json=body, timeout=TIMEOUT) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            text = msg.get("message", {}).get("content", "")
            if text:
                yield text
            if msg.get("done"):
                break


def stream_turn(messages: list[dict], tools: list[dict], temperature: float = 0.2, max_tokens: int = 1500,
                think: bool = False) -> Iterator[tuple[str, object]]:
    """One model turn with tools available. Yields ("token", text) as the
    answer streams, and ("tool_calls", [{"name", "arguments"}]) if the model
    asks to use tools instead (then the caller runs them and calls again)."""
    cfg = settings()
    body = {
        "model": cfg["model"],
        "messages": messages,
        "tools": tools,
        "stream": True,
        "think": think,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": max_tokens},
    }
    calls = []
    with httpx.stream("POST", f"{cfg['url']}/api/chat", json=body, timeout=THINK_TIMEOUT if think else TIMEOUT) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            m = msg.get("message", {})
            if m.get("thinking"):
                yield "thinking", m["thinking"]  # reasoning: not shown, but proves it's working
            for c in m.get("tool_calls") or []:
                f = c.get("function", {})
                calls.append({"name": f.get("name", ""), "arguments": f.get("arguments") or {}})
            if m.get("content"):
                yield "token", m["content"]
            if msg.get("done"):
                break
    if calls:
        yield "tool_calls", calls
