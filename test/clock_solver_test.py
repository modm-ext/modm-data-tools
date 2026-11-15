# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

import contextlib
import io
import unittest
from functools import cache

from modm_data.clocks import evaluate
from modm_data_tools.clocks.configure import find_node, parse_targets, report, solve_text
from modm_data_tools.clocks.modm import _pick_setter, _Writer, candidates, generate, system_clock
from modm_data_tools.clocks.solver import Solver, max_performance
from modm_data_tools.lookup.__main__ import main as cli
from modm_data.utils import ext_path


@cache
def tree(partname):
    from modm_data.cubemx import clock_tree_from_partname

    return clock_tree_from_partname(partname)


class ClockSolverTest(unittest.TestCase):
    def setUp(self):
        # The CubeMX database is private, the CI does not check it out
        if not ext_path("stmicro/cubemx/mcu/families.xml").exists():
            self.skipTest("no CubeMX database")

    # device, base config, targets, use, solvable, tolerance
    CASES = [
        ("STM32G071RBTx", {}, {"SysCLKOutput": 64e6, "USART1": 16e6}, ["USART1"], True, 0),
        ("STM32G071RBTx", {}, {"SysCLKOutput": 65e6}, [], False, 0),
        ("STM32G071RBTx", {"PWR_Regulator_Voltage_Scale": "SCALE2"}, {"SysCLKOutput": 64e6}, [], False, 0),
        ("STM32G071RBTx", {}, {"SysCLKOutput": 64e6, "TIM1": 128e6}, ["TIM1"], True, 0),
        ("STM32F103RBTx", {"HSEOSC": "8000000"}, {"SysCLKOutput": 72e6, "USB": 48e6}, ["USB"], True, 0),
        ("STM32L476RGTx", {}, {"SysCLKOutput": 80e6, "USB_OTG_FS": 48e6}, ["USB_OTG_FS"], True, 0),
        ("STM32H743ZITx", {"HSEOSC": "8000000"}, {"SysCLKOutput": 480e6, "SPI1": 49.152e6}, ["SPI1"], True, 1e-5),
        ("STM32U083RCTx", {}, {"SysCLKOutput": 56e6, "TIM1": 56e6, "USART1": 56e6}, ["TIM1", "USART1"], True, 0),
    ]

    def test_solver(self):
        for device, config, targets, use, solvable, tolerance in self.CASES:
            with self.subTest(device=device, targets=targets):
                r = Solver(tree(device)).solve(config, (), targets, set(use), tolerance, budget=5)
                self.assertEqual(r["ok"], solvable)
                if r["ok"]:
                    # An independent evaluation meets every target without errors
                    check = evaluate(tree(device), r["config"], set(use))
                    for name, f in targets.items():
                        self.assertAlmostEqual(check[name]["freq"], f, delta=max(f * tolerance, 1e-3))
                    self.assertFalse([n for n, v in check.items() if v.get("used") and v.get("errors")])

    def test_max_performance(self):
        # The F7 limits HCLK only, through the over-drive
        r = max_performance(tree("STM32F767ZITx"), {"HSEOSC": "25000000"})
        self.assertEqual(r["result"]["SysCLKOutput"]["freq"], 216e6)

    def test_core_stays(self):
        # A kernel clock target that needs another PLL source keeps SYSCLK at its frequency
        t = tree("STM32H743ZITx")
        start = max_performance(t, {"HSEOSC": "25000000"})["config"]
        r = Solver(t).solve(start, (), {"SPI123Mult": 49.152e6}, {"SPI1"}, 0, budget=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["SysCLKOutput"]["freq"], 480e6)

    def test_rules_and_fast_failures(self):
        import time

        # The G0 TIM1 x2 needs an APB prescaler other than 1, which USART2 at 64 MHz rules out: TIM1 takes PLLQ
        t = tree("STM32G071RBTx")
        s = solve_text(t, "SYSCLK=64M, USART2=64M, I2C1=16M, TIM1=128M, ADC1<=35M", "HSE=8M, PLLN=16")
        self.assertTrue(s["ok"])
        self.assertEqual(s["result"]["TIM1"]["freq"], 128e6)
        # SAI1 at 12.288 MHz within 0.1 % is out of reach of the G4 integer PLL from 24 MHz: no search needed
        start = time.monotonic()
        s = solve_text(tree("STM32G474RETx"), "SYSCLK=160M, SAI1=12.288M~0.1%", "HSE=24M")
        self.assertFalse(s["ok"])
        self.assertEqual(s["unmet"][0]["id"], "SAI1")
        self.assertLess(time.monotonic() - start, 1)

    def test_targets(self):
        t = tree("STM32G474RETx")
        goals = parse_targets(t, "sysclk=max, usb=48M, FDCAN=40M~1%, adc12<=50M, I2C1>=8M, SAI1=12.288M..12.3M")
        self.assertIsNone(goals["SysCLKOutput"][2])
        self.assertEqual(goals[find_node(t, "usb")], (48e6 - 0.048, 48e6 + 0.048, 48e6))
        self.assertAlmostEqual(goals["FDCANoutput"][0], 39.6e6)
        self.assertEqual(goals[find_node(t, "I2C1")][0], 8e6)
        with self.assertRaisesRegex(ValueError, "did you mean"):
            parse_targets(t, "USART7=1M")

    def test_solve_text(self):
        t = tree("STM32G474RETx")
        # 170 MHz leaves no PLLQ for 40 MHz, the maximum steps down to 160 MHz and runs from the HSE
        s = solve_text(t, "SYSCLK=max, FDCAN=40M, USB=48M", "HSE=24M")
        self.assertTrue(s["ok"])
        self.assertEqual(s["result"]["SysCLKOutput"]["freq"], 160e6)
        self.assertIn("PLLSource=HSE", report(t, s))
        # No LSE on the board: USART2 must not run from it
        s = solve_text(tree("STM32L476RGTx"), "SYSCLK=80M, USART2<=10M")
        self.assertEqual(s["result"]["USART2"]["freq"], 10e6)
        # A limit of the locked voltage scale explains the failure
        s = solve_text(tree("STM32G071RBTx"), "SYSCLK=64M", "PWR_Regulator_Voltage_Scale=SCALE2")
        self.assertFalse(s["ok"])
        self.assertIn("max 16 MHz", report(tree("STM32G071RBTx"), s))
        # The maximum of the locked voltage scale
        s = solve_text(tree("STM32L476RGTx"), "SYSCLK=max", "HSE=8M, PWR_Regulator_Voltage_Scale=SCALE2")
        self.assertEqual(s["result"]["SysCLKOutput"]["freq"], 26e6)
        # 8 MHz / 7 * 210 / 2 is a little above the 120 MHz of the flash table, HCLK stays at SYSCLK
        s = solve_text(tree("STM32F205RGTx"), "SYSCLK=max", "HSE=8M")
        self.assertAlmostEqual(s["result"]["AHBOutput"]["freq"], 120e6, delta=1)

    def test_cli(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli(["clock_solve", "STM32G474RETx", "SYSCLK=max, FDCAN1=40M", "--config", "HSE=24M"])
        self.assertEqual(code, 0)
        self.assertIn("FDCAN1", output.getvalue())

    def test_modm_names(self):
        self.assertEqual(candidates("RTCWkupOutput"), ["RtcWakeup"])
        self.assertEqual(candidates("DIVS2"), ["Pll2S"])
        w = _Writer(tree("STM32F303RETx"), {"functions": {}, "enums": {}}, {}, {})
        # setTim34ClockSource clocks TIM3 and TIM4
        self.assertEqual(
            _pick_setter(w, "TIMMUX3", ["setTim2ClockSource", "setTim34ClockSource"]), "setTim34ClockSource"
        )
        w = _Writer(tree("STM32F769NIHx"), {"functions": {}, "enums": {}}, {}, {})
        setters = ["setDfsdm1KernelClock", "setDfsdm1AudioClock", "setSai1ClockSource"]
        self.assertEqual(_pick_setter(w, "DFSDMMult", setters), "setDfsdm1KernelClock")
        self.assertEqual(_pick_setter(w, "DFSDMAudioMult", setters), "setDfsdm1AudioClock")

    def test_modm_code(self):
        # The STM32F2 takes its PllConfig by value
        api = {
            "functions": {"enablePll": "PllSource src, PllConfig cfg", "enableSystemClock": "SystemClockSource src"},
            "enums": {"PllSource": [["Hsi"], ["Hse"]], "SystemClockSource": [["Hsi"], ["Hse"], ["Pll"]]},
            "structs": {"PllConfig": [["uint8_t", "M", ""], ["uint16_t", "N", ""], ["uint8_t", "P", ""]]},
        }
        t = tree("STM32F215RETx")
        s = solve_text(t, "SYSCLK=120M", "HSE=8M")
        data = system_clock(t, "STM32F215RETx", s["config"], s["result"], set(), api)
        self.assertEqual(data["constants"][0]["name"], "pll")
        self.assertEqual([f.split(" ")[0] for f in data["constants"][0]["fields"]], [".M", ".N", ".P"])
        code = generate(t, "STM32F215RETx", s["config"], s["result"], set(), api)
        self.assertIn("Rcc::enablePll(Rcc::PllSource::Hse, pll);", code)
        self.assertIn("\tstatic constexpr uint32_t SysClk    = 120_MHz;", code)


if __name__ == "__main__":
    unittest.main()
