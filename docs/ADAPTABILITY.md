# Adaptability — why the same pipeline works beyond sensor data

The challenge asks that rule extraction, drift detection and root-cause logic be portable "from the start, not
added afterward". This document is the architectural walkthrough: which stages are generic, which adapters change,
and how two very different failures — a drifting sensor and a corrupted batch of manually entered records — flow
through the identical code. The team decided (DECISIONS #47–48) not to tune for extra domains; a synthetic
records fixture (`samples/demo_records.csv`) exists to show schema-agnostic ingestion, and nothing in the pipeline
contains domain logic.

## 1. The invariant: a table of aliased columns with structural roles

Every input becomes the same object: `dataset.parquet` with columns addressed as `S01 … Snn`, a `DatasetSchema`
describing time, ordering, grouping and excluded columns, and a **signal catalog** (`signals.json`) holding, per
column, a fingerprint of aggregates and a *structural* role from a domain-free vocabulary:

`continuous_measured · actuator_like · held_sampled · constant · derived_redundant · counter · timestamp ·
categorical · text · identifier · unknown`

Nothing downstream knows whether `S07` is a reactor pressure or an invoice amount. Instrument and unit-operation
names ("pressure", "separator") exist only as **hypotheses** with their own confidence, gated by a data-driven
domain likelihood (`schema.domain_likelihood`: sensor stream vs. business records vs. log records), and they are
never inputs to detection.

## 2. Stage by stage: generic vs. adapter

| Stage | Generic (unchanged across domains) | Adapter (what differs) |
|---|---|---|
| **ingest** | format sniffing, header detection, transposition detection, chunked Parquet conversion, blind aliases, time / counter inference, grouping strategies scored against each other, label / meta auto-detection | the *reader* (CSV, whitespace `.dat`, Parquet, Excel, JSON/JSONL); nothing else |
| **profile** | fingerprints (distribution, noise, autocorrelation, stuck fraction, quantisation, dominant period), lagged cross-correlation, clustering, structural role rules | the weight given to `categorical` / `text` columns (records have many; sensor streams few) and the hypothesis vocabulary (a hypothesis layer only) |
| **quality** | completeness / validity / consistency / timeliness checks per batch, trust verdict, the closed rule vocabulary (range, rate of change, second-order change, duration, cross-signal, rolling statistics, missing / stale) | none: "amount must be positive" and "S03 must stay between 100 and 140" compile to the same `range` spec |
| **detect** | baseline regime selection, detectors on numeric columns (PCA T²/SPE, robust z / EWMA / CUSUM, correlation-structure break, isolation forest, autoencoder), GroupKFold out-of-fold scoring, threshold calibration, per-signal attribution, change points, pattern clustering, cascade ordering | how categorical / text columns enter: as derived numeric features (frequency, rarity, entropy per batch, embedding distance to the batch centroid) computed by the same profile stage |
| **diagnose** | diagnosis assembly from flags + relations + patterns, sensor-vs-process discrimination, template narrative, critique | vocabulary of the template sentences ("signal" vs. "field") — a translation table, not logic |
| **assess / report / log / llm** | identical | identical |

The batch abstraction is the second invariant: a 5-minute window when a timestamp exists, else 10 % of the rows.
A sensor stream, a day of order entries and an hour of log lines are all "batches of rows in time order within a
group", and every check, score and flag is expressed on that grid.

## 3. Same pipeline, two failures

### A. A drifting sensor (industrial stream)

1. **ingest**: 52 numeric columns, no header, block-constant column 0 → groups; column 1 resets → order; 3-minute
   period inferred from a timestamp column (else sample units).
2. **profile**: `S07` is `continuous_measured` (autocorrelation 0.97, noise level 0.12); it leads `S12` by 2 samples
   (r = 0.81) and `S15` by 4; they form cluster C1.
3. **quality**: batch B0041 passes completeness / validity; nothing stuck. Trust 0.98.
4. **detect**: from batch B0044 the out-of-fold score rises slowly above the threshold (a `drift` flag, gradual
   change point at row 8 810). Attribution: `S07` 48 %, `S12` 31 %, `S15` 21 %, all direction *up*, lags 0 / 2 / 4.
5. **diagnose**: several correlated signals move together in lag order → cause class **process**; propagation
   `S07 → S12 → S15`; pattern `PATTERN-A` (three earlier events with the same signature). Critique checks that
   the lag order matches `relations.json` and that the batch was trusted: *supported*, confidence 0.72.

### B. A corrupted batch of records (manually entered orders)

`samples/demo_records.csv`: order id, timestamp, customer id, region, category, quantity, unit price, discount,
amount, operator, status — with injected errors (amounts typed in cents for one operator during one afternoon,
a price table that drifts 25 % for one category, a block of missing customer ids, negative quantities, out-of-range
discounts, duplicated rows, a batch entered two days late, a copy-pasted constant discount).

1. **ingest**: header present but treated as weak evidence; `order_id` → `identifier` (unique, monotone prefix),
   `created_at` → `timestamp` (irregular, so batches are 5-minute windows of *entry* time), no block-constant key →
   grouping by change-point segmentation or none; `status` / `region` / `category` / `entered_by` → `categorical`.
2. **profile**: `amount ≈ quantity × unit_price × (1 − discount/100)` → `derived_redundant` with r = 0.99 at lag 0;
   `discount_pct` has a stuck fraction 0.3 in one segment; categorical columns get frequency features.
3. **quality**: completeness fails on the missing-id block; validity fails on negative quantities and discounts
   > 100 (range checks the rule compiler produces from "discount must be between 0 and 100"); consistency flags the
   duplicated rows and the frozen discount block; timeliness flags the out-of-order timestamps. The trust verdict
   marks the late-entered batch as "cannot be trusted" — exactly as it would mark a sensor batch with a dead channel.
4. **detect**: the cents-instead-of-euros afternoon is an abrupt `anomaly` flag: `amount` breaks its relation to
   `quantity × unit_price` (correlation-structure break) while its partners are unchanged. The price-table drift is
   a gradual `drift` flag attributed to `unit_price` and `amount` for one category (a group-level change point).
5. **diagnose**: one column breaking its relations with everything else → cause class **data / sensor**
   ("one field changed its behaviour; its partners did not") for the unit-shift event; two related columns moving
   together → **process** ("the pricing input changed") for the drift. The step-by-step explanation uses the same
   template: what was compared, from which row the score rose, which fields explain it, what earlier events match.

The operator experience is the same: the flag names the responsible columns, the diagnosis separates "broken data"
from "broken process", and every claim links to evidence IDs.

### C. Log / free-text records (third domain - implemented and run)

`samples/demo_log.csv` (made by `samples/make_demo_log.py`, truth in `samples/demo_log_truth.json`) is a web-service
event log: timestamp, service, level, status code, latency, bytes, user id and a free-text message, with four planted
problems (a database incident, a logging bug that writes latency 0, 100 rows written twice, a slow memory leak).

The only adapter is `tpm/ingest/events_adapter.py`. On event-like tables (the domain estimate says event log, or not a
sensor stream and there is a time column) it turns what logs are made of into ordinary numeric signals:

- for every per-row category (2-12 values that change from row to row, e.g. `level`, `service`): the share of each of
  its rarest values over the last 50 rows (`share of level = ERROR in the last 50 rows`);
- for every free-text column: the length of the text.

Nothing depends on column names; sensor tables are untouched (tested). From there the pipeline is unchanged. Result of
`python -m tpm run samples/demo_log.csv`:

| Planted | Found |
|---|---|
| database incident (30 % errors, db/api 4x slower), rows 8000-8599 | drift, rows 8014-9043, led by *share of level = ERROR* rising and *share of INFO* falling; cause: process |
| logging bug (latency written as 0), rows 13000-13299 | data problem on latency ("repeats the same value"), rows 12998-13404; cause: data |
| 100 duplicated rows, 13200-13299 | inside the same data finding (duplicate-row check) |
| slow worker memory leak, rows 16000-17499 | not found: only 10 % of the rows are worker jobs, so the latency mix hides it (a per-service latency signal would need the grouping below) |

Also exposed and fixed by this domain and by the business records of section B: whole numbers in a narrow band that
repeat without order (customer id, user id, status code) are identifiers or codes, not measurements; diagnoses on
records and logs use data words ("entry problem in a column", "repeats the same value") instead of sensor words.

## 4. What stays fixed by design

- **Evidence first**: every claim cites `EV-…` records computed by code, in every domain.
- **Template first**: narratives exist without any model; a model only adds hypotheses and prose on top.
- **Labels never reach detection**: label-like columns are auto-detected and used for evaluation only.
- **Egress guard**: the same payload rules protect an order table's customer ids as they protect sensor rows
  (categorical / free-text values are blocked from leaving).
- **Rules are a closed vocabulary**, never code, so "amount must be positive" is as safe as "S03 in [100, 140]".

## 5. Try it

```
python -m tpm run samples/demo_records.csv --lang en
python -m tpm run samples/demo_headerless.dat          # blind mode, no header at all
python -m tpm run samples/demo_process_labeled.csv     # label column auto-detected -> evaluation section
```

Open the runs in the UI: the same views, the same evidence trail, the same report sections.
