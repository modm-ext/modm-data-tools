# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""MCP server of the lookup functions (needs modm-data-tools[mcp]).

python -m modm_data_tools.lookup.mcp starts one server per client over stdio. One server shared by all clients over HTTP,
restarted whenever the code changes:

    uvicorn modm_data_tools.lookup.mcp:http_app --factory --port 8765 --reload \
        --reload-dir src --reload-dir ../modm-data/src
"""

import contextlib
import importlib

import anyio
from mcp.server import NotificationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations

from .. import figures as figure_data
from ..figures import curves as figure_curves, graph as figure_graph, timing as figure_timing
from . import markdown, tree
from modm_data.svd import read
from modm_data import cubemx
from ..clocks import solver as clocks_solver, configure as clocks_configure
from ..clocks import modm as clocks_modm, modm_api as clocks_modm_api
from modm_data.cubemx import clock_tree as cubemx_clock_tree, conditions as cubemx_conditions
from ..irq import sources as irq_sources, graph as irq_graph, interconnect as irq_interconnect
from modm_data import html2pinout
from modm_data.html2pinout import signals as html2pinout_signals
from . import apps, clocks, core, docs, figures, headers, irq, pinout, svd, tools
from .. import errors

# the package exports its function evaluate under the name of the module
clocks_evaluate = importlib.import_module("modm_data.clocks.evaluate")

# dependencies before the modules that import from them
MODULES = (
    errors,
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
    apps,
    tools,
)

server = MCPServer(
    "modm-data",
    instructions=(
        "STM32 data from ST's own sources: reference manuals, datasheets, errata sheets, programming manuals and "
        "application notes, CMSIS device headers, CubeMX clock trees and pin data, NUCLEO and Discovery board "
        "manuals. For the registers, clocks, pins, interrupts, DMA, timing or electrical values of an STM32, look it "
        "up here before answering from memory, and use these tools instead of grepping or reading modm-data/ext. "
        "Devices are names or prefixes like 'stm32g474re', 'g474re' or 'h7', documents like 'RM0440'.\n"
        "How to answer:\n"
        "- Name the document and section or table of every value.\n"
        "- When the answer configures or relies on a peripheral, look it up in the device's errata sheet (device_docs, "
        "doc_search kind='ES') and say what applies. Only report errata you have read.\n"
        "- Check the question's premise against the data and say when it is wrong.\n"
        "- When tools and documents disagree, show both.\n"
        "Which tool:\n"
        "- Text: doc_search by words, doc_grep by regex, find_tables by caption; read with doc_section and doc_table, "
        "navigate with doc_toc.\n"
        "- Registers: doc_register the manual's bit descriptions, register_map the header's addresses and fields, "
        "decode_register decodes values read from a board.\n"
        "- Clocks: clock_solve finds a configuration for frequency targets (feed its config line back to refine) and "
        "gives modm SystemClock code, clock_tree the tree, limits and frequencies, flash_latency the wait states.\n"
        "- Interrupts: irq_events the events ORed into each vector with enable, flag and clear bits, interconnect the "
        "trigger multiplexers (timer ITR, DAC TSEL).\n"
        "- Pins: pin_functions the alternate functions and which pins carry a signal like USART2_TX, board_pinout the "
        "connector pins and solder bridges of NUCLEO and Discovery boards.\n"
        "- Across devices: find_define the headers defining a macro, device_condition the lbuild condition for them, "
        "device_header, header_devices, peripheral_buses.\n"
        "- Figures: doc_figures finds them, doc_figure their text or a PNG (image=True), doc_figure_data chart curves, "
        "timing diagrams and state machines as Mermaid."
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
    with contextlib.suppress(ToolError):  # already removed by an earlier call
        server.remove_tool("reload_server")
    docs.warm_up()
    app = server.streamable_http_app(stateless_http=True, json_response=True)

    async def without_stream(scope, receive, send):
        # Clients keep a GET open for server messages, which a stateless server never sends, and uvicorn waits
        # for it forever on a restart. The spec allows refusing it with 405, then clients do not open one.
        if scope["type"] == "http" and scope["method"] == "GET":
            await send({"type": "http.response.start", "status": 405, "headers": [(b"allow", b"POST")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)

    return without_stream


def main():
    docs.warm_up()
    anyio.run(_run)


if __name__ == "__main__":
    main()
