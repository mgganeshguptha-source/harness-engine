# harness-engine

The **deterministic engine** for Harness Engineering — a GitHub Actions pipeline
that drives a GitHub Copilot SDK agent through fixed phases with hard gates. The
agent writes code, tests, and docs; the engine enforces that the result stayed in
scope, passed the real test build, hit coverage, and cleared an independent review
before a pull request is raised.

This repo holds the engine and the **reusable workflow** that service repos call.
You normally **do not edit anything here per run** — service-specific settings live
in each caller repo, not here.

---

## What's in here

```
harness-engine/
├── .github/workflows/harness.yml   # the reusable workflow (on: workflow_call)
├── harness/                        # the Python engine (run.py + phases + gates)
├── requirements.txt                # Python deps (Copilot SDK, pyyaml)
└── README.md
```

## How to use it (from a service repo)

A service/sample repo calls this engine — it does **not** copy it. In the service
repo, add a thin caller workflow at `.github/workflows/harness.yml`:

```yaml
name: Harness
on:
  workflow_dispatch:
    inputs:
      feature_id:
        description: "Story / feature id to run"
        required: true
jobs:
  harness:
    uses: mgganeshguptha-source/harness-engine/.github/workflows/harness.yml@v1
    with:
      feature_id: ${{ inputs.feature_id }}
      java-version: "17"
      python-version: "3.13"
      node-version: "20"
    secrets: inherit
```

## What the calling repo must provide

1. **GitHub Actions enabled** on the repo.
2. **A `.harness/config.yaml`** describing the run (test command, coverage gate,
   write-exclude, model routing). Module paths are auto-detected from `pom.xml` —
   no service name is hardcoded. See `harness/config.yaml.sample` for the shape.
3. **A secret `COPILOT_GITHUB_TOKEN`** — a fine-grained PAT on a Copilot-licensed
   seat, with `Contents: read/write` and `Pull requests: write` on the repo.
4. **The Copilot toolkit** — the skills/instructions from `copilot-toolkit` copied
   into the repo's `.github/` so the engine loads them during the run.

## What a run does

`init` seeds the story, then `autorun` drives every phase end-to-end
(context → prompt-steps → coding → code-review → unit-testing → documentation →
raise-PR). Human approval gates are auto-approved in CI, but the **deterministic
gates always apply**: write boundary, scope, code-review verdict, and validation +
coverage. On success the engine raises a PR on a `harness/<feature>-<run_id>`
branch; on a halt it pushes `harness-halted/<feature>-<run_id>` and raises **no**
PR. Every run commits a full audit trail (context, plan, review verdict, validation
report, PR body, capability manifest).

## Versioning

Callers **pin to a tag** (`@v1`) for stability — a known-good engine that won't
change under them when `main` moves. Cut a new tag to release engine updates;
callers upgrade on their own schedule.

## Note

This is the engine. It runs against the **caller's** repo, on the **caller's**
Actions minutes. It does not run anything on its own. Do not commit a real
`current-story` or a filled `config.yaml` here — those belong in the calling repos;
this repo ships only `config.yaml.sample`.
