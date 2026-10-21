import json
from pathlib import Path
import tempfile

import pytest


def pytest_addoption(parser):
    parser.addoption("--external-materials", action="store_true", default=False,
                     help="Run checks requiring separately supplied research evidence or game assets")
    parser.addoption("--external-materials-root", default=None,
                     help="Material tree containing docs/, artifacts/, and data/ (default: project/external)")


def pytest_configure(config):
    # The run path does not exist; pytest can never clear another run's files.
    if config.option.basetemp is None:
        config.option.basetemp = str(Path(tempfile.mkdtemp(prefix="rumi-assistant-pytest-")) / "run")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--external-materials"):
        return
    marker = pytest.mark.skip(reason="External research/game materials excluded; opt in with --external-materials")
    for item in items:
        if item.get_closest_marker("external_materials") is not None:
            item.add_marker(marker)


@pytest.fixture
def external_materials_root(request):
    supplied = request.config.getoption("--external-materials-root")
    return (Path(supplied).expanduser().resolve() if supplied
            else Path(__file__).resolve().parents[2] / "external")


@pytest.fixture
def catalog_path(tmp_path):
    path = tmp_path / "pets" / "catalog.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"pets": [
        {"pet_id": "101", "name": "蜜桃", "name_verified": True,
         "aliases": ["桃桃"], "sources": ["test fixture"]},
        {"pet_id": "102", "name": "蜜桃", "name_verified": True,
         "sources": ["test fixture"]},
        {"pet_id": "103", "name": "猜测蜜桃", "name_verified": False},
        {"pet_id": "104", "name": None},
    ]}), encoding="utf-8")
    return path
