from glow import config


def test_local_config_merges_nested_keys_and_replaces_lists(tmp_path, monkeypatch):
    local = tmp_path / "local.yaml"
    local.write_text("server: {port: 9000}\nforecast: {events: [sunset]}\n", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_CONFIG", local)
    cfg = config.load_config()
    assert cfg["server"]["port"] == 9000 and cfg["server"]["host"] == "127.0.0.1"
    assert cfg["forecast"]["events"] == ["sunset"] and cfg["forecast"]["days"] == 3


def test_local_config_ignored_for_explicit_config_file(tmp_path, monkeypatch):
    local = tmp_path / "local.yaml"
    local.write_text("server: {port: 9000}\n", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_CONFIG", local)
    other = tmp_path / "other.yaml"
    other.write_text(config.DEFAULT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    assert config.load_config(other)["server"]["port"] == 8765
