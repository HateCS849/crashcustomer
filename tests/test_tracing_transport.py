"""Exercise the installed LangSmith SDK over a synthetic HTTP transport, never its cloud."""
import json
from urllib.parse import urlsplit

import pytest
import requests
from langsmith import Client

from crashcustomer.tracing import _verify


class SyntheticTransport(requests.adapters.BaseAdapter):
    def __init__(self):
        self.run = None
        self.methods = []

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        self.methods.append((request.method, path))
        if request.method == "POST" and path == "/runs":
            self.run = json.loads(request.body)
            data = {}
        elif request.method == "PATCH" and path.startswith("/runs/"):
            self.run.update({k: v for k, v in json.loads(request.body).items() if v is not None})
            data = {}
        elif request.method == "GET" and path.startswith("/runs/"):
            data = {**self.run, "trace_id": self.run["id"], "app_path": "/synthetic/run", "dotted_order": self.run.get("dotted_order") or ""}
        elif path == "/info":
            data = {"version": "synthetic", "batch_ingest_config": {}}
        else:
            pytest.fail(f"Unexpected synthetic request: {request.method} {path}")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(data).encode()
        response.headers["Content-Type"] = "application/json"
        response.url = request.url
        return response

    def close(self): pass


def test_real_sdk_creates_updates_and_reads_synthetic_run():
    transport = SyntheticTransport()
    session = requests.Session()
    session.mount("http://trace.test", transport)
    result = _verify(lambda **kwargs: Client(api_url="http://trace.test", api_key="synthetic-key",
                                             session=session, **kwargs))
    assert result["status"] == "verified"
    assert {m for m, _ in transport.methods} == {"POST", "PATCH", "GET"}
    assert transport.run["inputs"] == {"synthetic": True}
    assert transport.run["outputs"]["correlation_id"] == result["run_id"]
