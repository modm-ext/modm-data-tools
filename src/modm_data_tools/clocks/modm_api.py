# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""
The modm Rcc API of an STM32 device for the code generator.

Renders `modm:platform:rcc` with lbuild for the device and parses the enums,
the setter signatures and the PllConfig struct out of `rcc.hpp`. The result is
cached per modm commit and target. The modm repository is `$MODM_REPO`, else
`modm` next to this repository.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

from modm_data.utils import cache_path, root_path


def _lbuild() -> list[str]:
    """lbuild from PATH, the user's pip location or as a module of this Python, servers often lack the PATH."""
    if found := shutil.which("lbuild") or next(
        (str(p) for p in (Path.home() / ".local/bin/lbuild", Path(sys.executable).parent / "lbuild") if p.exists()),
        None,
    ):
        return [found]
    return [sys.executable, "-m", "lbuild"]


def modm_repo() -> Path:
    return Path(os.environ.get("MODM_REPO") or root_path("..") / "modm").resolve()


def _git_head(repo: Path) -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "src/modm/platform/clock"],
                           capture_output=True, text=True).stdout.strip()  # fmt: skip
    return out.stdout.strip() + ("-dirty" if dirty else "")


@cache
def targets(repo: Path) -> list[str]:
    out = subprocess.run([*_lbuild(), "-r", str(repo / "repo.lb"), "discover", "--values", "-n", "modm:target"],
                         capture_output=True, text=True, cwd=tempfile.gettempdir()).stdout  # fmt: skip
    return sorted(set(re.findall(r"stm32[a-z0-9]+", out)))


def target_of(partname: str, known: list[str]) -> str | None:
    """STM32G071RBTx is stm32g071rbt6, STM32L552ZETxQ stm32l552zet6q: the x is the temperature range."""
    pattern = re.compile(partname.lower().replace("x", "[0-9]", 1) + "$")
    found = [t for t in known if pattern.match(t)]
    return min(found, key=lambda t: (not re.search(r"6q?$", t), t)) if found else None


def parse(text: str) -> dict:
    """:return: The enums, functions, structs and constants of a rendered `rcc.hpp`."""
    enums = {}
    for name, body in re.findall(r"enum class\s+(\w+)\s*(?::\s*[\w:]+)?\s*\{(.*?)\};", text, re.S):
        body = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)
        enums.setdefault(name, [])
        for item in body.split(","):
            if (m := re.match(r"\s*(\w+)\s*(?:=\s*(.+?))?\s*$", item, re.S)) and m[1] not in [
                e[0] for e in enums[name]
            ]:
                enums[name].append([m[1], " ".join((m[2] or "").split())])
    # using I3cClockSource = I2cClockSource;
    for name, other in re.findall(r"using\s+(\w+)\s*=\s*(\w+)\s*;", text):
        if other in enums:
            enums.setdefault(name, enums[other])
    functions = {}
    for name, params in re.findall(
        r"\n\s*(?:static\s+)?(?:inline\s+)?(?:constexpr\s+)?(?:bool|void|uint32_t)(?:\s+modm_\w+)?(?:\s*//[^\n]*)?\s*\n\s*(\w+)\(([^)]*)\)",
        text,
    ):
        functions[name] = " ".join(params.split())
    structs = {}
    for name, body in re.findall(r"struct\s+(\w+)\s*\{(.*?)\};", text, re.S):
        fields = re.findall(r"^\s*([\w:]+)\s+(\w+)\s*(?:=\s*([^;]+))?;", body, re.M)
        structs[name] = [[t, n, d.strip()] for t, n, d in fields]
    constants = dict(re.findall(r"static constexpr uint32_t (\w+)\s*=\s*([^;]+);", text))
    return {"enums": enums, "functions": functions, "structs": structs, "constants": constants}


def render(target: str, repo: Path) -> dict | None:
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run([*_lbuild(), "-r", str(repo / "repo.lb"), "-D", f"modm:target={target}",
                        "build", "-m", "modm:platform:rcc"], cwd=folder, capture_output=True, text=True)  # fmt: skip
        path = Path(folder) / "modm/src/modm/platform/clock/rcc.hpp"
        return parse(path.read_text()) | {"target": target} if path.exists() else None


def api_of(partname: str, repo: Path | None = None) -> dict | None:
    """:return: The modm Rcc API for a CubeMX part name like STM32G071RBTx, None if modm has no such target."""
    repo = repo or modm_repo()
    if not (repo / "repo.lb").exists():
        raise ValueError(f"no modm repository at {repo}, set MODM_REPO")
    if not (target := target_of(partname, targets(repo))):
        return None
    path = cache_path(Path("rcc2") / _git_head(repo) / f"{target}.json")
    if path.exists() and "-dirty" not in path.parent.name:
        return json.loads(path.read_text())
    api = render(target, repo)
    if api is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(api, separators=(",", ":"), sort_keys=True))
    return api
