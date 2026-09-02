---
name: preflight
description: Run the workflow_ocr_backend quality gate - unit tests with coverage, the Docker build, and the HaRP integration test when deployment changed - and fix what it reports. Use before committing or opening a PR.
allowed-tools: Bash, Read, Edit, Grep, Glob
---

## 1. Unit tests

```bash
make deps      # first run only
make test
```

Runs pytest with coverage, excluding the `harp_integration` marker. CI enforces a coverage
threshold on `coverage/coverage.xml`, so a new module without tests will fail the build
even when every test passes locally.

Tests load `.env` through `python-dotenv` and build ExApp auth headers from it. A 401 in a
test means the headers or `.env` are wrong, not the endpoint.

## 2. Docker build

```bash
make build
```

Required whenever `Dockerfile`, `start.sh`, `requirements*.txt`, or the package layout
changed — the image is the deliverable, and a change that only works outside the container
is not done. `COPY` lines in the `app` stage list the shipped files explicitly; a new
top-level module or package must be added there or it is missing at runtime.

## 3. HaRP integration test

```bash
make harp-integrationtest
```

Needed when `start.sh`, the FRP config, the entrypoint, `appinfo/info.xml`, or anything
about ExApp registration changed. It spins up a HaRP container and the ExApp over Docker
and is slow; it self-skips when the Docker CLI is unavailable — **a skip is not a pass**,
report it as not run.

## 4. Contract check

If the change touched an endpoint, a request field, a response model, a status code, or the
`ocrmypdf_parameters` parsing, run `/sync-backend-contract`. Nothing in this repo's tests
catches a break on the PHP side.

## Reporting

Say which of the four ran, which passed, and which were skipped and why.
