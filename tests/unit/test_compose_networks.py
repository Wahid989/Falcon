"""Keep agent isolation separate from host-accessible storage services."""

from pathlib import Path

import yaml


def test_agents_and_storage_have_separate_networks_and_published_ports():
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    assert config["networks"]["falcon"]["internal"] is True
    assert not config["networks"]["storage"].get("internal", False)
    for name in ("commander", "follower", "follower-2", "follower-3", "follower-low"):
        assert config["services"][name]["networks"] == ["falcon"]
    for name, port in (("storage-api", "8080:8080"), ("sql-server", "1433:1433")):
        assert config["services"][name]["networks"] == ["storage"]
        assert port in config["services"][name]["ports"]
