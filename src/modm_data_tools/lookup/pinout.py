# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""The connector pinouts of the NUCLEO boards and Discovery kits (modm_data.html2pinout) and the alternate functions
of the device pins (devices.db)"""

import json
import re
from functools import cache
from pathlib import Path

from ..errors import UserError, UserRegex
from .docs import ARCHIVE, CACHE, write_atomic
from .headers import MODM_DATA, _devices, _short, normalize

PINOUT = MODM_DATA / "ext/stmicro/pinout/nucleo.json"
# the extraction of a single user manual, the version changes when html2pinout extracts something else
EXTRACTED = CACHE / "pinout-v3"


def _key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def _version(path: Path) -> tuple[int, ...]:
    return tuple(int(v) for v in path.name.split("-v")[1].split("_"))


@cache
def _manuals() -> dict[str, list[Path]]:
    """All versions of every user manual in the archive, latest first"""
    manuals = {}
    for path in ARCHIVE.glob("UM*-v*"):
        manuals.setdefault(path.name.split("-v")[0], []).append(path)
    return {name: sorted(paths, key=_version, reverse=True) for name, paths in sorted(manuals.items())}


@cache
def _index() -> dict[str, list[Path]]:
    """The boards that the latest version of every user manual describes, without extracting them"""
    from modm_data.html.document import Document
    from modm_data.html2pinout import _document_boards
    from modm_data.html2pinout.signals import has_board_file

    index = {}
    for paths in _manuals().values():
        chapters = sorted(Document(paths[0].absolute()).chapters(), key=lambda c: c.number)
        for board in _document_boards(chapters):
            index.setdefault(board, []).append(paths[0])
    # Neither placeholders like STM32XXYYZ-DK nor family names like NUCLEO-WB55 next to NUCLEO-WB55RG, which
    # html2pinout also leaves out, but the Discovery kits without CubeMX board file stay
    return {
        board: paths
        for board, paths in index.items()
        if not re.search("XX|YY", board)
        and (has_board_file(board) or not any(o != board and o.startswith(board) for o in index))
    }


def _extract(path: Path) -> dict[str, dict]:
    """The pinouts and solder bridges of the boards of one user manual, like html2pinout, cached as JSON"""
    cache = EXTRACTED / f"{path.name}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from modm_data.html.document import Document
    from modm_data.html2pinout import (
        board_bridges_from_user_manual,
        board_pinouts_from_user_manual,
        inherit_bridge_defaults,
    )
    from modm_data.html2pinout.signals import board_uses, device_for_board

    document = Document(path.absolute())
    bridges = board_bridges_from_user_manual(document)
    # older revisions of the manual may still know the defaults that this one lost
    for old in _manuals()[document.name][1:]:
        if not any(not any(o["default"] for o in b["states"]) for bs in bridges.values() for b in bs):
            break
        inherit_bridge_defaults(bridges, board_bridges_from_user_manual(Document(old.absolute())), old.name)
    boards = {}
    for board, connectors in board_pinouts_from_user_manual(document).items():
        gpios = {p["signal"] for c in connectors.values() for p in c["pins"].values() if p["kind"] == "gpio"}
        device = device_for_board(board)
        boards[board] = {
            "board": board,
            "document": document.fullname,
            "connectors": connectors,
            "device": device[0] if device else None,
            "bridges": [
                dict(b, gpios=[g for g in b["gpios"] if g in gpios])
                for b in bridges.get(board, [])
                if set(b["gpios"]) & gpios
            ],
            "uses": {g: u for g, u in board_uses(board).items() if g in gpios},
        }
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    # through JSON, so that the pin numbers are strings like in nucleo.json
    text = json.dumps(boards, separators=(",", ":"))
    write_atomic(cache, text)
    return json.loads(text)


@cache
def _pinouts(mtime: float) -> dict[str, dict]:
    return {b["board"]: b for b in json.loads(PINOUT.read_text())["boards"]}


def _match(name: str, boards) -> list[str]:
    """The boards that a name in any spelling means: exactly, without NUCLEO- or by all its words"""
    # the marketing name 32F429IDISCOVERY is the STM32F429I-DISCO, like in html2pinout
    name = re.sub(r"\b32([A-Z]\d\w+?)DISCOVERY\b", r"STM32\1-DISCO", name.strip().upper())
    key, words = _key(name), re.findall(r"[A-Z0-9]+", name)
    return (
        [b for b in boards if b == name]
        or [b for b in boards if _key(b) in (key, "NUCLEO" + key)]
        or [b for b in boards if all(w in _key(b) for w in words)]
    )


def _board(name: str) -> dict:
    """A board by its name in any spelling, e.g. 'nucleo-u5a5zj-q', 'u5a5zj' or 'disco f429', from nucleo.json if it
    was built, otherwise (and for the boards that nucleo.json leaves out) extracted from the latest user manual"""
    if not _key(name):
        raise UserError("no board given, e.g. 'NUCLEO-G474RE'")
    if PINOUT.exists() and len(found := _match(name, built := _pinouts(PINOUT.stat().st_mtime))) == 1:
        return built[found[0]]
    index = _index()
    if len(found := _match(name, index)) != 1:
        import difflib

        close = found or difflib.get_close_matches(name.upper(), list(index), n=8, cutoff=0.6)
        raise UserError(
            f"{len(found) or 'no'} boards match '{name}'" + (f": {' '.join(sorted(close))}" if close else "")
        )
    # Several manuals may name a board: like html2pinout, a NUCLEO board or else the manual of the fewest boards,
    # then the one that describes the most of it
    extracted = [(e[found[0]], len(e)) for path in index[found[0]] if found[0] in (e := _extract(path))]
    if not extracted:
        raise UserError(f"{', '.join(p.name for p in index[found[0]])} names {found[0]}, but has no pinout of it")
    return max(
        extracted,
        key=lambda e: (
            e[0]["board"].startswith("NUCLEO") or -e[1],
            sum(len(c["pins"]) for c in e[0]["connectors"].values()),
        ),
    )[0]


def _words(query: str, text: str) -> bool:
    """Every word of the query is in the text, but not inside another name: 'A7' is not in 'PA7' or 'A70'"""
    return all(re.search(rf"(?<![A-Z0-9]){re.escape(word)}(?![A-Z0-9])", text, re.I) for word in query.split())


def board_pinout(board: str, connector: str = "", query: str = "") -> str:
    """The connector pins of a NUCLEO board or Discovery kit from its user manual, with the solder bridges.
    For example 'nucleo-u5a5zj-q' or 'u5a5zj': connector and pin number, label, GPIO, net, function, what the board
    itself uses the GPIO for (ST-LINK VCP, LED, ...) and the solder bridges that change what the pin is connected to,
    with their default state (*).
    `connector` selects connectors by name or type ('CN10', 'morpho', 'arduino'). `query` selects pins by words that
    must all occur in their label, GPIO, net, function, bridges or board use, e.g. 'A6', 'ADC A7', 'D12', 'SB53',
    'LED', 'RMII' or a GPIO like 'PC2' to find all connector pins of that GPIO."""
    data = _board(board)
    bridges = {g: [b for b in data["bridges"] if g in b["gpios"]] for b in data["bridges"] for g in b["gpios"]}
    device = data["device"]
    # CubeMX lacks the newest families (C5, G5, H7P), devices.db has them from their DFP
    if not device and (name := re.fullmatch(r"NUCLEO-(\w+?)(?:-[PQ])?", data["board"])):
        try:
            device = f"STM32{name[1]} (by the board name, {_part(name[1])[0]} in devices.db)"
        except UserError:
            pass
    out = [f"{data['board']}: {data['document']}, device {device or 'unknown'}"]
    shown = []
    for name, c in data["connectors"].items():
        if connector and connector.lower() not in (name.lower(), c["type"]):
            continue
        lines = []
        for number, pin in c["pins"].items():
            signal = pin["signal"]
            # the manuals write - for empty cells, and repeat the signal in several columns
            values = {
                k: "" if pin.get(k, "-") in ("-", signal) else pin[k]
                for k in ("label", "pin", "net", "function", "remark")
            }
            # a pin may carry several GPIOs, chosen by solder bridges ('PA3 / PC5') or wired together on the board
            # (NUCLEO-G564RE 'PB12/PB9'): the bridges and board uses of all of them matter
            gpios = list(
                dict.fromkeys([signal] + re.findall(r"\bP[A-Z]\d{1,2}\b", values["label"] + " " + values["pin"]))
            )
            ids = [i for g in gpios for b in bridges.get(g, []) for i in b["ids"]]
            uses = [(g, data["uses"][g]) for g in gpios if g in data["uses"]]
            fields = [
                values["label"],
                signal,
                values["pin"],
                values["net"],
                values["function"],
                values["remark"],
                " ".join(ids),
            ]
            # what the board uses the pin for counts too: 'button', 'LD2', 'RMII'
            board = " ".join(f"{u['use']} {u.get('label', '')} {u.get('signal', '')}" for _, u in uses)
            if query and not _words(query, " ".join(fields + gpios) + " " + board):
                continue
            label, _, raw, net, function, remark, _ = fields
            text = f"  {name}-{number}" + (f" {label}" if label else "") + f": {signal}"
            text += f" (also {', '.join(gpios[1:])})" if len(gpios) > 1 else ""
            text += "".join(
                f", {k} {v}" for k, v in (("pin", raw), ("net", net), ("function", function), ("remark", remark)) if v
            )
            for g, use in uses:
                # which of the GPIOs the board uses, the VCP on PA3 of 'PA3 / PC5' is not on the pin by default
                text += f", on board: {g + ' ' if len(gpios) > 1 else ''}{use['use']} {use.get('label') or use.get('signal', '')}"
                text = text.rstrip()
            if ids:
                text += f" [{' '.join(dict.fromkeys(ids))}]"
                shown += [b for g in gpios for b in bridges.get(g, []) if b not in shown]
            lines.append(text)
        if lines:
            out.append(f"{name} ({c['type']}{', ' + c['side'] if c.get('side') else ''}):")
            out += lines
    if len(out) == 1:
        raise UserError(
            f"no pin of {data['board']} matches"
            + (f" connector '{connector}'" if connector else "")
            + (f" query '{query}'" if query else "")
            + f", connectors: {', '.join(data['connectors'])}"
        )
    for b in shown:
        states = "; ".join(
            f"{s['state']}{'*' if s['default'] else ''}" + (f": {s['text']}" if s["text"] else "") for s in b["states"]
        )
        out.append(f"{' '.join(b['ids'])} ({b['name'] or b['source']}): {states}")
    return "\n".join(out)


def _part(device: str) -> tuple[str, list[str]]:
    """The first part name in devices.db that matches a device prefix, x is any character like in CubeMX names"""
    name, _, core = normalize(device).partition("@")
    name = "stm32" + name
    pattern = re.compile(re.escape(name).replace("x", "."))
    parts = [
        p
        for (p,) in _devices().execute("SELECT partname FROM device ORDER BY partname")
        if pattern.match(p) and (not core or p.endswith("@" + core))
    ]
    if not parts:
        raise UserError(f"no STM32 device in devices.db matches '{device}'")
    # some names have a real X (STM32N657X0HxQ), so the parts that need the fewest x to be another character win
    wildcards = {p: sum(p[i] != "x" for i, c in enumerate(name) if c == "x") for p in parts}
    parts = [p for p in parts if wildcards[p] == min(wildcards.values())]
    return parts[0], parts


def _af(af: str) -> str:
    return af if af.startswith("remap") else f"AF{af}"


def pin_functions(device: str, query: str = "") -> str:
    """The signals of the GPIOs of a device with their alternate function number (AFn, the GPIOx_AFR value).
    The others are additional functions (ADC inputs, wakeup, ...) without AF. `query` is a pin ('PA2') for its
    signals or a regex on the signal names ('USART2_TX', 'TIM1_CH', 'SPI') for the pins that can carry them, a number
    does not continue into another one ('TIM1' is not TIM15). Without `query` all pins."""
    part, parts = _part(device)
    rows = (
        _devices()
        .execute(
            "SELECT g.port, g.pin, g.name, s.peripheral, s.instance, s.name, s.af FROM device d "
            "JOIN gpio g ON g.set_id = d.gpio_set_id LEFT JOIN signal s ON s.pin_signals_id = g.pin_signals_id "
            "WHERE d.partname = ? ORDER BY length(g.port), g.port, g.pin, s.idx",
            (part,),
        )
        .fetchall()
    )
    # the F1 selects the pins of a peripheral with its AFIO_MAPR remap field instead
    rows += (
        _devices()
        .execute(
            "SELECT r.port, r.pin, '', r.peripheral, r.instance, r.signal, 'remap' || r.grp FROM device d "
            "JOIN remap r ON r.set_id = d.gpio_set_id WHERE d.partname = ? ORDER BY r.idx",
            (part,),
        )
        .fetchall()
    )
    pins = {}
    for port, number, name, peripheral, instance, signal, af in sorted(rows, key=lambda r: (len(r[0]), r[0], r[1])):
        signals = pins.setdefault(name or f"P{port.upper()}{number}", [])
        # pins without any signal (BOOT0, PB2 of the F4) are GPIOs all the same
        if signal is not None:
            signals.append(((f"{peripheral}{instance}_{signal}" if peripheral else signal).upper(), af))
    out = [f"{part}" + (f" (also matches {_short(parts[1:])})" if len(parts) > 1 else "")]
    if query and re.fullmatch(r"P[A-Z]\d{1,2}", query, re.I):
        if query.upper() not in pins:
            # devices.db only has the GPIOs, the package also has pins like the monoio of the WBA
            kind = (
                _devices()
                .execute(
                    "SELECT p.type FROM device d JOIN package_pin p ON p.package_id = d.package_id "
                    "WHERE d.partname = ? AND p.name LIKE ?",
                    (part, f"{query.upper()}%"),
                )
                .fetchone()
            )
            raise UserError(
                f"{part} has no GPIO {query.upper()}"
                + (f", it is a {kind[0]} pin of the package without signals in devices.db" if kind else "")
            )
        pins = {query.upper(): pins[query.upper()]}
    elif query:
        UserRegex(query)  # an invalid regex is reported at its position in the query, not in the pattern below
        regex = UserRegex(r"(?<![A-Z0-9])(?:" + query + r")" + (r"(?!\d)" if query[-1:].isdigit() else ""), re.I)
        found = {}
        for pin, signals in pins.items():
            for signal, af in signals:
                if regex.search(signal):
                    found.setdefault(signal, []).append(f"{pin} {_af(af)}" if af else pin)
        if not found:
            raise UserError(f"no signal of {part} matches '{query}'")
        return "\n".join(out + [f"{s}: {', '.join(p)}" for s, p in sorted(found.items())])
    for pin, signals in pins.items():
        afs = [f"{_af(af)} {s}" for s, af in sorted(signals, key=lambda x: (len(x[1]), x[1])) if af]
        extra = [s for s, af in signals if not af]
        out.append(f"{pin}: {', '.join(afs) or '-'}" + (f"; additional: {', '.join(extra)}" if extra else ""))
    return "\n".join(out)
