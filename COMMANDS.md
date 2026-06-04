# FI-PR-GENERATOR — Terminal Commands Reference

All commands are run from the project root directory.
Always activate your virtual environment first if you use one.

---

## Quick Start (First Time)

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill your API keys
copy .env.example .env
# Then edit .env with your actual keys

# 3. Verify your setup (checks all API keys)
python main.py --help

# 4. Add a target repo to config/orgs.json (see README for format)
# At minimum: {"orgs": [{"name": "your-org", "repos": [{"name": "your-repo"}]}]}
```

---

## Main CLI — `python main.py`

### `build-memory` — Analyze a repository

Fetches 50 merged PRs, extracts conventions, detects contribution workflow.
Takes 1–2 minutes. Run this BEFORE `run` or `scan-orgs` on a new repo.

```powershell
# Basic (uses existing memory if it exists)
python main.py build-memory --org GSSoC-ExtD --repo my-repo

# Force rebuild even if memory already exists
python main.py build-memory --org GSSoC-ExtD --repo my-repo --force
```

---

### `scan-orgs` — Preview scored issues (SAFE — no writes)

Shows all open issues with scores. Good for checking what the system sees
before running the full pipeline. NO GitHub writes, NO coding.

```powershell
# Show ALL open issues (all labels, sorted by score)
python main.py scan-orgs

# Show only issues scoring >= 70
python main.py scan-orgs --min-score 70

# Limit to one org
python main.py scan-orgs --org GSSoC-ExtD

# Limit to one specific repo
python main.py scan-orgs --org GSSoC-ExtD --repo my-repo

# Combined: one repo, show all scores
python main.py scan-orgs --org GSSoC-ExtD --repo my-repo --min-score 0
```

**Output columns:**
```
[GO] #123 [85.5]  Fix navbar overlap on mobile         ← score >= 75: ready
[OK] #456 [67.2]  Update README typo                   ← score 60-75: ok but review
[--] #789 [42.0]  Refactor entire authentication       ← score < 60: skip
       Labels: (no labels) | Clarity:72 Scope:60 | https://github.com/...
```

---

### `run` — Execute the full 10-step pipeline

Runs the entire pipeline on one repo. Always use `--dry-run` first.

```powershell
# DRY RUN (no GitHub writes, no ntfy approval) — TEST THIS FIRST
python main.py run --org GSSoC-ExtD --repo my-repo --dry-run

# Full run (will send ntfy notification and wait for your approval)
python main.py run --org GSSoC-ExtD --repo my-repo

# Work on a specific issue (skip automatic issue scoring)
python main.py run --org GSSoC-ExtD --repo my-repo --issue 42

# Specific issue + dry run
python main.py run --org GSSoC-ExtD --repo my-repo --issue 42 --dry-run
```

**What happens in full run:**
1. Checks repo activity score
2. Scores all open issues and picks the best one
3. Checks if you're assigned (posts comment if not → run again after assignment)
4. Clones repo and creates branch
5. Runs tests before coding (records preexisting failures)
6. Generates patch (Gemini 2.5 Pro → fallbacks)
7. Runs reviewer (Llama 3.3 70B)
8. Computes risk score
9. **Sends ntfy notification to your phone — WAITS for you to tap Approve/Reject**
10. Rebases, runs tests again, pushes branch, creates draft PR

---

### `list-states` — View pipeline run history

```powershell
# List last 20 runs
python main.py list-states

# Show only blocked runs (waiting for assignment)
python main.py list-states --status blocked

# Show only completed runs
python main.py list-states --status completed

# Show only failed runs
python main.py list-states --status failed

# Show runs waiting for approval (sent to phone)
python main.py list-states --status waiting_approval
```

---

## Scheduler — `python scheduler.py`

Keeps org memory fresh automatically. Run this as a background service.

```powershell
# Start daily refresh daemon (refreshes all repos at 02:00 UTC)
python scheduler.py

# Run a one-time refresh now and exit
python scheduler.py --once

# Refresh every 6 hours instead of daily
python scheduler.py --interval-hours 6

# Refresh at 08:00 UTC daily
python scheduler.py --hour 8
```

---

## Tests — `python -m pytest`

```powershell
# Run all offline unit tests (no API keys needed)
python -m pytest tests/test_scorer.py tests/test_ntfy.py -v

# Run LLM smoke tests (needs API keys in .env)
python -m pytest tests/test_llm_endpoints.py -v -s

# Run ntfy unit tests only (no API keys needed)
python -m pytest tests/test_ntfy.py -v -s

# Run the real ntfy notification test (needs NTFY_TOPIC in .env)
python -m pytest tests/test_ntfy.py::TestNtfyNotification -v -s

# Run all tests
python -m pytest tests/ -v

# Run tests and show print output (for LLM tests)
python -m pytest tests/ -v -s

# Run one specific test
python -m pytest tests/test_llm_endpoints.py::TestGroqEndpoints::test_llama_8b_instant -v -s

# Run with extra debug output
python -m pytest tests/ -v -s --tb=long

# Skip slow/live tests (only run offline unit tests)
python -m pytest tests/ -v -k "not TestNtfyNotification and not TestGroqEndpoints and not TestGemini and not TestOpenRouter and not TestAnthropic and not TestGitHub"
```

**Test categories:**
```
test_scorer.py           — 15 offline unit tests (scoring formulas)
test_ntfy.py             — Flask server + file I/O (offline) + real send (needs NTFY_TOPIC)
test_llm_endpoints.py    — Live API smoke tests (auto-skipped if key not set)
```

---

## Logging

Logs are structured JSON in `logs/` and printed to console.

```powershell
# Run with verbose debug logging
set LOG_LEVEL=DEBUG && python main.py scan-orgs

# Run with minimal output
set LOG_LEVEL=WARNING && python main.py run --org myorg --repo myrepo

# Default level
set LOG_LEVEL=INFO && python main.py run --org myorg --repo myrepo
```

---

## ntfy / Phone Approval Setup

```powershell
# Check the approval server is running (it auto-starts when pipeline runs)
curl http://localhost:8080/health

# Manually approve a run (simulates phone tap)
curl -X POST http://localhost:8080/approve/run_20240601_120000_abc123

# Manually reject a run
curl -X POST http://localhost:8080/reject/run_20240601_120000_abc123

# Test notification send (runs ntfy test, requires NTFY_TOPIC)
python -m pytest tests/test_ntfy.py::TestNtfyNotification::test_send_notification_to_phone -v -s

# Start ngrok tunnel for phone approval (requires ngrok installed)
# ngrok.com → download → put ngrok.exe in PATH
ngrok http 8080
# Then set APPROVAL_SERVER_URL=https://xxxx.ngrok.io in your .env
```

---

## Config Files

| File | Purpose |
|---|---|
| `.env` | All API keys and settings |
| `config/orgs.json` | Repos to scan |
| `config/models.json` | LLM chain config |
| `memory_store/` | Per-repo org memory JSON |
| `state/` | Pipeline run states |
| `diffs/` | Generated patch diffs |
| `repos/` | Cloned repositories |

---

## Common Workflows

### Check if Gemini API key works
```powershell
python -m pytest tests/test_llm_endpoints.py::TestGeminiEndpoints -v -s
```

### Check all API keys at once
```powershell
python -m pytest tests/test_llm_endpoints.py -v -s
```

### Add a new repo and do a full dry run
```powershell
# 1. Edit config/orgs.json to add your repo
# 2. Build memory
python main.py build-memory --org your-org --repo your-repo
# 3. Check what issues it found
python main.py scan-orgs --org your-org --repo your-repo
# 4. Dry run on best issue
python main.py run --org your-org --repo your-repo --dry-run
```

### Debug why a specific issue scored low
```powershell
# Run scan with --min-score 0 to see all issues
python main.py scan-orgs --org your-org --repo your-repo --min-score 0
# Then run on that specific issue number
python main.py run --org your-org --repo your-repo --issue 123 --dry-run
```
