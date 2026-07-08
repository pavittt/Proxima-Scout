from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
import os
import json
from rapidfuzz import fuzz
import unicodedata
import math
import sys

# Windows consoles default to cp1252 — debug prints with accented player
# names must never crash a request
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

app = Flask(__name__)
CORS(app)

# ─── CONFIG ───────────────────────────────────
DATA_DIR = "fbref_data"
CURRENT_SEASON = "2425"

LEAGUE_DIFFICULTY = {
    "Champions League": 1.00,
    "Premier League":   1.00,
    "La Liga":          0.97,
    "Bundesliga":       0.95,
    "Serie A":          0.94,
    "Ligue 1":          0.90,
    "Eredivisie":       0.82,
    "Primeira Liga":    0.80,
    "Championship":     0.78,
    "Saudi Pro League": 0.55,
}

# ─── LOAD DATA ────────────────────────────────
players_db = {}

def normalize(name):
    if not name: return ""
    n = str(name).lower().strip()
    n = ''.join(c for c in unicodedata.normalize('NFD', n)
                if unicodedata.category(c) != 'Mn')
    return n.replace('.','').replace('-',' ').replace("'",'')

def safe_float(val, default=0.0):
    try:
        f = float(val)
        return f if not math.isnan(f) else default
    except:
        return default

def per90(val, apps):
    v = safe_float(val)
    a = safe_float(apps)
    if a <= 0: return 0.0
    return round(v / a, 3)

def infer_position_from_stats(p):
    """Last-resort position inference — only called when FBref lookup misses."""
    cl = safe_float(p.get('clearances_per90'))
    sh = safe_float(p.get('shots_per90'))
    g  = safe_float(p.get('goals_per90'))
    sv = safe_float(p.get('saves', 0))

    if sv > 20:               return 'GK'
    if cl > 3.0 and sh < 0.5: return 'DF'   # high clearances, almost no shots
    if sh > 2.5 or g > 0.35:  return 'FW'   # heavy goal/shot output
    return 'MF'

LEAGUE_MAP = {
    'ENG_Premier_League': 'Premier League',
    'ESP_La_Liga':        'La Liga',
    'GER_Bundesliga':     'Bundesliga',
    'ITA_Serie_A':        'Serie A',
    'FRA_Ligue_1':        'Ligue 1',
    'ENG_Championship':   'Championship',
    'NED_Eredivisie':     'Eredivisie',
    'POR_Primeira':       'Primeira Liga',
    'TUR_Super_Lig':      'Süper Lig',
    'SAU_Pro_League':     'Saudi Pro League',
}

def build_age_lookup():
    """
    Load FBref standard CSVs (Big5 only) and build normalize(name) → age dict.
    Uses birth year from the latest available season so ages stay current.
    Age = 2025 - born_year (end of 2024/25 season approximation).
    """
    age_lookup = {}
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith('_standard.csv'):
            continue
        path = os.path.join(DATA_DIR, fname)
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception:
            continue
        if 'born_' not in df.columns or 'player_' not in df.columns:
            continue
        # Keep the most recent season row per player (highest season_ value)
        if 'season_' in df.columns:
            df = df.sort_values('season_', ascending=False)
        for _, row in df.iterrows():
            name = str(row.get('player_', '')).strip()
            born = safe_float(row.get('born_', 0))
            if not name or name == 'nan' or born < 1970:
                continue
            key = normalize(name)
            if key not in age_lookup:          # first row = most recent season
                age_lookup[key] = round(2025 - born)
    print(f"Age lookup: {len(age_lookup)} players from FBref standard CSVs")
    return age_lookup

def build_position_lookup():
    """
    Build normalize(name) → primary_pos from FBref standard CSVs.
    Uses the most recent season row per player (sort descending).
    Primary pos = first token before comma: 'FW,MF' → 'FW'.
    """
    position_lookup = {}
    fbref_files = [
        f for f in os.listdir(DATA_DIR)
        if 'standard' in f and 'sofascore' not in f and f.endswith('.csv')
    ]
    for fname in fbref_files:
        path = os.path.join(DATA_DIR, fname)
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception:
            continue
        if 'player_' not in df.columns or 'pos_' not in df.columns:
            continue
        # Sort so most recent season comes first
        if 'season_' in df.columns:
            df = df.sort_values('season_', ascending=False)
        for _, row in df.iterrows():
            name = str(row.get('player_', '')).strip()
            pos  = str(row.get('pos_', '')).strip()
            if not name or name == 'nan': continue
            if not pos  or pos  == 'nan': continue
            # Keep the FULL compound string ('MF,FW' etc.) — the second
            # token is the strongest winger/wing-back signal we have.
            tokens = [t.strip() for t in pos.split(',')]
            if tokens[0] not in ('GK', 'DF', 'MF', 'FW'): continue
            key = normalize(name)
            if key not in position_lookup:   # first row = most recent season
                position_lookup[key] = pos
    print(f"Position lookup: {len(position_lookup)} players from FBref standard CSVs")
    return position_lookup

# ─── SUB-ROLE DETECTION ───────────────────────────────────────────────
# 7 tactical sub-roles. FBref compound position ('MF,FW') is the primary
# signal; per-90 output profile resolves the ambiguous cases.
# Calibrated against 24/25 data:
#   Yamal/Vinícius/Saka/Doku (MF+FW hybrids)      → Winger
#   Salah (pure MF, box shots 3.1, dribbles 1.5)   → Winger
#   Kvaratskhelia (pure FW, dribbles 1.8)          → Winger
#   Palmer/Bruno/Wirtz/Musiala (creation ≥ 4.0)    → Attacking Mid
#   Pedri/Rice/Caicedo (creation < 4.0)            → Central Mid
#   TAA/Hakimi (crosses ≥ .45, clearances < 2.6)   → Full-Back
#   Van Dijk/Saliba (clearances 3+, aerial 60%+)   → Centre-Back
#   Haaland/Kane (box shots 2.8+, dribbles < 1)    → Striker

SUBROLE_LABELS = {
    'GK': 'Goalkeeper',
    'CB': 'Centre-Back',
    'FB': 'Full-Back',
    'CM': 'Central Midfielder',
    'AM': 'Attacking Midfielder',
    'W':  'Winger',
    'ST': 'Striker',
}

def detect_subrole(p, pos_full):
    tokens  = [t.strip() for t in str(pos_full).split(',') if t.strip()]
    primary = tokens[0] if tokens else 'MF'

    cross = safe_float(p.get('crosses_per90', 0))
    drib  = safe_float(p.get('dribbles_per90', 0))
    clr   = safe_float(p.get('clearances_per90', 0))
    box   = safe_float(p.get('shots_inside_box_per90', 0))
    kp    = safe_float(p.get('key_passes_per90', 0))
    xa    = safe_float(p.get('xa_per90', 0))
    g     = safe_float(p.get('goals_per90', 0))
    aer   = safe_float(p.get('aerial_duels_pct', 0))
    saves = safe_float(p.get('saves', 0))
    shots = safe_float(p.get('shots_per90', 0))

    # Goalkeeper — position or unmistakable save volume
    if primary == 'GK' or saves >= 10:
        return 'GK'

    # Chance-creation output — splits AM from CM
    creation = kp + xa * 5.0 + g * 2.0 + box * 0.5

    tkl = safe_float(p.get('tackles_per90', 0))

    if primary == 'DF':
        # High-volume wide tackler = full-back (Mazraoui 3.1, Robinson
        # 2.6 vs CBs: Dias 0.6, Bastoni 1.4, Saliba 1.8 — FBs defend
        # 1v1 out wide, CBs don't)
        if tkl >= 2.4 and clr < 4.0:
            return 'FB'
        # Aerial presence without crossing volume = CB (Bastoni-type
        # wide CBs cross a little but win headers)
        if aer >= 58 and cross < 0.9:
            return 'CB'
        # Attacking width = full-back (TAA, Robertson, Kerkez, Porro)
        if (cross >= 0.6 or drib >= 1.0) and clr < 4.5:
            return 'FB'
        # DF,MF hybrid with any wide involvement = wing-back
        if 'MF' in tokens and (cross >= 0.3 or drib >= 0.8):
            return 'FB'
        # Defensive fullback: low clearances + not aerial (Cucurella,
        # Di Lorenzo) — CBs live above 2.5 clearances (Dias 2.59)
        if clr < 2.5 and aer < 55:
            return 'FB'
        return 'CB'

    wide_hybrid = 'MF' in tokens and 'FW' in tokens

    if primary == 'FW' and not wide_hybrid:
        # Wide forward: dribble/cross-heavy without pure box presence
        if (drib >= 1.5 and box < 2.2) or (cross >= 1.0 and box < 2.0):
            return 'W'
        return 'ST'

    if wide_hybrid:
        # Central players in a hybrid shirt fall through to the central
        # resolution below; everyone else with wide output is a winger.
        # Dribble volume protects real wingers: Pulišić 1.15, Díaz 1.47
        # stay wide; KDB 0.46, Álvarez 0.92, Šeško 1.18 resolve central.
        central = (
            (kp >= 2.0 and drib < 0.8) or      # KDB-type deep playmaker
            (box >= 2.3 and cross < 0.4) or    # pure box forward
            (cross < 0.5 and drib < 1.3)       # no wide output (Joelinton)
        )
        if not central:
            return 'W'
        if box >= 1.3 and kp < 1.5 and creation < 3.5:
            return 'ST'                        # Álvarez, Šeško
        return 'AM' if creation >= 3.5 else 'CM'

    # ── primary MF (incl. MF,DF hybrids) ────────────────────────────
    # Wing-backs that FBref lists as plain MF (Muñoz, Angeliño,
    # Mitchell, Semedo): defensive volume + crossing = full-back
    if 'DF' in tokens and clr >= 2.2:
        return 'FB'
    if clr >= 3.5 and kp < 0.5 and cross < 0.3:
        return 'CB'                            # mislabelled CB (rare)
    if clr >= 1.5 and cross >= 0.4 and drib < 1.5:
        return 'FB'

    # Salah-type wide forward: elite box presence + carries
    if box >= 2.5 and drib >= 1.2:
        return 'W'

    # Attacking mid needs shot volume too, or corner-takers like
    # Kimmich sneak in on inflated key passes
    if creation >= 4.0 and shots >= 1.2:
        return 'AM'

    # Wide dribbler hiding in the MF pool (Doku, Semenyo, Doan):
    # high carries, no defensive volume, some crossing
    if drib >= 1.6 and clr < 1.2 and cross >= 0.2:
        return 'W'

    return 'CM'


def load_sofascore():
    global players_db
    players_db = {}

    age_lookup      = build_age_lookup()
    position_lookup = build_position_lookup()

    files = [f for f in os.listdir(DATA_DIR)
             if 'sofascore' in f and f.endswith('.csv')]

    print(f"Loading {len(files)} Sofascore files...")

    EXCLUDED_LEAGUES = ['TUR', 'Super_Lig', 'Turkiye', 'Turkey']

    for fname in files:
        if any(x in fname for x in EXCLUDED_LEAGUES):
            print(f"Skipping {fname} — Turkey excluded")
            continue

        path = os.path.join(DATA_DIR, fname)
        try:
            df = pd.read_csv(path)
        except Exception as e:
            print(f"  Failed to read {fname}: {e}")
            continue

        # Derive league name from filename
        parts = fname.replace('_sofascore_2425.csv', '')
        league = LEAGUE_MAP.get(parts, parts)

        for _, row in df.iterrows():
            name = str(row.get('player', '')).strip()
            if not name or name == 'nan': continue

            apps = safe_float(row.get('appearances', 0))
            if apps < 3: continue

            norm_name = normalize(name)

            # ── Age: FBref born-year lookup → last-name fallback → None ──
            age = age_lookup.get(norm_name, None)
            if age is None:
                last = norm_name.split()[-1] if norm_name else ''
                age_matches = [(k, v) for k, v in age_lookup.items()
                               if k.split()[-1] == last]
                if len(age_matches) == 1:
                    age = age_matches[0][1]

            p = {
                'player': name,
                'team': str(row.get('team', '')),
                'league': league,
                'season': CURRENT_SEASON,
                'appearances': apps,
                'minutes': safe_float(row.get('minutesPlayed', 0)),
                'age': age,

                # Raw stats
                'goals':               safe_float(row.get('goals', 0)),
                'assists':             safe_float(row.get('assists', 0)),
                'xg':                  safe_float(row.get('expectedGoals', 0)),
                'xa':                  safe_float(row.get('expectedAssists', 0)),
                'shots':               safe_float(row.get('totalShots', 0)),
                'shots_on_target':     safe_float(row.get('shotsOnTarget', 0)),
                'shots_inside_box':    safe_float(row.get('shotsFromInsideTheBox', 0)),
                'key_passes':          safe_float(row.get('keyPasses', 0)),
                'dribbles':            safe_float(row.get('successfulDribbles', 0)),
                'dribble_pct':         safe_float(row.get('successfulDribblesPercentage', 0)),
                'crosses':             safe_float(row.get('accurateCrosses', 0)),
                'total_crosses':       safe_float(row.get('totalCross', 0)),
                'long_balls':          safe_float(row.get('accurateLongBalls', 0)),
                'long_ball_pct':       safe_float(row.get('accurateLongBallsPercentage', 0)),
                'big_chances_created': safe_float(row.get('bigChancesCreated', 0)),
                'big_chances_missed':  safe_float(row.get('bigChancesMissed', 0)),
                'pass_to_assist':      safe_float(row.get('passToAssist', 0)),
                'final_third_passes':  safe_float(row.get('accurateFinalThirdPasses', 0)),
                'opp_half_passes':     safe_float(row.get('accurateOppositionHalfPasses', 0)),
                'tackles':             safe_float(row.get('tackles', 0)),
                'tackles_won':         safe_float(row.get('tacklesWon', 0)),
                'tackles_won_pct':     safe_float(row.get('tacklesWonPercentage', 0)),
                'interceptions':       safe_float(row.get('interceptions', 0)),
                'clearances':          safe_float(row.get('clearances', 0)),
                'blocks':              safe_float(row.get('blockedShots', 0)),
                'ball_recoveries':     safe_float(row.get('ballRecovery', 0)),
                'press_won_att3':      safe_float(row.get('possessionWonAttThird', 0)),
                'ground_duels_won':    safe_float(row.get('groundDuelsWon', 0)),
                'ground_duels_pct':    safe_float(row.get('groundDuelsWonPercentage', 0)),
                'aerial_duels_won':    safe_float(row.get('aerialDuelsWon', 0)),
                'aerial_duels_pct':    safe_float(row.get('aerialDuelsWonPercentage', 0)),
                'total_duels_won':     safe_float(row.get('totalDuelsWon', 0)),
                'total_duels_pct':     safe_float(row.get('totalDuelsWonPercentage', 0)),
                'fouls':               safe_float(row.get('fouls', 0)),
                'fouls_drawn':         safe_float(row.get('wasFouled', 0)),
                'yellow_cards':        safe_float(row.get('yellowCards', 0)),
                'red_cards':           safe_float(row.get('redCards', 0)),
                'rating':              safe_float(row.get('rating', 0)),

                # Goalkeeper stats
                'saves':               safe_float(row.get('saves', 0)),
                'goals_conceded':      safe_float(row.get('goalsConceded', 0)),
                'goals_prevented':     safe_float(row.get('goalsPrevented', 0)),
                'clean_sheets':        safe_float(row.get('cleanSheet', 0)),
                'high_claims':         safe_float(row.get('highClaims', 0)),
                'punches':             safe_float(row.get('punches', 0)),
                'penalty_saves':       safe_float(row.get('penaltySave', 0)),
                'runs_out':            safe_float(row.get('successfulRunsOut', 0)),
                'saved_box_shots':     safe_float(row.get('savedShotsFromInsideTheBox', 0)),
            }

            # Calculate ALL per90 stats
            for stat in ['goals', 'assists', 'xg', 'xa', 'shots',
                         'shots_on_target', 'shots_inside_box',
                         'key_passes', 'dribbles', 'crosses',
                         'total_crosses', 'long_balls',
                         'big_chances_created', 'big_chances_missed',
                         'pass_to_assist', 'final_third_passes',
                         'opp_half_passes', 'tackles', 'tackles_won',
                         'interceptions', 'clearances', 'blocks',
                         'ball_recoveries', 'press_won_att3',
                         'ground_duels_won', 'aerial_duels_won',
                         'total_duels_won', 'fouls', 'fouls_drawn',
                         'saves', 'goals_conceded', 'goals_prevented',
                         'high_claims', 'punches', 'runs_out',
                         'saved_box_shots']:
                p[stat + '_per90'] = per90(p[stat], apps)

            # GK rate stats
            faced = p['saves'] + p['goals_conceded']
            p['save_pct']        = round(p['saves'] / faced * 100, 1) if faced > 0 else 0.0
            p['clean_sheet_pct'] = round(p['clean_sheets'] / apps * 100, 1) if apps > 0 else 0.0

            # ── Position: FBref exact → FBref last-name → stat inference ──
            pos = position_lookup.get(norm_name, '')
            pos_source = 'fbref_exact'

            if not pos:
                parts = norm_name.split()
                if len(parts) > 1:
                    last_name = parts[-1]
                    for lk_name, lk_pos in position_lookup.items():
                        if lk_name.endswith(last_name):
                            pos = lk_pos
                            pos_source = 'fbref_lastname'
                            break

            if not pos:
                pos = infer_position_from_stats(p)
                pos_source = 'stats'

            p['pos'] = pos
            p['_pos_source'] = pos_source

            # Tactical sub-role from compound position + output profile
            p['subrole']    = detect_subrole(p, pos)
            p['role_label'] = SUBROLE_LABELS[p['subrole']]

            key = norm_name + '_' + league
            players_db[key] = p

    # ── Coverage stats ──────────────────────────────────────────────────
    src_counts = {'fbref_exact': 0, 'fbref_lastname': 0, 'stats': 0}
    for p in players_db.values():
        src = p.get('_pos_source', 'stats')
        src_counts[src] = src_counts.get(src, 0) + 1
    total = len(players_db)
    print(
        f"Loaded {total} players  |  "
        f"Position sources: FBref={src_counts['fbref_exact']}  "
        f"LastName={src_counts['fbref_lastname']}  "
        f"Stats={src_counts['stats']}"
    )

def calculate_percentiles(players_db):
    """Rank each stat within tactical sub-role group (GK/CB/FB/CM/AM/W/ST).
    Writes stat + '_pct' (0-100) back into each player dict."""
    groups = {sr: [] for sr in SUBROLE_LABELS}
    for key, p in players_db.items():
        groups[p.get('subrole', 'CM')].append(key)

    PERCENTILE_STATS = [
        'goals_per90', 'assists_per90',
        'xg_per90', 'xa_per90',
        'shots_per90', 'shots_inside_box_per90',
        'key_passes_per90', 'dribbles_per90',
        'crosses_per90', 'long_balls_per90',
        'big_chances_created_per90',
        'tackles_per90', 'interceptions_per90',
        'clearances_per90', 'blocks_per90',
        'ball_recoveries_per90',
        'press_won_att3_per90',
        'final_third_passes_per90',
        'opp_half_passes_per90',
        'aerial_duels_pct', 'total_duels_pct',
        'fouls_drawn_per90',
        'dribble_pct', 'ground_duels_pct',
        'fouls_per90', 'appearances', 'minutes',
        # Goalkeeper stats
        'saves_per90', 'goals_prevented_per90', 'save_pct',
        'clean_sheet_pct', 'high_claims_per90', 'punches_per90',
        'runs_out_per90', 'saved_box_shots_per90',
        'goals_conceded_per90', 'long_ball_pct',
        # Quality anchor — possession/volume-independent performance
        'rating',
    ]

    for broad, keys in groups.items():
        if not keys:
            continue
        for stat in PERCENTILE_STATS:
            values      = [safe_float(players_db[k].get(stat, 0)) for k in keys]
            sorted_vals = sorted(values)
            n           = len(sorted_vals)
            for k in keys:
                v   = safe_float(players_db[k].get(stat, 0))
                rank = sum(1 for x in sorted_vals if x < v)
                players_db[k][stat + '_pct'] = round(rank / n * 100, 1)

    counts = '  '.join(f"{sr}={len(keys)}" for sr, keys in groups.items())
    print(f"[percentiles] calculated for {len(players_db)} players  ({counts})")
    return players_db


load_sofascore()
players_db = calculate_percentiles(players_db)

# ─── STARTUP ROLE DEBUG ────────────────────────────────────────────────────
def _debug_roles():
    CHECK = [
        'salah', 'yamal', 'leao', 'vinicius', 'mbappe',
        'bruno fernandes', 'bellingham', 'de bruyne', 'kane', 'haaland',
    ]
    print("\n[startup] Role detection check:")
    for key, p in players_db.items():
        norm = normalize(p['player'])
        if any(c in norm for c in CHECK):
            print(f"  {p['player']:30} pos={p.get('pos','?'):8}  -> {get_role_label(p)}")

# ─── ROLES + STAT LISTS ───────────────────────
# DNA matching runs WITHIN a tactical sub-role pool: wingers only match
# wingers, full-backs only match full-backs, attacking mids only match
# attacking mids. Percentiles are also ranked within the same pool, so
# the comparison is apples-to-apples by construction.

# Broad grouping kept for the drift/gravity features
_BROAD = {'GK': 'Goalkeeper', 'CB': 'Defender', 'FB': 'Defender',
          'CM': 'Midfielder', 'AM': 'Midfielder',
          'W': 'Forward', 'ST': 'Forward'}

def get_broad_role(p):
    return _BROAD.get(p.get('subrole', 'CM'), 'Midfielder')

def get_role_label(p):
    return p.get('role_label', SUBROLE_LABELS.get(p.get('subrole', 'CM')))

# Full 10-stat list per sub-role — used for similarity scoring.
# Top 5 by elite's percentile get 2x weight; bottom 5 get 1x.
ROLE_STATS = {
    'GK': [
        'saves_per90',              # shot-stopping volume
        'save_pct',                 # shot-stopping quality
        'goals_prevented_per90',    # xG faced vs conceded
        'clean_sheet_pct',          # results
        'saved_box_shots_per90',    # close-range reflexes
        'high_claims_per90',        # aerial command
        'punches_per90',            # box dominance style
        'runs_out_per90',           # sweeper-keeper actions
        'long_balls_per90',         # distribution range
        'long_ball_pct',            # distribution accuracy
    ],
    'CB': [
        'tackles_per90',            # defending duels
        'interceptions_per90',      # reading play
        'clearances_per90',         # last-ditch defending
        'aerial_duels_pct',         # aerial dominance
        'blocks_per90',             # shot blocking
        'ball_recoveries_per90',    # regaining possession
        'total_duels_pct',          # overall duel success
        'long_balls_per90',         # ball-playing range
        'opp_half_passes_per90',    # progressive involvement
        'fouls_per90',              # aggression profile
    ],
    'FB': [
        'crosses_per90',            # wide delivery
        'key_passes_per90',         # creative output
        'xa_per90',                 # chance quality
        'dribbles_per90',           # overlapping carries
        'assists_per90',            # end product
        'tackles_per90',            # 1v1 defending
        'interceptions_per90',      # reading play
        'ball_recoveries_per90',    # work rate
        'final_third_passes_per90', # attacking involvement
        'total_duels_pct',          # duel success
    ],
    'CM': [
        'key_passes_per90',         # creativity
        'final_third_passes_per90', # progression
        'opp_half_passes_per90',    # build-up volume
        'long_balls_per90',         # range of passing
        'tackles_per90',            # ball winning
        'interceptions_per90',      # reading play
        'ball_recoveries_per90',    # pressing / work-rate
        'dribbles_per90',           # press resistance
        'xa_per90',                 # chance creation
        'goals_per90',              # box arrival
    ],
    'AM': [
        'goals_per90',              # goal threat
        'xg_per90',                 # shot quality
        'assists_per90',            # end product
        'xa_per90',                 # chance creation
        'key_passes_per90',         # playmaking
        'big_chances_created_per90',# elite creation
        'dribbles_per90',           # carries
        'shots_per90',              # shot volume
        'final_third_passes_per90', # progression
        'fouls_drawn_per90',        # drawing pressure
    ],
    'W': [
        'goals_per90',              # cutting-inside threat
        'xg_per90',                 # shot quality
        'assists_per90',            # end product
        'xa_per90',                 # chance creation
        'dribbles_per90',           # 1v1 threat
        'crosses_per90',            # wide delivery
        'key_passes_per90',         # creativity
        'big_chances_created_per90',# elite creation
        'shots_inside_box_per90',   # box arrival
        'fouls_drawn_per90',        # beating markers
    ],
    'ST': [
        'goals_per90',              # primary output
        'xg_per90',                 # positioning
        'shots_per90',              # volume
        'shots_inside_box_per90',   # penalty-box presence
        'assists_per90',            # link-up end product
        'xa_per90',                 # chance creation
        'key_passes_per90',         # playmaking
        'aerial_duels_pct',         # target-man profile
        'big_chances_created_per90',# hold-up creation
        'fouls_drawn_per90',        # winning fouls / pens
    ],
}

# ─── SIMILARITY ───────────────────────────────

STAT_LABELS = {
    'goals_per90':               'Goals/90',
    'assists_per90':             'Assists/90',
    'xg_per90':                  'xG/90',
    'xa_per90':                  'xA/90',
    'shots_per90':               'Shots/90',
    'shots_inside_box_per90':    'Box Shots/90',
    'key_passes_per90':          'Key Passes/90',
    'dribbles_per90':            'Dribbles/90',
    'crosses_per90':             'Crosses/90',
    'long_balls_per90':          'Long Balls/90',
    'big_chances_created_per90': 'Big Chances/90',
    'tackles_per90':             'Tackles/90',
    'interceptions_per90':       'Interceptions/90',
    'clearances_per90':          'Clearances/90',
    'blocks_per90':              'Blocks/90',
    'ball_recoveries_per90':     'Ball Recoveries/90',
    'final_third_passes_per90':  'Final 3rd Passes/90',
    'aerial_duels_pct':          'Aerial Won%',
    'total_duels_pct':           'Duels Won%',
    'fouls_drawn_per90':         'Fouls Drawn/90',
    'fouls_per90':               'Fouls/90',
    'opp_half_passes_per90':     'Opp-Half Passes/90',
    'saves_per90':               'Saves/90',
    'save_pct':                  'Save %',
    'goals_prevented_per90':     'Goals Prevented/90',
    'clean_sheet_pct':           'Clean Sheet %',
    'saved_box_shots_per90':     'Box Saves/90',
    'high_claims_per90':         'High Claims/90',
    'punches_per90':             'Punches/90',
    'runs_out_per90':            'Sweeper Actions/90',
    'long_ball_pct':             'Long Ball %',
    'goals_conceded_per90':      'Conceded/90',
}


def get_top_stats(player, role, top_n=5):
    """Return player's top_n stats by percentile FROM their role's stat list."""
    stat_list = ROLE_STATS.get(role, ROLE_STATS['CM'])
    pcts = {s: safe_float(player.get(s + '_pct', 0)) for s in stat_list}
    return [s for s, _ in sorted(pcts.items(), key=lambda x: -x[1])[:top_n]]

# Alias used inside calculate_similarity for weighted vector building
def get_top5_stats(player, role):
    return set(get_top_stats(player, role, top_n=5))

# Run startup role check now that get_broad_role is defined
_debug_roles()


def calculate_similarity(elite, candidate, role):
    """Percentile-distance similarity — no league difficulty penalty.

    For each of the 10 role stats:
      sim = 1 − |elite_pct − candidate_pct| / 100

    Both players are ranked within the same broad-role group (Forward /
    Midfielder / Defender), so percentiles are directly comparable.
    This naturally surfaces players with the same output profile:
      - Two elite scorers both sit at the 90th+ pct in goals/xG → near-perfect match.
      - A playmaker vs a pure striker differ in key-pass pct → partial penalty.
      - No league origin bias — the comparison is pure statistical rank.

    Top-5 stats (by elite's percentile in their role group) get 2× weight.
    Bottom-5 stats get 1× weight.   Total weight = 15.
    Final score scaled 0–99 with a small age-trajectory bonus.
    """
    # ── WEIGHTED COSINE SIMILARITY (2× top-5) + MAGNITUDE GUARD ─────
    # 1. Profile SHAPE: weighted cosine over the 10 role-stat percentile
    #    vectors, with the elite's top-5 identity stats at 2× weight.
    #    Cosine punishes shape mismatches in BOTH directions — a
    #    no-dribble target man cannot match an explosive runner.
    # 2. Magnitude guard: average deficit on the top-5 stats only.
    #    Stops budget versions with the right shape but lower output
    #    from outranking genuine peers. Surplus never costs anything.
    role_stats = ROLE_STATS.get(role, ROLE_STATS['CM'])
    top5 = get_top5_stats(elite, role)

    num = den_e = den_c = 0.0
    for stat in role_stats:
        w = 2.0 if stat in top5 else 1.0
        e = safe_float(elite.get(stat + '_pct', 50)) / 100.0
        c = safe_float(candidate.get(stat + '_pct', 50)) / 100.0
        num   += w * e * c
        den_e += w * e * e
        den_c += w * c * c
    if den_e <= 0 or den_c <= 0:
        return 0.0
    cos = num / math.sqrt(den_e * den_c)          # 0..1 shape similarity

    deficits = [max(safe_float(elite.get(s + '_pct', 50))
                    - safe_float(candidate.get(s + '_pct', 50)), 0.0)
                for s in top5]
    mag = 1.0 - (sum(deficits) / max(len(deficits), 1)) / 100.0

    base = cos * 0.70 + mag * 0.30

    # Calibrated stretch for tier separation (avg pair ~45, near-clone ~93+)
    stretched = max(0.0, min((base - 0.55) / 0.45, 1.0))

    age   = safe_float(candidate.get('age', 25) or 25)
    bonus = 5 if age < 19 else 3 if age < 21 else 2 if age < 23 else 1 if age < 25 else 0

    final = stretched * 96.0 + bonus * 0.6
    return min(final, 99)

# ─── TRANSLATION ESCALATOR ───────────────────

TRANSLATION_CURVES = {}
curves_path = 'fbref_data/translation_curves.json'
if os.path.exists(curves_path):
    with open(curves_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    for pair_key, data in raw.items():
        TRANSLATION_CURVES[pair_key] = data
    print(f"Translation curves: {len(TRANSLATION_CURVES)} pairs loaded")
else:
    print("Warning: translation_curves.json not found")


def translate_to_league(player, target='Premier League'):
    source = player.get('league', '')
    if source == target:
        return None

    pair_key = f"{source} — {target}"
    curve = TRANSLATION_CURVES.get(pair_key)

    STATS_TO_TRANSLATE = [
        'goals_per90', 'xg_per90',
        'assists_per90', 'xa_per90',
        'shots_per90', 'shots_inside_box_per90',
        'key_passes_per90', 'dribbles_per90',
        'big_chances_created_per90',
        'tackles_per90', 'interceptions_per90',
        'ball_recoveries_per90',
    ]

    translated = {}

    for stat in STATS_TO_TRANSLATE:
        raw = safe_float(player.get(stat, 0))

        if curve and 'stats' in curve:
            stat_short = stat.replace('_per90', '')
            ratio = curve['stats'].get(stat_short, {}).get('mean', None)
            if ratio is None:
                ratio = curve['stats'].get('goals', {}).get('mean', 0.85)
        else:
            STATIC = {
                'Premier League': 1.00,
                'La Liga': 0.97,
                'Bundesliga': 0.94,
                'Serie A': 0.93,
                'Ligue 1': 0.89,
                'Championship': 0.74,
                'Eredivisie': 0.71,
                'Primeira Liga': 0.69,
                'Saudi Pro League': 0.54,
            }
            source_r = STATIC.get(source, 0.75)
            target_r = STATIC.get(target, 1.00)
            ratio = source_r / target_r

        translated[stat] = round(raw * ratio, 3)

    n = curve.get('n_transfers', 0) if curve else 0
    if n >= 15: confidence = 'High'
    elif n >= 8: confidence = 'Medium'
    elif n > 0: confidence = 'Low'
    else: confidence = 'Estimated'

    translated['confidence'] = confidence
    translated['n_transfers'] = n
    translated['source'] = source
    translated['target'] = target
    translated['ratio'] = ratio
    return translated


# ─── ENDPOINTS ────────────────────────────────

@app.route('/health')
def health():
    return jsonify({
        "status":          "ok",
        "players":         len(players_db),
        "translation": {
            "pairs_loaded": len(TRANSLATION_CURVES),
        },
    })

@app.route('/search')
def search():
    q = normalize(request.args.get('name', ''))
    if len(q) < 3:
        return jsonify({"results": []})

    results = []
    for key, p in players_db.items():
        score = fuzz.partial_ratio(q, normalize(p['player']))
        if score >= 60:
            results.append({**p, '_score': score})

    results.sort(key=lambda x: (
        -int(normalize(x['player']).startswith(q)),
        -x['_score'],
        -x.get('appearances', 0)
    ))

    return jsonify({"results": results[:10]})

@app.route('/similarity')
def similarity():
    name = request.args.get('name', '')
    norm = normalize(name)

    # ── Find elite player ────────────────────────────────────────────────
    elite      = None
    best_score = 0
    for key, p in players_db.items():
        s    = fuzz.partial_ratio(norm, normalize(p['player']))
        apps = safe_float(p.get('appearances', 0))
        if s > best_score and apps >= 5:
            best_score = s
            elite      = p

    if not elite or best_score < 60:
        return jsonify({"error": "Player not found", "results": []})

    role       = elite.get('subrole', 'CM')     # GK/CB/FB/CM/AM/W/ST
    role_label = get_role_label(elite)
    elite_key  = normalize(elite['player']) + '_' + elite.get('league', '')

    # ── Top 5 stats by percentile within role stat list ──────────────────
    top_stats  = get_top_stats(elite, role, top_n=5)
    top_labels = [STAT_LABELS.get(s, s) for s in top_stats]

    print(f"\n[sim] {elite['player']}  role={role_label}")
    for stat in top_stats:
        pct = elite.get(stat + '_pct', 0)
        val = safe_float(elite.get(stat, 0))
        print(f"  {STAT_LABELS.get(stat, stat)}: {val:.3f}  ({pct:.0f}th pct)")

    # ── Score all candidates in same broad position pool ─────────────────
    BIG5 = ['Premier League', 'La Liga', 'Bundesliga', 'Serie A', 'Ligue 1']

    big5_candidates  = []
    other_candidates = []

    for key, p in players_db.items():
        if key == elite_key: continue
        if safe_float(p.get('appearances', 0)) < 8: continue
        if p.get('league') == 'Saudi Pro League':    continue
        if p.get('subrole', 'CM') != role:           continue  # same tactical pool only

        score = calculate_similarity(elite, p, role)
        if score < 30: continue

        c_top  = get_top_stats(p, role, top_n=3)
        result = {
            **p,
            'proxima_score': round(score, 1),
            'detected_role': get_role_label(p),
            'top_stats':     c_top,
            'top_labels':    [STAT_LABELS.get(s, s) for s in c_top],
        }

        if p.get('league') in BIG5:
            big5_candidates.append(result)
        else:
            other_candidates.append(result)

    big5_candidates.sort( key=lambda x: -x['proxima_score'])
    other_candidates.sort(key=lambda x: -x['proxima_score'])

    top_big5  = big5_candidates[:20]
    top_other = other_candidates[:20]

    # Combined for "All" view — top 40 sorted by score
    all_combined = sorted(
        top_big5 + top_other,
        key=lambda x: -x['proxima_score']
    )[:40]

    # U25 from the full combined pool
    top_u25 = [c for c in all_combined
               if c.get('age') is not None and safe_float(c.get('age', 99)) < 25][:20]

    # ── Debug ────────────────────────────────────────────────────────────
    print(f"\n[debug] Elite: {elite['player']}  role={role}")
    print(f"[debug] Big5 candidates: {len(big5_candidates)}  Other: {len(other_candidates)}")
    print(f"[debug] Top 5 overall:")
    for r in all_combined[:5]:
        print(f"  {r['player']:30} score={r['proxima_score']:.1f} league={r['league']}")

    mbappe = next((c for c in all_combined
                   if 'mbappe' in normalize(c['player'])
                   or 'mbapp' in normalize(c['player'])), None)
    if mbappe:
        rank = next((i+1 for i,c in enumerate(all_combined)
                     if normalize(c['player']) == normalize(mbappe['player'])), '?')
        print(f"\n[debug] Mbappe rank: {rank}  score: {mbappe['proxima_score']}")
        for stat in top_stats:
            e_val = safe_float(elite.get(stat, 0))
            m_val = safe_float(mbappe.get(stat, 0))
            print(f"  {stat}: elite={e_val:.3f} Mbappe={m_val:.3f}")
    else:
        print("[debug] Mbappe not found in candidates")
    # ─────────────────────────────────────────────────────────────────────

    return jsonify({
        "elite": {
            **elite,
            'role':          role_label,
            'detected_role': role_label,
            'top_stats':     top_stats,
            'top_labels':    top_labels,
        },
        "results":       all_combined,
        "big5_results":  top_big5,
        "other_results": top_other,
        "results_u25":   top_u25,
        "role":          role_label,
        "subrole":       role,
    })

@app.route('/debug/player')
def debug_player():
    name    = request.args.get('name', '').lower()
    results = []
    for key, p in players_db.items():
        if name in normalize(p['player']):
            results.append(p)
    if not results:
        return jsonify({"error": "not found"})
    return jsonify(results[0])

# FIX 5 — startup debug: confirm role + top-5 stats for key players
def _startup_debug():
    probes = ['harry kane', 'pedri', 'trent alexander-arnold']
    print("\n" + "="*60)
    print("STARTUP DEBUG — role_key + top5_stats")
    print("="*60)
    for probe in probes:
        norm = normalize(probe)
        best, best_p = 0, None
        for key, p in players_db.items():
            s = fuzz.partial_ratio(norm, normalize(p['player']))
            if s > best:
                best, best_p = s, p
        if best_p and best >= 60:
            role   = best_p.get('subrole', 'CM')
            top5   = get_top_stats(best_p, role, top_n=5)
            labels = [STAT_LABELS.get(s, s) for s in top5]
            print(f"\n  {best_p['player']}  [{best_p.get('league','?')}]")
            print(f"  pos={best_p.get('pos','?')}  role_key={role}")
            print(f"  top5: {labels}")
            for s in top5:
                print(f"    {STAT_LABELS.get(s,s)}: {safe_float(best_p.get(s,0)):.3f}  "
                      f"({best_p.get(s+'_pct', 0):.0f}th pct)")
        else:
            print(f"\n  [{probe}] — NOT FOUND (best_score={best})")
    print("="*60 + "\n")

_startup_debug()


# ═══════════════════════════════════════════════════════════════════
# ARCHETYPE DRIFT ENGINE
# ═══════════════════════════════════════════════════════════════════

# ── CONSISTENT 3-STAT BASIS ────────────────────────────────────────
# Historical FBref seasons only provide goals / assists / shots per90.
# Positions MUST be computed from the same stats for every season,
# otherwise "drift" is just a data-coverage artifact.
_DRIFT_MAX = {
    'goals_per90':   0.75,   # ≈95th pct for attackers
    'assists_per90': 0.45,
    'shots_per90':   4.20,
}

def _dnorm(val, key):
    return min(safe_float(val) / _DRIFT_MAX[key] * 100.0, 100.0)

def calculate_archetype_position(stats):
    """
    2-D archetype map from the season-consistent stat basis.
      x > 0 → CREATOR  (assist output dominates goal output)
      x < 0 → FINISHER (goal output dominates assist output)
      y > 0 → HIGH-VOLUME / DIRECT (shot-heavy attacking profile)
      y < 0 → SELECTIVE / DEEP     (low shot volume, builds from deep)
    """
    g  = _dnorm(stats.get('goals_per90',   0), 'goals_per90')
    a  = _dnorm(stats.get('assists_per90', 0), 'assists_per90')
    sh = _dnorm(stats.get('shots_per90',   0), 'shots_per90')

    x = a - g                            # creator ↔ finisher balance
    y = (sh * 0.65 + g * 0.35) - 42.0    # attacking volume, centred
    return round(x, 1), round(y, 1)


def get_archetype_label(x, y):
    t = 14
    if x >  t and y >  t: return 'ATTACKING PLAYMAKER'
    if x < -t and y >  t: return 'COMPLETE STRIKER'
    if x >  t and y < -t: return 'DEEP PLAYMAKER'
    if x < -t and y < -t: return 'POACHER'
    if x >  t:            return 'CREATOR'
    if x < -t:            return 'FINISHER'
    if y >  t:            return 'VOLUME ATTACKER'
    if y < -t:            return 'SUPPORT PLAYER'
    return 'BALANCED PROFILE'


def _drift_direction(seasons):
    if len(seasons) < 2:
        return 'Insufficient data for drift analysis'
    dx = seasons[-1]['x'] - seasons[0]['x']
    dy = seasons[-1]['y'] - seasons[0]['y']
    mag = math.sqrt(dx**2 + dy**2)
    if mag < 8:  return 'Profile has remained consistent across seasons'
    if dx >  15 and dy >  12: return 'Evolving into a high-volume creative attacker'
    if dx < -15 and dy >  12: return 'Developing into an out-and-out goal threat'
    if dx >  18:              return 'Shifting from finisher toward chief creator'
    if dx < -18:              return 'Shifting from creator toward primary finisher'
    if dy >  18:              return 'Taking on far more attacking volume'
    if dy < -18:              return 'Dropping into a deeper, more selective role'
    if dx >  8:               return 'Gradually becoming more creative'
    if dx < -8:               return 'Gradually becoming more goal-focused'
    if dy >  8:               return 'Trending toward more direct attacking play'
    if dy < -8:               return 'Trending toward a deeper supporting role'
    return 'Subtle tactical evolution detected'


def _resolve_canonical(player_name):
    """Best Sofascore match for a query — the single source of truth
    for which player we are analysing. Near-tie fuzzy scores are broken
    by minutes played, so 'bellingham' resolves to Jude, not Jobe."""
    norm_q = normalize(player_name)
    candidates = []
    for key, p in players_db.items():
        if safe_float(p.get('appearances', 0)) < 3:
            continue
        s = fuzz.WRatio(norm_q, normalize(p['player']))
        if s >= 75:
            # League-quality-weighted minutes: a Real Madrid regular beats a
            # Championship regular on an ambiguous surname query.
            lq = LEAGUE_DIFFICULTY.get(p.get('league', ''), 0.75) ** 2
            candidates.append((s, safe_float(p.get('minutes', 0)) * lq, p))
    if not candidates:
        return None, 0
    best_score = max(c[0] for c in candidates)
    pool = [c for c in candidates if c[0] >= best_score - 5]   # near-ties
    pool.sort(key=lambda c: -c[1])                             # weighted minutes win
    return pool[0][2], best_score


def _same_player(canon_norm, fbref_name):
    """Strict identity check — full-string ratio, NOT substring.
    Prevents 'Pedri' matching 'Pedrinho' etc."""
    n = normalize(fbref_name)
    if n == canon_norm:
        return True
    return fuzz.ratio(canon_norm, n) >= 88


def load_historical_seasons(player_name):
    """
    Per-season stats across 22/23, 23/24, 24/25 — same stat basis everywhere.
      - FBref standard CSVs  → goals/assists per90, apps, minutes (all 3 seasons)
      - FBref shooting CSVs  → shots per90 (all 3 seasons)
      - Sofascore 24/25      → fills 24/25 if FBref row missing + tooltip extras
    """
    canon, _ = _resolve_canonical(player_name)
    canon_norm = normalize(canon['player']) if canon else normalize(player_name)

    SEASON_MAP = {2223: '22/23', 2324: '23/24', 2425: '24/25'}
    seasons = {}

    # ── 1. FBref standard — goals / assists / minutes ──────────────
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith('_standard.csv') or 'sofascore' in fname:
            continue
        league = LEAGUE_MAP.get(fname.replace('_standard.csv', ''), '')
        try:
            df = pd.read_csv(os.path.join(DATA_DIR, fname), low_memory=False)
        except Exception:
            continue
        if 'player_' not in df.columns or 'season_' not in df.columns:
            continue
        for _, row in df.iterrows():
            name = str(row.get('player_', '')).strip()
            if not _same_player(canon_norm, name):
                continue
            label = SEASON_MAP.get(int(safe_float(row.get('season_', 0))))
            if not label:
                continue
            mins = safe_float(row.get('Playing Time_Min', 0))
            if mins < 270:                       # min ~3 full games
                continue
            apps  = int(safe_float(row.get('Playing Time_MP', 0)))
            entry = {
                'season':        label,
                'league':        league,
                'team':          str(row.get('team_', '')),
                'appearances':   apps,
                'minutes':       int(mins),
                'goals_per90':   safe_float(row.get('Per 90 Minutes_Gls', 0)),
                'assists_per90': safe_float(row.get('Per 90 Minutes_Ast', 0)),
            }
            ex = seasons.get(label)
            if not ex or apps > ex.get('appearances', 0):
                seasons[label] = entry

    # ── 2. FBref shooting — shots per90 ─────────────────────────────
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith('_shooting.csv') or 'sofascore' in fname:
            continue
        try:
            df = pd.read_csv(os.path.join(DATA_DIR, fname), low_memory=False)
        except Exception:
            continue
        for _, row in df.iterrows():
            name = str(row.get('player_', '')).strip()
            if not _same_player(canon_norm, name):
                continue
            label = SEASON_MAP.get(int(safe_float(row.get('season_', 0))))
            if label and label in seasons:
                seasons[label]['shots_per90'] = safe_float(row.get('Standard_Sh/90', 0))

    # ── 3. Sofascore 24/25 — core basis + tooltip extras ───────────
    if canon:
        label = '24/25'
        if label not in seasons:
            seasons[label] = {
                'season':      label,
                'league':      canon.get('league', ''),
                'team':        canon.get('team',   ''),
                'appearances': int(safe_float(canon.get('appearances', 0))),
                'minutes':     int(safe_float(canon.get('minutes', 0))),
            }
        # Core basis — only fill if FBref didn't already provide it
        for stat in ('goals_per90', 'assists_per90', 'shots_per90'):
            if stat not in seasons[label] or seasons[label][stat] == 0:
                seasons[label][stat] = safe_float(canon.get(stat, 0))
        # Tooltip extras (24/25 only — clearly richer data)
        for stat in ('key_passes_per90', 'tackles_per90'):
            seasons[label][stat] = safe_float(canon.get(stat, 0))
        seasons[label]['team']   = canon.get('team',   seasons[label].get('team',   ''))
        seasons[label]['league'] = canon.get('league', seasons[label].get('league', ''))

    order = ['22/23', '23/24', '24/25']
    return [seasons[s] for s in order if s in seasons]


def _peer_cloud(pos_group, exclude_norm, n=25):
    """Representative same-role peers on the SAME 3-stat basis as the trail.
    Picks the most-established players (by minutes), not the most extreme."""
    out = []
    for key, p in players_db.items():
        if normalize(p['player']) == exclude_norm:
            continue
        if get_broad_role(p) != pos_group:
            continue
        if safe_float(p.get('appearances', 0)) < 15:
            continue
        x, y = calculate_archetype_position(p)
        out.append({'x': x, 'y': y, 'name': p['player'],
                    'mins': safe_float(p.get('minutes', 0))})
    out.sort(key=lambda d: -d['mins'])           # most established first
    return [{'x': d['x'], 'y': d['y'], 'name': d['name']} for d in out[:n]]


@app.route('/archetype/<path:player_name>')
def archetype_drift(player_name):
    seasons_raw = load_historical_seasons(player_name)
    if not seasons_raw:
        return jsonify({'error': 'Player not found or insufficient data'}), 404

    seasons_out = []
    for s in seasons_raw:
        x, y = calculate_archetype_position(s)
        seasons_out.append({
            'season':        s['season'],
            'x':             x,
            'y':             y,
            'archetype':     get_archetype_label(x, y),
            'appearances':   s.get('appearances', 0),
            'minutes':       s.get('minutes', 0),
            'team':          s.get('team', ''),
            'league':        s.get('league', ''),
            # Key stats for tooltip
            'goals_per90':           round(s.get('goals_per90', 0), 2),
            'assists_per90':         round(s.get('assists_per90', 0), 2),
            'shots_per90':           round(s.get('shots_per90', 0), 2),
            'key_passes_per90':      round(s.get('key_passes_per90', 0), 2),
            'tackles_per90':         round(s.get('tackles_per90', 0), 2),
        })

    # Drift metrics
    drift_magnitude  = 0.0
    drift_direction  = 'Insufficient data'
    drift_alert      = False
    alert_message    = ''
    positive_drift   = False

    if len(seasons_out) >= 2:
        f, l   = seasons_out[0], seasons_out[-1]
        dx, dy = l['x'] - f['x'], l['y'] - f['y']
        drift_magnitude = round(math.sqrt(dx**2 + dy**2), 1)
        drift_direction = _drift_direction(seasons_out)
        if drift_magnitude > 22:
            drift_alert = True
            if dy > 5 and dx > 5:
                positive_drift  = True
                alert_message   = 'Strong upward trajectory across attacking dimensions.'
            else:
                alert_message = ('Profile has shifted significantly — '
                                 'review tactical fit before transfer commitment.')

    # Canonical player + peer cloud on the same stat basis
    canon, _   = _resolve_canonical(player_name)
    best_name  = canon['player'] if canon else player_name
    pos_group  = get_broad_role(canon) if canon else 'Midfielder'
    peers      = _peer_cloud(pos_group, normalize(best_name))

    return jsonify({
        'player':          best_name,
        'seasons':         seasons_out,
        'drift_magnitude': drift_magnitude,
        'drift_direction': drift_direction,
        'drift_alert':     drift_alert,
        'positive_drift':  positive_drift,
        'alert_message':   alert_message,
        'peers':           peers,
    })


# ═══════════════════════════════════════════════════════════════════
# STATISTICAL GRAVITY ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/gravity/search')
def search_teams():
    query = request.args.get('q', '').strip().lower()
    if not query or len(query) < 2:
        return jsonify([])
    teams = set(
        p.get('team', '') for p in players_db.values()
        if query in p.get('team', '').lower() and p.get('team', '')
    )
    return jsonify(sorted(list(teams))[:10])


@app.route('/api/gravity/<path:team_name>')
def get_team_gravity(team_name):
    players = [
        p for p in players_db.values()
        if p.get('team', '').lower() == team_name.lower()
    ]
    if not players:
        return jsonify({"error": "Team not found"}), 404

    processed_raw  = []
    team_totals    = {"attack": 0.0, "creative": 0.0, "defensive": 0.0}

    for p in players:
        mins  = safe_float(p.get('minutes', 0)) or 1.0
        scale = mins / 90.0          # total actions ≈ per90 × nineties

        goals   = safe_float(p.get('goals_per90',             0)) * scale
        assists = safe_float(p.get('assists_per90',           0)) * scale
        kp      = safe_float(p.get('key_passes_per90',        0)) * scale
        bcc     = safe_float(p.get('big_chances_created_per90',0)) * scale
        shots   = safe_float(p.get('shots_per90',             0)) * scale
        xa      = safe_float(p.get('xa_per90',                0)) * scale
        fp      = safe_float(p.get('final_third_passes_per90',0)) * scale
        tackles = safe_float(p.get('tackles_per90',           0)) * scale
        ints    = safe_float(p.get('interceptions_per90',     0)) * scale
        clears  = safe_float(p.get('clearances_per90',        0)) * scale
        recov   = safe_float(p.get('ball_recoveries_per90',   0)) * scale

        attack_val    = goals + assists + kp + bcc + shots
        creative_val  = xa + kp + fp
        defensive_val = tackles + ints + clears + recov

        team_totals["attack"]    += attack_val
        team_totals["creative"]  += creative_val
        team_totals["defensive"] += defensive_val

        processed_raw.append({
            "name":             p.get('player', 'Unknown'),
            "role":             get_broad_role(p),
            "pos":              p.get('pos', 'MF'),
            "minutes":          int(mins),
            "attack_val":       attack_val,
            "creative_val":     creative_val,
            "defensive_val":    defensive_val,
            "goals_per90":      safe_float(p.get('goals_per90',        0)),
            "assists_per90":    safe_float(p.get('assists_per90',       0)),
            "key_passes_per90": safe_float(p.get('key_passes_per90',   0)),
            "shots_per90":      safe_float(p.get('shots_per90',        0)),
            "tackles_per90":    safe_float(p.get('tackles_per90',      0)),
            "xa_per90":         safe_float(p.get('xa_per90',           0)),
        })

    # Guard against all-zero totals
    for k in team_totals:
        if team_totals[k] == 0:
            team_totals[k] = 1.0

    output_players = []
    for p in processed_raw:
        attacking_pct  = round(p["attack_val"]    / team_totals["attack"]    * 100, 1)
        creative_pct   = round(p["creative_val"]  / team_totals["creative"]  * 100, 1)
        defensive_pct  = round(p["defensive_val"] / team_totals["defensive"] * 100, 1)

        overall_gravity = round(
            attacking_pct  * 0.5 +
            creative_pct   * 0.3 +
            defensive_pct  * 0.2, 1
        )

        if   overall_gravity > 25: planet_type = "STAR"
        elif overall_gravity > 15: planet_type = "PLANET"
        elif overall_gravity > 8:  planet_type = "MOON"
        else:                      planet_type = "ASTEROID"

        output_players.append({
            "name":             p["name"],
            "role":             p["role"],
            "pos":              p["pos"],
            "gravity":          overall_gravity,
            "attacking_pct":    attacking_pct,
            "creative_pct":     creative_pct,
            "defensive_pct":    defensive_pct,
            "planet_type":      planet_type,
            "goals_per90":      round(p["goals_per90"],        2),
            "assists_per90":    round(p["assists_per90"],       2),
            "key_passes_per90": round(p["key_passes_per90"],   2),
            "shots_per90":      round(p["shots_per90"],        2),
            "tackles_per90":    round(p["tackles_per90"],      2),
            "xa_per90":         round(p["xa_per90"],           2),
            "minutes":          p["minutes"],
        })

    output_players.sort(key=lambda x: -x["gravity"])

    return jsonify({
        "team":          players[0].get('team', team_name) if players else team_name,
        "total_players": len(output_players),
        "players":       output_players,
    })


# ═══════════════════════════════════════════════════════════════════
# DATA SCIENCE LAYER — style clusters + squad gap analysis
# numpy-only: SVD PCA, k-means. No sklearn dependency.
# ═══════════════════════════════════════════════════════════════════
import numpy as np

_DS_ROLES = ['ST', 'W', 'AM', 'CM', 'FB', 'CB']
_cluster_cache = {}


def _role_pool(role, min_minutes=600):
    """Qualified players + percentile feature matrix for one sub-role."""
    stats = ROLE_STATS[role]
    keys, X = [], []
    for key, p in players_db.items():
        if p.get('subrole') != role:                            continue
        if safe_float(p.get('minutes', 0)) < min_minutes:       continue
        if safe_float(p.get('rating', 0)) < 5.5:                continue
        keys.append(key)
        X.append([safe_float(p.get(s + '_pct', 50)) / 100.0 for s in stats])
    return stats, keys, np.array(X)


def _player_strength(p, role):
    """0-100 quality score for a player in their role.

    70% Sofascore rating percentile within the sub-role — rating is
    per-match performance judged against the game context, so it does
    NOT punish defenders at dominant teams for having little defending
    to do. 30% role-stat profile percentile for stylistic texture.
    (Pure volume-stat averages made Saliba/Gabriel look below-average
    because Arsenal concede so few actions to defend.)"""
    stats = ROLE_STATS[role]
    rating_pct  = safe_float(p.get('rating_pct', 50))
    profile_pct = sum(safe_float(p.get(s + '_pct', 50)) for s in stats) / len(stats)
    return 0.70 * rating_pct + 0.30 * profile_pct


def _kmeans(X, k, iters=40, seed=7):
    rng = np.random.default_rng(seed)
    centers = X[rng.choice(len(X), k, replace=False)]
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        d = ((X[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = d.argmin(axis=1)
        if (new_labels == labels).all():
            break
        labels = new_labels
        for c in range(k):
            pts = X[labels == c]
            if len(pts):
                centers[c] = pts.mean(axis=0)
    return labels, centers


@app.route('/api/clusters/<role>')
def style_clusters(role):
    role = role.upper()
    if role not in _DS_ROLES:
        return jsonify({'error': f'role must be one of {_DS_ROLES}'}), 400
    if role in _cluster_cache:
        return jsonify(_cluster_cache[role])

    stats, keys, X = _role_pool(role, min_minutes=600)
    if len(keys) < 20:
        return jsonify({'error': f'only {len(keys)} qualified {role} players'}), 500

    # Standardise, PCA via SVD → 2 components
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    Z = (X - mu) / sd
    U, S, Vt = np.linalg.svd(Z, full_matrices=False)
    pc = Z @ Vt[:2].T
    explained = (S ** 2) / (S ** 2).sum()

    k = 3 if len(keys) < 120 else 4 if len(keys) < 260 else 5
    labels, centers = _kmeans(pc, k)

    # Auto-name each cluster from its two most distinguishing stats
    cluster_meta = []
    for c in range(k):
        mask = labels == c
        if not mask.any():
            cluster_meta.append({'id': int(c), 'label': '—', 'count': 0})
            continue
        diff  = Z[mask].mean(axis=0)
        order = np.argsort(-np.abs(diff))
        parts = [('High ' if diff[i] > 0 else 'Low ') + STAT_LABELS.get(stats[i], stats[i])
                 for i in order[:2]]
        cluster_meta.append({'id': int(c), 'label': ' · '.join(parts),
                             'count': int(mask.sum())})

    pts = []
    for i, key in enumerate(keys):
        p = players_db[key]
        pts.append({
            'player': p['player'], 'team': p.get('team', ''),
            'league': p.get('league', ''), 'age': p.get('age'),
            'minutes': int(safe_float(p.get('minutes', 0))),
            'x': round(float(pc[i, 0]), 3), 'y': round(float(pc[i, 1]), 3),
            'cluster': int(labels[i]),
        })

    out = {
        'role': role, 'role_label': SUBROLE_LABELS[role],
        'explained': [round(float(explained[0]), 3), round(float(explained[1]), 3)],
        'clusters': cluster_meta,
        'points': pts,
    }
    _cluster_cache[role] = out
    return jsonify(out)


@app.route('/api/squad-gap/<path:team_name>')
def squad_gap(team_name):
    squad = [p for p in players_db.values()
             if p.get('team', '').lower() == team_name.lower()]
    if not squad:
        return jsonify({'error': 'Team not found'}), 404

    league = squad[0].get('league', '')
    role_rows = []
    for role in _DS_ROLES:
        group = [p for p in squad if p.get('subrole') == role
                 and safe_float(p.get('minutes', 0)) >= 450]
        if not group:
            role_rows.append({'role': role, 'label': SUBROLE_LABELS[role],
                              'strength': None, 'players': []})
            continue
        tot_min  = sum(safe_float(p['minutes']) for p in group)
        strength = sum((safe_float(p['minutes']) / tot_min) * _player_strength(p, role)
                       for p in group)
        role_rows.append({
            'role': role, 'label': SUBROLE_LABELS[role],
            'strength': round(strength, 1),
            'players': [{'player': p['player'],
                         'minutes': int(safe_float(p['minutes'])),
                         'age': p.get('age'),
                         'rating': round(safe_float(p.get('rating', 0)), 2)}
                        for p in sorted(group, key=lambda q: -safe_float(q['minutes']))[:4]],
        })

    scored  = [r for r in role_rows if r['strength'] is not None]
    weakest = min(scored, key=lambda r: r['strength']) if scored else None

    suggestions = []
    if weakest:
        role = weakest['role']
        cands = []
        for key, p in players_db.items():
            if p.get('subrole') != role:                        continue
            if p.get('team', '').lower() == team_name.lower():  continue
            if p.get('league') == 'Saudi Pro League':           continue
            if safe_float(p.get('minutes', 0)) < 1200:          continue
            age = safe_float(p.get('age') or 99)
            if age > 27:                                        continue
            fit = _player_strength(p, role)
            top = get_top_stats(p, role, top_n=3)
            cands.append({
                'player': p['player'], 'team': p.get('team', ''),
                'league': p.get('league', ''), 'age': p.get('age'),
                'minutes': int(safe_float(p.get('minutes', 0))),
                'rating': round(safe_float(p.get('rating', 0)), 2),
                'fit': round(fit, 1),
                'top_labels': [STAT_LABELS.get(s, s) for s in top],
            })
        cands.sort(key=lambda c: -c['fit'])
        suggestions = cands[:6]

    return jsonify({
        'team': squad[0].get('team', team_name),
        'league': league,
        'roles': role_rows,
        'weakest': weakest['role'] if weakest else None,
        'weakest_label': weakest['label'] if weakest else None,
        'suggestions': suggestions,
    })


# ─── ROLE-FIT RANKINGS (Position Filter backend) ─────────────────────
# Tactical archetypes scored on the full rich-stat percentile database
# (same source as DNA Match) instead of API-Football's ~10 crude stats.
# Keys must match POSITION_TYPES in src/data/positions.js exactly.
ARCHETYPES_RICH = {
    # (subrole pools, [(stat, weight)]) — weights sum to 1.0
    'Centre Forward':       (['ST'],       [('goals_per90', .28), ('xg_per90', .22), ('shots_inside_box_per90', .20), ('shots_per90', .15), ('aerial_duels_pct', .15)]),
    'Second Striker':       (['ST', 'AM'], [('goals_per90', .22), ('key_passes_per90', .20), ('dribbles_per90', .20), ('big_chances_created_per90', .20), ('assists_per90', .18)]),
    'Inside Forward':       (['W'],        [('goals_per90', .28), ('dribbles_per90', .24), ('shots_inside_box_per90', .20), ('xg_per90', .16), ('key_passes_per90', .12)]),
    'False Nine':           (['ST', 'AM'], [('key_passes_per90', .24), ('xa_per90', .20), ('dribbles_per90', .20), ('big_chances_created_per90', .20), ('goals_per90', .16)]),
    'Winger':               (['W'],        [('dribbles_per90', .28), ('crosses_per90', .20), ('xa_per90', .20), ('key_passes_per90', .16), ('fouls_drawn_per90', .16)]),

    'Box-to-Box':           (['CM'],       [('tackles_per90', .22), ('key_passes_per90', .22), ('ball_recoveries_per90', .20), ('goals_per90', .18), ('dribbles_per90', .18)]),
    'Deep Lying Playmaker': (['CM'],       [('long_balls_per90', .26), ('opp_half_passes_per90', .24), ('final_third_passes_per90', .20), ('key_passes_per90', .15), ('interceptions_per90', .15)]),
    'Advanced Playmaker':   (['AM', 'CM'], [('key_passes_per90', .28), ('xa_per90', .24), ('big_chances_created_per90', .20), ('dribbles_per90', .16), ('goals_per90', .12)]),
    'Press Disruptor':      (['CM'],       [('tackles_per90', .28), ('interceptions_per90', .24), ('ball_recoveries_per90', .24), ('total_duels_pct', .12), ('fouls_per90', .12)]),
    'Holding Midfielder':   (['CM'],       [('interceptions_per90', .26), ('tackles_per90', .24), ('ball_recoveries_per90', .20), ('total_duels_pct', .16), ('long_balls_per90', .14)]),
    'Mezzala':              (['CM', 'AM'], [('dribbles_per90', .24), ('final_third_passes_per90', .20), ('key_passes_per90', .20), ('goals_per90', .18), ('tackles_per90', .18)]),
    'Carrilero':            (['CM'],       [('ball_recoveries_per90', .26), ('tackles_per90', .22), ('opp_half_passes_per90', .22), ('total_duels_pct', .16), ('interceptions_per90', .14)]),

    'Ball Playing Defender':(['CB'],       [('long_balls_per90', .28), ('opp_half_passes_per90', .26), ('interceptions_per90', .16), ('total_duels_pct', .16), ('clearances_per90', .14)]),
    'Aggressive Defender':  (['CB'],       [('tackles_per90', .26), ('total_duels_pct', .24), ('aerial_duels_pct', .20), ('clearances_per90', .16), ('fouls_per90', .14)]),
    'Inverted Fullback':    (['FB'],       [('key_passes_per90', .24), ('final_third_passes_per90', .22), ('dribbles_per90', .20), ('tackles_per90', .18), ('interceptions_per90', .16)]),
    'Attacking Fullback':   (['FB'],       [('crosses_per90', .26), ('assists_per90', .22), ('xa_per90', .20), ('dribbles_per90', .16), ('key_passes_per90', .16)]),

    'Sweeper Keeper':       (['GK'],       [('runs_out_per90', .26), ('long_balls_per90', .22), ('long_ball_pct', .18), ('save_pct', .18), ('goals_prevented_per90', .16)]),
    'Traditional GK':       (['GK'],       [('save_pct', .26), ('saves_per90', .22), ('clean_sheet_pct', .20), ('high_claims_per90', .16), ('goals_prevented_per90', .16)]),
}


@app.route('/api/role-fit/<path:archetype>')
def role_fit(archetype):
    spec = ARCHETYPES_RICH.get(archetype)
    if not spec:
        return jsonify({'error': f'unknown archetype: {archetype}'}), 400
    pools, weights = spec

    min_age  = safe_float(request.args.get('min_age', 15))
    max_age  = safe_float(request.args.get('max_age', 45))
    min_apps = safe_float(request.args.get('min_apps', 5))
    leagues  = request.args.get('leagues', '')
    league_set = {l.strip() for l in leagues.split(',') if l.strip()} if leagues else None

    rows = []
    for key, p in players_db.items():
        if p.get('subrole') not in pools:                       continue
        if p.get('league') == 'Saudi Pro League':               continue
        if league_set and p.get('league') not in league_set:    continue
        if safe_float(p.get('appearances', 0)) < min_apps:      continue
        age = p.get('age')
        if age is not None and not (min_age <= safe_float(age) <= max_age):
            continue
        if age is None and min_age > 15:                        continue

        fit = sum(w * safe_float(p.get(stat + '_pct', 50)) for stat, w in weights)

        # Top contributing stats for the row microcopy
        contribs = sorted(weights, key=lambda sw: -sw[1] * safe_float(p.get(sw[0] + '_pct', 50)))
        rows.append({
            'player': p['player'], 'team': p.get('team', ''),
            'league': p.get('league', ''), 'age': age,
            'minutes': int(safe_float(p.get('minutes', 0))),
            'apps': int(safe_float(p.get('appearances', 0))),
            'rating': round(safe_float(p.get('rating', 0)), 2),
            'role_label': get_role_label(p),
            'fit': round(fit, 1),
            'top_labels': [STAT_LABELS.get(s, s) for s, _ in contribs[:3]],
        })

    rows.sort(key=lambda r: -r['fit'])
    return jsonify({
        'archetype': archetype,
        'pools': [SUBROLE_LABELS[r] for r in pools],
        'features': [{'stat': STAT_LABELS.get(s, s), 'weight': w} for s, w in weights],
        'results': rows[:20],
        'pool_size': len(rows),
    })


if __name__ == '__main__':
    app.run(port=5001, debug=False)
