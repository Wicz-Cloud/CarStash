# Contributing to CarStash

Thanks for your interest in contributing. CarStash is a focused tool — pull requests that keep it simple and reliable are the most welcome.

## What we're looking for

- Bug fixes and reliability improvements (especially around transfer edge cases)
- New media server adapters (see `client/media_servers.py`)
- Better ffmpeg profiles for specific hardware
- Documentation improvements
- Test coverage

## What to avoid

- Features that require the Pi to initiate outbound connections
- Adding external service dependencies beyond `flask` and `requests`
- UI rewrites — the current UI is deliberately minimal

## Adding a media server adapter

1. Create a class that extends `MediaServerAdapter` in `client/media_servers.py`
2. Implement `refresh_library() -> bool`
3. Add it to `DEFAULT_PORTS`, `get_adapter()`, and `SUPPORTED_SERVERS`
4. Add setup instructions to `docs/MEDIA_SERVERS.md`

```python
class MyServerAdapter(MediaServerAdapter):
    name = "myserver"

    def __init__(self, url: str, token: str):
        self.url   = url.rstrip("/")
        self.token = token

    def refresh_library(self) -> bool:
        # best-effort — log but don't raise
        resp = self._safe_post(f"{self.url}/scan", headers={"Authorization": self.token})
        if resp and resp.status_code == 200:
            logger.info("[myserver] Refresh triggered ✓")
            return True
        return False
```

## Development setup

```bash
git clone https://github.com/yourname/carstash.git
cd carstash
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, ruff
```

## Running tests

```bash
pytest tests/
```

## Code style

- Black-formatted, 100 char line length
- Type hints on all public functions
- Docstrings on all classes and non-trivial functions

```bash
ruff check .
black .
```

## Submitting a PR

1. Fork the repo and create a branch: `git checkout -b feature/my-thing`
2. Make your changes with tests where applicable
3. Run `ruff` and `black`
4. Open a PR with a clear description of what and why

## Reporting issues

Please include:
- CarStash version / git commit
- Server OS and Python version
- Pi model and OS
- Media server type and version
- Relevant log output (set `LOG_LEVEL=DEBUG` for verbose logs)
