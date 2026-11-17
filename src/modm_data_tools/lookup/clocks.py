"""STM32 clock trees from the CubeMX clock plugin and RCC modes files.

The data is derived from STM32CubeMX and under the ST SLA: fine for local lookups, never publish it.
"""

import os
import re
from functools import cache

from ..errors import UserError
from .headers import normalize


@cache
def _partnames(prefix: str) -> list[str]:
    from modm_data.cubemx import devices_from_prefix

    return devices_from_prefix(prefix)


@cache
def _tree(partname: str) -> dict:
    from modm_data.cubemx import clock_tree_from_partname

    return clock_tree_from_partname(partname)


def _device(device: str) -> tuple[str, list[str]]:
    """The CubeMX part name for a device prefix, e.g. 'g071rbt6' is STM32G071RBTx, and all that match"""
    name = "STM32" + normalize(device).split("@")[0].upper()
    # CubeMX names the temperature range x, so the ordering code is cut until something matches, but not
    # into the line: 'wba52cg' must not end up at the STM32WB05
    for length in range(len(name), max(len(name) - 4, len("STM32F0")) - 1, -1):
        if found := _partnames(name[:length]):
            return found[0], found
    raise UserError(f"no CubeMX device matches '{device}'")


def _hz(value: float | None) -> str:
    if value is None:
        return "?"
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:.6g} {unit}"
    return f"{value:.6g} Hz"


def _span(low: float | None, high: float | None) -> str:
    if not low and high is None:
        return "no limit"
    if low == high:
        return _hz(low)
    if low is None or low == 0:
        return f"<={_hz(high)}"
    return f">={_hz(low)}" if high is None else f"{_hz(low)}..{_hz(high)}"


def _numbers(values: list[float]) -> str:
    """1,2,3,4,8 as 1..4,8 and 2,4,..,128 as 2..128/2"""
    values, runs = sorted(values), []
    steps = {b - a for a, b in zip(values, values[1:])}
    if len(values) > 3 and len(steps) == 1 and (step := steps.pop()) > 1:
        return f"{values[0]:g}..{values[-1]:g}/{step:g}"
    for value in values:
        if runs and value == runs[-1][1] + 1 and value == int(value):
            runs[-1][1] = value
        else:
            runs.append([value, value])
    return ",".join(f"{a:g}" if a == b else f"{a:g}{',' if b == a + 1 else '..'}{b:g}" for a, b in runs)


def _condition(gate: list) -> str:
    kind = gate[0]
    if kind == "var":
        return gate[1]
    if kind == "eq":
        return f"{gate[1]}={gate[2]}"
    if kind == "cmp":
        return f"{gate[1]}{gate[2]}{gate[3]}"
    if kind == "const":
        return str(gate[1]).lower()
    if kind == "not":
        inner = _condition(gate[1])
        return f"!{inner}" if gate[1][0] in ("var", "const") else f"!({inner})"
    parts = [
        _condition(g) if g[0] in ("var", "eq", "cmp", "not", "const", kind) else f"({_condition(g)})" for g in gate[1:]
    ]
    return f" {'&' if kind == 'and' else '|'} ".join(parts)


def _short(text: str, limit: int = 160) -> str:
    # the STM32H7 PLL limits hold if any of ~30 kernel clocks uses the PLL
    return text if len(text) <= limit else text[: limit - 5] + " ...)"


def _variants(variants: list[dict], prefix: str) -> str:
    return " | ".join(
        (f"if {_short(_condition(v['when']))}: " if "when" in v else "else ")
        + _span(v.get(f"{prefix}min"), v.get(f"{prefix}max"))
        for v in variants
    )


def _line(name: str, node: dict, macros: bool = False) -> str:
    """One node as `name kind setting <- inputs, limits`, with the macros of mux inputs and dividers if wanted"""
    kind, parts = node["kind"], [name, node["kind"]]
    if kind == "fixed":
        parts.append(_hz(node.get("value")))
    elif kind in ("varied", "choice"):
        parts.append(_hz(node.get("default")))
        if kind == "choice" and node.get("domain"):
            parts.append("of " + ",".join(_hz(d["value"]).replace(" ", "") for d in node["domain"]))
    elif kind == "mux":
        # the node with its macro in the detail view, PLL2Q=RCC_USART16CLKSOURCE_PLL2
        inputs = [f"{i['from']}={i['macro']}" if macros and i.get("macro") else i["from"] for i in node["inputs"]]
        parts.append(
            "<- "
            + " | ".join(
                f"{i}*" if node.get("default") in (m.get("macro"), m["from"]) else i
                for i, m in zip(inputs, node["inputs"])
            )
        )
    elif kind in ("div", "mul", "mulfrac", "frac"):
        if rules := node.get("rules"):
            parts.append(
                ", ".join(
                    ("x" if kind == "mul" else "/")
                    + f"{r['factor']:g}"
                    + (f" if {_condition(r['when'])}" if "when" in r else "")
                    for r in rules
                )
            )
        elif "range" in node:
            low, high = node["range"]
            parts.append(f"{low}" if low == high else f"{low}..{high}")
        elif "domain" in node:
            prefix = os.path.commonprefix([d["macro"] for d in node["domain"]])
            if macros and all(d["macro"] == f"{prefix}{d['factor']:g}" for d in node["domain"]):
                # RCC_PLLP_DIV2..RCC_PLLP_DIV32 as one pattern
                numbers = _numbers([d["factor"] for d in node["domain"]])
                parts.append(f"{numbers} as {prefix}<n>" if prefix else numbers)
            elif macros:
                parts.append(", ".join(f"{d['factor']:g}={d['macro']}" for d in node["domain"]))
            else:
                parts.append(_numbers([d["factor"] for d in node["domain"]]))
        if "default" in node and not node.get("rules"):
            parts.append(f"(default {node['default']:g})")
    if "init" in node and node["init"] is not None:
        # the default is the reset value, the CubeMX generated code writes another one
        init = node["init"] if isinstance(node["init"], str) else f"{node['init']:g}"
        parts.append(f"(CubeMX init {init})")
    if kind != "mux" and (sources := [i["from"] for i in node["inputs"]]):
        # the same source on several signals, for example, the STM32F107 HSE and XT1
        signals = {}
        for i in node["inputs"]:
            signals.setdefault(i["from"], []).append(i.get("signal", ""))
        labels = [f"{s}:{'/'.join(signals[s])}" if len(set(signals[s])) > 1 else s for s in dict.fromkeys(sources)]
        parts.append("<- " + ", ".join(labels))
    if kind in ("fixed", "varied", "output") and (node.get("min") or "max" in node):
        parts.append(
            "["
            + (
                _variants(node["limits"], "")
                if macros and "limits" in node
                else _span(node.get("min"), node.get("max"))
            )
            + "]"
        )
    if node.get("out_min") or "out_max" in node:
        parts.append(
            "out ["
            + (
                _variants(node["out_limits"], "out_")
                if macros and "out_limits" in node
                else _span(node.get("out_min"), node.get("out_max"))
            )
            + "]"
        )
    if macros:
        if "param" in node:
            parts.append(f"param {node['param']}")
        if "when" in node:
            parts.append(f"when {' & '.join(node['when'])}")
        if node.get("outputs"):
            parts.append("-> " + ", ".join(node["outputs"]))
    return " ".join(parts)


def _find(tree: dict, name: str) -> list[str]:
    """The nodes a name stands for: a node, parameter, peripheral, an alias like SYSCLK or PCLK1 like in clock_solve, or
    a peripheral family like FDCAN for all its instances"""
    from ..clocks.configure import find_node

    try:
        found = find_node(tree, name)
        return [found] if found in tree["nodes"] else [tree["clocks"][found]]
    except UserError:
        family = [n for n in tree["nodes"] if re.fullmatch(rf"{re.escape(name)}\d+", n, re.I)]
        if family:
            return family
        close = [n for n in tree["nodes"] if name.lower() in n.lower()][:8]
        raise UserError(
            f"no node '{name}' in the clock tree of {tree['device']}"
            + (f", similar: {', '.join(close)}" if close else "")
        ) from None


def clock_tree(device: str, node: str = "", config: str = "", use: str = "") -> str:
    """Clock tree of a device from STM32CubeMX with domains, limits and the clock of every peripheral.
    Its sources, muxes, dividers, multipliers and outputs with their domains, defaults (marked *), frequency limits
    (`[..]` of the node, `out [..]` of its output signal, the widest over all voltage scales) and which node clocks
    each peripheral.
    `node`: comma separated nodes, parameters or peripherals to show in detail with the macros, the limits per
    configuration and the path from the clock sources.
    Defaults are the register reset values where CubeMX's own default differs (shown as `CubeMX init`).
    `config`: settings as name=value, e.g. 'SysClkSource=PLLCLK, PLLN=16, APBPrescaler=2, HSEOSC=8M, HSE=ByPass,
    PWR_Regulator_Voltage_Scale=SCALE2', evaluates the frequency of every node (`= ...`), marks violated limits with
    `!!` and gives the flash wait states; unset nodes stay at their default, options CubeMX derives (voltage scale,
    over-drive) follow the clocks unless set.
    `use`: comma separated peripherals and features in use, e.g. 'USART1,USB_OTG_FS,MCO': sets their CubeMX usage
    flags and only they, their bus and the buses count as used; without it everything counts as used.
    Derived from CubeMX (ST SLA), do not publish."""
    from modm_data.cubemx import evaluate_clock_tree

    partname, matches = _device(device)
    tree = _tree(partname)
    if tree is None:
        raise UserError(f"CubeMX has no clock tree for {partname}")
    nodes = tree["nodes"]
    # the defaults also choose the path through the muxes, but their frequencies are only shown for a config
    used = {u.strip() for u in use.split(",") if u.strip()} if use else None
    try:
        # the names and aliases of clock_solve, HSE=8M is the HSE oscillator, HSE=ByPass its mode
        from ..clocks.configure import parse_config

        evaluated = evaluate_clock_tree(tree, parse_config(tree, config), used)
    except ValueError as e:
        # an unknown node or a value it cannot take, the message names them
        raise UserError(str(e)) from None
    result = evaluated if config or use else None

    def line(name: str, macros: bool = False) -> str:
        text = _line(name, nodes[name], macros)
        if result and (value := result[name]["freq"]) is not None and nodes[name]["kind"] != "frac":
            text += f" = {_hz(value)}" + ("" if result[name]["used"] else " (unused)")
            # the limits of this configuration, when they are not the static ones
            node = nodes[name]
            for prefix, (low, high) in result[name].get("bounds", {}).items():
                if (low, high) != (node.get(f"{prefix}min"), node.get(f"{prefix}max")) and (low or high):
                    text += f" {'out ' if prefix else ''}within [{_span(low, high)}]"
            if result[name]["used"] and result[name]["errors"]:
                text += " !! " + ", ".join(result[name]["errors"])
        return text

    header = (
        f"{partname} ({len(matches)} CubeMX devices match '{device}', the first one is shown)"
        if len(matches) > 1
        else partname
    )
    lines = [
        f"{header}: drawing {tree['tree']}, "
        + (f"RCC {tree['rcc']}, " if tree.get("rcc") else "")
        + f"{sum(n['kind'] != 'peripheral' for n in nodes.values())} nodes, {len(tree['clocks'])} peripherals"
    ]
    options = evaluated["options"]["setting"] if result else tree["options"]
    if options:
        lines.append("options: " + ", ".join(f"{k}={v}" for k, v in options.items()))
    if tree.get("modes"):
        lines.append("modes: " + "; ".join(f"{k}={'|'.join(v)}" for k, v in tree["modes"].items()))

    if node:
        for name in dict.fromkeys(f for n in node.split(",") if n.strip() for f in _find(tree, n.strip())):
            lines.append("")
            lines.append(line(name, macros=True))
            # the path from the clock source through the chosen mux inputs
            seen, stack, path = {name}, [name], []
            while stack:
                current = stack.pop()
                for input in nodes[current]["inputs"]:
                    source = input["from"]
                    chosen = evaluated[current]["setting"] in (input.get("macro"), source)
                    if source in nodes and source not in seen and (nodes[current]["kind"] != "mux" or chosen):
                        seen.add(source)
                        stack.append(source)
                        path.append(source)
            if path:
                lines.append("  from: " + "; ".join(line(p) for p in path))
            if attached := sorted(p for p, n in tree["clocks"].items() if n == name and p != name):
                lines.append("  clocks: " + " ".join(attached))
            if bus := nodes[name].get("bus"):
                lines.append(f"  bus: {line(bus)}")
        return "\n".join(lines)

    if result:
        errors = [f"{n} {', '.join(r['errors'])}" for n, r in result.items() if r["used"] and r["errors"]]
        lines.append(f"violations: {'; '.join(errors)}" if errors else "violations: none")
        if "FLatency" in result and result["FLatency"]["setting"]:
            lines.append(f"flash: {result['FLatency']['setting']}")
    lines.append("")
    lines += [line(name) for name, n in nodes.items() if n["kind"] != "peripheral"]
    groups = {}
    for peripheral, name in sorted(tree["clocks"].items()):
        # the bus clock of a peripheral with a kernel clock of its own in brackets
        bus = nodes[peripheral].get("bus")
        groups.setdefault(name, []).append(f"{peripheral}[{bus}]" if bus else peripheral)
    lines.append("")
    lines.append("peripherals: " + "; ".join(f"{name}: {' '.join(ps)}" for name, ps in groups.items()))
    if tree["unattached"]:
        lines.append("unattached: " + " ".join(tree["unattached"]))
    return "\n".join(lines)


def clock_solve(device: str, targets: str, config: str = "", use: str = "", lock: str = "", code: bool = False) -> str:
    """Finds a clock configuration for frequency targets on the CubeMX clock tree of a device and explains a failure.
    `targets`: comma separated, e.g. 'SYSCLK=max, USB=48M, SPI1=49.152M~0.1%, ADC<=50M, I2C1>=8M, SAI1=12.288M..12.3M'
    (~ relative tolerance, otherwise exact). Names are nodes, CubeMX parameters, peripherals or SYSCLK, HCLK, CPU, PCLK1..4.
    `config`: the board and earlier choices as name=value, e.g. 'HSE=8M, HSE=ByPass, LSE=32.768k' or a `config:` line of
    an earlier result. External clocks (HSE, LSE, clock pins) count only when set here; with an HSE the core runs from
    it when possible. The solver keeps every setting of config unless a target needs a change.
    `use`: further peripherals in use, which the limits and `code` take into account; targets count as in use.
    `lock`: nodes the solver must not move. `code`: the modm Board::SystemClock struct for the result.
    Work iteratively: start with the core (SYSCLK=max), pass the returned config back with more targets.
    Derived from CubeMX (ST SLA), do not publish."""
    return _solve(device, targets, config, use, lock, code)[0]


def _solve(device: str, targets: str, config: str = "", use: str = "", lock: str = "", code: bool = False):
    """The text of clock_solve and the solution for the MCP app: {partname, tree, result, targets: {id: Hz}, use,
    config}"""
    from ..clocks.configure import report, solve_text
    from ..clocks.modm import generate
    from ..clocks.modm_api import api_of

    partname, matches = _device(device)
    tree = _tree(partname)
    if tree is None:
        raise UserError(f"CubeMX has no clock tree for {partname}")
    solved = solve_text(tree, targets, config, use, lock)
    source = None
    if code and solved["ok"]:
        try:
            source = generate(tree, partname, solved["config"], solved["result"], set(solved["use"]), api_of(partname))
        except (OSError, RuntimeError, ValueError) as error:
            # no modm repository, lbuild failed or the device has no modm target
            source = f"// no modm code: {error}"
    elif code:
        source = "// no code without a solution"
    header = f"{partname} (first of {len(matches)} CubeMX devices for '{device}')" if len(matches) > 1 else partname
    result = solved["result"]
    view = {"partname": partname, "tree": tree, "result": result, "use": solved["use"], "config": solved["config"],
            "targets": {n: result[n]["freq"] for n in solved["targets"] if n in result}}  # fmt: skip
    return report(tree, solved, header, source), view
