"""
tests/conftest.py
Shared fixtures available to all test modules.
"""
import os
import pytest


# Set safe environment variables before any imports so Flask/app
# startup doesn't fail in CI where real config isn't present.
os.environ.setdefault("PI_IP", "192.168.1.99")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("CARSTASH_MEDIA_SERVER", "none")
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("FLASK_TESTING", "1")


@pytest.fixture(scope="session")
def app():
    """Create a Flask test app for the server."""
    from server.app import create_app          # adjust if your factory is named differently
    application = create_app({"TESTING": True, "SECRET_KEY": "test-secret-key"})
    yield application


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture()
def runner(app):
    """Flask CLI test runner."""
    return app.test_cli_runner()
