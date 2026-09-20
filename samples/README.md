# Sample data

Small synthetic files, made by `scripts/make_samples.py`. Nothing here comes from a real plant.

| File | What it is | Used by |
|---|---|---|
| `demo_process.csv` | 20 runs x 400 rows, 12 sensors, six fault types and six data-quality defects, no labels | `python -m tpm demo`, the app's "Create a demo run" |
| `demo_process_labeled.csv` | the same data with its fault labels, for checking the evaluation | [docs/EVALUATION.md](../docs/EVALUATION.md) |
| `demo_headerless.dat` | the same shape without a header row, whitespace separated | headerless / transposed detection |
| `demo_records.csv` | business records (orders) with manual-entry errors: the second domain | [docs/ADAPTABILITY.md](../docs/ADAPTABILITY.md) |
| `demo_log.csv` + `demo_log_truth.json` | a web-service event log with free text: the third domain (`scripts/make_demo_log.py`) | [docs/ADAPTABILITY.md](../docs/ADAPTABILITY.md) |

Larger robustness sets (uneven batches, row labels, a wide labelled file, European decimals, JSONL) are not kept in
the repository because they are generated: `python scripts/make_extra_samples.py` writes them into `samples/` with a
truth file, and `python scripts/make_samples.py` writes the files above.
