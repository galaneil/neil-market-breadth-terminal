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
    # the quick scalp has no row of its own, so it is created as its own trade; the real buy is closed at 192.0
    assert [(a["do"], a["leg"]["exitPrice"]) for a in acts] == [("create", 198.335), ("close", 192.0)]


def test_sell_bigger_than_the_window_leaves_flat_so_the_next_buy_is_its_own_trade():
    # TWLO: 4 seen bought, 17 sold (13 were held before the report window), then a fresh buy of 10 sold in two lots.
    fills = [{"symbol": "TWLO", "quantity": 4, "price": 282.98, "side": "BUY", "datetime": "2026-09-28 12:40:48"},
             {"symbol": "TWLO", "quantity": 17, "price": 277.4, "side": "SELL", "datetime": "2026-10-14 11:59:47"},
             {"symbol": "TWLO", "quantity": 10, "price": 266.9995, "side": "BUY", "datetime": "2026-10-21 09:53:11"},
             {"symbol": "TWLO", "quantity": 5, "price": 252.82, "side": "SELL", "datetime": "2026-10-22 14:05:04"},
             {"symbol": "TWLO", "quantity": 5, "price": 252.82, "side": "SELL", "datetime": "2026-10-22 14:05:04"}]
    legs = bs._legs_from_fills(fills)["TWLO"]
    assert [(l["opened"][:10], (l["closed"] or "")[:10]) for l in legs] == [("2026-09-28", "2026-10-14"), ("2026-10-21", "2026-10-22")]
    assert legs[1]["exitPrice"] == 252.82 and legs[1]["shares"] == 10


def test_a_same_day_scalp_with_no_row_of_its_own_is_created():
    # PLTR 15 Oct: a 15-share scalp (one minute) and then the real 15-share buy; only the real trade has a row.
    fills = [{"symbol": "PLTR", "quantity": 15, "price": 198.4, "side": "BUY", "datetime": "2026-10-15 11:44:00"},
             {"symbol": "PLTR", "quantity": 15, "price": 198.335, "side": "SELL", "datetime": "2026-10-15 11:45:00"},
             {"symbol": "PLTR", "quantity": 15, "price": 198.745, "side": "BUY", "datetime": "2026-10-15 11:47:00"},
             {"symbol": "PLTR", "quantity": 15, "price": 192.0, "side": "SELL", "datetime": "2026-10-16 11:05:00"}]
    legs = bs._legs_from_fills(fills)["PLTR"]
    big = {"id": "p", "page": {"id": "p", "properties": {}}, "ticker": "PLTR", "opened": "2026-10-15", "closed": "2026-10-16", "entry": 198.745}
    acts = bs.plan_leg_actions(legs, [big])
    assert [(a["do"], round(a["leg"]["entryPrice"], 2)) for a in acts] == [("create", 198.4)]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
