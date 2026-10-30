"""The radar against Python's own call binding (see tests/radar_oracle_lib.py for what is and is not counted)."""

from tests import radar_oracle_lib as lib
from upkeep.apidiff import Change


def rates(result):
    s = result["stats"]
    recall = s["caught"] / s["breaks"]
    precision = 1 - s["hard_not_breaking"] / s["flagged_hard"]
    return recall, precision, s


def test_every_call_that_stops_binding_is_flagged_and_hard_flags_are_real():
    for seed in (11, 12):
        recall, precision, s = rates(lib.run(400, seed=seed))
        assert s["calls"] > 1400 and s["breaks"] > 300            # the generator really produced broken calls
        assert recall == 1.0, lib.run(400, seed=seed)["missed"][:3]  # a miss is a radar bug
        assert precision >= 0.99


def test_the_oracle_would_notice_a_broken_radar(monkeypatch):
    """Mutation check: if the radar ignored removed parameters, recall must fall visibly."""
    real = lib.site_affected
    monkeypatch.setattr(lib, "site_affected", lambda c, site, o, n: False if c.kind == "param_removed" else real(c, site, o, n))
    recall, _, _ = rates(lib.run(300, seed=11))
    assert recall < 0.9


def test_a_radar_that_flags_everything_has_poor_precision(monkeypatch):
    monkeypatch.setattr(lib, "site_affected", lambda c, site, o, n: True)
    _, precision, _ = rates(lib.run(300, seed=11))
    assert precision < 0.7


def test_default_changes_never_break_binding_but_are_reported_separately():
    s = lib.run(300, seed=13)["stats"]
    assert s["soft_only"] > 0      # calls flagged only because a default changed
    assert s["uncertain"] > 0      # values moved into *args/**kwargs are reported, not silently counted
