import os

import pytest


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="also run against the configured stack")
    parser.addoption("--agents", action="store_true",
                     help="also load the built package in the Gemini, Copilot and Codex CLIs on PATH")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits the configured stack; runs only with --live")
    config.addinivalue_line("markers", "agents: runs other vendors' agent CLIs; runs only with --agents")
    # The aggregate cache short-circuits BEFORE call() is reached, so on a machine with a real cache
    # file a fixture-driven test was served the operator's live stack instead of its fixture. Off for
    # the whole run; the cache's own tests turn it back on around a temp CFG_DIR.
    os.environ["MERIDIAN_NO_CACHE"] = "1"


def pytest_collection_modifyitems(config, items):
    needs_live = pytest.mark.skip(reason="pass --live to run against your stack")
    needs_agents = pytest.mark.skip(reason="pass --agents to load the package in other agents' CLIs")
    for item in items:
        if "live" in item.keywords and not config.getoption("--live"):
            item.add_marker(needs_live)
        if "agents" in item.keywords and not config.getoption("--agents"):
            item.add_marker(needs_agents)
