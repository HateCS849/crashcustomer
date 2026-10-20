"""Optional LangSmith tracing. Disabled unless LANGSMITH_TRACING=true and an API key are set.

LangGraph graphs are traced automatically through LangChain callbacks when enabled; direct
Ollama HTTP calls are wrapped with `traced` so prompts, outputs and token counts appear in the
same trace tree. When disabled, `traceable` passes calls through without sending data.
"""
import os

from langsmith import traceable


def tracing_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").lower() == "true" and bool(os.getenv("LANGSMITH_API_KEY"))


def tracing_status() -> dict:
    return {"enabled": tracing_enabled(), "project": os.getenv("LANGSMITH_PROJECT", "default")}


def traced(name: str, run_type: str = "chain"):
    return traceable(name=name, run_type=run_type)


def graph_config(name: str, run: dict, **extra) -> dict:
    """LangGraph invocation config: names the trace and tags it with the run for filtering."""
    return {"run_name": name, "tags": ["crashcustomer", run.get("backend", "rule")],
            "metadata": {"run_id": run["id"], "backend": run.get("backend"), **extra}}
