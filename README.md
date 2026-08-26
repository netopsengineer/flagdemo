# FastAPI OpenFeature provider-path benchmark

This project measures what it costs a FastAPI application to evaluate feature
flags through each released Python OpenFeature integration path available for
this stack:

- **Flipt** through `openfeature-provider-flipt`, which evaluates one flag per
  synchronous OFREP HTTP request.
- **flagd** through remote gRPC evaluation.
- **flagd** through local file-backed evaluation.

## Why this experiment exists

Feature-flag evaluation looks free in application code. A call like
`client.get_boolean_details("checkout-flag-001", False, ctx)` reads like a
dictionary lookup, but depending on the provider it may be a blocking HTTP
round trip issued from inside an `async def` handler. On a single-worker
asyncio service, that difference decides whether flag evaluation is a rounding
error or the dominant cost of a request.

The question this project answers is narrow and practical:

> For a FastAPI service that evaluates N feature flags per request, what does
> each released Python OpenFeature provider path actually cost in request
> latency, application CPU, and event-loop health?

Three design decisions follow from that question.

**Distinct flag keys.** A benchmark that evaluates the same key repeatedly
measures provider caching, not evaluation cost. The catalog therefore contains
50 distinct keys, and `FLAGS_PER_REQUEST=N` evaluates N different keys exactly
once each.

**Detectable fallbacks.** OpenFeature returns the caller's default value when
evaluation fails. A provider that is misconfigured, unreachable, or timing out
will happily return HTTP 200 with default values, and a naive load generator
records that as a fast success. Here every configured value is `true` while
every caller default is `false`, so any fallback is a visible mismatch. The
application returns HTTP 502 on a mismatch and the load generator refuses to
count it.

**Separating "which backend" from "how you call it".** Moving a blocking call
off the event loop is a different intervention from changing providers. The
default matrix crosses three backends with two call strategies so the two
effects can be read independently.

## What gets measured

Each run records request latency percentiles, server-side evaluation time,
application-process CPU per successful request, event-loop lag and stall
counts, a `/ping` probe on an endpoint that evaluates no flags, RSS, thread
count, and load-generator CPU and launch lag.

The last two matter more than they look. An open-loop generator that cannot
keep its own schedule produces latency numbers that describe the generator
rather than the server, so the harness records launch lag and fails validation
when the schedule slips.

## Call strategies

| Strategy            | Behaviour                                                   |
|---------------------|-------------------------------------------------------------|
| `direct`            | Sequential synchronous evaluations on the event-loop thread |
| `threaded_batch`    | One unchanged sequential batch offloaded to one pool worker |
| `threaded_parallel` | One pool job per flag, gathered concurrently                |

`threaded_batch` is in the default matrix because it changes exactly one thing:
whether the batch runs on the event loop. `threaded_parallel` also changes
per-request provider concurrency and pressure on the remote service, so it is
available but excluded from the default matrix and labelled separately.

## Scope and claim boundary

PyPI lists `openfeature-provider-flipt` as the released Python OpenFeature
provider for Flipt. It is an OFREP HTTP provider. Flipt also publishes a
separate local-evaluation Python client SDK, but there is no released official
Python OpenFeature provider built around that client.

This experiment is therefore valid for a requirement that application code must
use the currently released Python OpenFeature provider. It does not claim that
a custom adapter around Flipt's separate local client SDK would perform the
same way.

Note also that the `flagd_inprocess` backend uses the provider's file-backed
resolver. It isolates evaluation cost and carries no flag-synchronisation
overhead, which is the right comparison for per-evaluation cost but is not
identical to a deployed in-process resolver streaming updates from flagd.

## What the publication smoke test showed

The publication smoke test ran all six default backend/strategy combinations
with 3 and 50 distinct flags per request. It used a single one-second repetition at
5 offered requests/second on Docker Desktop for macOS/ARM64. All 12 rows passed
semantic and schedule validation with zero failed requests.

The 50-flag rows make the architectural result easiest to see:

| Provider path and strategy | Request p50 | Evaluation p50 | Event-loop lag p99 |
|---|---:|---:|---:|
| flagd in-process, direct | 5.1 ms | 1.6 ms | 1.2 ms |
| flagd RPC, direct | 58.6 ms | 52.8 ms | 55.5 ms |
| flagd RPC, threaded batch | 55.6 ms | 52.3 ms | 1.1 ms |
| Flipt OFREP HTTP, direct | 137.6 ms | 131.2 ms | 155.0 ms |
| Flipt OFREP HTTP, threaded batch | 134.4 ms | 128.9 ms | 1.1 ms |

The data supports one strong implementation conclusion: invoking these
synchronous remote providers directly blocks FastAPI's event loop. Moving the
unchanged batch to a worker thread reduced remote-provider event-loop lag from
55--155 ms to about 1 ms, but did not materially reduce request latency because
the same synchronous evaluations still had to finish. The in-process provider
was already event-loop-safe at this workload, so offloading it added no value.

The supporting 50-flag microbenchmark showed the same directional ordering:
0.76 ms per local flagd batch, 16.1 ms per flagd RPC batch, and 106.2 ms per
Flipt HTTP batch. Among the two remote paths, flagd RPC was about 2.4 times
faster than Flipt HTTP in the application smoke rows.

These numbers are a correctness and implementation smoke test, not a capacity
study. Each application table cell contains only five requests, so its p95 and
p99 request latencies are not statistically meaningful. Do not generalize the
provider ratio beyond this pinned same-host setup, use the smoke run as a
maximum-throughput claim, or compare application CPU as total system CPU: the
application metric excludes the Flipt and flagd service processes. Run the
repeated matrix described below before publishing performance claims.

See [`docs/EXPERIMENT.md`](docs/EXPERIMENT.md) for the full methodology and
interpretation contract.

## Quick start

Prerequisites are Docker with the Compose plugin, `uv`, and a shell with Bash.
The application targets Python 3.12; the checked-in lock file fixes the complete
runtime dependency graph used by the Docker image and local runs.

### 1. Create the local environment

Run these commands from the repository root:

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python \
  -r app/requirements.lock \
  -r requirements-dev.txt
```

Activating the environment is optional. The runner automatically uses
`.venv/bin/python` when it exists.

### 2. Prepare the providers and flag catalog

```bash
./prepare.sh
docker compose ps
```

`prepare.sh` generates the shared catalog, starts the pinned Flipt and flagd
images, waits for flagd, and idempotently seeds Flipt from the same manifest the
application reads. `docker compose ps` should show both services running before
an experiment starts.

### 3. Run a short all-provider smoke test

Use this before a longer experiment or after changing provider wiring:

```bash
FLAG_COUNTS="3 50" \
RATES="5" \
REPS=1 \
DURATION=1 \
WARMUP=0.2 \
SETTLE_SECONDS=0 \
MICRO_FLAG_COUNTS="1,3,50" \
MICRO_ROUNDS=1 \
./run_experiment.sh
```

The command exits nonzero if semantic preflight, a measurement, matrix
completeness, or generator-health validation fails. It prints the exact output
paths at the end. The automatically generated UTC run ID prevents accidental
overwrites; if `RUN_ID` is supplied explicitly, it must be unique.

### 4. Run the repeated default experiment

```bash
./run_experiment.sh
```

The default experiment runs:

```text
backends:    flipt, flagd_rpc, flagd_inprocess
strategies:  direct, threaded_batch
flag counts: 3, 50 distinct flags/request
offered load: 5 requests/second
users:       5,000 stable user IDs
repetitions: 3, interleaved
```

Results are written to timestamped files under `results/`:

```text
<run-id>.jsonl             raw rows
<run-id>.md                aggregated report
<run-id>.metadata.json     runtime, host, and source metadata
<run-id>.micro.json        isolated per-evaluation batch costs
<run-id>.validation.json   matrix, semantic, and generator-health validation
```

Only rows with `measurement_ok: true` in a run whose validation file reports
`ok: true` are acceptable evidence.

The included GitHub Actions workflow repeats static/unit checks, prepares the
pinned Docker services, builds the application image, and runs a reduced
all-provider matrix on every push and pull request.

### 5. Stop the provider services

Preserve the seeded Flipt volume for another run:

```bash
docker compose down
```

Or remove containers, the Compose network, and the seeded volume completely:

```bash
docker compose down --volumes
```

## Sizing the load

Five thousand users is a population, not a throughput. Choose an arrival rate
that matches user behaviour: one request every ten seconds per active user is
500 requests/second. The generator cycles a stable 5,000-ID pool, so set the
duration to at least `5000 / RPS` if every ID must appear in a run.

```bash
FLAG_COUNTS="50" \
RATES="50 100 250 500" \
VIRTUAL_USERS=5000 \
REPS=3 \
./run_experiment.sh
```

Before committing to a long matrix, run a single row at your target flag count
and check that it passes validation. Remote paths issue one round trip per
flag, so 50 flags per request at a high rate can exceed what a single worker
can serve; a saturated run fails validation rather than producing a usable
number.

Do not use one shared laptop or VM to establish a production ceiling. Put the
load generator, application tier, and remote flag service on separate hosts and
monitor each. The bundled runner is appropriate for implementation validation
and controlled same-host comparisons; `bench/bench.py` can be pointed at
remotely deployed application instances.

## Main files

| Path                        | Purpose                                              |
|-----------------------------|------------------------------------------------------|
| `flags/manifest.json`       | Canonical 50-flag catalog and expected values        |
| `tools/generate_flags.py`   | Generates the manifest and matching flagd document   |
| `seed/seed.py`              | Seeds Flipt from the canonical manifest              |
| `app/main.py`               | FastAPI endpoints and process metrics                |
| `app/requirements.lock`     | Fully pinned runtime dependency graph                |
| `app/flag_client.py`        | Provider wiring, strategies, and semantic validation |
| `bench/preflight.py`        | Fails before timing if any selected flag is wrong    |
| `bench/bench.py`            | Open-loop load driver with response validation       |
| `bench/microbench.py`       | Distinct-key batch microbenchmark                    |
| `bench/validate_results.py` | Matrix, semantic, and generator-health validation    |
| `bench/report.py`           | Aggregates raw rows into a report                    |
| `docs/EXPERIMENT.md`        | Full methodology and interpretation contract         |
| `BUILD_VALIDATION.json`     | Checks performed before packaging                    |

## Regenerating the catalog

```bash
FLAG_CATALOG_SIZE=100 ./prepare.sh
```

The benchmark refuses to start if the checked-in manifest and flagd document do
not match the requested catalog size. Any app image built after regeneration
must contain or mount the regenerated files.

## Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check app bench seed tools tests
.venv/bin/ruff format --check app bench seed tools tests
bash -n prepare.sh run_experiment.sh
docker compose config --quiet
```
