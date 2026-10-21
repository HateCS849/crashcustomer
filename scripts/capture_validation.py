"""Capture reproducible local validation evidence; external tracing is always disabled here."""
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def main():
    environment = {**os.environ, "LANGSMITH_TRACING": "false"}
    command = [sys.executable, "-m", "pytest", "-q"]
    result = subprocess.run(command, capture_output=True, text=True, env=environment)
    output = result.stdout + result.stderr
    Path("docs/tests-submission-20261021.txt").write_text(output)
    counts = re.search(r"(\d+) passed", output)
    js = []
    for path in sorted(Path("static").glob("*.js")):
        check = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        js.append({"file": str(path), "passed": check.returncode == 0})
    evidence = {"measured_at": datetime.now(timezone.utc).isoformat(), "command": "python -m pytest -q",
                "returncode": result.returncode, "passed": int(counts[1]) if counts else None,
                "javascript_syntax": js, "external_tracing": False,
                "environment": {"python": platform.python_version(), "system": platform.system(), "architecture": platform.machine(),
                                "packages": {name: importlib.metadata.version(name) for name in ("fastapi", "langgraph", "langsmith", "mcp", "playwright", "httpx")}}}
    Path("docs/validation-submission-20261021.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(evidence, ensure_ascii=False))
    return result.returncode == 0 and all(c["passed"] for c in js)


if __name__ == "__main__": raise SystemExit(0 if main() else 1)
