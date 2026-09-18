import pytest

from backend.data_jobs import DataJobs


@pytest.fixture
def api_operations(tmp_path, monkeypatch):
    """API tests must never write simulated training events into the user's logs."""
    from backend import app_enhanced as api
    store = DataJobs(tmp_path / 'api_operations')
    monkeypatch.setattr(api, 'data_jobs', store)
    yield store
    store.close()
