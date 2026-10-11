"""Live acceptance is explicit; all ordinary tests are provider-free."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", help="Run paid/real provider acceptance")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        for item in items:
            if "live" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Run explicitly with --run-live"))
