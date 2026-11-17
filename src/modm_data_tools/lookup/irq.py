# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""The interrupt event graph and the interconnect muxes of STM32 devices (modm_data_tools.irq)"""

import re

from ..errors import UserRegex
from ..irq import graph as _graph, interconnect as _interconnect


def _bits(entry) -> str:
    if not entry:
        return "-"
    # header2svd names the DMAMUX channel 0 register CCR, the reference manual C0CR like the other channels
    return f"{'C0CR' if entry[0].startswith('DMAMUX') and entry[1] == 'CCR' else entry[1]}.{entry[2]}"


def irq_events(device: str, query: str = "") -> str:
    """Which events are ORed into which interrupt vector of a device, e.g. 'stm32g071rb'. Without `query` one line per
    vector with the peripheral instances (and which sources name them: name = IRQn name, rm = reference manual vector
    table, svd = ST SVD, cubemx = CubeMX NVIC). With `query` (regex on vector, instance or event, e.g. 'RTC', 'TIM6_DAC')
    the events of the matching vectors: enable, flag and clear bits, the clear method, the EXTI line and the sources
    that agree (name = paired by name, ll = CubeHAL LL functions, rm-table, rm-register)."""
    g = _graph.build(device)
    exti_regs = {r["name"] for r in _graph.src.header_svd(device).get("EXTI", {}).get("registers", [])}

    def exti_clear(line):  # the pending register of this device: PR, PR1, C1PR1 or RPR1/FPR1 (G0, C0, U0, L5, U5, H5)
        n = line // 32 + 1
        if f"RPR{n}" in exti_regs:
            return f"flag and clear EXTI_RPR{n}/FPR{n}"
        reg = next((r for r in (f"PR{n}", "PR" if n == 1 else "", f"C1PR{n}") if r in exti_regs), None)
        return f"flag and clear EXTI_{reg}" if reg else None

    out = [
        f"{g['device']}: {g['header']}, {g['rm'] or 'no reference manual'}, ST SVD {g['st_svd'] or '-'}, "
        f"{len(g['vectors'])} vectors, {len(g['events'])} events"
    ]
    if g["aliases"]:
        out.append(
            "CMSIS handler aliases that rename MODM_ISR(): "
            + ", ".join(f"{a}_IRQHandler -> {b}_IRQHandler" for a, b in g["aliases"].items())
        )
    by_vector = {}
    for e in g["events"]:
        for v in e["vectors"] or ["(no vector)"]:
            by_vector.setdefault(v, []).append(e)
    rx = UserRegex(query, re.I) if query else None
    for name, v in g["vectors"].items():
        srcs = ", ".join(f"{s}[{','.join(t)}]" for s, t in v["sources"].items())
        evs = by_vector.get(name, [])
        if rx is None:
            out.append(f"{v['irqn']:3} {name}: {srcs or '-'} ({len(evs)} events)")
            continue
        if not (
            rx.search(name)
            or any(rx.search(s) for s in v["sources"])
            or any(rx.search(f"{e['peripheral']}_{e['stem']}") for e in evs)
        ):
            continue
        out.append(f"\n{v['irqn']} {name}: {srcs or '-'}")
        if v["description"]:
            out.append(f"  RM: {v['description']}")
        if v["cubemx"]:
            out.append(f"  CubeMX: {v['cubemx']}")
        dropped = [e for e in evs if e["in_rm"] is False]
        for e in evs:
            if e["in_rm"] is False:
                continue
            if e["clear"]:
                clear = f"write 1 to {_bits(e['clear'])}" + (" keeping the rw bits" if e.get("clear_keep") else "")
            elif e["flag_access"] == "rc_w1":
                clear = "write 1 to the flag (no read-modify-write)"
            elif e["flag_access"] == "rc_w0":
                clear = "write 0 to the flag, 1 to the other rc_w0 flags, keep the rw bits"
            else:
                clear = {
                    "merged": "clear method not resolved (the RM merges the rows of several fields, see its text)",
                    "": "no clear bit (access unknown)",
                }.get(e["flag_access"], f"no clear bit ({e['flag_access']})")
            exti = f", EXTI line {e['exti'][0]} ({e['exti'][1]})" if e["exti"] else ""
            amb = f" ambiguous: {', '.join(e['vectors'])}" if len(e["vectors"]) > 1 else ""
            amb += f" (also raises {', '.join(e['also'])}, without EXTI)" if e.get("also") else ""
            ch = f" channel {e['channel']}" if e["channel"] else ""
            out.append(
                f"  {e['peripheral']}{ch} {e['stem']}: enable {_bits(e['enable'])}, flag {_bits(e['flag'])}, "
                f"{clear}{exti} [{','.join(e['provenance'])}]{amb}"
            )
        if dropped:
            out.append(
                "  dropped, the enable or flag bit is not in the RM register (shared CMSIS struct): "
                + ", ".join(f"{e['peripheral']} {e['stem']}" for e in dropped)
            )
        if m := re.fullmatch(r"EXTI(\d+)(?:_(\d+))?", name):  # GPIO lines: EXTI0_1, EXTI4_15, EXTI9_5
            a, b = sorted(int(n) for n in m.groups(default=m[1]))
            out.append(
                f"  EXTI lines {a}..{b} (GPIO, port selected in EXTICR): enable IMR + RTSR/FTSR, {exti_clear(a)}"
            )
        for x in g["exti"]:
            if name in x["vectors"] or (not x["vectors"] and rx.search(x["source"])):
                how = (
                    "not on this device (no EXTI pending register for it)"
                    if not exti_clear(x["line"])
                    else f"enable EXTI IMR + RTSR/FTSR, {exti_clear(x['line'])}"
                    if x["type"] == "configurable"
                    else "direct: the interrupt needs no EXTI setup, IMR only for wakeup"
                )
                out.append(f"  EXTI line {x['line']} ({x['source']}): {how}")
    return "\n".join(out)


def interconnect(device: str, sink: str = "", signal: str = "") -> str:
    """Peripheral to peripheral signal multiplexers of a device from the reference manual, with their values.
    Timer internal trigger inputs (TIMx_SMCR.TS, sinks named 'TIM2_TS') and DAC triggers (DAC_CR.TSELx, sinks named
    'DAC1_TSEL1'), with the register value per signal and the ITRn remaps of the timer option register
    (F4 TIM2_OR.ITR1_RMP). `sink` and `signal` filter by regex, a number does not continue into another one
    (sink='TIM2' is not TIM20), e.g. sink='TIM2', signal='TIM3_TRGO'."""
    ic = _interconnect.build(device)
    out = [f"{device}: {ic['rm']}, {len(ic['sinks'])} multiplexers"]
    sinks = UserRegex(rf"(?:{sink})(?!\d)" if re.search(r"\d$", sink) else sink, re.I) if sink else None
    signals = UserRegex(signal, re.I) if signal else None
    for name, s in ic["sinks"].items():
        if sinks and not (sinks.search(name) or sinks.search(name.split("_")[0])):
            continue
        values = {k: v for k, v in s["values"].items() if not signals or signals.search(k)}
        if not values:
            continue
        parts = " + ".join(f"bits {p + w - 1}:{p}" for p, w in s["parts"])

        def explain(v):
            # the timer input ITRn of a TS value, and the value split into its register fields, lowest bits first
            notes = [f"ITR{v if v < 4 else v - 8 + 4}"] if name.endswith("_TS") and (v < 4 or v >= 8) else []
            if len(s["parts"]) > 1:
                fields, rest = [], v
                for p, w in s["parts"]:
                    fields.append(f"{p + w - 1}:{p}={rest & ((1 << w) - 1):0{w}b}")
                    rest >>= w
                notes.append("bits " + " ".join(fields))
            return f" ({', '.join(notes)})" if notes else ""

        out.append(
            f"{name} @0x{s['address']:08x} ({parts}): "
            + ", ".join(f"{k}={v}{explain(v)}" for k, v in sorted(values.items(), key=lambda x: x[1]))
        )
        for itr, r in s.get("remaps", {}).items():
            out.append(
                f"  {itr} remapped by {name.split('_')[0]}_{r['field']}: "
                + ", ".join(f"{v}={t}" for v, t in r["options"].items())
            )
    return "\n".join(out)
