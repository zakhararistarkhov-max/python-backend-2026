import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from isolation_lab.journal import Journal


class TimelineVisualizer:
    """Renders per-scenario two-lane transaction timelines from a journal."""

    LANES = {"T1": 1.0, "T2": 0.0}
    COLORS = {"T1": "#2a6fdb", "T2": "#d9480f"}
    LEVELS = (0.25, 0.6)

    def __init__(self, events):
        self.events = events

    @classmethod
    def from_file(cls, path):
        return cls(Journal.load(path))

    def scenarios(self):
        names = []
        for event in self.events:
            if event["scenario"] not in names:
                names.append(event["scenario"])
        return names

    def events_of(self, scenario):
        return [e for e in self.events if e["scenario"] == scenario]

    def title(self, scenario):
        events = self.events_of(scenario)
        outcome = next((e["value"] for e in events if e["step"] == "outcome"), "no outcome")
        isolation = next((e["isolation"] for e in events if e["isolation"]), "unknown isolation")
        return f"{scenario}: {isolation} - {outcome}"

    def render(self, out_dir):
        names = self.scenarios()
        if not names:
            raise ValueError("journal contains no scenarios")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        return [self.render_one(name, out_dir / f"{name}.png") for name in names]

    def render_one(self, scenario, path):
        steps = [e for e in self.events_of(scenario) if e["tx"] in self.LANES]
        times = [e["t_ms"] for e in steps] or [0.0]
        start, end = min(times), max(times)
        pad = max((end - start) * 0.06, 0.05)
        left, right = start - pad, end + pad
        slot = (right - left) / max(len(steps), 1)
        fig, (ax, notes) = plt.subplots(
            2, 1, figsize=(13, 6.6), gridspec_kw={"height_ratios": [3, 1.3]}
        )
        for tx, y in self.LANES.items():
            ax.axhline(y, color=self.COLORS[tx], alpha=0.35, linewidth=6, zorder=0)
        lines = []
        for number, event in enumerate(steps, start=1):
            tx = event["tx"]
            y = self.LANES[tx]
            direction = 1 if tx == "T1" else -1
            level = self.LEVELS[(number - 1) % 2] * direction
            label = f"#{number} {event['step']}"
            if event["value"] is not None:
                label += f"\n= {event['value']}"
            ax.plot(event["t_ms"], y, "o", color=self.COLORS[tx], markersize=9, zorder=3)
            ax.annotate(
                label,
                xy=(event["t_ms"], y),
                xytext=(left + slot * (number - 0.5), y + level),
                ha="center",
                va="bottom" if direction > 0 else "top",
                fontsize=8,
                arrowprops={"arrowstyle": "-", "color": "#888888", "linewidth": 0.7},
                bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": self.COLORS[tx], "lw": 0.8},
            )
            value = "" if event["value"] is None else f"  ->  {event['value']}"
            lines.append(f"#{number:<2} {event['t_ms']:>9.3f} ms  {tx}  {event['step']}: {event['sql'] or ''}{value}")
        ax.set_yticks(list(self.LANES.values()), [f"{tx}\n{self.isolation_of(steps, tx)}" for tx in self.LANES])
        ax.set_ylim(-1.0, 2.0)
        ax.set_xlim(left, right)
        ax.set_xlabel("time since scenario start, ms (monotonic); markers at real time, labels in event order")
        ax.grid(axis="x", alpha=0.3)
        ax.set_title(self.title(scenario), fontsize=11, fontweight="bold")
        notes.axis("off")
        notes.text(0, 1, "\n".join(lines), va="top", ha="left", family="monospace", fontsize=8)
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        return path

    @staticmethod
    def isolation_of(steps, tx):
        return next((e["isolation"] for e in steps if e["tx"] == tx and e["isolation"]), "")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render transaction timelines from a JSONL journal.")
    parser.add_argument("journal")
    parser.add_argument("out_dir")
    args = parser.parse_args(argv)
    for path in TimelineVisualizer.from_file(args.journal).render(args.out_dir):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
