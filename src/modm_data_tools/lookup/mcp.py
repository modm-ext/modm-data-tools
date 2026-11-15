# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""MCP server of the lookup functions (needs modm-data-tools[mcp]).

python -m modm_data_tools.lookup.mcp starts one server per client over stdio. One server shared by all clients over HTTP,
restarted whenever the code changes:

    uvicorn modm_data_tools.lookup.mcp:http_app --factory --port 8765 --reload \
        --reload-dir src --reload-dir ../modm-data/src
"""

import importlib

import anyio
from mcp.server import NotificationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations

from .. import figures as figure_data
from ..figures import curves as figure_curves, graph as figure_graph, timing as figure_timing
from . import markdown, tree
from modm_data.svd import read
from modm_data import cubemx
from modm_data.clocks import evaluate as clocks_evaluate
from ..clocks import solver as clocks_solver, configure as clocks_configure
from ..clocks import modm as clocks_modm, modm_api as clocks_modm_api
from modm_data.cubemx import clock_tree as cubemx_clock_tree, conditions as cubemx_conditions
from ..irq import sources as irq_sources, graph as irq_graph, interconnect as irq_interconnect
from modm_data import html2pinout
from modm_data.html2pinout import signals as html2pinout_signals
from . import clocks, core, docs, figures, headers, irq, pinout, svd, tools

# dependencies before the modules that import from them
MODULES = (
    tree,
    markdown,
    read,
    figure_curves,
    figure_timing,
    figure_graph,
    figure_data,
    headers,
    core,
    svd,
    docs,
    figures,
    clocks_evaluate,
    clocks_solver,
    clocks_configure,
    clocks_modm,
    clocks_modm_api,
    cubemx_conditions,
    cubemx_clock_tree,
    cubemx,
    clocks,
    irq_sources,
    irq_graph,
    irq_interconnect,
    irq,
    html2pinout,
    html2pinout_signals,
    pinout,
    tools,
)

server = MCPServer(
    "modm-data",
    instructions=(
        "Device headers: use device_header/header_devices/find_define instead of grepping modm-data/ext. "
        "Documentation: device_docs lists the datasheet, reference manual, errata etc. of a device; find the content with "
        "doc_search/find_tables (doc_grep for regexes and where something is concentrated), navigate with doc_toc, "
        "read with doc_section/doc_table instead of reading the HTML files. "
        "Figures: doc_figures finds them by caption and text, doc_figure gives their text by region and, with image=True, "
        "a PNG of the figure or of one region of it, doc_figure_data the curves of charts (values at any x), the signals and "
        "timing parameters of timing diagrams, and state machines and flowcharts as Mermaid graphs. "
        "Clocks: flash_latency gives the wait states per voltage, clock_tree the CubeMX clock tree with domains, limits, "
        "the clock of every peripheral and, with config, the frequencies of a configuration, clock_solve finds a configuration "
        "for frequency targets (iteratively: feed its config line back) and gives the modm SystemClock code. "
        "Registers: doc_register for the reference manual description, register_map/decode_register for addresses "
        "and decoding values read from a board. "
        "Interrupts: irq_events gives the events ORed into each vector (enable, flag, clear bits, EXTI line) with the "
        "sources that agree, interconnect the peripheral trigger multiplexers (timer ITR, DAC TSEL) with their values. "
        "Pins: board_pinout gives the connector pins of NUCLEO boards and Discovery kits (label, GPIO, net, solder bridges "
        "with their defaults) and which connector pins a GPIO is on, pin_functions the alternate function numbers of the "
        "device pins and which pins can carry a signal like USART2_TX. "
        "The same functions are a CLI: python -m modm_data_tools.lookup <tool> --help. "
        "Over stdio, reload_server loads the code of modm_data_tools.lookup after editing it without a reconnect, "
        "the shared HTTP server restarts by itself."
    ),
)


async def reload_server(ctx: Context) -> str:
    """Reload the code of the lookup tools and tell the client that the tools changed, after editing them"""
    old = {t.name for t in await server.list_tools()} - {"reload_server"}
    for module in MODULES:
        importlib.reload(module)
    for name in old:
        server.remove_tool(name)
    tools.register(server)
    new = {t.name for t in await server.list_tools()} - {"reload_server"}
    # 2026-07-28 clients get it on their subscriptions/listen stream, older ones as a plain notification
    await ctx.notify_tools_changed()
    await ctx.session.send_tool_list_changed()
    added, removed = sorted(new - old), sorted(old - new)
    return (
        f"reloaded {len(MODULES)} modules, {len(new)} tools"
        + (f", added: {', '.join(added)}" if added else "")
        + (f", removed: {', '.join(removed)}" if removed else "")
    )


tools.register(server)
server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))(reload_server)


async def _run():
    # like server.run(), but announces that the tool list can change
    async with stdio_server() as (read_stream, write_stream):
        lowlevel = server._lowlevel_server
        await lowlevel.run(
            read_stream, write_stream, lowlevel.create_initialization_options(NotificationOptions(tools_changed=True))
        )


def http_app():
    """The server as an app for uvicorn, stateless, so that clients do not notice when it restarts"""
    # a restart loads all code, and stateless clients would not hear about changed tools anyway
    server.remove_tool("reload_server")
    docs.warm_up()
    app = server.streamable_http_app(stateless_http=True, json_response=True)

    async def without_stream(scope, receive, send):
        # Clients keep a GET open for server messages, which a stateless server never sends, and uvicorn waits
        # for it forever on a restart. The spec allows refusing it with 405, then clients do not open one.
        if scope["type"] == "http" and scope["method"] == "GET":
            await send({"type": "http.response.start", "status": 405, "headers": [(b"allow", b"POST, DELETE")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)

    return without_stream


def main():
    docs.warm_up()
    anyio.run(_run)


if __name__ == "__main__":
    main()
