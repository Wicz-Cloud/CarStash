# CarStash CI/CD Pipeline

GitHub Actions workflow + test scaffolding for the CarStash project.

---

## Files to add to your repo

```
CarStash/
├── .github/
│   └── workflows/
│       └── ci.yml          ← the pipeline
├── tests/
│   ├── conftest.py         ← shared fixtures + env setup
│   ├── test_queue.py       ← unit tests: sync queue
│   ├── test_dispatcher.py  ← unit tests: heartbeat + resumable push
│   └── test_media_servers.py ← unit tests: Plex/Jellyfin/Emby/Kodi adapters
└── pyproject.toml          ← pytest / coverage / black / isort config
```

---

## Pipeline stages

| Job | What it does | Blocks merge? |
|---|---|---|
| **lint** | flake8 (errors + style), black (formatting), isort (imports) | ✅ Yes |
| **security** | bandit (static analysis), safety (CVE scan on requirements.txt) | ✅ Yes (bandit) / ⚠️ advisory (safety) |
| **test** | pytest on Python 3.10, 3.11, 3.12 with coverage report | ✅ Yes |
| **integration** | Spins up Flask server, hits `/health` endpoint | ✅ Yes |
| **build-gate** | Final aggregated status — the one to require on branch protection | ✅ Yes |

---

## One-time setup

### 1. Add the `/health` endpoint to your server

The integration smoke test hits `GET /health`. Add this to `server/app.py`:

```python
@app.route("/health")
def health():
    return {"status": "ok"}, 200
```

### 2. Enable branch protection (recommended)

In **Settings → Branches → Add rule** for `main`:
- ✅ Require status checks to pass before merging
- Search for and enable: **Build Gate**
- ✅ Require branches to be up to date before merging

### 3. Optional: Codecov

If you want coverage badges and PR coverage diffs:
1. Sign up at https://codecov.io with your GitHub account
2. Add `CODECOV_TOKEN` to **Settings → Secrets → Actions**
3. The workflow already includes the upload step — it will activate automatically

---

## Running tests locally

```bash
# Install test deps
pip install -r requirements.txt
pip install pytest pytest-cov pytest-mock

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=server --cov=client --cov-report=term-missing

# Run just one module
pytest tests/test_queue.py -v
```

---

## Running linters locally

```bash
pip install flake8 black isort

flake8 server/ client/ --max-line-length=120
black --check --line-length 120 server/ client/
isort --check-only --profile black server/ client/

# Auto-fix formatting
black --line-length 120 server/ client/
isort --profile black server/ client/
```

---

## Notes on the test scaffold

The tests in `tests/` are **stubs** — they define the expected interface (method names, return types, error handling) based on the architecture described in the README. They will fail until the corresponding server/client modules are implemented, which is exactly the point: **write the code to make the tests green**.

Tests are intentionally written against the public interface, not implementation details, so they stay useful even as internals change.
