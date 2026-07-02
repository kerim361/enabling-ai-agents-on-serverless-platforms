#!/usr/bin/env python3
"""
Analysis & plotting for the thesis measurements.

Reads the raw CSVs in data/, computes aggregated statistics, and renders
thesis-ready PDF figures into figures/. Run after measure.py.

  ./venv/bin/python analyze.py
"""
import csv
import glob
import os
import statistics
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
FIG_DIR = os.path.join(HERE, "figures")
os.makedirs(FIG_DIR, exist_ok=True)

# TU red for accent, neutral grey for the baseline.
C_LAMBDA = "#C50E1F"
C_EC2 = "#444444"


def latest(pattern):
    files = sorted(glob.glob(os.path.join(DATA_DIR, pattern)))
    return files[-1] if files else None


def read_rows(path):
    if not path or not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": statistics.mean(vals),
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
    }


def fmt(s):
    return (f"n={s['n']}  mean={s['mean']:.3f}s  median={s['median']:.3f}s  "
            f"min={s['min']:.3f}s  max={s['max']:.3f}s  sd={s['stdev']:.3f}s") if s else "no data"


# --------------------------------------------------------------------------
# Experiment 1: latency lambda vs ec2
# --------------------------------------------------------------------------
def analyze_exp1():
    out = {}
    for sysname, color in (("lambda", C_LAMBDA), ("ec2", C_EC2)):
        rows = read_rows(latest(f"exp1_latency_{sysname}_*.csv"))
        lat = [fnum(r["latency_s"]) for r in rows]
        s = stats(lat)
        out[sysname] = (s, [v for v in lat if v is not None])
        print(f"[exp1] {sysname:7s}: {fmt(s)}")

    fig, ax = plt.subplots(figsize=(6, 4))
    data = [out["lambda"][1], out["ec2"][1]]
    bp = ax.boxplot(data, tick_labels=["Lambda\n(serverless)", "EC2\n(server)"],
                    patch_artist=True, widths=0.5)
    for patch, c in zip(bp["boxes"], (C_LAMBDA, C_EC2)):
        patch.set_facecolor(c); patch.set_alpha(0.6)
    for med in bp["medians"]:
        med.set_color("black")
    ax.set_ylabel("End-to-end latency per turn (s)")
    ax.set_title("Warm-state latency: serverless vs. server")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "exp1_latency.pdf"))
    plt.close(fig)
    return out


# --------------------------------------------------------------------------
# Experiment 2: cold vs warm (lambda)
# --------------------------------------------------------------------------
def analyze_exp2():
    rows = read_rows(latest("exp2_coldwarm_lambda_*.csv"))
    by_mode = defaultdict(list)
    for r in rows:
        v = fnum(r["latency_s"])
        if v is not None:
            by_mode[r["mode"]].append(v)
    res = {m: stats(v) for m, v in by_mode.items()}
    for m in ("warm", "cold"):
        print(f"[exp2] lambda {m:5s}: {fmt(res.get(m))}")

    if res.get("warm") and res.get("cold"):
        fig, ax = plt.subplots(figsize=(5, 4))
        modes = ["warm", "cold"]
        means = [res[m]["mean"] for m in modes]
        errs = [res[m]["stdev"] for m in modes]
        bars = ax.bar(modes, means, yerr=errs, capsize=6,
                      color=[C_EC2, C_LAMBDA], alpha=0.8, width=0.5)
        for b, mean in zip(bars, means):
            ax.text(b.get_x() + b.get_width()/2, mean, f"{mean:.2f}s",
                    ha="center", va="bottom")
        ax.set_ylabel("Latency per turn (s)")
        ax.set_title("Lambda cold-start vs. warm-start latency")
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(FIG_DIR, "exp2_coldwarm.pdf"))
        plt.close(fig)
    return res


# --------------------------------------------------------------------------
# Experiment 3: scaling with conversation length
# --------------------------------------------------------------------------
def analyze_exp3():
    out = {}
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for sysname, color, marker in (("lambda", C_LAMBDA, "o"), ("ec2", C_EC2, "s")):
        rows = read_rows(latest(f"exp3_scaling_{sysname}_*.csv"))
        by_len = defaultdict(list)
        for r in rows:
            v = fnum(r["latency_s"])
            mc = fnum(r["actual_message_count"]) or fnum(r["target_length"])
            if v is not None and mc is not None:
                by_len[int(mc)].append(v)
        xs = sorted(by_len)
        means = [statistics.mean(by_len[x]) for x in xs]
        out[sysname] = list(zip(xs, means))
        ax.plot(xs, means, marker=marker, color=color,
                label=f"{sysname} ({'serverless' if sysname=='lambda' else 'server'})")
        print(f"[exp3] {sysname:7s}: " + ", ".join(f"{x}msg={m:.3f}s" for x, m in zip(xs, means)))
    ax.set_xlabel("Conversation length (messages in history)")
    ax.set_ylabel("Mean latency per turn (s)")
    ax.set_title("Latency vs. conversation length")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "exp3_scaling.pdf"))
    plt.close(fig)
    return out


def analyze_exp4():
    """Compression: message_count over turns (sawtooth) for both systems."""
    fig, ax = plt.subplots(figsize=(6.5, 4))
    info = {}
    for sysname, color in (("lambda", C_LAMBDA), ("ec2", C_EC2)):
        rows = read_rows(latest(f"exp4_compression_{sysname}_*.csv"))
        if not rows:
            continue
        turns = [int(r["turn"]) for r in rows]
        counts = [int(r["message_count"]) for r in rows]
        ax.plot(turns, counts, color=color, marker=".",
                label=f"{sysname} ({'serverless' if sysname=='lambda' else 'server'})")
        drops = [(int(r["turn"]), int(r["count_drop"])) for r in rows if r["compressed_here"] == "1"]
        comp_lat = [float(r["latency_s"]) for r in rows if r["compressed_here"] == "1"]
        info[sysname] = {"drops": drops, "comp_latency": comp_lat}
        print(f"[exp4] {sysname:7s}: compression events {drops}, "
              f"compress-turn latency {comp_lat}")
    ax.axhline(150, ls="--", color="grey", alpha=0.6, label="threshold (150 msgs)")
    ax.set_xlabel("Conversation turn")
    ax.set_ylabel("Messages held in state")
    ax.set_title("State growth and compression event")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "exp4_compression.pdf"))
    plt.close(fig)
    return info


def analyze_exp5():
    """Token usage vs conversation length."""
    fig, ax = plt.subplots(figsize=(6.5, 4))
    out = {}
    for sysname, color, marker in (("lambda", C_LAMBDA, "o"), ("ec2", C_EC2, "s")):
        rows = read_rows(latest(f"exp5_cost_{sysname}_*.csv"))
        if not rows:
            continue
        xs = [int(r["actual_message_count"]) for r in rows]
        intok = [int(r["est_input_tokens"]) for r in rows]
        ax.plot(xs, intok, marker=marker, color=color,
                label=f"{sysname} input tokens")
        out[sysname] = list(zip(xs, intok))
        print(f"[exp5] {sysname:7s}: " + ", ".join(f"{x}msg={t}tok" for x, t in zip(xs, intok)))
    ax.set_xlabel("Conversation length (messages in history)")
    ax.set_ylabel("Estimated input tokens per turn")
    ax.set_title("Input token growth with conversation length")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "exp5_tokens.pdf"))
    plt.close(fig)
    return out


def main():
    print("=" * 64)
    e1 = analyze_exp1()
    print("-" * 64)
    e2 = analyze_exp2()
    print("-" * 64)
    e3 = analyze_exp3()
    print("-" * 64)
    e4 = analyze_exp4()
    print("-" * 64)
    e5 = analyze_exp5()
    print("=" * 64)
    print(f"Figures written to {FIG_DIR}")
    for f in sorted(glob.glob(os.path.join(FIG_DIR, "*.pdf"))):
        print("  -", os.path.basename(f))


if __name__ == "__main__":
    main()
