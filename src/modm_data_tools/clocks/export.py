# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""
Adds what the clock solver page (web/clocks/) needs next to the clock trees that
`python -m modm_data.clocks --output <folder>` exported:

    python -m modm_data_tools.clocks --output <folder>

Writes the modm Rcc API of every device in <folder>/data/devices.json that has a
modm target to data/rcc.json and data/rcc/<id>.json (lbuild of $MODM_REPO, see
`modm_api`), identical ones share a file. Writes the max performance of every clock
tree to data/max.json, since the search is too slow in Pyodide. Copies the pure Python clock code that
the page runs in Pyodide to py/ (web/clocks/py/site_api.py calls it).

The data is derived from STM32CubeMX and under the ST SLA: never commit it.
"""

import argparse
import hashlib
import json
import shutil
import sys
from multiprocessing import Pool
from pathlib import Path

import modm_data.clocks

import modm_data_tools
import modm_data_tools.clocks
from .modm_api import api_of
from .solver import max_performance


def export_api(partname: str):
    try:
        return partname, api_of(partname)
    except Exception as error:
        # the device is left out of rcc.json, but not silently
        print(f"{partname}: no modm Rcc API, {type(error).__name__}: {error}", file=sys.stderr)
        return partname, None


def export_apis(out: Path, partnames: list[str]):
    """The modm Rcc API per device, identical ones share a file."""
    (out / "rcc").mkdir(parents=True, exist_ok=True)
    index = {}
    with Pool(8) as pool:
        for partname, api in pool.imap_unordered(export_api, partnames, chunksize=4):
            if api is None:
                continue
            text = json.dumps({k: v for k, v in api.items() if k != "target"}, separators=(",", ":"), sort_keys=True)
            key = hashlib.sha1(text.encode()).hexdigest()[:12]
            (out / "rcc" / f"{key}.json").write_text(text)
            index[partname] = key
    (out / "rcc.json").write_text(json.dumps(dict(sorted(index.items())), separators=(",", ":")))
    print(f"{len(index)} devices with a modm Rcc API")


def export_max_one(path: Path):
    r = max_performance(json.loads(path.read_text()))
    if r is None:
        return path.stem, None
    # what web/clocks/app.js reads of a max_performance() result
    return path.stem, {
        "config": r["config"],
        "use": r["use"],
        "result": {"SysCLKOutput": {"freq": r["result"]["SysCLKOutput"]["freq"]}},
    }


def export_max(out: Path, keys: set[str]):
    """The max performance of every clock tree at its default HSE."""
    paths = [out / "tree" / f"{key}.json" for key in sorted(keys)]
    with Pool(8) as pool:
        index = {k: v for k, v in pool.imap_unordered(export_max_one, paths) if v is not None}
    (out / "max.json").write_text(json.dumps(dict(sorted(index.items())), separators=(",", ":"), default=str))
    print(f"{len(index)} clock trees with a max performance")


def copy_python(target: Path):
    """The pure Python clock code in its packages, which web/clocks/worker.js loads into Pyodide."""
    packages = {
        "modm_data/clocks": (Path(modm_data.clocks.__file__).parent, ("__init__.py", "evaluate.py")),
        "modm_data_tools/clocks": (
            Path(modm_data_tools.clocks.__file__).parent,
            ("__init__.py", "solver.py", "configure.py", "modm.py"),
        ),
    }
    for package, (source, names) in packages.items():
        folder = target / package
        folder.mkdir(parents=True, exist_ok=True)
        # the parent packages import nothing in the browser
        (folder.parent / "__init__.py").write_text("")
        for name in names:
            shutil.copy(source / name, folder / name)
    # configure.py raises its errors
    shutil.copy(Path(modm_data_tools.__file__).parent / "errors.py", target / "modm_data_tools/errors.py")


def main():
    parser = argparse.ArgumentParser(description="Add the modm Rcc API and the Python code for the clock solver page.")
    parser.add_argument("--output", type=Path, required=True, help="folder of the page with the exported clock trees")
    args = parser.parse_args()
    devices = json.loads((args.output / "data/devices.json").read_text())
    export_apis(args.output / "data", sorted(devices))
    export_max(args.output / "data", set(devices.values()))
    copy_python(args.output / "py")
