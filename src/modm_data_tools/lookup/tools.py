# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""The lookup functions that the CLI and the MCP server offer, in the order of their help"""

import functools
import inspect
import re
import textwrap

from ..errors import UserError
from . import apps, clocks, docs, figures, headers, irq, pinout, svd

TOOLS = (
    headers.device_header,
    headers.header_devices,
    headers.find_define,
    headers.device_condition,
    headers.peripheral_buses,
    headers.flash_latency,
    clocks.clock_tree,
    clocks.clock_solve,
    svd.register_map,
    svd.decode_register,
    docs.device_docs,
    docs.doc_search,
    docs.doc_grep,
    docs.doc_toc,
    docs.doc_section,
    docs.find_tables,
    docs.doc_table,
    docs.doc_register,
    figures.doc_figures,
    figures.doc_figure,
    figures.doc_figure_data,
    irq.irq_events,
    irq.interconnect,
    pinout.board_pinout,
    pinout.pin_functions,
)


def summary(function) -> str:
    """The first sentence of the docstring (up to the first '. ' that does not end 'e.g.', or the end of the first
    paragraph) on one line, for the --help list of the CLI"""
    paragraph = " ".join((inspect.getdoc(function) or "").split("\n\n")[0].split())
    sentence = re.split(r"(?<!e\.g)\.(?:\s|$)", paragraph, maxsplit=1)[0].rstrip(".")
    return textwrap.shorten(sentence + ".", 150, placeholder=" ...")


def message(error: Exception) -> str | None:
    """The message of an error caused by the input (a UserError, also invalid and too slow regexes and search
    queries), None for a bug"""
    return str(error) if isinstance(error, UserError) else None


def register(server):
    """Adds all lookup functions to an MCPServer, read-only, errors as ToolError with their message"""
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    def tool(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                result = function(*args, **kwargs)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(message(e) or f"{type(e).__name__}: {e}") from e
            # doc_figure(image=True) gives the PNG as bytes
            if isinstance(result, tuple):
                return [result[0], Image(data=result[1], format="png")]
            return result

        signature = inspect.signature(function)
        if "tuple" in str(signature.return_annotation):
            wrapper.__signature__ = signature.replace(return_annotation=str | list[str | Image])
        return wrapper

    annotations = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    for function in TOOLS:
        # with MCP apps on, decode_register and clock_solve also link to their app and send its data
        function, meta = apps.tool(function)
        # the text only once: a return annotation would add an outputSchema and the answer again as structuredContent
        server.tool(annotations=annotations, structured_output=False, meta=meta)(tool(function))
    apps.register(server)
