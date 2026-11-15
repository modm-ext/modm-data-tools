# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""
The independent sources of the STM32 interrupt event graph, each normalized to plain dicts:

- CMSIS header: vectors (name -> IRQn) and handler aliases.
- Header SVD (header2svd): register fields, to pair interrupt enable, flag and clear bits by name.
- Reference manual: vector/NVIC table, EXTI line table, interrupt event tables, register access types.
- ST SVD from the CMSIS pack: the interrupts listed per peripheral.
- CubeMX NVIC modes: the peripheral instances per vector, with part conditions.
- CubeHAL LL drivers: EnableIT_x / IsActiveFlag_x / ClearFlag_x functions with their register and field.
"""

import re
from functools import cache
from pathlib import Path

from lxml import etree

from ..lookup import docs, headers, svd
from modm_data.utils import ext_path

_CUBEMX = ext_path("stmicro/cubemx/mcu")
_CUBEHAL = ext_path("stmicro/cubehal")
_STMICRO = ext_path("stmicro")


def clean(text: str) -> str:
    text = re.sub(r"\[\^\d+\]|\(\d\)", "", text or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text).replace(" ", " ")).strip()


# ---------------------------------------------------------------------------------------------------------------------
@cache
def header_path(device: str) -> Path:
    found = headers._select(headers.normalize(device))
    if len(found) != 1:
        raise ValueError(f"'{device}' must select exactly one device header, it selects {len(found)}")
    return next(iter(found))


@cache
def header_vectors(device: str) -> dict[str, int]:
    """vector name -> IRQn (positions >= 0)"""
    text = header_path(device).read_text(errors="replace")
    return {n: int(v) for n, v in re.findall(r"^\s*(\w+)_IRQn\s*=\s*(\d+)", text, re.M)}


@cache
def header_aliases(device: str) -> dict[str, str]:
    """handler aliases whose name is also a vector of the device: they rename MODM_ISR(name)"""
    text = header_path(device).read_text(errors="replace")
    vectors = header_vectors(device)
    return {a: b for a, b in re.findall(r"^#define\s+(\w+)_IRQHandler\s+(\w+)_IRQHandler", text, re.M) if a in vectors}


@cache
def header_svd(device: str) -> dict[str, dict]:
    return svd._device(device)[1]


def rm_of(device: str) -> str:
    return next(l.split()[0] for l in docs.device_docs(device, "RM").splitlines() if l.startswith("RM"))


# ---------------------------------------------------------------------------------------------------------------------
# Reference manual
@cache
def _rm_tables(doc: str) -> list[tuple]:
    return (
        docs._db()
        .execute(
            "SELECT t.number, t.caption, t.section, f.header FROM tables t JOIN table_fts f ON f.rowid=t.id "
            "WHERE t.doc=? AND t.kind='table' ORDER BY t.id",
            (doc,),
        )
        .fetchall()
    )


def rm_tables(doc: str, where) -> list[tuple[str, str, str]]:
    """tables whose (caption, header) satisfy where(caption, header), both lower case"""
    return [(n, c, s) for n, c, s, h in _rm_tables(doc) if where((c or "").lower(), (h or "").lower())]


@cache
def rm_table(doc: str, number: str) -> tuple[list[str], list[list[str]]]:
    md = docs.doc_table(doc, number, limit=1000)
    lines = [l for l in md.splitlines() if l.startswith("|")]
    rows = [[c.strip() for c in r.strip("|").split("|")] for r in lines]
    return rows[0], rows[2:]


@cache
def rm_vector_tables(doc: str) -> list[dict[int, dict]]:
    """Every vector table of the manual (F1: connectivity line, XL-density, others): IRQn -> acronym, description,
    signals (H7 lists one row per signal)"""
    found = [
        t
        for t in rm_tables(doc, lambda c, h: "vector table" in c or "nvic" in c)
        if re.search(r"vector table|NVIC", t[1], re.I) and "initialization" not in t[1].lower()
    ]
    tables = []
    for number, _, _ in found:
        header, rows = rm_table(doc, number)
        try:
            pos = next(i for i, h in enumerate(header) if "position" in h.lower())
            acr = next(i for i, h in enumerate(header) if "acronym" in h.lower())
            des = next(i for i, h in enumerate(header) if "description" in h.lower())
        except StopIteration:
            continue
        sig = next((i for i, h in enumerate(header) if "signal" in h.lower()), None)
        vectors = {}
        for r in rows:
            if len(r) > max(pos, acr, des) and r[pos].isdigit():
                v = vectors.setdefault(int(r[pos]), {"acronym": clean(r[acr]), "description": "", "signals": ""})
                v["description"] = (v["description"] + " " + clean(r[des])).strip()
                if sig is not None:
                    v["signals"] = (v["signals"] + " " + clean(r[sig])).strip()
        if vectors:
            tables.append(vectors)
    return tables


def rm_vectors(doc: str, names: dict[str, int] | None = None) -> dict[int, dict]:
    """The vector table that matches the header vector names best"""
    tables = rm_vector_tables(doc)
    if not tables:
        return {}
    if not names:
        return tables[0]

    def score(t):
        return sum(
            1
            for n, i in names.items()
            if i in t and set(re.split(r"[_ /]+", n.upper())) & set(re.split(r"[_ /]+", t[i]["acronym"].upper()))
        )

    return max(tables, key=score)


@cache
def rm_exti(doc: str) -> dict[int, dict]:
    """EXTI line -> source, type (direct/configurable, empty if the manual does not say), nvic connection"""
    lines = {}
    for number, _, _ in rm_tables(
        doc, lambda c, h: "exti" in c and ("source" in h or "connection" in c or "mapping" in c)
    ):
        header, rows = rm_table(doc, number)
        if not any("source" in h.lower() for h in header):
            continue
        src = next(i for i, h in enumerate(header) if "source" in h.lower())
        kind = next((i for i, h in enumerate(header) if "type" in h.lower()), None)
        nvic = next((i for i, h in enumerate(header) if "nvic" in h.lower()), None)
        for r in rows:
            m = re.fullmatch(r"(?:EXTI\s*)?(\d+)", clean(r[0]))
            if not m or len(r) <= src:
                continue
            lines[int(m[1])] = {
                "source": clean(r[src]),
                "type": clean(r[kind]).lower() if kind is not None and kind < len(r) else "",
                "nvic": not clean(r[nvic]).startswith("No") if nvic is not None and nvic < len(r) else True,
            }
        if lines:
            break
    if not lines:
        # older manuals list the lines in the text: "EXTI line 16 is connected to the PVD output", all configurable
        db = docs._db()
        for number, title in db.execute(
            "SELECT number, title FROM section WHERE doc=? AND title LIKE ?", (doc, "%nterrupt/event line mapping%")
        ).fetchall():
            text = clean(docs.doc_section(doc, number, max_chars=50000))
            for line, source in re.findall(
                r"EXTI line (\d+) is connected to (?:the )?(.+?)(?= EXTI line \d+ is connected|\. |$)", text
            ):
                lines.setdefault(int(line), {"source": source.strip(" ,;-"), "type": "", "nvic": True})
    return lines


@cache
def rm_event_tables(doc: str) -> dict[str, list[dict]]:
    """chapter title -> rows of the interrupt event tables (flag, enable, clear cells)"""
    chapters = dict(docs._db().execute("SELECT number, title FROM section WHERE doc=? AND level=2", (doc,)).fetchall())
    out = {}
    for number, _, section in rm_tables(doc, lambda c, h: "flag" in h and ("enable" in h or "control" in h)):
        try:
            header, rows = rm_table(doc, number)
        except Exception:
            continue
        low = [h.lower() for h in header]
        fi = next((i for i, h in enumerate(low) if "flag" in h and "clear" not in h), None)
        ei = next((i for i, h in enumerate(low) if "enable" in h or "control bit" in h), None)
        if fi is None or ei is None:
            continue
        ci = next((i for i, h in enumerate(low) if "clear" in h), None)
        title = chapters.get((section or "").split(".")[0], "?")
        for r in rows:
            if len(r) > max(fi, ei):
                out.setdefault(title, []).append(
                    {
                        "flag": clean(r[fi]),
                        "enable": clean(r[ei]),
                        "clear": clean(r[ci]) if ci is not None and ci < len(r) else "",
                    }
                )
    return out


@cache
def rm_register(doc: str, register: str) -> dict[str, tuple[int, int, str]]:
    """field -> (offset, width, access) of a register in the reference manual"""
    try:
        text = docs.doc_register(register, doc=doc)
    except Exception:
        return {}
    fields = {}
    for bits, name, access in re.findall(r"^\| (\d+(?::\d+)?) \| (\w+) \| ([\w_]*) \|", text, re.M):
        hi, lo = (bits.split(":") + [bits])[:2]
        fields.setdefault(name, (int(lo), int(hi) - int(lo) + 1, access))
    return fields


def rm_field(doc: str, instance: str, register: str, field: str) -> tuple[str, dict]:
    """access of a field and all fields of the register ({} if the manual has no such register)"""
    # the most specific name only: TIMx_DIER may be the chapter of another timer type
    for name in (
        f"{instance}_{register}",
        re.sub(r"^([A-Z]+)\d+$", r"\1", instance) + f"_{register}",
        re.sub(r"\d+$", "x", instance) + f"_{register}",
    ):
        if fields := rm_register(doc, name):
            # CMSIS joins the FIFO and non-FIFO names (RXNEIE_RXFNEIE), the manual lists both separately
            part = next((f for f in [field] + field.split("_") if f in fields), None)
            return (fields[part][2] if part else ""), fields
    return "", {}


# ---------------------------------------------------------------------------------------------------------------------
# ST SVD from the CMSIS pack
@cache
def st_svd_path(device: str) -> Path | None:
    from modm_data.cubemx.device_data import _dfp_node, _dfp_findall, _dfp_folder
    from modm_data.kg.stmicro import did_from_string

    part = headers.normalize(device)
    did = did_from_string((f"stm32{part}" + "t6")[:13])  # the identifier wants a package and temperature
    node = _dfp_node(f"STM32{part.upper()}", did)
    if not node:
        return None
    rel = _dfp_findall(node[0], "debug", attribs=["svd"]).get("svd")
    return _STMICRO / _dfp_folder(did) / rel if rel else None


@cache
def st_svd_interrupts(device: str) -> dict[str, set[str]]:
    """vector name -> peripherals that list it in the ST SVD"""
    path = st_svd_path(device)
    if not path or not path.exists():
        return {}
    out = {}
    root = etree.parse(str(path)).getroot()
    for p in root.iter("peripheral"):
        name = p.findtext("name")
        for irq in p.findall("interrupt"):
            out.setdefault(irq.findtext("name"), set()).add(name)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# CubeMX NVIC modes
@cache
def _cubemx_mcu(device: str) -> tuple[str, etree._Element] | None:
    from modm_data.cubemx.device_data import _family_file

    part = "STM32" + headers.normalize(device).upper()
    mcus = _family_file().query(f'//Family/SubFamily/Mcu[starts-with(@RefName,"{part}")]')
    if not mcus:
        return None
    refname = sorted(m.get("RefName") for m in mcus)[0]
    name = sorted(m.get("Name") for m in mcus)[0]
    return refname, etree.parse(str(_CUBEMX / f"{name}.xml")).getroot()


def _condition(expr: str, refname: str, instances: set[str]) -> bool:
    """Evaluates the CubeMX conditions that depend on the part: names, X_Exist, the rest is True"""
    if not expr:
        return True

    def token(m):
        t = m[0]
        if t.startswith("STM32"):
            return str(
                re.fullmatch(t.replace("x", ".").replace("(", "").replace(")", ""), refname) is not None
                or refname.startswith(t.rstrip("x"))
            )
        if t.endswith("_Exist"):
            return str(t[:-6] in instances)
        return "True"

    py = re.sub(r"STM32[\w()-]+|\w+", token, expr).replace("&", " and ").replace("|", " or ").replace("!", " not ")
    try:
        return bool(eval(py, {}, {}))
    except Exception:
        return True


@cache
def cubemx_vectors(device: str) -> dict[str, dict]:
    """vector name -> instances (CubeMX), comment"""
    found = _cubemx_mcu(device)
    if not found:
        return {}
    refname, mcu = found
    ns = {"m": mcu.nsmap.get(None, "")}
    ips = mcu.findall("m:IP", ns) if ns["m"] else mcu.findall("IP")
    instances = {ip.get("InstanceName") for ip in ips}
    nvic = next((ip for ip in ips if re.fullmatch(r"NVIC\d?", ip.get("Name", ""))), None)
    if nvic is None:
        return {}
    path = _CUBEMX / "IP" / f"{nvic.get('Name')}-{nvic.get('Version')}_Modes.xml"
    if not path.exists():
        path = _CUBEMX / "IP" / f"NVIC-{nvic.get('Version')}_Modes.xml"
    if not path.exists():
        return {}
    out = {}
    for pv in etree.parse(str(path)).getroot().iter("{*}PossibleValue"):
        value = pv.get("Value", "")
        if "_IRQn:" not in value or not _condition(pv.get("Condition", ""), refname, instances):
            continue
        parts = value.split(":")
        vector = parts[0][:-5]
        insts = [i for i in (parts[2].split(",") if len(parts) > 2 else []) if i and i != "NONE"]
        out[vector] = {"instances": insts, "comment": pv.get("Comment", "")}
    return out


# ---------------------------------------------------------------------------------------------------------------------
# CubeHAL LL drivers
def _cubehal_folder(device: str) -> Path | None:
    part = headers.normalize(device)
    for name in (f"stm32{part[:4]}xx", f"stm32{part[:3]}xx", f"stm32{part[:2]}xx", f"stm32{part[:3]}x"):
        if (_CUBEHAL / name / "Inc").exists():
            return _CUBEHAL / name / "Inc"
    return None


_LL_FUNC = re.compile(
    r"@rmtoll\s+(\w+)\s+(\w+)\s+LL_(\w+?)_(EnableIT|IsActiveFlag|ClearFlag)_(\w+).*?"
    r"__STATIC_INLINE\s+\w+\s+LL_\3_\4_\5\s*\([^)]*\)\s*\{(.*?)\n\}",
    re.S,
)


@cache
def ll_events(folder: Path) -> dict[tuple[str, str], dict]:
    """(LL peripheral, event name) -> enable/flag/clear (register, field) and the clear statement"""
    out = {}
    for path in sorted(folder.glob("*_ll_*.h")):
        text = path.read_text(errors="replace")
        for reg, field, per, kind, name, body in _LL_FUNC.findall(text):
            reg = re.sub(r"^\w+?_(?=[A-Z]+\d*$)", "", reg)  # RTC_SCR -> SCR
            entry = out.setdefault((per, name), {})
            entry[kind] = (reg, field)
            if kind == "ClearFlag":
                if not re.search(r"WRITE_REG|SET_BIT|CLEAR_BIT|MODIFY_REG|->\w+\s*=", body):
                    entry["clear_kind"] = "read"  # read sequence (F1 USART: SR then DR)
                elif "~" in body:
                    entry["clear_kind"] = "write0"
                elif "CLEAR_BIT" in body:
                    entry["clear_kind"] = "rmw0"
                else:
                    entry["clear_kind"] = "write1"
    return out


def ll_device_events(device: str) -> dict[tuple[str, str], dict]:
    folder = _cubehal_folder(device)
    return ll_events(folder) if folder else {}
