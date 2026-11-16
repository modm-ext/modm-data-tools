"""
Collects the clock meter coverage of the hardware CI boards for web/clocks/coverage.html.

    python -m modm_data_tools.clocks.coverage "<folder with <board>.json>" --output <folder>

Needs the clock trees that `python -m modm_data.clocks --output <folder>` exported and
writes <folder>/data/coverage.json: per board the CubeMX part, config, covered,
uncovered and failed edges and notes. The data is ST SLA: never commit it.
"""

import argparse
import json
import re
from pathlib import Path

# Hardware results that are not a pass, from the README next to the coverage files
# [node, input, reason]: the reason shows on the edge
FAILED = {
    "nucleo-h563zi": [[f"LPTIM{n}Mult", "PLL2P", "erratum ES0565 2.11.5: no kernel clock"] for n in range(1, 7)],
    "weact-h7r3z8": [
        ["USART1Mult", "DIVQ3", "no output at DIVQ3=1 (576 MHz kernel), limit unknown: RM0477/DS14360 give none"]
    ],
}
NOTES = {
    "nucleo-h723zg": "Every edge downstream of HSEOSC measures +7.7..9.0‰: the board's HSE (ST-LINK MCO), not the graph.",
    "disco-f746ng": "HSIRC edges measure +3%: the chip's HSI, not the graph.",
    "nucleo-h563zi": "LPTIMx ← PLL2P cannot work: erratum ES0565 2.11.5.",
    "weact-h7r3z8": "USART1 ← DIVQ3 gives no output at DIVQ3=1 (576 MHz kernel): failed, limit unknown (none in RM0477/DS14360).",
}
# Edges whose failure comes from the board or the chip, not the graph
SUSPECT = {"nucleo-h723zg": "HSEOSC", "disco-f746ng": "HSIRC"}


def partname(board: str, device: str, devices: dict) -> str:
    stem = device.upper().removesuffix("6Q")
    found = sorted(d for d in devices if d.startswith(stem))
    if board.endswith("-q"):
        found = [d for d in found if d.endswith("Q")] or found
    else:
        found = [d for d in found if not d.endswith("Q")] or found
    return found[0]


def node_coverage(tree: dict, data: dict) -> tuple[set, set]:
    """
    The nodes a measured signal passed through, and the nodes that count: everything upstream of a
    tested node or a peripheral clock. Same as modm-hardware-ci clockmeter/gen/coverage3.py.
    """
    nodes = tree["nodes"]
    cov = {tuple(e) for e in data["covered"]}
    relevant, covered = set(), set()

    def up(n):
        if n in relevant or n not in nodes:
            return
        relevant.add(n)
        for i in nodes[n]["inputs"]:
            up(i["from"])

    for k, _ in cov | {tuple(e) for e in data["uncovered"]}:
        up(k)
    for out in set(tree["clocks"].values()):
        up(out)

    # Covered config entries mark their node and the selected mux input, then upstream to the oscillator
    def mark(n):
        if n in covered or n not in nodes:
            return
        covered.add(n)
        if nodes[n]["kind"] == "mux":
            for k, v in cov:
                if k == n and v in nodes:
                    mark(v)
        elif nodes[n]["inputs"]:
            mark(nodes[n]["inputs"][0]["from"])

    for k, v in cov:
        mark(k)
        if nodes.get(k, {}).get("kind") == "mux" and v in nodes:
            mark(v)
    # Peripheral clock outputs count when a case measured one of their peripherals
    probed = {m for c in data.get("cases", []) for m in re.findall(r"[A-Z][A-Z0-9_]*\d|MCO\d?", c)}
    for p, out in tree["clocks"].items():
        if p in probed and out in nodes and all(i["from"] in covered for i in nodes[out]["inputs"][:1]):
            covered.add(out)
    for k, n in nodes.items():
        if (
            n["kind"] == "output"
            and "MCO" in k.upper()
            and any(i["from"] in covered and nodes[i["from"]]["kind"] != "mux" for i in n["inputs"][:1])
        ):
            covered.add(k)
    return covered & relevant, relevant


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect the clock coverage of the hardware CI boards.")
    parser.add_argument("results", type=Path, help="folder with the <board>.json of the clock meter")
    parser.add_argument("--output", type=Path, required=True, help="folder of the page with the exported clock trees")
    args = parser.parse_args()
    OUT = args.output / "data"
    devices = json.loads((OUT / "devices.json").read_text())
    boards = {}
    for path in sorted(args.results.glob("*.json")):
        data = json.loads(path.read_text())
        board = data["board"]
        covered, uncovered = data["covered"], data["uncovered"]
        device = partname(board, data["device"], devices)
        nodes, relevant = node_coverage(json.loads((OUT / "tree" / f"{devices[device]}.json").read_text()), data)
        boards[board] = {
            "device": device,
            "nodes": sorted(nodes),
            "nodes_total": len(relevant),
            "config": data["config"],
            "covered": covered,
            "uncovered": uncovered,
            "failed": FAILED.get(board, []),
            "suspect": SUSPECT.get(board),
            "note": NOTES.get(board, ""),
            "cases": len(data.get("cases", [])),
        }
    (OUT / "coverage.json").write_text(json.dumps(boards, indent=1))
    for board, b in boards.items():
        print(
            f"{board:18} {b['device']:16} config {len(b['covered']):3}/{len(b['covered']) + len(b['uncovered']):<3}"
            f" nodes {len(b['nodes']):3}/{b['nodes_total']}"
        )
    print(
        "total nodes",
        sum(len(b["nodes"]) for b in boards.values()),
        "/",
        sum(b["nodes_total"] for b in boards.values()),
        "config",
        sum(len(b["covered"]) for b in boards.values()),
        "/",
        sum(len(b["covered"]) + len(b["uncovered"]) for b in boards.values()),
    )
