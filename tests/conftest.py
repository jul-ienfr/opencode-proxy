import pytest
import os

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture(autouse=True)
def _reset_disable_mapping():
    """Ensure DISABLE_MAPPING is False during tests unless explicitly set."""
    os.environ.pop("DISABLE_MAPPING", None)
    import config.settings as s
    s.DISABLE_MAPPING = False
    yield
