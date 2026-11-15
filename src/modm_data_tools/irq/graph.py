# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""
The interrupt event graph of an STM32 device: which events (enable bit, flag, clear method, EXTI line) are ORed
into which vector, merged from the sources in `sources` with the provenance of every fact.

Provenance tags:
- vectors: `name` (header IRQn name), `rm` (reference manual vector table), `svd` (ST SVD), `cubemx` (NVIC modes)
- events: `name` (enable/flag/clear paired by name in the header SVD), `ll` (CubeHAL LL functions),
  `rm-table` (RM interrupt event table), `rm-register` (the enable bit exists in the RM register description)
"""

import collections
import re
from functools import cache

from . import sources as src

CORE = {"NVIC", "SCB", "SysTick", "MPU", "FPU", "DWT", "ITM", "CoreDebug", "TPI", "SCnSCB", "FPU_EX", "MEMORY"}

# Which sub-vector of a peripheral with several vectors an event goes to: rules no table gives
SUBVECTOR = [
    (r"TIM\d+", r"^(U|UIE)$", "UP"),
    (r"TIM\d+", r"^CC\d", "CC"),
    (r"TIM\d+", r"^(T|COM|IDX|DIR)$", "TRG_COM"),
    (r"TIM\d+", r"^(B|B2|SB|IERR|TERR)$", "BRK"),
    (r"RTC", r"^WUT", "WKUP"),
    (r"RTC", r"^(SEC|OW)$", "^RTC$"),
    (r"RTC", r"^ALR", "Alarm"),
    (r"RTC", r"^(TS|ITS|TAMP|TSOV)", "TAMP|STAMP"),
    (r"I2C\d", r"^(BERR|ARLO|OVR|PECERR|TIMEOUT|ALERT|ERR)$", "ER"),
    (r"I2C\d", r".", "EV"),
    (r"RCC", r"^LSECSS", "LSECSS|TAMP"),
    (r"RCC", r".", "^RCC"),
    (r"DAC[13]", r".", "TIM6_DAC"),
    (r"DAC[24]", r".", "TIM7_DAC"),
    (r"CAN\d?", r"^(TME)$", "TX"),
    (r"CAN\d?", r"^(FMP0|FF0|FOV0)$", "RX0"),
    (r"CAN\d?", r"^(FMP1|FF1|FOV1)$", "RX1"),
    (r"CAN\d?", r".", "SCE"),
]

RTC_EXTI = [
    (r"SEC|OW", r"second|overflow"),
    (r"WUT", r"wake"),
    (r"ALR", r"alarm"),
    (r"I?TS", r"stamp"),
    (r"I?TAMP", r"tamper|\bTAMP\b"),
]

# Pairs the naming convention misses, (instance regex, enable field, flag field), every entry a manual patch
PATCHES = [
    (r"WWDG\d?", "EWI", "EWIF"),
    (r"BKP", "TPIE", "TIF"),
    (r"FLASH", "ERRIE", "OPERR"),
    (r"FLASH", "OPERRIE", "OPERR"),
    (r"RCC", "HSECSSIE", "HSECSSF"),
    (r"RCC", "PLL1RDYIE", "PLLRDYF"),
    (r"COMP1", "ITEN", "COMP12:C1IF"),
    (r"COMP2", "ITEN", "COMP12:C2IF"),
]

# One enable bit for flags of other names, (instance regex, enable field regex, flags as FLAG or FLAG:CLEAR)
SHARED_ENABLE = [
    (r"TIM\d+", "BIE", ["B2IF", "SBIF"]),  # break2 and system break interrupt "if BIE=1"
    (r"(LP)?U?S?ART\d+", r"RXNEIE(_RXFNEIE)?", ["ORE:ORECF"]),
    (r"(LP)?U?S?ART\d+", "EIE", ["NE:NECF/NCF", "FE:FECF"]),  # F0/F3/F7 manuals call NE 'NF', its clear NCF
    (
        r"I2C\d+",
        "ERRIE",
        ["BERR:BERRCF", "ARLO:ARLOCF", "OVR:OVRCF", "PECERR:PECCF", "TIMEOUT:TIMOUTCF", "ALERT:ALERTCF"],
    ),
    (r"I2C\d+", "TXIE", ["TXIS"]),
    (r"I2C\d+", "RXIE", ["RXNE"]),
    (r"I2C\d+", "TCIE", ["TCR"]),
]

DRIVER = (
    r"^(USART|UART|LPUART|SPI|I2C|I2S|TIM|LPTIM|ADC|USB|OTG|FDCAN|CAN|ETH|SDMMC|SDIO|QUADSPI|OCTOSPI|XSPI|SAI|DCMI|"
    r"PSSI|LTDC|DMA2D|HRTIM|UCPD|CEC|SWPMI|MDIOS|DFSDM|MDF|ADF|FMC|FSMC|SPDIF|CRYP|HASH|AES|SAES|PKA|RNG|CORDIC|"
    r"FMAC|MDMA|TSC|LCD|JPEG|GPU2D|GFXMMU|DSI|SUBGHZ|IPCC|HSEM|GPDMA|HPDMA|LPDMA|DMA|BDMA)"
)


# ---------------------------------------------------------------------------------------------------------------------
def _instances(peripherals: dict) -> set[str]:
    instances = {p for p in peripherals if p not in CORE}
    # TrustZone headers define secure aliases at other addresses (GPDMA1_S next to GPDMA1)
    instances = {
        p for p in instances if not (p.endswith("_S") and (p[:-2] in instances or p[:-2] + "_NS" in instances))
    }
    # the headers define aliases (DAC and DAC1 at one address), keep the name with a digit
    by_base = {}
    for p in sorted(instances, key=lambda p: (not re.search(r"\d$", p), p)):
        by_base.setdefault(peripherals[p]["base"], p)
    return set(by_base.values()) | {p for p in instances if not peripherals[p]["base"]}


def _canon(name: str, instances: set[str], peripherals: dict) -> str | None:
    """The header instance of a name another source uses (DAC -> DAC1, WWDG -> WWDG1, ADC12_Common -> ADC1)"""
    name = name.upper()
    if name in instances:
        return name
    if name in peripherals and peripherals[name]["base"]:
        base = peripherals[name]["base"]
        return next((i for i in instances if peripherals[i]["base"] == base), None)
    for cand in (
        name + "1",
        re.sub(r"1$", "", name),
        re.sub(r"_COMMON$", "", name),
        re.sub(r"(\d)(\d)_COMMON$", r"\1", name),
        re.sub(r"(\D)(\d)\d_COMMON$", r"\1\2", name),
    ):
        if cand in instances:
            return cand
    return None


def _name_sources(name: str, acronym: str, description: str, instances: set[str]) -> set[str]:
    """Peripheral instances named by the vector name, its RM acronym and description"""
    acronym = re.sub(r"([A-Z]+\d) (\d)\b", r"\1\2", acronym)  # pdf2html splits 'TIM8_TRG_COM_TIM14' into 'TIM1 4'
    text = f"{name} {acronym.replace('/', ' ')} {description}".replace("_", " ")
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*|\d+", text)
    found, prev = set(), None
    for i, tok in enumerate(tokens):
        up = tok.upper()
        number_follows = i + 1 < len(tokens) and tokens[i + 1].isdigit()
        if up == "COMP" and "COMP" not in instances:
            found |= {i for i in instances if re.fullmatch(r"COMP\d", i)}
        elif up in instances:
            found.add(up)
            prev = re.sub(r"\d+$", "", up)
        elif up + "1" in instances and len(up) > 2 and not number_follows:
            found.add(up + "1")
            prev = up
        elif up + "1" in instances and number_follows:  # 'SPI 4 global interrupt'
            prev = up
        elif tok.isdigit() and prev and f"{prev}{tok}" in instances:
            found.add(f"{prev}{tok}")
        elif up in {"PVD", "PVM", "AVD", "WAKEUP"} and "PWR" in instances:
            found.add("PWR")
        elif up in {"LSECSS", "CSS"}:
            found.add("RCC")
        elif up in {"STAMP", "WKUP", "ALARM"} and re.search(r"RTC|TAMP", name) and "RTC" in instances:
            found.add("RTC")
        elif up == "TAMPER" and "BKP" in instances:  # F1: the tamper pin is in the backup domain registers
            found.add("BKP")
        elif up == "ECC" and "RAMECC1" in instances:
            found.add("RAMECC1")
        else:
            prev = prev if tok.isdigit() else None
    for ctl, _, a, b in re.findall(r"((?:GP|HP|LP)?B?DMA\d?)_(Channel|Ch|Stream|CH)(\d+)(?:_(\d+))?", name):
        for n in range(int(a), int(b or a) + 1):
            found.add(f"{ctl}#{n}")
    for ctl, chans in re.findall(r"(DMA\d) channels? ([\d, ]+)", description):
        for n in re.findall(r"\d+", chans):
            found.add(f"{ctl}#{n}")
    return found


def _fields(peripherals: dict, instance: str) -> list[tuple]:
    return [
        (instance, r["name"], f["name"], f["offset"], f["width"])
        for r in peripherals[instance]["registers"]
        for f in r["fields"]
    ]


def _clear_candidates(base: str, stem: str, idx: str) -> list[str]:
    return [
        c
        for c in (
            "C" + base,
            base[:-1] + "C" if base.endswith("F") else "",
            base[:-1] + "CF" if base.endswith("F") else "",
            stem + "C" + idx,
            stem + "CF" + idx,
            base + "C",
            "CLR_" + base,
            "C" + base[:-1] if base.endswith("F") else "",
            base,
        )
        if c
    ]


def _pair_by_name(peripherals: dict, instance: str) -> list[dict]:
    """Interrupt enable bits paired with flag and clear bits by name"""
    fields = _fields(peripherals, instance)
    base_pool, channel_of_instance = fields, None
    # DMA1_Stream0 / BDMA_Channel3 instances: flags are in the controller (DMA1_LISR.TCIF0)
    if (m := re.fullmatch(r"(B?DMA\d?)_(?:Stream|Channel)(\d+)", instance)) and m[1] in peripherals:
        base_pool, channel_of_instance = fields + _fields(peripherals, m[1]), m[2]
    events = []
    for _, reg, name, off, width in fields:
        m = re.fullmatch(r"(\w+?)IE(\d*)", name)
        patch = next((fl for rx, en, fl in PATCHES if re.fullmatch(rx, instance) and en == name), None)
        if not (m or patch) or width != 1:
            continue
        stem, idx = (m[1], m[2]) if m else (name, "")
        pool = base_pool
        if patch and ":" in patch:
            other, patch = patch.split(":")
            if other in peripherals:
                pool = fields + _fields(peripherals, other)
        channel = channel_of_instance
        cands = [patch] if patch else [stem + "F" + idx, stem + "IF" + idx, stem + idx, stem + "_F" + idx]
        if re.match(r"(GP|HP|LP)DMA", instance) and (cm := re.fullmatch(r"CCR(\d+)", reg)):
            n = cm[1]
            flag = next(((i, r, f, o) for i, r, f, o, w in pool if r == f"CSR{n}" and f == stem + "F"), None)
            if flag:
                clr = next(((i, r, f, o) for i, r, f, o, w in pool if r == f"CFCR{n}" and f == stem + "F"), None)
                events.append(
                    {
                        "peripheral": instance,
                        "stem": stem,
                        "channel": n,
                        "patched": False,
                        "enable": [instance, reg, name, off],
                        "flag": list(flag),
                        "clear": list(clr) if clr else None,
                        "provenance": ["name"],
                    }
                )
            continue
        if instance.startswith("DMAMUX") and reg == "CCR":  # header2svd names the channel 0 register CCR
            channel = "0"
        elif (
            instance.startswith(("DMA", "BDMA"))
            and not channel
            and (cm := re.fullmatch(r"CCR(\d+)|S(\d+)F?CR|C(\d+)CR|RG(\d+)CR", reg))
        ):
            if reg.startswith("RG"):  # DMAMUX request generator: RGxCR.OIE -> RGSR.OFx
                cands, stem = [f"{stem}F{cm[4]}"], f"RG{cm[4]}_{stem}"
            else:
                channel = next(c for c in cm.groups() if c is not None)
        if channel:
            cands = [f"{stem}IF{channel}", f"{stem}F{channel}"]
        flag = next(((i, r, f, o) for c in cands for i, r, f, o, w in pool if f == c and (r, f) != (reg, name)), None)
        if not flag:
            continue
        clr = next(
            (
                (i, r, f, o)
                for c in _clear_candidates(flag[2], stem, idx)
                for i, r, f, o, w in pool
                if f == c and r not in (reg, flag[1])
            ),
            None,
        ) or next(
            (
                (i, r, f, o)
                for c in _clear_candidates(flag[2], stem, idx)[:-1]
                for i, r, f, o, w in pool
                if f == c and r == flag[1] and f != flag[2]
            ),
            None,
        )
        events.append(
            {
                "peripheral": instance,
                "stem": stem + idx,
                "channel": channel,
                "patched": bool(patch),
                "enable": [instance, reg, name, off],
                "flag": list(flag),
                "clear": list(clr) if clr else None,
                "provenance": ["name"],
            }
        )
    for rx, en, flags in SHARED_ENABLE:
        if not re.fullmatch(rx, instance) or not (enable := next((x for x in fields if re.fullmatch(en, x[2])), None)):
            continue
        for fl, _, cl in (f.partition(":") for f in flags):
            if flag := next(((i, r, f, o) for i, r, f, o, w in fields if f == fl), None):
                clr = (
                    next(((i, r, f, o) for c in cl.split("/") for i, r, f, o, w in fields if f == c), None)
                    if cl
                    else None
                )
                events.append(
                    {
                        "peripheral": instance,
                        "stem": fl.removesuffix("IF").removesuffix("F"),
                        "channel": None,
                        "patched": True,
                        "enable": list(enable[:4]),
                        "flag": list(flag),
                        "clear": list(clr) if clr else None,
                        "provenance": ["name"],
                    }
                )
    return events


def _ll_type(instance: str) -> str:
    return re.sub(r"\d+$", "", re.sub(r"_(Stream|Channel)\d+$", "", instance))


def _pair_by_ll(peripherals: dict, instance: str, ll: dict) -> dict[tuple[str, str], dict]:
    """LL events of the peripheral type whose enable and flag fields exist in this instance: (enable, flag) -> ll"""
    fields = _fields(peripherals, instance)
    by_reg = {(r, f): o for _, r, f, o, w in fields}
    by_name = {}
    for _, r, f, o, w in fields:
        by_name.setdefault(f, []).append((r, o))

    def resolve(reg, field):
        if (reg, field) in by_reg:
            return reg, field
        if len(by_name.get(field, [])) == 1:
            return by_name[field][0][0], field
        return None

    out = {}
    # one enable bit for several numbered flags (old RTC: TAFCR.TAMPIE for TAMP1F/TAMP2F): EnableIT_TAMP, IsActiveFlag_TAMPn
    ll = dict(ll)
    for (per, name), e in list(ll.items()):
        if per == _ll_type(instance) and "EnableIT" in e and "IsActiveFlag" not in e:
            for (p2, n2), e2 in list(ll.items()):
                # the LL of a family can also cover a newer RTC (H7RS IER.TAMPnIE) that this instance does not have
                if (
                    p2 == per
                    and re.fullmatch(re.escape(name) + r"\d+", n2)
                    and "IsActiveFlag" in e2
                    and ("EnableIT" not in e2 or not resolve(*e2["EnableIT"]))
                ):
                    ll[(p2, n2)] = {**e2, "EnableIT": e["EnableIT"]}
    for (per, name), e in ll.items():
        if per != _ll_type(instance) or "EnableIT" not in e or "IsActiveFlag" not in e:
            continue
        en, fl = resolve(*e["EnableIT"]), resolve(*e["IsActiveFlag"])
        if en and fl:
            cl = resolve(*e["ClearFlag"]) if "ClearFlag" in e else None
            out[(en[1], fl[1])] = {
                "enable": en,
                "flag": fl,
                "clear": cl,
                "clear_kind": e.get("clear_kind"),
                "name": name,
            }
    return out


def _title_instances(title: str) -> set[str]:
    """Instances a chapter title names: 'Basic timers (TIM6/TIM7)', 'TIM2 to TIM5', 'TIM15/16/17'"""
    names = set()
    for per, a, rest in re.findall(r"\b([A-Z]+)(\d+)((?:\s*(?:/|,|to|and)\s*(?:[A-Z]+)?\d+)*)", title):
        nums = [int(a)] + [int(n) for n in re.findall(r"\d+", rest)]
        if re.search(r"\bto\b", rest) and len(nums) == 2:
            nums = range(nums[0], nums[1] + 1)
        names |= {f"{per}{n}" for n in nums}
    return names


def _rm_confirms(event: dict, rm_events: dict[str, list[dict]]) -> bool:
    inst = event["peripheral"]
    per = re.sub(r"\d+$", "", inst)
    flag, en = event["flag"][2], event["enable"][2]
    flag_g = re.sub(r"\d+$", "x", flag)
    titles = [t for t in rm_events if re.search(rf"\b{per}", t.replace("/", " "))]
    # several chapters of one type (basic, general-purpose, advanced timers): only the one naming the instance
    if named := [t for t in titles if inst in _title_instances(t)]:
        titles = named
    for title in titles:
        rows = rm_events[title]
        for row in rows:
            if re.search(
                rf"\b({re.escape(flag)}|{re.escape(flag_g)}|{re.escape(en)})\b", row["flag"] + " " + row["enable"]
            ):
                return True
    return False


# ---------------------------------------------------------------------------------------------------------------------
@cache
def build(device: str) -> dict:
    peripherals = src.header_svd(device)
    instances = _instances(peripherals)
    irqs = src.header_vectors(device)
    try:
        rm = src.rm_of(device)
    except StopIteration:
        rm = None
    vtab = src.rm_vectors(rm, irqs) if rm else {}
    exti = dict(src.rm_exti(rm)) if rm else {}
    # manuals without a line type (F0/F1/F3/F4/L1): a line with a trigger selection bit is configurable, the internal
    # lines (RM0091: "considered as internal") have none
    rtsr = {
        int(n)
        for r in peripherals.get("EXTI", {}).get("registers", [])  # RTSR2 fields count on: TR32, TR33
        if re.fullmatch(r"(C1)?RTSR\d?", r["name"])
        for f in r["fields"]
        for n in re.findall(r"^(?:TR|RT)(\d+)$", f["name"])
    }
    exti = {
        line: info if info["type"] else {**info, "type": "configurable" if line in rtsr else "direct"}
        for line, info in exti.items()
    }
    rm_events = src.rm_event_tables(rm) if rm else {}
    st_svd = src.st_svd_interrupts(device)
    cubemx = src.cubemx_vectors(device)
    ll = src.ll_device_events(device)

    def addr(instance, register):
        r = next(r for r in peripherals[instance]["registers"] if r["name"] == register)
        return peripherals[instance]["base"] + r["offset"]

    # vectors and the instances that can raise them, per source
    vectors = {}
    for name, n in sorted(irqs.items(), key=lambda x: x[1]):
        v = vtab.get(n, {"acronym": "", "description": "", "signals": ""})
        by = {
            "name": _name_sources(name, "", "", instances),
            "rm": _name_sources(name, v["acronym"], v["description"], instances) if v["acronym"] else set(),
            "svd": {c for p in st_svd.get(name, ()) if (c := _canon(p, instances, peripherals))},
            "cubemx": {
                c for p in cubemx.get(name, {}).get("instances", ()) if (c := _canon(p, instances, peripherals))
            },
        }
        if not name.startswith("EXTI"):  # "through EXTI line 16" names the path, not a source
            for found in by.values():
                found.discard("EXTI")
        sources = set().union(*by.values())
        lines = sorted(
            {
                int(x)
                for m in re.findall(
                    r"EXTI\b([^.;)]*)", v["description"] + " " + cubemx.get(name, {}).get("comment", "")
                )
                for x in re.findall(r"\d+", m)
                if 15 < int(x) < 128
            }
        )
        vectors[name] = {
            "irqn": n,
            "acronym": v["acronym"],
            "description": v["description"],
            "signals": v["signals"],
            "cubemx": cubemx.get(name, {}).get("comment", ""),
            "exti_lines": lines,
            "sources": {s: sorted(t for t, found in by.items() if s in found) for s in sorted(sources)},
        }
    by_source = {}
    for name, v in vectors.items():
        for s in v["sources"]:
            by_source.setdefault(s, []).append(name)

    events = []
    for inst in sorted(instances):
        named = _pair_by_name(peripherals, inst)
        from_ll = _pair_by_ll(peripherals, inst, ll)
        for ev in named:
            if (l := from_ll.pop((ev["enable"][2], ev["flag"][2]), None)) is not None:
                ev["provenance"].append("ll")
                ev["ll_clear"] = l["clear_kind"]
        for (en, fl), l in from_ll.items():
            if any(ev["enable"][2] == en and ev["flag"][2] != fl and ev["flag"][2].startswith(fl) for ev in named):
                continue  # a status bit next to the interrupt flag of this enable (USART ISR.CTS next to CTSIF)
            enable = [
                inst,
                l["enable"][0],
                en,
                next(o for _, r, f, o, w in _fields(peripherals, inst) if (r, f) == l["enable"]),
            ]
            flag = [
                inst,
                l["flag"][0],
                fl,
                next(o for _, r, f, o, w in _fields(peripherals, inst) if (r, f) == l["flag"]),
            ]
            clear = None
            if l["clear"] and l["clear"] != l["flag"]:
                clear = [
                    inst,
                    l["clear"][0],
                    l["clear"][1],
                    next(o for _, r, f, o, w in _fields(peripherals, inst) if (r, f) == l["clear"]),
                ]
            named.append(
                {
                    "peripheral": inst,
                    "stem": l["name"],
                    "channel": None,
                    "patched": False,
                    "enable": enable,
                    "flag": flag,
                    "clear": clear,
                    "provenance": ["ll"],
                    "ll_clear": l["clear_kind"],
                }
            )
        shared = collections.Counter(tuple(ev["enable"][1:3]) for ev in named)
        for ev in named:
            key = f"{inst}#{ev['channel']}" if ev["channel"] else inst
            cands = by_source.get(key) or by_source.get(inst, [])
            if len(cands) > 1:
                for prx, srx, sub in SUBVECTOR:
                    if re.fullmatch(prx, inst) and re.search(srx, ev["stem"]):
                        if narrowed := [c for c in cands if re.search(sub, c, re.I)]:
                            cands = narrowed
                        break
            # F1: the alarm also raises the RTC global vector, without the EXTI line (RM0008 18.4.2)
            ev["also"] = (
                ["RTC"]
                if inst == "RTC"
                and ev["stem"].startswith("ALR")
                and {"RTC", "RTC_Alarm"} <= set(by_source.get(inst, []))
                and cands != ["RTC"]
                else []
            )
            if len(cands) > 1:  # TrustZone devices have a secure twin (RCC_S, RTC_S): the non-secure one is the default
                cands = [c for c in cands if not (c.endswith("_S") and c[:-2] in cands)]
            ev["vectors"] = cands
            if rm:
                rm_reg = "C0CR" if inst.startswith("DMAMUX") and ev["enable"][1] == "CCR" else ev["enable"][1]
                access, fields = src.rm_field(rm, inst, rm_reg, ev["enable"][2])
                ev["in_rm"] = bool(access) if fields else None
                # one enable for several flags (BIE: BIF, B2IF, SBIF; TAMPIE: TAMPnF): the flag must exist too
                stem = re.sub(r"IE\d*$", "", ev["enable"][2])
                if shared[tuple(ev["enable"][1:3])] > 1 and ev["flag"][2] not in (stem, stem + "F", stem + "IF"):
                    flag_access, flags = src.rm_field(rm, ev["flag"][0], ev["flag"][1], ev["flag"][2])
                    flag_access = flag_access or (ev["flag"][2] == "NE" and "NF" in flags)
                    # pdf2html can merge the field names of a register (RM0008 USART_SR: TXETCRXNEIDLEORENEFEPE)
                    merged = any(ev["flag"][2] in f and len(f) > len(ev["flag"][2]) + 4 for f in flags)
                    if ev["in_rm"] is not False and flags and not flag_access and not merged:
                        ev["in_rm"] = False
                if ev["in_rm"] is not False and _rm_confirms(ev, rm_events):
                    ev["provenance"].append("rm-table")
                if access:
                    ev["provenance"].append("rm-register")
            else:
                ev["in_rm"] = None
            ev["driver"] = bool(re.match(DRIVER, inst))
            # clear method: a clear register, else the access type of the flag in the reference manual
            ev["flag_access"] = ""
            if ev["clear"] and rm and ev["clear"][1] in (ev["flag"][1], ev["enable"][1]):
                # the clear bit shares the register with rw bits (RCC_CIR): keep them when writing it
                _, fields = src.rm_field(rm, ev["clear"][0], ev["clear"][1], ev["clear"][2])
                ev["clear_keep"] = sum(((1 << w) - 1) << o for f, (o, w, a) in fields.items() if a == "rw")
            if not ev["clear"] and rm:
                access, fields = src.rm_field(rm, ev["flag"][0], ev["flag"][1], ev["flag"][2])
                if not access and any(ev["flag"][2] in f and len(f) > len(ev["flag"][2]) + 4 for f in fields):
                    access = "merged"  # pdf2html joined the rows of several fields (RM0008 USART_SR)
                ev["flag_access"] = access
                if access == "rc_w0":
                    ev["w0_ones"] = sum(((1 << w) - 1) << o for f, (o, w, a) in fields.items() if a == "rc_w0")
                    ev["w0_keep"] = sum(((1 << w) - 1) << o for f, (o, w, a) in fields.items() if a == "rw")
            ev["exti"] = _exti_line(ev, inst, cands, vectors, exti)
            events.append(ev)

    exti_events = []
    for line, info in sorted(exti.items()):
        if line < 16:
            continue
        vec = [n for n, v in vectors.items() if line in v["exti_lines"]]
        if not vec and info["nvic"]:
            word = (re.match(r"[A-Za-z]+\d*", info["source"]) or [""])[0].upper()
            word = {"PVD": "PWR", "AVD": "PWR", "PVM": "PWR"}.get(re.sub(r"\d+$", "", word), word)
            kind = next((k for st, k in RTC_EXTI if re.search(k, info["source"], re.I)), None)
            vec = [n for n, v in vectors.items() if word in v["sources"]]
            if word == "RTC" and kind:
                vec = [n for n in vec if re.search({"wake": "WKUP", "alarm": "ALARM"}.get(kind, "TAMP|STAMP"), n, re.I)]
            if len(vec) > 1 and info["source"]:
                vec = [n for n in vec if info["source"].split()[0].upper() in n]
            vec = vec if len(vec) == 1 else []
        exti_events.append(
            {"line": line, "source": info["source"], "type": info["type"], "nvic": info["nvic"], "vectors": vec}
        )

    graph = {
        "device": device,
        "header": src.header_path(device).name,
        "rm": rm,
        "st_svd": src.st_svd_path(device).name if src.st_svd_path(device) else None,
        "aliases": src.header_aliases(device),
        "vectors": vectors,
        "events": events,
        "exti": exti_events,
    }
    graph["data"] = normalize(graph, peripherals, addr)
    return graph


def _exti_line(ev, inst, cands, vectors, exti):
    per = re.sub(r"[0-9]+$", "", inst)
    kind = next((k for st, k in RTC_EXTI if re.match(st, ev["stem"])), "") if per in ("RTC", "TAMP") else ""
    for line, info in sorted(exti.items()):
        # configurable lines carry the interrupt, direct lines only matter for wakeup
        if line < 16 or info["type"] != "configurable":
            continue
        s = info["source"]
        name_ok = (
            re.search(rf"\b{inst}\b", s.upper())
            or (per == inst and re.search(rf"\b{per}\b", s.upper()))
            or (
                kind
                and re.search(kind, s, re.I)
                and not re.search(r"USB|I2C|U?S?ART|LPTIM|CEC|UCPD|ETH|OTG|MDIO|SPI|COMP|PVD|PVM", s.upper())
            )
            or (inst == "RCC" and ev["stem"] == "LSECSS" and re.search(r"CSS.?LSE|LSE.?CSS", s.upper()))
        )
        if not name_ok or (inst == "RCC" and ev["stem"] != "LSECSS"):
            continue
        if kind and not (re.search(kind, s, re.I) or s.upper().strip() in ("RTC", "TAMP")):
            continue
        # H7 lists the signals per vector: a peripheral signal (comp_gbl_it, lsecss_rcc_it) reaches the NVIC directly
        sig = " ".join(vectors[c]["signals"] for c in cands).lower()
        direct = per.lower() if per != "RCC" else "lsecss"
        if re.search(rf"\b{direct}\w*_it\b", sig):
            return None
        return [line, info["type"]]
    return None


def normalize(graph: dict, peripherals: dict, addr) -> dict:
    """The data a HAL needs: per event the vector, enable, flag, clear and EXTI registers with masks"""
    exti_regs = {
        r["name"]: peripherals["EXTI"]["base"] + r["offset"] for r in peripherals.get("EXTI", {}).get("registers", [])
    }

    def exti_reg(kinds, line):
        n = line // 32 + 1
        for k in kinds:
            for name in (f"{k}{n}", f"C1{k}{n}", k if n == 1 else None):
                if name in exti_regs:
                    return exti_regs[name]
        raise KeyError(f"no EXTI {kinds} register for line {line}")

    def exti(line, kind):
        x = {
            "line": line,
            "kind": "rising" if kind == "configurable" else "direct",
            "imr": exti_reg(["IMR"], line),
            "mask": 1 << (line % 32),
        }
        if x["kind"] == "rising":
            x["rtsr"], x["pr"] = exti_reg(["RTSR"], line), exti_reg(["PR", "RPR"], line)
        return x

    events, names = [], set()
    for e in graph["events"]:
        if e["in_rm"] is False or len(e["vectors"]) != 1:
            continue
        name = re.sub(
            r"\W", "_", f"{e['peripheral']}_{'CH' + e['channel'] + '_' if e['channel'] else ''}{e['stem']}".upper()
        )
        if name in names:
            continue
        names.add(name)
        ev = {
            "name": name,
            "vector": e["vectors"][0],
            "driver": e["driver"],
            "provenance": e["provenance"],
            "enable": [addr(*e["enable"][:2]), 1 << e["enable"][3]],
            "flag": [addr(*e["flag"][:2]), 1 << e["flag"][3]],
        }
        if e["clear"]:
            ev["clear"] = ["write", addr(*e["clear"][:2]), 1 << e["clear"][3], e.get("clear_keep", 0)]
        elif e["flag_access"] == "rc_w0":
            ev["clear"] = ["write0", ev["flag"][0], e["w0_ones"] & ~ev["flag"][1], e["w0_keep"]]
        elif e["flag_access"] == "rc_w1":
            ev["clear"] = ["write", ev["flag"][0], ev["flag"][1], 0]
        else:
            ev["clear"] = ["none", 0, 0, 0]
        if e["exti"]:
            try:
                ev["exti"] = exti(*e["exti"])
            except KeyError:
                pass
        events.append(ev)
    flagged = {ev["exti"]["line"] for ev in events if "exti" in ev}
    for x in graph["exti"]:
        if x["type"] != "configurable" or x["line"] in flagged or len(x["vectors"]) != 1 or not x["nvic"]:
            continue
        try:
            xe = exti(x["line"], "configurable")
        except KeyError:  # the manual covers the family, this device lacks the line
            continue
        events.append(
            {
                "name": f"EXTI{x['line']}",
                "vector": x["vectors"][0],
                "driver": False,
                "source": x["source"],
                "provenance": ["rm-exti"],
                "enable": [xe["imr"], xe["mask"]],
                "flag": [xe["pr"], xe["mask"]],
                "clear": ["write", xe["pr"], xe["mask"], 0],
                "exti": {**xe, "kind": "rising_only"},
            }
        )
    return {"vectors": {n: v["irqn"] for n, v in graph["vectors"].items()}, "events": events}
