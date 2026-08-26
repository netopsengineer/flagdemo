# Results directory

Each run of `./run_experiment.sh` writes a timestamped set of files here:

```text
<run-id>.jsonl             raw rows, one JSON object per measurement
<run-id>.md                aggregated report
<run-id>.metadata.json     runtime, host, and source metadata
<run-id>.micro.json        isolated per-evaluation batch costs
<run-id>.validation.json   matrix, semantic, and generator-health validation
```

## What counts as evidence

A row is usable only when both of the following hold:

- the row itself has `measurement_ok: true`
- the run's `.validation.json` reports `ok: true`

`measurement_ok` is false whenever the load generator recorded any transport
error, any non-200 response, or any semantic failure. A semantic failure means
the response did not carry the expected number of distinct flags, all resolved
to their configured values, with no OpenFeature error codes and a matching
value digest. An HTTP 200 containing OpenFeature fallback values is a failure,
not a fast success.

Validation additionally checks that the recorded matrix is complete, that
provider settings are consistent within a configuration label, that the
manifest hash matches the catalog on disk, and that the load generator kept its
schedule. A run in which the generator fell behind fails validation, because
its latency numbers describe the generator rather than the server.

Failed rows are written rather than discarded so that a failure is visible in
the record instead of appearing as a gap. The Markdown report excludes failed
rows from its aggregates and prints a warning; the run-level validator still
fails the run.

## Reproducing an aggregation

```bash
python3 bench/report.py results/<run-id>.jsonl
python3 bench/validate_results.py results/<run-id>.jsonl
```
