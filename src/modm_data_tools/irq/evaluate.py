# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""
Builds the interrupt event graph and the interconnect muxes for many devices and evaluates how consistent the
sources are with each other and across devices.

    python -m modm_data_tools.irq --devices stm32g0 stm32h7 --out ext/cache/irq --jobs 8
    python -m modm_data_tools.irq --devices all --out ext/cache/irq
"""

import argparse
import collections
import json
import multiprocessing
import re
import time
import traceback
from pathlib import Path

from ..lookup import headers


def representatives(prefixes: list[str]) -> list[str]:
    """One part per CMSIS header (the data only depends on the header and the reference manual)"""
    out = []
    for header, parts in sorted(headers._headers().items()):
        parts = sorted(p for p in parts if "@" not in p)
        if not parts:
            continue
        part = parts[0] if parts[0].startswith("stm32") else "stm32" + parts[0]
        if prefixes == ["all"] or any(part.startswith(p) for p in prefixes):
            if not re.match(r"stm32(mp|n6)", part):
                out.append(part)
    return out


def _one(args) -> tuple[str, dict | None, str]:
    device, out = args
    from . import graph, interconnect

    start = time.time()
    try:
        g = graph.build(device)
        try:
            ic = interconnect.build(device)
        except Exception:
            ic = {"sinks": {}}
        g["interconnect"] = ic["sinks"]
        g["seconds"] = round(time.time() - start, 1)
        (out / f"{device}.json").write_text(json.dumps(g, indent=1, default=sorted))
        return device, metrics(g), ""
    except Exception as e:
        return device, None, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"


# ---------------------------------------------------------------------------------------------------------------------
def metrics(g: dict) -> dict:
    """Per device: coverage and the disagreements between the sources"""
    ev = g["events"]
    m = {
        "rm": g["rm"],
        "vectors": len(g["vectors"]),
        "events": len(ev),
        "generated": len(g["data"]["events"]),
        "aliases": g["aliases"],
        "seconds": g.get("seconds"),
    }
    # vector -> instances: compare the sources that know the vector at all
    disagree, shared, compared = [], 0, 0
    for name, v in g["vectors"].items():
        insts = {s: set(p) for s, p in v["sources"].items() if not s.startswith(("DMA", "BDMA")) or "#" not in s}
        by_source = collections.defaultdict(set)
        for inst, tags in insts.items():
            for t in tags:
                by_source[t].add(inst)
        if len(insts) > 1:
            shared += 1
        known = {t: s for t, s in by_source.items() if s}
        if len(known) < 2:
            continue
        compared += 1
        union = set().union(*known.values())
        if any(s != union for s in known.values()):
            disagree.append({"vector": name, **{t: sorted(s) for t, s in known.items()}})
    m.update({"shared_vectors": shared, "compared_vectors": compared, "vector_disagreements": disagree})
    # event provenance
    prov = collections.Counter()
    for e in ev:
        p = set(e["provenance"])
        prov["name"] += "name" in p
        prov["ll"] += "ll" in p
        prov["name+ll"] += {"name", "ll"} <= p
        prov["ll_only"] += p & {"name", "ll"} == {"ll"}
        prov["name_only"] += p & {"name", "ll"} == {"name"}
        prov["rm-table"] += "rm-table" in p
        prov["rm-register"] += "rm-register" in p
        prov["not_in_rm"] += e["in_rm"] is False
        prov["no_vector"] += not e["vectors"]
        prov["ambiguous"] += len(e["vectors"]) > 1
        prov["ambiguous_system"] += len(e["vectors"]) > 1 and not e["driver"]
        prov["patched"] += e["patched"]
        prov["exti"] += bool(e["exti"])
    m["provenance"] = dict(prov)
    # clear method: ours (clear register or RM access type) against the LL ClearFlag function
    conflicts, rmw = [], []
    for e in ev:
        ll = e.get("ll_clear")
        if not ll or e["in_rm"] is False:  # bits of the shared TypeDef that the peripheral does not have
            continue
        ours = (
            "write1"
            if e["clear"] or e["flag_access"] == "rc_w1"
            else "write0"
            if e["flag_access"] == "rc_w0"
            else "read"
            if e["flag_access"] in ("r", "rc_r")
            else e["flag_access"] or "?"
        )
        if ll == "rmw0":
            rmw.append(f"{e['peripheral']}.{e['flag'][2]}")
        if ll != ours and not (ll == "rmw0" and ours == "write0"):
            conflicts.append({"event": f"{e['peripheral']}.{e['flag'][1]}.{e['flag'][2]}", "ours": ours, "ll": ll})
    m.update({"clear_conflicts": conflicts, "ll_rmw_clears": rmw})
    # EXTI line numbers of the vector table text against the CubeMX comments
    exti_diff = []
    for name, v in g["vectors"].items():
        rm = {
            int(x)
            for t in re.findall(r"EXTI\b([^.;)]*)", v["description"])
            for x in re.findall(r"\d+", t)
            if 15 < int(x) < 128
        }
        mx = {
            int(x)
            for t in re.findall(r"EXTI\b([^.;)]*)", v["cubemx"])
            for x in re.findall(r"\d+", t)
            if 15 < int(x) < 128
        }
        if rm and mx and rm != mx:
            exti_diff.append({"vector": name, "rm": sorted(rm), "cubemx": sorted(mx)})
    m["exti_disagreements"] = exti_diff
    m["event_stems"] = {}
    for e in ev:
        m["event_stems"].setdefault(e["peripheral"], []).append(e["stem"])
    m["interconnect"] = {k: len(v["values"]) for k, v in g.get("interconnect", {}).items()}
    return m


def report(results: dict[str, dict], errors: dict[str, str]) -> str:
    lines = ["# Interrupt event graph: consistency of the sources", ""]
    fam = collections.defaultdict(list)
    for d, m in results.items():
        fam[d[5:7]].append((d, m))
    tot = collections.Counter()
    lines += [
        "| Family | Devices | Vectors | Shared | Events | name | ll | name+ll | ll only | name only | RM table | "
        "Not in RM | No vector | Ambiguous (system) | Vector disagreements | Clear conflicts | EXTI diffs | Generated |",
        "|" + "---|" * 19,
    ]
    for f, items in sorted(fam.items()):
        c = collections.Counter()
        for d, m in items:
            p = m["provenance"]
            c.update(
                {
                    "vectors": m["vectors"],
                    "shared": m["shared_vectors"],
                    "events": m["events"],
                    "generated": m["generated"],
                    "vdis": len(m["vector_disagreements"]),
                    "compared": m["compared_vectors"],
                    "clear": len(m["clear_conflicts"]),
                    "exti_diff": len(m["exti_disagreements"]),
                    **p,
                }
            )
        tot.update(c)
        tot["devices"] += len(items)
        lines.append(
            f"| {f.upper()} | {len(items)} | {c['vectors']} | {c['shared']} | {c['events']} | {c['name']} | "
            f"{c['ll']} | {c['name+ll']} | {c['ll_only']} | {c['name_only']} | {c['rm-table']} | "
            f"{c['not_in_rm']} | {c['no_vector']} | {c['ambiguous']} ({c['ambiguous_system']}) | "
            f"{c['vdis']}/{c['compared']} | {c['clear']} | {c['exti_diff']} | {c['generated']} |"
        )
    lines.append(
        f"| **All** | {tot['devices']} | {tot['vectors']} | {tot['shared']} | {tot['events']} | {tot['name']} | "
        f"{tot['ll']} | {tot['name+ll']} | {tot['ll_only']} | {tot['name_only']} | {tot['rm-table']} | "
        f"{tot['not_in_rm']} | {tot['no_vector']} | {tot['ambiguous']} ({tot['ambiguous_system']}) | "
        f"{tot['vdis']}/{tot['compared']} | {tot['clear']} | {tot['exti_diff']} | {tot['generated']} |"
    )

    # which source disagrees how often
    kinds = collections.Counter()
    examples = collections.defaultdict(list)
    for d, m in results.items():
        for dis in m["vector_disagreements"]:
            srcs = {k: set(v) for k, v in dis.items() if k != "vector"}
            union = set().union(*srcs.values())
            for t, s in srcs.items():
                if missing := union - s:
                    kinds[f"{t} misses"] += 1
                    if len(examples[f"{t} misses"]) < 12:
                        examples[f"{t} misses"].append(f"{d} {dis['vector']}: {', '.join(sorted(missing))}")
    lines += ["", "## Vector sources: which source misses instances the others list", ""]
    for k, n in kinds.most_common():
        lines.append(f"- **{k}** {n} times, e.g.: " + "; ".join(examples[k][:6]))

    clear = collections.Counter()
    cex = collections.defaultdict(list)
    for d, m in results.items():
        for c in m["clear_conflicts"]:
            key = f"ours {c['ours']} vs LL {c['ll']}"
            clear[key] += 1
            if len(cex[key]) < 6:
                cex[key].append(f"{d} {c['event']}")
    lines += ["", "## Clear method: ours (clear register, RM access type) against the LL ClearFlag function", ""]
    for k, n in clear.most_common():
        lines.append(f"- **{k}**: {n}, e.g. " + "; ".join(cex[k]))
    rmw = collections.Counter(x for m in results.values() for x in m["ll_rmw_clears"])
    lines += [
        "",
        f"LL ClearFlag functions that clear an rc_w0 flag with CLEAR_BIT (read-modify-write, can clear other "
        f"flags set in between): {sum(rmw.values())} on {len(rmw)} distinct flags, e.g. "
        + ", ".join(k for k, _ in rmw.most_common(10)),
    ]

    exti = [
        f"{d} {x['vector']}: RM {x['rm']} vs CubeMX {x['cubemx']}"
        for d, m in results.items()
        for x in m["exti_disagreements"]
    ]
    lines += ["", f"## EXTI line numbers in the vector table vs CubeMX: {len(exti)} differences", ""] + [
        f"- {x}" for x in exti[:30]
    ]

    # cross-device: the same instance under the same reference manual should have the same events
    by_rm = collections.defaultdict(list)
    for d, m in results.items():
        if m["rm"]:
            by_rm[m["rm"]].append((d, m))
    unstable = []
    for rm, items in sorted(by_rm.items()):
        per = collections.defaultdict(dict)
        for d, m in items:
            for inst, stems in m["event_stems"].items():
                per[inst][d] = frozenset(stems)
        for inst, variants in per.items():
            if len(set(variants.values())) > 1 and not re.match(r"(TIM|USART|UART|SPI|I2C|ADC|OTG|USB)", inst):
                groups = collections.defaultdict(list)
                for d, s in variants.items():
                    groups[s].append(d)
                biggest = max(groups, key=lambda s: len(groups[s]))
                diffs = [
                    f"{', '.join(ds[:3])}{'…' if len(ds) > 3 else ''}: {'+' + ','.join(sorted(s - biggest)) if s - biggest else ''}"
                    f"{' -' + ','.join(sorted(biggest - s)) if biggest - s else ''}"
                    for s, ds in groups.items()
                    if s != biggest
                ]
                unstable.append(f"{rm} {inst} ({len(variants)} devices): " + "; ".join(diffs))
    lines += [
        "",
        f"## Same reference manual, same instance, different events: {len(unstable)} (system peripherals)",
        "",
        "Expected where the devices really differ (more PLLs, HSI48), suspicious where they share the die.",
        "",
    ]
    lines += [f"- {u}" for u in unstable[:60]]
    if len(unstable) > 60:
        lines.append(f"- … {len(unstable) - 60} more")

    al = {d: m["aliases"] for d, m in results.items() if m["aliases"]}
    lines += ["", f"## CMSIS handler aliases that shadow a vector: {len(al)} devices", ""] + [
        f"- {d}: {a}" for d, a in al.items()
    ]
    lines += ["", f"## Errors: {len(errors)}", ""] + [f"- {d}: {e.splitlines()[0]}" for d, e in errors.items()]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m modm_data_tools.irq")
    parser.add_argument("--devices", nargs="+", default=["all"])
    parser.add_argument("--out", type=Path, default=Path("ext/cache/irq"))
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    devices = representatives(args.devices)
    print(f"{len(devices)} devices")
    results, errors = {}, {}
    with multiprocessing.Pool(args.jobs) as pool:
        for i, (d, m, err) in enumerate(pool.imap_unordered(_one, [(d, args.out) for d in devices]), 1):
            if m is None:
                errors[d] = err
                print(f"[{i}/{len(devices)}] {d}: {err.splitlines()[0]}", flush=True)
            else:
                results[d] = m
                print(
                    f"[{i}/{len(devices)}] {d}: {m['events']} events, {len(m['vector_disagreements'])} vector "
                    f"disagreements, {m['seconds']} s",
                    flush=True,
                )
    results = dict(sorted(results.items()))
    (args.out / "metrics.json").write_text(json.dumps({"results": results, "errors": errors}, indent=1))
    (args.out / "report.md").write_text(report(results, errors))
    print(f"report: {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
