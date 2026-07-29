"""TMLE — True Market Leader Engine.

Ported from Neil's Colab notebook (Drive: Trading System/tmle) into the daily
pipeline. The factor logic, weights, bands and thresholds are his and are
carried over unchanged; what changed is WHEN they are measured — see
tmle/config.py for the calendar-year vs trailing-window note.
"""
