# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

# The modm-data checkout whose ext/ folder the tools read
MODM_DATA ?= ../modm-data

.DEFAULT_GOAL := help
.PHONY: help
## Show this help.
help:
	@awk '/^## /{doc=substr($$0,4)} /^[a-z-]+:/{if(doc){sub(":.*","",$$1); printf "  %-12s %s\n",$$1,doc}; doc=""}' $(MAKEFILE_LIST)

.PHONY: venv
## Create .venv with modm-data from $(MODM_DATA) and these tools, both editable.
venv:
	@python3 -m venv .venv
	@.venv/bin/pip install -e "$(MODM_DATA)" -e ".[all]"

.PHONY: test
## Run the unit tests (the ones that need private data skip without it).
test: test-web
	@.venv/bin/pytest

.PHONY: test-web
## Run the tests of the web page models with node's test runner.
test-web:
	@node --test 'test/web/*.test.mjs'

.PHONY: format
## Format and lint the Python code.
format:
	@ruff format src test
	@ruff check --fix src test

.PHONY: serve
## Serve web/ over the data that modm-data generated in $(MODM_DATA)/docs/src.
serve:
	@python3 serve.py $(MODM_DATA)/docs/src

.PHONY: mcp
## Run the MCP server over HTTP on port 8765, restarted on code changes.
mcp:
	@.venv/bin/uvicorn modm_data_tools.lookup.mcp:http_app --factory --port 8765 --reload \
		--reload-dir src --reload-dir $(MODM_DATA)/src
