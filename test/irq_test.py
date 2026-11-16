# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""modm_data_tools.irq: the source parsers, the event pairing and the interconnect tables on small inline inputs,
and the interrupt event graph and interconnect of a known device against the data in ext/. The expected bits of
the device are the ones of its reference manual (RM0440 USART_CR1/CR3/ISR/ICR, TIMx_SMCR.TS, Table 291)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from modm_data_tools.irq import graph, interconnect, sources
from modm_data_tools.lookup import docs, headers
from modm_data_tools.lookup import irq as lookup_irq


def _peripheral(base, **registers):
    """A header SVD peripheral: register name -> (offset, {field: bit offset or (offset, width)})"""
    return {
        "base": base,
        "registers": [
            {
                "name": name,
                "offset": offset,
                "fields": [
                    {"name": f, "offset": b if isinstance(b, int) else b[0], "width": 1 if isinstance(b, int) else b[1]}
                    for f, b in fields.items()
                ],
            }
            for name, (offset, fields) in registers.items()
        ],
    }


USART = _peripheral(
    0x40013800,
    CR1=(0x00, {"PEIE": 8, "TCIE": 6, "RXNEIE": 5, "UE": 0}),
    CR3=(0x08, {"EIE": 0}),
    ISR=(0x1C, {"TC": 6, "RXNE": 5, "ORE": 3, "NE": 2, "FE": 1, "PE": 0}),
    ICR=(0x20, {"TCCF": 6, "ORECF": 3, "NECF": 2, "FECF": 1, "PECF": 0}),
)


def _ev(e):
    """An event of _pair_by_name as (stem, enable, flag, clear) with register.field names"""
    name = lambda x: f"{x[1]}.{x[2]}" if x else None  # noqa: E731
    return e["stem"], name(e["enable"]), name(e["flag"]), name(e["clear"])


class Sources(unittest.TestCase):
    """The parsers of the sources, on inline text"""

    def tearDown(self):
        for f in (sources.rm_table, sources.rm_register, sources.header_vectors, sources.header_aliases):
            f.cache_clear()

    def test_clean(self):
        """Footnote markers, tags and non-breaking spaces go, whitespace collapses"""
        assert sources.clean("USART1<br>global interrupt(1)[^2]  ") == "USART1 global interrupt"
        assert sources.clean(None) == ""

    def test_rm_table(self):
        """Escaped pipes stay in their cell, also in the header, and the separator row is skipped"""
        table = (
            "RM9 v1 section 1\n\n**Table 3. Vectors**\n\n| Position | Acronym \\| alias | Description |\n|---|---|---|\n"
            "| 3 | RTC\\|TAMP | RTC \\| TAMP interrupts |\n| 4 |  FLASH  |  |"
        )
        with mock.patch.object(docs, "doc_table", return_value=table) as doc_table:
            header, rows = sources.rm_table("RM9", "3")
        doc_table.assert_called_once_with("RM9", "3", limit=1000)
        assert header == ["Position", "Acronym | alias", "Description"]
        assert rows == [["3", "RTC|TAMP", "RTC | TAMP interrupts"], ["4", "FLASH", ""]]

    def test_rm_register(self):
        """Fields of a register description with offset, width and access; rm_field tries the instance, the type
        and the x name, and finds a joined CMSIS name by its parts"""
        text = (
            "USART_CR1\n| Bit | Field | Access | Description |\n|---|---|---|---|\n"
            "| 31:29 | FIFOEN | rw | FIFO mode enable |\n| 6 | TCIE | rw | Transmission complete |\n"
            "| 5 | RXNEIE | rw | RXNE interrupt enable |\n| 4 | Res. |  |  |"
        )

        def doc_register(name, doc):
            if name != "USART_CR1":
                raise ValueError(name)
            return text

        with mock.patch.object(docs, "doc_register", side_effect=doc_register) as lookup:
            fields = sources.rm_register("RM9", "USART_CR1")
            # reserved rows ('Res.') are no fields
            assert fields == {"FIFOEN": (29, 3, "rw"), "TCIE": (6, 1, "rw"), "RXNEIE": (5, 1, "rw")}
            assert sources.rm_register("RM9", "USART1_CR1") == {}
            access, all_fields = sources.rm_field("RM9", "USART1", "CR1", "RXNEIE_RXFNEIE")
            assert access == "rw" and all_fields is fields
            assert sources.rm_field("RM9", "USART1", "CR1", "NOPE") == ("", fields)
            assert sources.rm_field("RM9", "SPI2", "CR1", "TCIE") == ("", {})
            tried = [c.args[0] for c in lookup.call_args_list]
        assert {"USART_CR1", "USART1_CR1", "SPI2_CR1", "SPI_CR1", "SPIx_CR1"} <= set(tried)

    def test_header(self):
        """Vectors with a position >= 0, and only the handler aliases whose name is a vector"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stm32test.h"
            path.write_text(
                "typedef enum {\n  NonMaskableInt_IRQn = -14,\n  WWDG_IRQn = 0,\n  USART1_IRQn = 37,\n"
                "  HASH_RNG_IRQn = 80,\n  RNG_IRQn = 81,\n} IRQn_Type;\n"
                "#define RNG_IRQHandler HASH_RNG_IRQHandler\n#define TIM1_UP_IRQHandler TIM1_UP_TIM16_IRQHandler\n"
            )
            with mock.patch.object(sources, "header_path", return_value=path):
                assert sources.header_vectors("test") == {"WWDG": 0, "USART1": 37, "HASH_RNG": 80, "RNG": 81}
                assert sources.header_aliases("test") == {"RNG": "HASH_RNG"}

    def test_condition(self):
        """CubeMX conditions on the part name and X_Exist, anything else is true"""
        cond = sources._condition
        assert cond("", "STM32G474RETx", set())
        assert cond("STM32G474RETx", "STM32G474RETx", set())
        assert cond("STM32G474xx", "STM32G474RETx", set())
        assert not cond("STM32G431xx", "STM32G474RETx", set())
        assert cond("TIM20_Exist", "STM32G474RETx", {"TIM20"})
        assert cond("!TIM20_Exist", "STM32G431RBTx", set())
        assert not cond("TIM20_Exist&!USB_Exist", "STM32G474RETx", {"TIM20", "USB"})
        assert cond("STM32G431xx|STM32G474xx", "STM32G474RETx", set())
        assert cond("((", "STM32G474RETx", set())

    def test_ll_events(self):
        """EnableIT/IsActiveFlag/ClearFlag of the LL drivers with their register, field and clear method"""
        fn = (
            "/**\n  * @brief  {brief}\n  * @rmtoll {reg:<12} {field:<12} LL_{per}_{kind}_{name}\n  * @retval None\n"
            "  */\n__STATIC_INLINE {ret} LL_{per}_{kind}_{name}({per}_TypeDef *{per}x)\n{{\n  {body}\n}}\n\n"
        )
        header = "".join(
            fn.format(brief="", reg=r, field=f, per=p, kind=k, name=n, ret=ret, body=b)
            for p, k, n, r, f, ret, b in [
                ("USART", "EnableIT", "TC", "CR1", "TCIE", "void", "ATOMIC_SET_BIT(USARTx->CR1, USART_CR1_TCIE);"),
                ("USART", "IsActiveFlag", "TC", "ISR", "TC", "uint32_t", "return (READ_BIT(USARTx->ISR, 1) == 1);"),
                ("USART", "ClearFlag", "TC", "ICR", "TCCF", "void", "WRITE_REG(USARTx->ICR, USART_ICR_TCCF);"),
                ("USART", "ClearFlag", "RXNE", "SR", "RXNE", "void", "__IO uint32_t tmp = USARTx->SR;\n  (void)tmp;"),
                ("TIM", "ClearFlag", "UPDATE", "SR", "UIF", "void", "WRITE_REG(TIMx->SR, ~(TIM_SR_UIF));"),
                ("RTC", "ClearFlag", "WUT", "RTC_ISR", "WUTF", "void", "CLEAR_BIT(RTCx->ISR, RTC_ISR_WUTF);"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "stm32xx_ll_test.h").write_text(header)
            events = sources.ll_events(Path(tmp))
        assert events[("USART", "TC")] == {
            "EnableIT": ("CR1", "TCIE"),
            "IsActiveFlag": ("ISR", "TC"),
            "ClearFlag": ("ICR", "TCCF"),
            "clear_kind": "write1",
        }
        assert events[("USART", "RXNE")]["clear_kind"] == "read"
        assert events[("TIM", "UPDATE")]["clear_kind"] == "write0"
        assert events[("RTC", "WUT")] == {"ClearFlag": ("ISR", "WUTF"), "clear_kind": "rmw0"}


class Pairing(unittest.TestCase):
    """How the graph finds instances, names sources and pairs enable, flag and clear bits"""

    def test_instances(self):
        """Core peripherals, secure aliases and address aliases go, the name with a digit stays"""
        peripherals = {
            "NVIC": _peripheral(0xE000E100),
            "DAC": _peripheral(0x50000800),
            "DAC1": _peripheral(0x50000800),
            "GPDMA1": _peripheral(0x40020000),
            "GPDMA1_S": _peripheral(0x50020000),
            "RTC_S": _peripheral(0x56007800),
            "USART1": USART,
            "OPAMP": _peripheral(0),
        }
        instances = graph._instances(peripherals)
        assert instances == {"DAC1", "GPDMA1", "RTC_S", "USART1", "OPAMP"}
        assert graph._canon("dac", instances, peripherals) == "DAC1"
        assert graph._canon("usart1", instances, peripherals) == "USART1"
        assert graph._canon("WWDG", {"WWDG1"}, {}) == "WWDG1"
        assert graph._canon("ADC12_COMMON", {"ADC1", "ADC2"}, {}) == "ADC1"
        assert graph._canon("ADC1_COMMON", {"ADC1"}, {}) == "ADC1"
        assert graph._canon("I2C9", instances, peripherals) is None

    def test_name_sources(self):
        """Instances named by vector names, RM acronyms and descriptions"""
        instances = {"TIM1", "TIM8", "TIM14", "TIM16", "USART1", "SPI1", "SPI4", "PWR", "RCC", "RTC", "DMA1"}
        names = graph._name_sources
        assert names("USART1", "", "", instances) == {"USART1"}
        assert names("TIM1_UP_TIM16", "", "", instances) == {"TIM1", "TIM16"}
        # pdf2html splits TIM14 into 'TIM1 4'
        assert names("TIM8_TRG_COM_TIM14", "TIM8_TRG_COM_TIM1 4", "", instances) == {"TIM8", "TIM14"}
        assert names("SPI4", "SPI4", "SPI 4 global interrupt", instances) == {"SPI4"}
        assert names("PVD_PVM", "", "", instances) == {"PWR"}
        assert names("RTC_WKUP", "", "", instances) == {"RTC"}
        assert names("DMA1_Channel2_3", "", "", instances) == {"DMA1", "DMA1#2", "DMA1#3"}
        assert names("DMA1_Ch4_7", "", "", instances) == {"DMA1", "DMA1#4", "DMA1#5", "DMA1#6", "DMA1#7"}
        # G0: the description lists the channels that the vector name does not
        assert names("DMA1_Ch4_5", "", "DMA1 channel 4, 5, 6, 7, DMAMUX", instances) == {
            "DMA1",
            "DMA1#4",
            "DMA1#5",
            "DMA1#6",
            "DMA1#7",
        }

    @pytest.mark.xfail(
        strict=True,
        reason="bug: _name_sources reads DMA1_Channel4_5_6_7 as the range 4..5 and stops a channel list at 'and', so "
        "on stm32f072rb the DMA1 channel 6/7 events match three vectors and are dropped from the HAL data",
    )
    def test_name_sources_dma_channel_lists(self):
        """Every channel of DMA1_Channel4_5_6_7 (F072 header) and of 'channel 4, 5, 6, and 7' (RM0490)"""
        all4to7 = {"DMA1", "DMA1#4", "DMA1#5", "DMA1#6", "DMA1#7"}
        assert graph._name_sources("DMA1_Channel4_5_6_7", "", "", {"DMA1"}) == all4to7
        assert graph._name_sources("DMA1_CH4_5", "", "DMA1 channel 4, 5, 6, and 7 interrupts", {"DMA1"}) == all4to7

    def test_pair_by_name(self):
        """USART bits paired by name, the ICR clear bits found, the shared error enables patched in"""
        events = sorted(_ev(e) for e in graph._pair_by_name({"USART1": USART}, "USART1"))
        assert events == [
            ("FE", "CR3.EIE", "ISR.FE", "ICR.FECF"),
            ("NE", "CR3.EIE", "ISR.NE", "ICR.NECF"),
            ("ORE", "CR1.RXNEIE", "ISR.ORE", "ICR.ORECF"),
            ("PE", "CR1.PEIE", "ISR.PE", "ICR.PECF"),
            ("RXNE", "CR1.RXNEIE", "ISR.RXNE", None),
            ("TC", "CR1.TCIE", "ISR.TC", "ICR.TCCF"),
        ]

    def test_pair_by_name_dma_stream(self):
        """A DMA stream finds its flags and clear bits in the controller, by the stream number"""
        peripherals = {
            "DMA1": _peripheral(0x40026000, LISR=(0x0, {"TCIF0": 5, "TCIF1": 11}), LIFCR=(0x8, {"CTCIF0": 5})),
            "DMA1_Stream0": _peripheral(0x40026010, CR=(0x0, {"TCIE": 4})),
        }
        events = graph._pair_by_name(peripherals, "DMA1_Stream0")
        assert [_ev(e) for e in events] == [("TC", "CR.TCIE", "LISR.TCIF0", "LIFCR.CTCIF0")]
        assert events[0]["channel"] == "0" and events[0]["flag"][0] == "DMA1"
        assert graph._ll_type("DMA1_Stream0") == "DMA" and graph._ll_type("USART1") == "USART"

    def test_clear_candidates(self):
        assert graph._clear_candidates("TCF", "TC", "") == [
            "CTCF",
            "TCC",
            "TCCF",
            "TCC",
            "TCCF",
            "TCFC",
            "CLR_TCF",
            "CTC",
            "TCF",
        ]
        assert graph._clear_candidates("ORE", "ORE", "")[-1] == "ORE"

    def test_pair_by_ll(self):
        """LL events whose bits exist in the instance, a field unique in the instance resolves another register"""
        ll = {
            ("USART", "TC"): {
                "EnableIT": ("CR1", "TCIE"),
                "IsActiveFlag": ("SR", "TC"),
                "ClearFlag": ("ICR", "TCCF"),
                "clear_kind": "write1",
            },
            ("USART", "WKUP"): {"EnableIT": ("CR3", "WUFIE"), "IsActiveFlag": ("ISR", "WUF")},
            ("SPI", "TXE"): {"EnableIT": ("CR2", "TXEIE"), "IsActiveFlag": ("SR", "TXE")},
        }
        assert graph._pair_by_ll({"USART1": USART}, "USART1", ll) == {
            ("TCIE", "TC"): {
                "enable": ("CR1", "TCIE"),
                "flag": ("ISR", "TC"),
                "clear": ("ICR", "TCCF"),
                "clear_kind": "write1",
                "name": "TC",
            }
        }

    def test_rm_confirms(self):
        """The interrupt event table of the chapter that names the instance confirms an event"""
        row = lambda flag, enable: {"flag": flag, "enable": enable, "clear": ""}  # noqa: E731
        titles = {
            "32 Basic timers (TIM6/TIM7)": [row("UIF", "UIE")],
            "30 General-purpose timers (TIM2 to TIM5)": [row("CC1IF", "CC1IE")],
            "29 Advanced-control timers (TIM1/TIM8/TIM20)": [row("CC2IF", "CC2IE"), row("B2IF", "BIE")],
            "26 Direct memory access controller (DMA)": [row("TCIFx", "TCIE")],
        }
        assert graph._title_instances("General-purpose timers (TIM2 to TIM5)") == {"TIM2", "TIM3", "TIM4", "TIM5"}
        assert graph._title_instances("TIM15/16/17") == {"TIM15", "TIM16", "TIM17"}
        event = lambda inst, en, fl: {"peripheral": inst, "enable": [inst, "DIER", en, 0], "flag": [inst, "SR", fl, 0]}  # noqa: E731
        assert graph._rm_confirms(event("TIM6", "UIE", "UIF"), titles)
        assert graph._rm_confirms(event("TIM1", "CC2IE", "CC2IF"), titles)
        # TIM3 only looks at its own chapter, the update interrupt is in the one of the basic timers
        assert not graph._rm_confirms(event("TIM3", "UIE", "UIF"), titles)
        assert graph._rm_confirms(event("TIM3", "CC1IE", "CC1IF"), titles)
        assert graph._rm_confirms(event("TIM20", "BIE", "B2IF"), titles)
        # numbered flags match the x form of the manual
        assert graph._rm_confirms(
            {"peripheral": "DMA1", "enable": ["DMA1", "CCR3", "TCIE", 1], "flag": ["DMA1", "ISR", "TCIF3", 9]}, titles
        )

    def test_normalize(self):
        """The HAL data: masks per event, clear methods, EXTI registers; dropped and ambiguous events are left out"""
        peripherals = {
            "USART1": USART,
            "EXTI": _peripheral(0x40010400, IMR1=(0x0, {}), RTSR1=(0x8, {}), PR1=(0x14, {})),
        }

        def addr(instance, register):
            return peripherals[instance]["base"] + next(
                r["offset"] for r in peripherals[instance]["registers"] if r["name"] == register
            )

        def ev(stem, enable, flag, clear=None, **extra):
            return {
                "peripheral": "USART1",
                "stem": stem,
                "channel": None,
                "vectors": ["USART1"],
                "in_rm": True,
                "driver": True,
                "provenance": ["name"],
                "enable": ["USART1", enable[0], "", enable[1]],
                "flag": ["USART1", flag[0], "", flag[1]],
                "clear": ["USART1", clear[0], "", clear[1]] if clear else None,
                "flag_access": "",
                "exti": None,
                **extra,
            }

        g = {
            "vectors": {"USART1": {"irqn": 37}, "PVD": {"irqn": 1}},
            "events": [
                ev("TC", ("CR1", 6), ("ISR", 6), ("ICR", 6)),
                ev("TC", ("CR1", 6), ("ISR", 6), ("ICR", 6)),  # the same name twice: the first one wins
                ev("RXNE", ("CR1", 5), ("ISR", 5)),
                ev("PE", ("CR1", 8), ("ISR", 0), flag_access="rc_w0", w0_ones=0b11, w0_keep=0x100),
                ev("FE", ("CR3", 0), ("ISR", 1), flag_access="rc_w1", exti=[25, "configurable"]),
                ev("NE", ("CR3", 0), ("ISR", 2), in_rm=False),
                ev("ORE", ("CR1", 5), ("ISR", 3), vectors=["USART1", "USART2"]),
            ],
            "exti": [
                {"line": 16, "source": "PVD", "type": "configurable", "nvic": True, "vectors": ["PVD"]},
                {"line": 25, "source": "USART1", "type": "configurable", "nvic": True, "vectors": ["USART1"]},
                {"line": 40, "source": "COMP", "type": "configurable", "nvic": True, "vectors": ["PVD"]},
                {"line": 17, "source": "RTC", "type": "direct", "nvic": True, "vectors": ["PVD"]},
            ],
        }
        data = graph.normalize(g, peripherals, addr)
        assert data["vectors"] == {"USART1": 37, "PVD": 1}
        events = {e["name"]: e for e in data["events"]}
        assert list(events) == ["USART1_TC", "USART1_RXNE", "USART1_PE", "USART1_FE", "EXTI16"]
        assert events["USART1_TC"]["enable"] == [0x40013800, 1 << 6]
        assert events["USART1_TC"]["flag"] == [0x4001381C, 1 << 6]
        assert events["USART1_TC"]["clear"] == ["write", 0x40013820, 1 << 6, 0]
        assert events["USART1_RXNE"]["clear"] == ["none", 0, 0, 0]
        # rc_w0: write 0 to the flag, 1 to the other rc_w0 flags, keep the rw bits
        assert events["USART1_PE"]["clear"] == ["write0", 0x4001381C, 0b10, 0x100]
        assert events["USART1_FE"]["clear"] == ["write", 0x4001381C, 1 << 1, 0]
        assert events["USART1_FE"]["exti"] == {
            "line": 25,
            "kind": "rising",
            "imr": 0x40010400,
            "mask": 1 << 25,
            "rtsr": 0x40010408,
            "pr": 0x40010414,
        }
        assert events["EXTI16"]["enable"] == [0x40010400, 1 << 16] and events["EXTI16"]["exti"]["kind"] == "rising_only"


class Interconnect(unittest.TestCase):
    """The timer trigger and DAC trigger tables in the layouts of the manuals"""

    def test_signal(self):
        signal = interconnect.signal
        assert signal("TIM1") == "TIM1_TRGO"
        assert signal("tim3_trgo") == "TIM3_TRGO"
        assert signal("TIM14_OC1") == "TIM14_OC1"
        assert signal("lptim1_out") == "LPTIM1_OUT"
        assert signal("TIM2 (TRGO)") == "TIM2_TRGO"
        assert signal("TIM5", "OC1") == "TIM5_OC1"
        assert signal("USB SOF SYNC") == "USB_SOF_SYNC"
        assert signal("EXTI9") == "EXTI9"
        for text in ("Reserved", "-", "", "USB", "SW trigger"):
            assert signal(text) is None, text

    def test_ts_value(self):
        """TIMx_SMCR.TS: ITR0..3 are 0..3, ITR4 and up start at 0b01000 (RM0440 TIMx_SMCR)"""
        assert [interconnect.ts_value(i) for i in (0, 3, 4, 10, 11, 15)] == [0, 3, 8, 14, 15, 19]

    def _tables(self, doc, tables):
        """Patches the reference manual tables: number -> (caption, header, rows)"""
        listing = [(n, c, "1.2", " | ".join(h)) for n, (c, h, _) in tables.items()]
        return (
            mock.patch.object(sources, "_rm_tables", return_value=listing),
            mock.patch.object(sources, "rm_table", side_effect=lambda d, n: tables[n][1:]),
        )

    def test_timer_triggers(self):
        """Rows per slave timer (F4, L4) and rows per ITRn with slave timer columns (G4, RM0440 Table 291)"""
        tables = {
            "1": (
                "TIMx internal trigger connection",
                ["Slave TIM", "ITR0 (TS = 000)", "ITR1 (TS = 001)", "ITR2 (TS = 010)", "ITR3 (TS = 011)"],
                [["TIM2", "TIM1", "TIM8/OTG_FS_SOF", "TIM3", "TIM4"], ["TIM3 and TIM4", "TIM1", "TIM2", "-", "TIM4"]],
            ),
            "2": (
                "TIMx internal trigger connection",
                ["TIMx", "TIM15", "TIM16/TIM17"],
                [
                    ["tim_itr0", "tim2_trgo", "tim1_trgo"],
                    ["tim_itr4", "Reserved", "tim16_oc1"],
                    ["tim_itr11", "USB SOF SYNC", "-"],
                    ["tim_itr[15:12]", "Reserved", ""],
                ],
            ),
            "3": ("Timer synchronization", ["Slave TIM", "ITR0"], [["TIM9", "TIM1"]]),
        }
        a, b = self._tables("RM9", tables)
        with a, b:
            triggers = interconnect.timer_triggers("RM9")
        assert triggers == {
            "TIM2.TS": {"TIM1_TRGO": 0, "TIM8_TRGO": 1, "TIM3_TRGO": 2, "TIM4_TRGO": 3},
            "TIM3.TS": {"TIM1_TRGO": 0, "TIM2_TRGO": 1, "TIM4_TRGO": 3},
            "TIM4.TS": {"TIM1_TRGO": 0, "TIM2_TRGO": 1, "TIM4_TRGO": 3},
            "TIM15.TS": {"TIM2_TRGO": 0, "USB_SOF_SYNC": 15},
            "TIM16.TS": {"TIM1_TRGO": 0, "TIM16_OC1": 8},
            "TIM17.TS": {"TIM1_TRGO": 0, "TIM16_OC1": 8},
        }

    def test_dac_triggers(self):
        """dac_chx_trgN rows (G0, H7), binary TSEL per source (L4) and TSEL rows per DAC (G4)"""
        tables = {
            "1": (
                "DAC2 interconnection",
                ["Signal name", "Source", "Type"],
                [["dac_chx_trg1", "tim1_trgo", "Internal"], ["dac_chx_trg2", "Reserved", ""]],
            ),
            "2": (
                "External triggers (DAC)",
                ["Source", "Type", "TSEL[2:0]"],
                [
                    ["TIM6_TRGO event", "Internal", "000"],
                    ["SWTRIG", "Software", "111"],
                    ["EXTI9", "External pin", "110"],
                ],
            ),
            "3": (
                "DAC trigger selection",
                ["TSEL", "DAC3 update", "DAC4 update", "DAC1 other"],
                [["0", "SW trigger", "SW trigger", "tim1_trgo"], ["1", "tim1_trgo", "tim8_trgo", "-"]],
            ),
            "4": ("HRTIM DAC triggers", ["Signal name"], [["dac_chx_trg9", "hrtim_dac_trg1"]]),
        }
        a, b = self._tables("RM9", tables)
        with a, b:
            triggers = interconnect.dac_triggers("RM9")
        assert triggers == {
            "DAC2.TSEL1": {"TIM1_TRGO": 1},
            "DAC2.TSEL2": {"TIM1_TRGO": 1},
            "DAC1.TSEL1": {"TIM6_TRGO": 0, "EXTI9": 6},
            "DAC1.TSEL2": {"TIM6_TRGO": 0, "EXTI9": 6},
            "DAC3.TSEL1": {"TIM1_TRGO": 1},
            "DAC3.TSEL2": {"TIM1_TRGO": 1},
            "DAC4.TSEL1": {"TIM8_TRGO": 1},
            "DAC4.TSEL2": {"TIM8_TRGO": 1},
        }


@unittest.skipUnless(
    headers.DB.exists() and docs.ARCHIVE.exists(), "needs ext/modm-data/devices.db and the HTML archive"
)
class Device(unittest.TestCase):
    """The graph and the interconnect of the STM32G474RE against RM0440"""

    def test_usart1_events(self):
        """USART1 (base 0x40013800): CR1 +0x00, CR3 +0x08, ISR +0x1C, ICR +0x20, bits as in RM0440"""
        g = graph.build("stm32g474re")
        assert g["rm"] == "RM0440" and g["data"]["vectors"]["USART1"] == 37
        events = {e["name"]: e for e in g["data"]["events"] if e["vector"] == "USART1"}
        assert events["USART1_TC"]["enable"] == [0x40013800, 1 << 6]
        assert events["USART1_TC"]["flag"] == [0x4001381C, 1 << 6]
        assert events["USART1_TC"]["clear"] == ["write", 0x40013820, 1 << 6, 0]
        assert events["USART1_PE"]["enable"] == [0x40013800, 1 << 8]
        assert events["USART1_PE"]["clear"] == ["write", 0x40013820, 1 << 0, 0]
        # RXNE is cleared by reading RDR, it has no clear bit
        assert events["USART1_RXNE"]["flag"] == [0x4001381C, 1 << 5]
        assert events["USART1_RXNE"]["clear"] == ["none", 0, 0, 0]
        # The error interrupt enable CR3.EIE covers noise and framing errors
        assert events["USART1_NE"]["enable"] == events["USART1_FE"]["enable"] == [0x40013808, 1 << 0]
        assert events["USART1_ORE"]["clear"] == ["write", 0x40013820, 1 << 3, 0]

        text = lookup_irq.irq_events("stm32g474re", "^USART1$")
        assert "37 USART1: USART1[cubemx,name,rm,svd]" in text, text
        assert "USART1 TC: enable CR1.TCIE, flag ISR.TC, write 1 to ICR.TCCF" in text, text
        assert "USART1 NE: enable CR3.EIE, flag ISR.NE, write 1 to ICR.NECF" in text, text

    def test_timer_itr(self):
        """TIM2_SMCR.TS (TIM2 at 0x40000000, SMCR +0x08, TS[2:0] at bit 4 and TS[4:3] at bit 20), RM0440 Table 291"""
        sinks = interconnect.build("stm32g474re")["sinks"]
        ts = sinks["TIM2_TS"]
        assert ts["address"] == 0x40000008 and ts["parts"] == [[4, 3], [20, 2]]
        assert ts["values"]["TIM1_TRGO"] == 0 and ts["values"]["TIM3_TRGO"] == 2
        # tim_itr4 = tim5_trgo is TS = 0b01000, tim_itr11 = USB SOF SYNC is 0b01111
        assert ts["values"]["TIM5_TRGO"] == 0b01000 and ts["values"]["USB_SOF_SYNC"] == 0b01111
        assert "TIM2_TRGO" not in ts["values"]  # reserved: a timer does not trigger itself
        assert "TIM2_TRGO" not in sinks["TIM3_TS"]["values"] or sinks["TIM3_TS"]["values"]["TIM2_TRGO"] == 1
        text = lookup_irq.interconnect("stm32g474re", sink="TIM2", signal="TIM5")
        assert "TIM2_TS @0x40000008 (bits 6:4 + bits 21:20): TIM5_TRGO=8" in text and "TIM20" not in text, text


if __name__ == "__main__":
    unittest.main()
