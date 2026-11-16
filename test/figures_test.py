# Copyright 2026, Niklas Hauser
# SPDX-License-Identifier: MPL-2.0

"""modm_data_tools.figures on small hand-drawn SVGs in the format of the HTML archive figures: a chart with two
curves and a legend, a chart with a log axis, a timing diagram with a clock, a signal, a bus and an interval, and a
state machine. The figures of real documents are checked in lookup_test.py (test_figure_data)."""

import tempfile
import unittest
from pathlib import Path

import pytest

from modm_data_tools.figures import curves, figure_data, graph, timing


def _path(points, stroke="#000000", width=0.5, fill="none"):
    d = "M" + "L".join(f"{x:g} {y:g}" for x, y in points)
    return f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>'


def _text(text, x, y, size=8, length=None):
    length = 0.5 * size * len(text) if length is None else length
    return f'<text font-size="{size}" x="{x:g}" y="{y:g}" textLength="{length:g}">{text}</text>'


def _svg(width, height, parts):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">' + "".join(parts) + "</svg>"


def _head(x, y, dx):
    """A filled arrow head with its tip at x, y pointing right (dx=1) or left (dx=-1)"""
    return _path([(x, y), (x - 4 * dx, y - 1.5), (x - 4 * dx, y + 1.5), (x, y)], "none", 0, "#000000")


def _chart(xlabels=("0", "25", "50", "75", "100"), ylabels=("0", "1", "2", "3", "4")):
    """Plot area x 30..130, y 10..110 with a grid at the ticks. Red: y = x / 25 (0..4 over 0..100),
    blue: y = 4 - x / 50, both in the plot positions of the default labels, with a legend inside."""
    parts = []
    for i, label in enumerate(xlabels):
        x = 30 + 25 * i
        parts += [_path([(x, 10), (x, 110)], "#808080", 0.25), _text(label, x - 4, 120.8, length=8)]
    for i, label in enumerate(ylabels):
        y = 110 - 25 * i
        parts += [
            _path([(30, y), (130, y)], "#808080", 0.25),
            _text(label, 27 - 2 * len(label), y + 2.8, length=2 * len(label)),
        ]
    parts.append(_text("Temperature (°C)", 50, 132, length=60))
    parts.append('<text font-size="8" transform="translate(12 60) rotate(-90)" textLength="40">Current</text>')
    parts.append(_path([(30 + x, 110 - x) for x in range(0, 101, 5)], "#ff0000", 1))
    parts.append(_path([(30 + x, 10 + 0.5 * x) for x in range(0, 101, 5)], "#0000ff", 1))
    parts += [_path([(40, 60), (52, 60)], "#ff0000", 1), _text("VDD = 3.3 V", 55, 62.8, length=40)]
    parts += [_path([(40, 70), (52, 70)], "#0000ff", 1), _text("VDD = 1.8 V", 55, 72.8, length=40)]
    return _svg(150, 140, parts)


def _wave(y, levels, x0=40, step=20, high=8):
    """A square wave, one level per half clock cycle of step"""
    points = []
    for i, v in enumerate(levels):
        level = y - high if v else y
        if points and points[-1][1] != level:
            points.append((points[-1][0], level))
        points += [(x0 + i * step, level), (x0 + (i + 1) * step, level)]
    return [p for i, p in enumerate(points) if not i or p != points[i - 1]]


def _timing():
    parts = [_text("CLK", 5, 22), _path(_wave(25, [0, 1] * 5))]
    parts += [_text("EN", 5, 52), _path(_wave(55, [0, 0, 0, 1, 1, 1, 1, 0, 0, 0]))]
    # setup time from the rising edge of EN to the next rising clock edge
    parts += [_path([(100, 68), (140, 68)]), _head(100, 68, -1), _head(140, 68, 1), _text("t_SU", 112, 76, 6)]
    # a bus with the value D0 from cycle 1 to 3
    parts += [_text("DATA", 5, 92), _text("D0", 130, 93, 6)]
    parts.append(_path([(40, 95), (98, 95), (102, 87), (178, 87), (182, 95), (240, 95)]))
    parts.append(_path([(40, 87), (98, 87), (102, 95), (178, 95), (182, 87), (240, 87)]))
    return _svg(260, 110, parts)


def _state_machine():
    def box(x, y, w, h):
        return _path([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)])

    parts = [box(20, 20, 50, 20), _text("IDLE", 33, 33), box(130, 20, 50, 20), _text("RUN", 138, 33)]
    parts += [box(75, 80, 50, 20), _text("ERROR", 85, 93)]
    parts += [_path([(70, 25), (130, 25)]), _head(130, 25, 1), _text("start", 90, 22, 6)]
    parts += [_path([(130, 35), (70, 35)]), _head(70, 35, -1), _text("stop", 92, 45, 6)]
    parts += [_path([(155, 40), (155, 90), (125, 90)]), _head(125, 90, -1), _text("fault", 158, 70, 6)]
    return _svg(200, 120, parts)


class Parsers(unittest.TestCase):
    """The pure parts: tick labels, axis fits, shapes, Mermaid"""

    def test_number(self):
        """Tick labels with SI prefixes and units"""
        assert curves._number("10 mA") == (0.01, "A")
        assert curves._number("2.5") == (2.5, "")
        assert curves._number("1,5 V") == (1.5, "V")
        assert curves._number("−4") == (-4.0, "")
        assert curves._number("100k") == (100e3, "")
        assert curves._number("3 MHz") == (3e6, "Hz")
        # A bare m is a unit (meters, minutes), not milli
        assert curves._number("5 m") == (5.0, "m")
        assert curves._number("1e-3") == (0.001, "")
        assert curves._number("VDD") is None and curves._number("1.8 V to 3.6 V") is None

    def test_fit(self):
        """Linear and logarithmic axes, the better fit wins"""
        scale, a, b, residual = curves._fit([0, 10, 20, 30], [0, 1, 2, 3])
        assert scale == "linear" and abs(a - 0.1) < 1e-9 and abs(b) < 1e-9 and residual < 1e-9
        scale, a, b, residual = curves._fit([0, 10, 20, 30], [1, 10, 100, 1000])
        assert scale == "log" and abs(a - 0.1) < 1e-9 and residual < 1e-9
        # Non-positive values cannot be logarithmic
        assert curves._fit([0, 10, 20], [-1, 10, 100])[0] == "linear"

    def test_axis(self):
        """Axis values: linear, log and evenly spaced categories"""
        assert curves.Axis("", "linear", 2, 1, [1, 3], 0).value(3) == 7
        assert curves.Axis("", "log", 1, 0, [1, 100], 0).value(2) == 100
        category = curves.Axis("", "category", 0, 0, [-40, 25, 85], 0, [0, 10, 20])
        assert category.value(5) == -7.5 and category.range == (-40, 85)

    def test_curve(self):
        """Curves interpolate linearly, in log x for log axes, and only in their range"""
        curve = curves.Curve("a", "#ff0000", None, [(0, 0), (10, 5)])
        assert curve(4) == 2
        with self.assertRaises(ValueError):
            curve(11)
        assert curves.Curve("b", "#ff0000", None, [(1, 0), (100, 2)], xlog=True)(10) == 1

    def test_superscripts(self):
        """10 with a raised exponent is a power of ten"""
        base = curves.Text("10", 14, 50, 10, 18, 8, False)
        exponent = curves.Text("-3", 20, 48, 18.5, 21.5, 5, False)
        joined = curves._superscripts([base, exponent])
        assert [t.text for t in joined] == ["1e-3"] and curves._number(joined[0].text) == (0.001, "")
        # Beside the 10, not raised: two labels
        low = curves.Text("3", 20, 50, 18.5, 21.5, 5, False)
        assert [t.text for t in curves._superscripts([base, low])] == ["10", "3"]

    def test_chart_caption(self):
        assert curves.is_chart_caption("Typical current consumption vs. temperature")
        for caption in ("LQFP64 pinout", "Package outline", "Clock tree block diagram", "UART state machine"):
            assert not curves.is_chart_caption(caption), caption

    def test_shapes(self):
        """Node shapes by how much of their box they fill, point in polygon, distance to a segment"""
        assert graph._shape([(0, 0), (10, 0), (10, 5), (0, 5)]) == "rectangle"
        assert graph._shape([(5, 0), (10, 5), (5, 10), (0, 5)]) == "diamond"
        assert graph._shape([(0, 0), (10, 0), (10, 10), (5, 4), (0, 10)]) is None
        assert graph._inside((5, 2), [(0, 0), (10, 0), (10, 5), (0, 5)])
        assert not graph._inside((11, 2), [(0, 0), (10, 0), (10, 5), (0, 5)])
        assert graph._distance((5, 3), (0, 0), (10, 0)) == 3
        assert graph._distance((13, 4), (0, 0), (10, 0)) == 5
        assert graph._distance((3, 4), (0, 0), (0, 0)) == 5

    def test_mermaid(self):
        """Quotes in labels and pipes in edge labels do not break the Mermaid syntax"""
        g = {
            "nodes": [
                {"id": "n1", "shape": "rounded", "label": 'Mode "A"'},
                {"id": "n2", "shape": "diamond", "label": "BUSY?"},
                {"id": "n3", "shape": "circle", "label": "end"},
            ],
            "groups": [{"title": "Init", "members": ["n1", "n2"]}, {"title": "empty", "members": []}],
            "edges": [
                {"source": "n1", "target": "n2", "label": "A | B", "dashed": False},
                {"source": "n2", "target": "n3", "label": "", "dashed": True},
            ],
            "legend": [("dashed", "optional")],
            "notes": ["see text"],
        }
        assert graph.mermaid(g).splitlines() == [
            "flowchart TD",
            "    n1(\"Mode 'A'\")",
            '    n2{"BUSY?"}',
            '    n3(("end"))',
            '    subgraph g1["Init"]',
            "        n1",
            "        n2",
            "    end",
            "    n1 -->|A / B| n2",
            "    n2 -.-> n3",
            "    %% legend: dashed arrow -.-> means optional",
            "    %% note: see text",
        ]

    def test_timing_patterns(self):
        """Interval parameters and level labels are never lane names"""
        for text in ("t_SU", "tsu(SI)", "td(SDCLKL_AddR)", "–t_h"):
            assert timing.PARAMETER.match(text), text
        for text in ("V_HSEH", "90 %", "0.3VDD"):
            assert timing.LEVEL.match(text), text
        assert not timing.PARAMETER.match("TIMx_CH1") and not timing.LEVEL.match("CLK")


class Figures(unittest.TestCase):
    """The extractors and their audits on hand-drawn figures"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def svg(self, text, name="figure.svg"):
        path = Path(self.tmp.name) / name
        path.write_text(text)
        return path

    def test_chart(self):
        """Axes from the tick labels, titles, two curves labelled by the legend, verified by the audit"""
        path = self.svg(_chart())
        plot = curves.extract(path)
        assert plot.x.scale == plot.y.scale == "linear"
        assert plot.x.range == (0, 100) and plot.y.range == (0, 4)
        assert plot.x.title == "Temperature (°C)" and plot.y.title == "Current"
        red, blue = plot.curves
        assert (red.label, red.color, blue.label) == ("VDD = 3.3 V", "#ff0000", "VDD = 1.8 V")
        assert abs(red(50) - 2) < 1e-9 and abs(blue(50) - 3) < 1e-9 and abs(red(90) - 3.6) < 1e-9
        audit = curves.audit(path)
        assert audit["coverage"] == 1.0 and audit["unexplained"] == 0 and audit["curves"] == 2, audit
        kind, text, verified = figure_data(path, "Current consumption vs. temperature", [50, 150])
        assert kind == "chart" and verified, text
        assert "x: Temperature (°C), linear, 0 to 100" in text
        assert "curve VDD = 3.3 V: y(50) = 2, y(150) outside " in text, text

    @pytest.mark.xfail(
        strict=True,
        reason="bug: figures._chart_text formats the curve range with :g, so the float noise of the axis fit shows as "
        "'y(150) outside -2.84217e-14..100' instead of '0..100'",
    )
    def test_chart_range_text(self):
        """The x range of a curve in the text of a value outside of it"""
        _, text, _ = figure_data(self.svg(_chart()), "Current consumption vs. temperature", [150])
        assert "curve VDD = 3.3 V: y(150) outside 0..100" in text, text

    def test_chart_log_axis(self):
        """Evenly spaced decades are a log axis, curves interpolate in log x"""
        path = self.svg(_chart(xlabels=("1", "10", "100", "1k", "10k")))
        plot = curves.extract(path)
        assert plot.x.scale == "log" and plot.x.range == (1, 10000)
        red = plot.curves[0]
        assert red.xlog and abs(red(100) - 2) < 1e-6 and abs(red(1000) - 3) < 1e-6

    def test_not_a_chart(self):
        """The caption excludes charts, and a drawing without lines is no figure data"""
        path = self.svg(_chart())
        assert (figure_data(path, "LQFP64 pinout") or ["none"])[0] != "chart"
        assert figure_data(self.svg(_svg(50, 50, [_text("PA0", 10, 10)]), "text.svg"), "Figure") is None

    def test_timing(self):
        """Clock, signal and bus lanes in cycles of the clock, an interval between edges"""
        result = timing.extract(self.svg(_timing()))
        assert result["clock"] == "CLK" and result["unit"] == "cycles of CLK, 0 = its first rising edge"
        lanes = {lane["name"]: lane for lane in result["lanes"]}
        assert lanes["CLK"]["kind"] == "clock"
        assert lanes["EN"]["kind"] == "digital" and lanes["EN"]["start"] == "0"
        assert lanes["EN"]["events"] == [(1.0, "1"), (3.0, "0")]
        assert lanes["DATA"]["kind"] == "bus" and lanes["DATA"]["events"][1:] == [(1.0, "D0"), (3.0, "-")]
        assert [list(i) for i in result["intervals"]] == [["t_SU", 1.0, 2.0]]
        # EN and DATA both change at the start of the interval
        assert [list(link) for link in result["links"]] == [["DATA transition", "CLK ↑"]]
        assert result["audit"]["ink"] == 1.0 and result["audit"]["agreement"] > 0.9, result["audit"]
        kind, text, verified = figure_data(self.svg(_timing(), "t.svg"), "SPI timing")
        assert kind == "timing" and verified, text
        assert (
            "EN: starts low, ↑1.0 ↓3.0" in text
            and "interval 't_SU': 1.0 to 2.0 (from DATA transition to CLK ↑)" in text
        )
        assert "at 1.0: DATA = D0, EN ↑" in text, text

    def test_state_machine(self):
        """Boxes are nodes, arrows edges with the closest text as label"""
        g = graph.extract(self.svg(_state_machine()))
        assert [(n["id"], n["shape"], n["label"]) for n in g["nodes"]] == [
            ("n1", "rectangle", "IDLE"),
            ("n2", "rectangle", "RUN"),
            ("n3", "rectangle", "ERROR"),
        ]
        assert [(e["source"], e["target"], e["label"]) for e in g["edges"]] == [
            ("n1", "n2", "start"),
            ("n2", "n1", "stop"),
            ("n2", "n3", "fault"),
        ]
        assert g["audit"]["dangling"] == 0 and g["audit"]["text"] == 1.0, g["audit"]
        kind, text, verified = figure_data(self.svg(_state_machine(), "g.svg"), "USART state machine")
        assert kind == "graph" and verified and "n2 -->|fault| n3" in text, text


if __name__ == "__main__":
    unittest.main()
