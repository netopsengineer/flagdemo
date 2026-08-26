# Experiment contract

## 1. Question being tested

This experiment answers a specific deployment question:

> What request-path cost and event-loop behavior do the currently released Python OpenFeature provider paths produce in a FastAPI service when each request evaluates a known number of distinct flags?

It compares:

1. Flipt through the released Python OpenFeature provider and OFREP HTTP.
2. flagd through the released Python OpenFeature provider and gRPC RPC evaluation.
3. flagd through the released Python OpenFeature provider and local in-process evaluation.

It does not compare every possible Flipt SDK or a custom provider implementation.

The fair RPC comparison disables flagd's optional LRU value cache because the released Flipt OFREP provider has no equivalent cache. This forces both remote paths to perform their documented remote work. Set `FLAGD_RPC_CACHE=lru` in a separate run to measure flagd's production-default static-value cache; do not combine those rows with the uncached matrix.

## 2. Why the Flipt test is valid for this stack

Verified on August 25, 2026:

- PyPI lists `openfeature-provider-flipt==0.1.3` as the latest release. Its package description states that it evaluates against Flipt through OFREP and performs evaluations through HTTP: <https://pypi.org/project/openfeature-provider-flipt/>
- Flipt documents a separate family of client-side SDKs that synchronize state and evaluate locally: <https://docs.flipt.io/v2/integration/client>
- The OpenFeature Python contrib repository has an open request to add a provider around the Flipt client-side SDK: <https://github.com/open-feature/python-sdk-contrib/issues/163>
- The corresponding implementation is still an open, unmerged pull request and is marked as work in progress with tests required: <https://github.com/open-feature/python-sdk-contrib/pull/179>

The exact conclusion is:

> The benchmark represents the currently released Flipt Python OpenFeature provider path. There is no released official Python OpenFeature provider for Flipt local client evaluation in this stack. A custom adapter around the separate Flipt client SDK would be a different implementation and requires a separate test.

A Flipt bulk endpoint or vendor-specific client call is also a different application API. Standard OpenFeature evaluations are one flag at a time, and the released provider used here calls the single-flag OFREP path.

For Flipt v2, the harness supplies `X-Flipt-Environment` through the provider's published `headers_factory` option and supplies the namespace through the published Flipt provider. This keeps the test on the released provider path while selecting the same environment and namespace seeded by `prepare.sh`.

## 3. Canonical flag workload

The experiment uses `flags/manifest.json` as the source of truth. `tools/generate_flags.py` creates both that manifest and `flagd/flags.json`.

The default catalog contains 50 keys:

```text
checkout-flag-001
checkout-flag-002
...
checkout-flag-050
```

Every flag has the same deliberately simple semantic contract:

```text
configured value: true
caller default:   false
```

The simple static rule holds rule complexity constant and isolates provider-path cost. This is not a complex targeting-engine benchmark.

Each request selects the first `FLAGS_PER_REQUEST` entries from the manifest. The keys are checked for uniqueness. Therefore:

- `FLAGS_PER_REQUEST=3` evaluates three different keys.
- `FLAGS_PER_REQUEST=50` evaluates all 50 different keys once.
- Repeated evaluation of the same key is not used to simulate a larger flag count.

The application still passes the same evaluation context to every provider so context serialization and transport remain part of remote-provider cost.

## 4. Semantic correctness gate

HTTP completion alone is insufficient because OpenFeature returns the caller default when many evaluation failures occur.

The application calls `get_boolean_details()` and checks each result for:

- No OpenFeature `error_code`.
- Returned value equals the manifest's expected value.
- Number of evaluated keys equals the configured count.
- Number of distinct keys equals the configured count.

Because the configured value is `true` and the caller default is `false`, a missing flag, timeout, provider-not-ready state, parse error, or other fallback becomes a mismatch.

The endpoint returns HTTP 502 when semantic validation fails. The load generator also validates the response body independently. A request is counted as successful only when both HTTP and semantic checks pass.

`bench/preflight.py` evaluates the selected key set for several users before every timed run. A failed preflight aborts the experiment instead of producing timing data from a broken setup.

## 5. Application call strategies

### `direct`

Evaluations run sequentially on the FastAPI event-loop thread.

- Correct baseline for local in-process evaluation.
- Intentionally unsafe control for synchronous remote providers because each round trip blocks the event loop.

### `threaded_batch`

The complete sequential evaluation batch is submitted as one job to a dedicated bounded thread pool.

- Protects the event loop from the synchronous provider.
- Preserves the same number and order of provider calls.
- Does not add per-request fan-out.
- Remote cost remains and requests can queue for pool workers.

This is the default offload comparison.

### `threaded_parallel`

Each flag evaluation is submitted separately and gathered concurrently.

- Adds per-request fan-out.
- Changes backend concurrency and resource pressure.
- Can reduce one request's wall time while increasing total concurrency.
- Is not directly comparable to `direct` as an isolated offload change.

It is available only when explicitly requested:

```bash
STRATEGIES="direct threaded_batch threaded_parallel" ./run_experiment.sh
```

## 6. Traffic model

### Primary: open loop

The load generator precomputes the fixed arrival count from rate and duration, then launches every scheduled arrival regardless of server response time. It never silently drops overdue arrivals. If the generator falls behind, launch lag and the stretched launch span cause validation to fail. This gives every configuration the same offered work and is the primary latency and CPU comparison.

The result records:

- Offered and achieved request rates.
- Generator launch-lag percentiles.
- Load-generator process CPU.
- HTTP and server-side flag-evaluation latency.
- Event-loop lag and stalls.
- Application-process CPU per valid request.
- Semantic and transport errors.

A separate `/ping` probe waits for each response and then sleeps 50 ms, so it contributes at most about 20 requests/second and slows down when the event loop is delayed. Treat it as supporting collateral-latency evidence; loop-lag measurements are the primary event-loop signal.

The validator rejects an open-loop row when scheduled rate differs from offered rate by more than 2 percent or launch-lag p99 exceeds the larger of 2 ms and 25 percent of the request interval.

### Optional: closed loop

Closed-loop workers send another request as soon as the prior one completes. This is useful for stress behavior, but faster configurations generate more work for themselves. Do not compare closed-loop latency across configurations or call a single concurrency point a capacity ceiling.

Enable it explicitly:

```bash
RUN_STRESS=1 STRESS_CONCURRENCY=50 ./run_experiment.sh
```

## 7. Five thousand users

The harness uses a stable pool of user IDs, controlled by `VIRTUAL_USERS`. It does not equate users with requests per second.

Examples for 5,000 active users:

| Average request interval per user |  Aggregate rate |
|-----------------------------------|----------------:|
| 60 seconds                        |  about 83 req/s |
| 30 seconds                        | about 167 req/s |
| 10 seconds                        |       500 req/s |
| 5 seconds                         |     1,000 req/s |
| 1 second                          |     5,000 req/s |

The default user pool is 5,000. Open-loop arrivals cycle deterministically through that pool. A short low-rate run may not touch every ID; use a duration of at least `VIRTUAL_USERS / RPS` when every user must appear at least once. Select the rate separately:

```bash
VIRTUAL_USERS=5000 RATES="83 167 500" FLAG_COUNTS="50" ./run_experiment.sh
```

## 8. Running the experiment

### Prepare providers and flags

```bash
./prepare.sh
```

This performs:

1. Generate a canonical catalog and matching flagd document.
2. Start release-tagged Flipt and flagd container versions.
3. Seed every manifest flag into Flipt through the v2 resources API.

The metadata capture records the resolved Docker image IDs and repository digests after preparation. Retain that metadata with the result file; release tags alone are not an immutable provenance record.

### Install Python dependencies

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-dev.txt
source .venv/bin/activate
```

`app/requirements.lock` is the complete pinned runtime graph used by the
application image. `requirements-dev.txt` adds only the test and lint tools.

### Run the default matrix

```bash
./run_experiment.sh
```

The runner interleaves repetitions, deterministically rotates configuration order between repetitions, executes semantic preflight before timing, writes one JSON line per measurement, produces a Markdown report, captures environment metadata, and validates the completed matrix.

The supporting microbenchmark defaults to six rounds and flag counts 1, 3, 10, 25, and 50. Provider order rotates each round and count order alternates, so no provider always runs first. It validates every evaluation but remains supporting evidence; the HTTP workload is primary.

### Run only the 50-flag case

```bash
FLAG_COUNTS="50" RATES="40 100 250 500" REPS=3 ./run_experiment.sh
```

### Run one configuration manually

```bash
FLAG_BACKEND=flipt \
FLAG_STRATEGY=threaded_batch \
FLAGS_PER_REQUEST=50 \
FLAG_MANIFEST_PATH="$PWD/flags/manifest.json" \
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another shell:

```bash
python3 -m bench.preflight --url http://127.0.0.1:8000
python3 -m bench.bench \
  --url http://127.0.0.1:8000 \
  --label flipt+threaded_batch \
  --users 5000 \
  --rate 500 \
  --duration 30
```

## 9. Host-isolated scale testing

A same-host comparison is useful for implementation behavior but cannot prove that a tier is unconstrained at production scale.

For a decision-grade 500 req/s or higher run, use at least three independently monitored tiers:

```text
load-generator host
        |
application host or cluster
        |
Flipt or flagd remote backend host/cluster
```

For flagd in-process, there is no remote evaluation tier, but the ruleset delivery component should still be monitored separately from the request-path measurement.

Before accepting a run:

- Load-generator CPU should have clear headroom.
- Generator launch lag should pass the included validator.
- Offered and scheduled request rates should agree.
- No individual application or backend CPU core should remain pinned.
- Application and remote backend aggregate CPU should normally remain below the chosen headroom limit, commonly 60 to 70 percent for latency testing.
- Network, connection pools, file descriptors, and thread-pool queues must remain below limits.
- All semantic checks must pass.
- Repeat the same point after doubling one tier's CPU. A material result change indicates the original tier was constrained.

Application CPU in the included rows excludes Flipt and flagd service CPU. Collect remote backend CPU independently.

No ad hoc latency proxy is included. Network sensitivity tests should use a controlled impairment layer or separate hosts, record the applied one-way delay/loss/jitter, and be reported as a separate matrix.

## 10. Permitted conclusions

The experiment can support statements such as:

- The released Flipt Python OpenFeature OFREP path has a measured per-flag remote HTTP cost in this FastAPI workload.
- Direct synchronous remote evaluations can produce event-loop lag and stalls under specified load conditions.
- Threaded batch offload protects the event loop but does not remove remote per-flag operations.
- flagd in-process evaluation has lower request-path overhead than the measured remote paths under equivalent flag counts and semantics.
- At a specified rate and flag count, a configuration passed or failed the recorded semantic, latency, generator-health, and resource criteria.

It cannot by itself support statements such as:

- Every Flipt SDK or custom integration is slow.
- A custom OpenFeature adapter around Flipt's separate local client SDK would perform like OFREP.
- One closed-loop point is the maximum capacity of a system.
- Application-process CPU is total system CPU.
- A same-host result directly predicts a production network deployment.
