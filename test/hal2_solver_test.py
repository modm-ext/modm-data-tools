# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

import unittest

from modm_data.clocks import evaluate
from modm_data_tools.clocks.configure import solve_text
from modm_data_tools.clocks.modm import generate
from modm_data_tools.clocks.solver import max_performance
from modm_data.cubemx import clock_tree_from_partname
from modm_data.utils import ext_path


class Hal2SolverTest(unittest.TestCase):
    def setUp(self):
        # The CI does not clone the HAL2 device family packs
        if not ext_path("stmicro/hal2").exists():
            self.skipTest("no HAL2 device family packs")

    def test_solve(self):
        for device, targets, config, sysclk in (
            ("STM32G553CETx", "SYSCLK=max, FDCAN1=40M", "HSE=24M", 280e6),
            # The PSI locks to the HSE, from 25 MHz only to 100 MHz
            ("STM32C531CBTx", "SYSCLK=max", "HSE=24M", 144e6),
            ("STM32C531CBTx", "SYSCLK=max", "HSE=25M", 100e6),
            # The voltage scale limits SYSCLK
            ("STM32G553CETx", "SYSCLK=240M", "HSE=24M, PWR_Regulator_Voltage_Scale=SCALE1", 240e6),
        ):
            with self.subTest(device=device, targets=targets, config=config):
                s = solve_text(clock_tree_from_partname(device), targets, config)
                self.assertTrue(s["ok"])
                self.assertEqual(s["result"]["SysCLKOutput"]["freq"], sysclk)

    def test_fastest_core(self):
        tree = clock_tree_from_partname("STM32H7P5ZITx")
        for scale, sysclk, cpu, hclk in (
            # HCLK would need a divider of 2.0046 at 435 MHz, the BMPRE divides by 1 or 2 only
            ("SCALE1", 434e6, 434e6, 217e6),
            # 350 MHz would leave the CPU at 175 MHz and HCLK at 87.5 MHz
            ("SCALE2", 300e6, 300e6, 150e6),
        ):
            with self.subTest(scale=scale):
                s = solve_text(tree, "SYSCLK=max", f"HSE=24M, PWR_Regulator_Voltage_Scale={scale}")
                r = s["result"]
                self.assertEqual((r["SysCLKOutput"]["freq"], r["CpuClockOutput"]["freq"], r["AHBOutput"]["freq"]),
                                 (sysclk, cpu, hclk))  # fmt: skip
        # A SYSCLK target keeps the CPU as fast as HCLK allows, CPRE 2 and BMPRE 2, not CPRE 4
        s = solve_text(tree, "SYSCLK=435M", "HSE=24M, PWR_Regulator_Voltage_Scale=SCALE1")
        self.assertEqual(s["result"]["CpuClockOutput"]["freq"], 217.5e6)

    def test_voltage_scale_limit(self):
        s = solve_text(
            clock_tree_from_partname("STM32G553CETx"), "SYSCLK=250M", "HSE=24M, PWR_Regulator_Voltage_Scale=SCALE1"
        )
        self.assertFalse(s["ok"])

    def test_clock_meter_findings(self):
        # Measured on the NUCLEO-G564RE and NUCLEO-C5A3ZG with modm-hardware-ci/clockmeter
        tree = clock_tree_from_partname("STM32G564RETx")
        # RM0514: PLL1P divides by even factors only
        s = solve_text(tree, "SYSCLK=280M", "HSE=24M")
        self.assertEqual(s["result"]["PLL1P"]["setting"], 2)
        # DS14920 Table 18: the APB clocks run at the HCLK of each voltage scale
        self.assertFalse(solve_text(tree, "SYSCLK=170M, APB1Output=200M", "HSE=24M")["ok"])
        # The PSI multiplies 48 MHz by 100 / 48
        s = solve_text(clock_tree_from_partname("STM32C5A3ZGTx"), "SYSCLK=100M", "HSE=48M")
        self.assertTrue(s["ok"])

    def test_max_performance_with_every_peripheral(self):
        # "Every peripheral in use" must also check the limits gated by their usage flags
        tree = clock_tree_from_partname("STM32H7P4IGKx")
        r = max_performance(tree, {"HSEOSC": "24M"})
        self.assertIsNone(r["use"])
        every = {name for name, node in tree["nodes"].items() if node["kind"] == "peripheral"}
        result = evaluate(tree, r["config"], every)
        self.assertFalse([n for n, v in result.items() if v["used"] and v["errors"]])

    def test_modm_code(self):
        # A part of the STM32G5 Rcc API of modm feature/hal2-port, see modm_api.api_of
        api = {
            "functions": {
                "enableHseCrystal": "uint32_t waitLoops = 0x10000",
                "setVoltageScaling": "VoltageScaling voltage, uint32_t waitLoops = 0x10000",
                "setAhbPrescaler": "AhbPrescaler prescaler",
                "setApb1Prescaler": "ApbPrescaler prescaler",
                "enablePll1": "PllSource source, const PllConfig& cfg, uint32_t waitLoops = 0x10000",
                "enableSystemClock": "SystemClockSource src, uint32_t waitLoops = 0x10000",
                "setCanClockSource": "CanClockSource src",
                "setAdcClockSource": "AdcClockSource src, AdcPrescaler prescaler = AdcPrescaler::Div1",
            },
            "enums": {
                "VoltageScaling": [["Scale0"], ["Scale1"], ["Scale2"]],
                "AhbPrescaler": [["Div1"], ["Div2"]],
                "ApbPrescaler": [["Div1"], ["Div2"]],
                "PllSource": [["None"], ["Hsi"], ["Hse"]],
                "PllInputRange": [["MHz1_2"], ["MHz2_4"], ["MHz4_8"], ["MHz8_16"]],
                "SystemClockSource": [["Hsi"], ["Hse"], ["Pll1P"]],
                "CanClockSource": [["Hse"], ["Pll1Q"], ["Pll2Q"]],
                "AdcClockSource": [["Tim"], ["Hrtim"], ["Hsi"], ["Hse"], ["Pll1Q"]],
                "AdcPrescaler": [["Div1"], ["Div2"], ["Div4"]],
            },
            "structs": {"PllConfig": [["PllInputRange", "range", ""], ["uint8_t", "M", ""], ["uint16_t", "N", ""],
                                      ["uint8_t", "P", "0"], ["uint8_t", "Q", "0"], ["uint8_t", "R", "0"]]},
        }  # fmt: skip
        tree = clock_tree_from_partname("STM32G564RETx")
        s = solve_text(tree, "SYSCLK=280M, FDCAN1=40M, ADC1=70M", "HSE=24M")
        code = generate(tree, "STM32G564RETx", s["config"], s["result"], set(s["use"]), api)
        self.assertNotIn("TODO", code)
        for line in ("Rcc::enablePll1(Rcc::PllSource::Hse, pll1);", "Rcc::enableSystemClock(Rcc::SystemClockSource::Pll1P);",
                     "Rcc::setCanClockSource(Rcc::CanClockSource::Pll1Q);",
                     "Rcc::setAdcClockSource(Rcc::AdcClockSource::Tim, Rcc::AdcPrescaler::Div4);"):  # fmt: skip
            self.assertIn(line, code)


if __name__ == "__main__":
    unittest.main()
