"""Real stdio MCP client used by the campaign graph."""
import json
import os
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ROOT
from .tracing import traced


class PlanningContext:
    def __init__(self, directory):
        self.directory = directory
        self.calls = []

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        try:
            transport = await self.stack.enter_async_context(stdio_client(StdioServerParameters(
                command=sys.executable, args=["-m", "crashcustomer.mcp_context_server"], cwd=str(ROOT),
                env={**os.environ, "CRASHCUSTOMER_DATA_DIR": str(self.directory)})))
            self.session = await self.stack.enter_async_context(ClientSession(*transport))
            await self.session.initialize()
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)

    @traced("mcp.context", run_type="tool")
    async def context(self, project_id, goal, query):
        prompt = await self.session.get_prompt("qa_plan", {"goal": goal})
        self.calls.append("prompts/get:qa_plan")
        resource = await self.session.read_resource(f"crashcustomer://runbooks/{project_id}")
        self.calls.append("resources/read:runbooks")
        refs = await self.call("search_project_references", {"project_id": project_id, "query": query})
        return {"prompt": "\n".join(m.content.text for m in prompt.messages if hasattr(m.content, "text")),
                "runbook": json.loads(resource.contents[0].text), "references": refs}

    @traced("mcp.tool", run_type="tool")
    async def call(self, name, arguments):
        result = await self.session.call_tool(name, arguments)
        if result.isError:
            raise RuntimeError("MCP 도구 실행 실패: " + name)
        self.calls.append("tools/call:" + name)
        if result.structuredContent is not None:
            data = result.structuredContent
            return data.get("result", data)
        return json.loads(next(c.text for c in result.content if c.type == "text"))
