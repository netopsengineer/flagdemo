# Repository agent contract

## Scope and precedence

- Apply these instructions to every file in this repository.
- Follow system, developer, and explicit user instructions before this file.
- Treat `README.md` as the operator run guide and `docs/EXPERIMENT.md` as the
  methodology contract. Read the latter before changing benchmark semantics,
  traffic generation, validation, provider comparisons, or performance claims.
- Do not broaden the experiment beyond released Python OpenFeature provider
  paths without an explicit requirement and a separately labelled comparison.

## Required environment

- Use Python 3.12.
- Prefer `.venv/bin/python` and tools installed in `.venv`.
- Create the environment when absent:

  ```bash
  uv venv --python 3.12
  uv pip install --python .venv/bin/python -r requirements-dev.txt
  ```

- Require Docker with the Compose plugin for Flipt, flagd RPC, image-build, and
  all-provider integration checks.
- If Docker or another required tool is unavailable, run every unaffected gate,
  report the omitted gate explicitly, and do not claim full integration success.

## Repository map

| Path | Contract |
|---|---|
| `flags/manifest.json` | Canonical flag keys and expected values |
| `flagd/flags.json` | Generated flagd document; never edit independently |
| `tools/generate_flags.py` | Generates and verifies both catalog documents |
| `app/` | FastAPI application, provider wiring, and semantic checks |
| `bench/` | Preflight, load generation, reporting, metadata, and validation |
| `seed/` | Idempotent Flipt v2 resource seeding |
| `run_experiment.sh` | Validated matrix orchestration |
| `docs/EXPERIMENT.md` | Canonical methodology and claim boundaries |
| `results/` | Ignored local run artifacts except `results/README.md` |

## Mandatory benchmark invariants

- Evaluate exactly `FLAGS_PER_REQUEST` distinct manifest keys once per request.
- Keep configured values opposite caller defaults. The current contract is
  configured `true` and caller default `false` so provider fallbacks are visible.
- Use `get_boolean_details()` and reject any OpenFeature error code, fallback,
  value mismatch, count mismatch, duplicate key, or digest mismatch.
- Return HTTP 502 for application-level semantic failure. Never convert a
  fallback or partial evaluation into a successful measurement.
- Build evaluation contexts through `app/evaluation_context.py`. Keep values
  string-compatible with Flipt v2 OFREP's `map<string,string>` context contract.
- Run semantic preflight before timing. Keep its expected label, flag count, and
  spawned-process PID checks; do not weaken them to make a run pass.
- Keep backend and strategy identity explicit. Allowed backends are `flipt`,
  `flagd_rpc`, and `flagd_inprocess`; allowed strategies are `direct`,
  `threaded_batch`, and `threaded_parallel`. A row label must equal its backend
  and strategy joined with `+`, and `provider_path` must identify the measured
  path.
- Preserve strategy meaning:
  - `direct`: one sequential batch on the event-loop thread.
  - `threaded_batch`: the same sequential batch on one bounded-pool worker.
  - `threaded_parallel`: one worker job per flag; label and analyze separately.
- Do not add `threaded_parallel` to the default comparison. It changes provider
  concurrency in addition to event-loop offload.
- Keep flagd RPC caching disabled in the fair default remote comparison. Label
  cached runs separately and do not aggregate them with uncached rows.
- Reject invalid inputs and incomplete matrices. Do not remove schedule-health,
  semantic, process-identity, or output-overwrite guards.

## Generated files and dependencies

- Change the catalog only through `tools/generate_flags.py`. Regenerate both
  catalog documents together and rebuild the application image afterward.
- Verify the default catalog with:

  ```bash
  .venv/bin/python tools/generate_flags.py --count 50 --check
  ```

- Keep direct runtime requirements pinned in `app/requirements.txt` and the full
  transitive runtime graph in `app/requirements.lock`.
- Regenerate the lock after a runtime dependency change:

  ```bash
  uv pip compile app/requirements.txt \
    --python .venv/bin/python \
    --output-file app/requirements.lock
  ```

- Build the Docker image from the lock, not the direct-requirements file.
- Verify current upstream documentation before changing version tags or claims
  about released, latest, supported, or official provider paths.

## Change procedure

1. Inspect `git status --short --branch` before editing. Preserve unrelated user
   changes and ignored local benchmark results.
2. Read the implementation, tests, and relevant methodology section before
   changing behavior.
3. Make the smallest coherent change. Add a regression test for every corrected
   invariant or failure mode.
4. Run the gates required by the change matrix below.
5. Inspect the final diff and repository status. Do not include caches, virtual
   environments, host-specific metadata, secrets, or local result artifacts.
6. Commit or push only when explicitly authorized. Stage explicit paths; do not
   use blanket staging in a mixed worktree.

## Validation gates

Run these gates for every change:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
```

Add the applicable conditional gates:

| Changed surface | Required gate |
|---|---|
| Python source or tests | `.venv/bin/python -m compileall -q app bench seed tools tests` |
| Shell scripts | `bash -n prepare.sh run_experiment.sh` |
| Catalog or generator | `.venv/bin/python tools/generate_flags.py --count 50 --check` |
| Compose or Dockerfiles | `docker compose config --quiet` and `docker compose build app` |
| Provider wiring, context, seeding, runner, or validation | All-provider smoke procedure below |
| Documentation-only | Verify commands, paths, defaults, and claims against current code |

For an all-provider smoke test:

```bash
./prepare.sh
FLAG_COUNTS="3 50" \
REPS=1 \
DURATION=1 \
WARMUP=0.2 \
SETTLE_SECONDS=0 \
MICRO_FLAG_COUNTS="1,3,50" \
MICRO_ROUNDS=1 \
./run_experiment.sh
```

Acceptance criteria:

- The command exits zero.
- The validation JSON reports `ok: true`, an empty `failures` list, and an empty
  `warnings` list.
- The matrix contains six configurations at flag counts 3 and 50: 12 open-loop
  rows total.
- Every row has `measurement_ok: true`, zero failed requests, and the expected
  manifest hash.
- Flipt HTTP, flagd RPC, and flagd in-process microbenchmarks all complete.

Stop services after integration checks with `docker compose down`. Use
`docker compose down --volumes` only when the task authorizes removal of seeded
state or requires a fully clean Docker state.

## Results and claims

- Treat a row as evidence only when `measurement_ok` is true and the run-level
  validation JSON reports `ok: true`.
- Keep failed rows in raw output. Reports must exclude them from aggregates,
  warn about the exclusion, and leave the run-level validator failing.
- Treat a one-second, one-repetition smoke matrix as correctness evidence only.
  Do not use its p95 or p99 request latency, throughput, or provider ratio as a
  decision-grade performance claim.
- State flag count, offered rate, duration, repetitions, provider versions,
  strategy, cache mode, host topology, and validation status with any result.
- Do not describe application CPU as total system CPU; it excludes Flipt and
  flagd service processes.
- Do not generalize same-host Docker Desktop results to production capacity or
  to SDK/provider implementations outside the recorded `provider_path`.
- Preserve metadata and raw rows for any result selected for publication, but
  remove or redact host-specific absolute paths before intentionally committing
  an artifact.

## Failure recovery

| Failure | Required action |
|---|---|
| Output file already exists | Use a new automatic or explicit `RUN_ID`; do not overwrite evidence |
| Port is occupied or health responds from another process | Stop the conflicting process or choose `PORT`; retain expected-PID preflight |
| Flipt reports `INVALID_CONTEXT` | Verify all shared context values are strings; do not remove context validation |
| Seed reports an existing flag | Preserve revision-aware update behavior; rerunning `prepare.sh` must succeed |
| Generator misses its schedule | Reduce offered rate or isolate the load generator; do not loosen validator thresholds |
| Measurement contains errors or fallbacks | Diagnose provider/application state and rerun; do not aggregate the row |
| Docker integration cannot run | Mark the gate unavailable and stop short of full-readiness claims |

## Completion report

- State the behavioral outcome first.
- List changed files or surfaces, validation commands and outcomes, and any
  skipped or unavailable gate.
- State whether Docker services remain running and whether volumes were removed.
- State the committed branch and pushed remote only after verifying them.
- Do not declare completion with a failed required gate or an unreported blocker.
