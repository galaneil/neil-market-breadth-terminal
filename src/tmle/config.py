"""
TMLE — config.py

Single source of truth for the engine's constants: weights, thresholds, bands,
benchmarks and the seeded validation set. Nothing here computes anything.

Ported from the Colab version. Every weight, band and threshold is unchanged —
the numbers below are Neil's calibration and should only move deliberately.

WHAT CHANGED IN THE PORT: the measurement window.
---------------------------------------------------------------------------
The notebook scored by CALENDAR YEAR. A trailing year was tried next and was no
better: both are FIXED windows, and a fixed window is dominated by the past, so
it keeps scoring a stock as a leader long after its move has ended. Measured on
2026-07-29, SNDK read +3451% over the trailing year while sitting 45% off a
high it had made five weeks earlier — a maximum leadership score on a broken
stock. The mirror failure is just as bad: NVDA's real advance ran Oct-2022 to
Jan-2025, which no calendar year and no trailing year contains.

Factors are now measured over the MOVE — from where the advance began, defined
the Weinstein way as the last close below the 30-week average — and the move's
health is reported separately as a stage. See structure.py.
"""

# ── Measurement windows (trading days) ─────────────────────────────────────
# Retained as the normalisation scale for rate-based bands, NOT as the window
# factors are measured over — that is now the episode. See structure.py.
TRAILING_DAYS = 252
F5_MOMENTUM_DAYS = 63        # ~3 months: matches the notebook's 3-month rank lookback

# How far back to backfill scores on a first run, and how often. Scoring every
# session for every name is wasted work — leadership does not turn over daily,
# and a weekly point is enough to read a trajectory. Backfilling means the
# trajectory charts are useful immediately instead of in six months.
BACKFILL_DAYS = 252
BACKFILL_EVERY = 5           # one checkpoint per trading week

# ── Benchmark ──────────────────────────────────────────────────────────────
# F1 scores against the Nasdaq 100 ONLY; the others are context. Kept as QQQ
# rather than switched to the ^IXIC series the terminal already holds, because
# F1_RS_SCALE below was calibrated against QQQ and swapping the benchmark would
# silently move every F1 score.
PRIMARY_BENCHMARK = "QQQ"

# ── Composite weights (must sum to 1.00) ───────────────────────────────────
# Rebalanced to make room for F6. The three fundamental factors now carry 0.45
# between them (trailing quality, quarterly excellence, forward consensus),
# price 0.15, structure 0.20, theme 0.20. F1 came down from 0.20 because stage
# and drawdown already gate on price separately, so weighting price heavily
# inside the score as well double-counts the same information.
WEIGHTS = {
    "F1": 0.15,   # Price Leadership (the RS rating)
    "F2": 0.15,   # Fundamental Quality      — reported, trailing
    "F2B": 0.15,  # Fundamental Excellence   — reported, quarterly
    "F6": 0.15,   # Forward Growth           — consensus, LEADING
    "F4": 0.10,   # Price Structure
    "F4B": 0.10,  # Volume Behavior
    "F5": 0.20,   # Theme Alignment
    # "F3": deferred — Institutional Support
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"

# Factors live today. The composite renormalises over whatever is available, so
# adding F2/F2B later raises coverage without rescaling anything already stored.
ACTIVE_FACTORS = ["F1", "F2", "F2B", "F6", "F4", "F4B", "F5"]

# A score built on too little of the weight is not a score. All six factors are
# live, so the full weight is 1.00; this sits low enough that one small factor
# (F4B, when a name has no volume data) can drop out without disqualifying it.
MIN_COVERAGE = 0.85

# ── F1 — Price Leadership ──────────────────────────────────────────────────
F1_RS_SCALE = 200.0   # RS of +200 -> 100; RS of -100 -> 0

# ── F2 — Fundamental Quality (annual) 40/30/30 ─────────────────────────────
F2_REVENUE_BANDS = [(50, 40), (25, 30), (15, 20), (5, 11), (0, 4)]
F2_EPS_BANDS = [(100, 30), (50, 24), (25, 18), (10, 11), (0, 5)]
F2_MARGIN_BANDS = [(30, 30), (20, 23), (10, 15), (0, 6)]

# ── F2B — Fundamental Excellence (quarterly) ───────────────────────────────
F2B_TRIPLE_DIGIT_THRESHOLD = 100.0
F2B_TRIPLE_SCORE = {4: 100, 3: 80, 2: 55, 1: 30, 0: 0}
F2B_ACCEL_SCORE = {3: 100, 2: 70, 1: 40, 0: 0}
F2B_TRIPLE_WEIGHT = 0.50
F2B_ACCEL_WEIGHT = 0.50

# ── F6 — Forward Growth (next fiscal year consensus) ─────────────────────
# Triple-digit projected growth takes the top band, mirroring F2B's treatment
# of triple-digit reported quarters.
F6_REVENUE_BANDS = [(100, 50), (50, 40), (30, 30), (15, 20), (5, 10)]
F6_EPS_BANDS = [(100, 50), (50, 40), (30, 30), (15, 20), (5, 10)]

# ── F4 — Price Structure ───────────────────────────────────────────────────
F4_MA_WINDOW = 50
F4_TREND_MAX = 35
# Bands are a COUNT of new-high sessions per 252 traded, so they keep meaning
# what they meant in the notebook even though episodes vary in length.
F4_NEWHIGH_BANDS = [(40, 35), (25, 27), (15, 18), (5, 10), (1, 4)]
F4_MA_DISCIPLINE_MAX = 30
# Discipline is Neil's test for a clean leader: "did not break the 20 more than
# twice, did not break the 50 at all". Expressed as the share of the advance
# spent below each average, with the tolerance at which the component reaches
# zero. The 10-week is the serious one and carries most of the weight; the
# 20-day is allowed to wobble.
F4_BREAK_10W_TOLERANCE = 20.0   # % of episode below the 10-week -> 0 credit
F4_BREAK_20_TOLERANCE = 40.0    # % of episode below the 20-day  -> 0 credit
F4_10W_SHARE = 0.60
F4_20_SHARE = 0.40

# ── F4B — Volume Behavior ──────────────────────────────────────────────────
F4B_ADR_WINDOW = 20
F4B_BIG_DAY_ADR_MULT = 2.0
F4B_UD_BANDS = [(2.0, 100), (1.5, 85), (1.25, 70), (1.0, 50), (0.8, 30)]
F4B_UD_WEIGHT = 0.50
F4B_BIGUP_WEIGHT = 0.25
F4B_BIGDOWN_WEIGHT = 0.25
F4B_VOL_AVG_WINDOW = 50

# ── F5 — Theme & Sector Alignment ──────────────────────────────────────────
F5_CURRENT_WEIGHT = 0.40
F5_MOMENTUM_WEIGHT = 0.60
F5_MOMENTUM_BANDS = [(20, 100), (10, 85), (5, 70), (1, 60),
                     (-5, 50), (-10, 35), (-20, 20)]
F5_NO_DATA_SCORE = 30.0

# ── Universe gate ──────────────────────────────────────────────────────────
# Below this the "leaders" list fills with micro-caps whose 300% move is a
# liquidity artefact rather than leadership.
MIN_MARKET_CAP = 2e9

# Only a Stage 2 name is actionable. Stage is never folded into the composite —
# it decides whether the composite is worth acting on, so a broken ex-leader
# keeps the strength score it genuinely earned and simply never appears in a
# buy list. A drawdown past this from the episode high is treated as damage
# even if the 30-week has not rolled over yet.
# A name with no reported revenue is not actionable at any rank. Scoring F2 as
# zero demotes it but does not remove it, and MAAS — a company with no published
# financials at all — still reached 61st of 878 on price and theme alone. A
# leader we cannot verify has a business is not a leader worth buying, so this
# is a gate rather than a score adjustment. It stays SCORED, so the history is
# intact and the name is still visible under "everything scored".
REQUIRE_REVENUE = True

ACTIONABLE_STAGES = [2]
# Neil's call: a leader is allowed a 25% pullback mid-advance and no more.
# This does the work the stage cannot — the 30-week average is slow, and five
# weeks after SNDK topped it still read "Advancing" while the stock was 55%
# down. Drawdown catches a fresh break in weeks; stage confirms it in months.
MAX_ACTIONABLE_DRAWDOWN = -25.0

# TWO RANKINGS, ONE SCORING PASS.
#
# The full composite answers "what can I buy today" and is gated on price
# structure, so in a tape where growth is being liquidated it SHOULD come back
# nearly empty. That emptiness is information, not a failure.
#
# But the fundamentals and theme of a broken group do not vanish with its price.
# Memory and optics still have exceptional numbers today; they are simply
# unbuyable. So a second ranking drops the two price factors entirely and asks
# "where is the business quality and thematic strength", which is the list that
# tells you what to be ready for when price repairs — and, run over history, what
# the next NVDA looked like before it moved.
# F6 belongs here above all: the forward list is the one meant to surface a
# theme BEFORE its price confirms.
THEME_FACTORS = ["F2", "F2B", "F6", "F5", "F4B"]

# How many names the stored leaderboard keeps per checkpoint. The full universe
# is scored; only the top slice is worth accumulating forever.
LEADERBOARD_SIZE = 250

# ── Validation set (Neil's seeded true market leaders) ─────────────────────
SEEDED_TMLS = {
    2021: ["NVDA", "AMD", "FTNT"],
    2022: ["OXY", "DVN", "SMCI"],
    2023: ["NVDA", "AMD", "SMCI", "META", "AAOI"],
    2024: ["NVDA", "PLTR", "CLS", "GEV"],
    2025: ["MU", "WDC", "STX", "AMD"],
    2026: ["MU", "WDC", "AAOI", "AMD", "INTC"],
}

THEMES = {
    2021: "Reopening / semis",
    2022: "Bear market / energy + early AI",
    2023: "AI compute / GPU",
    2024: "AI infrastructure / power",
    2025: "Memory supercycle",
    2026: "Memory + optical interconnects",
}

FACTOR_LABELS = {
    "F1": "Price Leadership",
    "F2": "Fundamental Quality",
    "F2B": "Fundamental Excellence",
    "F4": "Price Structure",
    "F4B": "Volume Behavior",
    "F5": "Theme Alignment",
    "F6": "Forward Growth",
}

FACTOR_BLURBS = {
    "F1": "RS rating — how it is performing against the market right now",
    "F2": "revenue growth, EPS growth and operating margin",
    "F2B": "triple-digit quarters and whether growth is accelerating",
    "F4": "new highs, and whether it held its 20-day and 10-week while advancing",
    "F4B": "up/down volume and whether big moves came on volume",
    "F5": "its industry's rank, and whether that rank is climbing",
    "F6": "next year's consensus revenue and EPS growth — the only forward-looking factor",
}
