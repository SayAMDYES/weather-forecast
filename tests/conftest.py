import pytest

from glow import config


@pytest.fixture(autouse=True)
def no_local_config(tmp_path, monkeypatch):
    # Tests must see the shipped defaults, whatever personal overrides exist on this machine
    monkeypatch.setattr(config, "LOCAL_CONFIG", tmp_path / "no-local.yaml")
