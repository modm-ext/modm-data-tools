# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""The MCP server and the CLI around the lookup functions: registration, errors, reload and the shared HTTP app."""

import asyncio
import contextlib
import inspect
import io
import subprocess
import sys
import unittest
from unittest import mock

from modm_data_tools.errors import UserError
from modm_data_tools.lookup import docs, headers, mcp, tools
from modm_data_tools.lookup.__main__ import _parser, main as cli
from mcp.server.mcpserver.exceptions import ToolError

HAS_DATA = headers.DB.exists() and docs.ARCHIVE.exists()
TYPES = {str: "string", int: "integer", bool: "boolean"}


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp.server.list_tools())}


class Registration(unittest.TestCase):
    """Without data: what the server announces and how errors reach the client and the CLI"""

    def test_tools(self):
        """Every lookup function as a read-only tool whose input schema follows its signature, text only"""
        found = _tools()
        assert set(found) - {"reload_server"} == {f.__name__ for f in tools.TOOLS}, set(found)
        for function in tools.TOOLS:
            tool, parameters = found[function.__name__], inspect.signature(function).parameters
            assert tool.annotations.read_only_hint and not tool.annotations.open_world_hint, tool
            # an outputSchema would make the server send every answer twice, also as structuredContent
            assert tool.output_schema is None, tool
            schema = tool.input_schema
            assert set(schema["properties"]) == set(parameters), tool.name
            assert set(schema.get("required", [])) == {
                n for n, p in parameters.items() if p.default is inspect.Parameter.empty
            }, tool.name
            for name, p in parameters.items():
                assert schema["properties"][name]["type"] == TYPES[p.annotation], (tool.name, name)

    def test_errors(self):
        """Errors of the input reach the client and the CLI as their message, bugs as such"""
        assert tools.message(UserError("no device")) == "no device"
        assert tools.message(ValueError("a bug")) is None and tools.message(KeyError("x")) is None

        def call(**arguments):
            return asyncio.run(mcp.server.call_tool("device_header", arguments))

        with mock.patch.object(headers, "_select", side_effect=UserError("no STM32 device matches 'zz'")):
            with self.assertRaisesRegex(ToolError, "no STM32 device matches 'zz'$"):
                call(device="zz")
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                assert cli(["device_header", "zz"]) == 1
            assert error.getvalue() == "no STM32 device matches 'zz'\n"
        with mock.patch.object(headers, "_select", side_effect=KeyError("boom")):
            with self.assertRaisesRegex(ToolError, "KeyError: 'boom'"):
                call(device="g474re")
            # the CLI shows the traceback
            with self.assertRaises(KeyError):
                cli(["device_header", "g474re"])

    def test_cli_defaults(self):
        """The CLI options default to the defaults of the signature, booleans also when they default to True"""
        commands = next(a for a in _parser()._actions if a.dest == "tool").choices
        for function in tools.TOOLS:
            parser = commands[function.__name__]
            for name, p in inspect.signature(function).parameters.items():
                if p.default is not inspect.Parameter.empty:
                    assert parser.get_default(name) == p.default, (function.__name__, name)
        args = commands["doc_section"].parse_args(["RM0440", "7", "--no-subsections"])
        assert args.subsections is False

    def test_reload(self):
        """reload_server reloads every module and keeps the tools, in a process of its own: a reload replaces the
        classes, e.g. UserError, that the other tests hold"""
        script = """
import asyncio, types
from modm_data_tools.lookup import mcp

assert all(isinstance(m, types.ModuleType) for m in mcp.MODULES), mcp.MODULES


class Context:
    class session:
        async def send_tool_list_changed():
            pass

    async def notify_tools_changed(self):
        pass


async def main():
    before = sorted(t.name for t in await mcp.server.list_tools())
    for _ in range(2):
        print(await mcp.reload_server(Context()))
    assert sorted(t.name for t in await mcp.server.list_tools()) == before


asyncio.run(main())
"""
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert f"reloaded {len(mcp.MODULES)} modules, 25 tools" in out.stdout, out.stdout


@unittest.skipUnless(HAS_DATA, "needs ext/modm-data/devices.db and the HTML archive")
class Http(unittest.TestCase):
    """The shared stateless HTTP app, as uvicorn runs it"""

    HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def post(self, client, method: str, params: dict | None = None) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": method} | ({"params": params} if params else {})
        response = client.post("/mcp", headers=self.HEADERS, json=body)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def test_app(self):
        from starlette.testclient import TestClient

        # uvicorn --reload calls the factory again, the second call must not fail on the removed reload_server
        for app in (mcp.http_app(), mcp.http_app()):
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                response = client.get("/mcp")
                assert response.status_code == 405 and response.headers["allow"] == "POST", response.headers
                # no session id needed, every request stands for itself
                result = self.post(
                    client,
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                )
                assert result["serverInfo"]["name"] == "modm-data", result
                names = {t["name"] for t in self.post(client, "tools/list")["tools"]}
                assert names == {f.__name__ for f in tools.TOOLS}, names
                result = self.post(client, "tools/call", {"name": "device_header", "arguments": {"device": "g474re"}})
                assert "stm32g474xx.h" in result["content"][0]["text"] and "structuredContent" not in result, result
                result = self.post(
                    client, "tools/call", {"name": "doc_search", "arguments": {"query": "x", "limit": 0}}
                )
                assert result["isError"] and result["content"][0]["text"].endswith(": limit must be at least 1"), result

    def test_image(self):
        result = asyncio.run(
            mcp.server.call_tool("doc_figure", {"doc": "RM0440", "figure": "17", "image": True, "region": "C2"})
        )
        assert [c.type for c in result.content] == ["text", "image"], result.content
        assert result.content[0].text.endswith("px") and result.structured_content is None, result


if __name__ == "__main__":
    unittest.main()
