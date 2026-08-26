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

| Provider path and strategy       | Request p50 | Evaluation p50 | Event-loop lag p99 |
|----------------------------------|------------:|---------------:|-------------------:|
| flagd in-process, direct         |      5.1 ms |         1.6 ms |             1.2 ms |
| flagd RPC, direct                |     58.6 ms |        52.8 ms |            55.5 ms |
| flagd RPC, threaded batch        |     55.6 ms |        52.3 ms |             1.1 ms |
| Flipt OFREP HTTP, direct         |    137.6 ms |       131.2 ms |           155.0 ms |
| Flipt OFREP HTTP, threaded batch |    134.4 ms |       128.9 ms |             1.1 ms |

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

## What the repeated local capacity ramp showed

On August 26, 2026, the 50-flag workload was ramped until each path either
queued, lost throughput, returned an invalid evaluation, or reached the limit
of the same-host load generator. The application and generator ran on the
macOS/ARM64 host; Flipt and flagd ran in Docker Desktop with six CPUs allocated.
The application used one Uvicorn worker, a 16-worker flag pool, a 500 ms flag
deadline, 5,000 virtual-user IDs, and uncached flagd RPC evaluation. Published
versions were Flipt v2.11.0, flagd v0.16.2,
`openfeature-provider-flipt==0.1.3`, and
`openfeature-provider-flagd==0.5.2` on Python 3.12.14. Reported confirmation
points contain three 15-second repetitions and passed semantic,
matrix-completeness, and generator-schedule validation unless explicitly
marked as rejected below.

The values in this table are medians across those three repetitions. A capacity
knee is identified by achieved rate falling below offered rate, a material
drain after the 15-second arrival window, or a latency cliff. This classification
is separate from the validator: a saturated row can still validate when every
scheduled request is launched and eventually succeeds.

| Provider path and strategy       | Repeated evidence                                                                                       | Next boundary evidence                                                                                                                   | Decision for this prototype                                                            |
|----------------------------------|---------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| Flipt OFREP, direct              | At 8 offered req/s: 7.95 achieved, 304 ms request p95, 250 ms loop-lag p99; already unstable            | At 10 offered: 8.51 achieved, 2.57 s p95, 2.63 s median drain                                                                            | Eliminate for any request path that needs more than single-digit throughput            |
| flagd RPC, direct                | At 40 offered: 39.99 achieved, 31 ms p95, 24 ms loop-lag p99                                            | At 50 offered: 44.50 achieved, 1.86 s p95, 547 ms loop-lag p99                                                                           | Eliminate at 50 req/s or when unrelated async endpoints need tight latency             |
| Flipt OFREP, threaded batch      | At 50 offered: 49.94 achieved, 60 ms p95, 1.0 ms loop-lag p99                                           | At 75 offered: 58.46 achieved, 4.17 s p95, 4.24 s median drain                                                                           | Offload fixes loop health, but this local path is not viable at 75 req/s               |
| flagd RPC, threaded batch        | At 75 offered: 74.94 achieved, 77 ms p95, 0.75 ms loop-lag p99                                          | The 100 req/s confirmation was rejected after 1 of 4,500 requests had a flag-evaluation error; diagnostic ramps queued by 175--200 req/s | Do not approve at 100+ req/s until the intermittent evaluation failure is diagnosed    |
| flagd in-process, direct         | At 750 offered: 748.48 achieved, 1.68 ms p95, 0.51 ms loop-lag p99; 33,750 of 33,750 requests succeeded | At 1,000+ offered, repeated runs reached the local generator/client limit while provider evaluation stayed sub-millisecond               | Preferred path; its provider ceiling was not reached in this topology                  |
| flagd in-process, threaded batch | At 500 offered: 498.94 achieved, 1.87 ms p95, 0.71 ms loop-lag p99; 22,500 of 22,500 requests succeeded | The same-host generator became inconsistent at 750+ req/s                                                                                | Capacity is ample, but offload adds no demonstrated value over the simpler direct path |

The flagd RPC errors at 100 req/s are correctness failures, not merely slow
responses. OpenFeature returned an evaluation error for one flag, the
application returned HTTP 502, and the run-level validator rejected the run.
The failed row was not included in a performance aggregate. Similar isolated
errors appeared in diagnostic 125 and 150 req/s runs, so the 75 req/s clean
confirmation is the highest publishable point from this local ramp.

### How this maps to real traffic

Five thousand users do not imply 5,000 requests/second. If every active user
hits this 50-flag endpoint at the following average interval, the aggregate
offered rate is:

| Average request interval per user |  Aggregate rate | What this ramp says                                                                 |
|-----------------------------------|----------------:|-------------------------------------------------------------------------------------|
| 10 minutes                        | about 8.3 req/s | Flipt direct is already at its local knee and has no burst headroom                 |
| 60 seconds                        |  about 83 req/s | Neither remote path has a clean confirmation at this rate; both in-process paths do |
| 30 seconds                        | about 167 req/s | Only the in-process paths remain inside demonstrated clean capacity                 |
| 10 seconds                        |       500 req/s | Both in-process strategies passed; direct was also clean at 750 req/s               |

For an internal administration endpoint that rarely reaches 5 req/s, any path
may be operationally adequate, although a direct synchronous remote provider
can still delay unrelated requests on the same event loop during a burst. For
a checkout, authorization, personalization, or API-gateway path serving tens
or hundreds of requests per second, the per-flag remote round trips dominate:
thread offload protects collateral endpoint latency but does not remove that
work. At a requirement of at least 100 req/s with zero fallback tolerance, this
run eliminates every measured remote configuration in its current form.

The recommended implementation from this prototype is therefore
`flagd_inprocess+direct`. The local resolver is already safe on the event loop,
had the lowest latency, and was the only option independently validated above
500 req/s without needing thread offload. `flagd_inprocess+threaded_batch` is
not a bad result, but it is dominated by the simpler direct strategy here.

These are elimination results for this pinned 50-flag, same-host experiment,
not universal production capacity limits. Fewer flags will change the result
but should not be extrapolated linearly. More Uvicorn workers or replicas can
raise aggregate throughput without making a blocking provider non-blocking.
A real deployment must repeat the chosen point with the load generator,
application, and remote provider on independently monitored tiers. The
file-backed in-process resolver also excludes production ruleset-delivery
overhead; that delivery path must be validated separately.

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
RATES="40 50" \
BACKENDS="flagd_rpc" \
STRATEGIES="direct" \
VIRTUAL_USERS=5000 \
REPS=3 \
DURATION=15 \
WARMUP=2 \
RUN_MICRO=0 \
./run_experiment.sh
```

Ramp one backend/strategy pair at a time so a badly saturated path does not
make the rest of a matrix wait for its queue to drain. Before committing to a
long matrix, run one repetition at the target flag count and check that it
passes validation, then repeat the two rates around the observed knee. Remote
paths issue one round trip per flag, so 50 flags per request can exceed what one
application worker, thread pool, or provider service can sustain.

Validation proves matrix completeness, response semantics, and generator
health; it does not impose a service latency or throughput SLO. A saturated row
may therefore validate if all requests eventually complete. Reject a capacity
point when achieved rate materially trails offered rate, the drain duration
grows, or latency jumps relative to the preceding rate. If generator launch lag
fails validation, move the generator to another host before making a provider
capacity claim.

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
