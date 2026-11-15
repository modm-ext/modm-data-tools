#!/usr/bin/env python3
# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0
"""
Serves the pages in web/ on top of the data that modm-data generated, without caching:

    python3 serve.py [data folder] [port]

A request is answered from web/ first, then from the data folder, which defaults to
../modm-data/docs/src, where `make build-homepage` and the single exporters write to.
"""

import functools
import http.server
import sys
from pathlib import Path

WEB = Path(__file__).parent / "web"
DATA = Path(sys.argv[1] if len(sys.argv) > 1 else WEB.parents[1] / "modm-data/docs/src").resolve()


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        local = Path(super().translate_path(path))
        if local.exists() and not (local.is_dir() and not (local / "index.html").exists()):
            return str(local)
        return str(DATA / local.relative_to(WEB))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8768
    print(f"http://127.0.0.1:{port}/  (web/ over {DATA})")
    handler = functools.partial(Handler, directory=str(WEB))
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
