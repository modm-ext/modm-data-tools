# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

import contextlib
import io
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path
from unittest import mock

from modm_data.clocks import evaluate
from modm_data_tools.clocks import export, modm_api
from modm_data_tools.clocks.configure import _source_path, find_node, parse_targets, report, solve_text
from modm_data_tools.errors import UserError
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
        # USB needs 48 MHz from the PLLQ: 96 MHz with every peripheral instead of 100 MHz with none
        r = max_performance(tree("STM32F411CEUx"), {"HSEOSC": "25000000"})
        self.assertIsNone(r["use"])
        self.assertAlmostEqual(r["result"]["SysCLKOutput"]["freq"], 96e6, delta=0.5e6)
        # The N6 has no SYSCLK, the CPU clock is its core clock
        r = max_performance(tree("STM32N657X0HxQ"))
        self.assertEqual(r["core"], "CpuClockOutput")
        self.assertEqual(r["result"]["CpuClockOutput"]["freq"], 800e6)

    def test_mux_default_without_input(self):
        # The HAL2 PLL source of the G5 defaults to NoClock, which is no input: the solver may not keep it
        r = Solver(tree("STM32G564HBUx")).solve({}, (), {"USART1": 16e6}, {"USART1"}, 0, budget=3)
        self.assertTrue(r["ok"])

    def test_no_default(self):
        # The F105 PLLMUL has no default, the solver may not lock it at None
        r = Solver(tree("STM32F105R8Tx")).solve({}, (), {"RTC": 32768}, {"RTC"}, 0, budget=3)
        self.assertTrue(r["ok"])

    def test_factor_needs_input(self):
        # The C5 PSI x2 is for a 50 MHz reference only, 8 MHz x2 is no setting
        t = tree("STM32C591CETx")
        self.assertNotIn(16e6, Solver(t).reachable("SysCLKOutput"))
        r = Solver(t).solve({}, (), {"SysCLKOutput": 100e6}, None, 0, budget=5)
        self.assertEqual(r["config"]["PSI"], "12.5")
        self.assertFalse(evaluate(t, r["config"])["PSI"]["errors"])

    def test_config_mux_kept(self):
        # A mux chosen in config stays when the targets allow it, USB from PLL3 and not from the HSI48
        s = solve_text(tree("STM32H743ZITx"), "SYSCLK=480M, USB_OTG_FS=48M", "HSE=8M, USBMult=PLL3")
        self.assertTrue(s["ok"])
        self.assertIn("PLL3", s["config"]["USBMult"])

    def test_maxima(self):
        # A second maximum is solved after the first: the fastest FDCAN of the fastest SYSCLK
        s = solve_text(tree("STM32H743ZITx"), "SYSCLK=max, FDCAN=max", "HSE=25M")
        self.assertTrue(s["ok"])
        self.assertEqual(s["result"]["SysCLKOutput"]["freq"], 480e6)
        self.assertEqual(s["result"]["FDCAN1"]["freq"], 125e6)

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

    def test_invalid_input(self):
        """Values the solver would ignore and targets it cannot mean are errors of the input, also when locked"""
        t = tree("STM32G474RETx")
        for config, message in (
            ("HSE=8M, PLLN=1000", "PLLN must be within 8..127, not 1000"),
            ("SysClkSource=BOGUS", "SysClkSource has no input 'BOGUS', choose from RCC_SYSCLKSOURCE_HSI"),
            ("HSE=Crystal", "HSE has no mode 'Crystal', choose from ByPass, Oscillator"),
            ("PLLNN=20", "no clock 'PLLNN'"),
            ("PWR_Regulator_Voltage_Scale=SCALE9", "cannot be 'SCALE9', choose from PWR_REGULATOR_VOLTAGE_SCALE1"),
            ("HSE=8M, HSE=ByPass, PWR_Regulator_Voltage_Scale=SCALE1", None),
        ):
            with self.subTest(config=config):
                if message is None:
                    self.assertTrue(solve_text(t, "SYSCLK=max", config)["ok"])
                    continue
                with self.assertRaisesRegex(UserError, message):
                    solve_text(t, "SYSCLK=max", config, lock="PLLN")
        for targets, message in (
            ("SYSCLK=20M..10M", "the range 20M..10M is reversed"),
            ("USART1=0", "above 0 Hz"),
            ("ADC12<=0", "above 0 Hz"),
            ("SYSCLK=16M, SysCLKOutput=170M", "SysCLKOutput is a target twice"),
            (" , ", "no targets given"),
        ):
            with self.subTest(targets=targets), self.assertRaisesRegex(UserError, message):
                parse_targets(t, targets)

    def test_sources(self):
        """With an HSE the core runs from it, a target out of reach reports the closest frequency it gets"""
        t = tree("STM32G474RETx")
        s = solve_text(t, "SYSCLK=16M", "HSE=8M")
        self.assertTrue(s["ok"])
        self.assertTrue(_source_path(t, s["result"], "SysCLKOutput").endswith("HSEOSC"))
        s = solve_text(t, "SYSCLK=170M, SAI1=12.288M~0.1%", "HSE=24M")
        self.assertFalse(s["ok"])
        self.assertAlmostEqual(s["unmet"][0]["closest"], 12.333e6, delta=1e3)
        self.assertIn("SAI1 cannot reach its target on any path, the closest it gets", report(t, s))

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


class ModmApiTest(unittest.TestCase):
    """The modm Rcc API without lbuild and without the CubeMX database"""

    def test_parse(self):
        api = modm_api.parse("""
        class Rcc {
        public:
            enum class PllSource : uint32_t {
                Hsi = RCC_PLLCFGR_PLLSRC_HSI, // the HSI16
                Hse = RCC_PLLCFGR_PLLSRC_HSE,
            };
            using UsbClockSource = PllSource;
            struct PllConfig {
                uint8_t pllM;
                uint16_t pllN = 8;
            };
            static constexpr uint32_t BootFrequency = 16'000'000;
            static bool
            enablePll(PllSource source, const PllConfig& pllConfig);
        };""")
        self.assertEqual(
            api["enums"]["PllSource"], [["Hsi", "RCC_PLLCFGR_PLLSRC_HSI"], ["Hse", "RCC_PLLCFGR_PLLSRC_HSE"]]
        )
        self.assertEqual(api["enums"]["UsbClockSource"], api["enums"]["PllSource"])
        self.assertEqual(api["structs"]["PllConfig"], [["uint8_t", "pllM", ""], ["uint16_t", "pllN", "8"]])
        self.assertEqual(api["functions"], {"enablePll": "PllSource source, const PllConfig& pllConfig"})
        self.assertEqual(api["constants"], {"BootFrequency": "16'000'000"})

    def test_target_of(self):
        known = [
            "stm32g071rbt3",
            "stm32g071rbt6",
            "stm32g071rbt7",
            "stm32l552zet3q",
            "stm32l552zet6q",
            "stm32h7a3zit6q",
        ]
        self.assertEqual(modm_api.target_of("STM32G071RBTx", known), "stm32g071rbt6")
        self.assertEqual(modm_api.target_of("STM32L552ZETxQ", known), "stm32l552zet6q")
        self.assertEqual(modm_api.target_of("STM32H7A3ZITxQ", known), "stm32h7a3zit6q")
        self.assertIsNone(modm_api.target_of("STM32G071RBIx", known))

    def test_lbuild_failure(self):
        """A failed lbuild is an error with its output and not cached, the export names the device it leaves out"""
        failed = subprocess.CompletedProcess([], 1, "", "ERROR: no repo.lb")
        repo = Path(tempfile.gettempdir()) / "no-modm-here"
        with mock.patch("subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "lbuild discover .* failed: ERROR: no repo.lb"):
                modm_api.targets(repo)
            with self.assertRaisesRegex(RuntimeError, "failed: ERROR"):
                modm_api.render("stm32g071rbt6", repo)
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "stm32g071rbt6", "")):
            self.assertEqual(modm_api.targets(repo), ["stm32g071rbt6"])
        modm_api.targets.cache_clear()
        error = io.StringIO()
        with mock.patch.object(export, "api_of", side_effect=RuntimeError("lbuild failed")):
            with contextlib.redirect_stderr(error):
                self.assertEqual(export.export_api("STM32G071RBTx"), ("STM32G071RBTx", None))
        self.assertIn("STM32G071RBTx: no modm Rcc API, RuntimeError: lbuild failed", error.getvalue())


if __name__ == "__main__":
    unittest.main()
