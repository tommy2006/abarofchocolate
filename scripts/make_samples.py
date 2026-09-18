"""Generate the small synthetic demo files in samples/ (each < 3 MB).

    python scripts/make_samples.py [--force] [--out samples]

Files:
  demo_process.csv          20 groups x 400 rows, header, timestamps, injected faults + DQ issues (no labels,
                            like the assessment data)
  demo_process_labeled.csv  same generator with a `fault_label` column: shows label auto-detection + evaluation
  demo_headerless.dat       whitespace-separated, no header (8 groups x 300 rows, numeric only)
  demo_records.csv          messy business-records-like table (orders) with injected manual-entry errors;
                            used only to show schema-agnostic ingestion (no domain logic anywhere)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.synth import make_synthetic  # noqa: E402


def make_records(n: int = 3000, seed: int = 11) -> pd.DataFrame:
    """Synthetic order records with realistic manual-entry problems. Purely generic: ids, timestamps, amounts,
    quantities, categoricals. Ground truth of the injected problems is returned in df.attrs['truth']."""
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-03-01 08:00:00")
    gaps = rng.exponential(scale=600, size=n).astype(int)  # seconds between entries
    ts = t0 + pd.to_timedelta(np.cumsum(gaps), unit="s")
    categories = np.array(["A", "B", "C", "D"])
    base_price = {"A": 12.5, "B": 48.0, "C": 3.2, "D": 120.0}
    cat = rng.choice(categories, size=n, p=[0.4, 0.3, 0.2, 0.1])
    qty = rng.integers(1, 40, size=n)
    unit_price = np.array([base_price[c] for c in cat]) * rng.normal(1.0, 0.03, n)
    operators = rng.choice(["op1", "op2", "op3", "op4", "op5", "op6"], size=n)
    region = rng.choice(["north", "south", "east", "west", "central"], size=n)
    customer = rng.integers(1000, 1040, size=n)
    discount = np.clip(rng.normal(5, 3, n), 0, 20).round(1)
    amount = (qty * unit_price * (1 - discount / 100)).round(2)
    status = rng.choice(["new", "paid", "shipped", "closed"], size=n, p=[0.2, 0.3, 0.3, 0.2])
    df = pd.DataFrame({
        "order_id": [f"ORD-{i + 1:06d}" for i in range(n)],
        "created_at": ts,
        "customer_id": customer,
        "region": region,
        "category": cat,
        "quantity": qty,
        "unit_price": unit_price.round(2),
        "discount_pct": discount,
        "amount": amount,
        "entered_by": operators,
        "status": status,
    })
    truth = []
    # 1) one operator enters amounts in cents for a period (unit shift x100)
    m = (df["entered_by"] == "op3") & (df.index > int(0.55 * n)) & (df.index < int(0.70 * n))
    df.loc[m, "amount"] = df.loc[m, "amount"] * 100
    truth.append({"type": "unit_shift", "column": "amount", "rows": int(m.sum()), "row_start": int(0.55 * n), "row_end": int(0.70 * n)})
    # 2) gradual drift: category B unit price creeps up 25 % over the last third (a pricing-table corruption)
    m = (df["category"] == "B") & (df.index > int(0.66 * n))
    ramp = (df.index[m] - int(0.66 * n)) / (n - int(0.66 * n))
    df.loc[m, "unit_price"] = (df.loc[m, "unit_price"] * (1 + 0.25 * ramp)).round(2)
    df.loc[m, "amount"] = (df.loc[m, "quantity"] * df.loc[m, "unit_price"] * (1 - df.loc[m, "discount_pct"] / 100)).round(2)
    truth.append({"type": "gradual_drift", "column": "unit_price", "row_start": int(0.66 * n), "row_end": n - 1})
    # 3) missing customer ids in a block
    r = int(0.20 * n)
    df.loc[r : r + 40, "customer_id"] = np.nan
    truth.append({"type": "missing_block", "column": "customer_id", "row_start": r, "row_end": r + 40})
    # 4) negative quantities (typos)
    idx = rng.choice(n, size=12, replace=False)
    df.loc[idx, "quantity"] = -df.loc[idx, "quantity"]
    truth.append({"type": "out_of_range", "column": "quantity", "rows": 12})
    # 5) discount out of range
    idx = rng.choice(n, size=6, replace=False)
    df.loc[idx, "discount_pct"] = rng.uniform(150, 900, size=6).round(1)
    truth.append({"type": "out_of_range", "column": "discount_pct", "rows": 6})
    # 6) duplicated rows
    r = int(0.80 * n)
    dup = df.iloc[r : r + 8].copy()
    df = pd.concat([df.iloc[: r + 8], dup, df.iloc[r + 8 :]], ignore_index=True)
    truth.append({"type": "duplicate_rows", "row_start": r + 8, "row_end": r + 15})
    # 7) timestamps out of order in a block (batch entered late)
    r = int(0.40 * n)
    df.loc[r : r + 25, "created_at"] = df.loc[r : r + 25, "created_at"] - pd.Timedelta(days=2)
    truth.append({"type": "timestamp_disorder", "column": "created_at", "row_start": r, "row_end": r + 25})
    # 8) stuck value: a field frozen by a copy-paste habit
    r = int(0.30 * n)
    df.loc[r : r + 60, "discount_pct"] = 7.5
    truth.append({"type": "frozen_block", "column": "discount_pct", "row_start": r, "row_end": r + 60})
    df.attrs["truth"] = truth
    return df


def make_all(out_dir: str | Path, force: bool = False) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    p = out / "demo_process.csv"
    if force or not p.exists():
        df, _ = make_synthetic(n_groups=20, n_samples=400, seed=42, with_timestamp=True, with_labels=False)
        df.to_csv(p, index=False, float_format="%.4f")
    paths["process"] = p

    p = out / "demo_process_labeled.csv"
    if force or not p.exists():
        df, _ = make_synthetic(n_groups=12, n_samples=300, seed=43, with_timestamp=True, with_labels=True)
        df.to_csv(p, index=False, float_format="%.4f")
    paths["process_labeled"] = p

    p = out / "demo_headerless.dat"
    if force or not p.exists():
        df, _ = make_synthetic(n_groups=8, n_samples=300, seed=7, header=False, with_timestamp=False)
        num = df.select_dtypes(include=[np.number])
        num.to_csv(p, index=False, header=False, sep=" ", float_format="%.5g")
    paths["headerless"] = p

    p = out / "demo_records.csv"
    if force or not p.exists():
        make_records().to_csv(p, index=False, date_format="%Y-%m-%d %H:%M:%S")
    paths["records"] = p

    readme = out / "README.md"
    if force or not readme.exists():
        readme.write_text(
            "# Sample data (synthetic)\n\n"
            "Generated by `python scripts/make_samples.py`. Nothing here is real data.\n\n"
            "| file | what | why |\n|---|---|---|\n"
            "| demo_process.csv | 20 groups x 400 rows, header, timestamps, injected faults (step, ramp, stuck sensor, correlation break, oscillation, noise burst) and DQ issues (missing block, spike, frozen block, unit shift, duplicates, timestamp gap); no labels | the default demo (`python -m tpm demo`) |\n"
            "| demo_process_labeled.csv | 12 groups x 300 rows with a `fault_label` column | shows label auto-detection, exclusion from detection, and the evaluation section |\n"
            "| demo_headerless.dat | whitespace-separated, no header, numeric only | shows blind-mode ingestion (aliases S01..Snn) |\n"
            "| demo_records.csv | order-like business records with manual-entry errors (cents-instead-of-euros, price-table drift, missing ids, negative quantities, duplicates, late-entered batch, copy-paste frozen field) | shows the same pipeline on non-sensor data (docs/ADAPTABILITY.md) |\n",
            encoding="utf-8",
        )
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "samples"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    for k, p in make_all(a.out, force=a.force).items():
        print(f"{k:<16} {p}  ({p.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
