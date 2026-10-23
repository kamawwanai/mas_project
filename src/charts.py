"""Dependency-free, labeled SVG bar charts for descriptive local reports."""
from html import escape
import math


def bar_chart(values, metric, mode, evaluation_mode="strict"):
    width, height, top, bottom = 680, 380, 65, 300
    finite = [(str(k), v) for k, v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    maximum = max([float(v) for _, v in finite]+[1e-12])
    label = (("REAL MODEL / SYNTHETIC CONDITIONS" if evaluation_mode == "benchmark_assumptions" else "REAL")
             if mode == "real" else "MOCK / SYNTHETIC — NOT EXPERIMENTAL RESULTS")
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
                f'<title>{escape(metric)} — {escape(label)}</title>',
                '<rect width="100%" height="100%" fill="white"/>',
                f'<text x="20" y="24" font-family="sans-serif" font-size="14">{escape(label)}</text>',
                f'<text x="20" y="48" font-family="sans-serif">{escape(metric)} (descriptive means)</text>',
                f'<path d="M 55 {top} V {bottom} H 660" stroke="black" fill="none"/>']
    step = 580/max(1, len(finite))
    for i, (name, value) in enumerate(finite):
        x = 65+i*step
        h = max(0, value)/maximum*(bottom-top-20)
        elements.extend([f'<rect x="{x:.2f}" y="{bottom-h:.2f}" width="{step*.65:.2f}" height="{h:.2f}" fill="#2962a3"/>',
                         f'<text x="{x:.2f}" y="{bottom-h-6:.2f}" font-family="sans-serif" font-size="12">{value:.5g}</text>',
                         f'<text x="{x:.2f}" y="325" font-family="sans-serif" font-size="12">{escape(name)}</text>'])
    return "\n".join(elements+["</svg>", ""])
