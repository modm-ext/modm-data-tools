# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""MCP Apps (the io.modelcontextprotocol/ui extension) for decode_register and clock_solve, behind the environment
variable MODM_DATA_MCP_APPS=1: hosts that render apps (Claude desktop, claude.ai) show the register diagram and the
active clock path next to the answer.

The text stays the same for the model; the data of the view goes into the `_meta` of the result, which the model
does not see. No structuredContent: Claude Code would show it to the model instead of the text.
The pages are the views of web/ bundled into one HTML file on every read, so edits there show up without a restart.
"""

import base64
import functools
import gzip
import json
import os
import posixpath
import re
from pathlib import Path

from . import clocks, svd

WEB = Path(__file__).parents[3] / "web"
KEY = "modm-data/view"
# the most registers of an mdw dump the view draws
REGISTERS = 16
# the App client of ext-apps and dagre for the clock graph come from these CDNs
SCRIPTS = ["https://unpkg.com", "https://cdnjs.cloudflare.com"]
DAGRE = "https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"


def enabled() -> bool:
    return os.environ.get("MODM_DATA_MCP_APPS", "") not in ("", "0")


IMPORT = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"([^"]+)";?[ \t]*$', re.M)
EXPORT = re.compile(r"^export\s+((?:async\s+)?(?:function\*?|const|let|class)\s+([\w$]+))", re.M)
# whatever the two above leave, e.g. export {a}, export default, import * as x, import "x"
LEFT = re.compile(r"^[ \t]*(import|export)\b.*", re.M)


def bundle(entry: str, root: Path = WEB) -> str:
    """The ES module `entry` (relative to root) and its relative imports as one module script: every module an
    async function scope in dependency order, `import {a, b as c} from "./x.js"` reads them from the modules before.
    Imports from URLs stay imports of the script."""
    # ponytail: regexes, not a parser. Only line-start `import {..} from "..."` and `export function/const/let/class
    # name` work, anything else (export {..}, export default, import *, side-effect imports, re-exports) raises;
    # `export let a, b` exports only a. Use esbuild once the views need more.
    done, urls, out = set(), [], []

    def add(path: str):
        if path in done:
            return
        done.add(path)
        text = (root / path).read_text()

        def imp(m) -> str:
            if "://" in m[2]:
                urls.append(m[0])
                return ""
            dep = posixpath.normpath(posixpath.join(posixpath.dirname(path), m[2]))
            add(dep)
            names = ", ".join(n.strip().replace(" as ", ": ") for n in m[1].split(",") if n.strip())
            return f"const {{{names}}} = __m[{dep!r}];"

        body = EXPORT.sub(r"\1", IMPORT.sub(imp, text))
        if left := LEFT.search(body):
            raise ValueError(f"{path}: the bundler cannot handle '{left[0].strip()}'")
        names = ", ".join(m[2] for m in EXPORT.finditer(text))
        # async for top-level await in the modules
        out.append(f"__m[{path!r}] = await (async () => {{\n{body}\nreturn {{{names}}};\n}})();")

    add(entry)
    return "\n".join([*dict.fromkeys(urls), "const __m = {};", *out])


def page(entry: str, css: list[str], head: str = "") -> str:
    """The HTML of an app: the shared theme and the view styles inline, the bundled entry module"""
    styles = "\n".join((WEB / c).read_text() for c in ["shared/theme.css", *css])
    # Google Fonts are outside the CSP, the fonts fall back to the system ones
    styles = re.sub(r"^@import\s+url\([^)]*\)[^;\n]*;", "", styles, flags=re.M)
    if "@import" in styles:
        raise ValueError(f"{entry}: an @import the page cannot load")
    script = bundle(entry)
    if "</script" in script.lower() or "</style" in styles.lower():
        raise ValueError(f"{entry}: a closing tag would end the inline script or style early")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head}<style>\n{styles}\n</style>\n</head>\n<body>\n"
        f'<script type="module">\n{script}\n</script>\n</body>\n</html>\n'
    )


@functools.cache
def _packed(partname: str) -> str:
    """The clock tree as base64 of gzipped JSON, a sixth of its size, unpacked with DecompressionStream"""
    return base64.b64encode(gzip.compress(json.dumps(clocks._tree(partname), separators=(",", ":")).encode())).decode()


def _register_view(device: str, register: str = "", value: str = ""):
    text, views = svd._decode(device, register, value)
    return text, {"registers": views[:REGISTERS], "total": len(views)}


def _clocks_view(device: str, targets: str, config: str = "", use: str = "", lock: str = "", code: bool = False):
    text, view = clocks._solve(device, targets, config, use, lock, code)
    return text, view | {"tree": _packed(view["partname"])}


# tool -> (page, view function with its signature that gives the text and the data of the view)
APPS = {
    "decode_register": ("register", _register_view),
    "clock_solve": ("clocks", _clocks_view),
}
PAGES = {
    "register": lambda: page("mcp/register.js", ["svd/register-view.css", "mcp/style.css"]),
    "clocks": lambda: page(
        "mcp/clocks.js", ["clocks/graph-view.css", "mcp/style.css"], f'<script src="{DAGRE}"></script>\n'
    ),
}


def tool(function) -> tuple:
    """The function as tools.register adds it and the _meta of its tool: with apps on, the function returns a
    CallToolResult with the text and the data of the view in _meta"""
    if not enabled() or function.__name__ not in APPS:
        return function, None
    from mcp.types import CallToolResult, TextContent

    name, view = APPS[function.__name__]

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        text, data = view(*args, **kwargs)
        return CallToolResult(content=[TextContent(type="text", text=text)], _meta={KEY: data})

    return wrapper, {"ui": {"resourceUri": f"ui://modm-data/{name}.html"}}


def register(server):
    """Adds the ui:// resources of the apps to an MCPServer, if apps are on"""
    if not enabled():
        return
    from mcp.server.apps import APP_MIME_TYPE

    meta = {"ui": {"csp": {"resourceDomains": SCRIPTS}, "prefersBorder": True}}
    for name, build in PAGES.items():
        uri = f"ui://modm-data/{name}.html"
        # reload_server registers again, the new code replaces the old
        server._resource_manager._resources.pop(uri, None)
        server.resource(uri, name=f"{name}-app", mime_type=APP_MIME_TYPE, meta=meta)(build)
