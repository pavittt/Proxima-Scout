#!/usr/bin/env python3
"""
build_translation_model.py
==========================
Loads Big-5 FBref standard + shooting CSVs, finds players who moved
between leagues across consecutive seasons, and computes empirical
per-stat translation ratios for each league pair.

Algorithm
---------
1. Load all *_standard.csv and *_shooting.csv from fbref_data/
2. Filter: non-GK players with >= MIN_90S 90s played in a season
3. For each player, find consecutive-season appearances in DIFFERENT leagues
4. For each such transfer: ratio_stat = stat_in_B / stat_in_A
5. Aggregate ratios per (source_league, dest_league) pair
6. Output: fbref_data/translation_curves.json

Usage
-----
    python build_translation_model.py            # normal run
    python build_translation_model.py --verbose  # print every transfer found
"""

import csv
import glob
import io
import json
import os
import sys
import statistics
from collections import defaultdict, Counter

# Force UTF-8 output on Windows so box-drawing chars and special names print cleanly
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Configuration ─────────────────────────────────────────────────────────────

ROOT      = os.path.dirname(os.path.abspath(__file__))
FBREF_DIR = os.path.join(ROOT, 'fbref_data')
OUT_PATH  = os.path.join(FBREF_DIR, 'translation_curves.json')

VERBOSE   = '--verbose' in sys.argv

# Minimum 90s played in a season to be included (avoids injury cameos / cup noise)
MIN_90S_SEASON = 10.0   # ≈ 900 minutes / ~10 full games

# Minimum per-stat samples before we trust the ratio (too few = noisy)
MIN_SAMPLES = 3

# Minimum 90s played (season before) to count the ratio — filters loan returnees
# who only got a handful of games in the source league
MIN_90S_SOURCE = 12.0

# Map FBref's league_ field → friendly name matching server.py's TRANSLATION_CURVES
LEAGUE_MAP = {
    'ENG-Premier League': 'Premier League',
    'ESP-La Liga':        'La Liga',
    'GER-Bundesliga':     'Bundesliga',
    'ITA-Serie A':        'Serie A',
    'FRA-Ligue 1':        'Ligue 1',
}

# Seasons in chronological order (FBref compact notation)
SEASON_ORDER = ['2223', '2324', '2425']

# Human-readable season labels for printing
SEASON_LABEL = {'2223': '2022/23', '2324': '2023/24', '2425': '2024/25'}

# ── Stats we track ─────────────────────────────────────────────────────────────
# Maps CSV column → output stat key
STANDARD_STAT_COLS = {
    'Per 90 Minutes_Gls': 'goals_per90',
    'Per 90 Minutes_Ast': 'assists_per90',
    'Per 90 Minutes_G+A': 'g_a_per90',
}
SHOOTING_STAT_COLS = {
    'Standard_Sh/90':  'shots_per90',
    'Standard_SoT/90': 'shots_on_target_per90',
}
ALL_STATS = list(STANDARD_STAT_COLS.values()) + list(SHOOTING_STAT_COLS.values())

# Threshold below which a per-90 value is too small to divide by safely
# (e.g. a fullback with 0.01 goals/90 creates noisy ratios)
MIN_BASE_VALUE = {
    'goals_per90':              0.05,
    'assists_per90':            0.04,
    'g_a_per90':                0.08,
    'shots_per90':              0.40,
    'shots_on_target_per90':    0.15,
}
DEFAULT_MIN_BASE = 0.03

# Ratio outlier bounds — anything outside is treated as noise/data error
RATIO_LO, RATIO_HI = 0.08, 4.0


# ── Data Loading ──────────────────────────────────────────────────────────────

def _safe_float(v):
    """Parse float; return None on failure."""
    try:
        f = float(v)
        return f if f == f else None   # guard NaN
    except (TypeError, ValueError):
        return None


def _load_csv_files(glob_pattern, col_map):
    """
    Load CSV files matching glob_pattern.
    Returns dict: (player, league, season) → {stat_key: float, '_90s': float}
    Multi-entry players (mid-season transfers) are 90s-weighted averaged.
    """
    records = {}

    for path in sorted(glob.glob(glob_pattern)):
        with open(path, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                player = row.get('player_', '').strip()
                league = row.get('league_', '').strip()
                season = row.get('season_', '').strip()
                pos    = row.get('pos_', '').strip().upper()

                if not player or not league or not season:
                    continue
                if league not in LEAGUE_MAP:
                    continue
                if 'GK' in pos:
                    continue

                # Get 90s played (column name differs between standard and shooting)
                ninety_s = _safe_float(
                    row.get('Playing Time_90s') or row.get('90s_') or '0'
                )
                if ninety_s is None or ninety_s < MIN_90S_SEASON:
                    continue

                key = (player, league, season)

                if key not in records:
                    records[key] = {'_90s': ninety_s, '_rows': 1}

                for col, stat_key in col_map.items():
                    val = _safe_float(row.get(col))
                    if val is None:
                        continue
                    existing = records[key]
                    if stat_key in existing:
                        # Weighted average over multiple rows (mid-season loan, etc.)
                        prev_90s = existing['_90s']
                        new_90s  = prev_90s + ninety_s
                        existing[stat_key] = (
                            existing[stat_key] * prev_90s + val * ninety_s
                        ) / new_90s
                    else:
                        existing[stat_key] = val

                # Update 90s to the maximum seen (another row might be the full-season entry)
                records[key]['_90s'] = max(records[key]['_90s'], ninety_s)
                records[key]['_rows'] += 1

    return records


def load_all_records():
    """Merge standard + shooting records into a single dict."""
    print('── Loading FBref standard CSVs …')
    std = _load_csv_files(os.path.join(FBREF_DIR, '*_standard.csv'), STANDARD_STAT_COLS)
    print(f'   {len(std):,} records  (≥{MIN_90S_SEASON} 90s, non-GK, Big-5 only)')

    print('── Loading FBref shooting CSVs …')
    sht = _load_csv_files(os.path.join(FBREF_DIR, '*_shooting.csv'), SHOOTING_STAT_COLS)
    print(f'   {len(sht):,} records')

    # Merge: standard keys are authoritative; overlay shooting stats
    merged = dict(std)
    for key, stats in sht.items():
        if key in merged:
            for k, v in stats.items():
                if k not in merged[key]:
                    merged[key][k] = v
        # (if key only in shooting but not standard, skip — no 90s baseline)

    print(f'   {len(merged):,} total records after merge\n')
    return merged


# ── Transfer Detection ────────────────────────────────────────────────────────

def find_transfers(records):
    """
    Identify players who appeared in two DIFFERENT leagues in two CONSECUTIVE seasons.
    Returns a list of transfer dicts.
    """
    # Build per-player timeline: player → sorted list of (season_idx, season, league)
    timeline = defaultdict(list)
    for (player, league, season) in records:
        if season in SEASON_ORDER:
            idx = SEASON_ORDER.index(season)
            timeline[player].append((idx, season, league))

    transfers = []
    for player, entries in timeline.items():
        entries.sort(key=lambda x: x[0])

        # De-duplicate: if player appears in two teams in same league/season, pick first
        seen = {}
        deduped = []
        for idx, season, league in entries:
            k = (idx, league)
            if k not in seen:
                seen[k] = True
                deduped.append((idx, season, league))

        for i in range(len(deduped) - 1):
            idx_a, s_a, lg_a = deduped[i]
            idx_b, s_b, lg_b = deduped[i + 1]

            # Must be consecutive seasons
            if idx_b != idx_a + 1:
                continue
            # Must be different leagues
            if lg_a == lg_b:
                continue

            key_a = (player, lg_a, s_a)
            key_b = (player, lg_b, s_b)
            if key_a not in records or key_b not in records:
                continue

            stats_a = records[key_a]
            stats_b = records[key_b]

            # Minimum 90s in source league (avoids short loan stints distorting ratios)
            if stats_a.get('_90s', 0) < MIN_90S_SOURCE:
                continue

            transfers.append({
                'player':    player,
                'from_lg':   lg_a,
                'to_lg':     lg_b,
                'from_name': LEAGUE_MAP[lg_a],
                'to_name':   LEAGUE_MAP[lg_b],
                'season_a':  s_a,
                'season_b':  s_b,
                '90s_a':     stats_a.get('_90s', 0),
                '90s_b':     stats_b.get('_90s', 0),
                'stats_a':   stats_a,
                'stats_b':   stats_b,
            })

    return transfers


# ── Ratio Calculation ─────────────────────────────────────────────────────────

def compute_ratios(transfers):
    """
    For each transfer compute ratio = stat_b / stat_a per stat.
    Groups into: pair_key → {stat → [ratios]}
    Also returns pair_key → [player names].
    """
    pair_ratios  = defaultdict(lambda: defaultdict(list))
    pair_players = defaultdict(list)

    for t in transfers:
        pair_key = f"{t['from_name']} → {t['to_name']}"
        pair_players[pair_key].append(t['player'])

        if VERBOSE:
            label_a = SEASON_LABEL.get(t['season_a'], t['season_a'])
            label_b = SEASON_LABEL.get(t['season_b'], t['season_b'])
            print(
                f"    {t['player']:<28}  "
                f"{t['from_name']:<18} ({label_a}, {t['90s_a']:.0f}×90)"
                f"  →  {t['to_name']:<18} ({label_b}, {t['90s_b']:.0f}×90)"
            )

        for stat in ALL_STATS:
            a = t['stats_a'].get(stat)
            b = t['stats_b'].get(stat)
            if a is None or b is None:
                continue
            # Skip near-zero base values — ratio would be meaningless
            min_base = MIN_BASE_VALUE.get(stat, DEFAULT_MIN_BASE)
            if abs(a) < min_base:
                continue
            ratio = b / a
            if RATIO_LO <= ratio <= RATIO_HI:
                pair_ratios[pair_key][stat].append(ratio)

    return pair_ratios, pair_players


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate(pair_ratios, pair_players):
    """
    Compute descriptive statistics per league-pair per stat.
    Returns serialisable dict.
    """
    result = {}
    for pair_key, stat_dict in sorted(pair_ratios.items()):
        players = pair_players[pair_key]
        entry = {
            'transfer_count': len(players),
            'players_sample': sorted(set(players))[:12],
        }
        for stat in ALL_STATS:
            ratios = stat_dict.get(stat, [])
            if len(ratios) < MIN_SAMPLES:
                continue
            entry[stat] = {
                'mean':   round(statistics.mean(ratios),   4),
                'median': round(statistics.median(ratios), 4),
                'stdev':  round(statistics.stdev(ratios),  4) if len(ratios) >= 2 else None,
                'n':      len(ratios),
                'min':    round(min(ratios), 4),
                'max':    round(max(ratios), 4),
            }
        result[pair_key] = entry

    return result


# ── Pretty printer ────────────────────────────────────────────────────────────

def print_results(result, pair_counts):
    """Print a summary table to stdout."""
    col_w = 9

    print('\n── Transfer counts per league pair')
    for pair, count in sorted(pair_counts.items(), key=lambda x: -x[1]):
        print(f'   {count:3d}  {pair}')

    print('\n── Empirical translation ratios  (ratio = stat_in_destination / stat_in_source)')
    header = f"  {'League pair':<38}  {'N':>4}  " + '  '.join(
        f"{s[:col_w]:>{col_w}}" for s in ALL_STATS
    )
    print(header)
    print('  ' + '─' * (len(header) - 2))

    for pair_key, data in sorted(result.items(), key=lambda x: -x[1].get('transfer_count', 0)):
        n    = data['transfer_count']
        vals = []
        for stat in ALL_STATS:
            if stat in data and 'mean' in data[stat]:
                vals.append(f"{data[stat]['mean']:>{col_w}.3f}")
            else:
                vals.append(f"{'—':>{col_w}}")
        print(f"  {pair_key:<38}  {n:>4}  " + '  '.join(vals))

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('╔══════════════════════════════════════════════════════╗')
    print('║    ProximaScout · Translation Model Builder          ║')
    print('║    Empirical cross-league stat ratios from FBref     ║')
    print('╚══════════════════════════════════════════════════════╝\n')

    # 1. Load data
    records = load_all_records()

    # 2. Detect transfers
    print('── Detecting cross-league transfers …')
    if VERBOSE:
        print()
    transfers = find_transfers(records)
    print(f'   Found {len(transfers)} qualifying inter-league transfers\n')

    if len(transfers) == 0:
        print('⚠  No transfers found. Check fbref_data/ contains *_standard.csv files.')
        return

    # 3. Compute ratios
    pair_ratios, pair_players = compute_ratios(transfers)

    # 4. Aggregate
    result = aggregate(pair_ratios, pair_players)

    # 5. Print
    pair_counts = Counter(
        f"{LEAGUE_MAP[t['from_lg']]} → {LEAGUE_MAP[t['to_lg']]}"
        for t in transfers
    )
    print_results(result, pair_counts)

    # 6. Build final JSON
    output = {
        '_meta': {
            'description': (
                'Empirical cross-league stat translation ratios derived from FBref Big-5 data. '
                'Each ratio = stat_in_destination / stat_in_source for the same player '
                'in consecutive seasons after a league transfer.'
            ),
            'methodology': {
                'min_90s_per_season': MIN_90S_SEASON,
                'min_90s_source_league': MIN_90S_SOURCE,
                'min_samples_for_stat': MIN_SAMPLES,
                'ratio_outlier_bounds': [RATIO_LO, RATIO_HI],
                'multi_team_handling': '90s-weighted average per season',
                'gk_excluded': True,
            },
            'seasons_covered': [SEASON_LABEL.get(s, s) for s in SEASON_ORDER],
            'leagues_covered': list(LEAGUE_MAP.values()),
            'stats_tracked': ALL_STATS,
            'total_transfers': len(transfers),
        },
        'league_pairs': result,
    }

    os.makedirs(FBREF_DIR, exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f'✓  Results saved → {OUT_PATH}')
    print(f'   {len(result)} league-pair curves  |  {len(transfers)} transfers analysed')

    # 7. Highlight any pair where goals_per90 is surprisingly high or low
    print('\n── Sanity check: goals_per90 mean ratios')
    for pair_key, data in sorted(result.items()):
        g = data.get('goals_per90')
        if g:
            flag = ' ⚠  (unusually high?)' if g['mean'] > 1.20 else \
                   ' ⚠  (unusually low?)'  if g['mean'] < 0.50 else ''
            print(f'   {pair_key:<38}  mean={g["mean"]:.3f}  n={g["n"]}{flag}')


if __name__ == '__main__':
    main()
