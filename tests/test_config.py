from scout.config import load_config


def test_private_overrides_public(tmp_path):
    (tmp_path / "scout.config.example.toml").write_text(
        'enabled = true\n[profile]\nname = "Default"\n[paths]\ndb = "scout.db"\n'
    )
    (tmp_path / "scout.config.toml").write_text('[profile]\nname = "Alex"\n')
    cfg = load_config(public=tmp_path / "scout.config.example.toml",
                      private=tmp_path / "scout.config.toml")
    assert cfg["profile"]["name"] == "Alex"      # private wins
    assert cfg["enabled"] is True                # public default preserved


def test_missing_private_is_ok(tmp_path):
    (tmp_path / "scout.config.example.toml").write_text('enabled = false\n[paths]\ndb="x"\n')
    cfg = load_config(public=tmp_path / "scout.config.example.toml",
                      private=tmp_path / "nope.toml")
    assert cfg["enabled"] is False
