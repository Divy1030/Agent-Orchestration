"""Provider abstraction for agent-node response generation."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx


class OpenRouterExecutor:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        self.model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    async def execute(self, node: dict[str, Any], messages: list[dict[str, str]]) -> dict[str, Any]:
        config = node.get("config", {})
        fallback = str(config.get("reply", ""))
        if not self.api_key:
            return {"reply": fallback, "provider": "static_fallback", "model": None, "latency_ms": 0, "fallback": True}

        system_prompt = str(config.get("system_prompt") or "You are a helpful conversational AI agent. Answer clearly and concisely.")
        workflow_instructions = str(config.get("workflow_instructions") or "").strip()
        routing_instructions = str(config.get("routing_instructions") or "").strip()
        instructions = [str(item).strip() for item in config.get("instructions", []) if str(item).strip()]
        guidance_sections = []
        if workflow_instructions:
            guidance_sections.append(f"Workflow instructions:\n{workflow_instructions}")
        if routing_instructions:
            guidance_sections.append(f"Current node routing guidance:\n{routing_instructions}")
        if instructions:
            guidance_sections.append("Current node key points:\n" + "\n".join(f"- {item}" for item in instructions))
        if guidance_sections:
            system_prompt += "\n\n" + "\n\n".join(guidance_sections)
        request_messages = [{"role": "system", "content": system_prompt}] + [
            {"role": item["role"], "content": item["content"]}
            for item in messages if item.get("role") in {"user", "assistant"}
        ]
        body = {
            "model": config.get("model") or self.model,
            "messages": request_messages,
            "temperature": float(config.get("temperature", 0.2)),
            "max_tokens": int(config.get("max_tokens", 300)),
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                             "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost:5173"),
                             "X-Title": os.getenv("OPENROUTER_SITE_NAME", "Agent Orchestration")},
                    json=body,
                )
            raw = response.json()
            response.raise_for_status()
            reply = raw["choices"][0]["message"]["content"]
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("OpenRouter returned an empty response")
            return {"reply": reply.strip(), "provider": "openrouter", "model": body["model"],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1), "usage": raw.get("usage", {}),
                    "fallback": False}
        except Exception as exc:
            if fallback:
                return {"reply": fallback, "provider": "static_fallback", "model": body["model"],
                        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                        "fallback": True, "error": type(exc).__name__}
            raise RuntimeError(f"Agent provider failed: {type(exc).__name__}") from exc
