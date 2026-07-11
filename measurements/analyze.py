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
        # Server-side phase timings from the instrumented backends:
        # quantifies the state-store overhead as a measurement, not an assumption.
        for phase in ("load_ms", "llm_ms", "store_ms"):
            vals = [fnum(r.get(phase)) for r in rows]
            ps = stats(vals)
            if ps:
                print(f"        {phase:9s}: mean={ps['mean']:.2f}ms  "
                      f"median={ps['median']:.2f}ms  min={ps['min']:.2f}ms  "
                      f"max={ps['max']:.2f}ms")
        toks = [fnum(r.get("prompt_tokens")) for r in rows]
        ts = stats(toks)
        if ts:
            print(f"        prompt_tokens: mean={ts['mean']:.0f}")

    # 1-GB memory variant of the Lambda function, if measured
    rows1g = read_rows(latest("exp1_latency_lambda1gb_*.csv"))
    if rows1g:
        lat1g = [fnum(r["latency_s"]) for r in rows1g]
        s1g = stats(lat1g)
        out["lambda1gb"] = (s1g, [v for v in lat1g if v is not None])
        print(f"[exp1] lambda@1GB: {fmt(s1g)}")

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

    # EC2 warm reference from the same experiment, if measured
    rows_ec2 = read_rows(latest("exp2_coldwarm_ec2_*.csv"))
    ec2_warm = stats([fnum(r["latency_s"]) for r in rows_ec2 if r["mode"] == "warm"])
    if ec2_warm:
        print(f"[exp2] ec2 warm  : {fmt(ec2_warm)}")

    # 1-GB memory variant, if measured
    rows1g = read_rows(latest("exp2_coldwarm_lambda1gb_*.csv"))
    if rows1g:
        by1g = defaultdict(list)
        for r in rows1g:
            v = fnum(r["latency_s"])
            if v is not None:
                by1g[r["mode"]].append(v)
        for m in ("warm", "cold"):
            s1 = stats(by1g.get(m, []))
            if s1:
                print(f"[exp2] lambda@1GB {m:5s}: {fmt(s1)}")

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
    # The two curves are nearly identical by construction (both systems
    # compress deterministically at the same threshold), so the lambda curve
    # is drawn dashed and slightly thicker underneath the ec2 markers to keep
    # BOTH visible instead of one hiding the other.
    styles = {"lambda": dict(color=C_LAMBDA, ls="--", lw=2.2, marker="o",
                             markersize=4, markevery=3, alpha=0.9, zorder=3),
              "ec2": dict(color=C_EC2, ls="-", lw=1.0, marker=".",
                          markersize=4, alpha=0.9, zorder=2)}
    for sysname in ("lambda", "ec2"):
        rows = read_rows(latest(f"exp4_compression_{sysname}_*.csv"))
        if not rows:
            continue
        turns = [int(r["turn"]) for r in rows]
        counts = [int(r["message_count"]) for r in rows]
        ax.plot(turns, counts,
                label=f"{sysname} ({'serverless' if sysname=='lambda' else 'server'})",
                **styles[sysname])
        drops = [(int(r["turn"]), int(r["count_drop"])) for r in rows if r["compressed_here"] == "1"]
        comp_lat = [float(r["latency_s"]) for r in rows if r["compressed_here"] == "1"]
        comp_tok = [(r.get("compression_prompt_tokens"), r.get("compression_completion_tokens"))
                    for r in rows if r["compressed_here"] == "1"]
        info[sysname] = {"drops": drops, "comp_latency": comp_lat,
                         "comp_tokens": comp_tok}
        print(f"[exp4] {sysname:7s}: compression events {drops}, "
              f"compress-turn latency {comp_lat}, "
              f"summarization tokens (in/out) {comp_tok}")
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
    """Token usage vs conversation length.

    Uses the *provider-reported* token counts (usage.prompt_tokens), averaged
    over the independent repetitions, with min-max bands showing the spread of
    the model-generated conversation content. Also validates the chars/4
    estimation heuristic against the exact counts.
    """
    fig, ax = plt.subplots(figsize=(6.5, 4))
    out = {}
    for sysname, color, marker in (("lambda", C_LAMBDA, "o"), ("ec2", C_EC2, "s")):
        rows = read_rows(latest(f"exp5_cost_{sysname}_*.csv"))
        if not rows:
            continue
        by_cp = defaultdict(lambda: {"real": [], "est": []})
        for r in rows:
            cp = int(r["checkpoint_msgs"])
            real = fnum(r.get("real_prompt_tokens"))
            est = fnum(r.get("est_input_tokens"))
            if real is not None:
                by_cp[cp]["real"].append(real)
            if est is not None:
                by_cp[cp]["est"].append(est)
        xs = sorted(x for x in by_cp if by_cp[x]["real"])
        if not xs:
            print(f"[exp5] {sysname:7s}: no rows with provider-reported tokens "
                  f"(old CSV format?) -- skipped")
            continue
        means = [statistics.mean(by_cp[x]["real"]) for x in xs]
        lows = [min(by_cp[x]["real"]) for x in xs]
        highs = [max(by_cp[x]["real"]) for x in xs]
        n_reps = max(len(by_cp[x]["real"]) for x in xs)
        ax.plot(xs, means, marker=marker, color=color,
                label=f"{sysname} (mean of {n_reps} runs)")
        ax.fill_between(xs, lows, highs, color=color, alpha=0.15,
                        label=f"{sysname} min–max")
        out[sysname] = list(zip(xs, means))
        print(f"[exp5] {sysname:7s} real prompt tokens (mean over {n_reps} reps): "
              + ", ".join(f"{x}msg={m:.0f}" for x, m in zip(xs, means)))
        # Heuristic validation: chars/4 vs provider-reported counts
        ratios = []
        for x in xs:
            if by_cp[x]["est"] and by_cp[x]["real"]:
                ratios.append(statistics.mean(by_cp[x]["est"]) /
                              statistics.mean(by_cp[x]["real"]))
        if ratios:
            print(f"        chars/4 estimate vs real: "
                  f"ratio mean={statistics.mean(ratios):.2f} "
                  f"(range {min(ratios):.2f}–{max(ratios):.2f})")
    ax.set_xlabel("Conversation length (messages in history)")
    ax.set_ylabel("Prompt tokens per turn (provider-reported)")
    ax.set_title("Input token growth with conversation length")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "exp5_tokens.pdf"))
    plt.close(fig)
    return out


def analyze_exp6():
    """Concurrent load: latency percentiles and throughput per concurrency level."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.5, 4))
    out = {}
    for sysname, color, marker in (("lambda", C_LAMBDA, "o"), ("ec2", C_EC2, "s")):
        rows = read_rows(latest(f"exp6_concurrency_{sysname}_*.csv"))
        if not rows:
            continue
        by_c = defaultdict(list)
        errs = defaultdict(int)
        totals = defaultdict(int)
        for r in rows:
            c = int(r["concurrency"])
            totals[c] += 1
            v = fnum(r["latency_s"])
            if v is not None and not r["error"]:
                by_c[c].append(v)
            else:
                errs[c] += 1
        levels = sorted(totals)
        p50s, p95s, thr = [], [], []
        for c in levels:
            lat = sorted(by_c[c])
            p50 = statistics.median(lat) if lat else float("nan")
            p95 = lat[min(len(lat) - 1, max(0, round(0.95 * len(lat)) - 1))] if lat else float("nan")
            p50s.append(p50)
            p95s.append(p95)
            # per-request wall estimate: turns per client are sequential, so
            # level wall-clock ~ sum of one client's latencies; throughput is
            # completed requests / wall. Approximate wall as (total/c)*mean.
            mean = statistics.mean(lat) if lat else float("nan")
            wall = (totals[c] / c) * mean if lat else float("nan")
            thr.append(len(lat) / wall if wall and wall == wall else float("nan"))
            print(f"[exp6] {sysname:7s} c={c:3d}: n={totals[c]:3d} errors={errs[c]} "
                  f"p50={p50:.3f}s p95={p95:.3f}s mean={mean:.3f}s "
                  f"~throughput={thr[-1]:.2f} req/s")
        out[sysname] = {"levels": levels, "p50": p50s, "p95": p95s,
                        "throughput": thr, "errors": dict(errs),
                        "totals": dict(totals)}
        ax1.plot(levels, p50s, marker=marker, color=color, label=f"{sysname} p50")
        ax1.plot(levels, p95s, marker=marker, color=color, ls="--", alpha=0.6,
                 label=f"{sysname} p95")
        ax2.plot(levels, thr, marker=marker, color=color, label=sysname)
    ax1.set_xlabel("Concurrent clients")
    ax1.set_ylabel("Latency per turn (s)")
    ax1.set_title("Latency under concurrent load")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)
    ax2.set_xlabel("Concurrent clients")
    ax2.set_ylabel("Completed requests / s")
    ax2.set_title("Throughput under concurrent load")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "exp6_concurrency.pdf"))
    plt.close(fig)
    return out


def analyze_lambda_reports():
    """Parse CloudWatch REPORT lines: billed duration, memory, init duration.

    This provides the measured basis for the cost table (billed GB-seconds)
    and the evidence that the cold-start init phase is not billed separately
    on this deployment type.
    """
    import re
    path = latest("lambda_report_*.log")
    if not path:
        print("[report] no lambda_report_*.log found")
        return None
    text = open(path).read()
    pat = re.compile(
        r"Billed Duration: (\d+) ms\s+Memory Size: (\d+) MB\s+"
        r"Max Memory Used: (\d+) MB(?:\s+Init Duration: ([\d.]+) ms)?")
    by_mem = defaultdict(lambda: {"billed": [], "used": [], "init": []})
    for m in pat.finditer(text):
        billed, memsize, used, init = m.groups()
        d = by_mem[int(memsize)]
        d["billed"].append(int(billed))
        d["used"].append(int(used))
        if init:
            d["init"].append(float(init))
    for mem, d in sorted(by_mem.items()):
        bs = stats([float(x) for x in d["billed"]])
        us = stats([float(x) for x in d["used"]])
        print(f"[report] {mem}MB: n={bs['n']} billed-duration "
              f"mean={bs['mean']:.0f}ms median={bs['median']:.0f}ms "
              f"max={bs['max']:.0f}ms | max-memory-used mean={us['mean']:.0f}MB "
              f"| cold inits recorded={len(d['init'])}"
              + (f" (init mean={statistics.mean(d['init']):.0f}ms, "
                 f"NOT included in billed duration)" if d["init"] else ""))
    return dict(by_mem)


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
    print("-" * 64)
    e6 = analyze_exp6()
    print("-" * 64)
    analyze_lambda_reports()
    print("=" * 64)
    print(f"Figures written to {FIG_DIR}")
    for f in sorted(glob.glob(os.path.join(FIG_DIR, "*.pdf"))):
        print("  -", os.path.basename(f))


if __name__ == "__main__":
    main()
