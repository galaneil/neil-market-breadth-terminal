"""Round-trip matching in broker_sync: each leg goes to its own Notion row.

Run with:  python tests/test_broker_sync_legs.py   (or pytest)
The DELL fills are the real ones that exposed the bug: bought Sep 24, sold
Oct 14, bought again Oct 15, sold Oct 16.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import broker_sync as bs  # noqa: E402

DELL_FILLS = [
    {"symbol": "DELL", "quantity": 6, "price": 534.5495, "side": "BUY", "datetime": "2026-09-24 12:35:44"},
    {"symbol": "DELL", "quantity": 6, "price": 513.86, "side": "SELL", "datetime": "2026-10-14 11:59:47"},
    {"symbol": "DELL", "quantity": 10, "price": 463.78, "side": "BUY", "datetime": "2026-10-15 13:05:55"},
    {"symbol": "DELL", "quantity": 10, "price": 457.585, "side": "SELL", "datetime": "2026-10-16 14:34:56"},
]


def legs_up_to(day):
    return bs._legs_from_fills([f for f in DELL_FILLS if f["datetime"][:10] <= day])["DELL"]


def row(rid, opened, entry, closed=None):
    return {"id": rid, "page": {"id": rid, "properties": {}}, "ticker": "DELL", "opened": opened,
            "closed": closed, "entry": entry}


def kinds(actions):
    return [(a["do"], (a["leg"]["opened"] or "")[:10]) for a in actions]


def test_two_legs_in_one_sync_become_two_positions():
    # The sync did not run between Oct 14 and Oct 16: one open core row exists.
    acts = bs.plan_leg_actions(legs_up_to("2026-10-16"), [row("core", "2026-09-24", 534.5495)])
    assert kinds(acts) == [("close", "2026-09-24"), ("create", "2026-10-15")]
    assert acts[0]["leg"]["exitPrice"] == 513.86 and (acts[0]["leg"]["closed"] or "")[:10] == "2026-10-14"
    assert acts[1]["leg"]["closed"][:10] == "2026-10-16"          # created already closed, not an "add"


def test_daily_sync_never_marks_a_new_position_as_an_add():
    acts = bs.plan_leg_actions(legs_up_to("2026-10-14"), [row("core", "2026-09-24", 534.5495)])
    assert kinds(acts) == [("close", "2026-09-24")]
    # Oct 15: the core is closed in Notion, the second buy is a NEW position
    acts = bs.plan_leg_actions(legs_up_to("2026-10-15"), [row("core", "2026-09-24", 534.5495, "2026-10-14")])
    assert kinds(acts) == [("create", "2026-10-15")]
    # Oct 16: second position open in Notion, now closes
    acts = bs.plan_leg_actions(legs_up_to("2026-10-16"), [row("core", "2026-09-24", 534.5495, "2026-10-14"),
                                                          row("two", "2026-10-15", 463.78)])
    assert kinds(acts) == [("close", "2026-10-15")]


def test_a_rerun_is_a_no_op():
    rows = [row("core", "2026-09-24", 534.5495, "2026-10-14"), row("two", "2026-10-15", 463.78, "2026-10-16")]
    assert bs.plan_leg_actions(legs_up_to("2026-10-16"), rows) == []


def test_a_real_pyramid_add_still_syncs_while_the_core_is_open():
    fills = DELL_FILLS[:1] + [{"symbol": "DELL", "quantity": 4, "price": 540.0, "side": "BUY",
                               "datetime": "2026-09-30 10:00:00"}]
    legs = bs._legs_from_fills(fills)["DELL"]
    assert len(legs) == 1 and legs[0]["open"]
    acts = bs.plan_leg_actions(legs, [row("core", "2026-09-24", 534.5495)])
    assert kinds(acts) == [("adds", "2026-09-24")]


def test_old_legs_are_not_resurrected():
    # A newer row exists; an older leg with no row is history, not a new trade.
    acts = bs.plan_leg_actions(legs_up_to("2026-10-16"), [row("two", "2026-10-15", 463.78, "2026-10-16")])
    assert acts == []


def test_position_from_before_the_window_uses_the_single_leg_fallback():
    leg = bs._legs_from_fills([{"symbol": "X", "quantity": 5, "price": 10.0, "side": "BUY",
                                "datetime": "2026-10-01 10:00:00"}])["X"]
    old = {"id": "old", "properties": {}}
    acts = bs.plan_leg_actions(leg, [], open_row=old)
    assert kinds(acts) == [("adds", "2026-10-01")] and acts[0]["row"]["id"] == "old"


def test_first_ever_sighting_logs_only_the_latest_leg():
    acts = bs.plan_leg_actions(legs_up_to("2026-10-16"), [])
    assert kinds(acts) == [("create", "2026-10-15")]


def test_two_same_day_round_trips_each_find_their_own_row():
    # PLTR: a 4-minute scalp, then a second buy held overnight. The row logged is the second one.
    fills = [{"symbol": "PLTR", "quantity": 15, "price": 198.4, "side": "BUY", "datetime": "2026-10-15 11:44:00"},
             {"symbol": "PLTR", "quantity": 15, "price": 198.335, "side": "SELL", "datetime": "2026-10-15 11:45:00"},
             {"symbol": "PLTR", "quantity": 15, "price": 198.745, "side": "BUY", "datetime": "2026-10-15 11:47:00"},
             {"symbol": "PLTR", "quantity": 15, "price": 192.0, "side": "SELL", "datetime": "2026-10-16 11:05:00"}]
    legs = bs._legs_from_fills(fills)["PLTR"]
    r = {"id": "p", "page": {"id": "p", "properties": {}}, "ticker": "PLTR", "opened": "2026-10-15", "closed": None, "entry": 198.745}
    acts = bs.plan_leg_actions(legs, [r])
    assert [(a["do"], a["leg"]["exitPrice"]) for a in acts] == [("close", 192.0)]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
