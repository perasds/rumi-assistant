"""Reject accidental imports from another checkout or an editable install."""

from pathlib import Path

import rumi_market
from rumi_market.game import proto


def test_backend_imports_this_checkout_source():
    source = Path(__file__).resolve().parents[1] / "src"
    assert Path(rumi_market.__file__).resolve().is_relative_to(source)
    assert Path(proto.__file__).resolve().is_relative_to(source)
