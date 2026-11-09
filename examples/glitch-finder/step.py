"""The design's example step (docs/analysis-python-steps.md §3.1): short pulses on digital lines."""

import numpy as np

from benchvista import Choice, Digital, Fields, Quantity, Series, Step, Table, Text, Tick

TRI_LOW, TRI_HIGH, TRI_UNKNOWN = 0, 1, 2


class GlitchFinder(Step):
    """Finds pulses shorter than a limit on digital lines.

    A pulse is a complete high run between two low ones (a rising edge's pulse) or a complete low
    run between two high ones (a falling edge's). Runs touching unknown samples, and the first and
    last run of a line, whose edges were not both observed, are never pulses.
    """

    id = "glitch-finder"
    version = "1.0.0"
    name = "Glitch finder"
    purity = "pure"
    category = "measure"
    inputs = {"lines": Series(fields=Digital(), axis=["uniform", "points"], min_fields=1)}
    outputs = {
        "glitches": Table(start=Tick(clock_of="lines"), width=Quantity.of("time", "s"), field="text"),
    }
    params = {
        "shortest": Quantity(
            "time",
            "s",
            default=1e-6,
            minimum=0,
            label="Shortest valid pulse",
            help=Text("Pulses shorter than this are listed as glitches.", technical=False),
        ),
        "fields": Fields(of="lines", type="tri", min=1, label="Lines to check"),
        "edge": Choice(
            {"rising": "High pulses", "falling": "Low pulses", "both": "Both"},
            default="both",
            label="Pulses",
        ),
    }
    translations = {
        "de": {"name": "Glitch-Sucher", "param.shortest.label": "Kürzester gültiger Puls"},
        "nb": {"name": "Glitch-søker", "param.shortest.label": "Korteste gyldige puls"},
    }

    def run(self, ctx, lines, params):
        per_tick = lines.clock.seconds_per_tick
        seconds_per_tick = per_tick.numerator / per_tick.denominator
        ticks = lines.ticks
        end = lines.end_tick
        starts, widths, names, order = [], [], [], []
        for index, field in enumerate(params.fields):
            values = np.asarray(lines.column(field), dtype=np.uint8).copy()
            valid = lines.valid(field)
            if valid is not None:
                values[valid == 0] = TRI_UNKNOWN
            if len(values) < 3:
                continue
            change = np.flatnonzero(values[1:] != values[:-1]) + 1
            run_starts = np.concatenate(([0], change))
            run_values = values[run_starts]
            run_ticks = ticks[run_starts]
            run_ends = np.append(ticks[change], end)
            previous, current, following = run_values[:-2], run_values[1:-1], run_values[2:]
            high = (current == TRI_HIGH) & (previous == TRI_LOW) & (following == TRI_LOW)
            low = (current == TRI_LOW) & (previous == TRI_HIGH) & (following == TRI_HIGH)
            pulses = high if params.edge == "rising" else low if params.edge == "falling" else high | low
            width_ticks = (run_ends - run_ticks)[1:-1]
            short = pulses & (width_ticks * seconds_per_tick < params.shortest)
            picked = np.flatnonzero(short)
            starts.append(run_ticks[1:-1][picked])
            widths.append(width_ticks[picked] * seconds_per_tick)
            names.extend([field] * len(picked))
            order.append(np.full(len(picked), index))
            ctx.progress((index + 1) / len(params.fields))
        start = np.concatenate(starts) if starts else np.empty(0, dtype=np.int64)
        width = np.concatenate(widths) if widths else np.empty(0)
        rank = np.concatenate(order) if order else np.empty(0, dtype=np.int64)
        # In time order, lines in their chosen order at the same tick: the same data, the same table.
        sort = np.lexsort((rank, start))
        ctx.table(
            "glitches",
            start=start[sort].astype(np.int64),
            width=width[sort],
            field=[names[i] for i in sort],
        )
