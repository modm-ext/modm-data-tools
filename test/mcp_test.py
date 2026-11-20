# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""The MCP server and the CLI around the lookup functions: registration, errors, reload and the shared HTTP app."""

import asyncio
import base64
import contextlib
import gzip
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from modm_data_tools.errors import UserError
from modm_data_tools.lookup import apps, clocks, docs, headers, mcp, svd, tools
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

    def test_instructions(self):
        """Name every tool within the 2048 characters that Claude Code passes on to the model"""
        text = mcp.server.instructions
        assert len(text) <= 2048, len(text)
        assert not [f.__name__ for f in tools.TOOLS if f.__name__ not in text]

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

    def test_summaries(self):
        """The --help list shows the first sentence of every docstring, complete and on one line"""
        dangling = {"a", "an", "the", "with", "and", "or", "of", "for", "to", "from", "by", "in", "on", "per", "e.g"}
        for function in tools.TOOLS:
            text = tools.summary(function)
            assert text.endswith(".") and "\n" not in text and len(text) <= 120, (function.__name__, text)
            assert text.rstrip(".").split()[-1].lower() not in dangling, (function.__name__, text)

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


class Apps(unittest.TestCase):
    """The MCP apps of decode_register and clock_solve behind MODM_DATA_MCP_APPS, on a server of their own"""

    HEADERS = Http.HEADERS
    UI = {"decode_register": "ui://modm-data/register.html", "clock_solve": "ui://modm-data/clocks.html"}
    CALLS = {
        "decode_register": {"device": "stm32g474re", "register": "RCC.CR", "value": "0x03030500"},
        "clock_solve": {"device": "STM32G474RETx", "targets": "SYSCLK=max"},
    }

    @contextlib.contextmanager
    def client(self, on: bool):
        from mcp.server.mcpserver import MCPServer
        from starlette.testclient import TestClient

        with mock.patch.dict(os.environ, {"MODM_DATA_MCP_APPS": "1" if on else ""}):
            server = MCPServer("modm-data")
            tools.register(server)
        app = server.streamable_http_app(stateless_http=True, json_response=True)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            yield lambda method, params=None: Http.post(self, client, method, params)

    def test_off(self):
        """Without the flag nothing changes: no ui:// resources, no _meta on the tools"""
        with self.client(on=False) as post:
            assert post("resources/list")["resources"] == []
            assert all("_meta" not in t for t in post("tools/list")["tools"])

    def test_on(self):
        """With the flag both tools link to their page, the pages are one HTML file each with the views inlined"""
        with self.client(on=False) as post:
            schemas = {t["name"]: t["inputSchema"] for t in post("tools/list")["tools"]}
        with self.client(on=True) as post:
            found = {t["name"]: t for t in post("tools/list")["tools"]}
            assert {n: t["_meta"]["ui"]["resourceUri"] for n, t in found.items() if "_meta" in t} == self.UI
            assert {n: t["inputSchema"] for n, t in found.items()} == schemas
            assert all("outputSchema" not in t for t in found.values())
            resources = {r["uri"]: r for r in post("resources/list")["resources"]}
            assert set(resources) == set(self.UI.values()), resources
            for uri, view in zip(self.UI.values(), ("renderRegister", "createClockGraph")):
                assert resources[uri]["mimeType"] == "text/html;profile=mcp-app"
                (content,) = post("resources/read", {"uri": uri})["contents"]
                assert content["mimeType"] == "text/html;profile=mcp-app", content
                assert "https://unpkg.com" in content["_meta"]["ui"]["csp"]["resourceDomains"]
                html = content["text"]
                # all of the Google Fonts import is gone, a rest of it would break the CSS after it
                assert f"function {view}(" in html and "@import" not in html and "display=swap" not in html
                script = re.search(r'<script type="module">\n(.*)\n</script>', html, re.S)[1]
                # only the import of the App client from the CDN is left
                assert re.findall(r"^\s*(?:import|export)\b.*", script, re.M) == [
                    'import {App} from "https://unpkg.com/@modelcontextprotocol/ext-apps@1.7.5/dist/src/app-with-deps.js";'
                ]
                if shutil.which("node"):
                    with tempfile.TemporaryDirectory() as tmp:
                        (Path(tmp) / "app.mjs").write_text(script)
                        out = subprocess.run(["node", "--check", Path(tmp) / "app.mjs"], capture_output=True, text=True)
                        assert out.returncode == 0, out.stderr

    @unittest.skipUnless(HAS_DATA, "needs ext/modm-data/devices.db and the HTML archive")
    def test_results(self):
        """The text stays byte for byte the same, the data of the view is in _meta only"""
        results = {}
        for on in (False, True):
            with self.client(on=on) as post:
                results[on] = {n: post("tools/call", {"name": n, "arguments": a}) for n, a in self.CALLS.items()}
        for name in self.CALLS:
            off, on = results[False][name], results[True][name]
            assert "_meta" not in off and "structuredContent" not in off and "structuredContent" not in on, on
            assert off["content"] == on["content"] and len(on["content"]) == 1, (off, on)
        assert results[False]["decode_register"]["content"][0]["text"] == svd.decode_register(
            **self.CALLS["decode_register"]
        )

        view = results[True]["decode_register"]["_meta"][apps.KEY]
        assert view["total"] == 1 and view["registers"][0]["value"] == 0x03030500, view
        register = view["registers"][0]["register"]
        assert register["path"] == "RCC.CR" and register["address"] == 0x40021000 and register["size"] == 32
        fields = {f["name"]: f for f in register["children"]}
        pllon = {k: fields["PLLON"][k] for k in ("type", "lsb", "msb", "width")}
        assert pllon == {"type": "field", "lsb": 24, "msb": 24, "width": 1}, fields["PLLON"]
        (cfgr,) = svd._decode("stm32g474re", "RCC.CFGR")[1]
        ppre2 = next(f for f in cfgr["register"]["children"] if f["name"] == "PPRE2")
        assert {"type": "enum", "name": "DIV_2", "value": 4, "desc": "HCLK divided by 2"} in ppre2["children"], ppre2

        view = results[True]["clock_solve"]["_meta"][apps.KEY]
        assert view["partname"] == "STM32G474RETx", view.keys()
        assert json.loads(gzip.decompress(base64.b64decode(view["tree"]))) == clocks._tree("STM32G474RETx")
        assert view["targets"] == {"SysCLKOutput": 170e6} and view["result"]["SysCLKOutput"]["freq"] == 170e6
        assert view["config"]["SysClkSource"] == "RCC_SYSCLKSOURCE_PLLCLK" and isinstance(view["use"], list)

    def test_bundle(self):
        """The bundler on a tiny module graph: named and renamed imports work, a re-export fails loudly"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib").mkdir()
            (root / "lib/b.js").write_text(
                "export const x = 1;\nexport function y() { return 2; }\nconst hidden = 3;\n"
            )
            (root / "lib/c.js").write_text('import {x} from "./b.js";\nexport const sum = x + 10;\n')
            (root / "a.js").write_text(
                'import {x, y as why} from "./lib/b.js";\nimport {\n  sum,\n} from "./lib/c.js";\n'
                "console.log(x + why() + sum + typeof hidden);\n"
            )
            script = apps.bundle("a.js", root)
            # b once, before the modules that import it
            assert script.count("__m['lib/b.js'] =") == 1 and script.index("lib/b.js'] =") < script.index(
                "lib/c.js'] ="
            )
            assert "const {x, y: why} = __m['lib/b.js'];" in script, script
            if shutil.which("node"):
                (root / "out.mjs").write_text(script)
                out = subprocess.run(["node", root / "out.mjs"], capture_output=True, text=True)
                assert out.stdout == "14undefined\n", out
            for bad in ('export {x} from "./lib/b.js";', "export default 1;", 'import * as b from "./lib/b.js";'):
                (root / "a.js").write_text(bad + "\n")
                with self.assertRaisesRegex(ValueError, "^a.js: the bundler cannot handle"):
                    apps.bundle("a.js", root)


if __name__ == "__main__":
    unittest.main()
