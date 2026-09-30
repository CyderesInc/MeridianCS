import os

import pytest


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="also run against the configured stack")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits the configured stack; runs only with --live")
    # The aggregate cache short-circuits BEFORE call() is reached, so on a machine with a real cache
    # file a fixture-driven test was served the operator's live stack instead of its fixture. Off for
    # the whole run; the cache's own tests turn it back on around a temp CFG_DIR.
    os.environ["MERIDIAN_NO_CACHE"] = "1"


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    needs_live = pytest.mark.skip(reason="pass --live to run against your stack")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(needs_live)
