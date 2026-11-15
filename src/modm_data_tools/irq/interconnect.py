# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""Extract two interconnect muxes from the reference manual: timer internal triggers (TIMx_SMCR.TS) and
DAC triggers (DAC_CR.TSELx). Normalizes the four table layouts the manuals use into
{sink field: {source signal: register value}} with the register address from the SVD.
"""

import re
from functools import cache

from . import sources as src


def signal(text: str, default_suffix="TRGO") -> str | None:
    """'TIM1' -> TIM1_TRGO, 'tim3_trgo' -> TIM3_TRGO, 'TIM14_OC1' -> TIM14_OC1, 'lptim1_out' -> LPTIM1_OUT"""
    text = src.clean(text).replace("<br>", "")
    if not text or text in {"-", "Reserved"}:
        return None
    text = re.split(r"[/(\[]", text)[0].strip()
    if re.fullmatch(r"[A-Z0-9]+( [A-Z0-9]+)+", text):  # 'USB SOF SYNC'
        text = text.replace(" ", "_")
    text = re.split(r" ", text)[0].upper().replace("-", "_")
    if re.fullmatch(r"TIM\d+", text):
        text += "_" + default_suffix
    # bare names (USB, ETH, SW) are truncated descriptions and collide with CMSIS peripheral macros
    return text if re.fullmatch(r"[A-Z]+\d*_[A-Z0-9_]+|EXTI\d+", text) else None


def ts_value(itr: int) -> int:
    # TIMx_SMCR.TS[4:0]: ITR0..3 = 0b00000..0b00011, ITR4..ITR15 = 0b01000.. (only TS[2:0] on L4 with ITR0..3)
    return itr if itr < 4 else 0b01000 + itr - 4


def timer_triggers(doc: str) -> dict[str, dict[str, int]]:
    out = {}
    for number, caption, _ in src.rm_tables(doc, lambda c, h: "internal trigger connection" in c):
        header, rows = src.rm_table(doc, number)
        if re.match(r"(?i)slave", header[0]):  # rows: slave timer, columns: ITRn
            itrs = [int(re.search(r"ITR(\d+)", h)[1]) if re.search(r"ITR(\d+)", h) else None for h in header]
            for r in rows:
                for slave in re.findall(r"TIM\d+", r[0]):
                    for i, cell in enumerate(r[1:], 1):
                        if itrs[i] is not None and (s := signal(cell)):
                            out.setdefault(f"{slave}.TS", {})[s] = ts_value(itrs[i])
        else:  # rows: tim_itrN, columns: slave timers (G4)
            slaves = [re.findall(r"TIM\d+", h) for h in header]
            for r in rows:
                m = re.fullmatch(r"tim_itr(\d+)", src.clean(r[0]))
                if not m:
                    continue
                for i, cell in enumerate(r[1:], 1):
                    for slave in slaves[i]:
                        if s := signal(cell):
                            out.setdefault(f"{slave}.TS", {})[s] = ts_value(int(m[1]))
    return out


def dac_triggers(doc: str) -> dict[str, dict[str, int]]:
    out = {}
    found = src.rm_tables(
        doc,
        lambda c, h: ("dac" in c and ("trigger" in c or "interconnection" in c) and "hrtim" not in c)
        or ("interconnect" in c and "dac trigger" in h),
    )
    for number, caption, _ in found:
        header, rows = src.rm_table(doc, number)
        low = [h.lower() for h in header]
        if low[0].startswith("signal name"):  # dac_chx_trgN -> source (G0, H7, one table per DAC on G4)
            dac = (re.search(r"DAC(\d)", caption) or ["", "1"])[1]
            for r in rows:
                if (m := re.match(r"dac_chx_trg(\d+)", src.clean(r[0]))) and (s := signal(r[1])):
                    for ch in (1, 2):
                        out.setdefault(f"DAC{dac}.TSEL{ch}", {})[s] = int(m[1])
        elif low[0] == "source" and any("tsel" in h for h in low):  # source -> binary TSEL (L4)
            ti = next(i for i, h in enumerate(low) if "tsel" in h)
            for r in rows:
                if (s := signal(r[0])) and re.fullmatch(r"[01]+", r[ti]):
                    for ch in (1, 2):
                        out.setdefault(f"DAC1.TSEL{ch}", {})[s] = int(r[ti], 2)
        elif "tsel" in low[0]:  # TSEL value -> per DAC instance (G4)
            for i, h in enumerate(header[1:], 1):
                if (m := re.search(r"DAC(\d)", h)) and "update" in h.lower():
                    for r in rows:
                        if r[0].isdigit() and (s := signal(r[i])):
                            for ch in (1, 2):
                                out.setdefault(f"DAC{m[1]}.TSEL{ch}", {})[s] = int(r[0])
    return out


def remaps(doc: str, timer: str) -> dict[str, dict]:
    """ITRn remap fields of the timer option register (F4 TIM2_OR.ITR1_RMP): ITRn -> field, options"""
    out = {}
    for reg in ("OR", "OR1"):
        try:
            text = src.docs.doc_register(f"{timer}_{reg}", doc=doc)
        except Exception:
            continue
        if not re.search(rf"\({timer}_{reg}\)", text):
            continue  # the lookup fell back to the register of another timer
        for field, n, desc in re.findall(r"^\| [\d:]+ \| (ITR(\d+)_RMP) \| \w+ \| (.*) \|$", text, re.M):
            options = {int(v, 2): src.clean(t) for v, t in re.findall(r"([01]+): ([^<]+)", desc)}
            out[f"ITR{n}"] = {"field": f"{reg}.{field}", "options": options}
    return out


@cache
def build(device: str) -> dict:
    """{sink: {address, parts: [[bit position, width]], values: {signal: value}}}"""
    rm = src.rm_of(device)
    per = src.header_svd(device)
    sinks = {}
    for name, values in {**timer_triggers(rm), **dac_triggers(rm)}.items():
        inst, field = name.split(".")
        if inst not in per:
            continue
        reg, f = next(((r, f) for r in per[inst]["registers"] for f in r["fields"] if f["name"] == field), (None, None))
        if not reg:
            continue
        # G4/H7 split TS[4:0] into TS[2:0] at bit 4 and TS[4:3] at bit 20 (CMSIS calls the upper part TS_3)
        parts = [[f["offset"], f["width"]]]
        if hi := next((g for g in reg["fields"] if g["name"] == field + "_3"), None):
            parts.append([hi["offset"], hi["width"]])
        values = {s: v for s, v in values.items() if v < (1 << sum(w for _, w in parts))}
        if values:
            sinks[name.replace(".", "_")] = {
                "address": per[inst]["base"] + reg["offset"],
                "parts": parts,
                "values": values,
                "remaps": remaps(rm, inst) if field == "TS" else {},
            }
    return {"device": device, "rm": rm, "sinks": sinks}
