# Harness PoC — File Manifest & Placement Guide

Update your local `harness-demo` repo with the files below. Paths are relative to
the repo root: `C:\Users\ganesh.guptha\projects\harness-demo\`

---

## 1. Harness orchestrator — folder: `harness\`

Replace/add ALL of these in `harness\`:

| File | New / Replace | Purpose |
|------|---------------|---------|
| `contracts.py`       | replace | Exit-code contract (added NEEDS_CLARIFICATION) |
| `phases.py`          | replace | 6-phase spine (context path, scan/record flags) |
| `boundaries.py`      | replace | Write-boundary logic (absolute-path handling) |
| `state.py`           | replace | Run state (tokens, per-phase log, validation attempts) |
| `state_machine.py`   | replace | Engine (validation/clarification gates, loopback, stamp) |
| `executor.py`        | replace | Per-phase enforcement, token + execution-record hooks |
| `sdk_runner.py`      | replace | Real Copilot SDK runner (CI auth, per-phase model, selective capability) |
| `fake_runner.py`     | keep    | Unchanged from earlier — fake runner for tests |
| `run.py`             | replace | CLI (init/run/approve/reject/status/autorun; story-from-file; token report) |
| `config.py`          | replace | All config (selective loading, repo_stacks, phase_models, coverage, etc.) |
| `config.yaml.sample` | replace | Sample config — copy to `.harness\config.yaml` |
| `validation.py`      | replace | mvn-test gate + format pre-step + coverage gate |
| `clarification.py`   | **new** | NEEDS CLARIFICATION scanner |
| `execution_record.py`| **new** | Appends actual-vs-approved audit to plan |
| `story_source.py`    | **new** | Reads story from file (MCP-swappable) |
| `list_models.py`     | keep    | Helper to list tenant models |
| `test_boundaries.py` | replace | 14 boundary tests |
| `test_state_machine.py` | replace | 8 engine tests |

---

## 2. GitHub Actions workflow — folder: `.github\workflows\`

| File | Placement |
|------|-----------|
| `harness.yml` | `.github\workflows\harness.yml` (repo ROOT .github, NOT under harness\) |

---

## 3. Modified skill — folder: `spring-petclinic\.github\skills\build-context\`

| File | Placement |
|------|-----------|
| `build-context-SKILL.md` | rename to `SKILL.md`, replace `spring-petclinic\.github\skills\build-context\SKILL.md` |

(Adds the §0 Mode-Detection block: non-interactive/CI mode writes
`[NEEDS CLARIFICATION]` instead of asking.)

---

## 4. Story file — folder: `spring-petclinic\stories\`

| File | Placement |
|------|-----------|
| `current-story.md` | `spring-petclinic\stories\current-story.md` (the hasPet story) |

---

## After placing files

```powershell
cd C:\Users\ganesh.guptha\projects\harness-demo
Remove-Item -Recurse -Force .\harness\__pycache__ -ErrorAction SilentlyContinue

# sanity: tests still pass locally
cd harness
python test_boundaries.py
python test_state_machine.py
cd ..

# refresh the local .harness config (for local runs)
Copy-Item .\harness\config.yaml.sample .\spring-petclinic\.harness\config.yaml -Force

# commit & push
git add harness/ .github/ spring-petclinic/.github/skills/build-context/ spring-petclinic/stories/
git commit -m "Harness: selective loading, clarification gate, execution audit, per-phase models, token reporting"
git push
```

Then run the **Harness Engineering Demo** workflow from the Actions tab.
The added **Copilot auth smoke test** step will confirm whether CI auth works
before the harness runs.

---

## Config knobs reference (`.harness\config.yaml`)

```yaml
# models
model: "gpt-5-mini"                 # default/fallback
phase_models:                      # per-phase override
  coding: "claude-sonnet-4.6"

# selective capability loading
selective_capability: true
repo_stacks: ["backend"]           # backend-only repo excludes angular/mobile
phase_file_scope: {...}            # which files each phase cares about
phase_skills: {...}                # which skills load per phase

# gates
min_coverage: 0                    # >0 enforces coverage
max_validation_retries: 3
validation_loopback_phase: "coding"

# story + context
story_file: "stories/current-story.md"
context_output_dir: ".github/story-context-files"
```
