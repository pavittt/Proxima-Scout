import pandas as pd, glob, os
from collections import defaultdict

data_dir = 'C:/Users/Pavit singh/OneDrive/Desktop/proxima-scout/fbref_data'

IDENTITY_RENAME = {
    'player_': 'player', 'team_': 'team', 'league_': 'league',
    'season_': 'season', 'nation_': 'nation', 'pos_': 'pos',
    'age_': 'age', 'born_': 'born',
}
STD_RENAME  = {'Playing Time_MP':'appearances','Playing Time_Min':'minutes','Performance_Gls':'goals','Performance_Ast':'assists'}
SHOT_RENAME = {'Standard_Sh':'shots','Standard_SoT':'shots_on_target','Expected_xG':'xg'}
PASS_RENAME = {'Total_Cmp%':'pass_completion','KP':'key_passes','PrgP':'progressive_passes','PPA':'passes_penalty_area'}
POSS_RENAME = {'Take-Ons_Succ':'dribbles_completed','Carries_PrgC':'progressive_carries','Touches_Att Pen':'touches_att_box'}
DEF_RENAME  = {'Tackles_Tkl':'tackles','Tackles_TklW':'tackles_won','Int':'interceptions','Pressures_Press':'pressures','Pressures_%':'pressure_pct','Clr':'clearances','Blocks_Blocks':'blocks'}
MERGE_KEYS  = ['player', 'season', 'team']
STAT_RENAMES = {'standard': STD_RENAME, 'shooting': SHOT_RENAME, 'passing': PASS_RENAME, 'possession': POSS_RENAME, 'defense': DEF_RENAME}
SEASON_PRIORITY = {'_2425': 3, '_2324': 2, '_2223': 1, '': 0}

raw_paths = defaultdict(dict)
raw_prio  = defaultdict(lambda: defaultdict(lambda: -1))
for path in sorted(glob.glob(os.path.join(data_dir, '*.csv'))):
    fname = os.path.basename(path)
    for st in STAT_RENAMES:
        matched = False
        for ss, pr in SEASON_PRIORITY.items():
            pat = f'_{st}{ss}.csv'
            if fname.endswith(pat):
                lk = fname[:-len(pat)]
                if pr > raw_prio[lk][st]:
                    raw_paths[lk][st] = path
                    raw_prio[lk][st] = pr
                matched = True
                break
        if matched:
            break

print('Files found per league:')
for lk, paths in raw_paths.items():
    print(f'  {lk}: {sorted(paths.keys())}')

# Load EPL standard + shooting and merge
def load(path, rename):
    df = pd.read_csv(path, low_memory=False)
    df.rename(columns={k: v for k, v in IDENTITY_RENAME.items() if k in df.columns}, inplace=True)
    df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)
    for k in MERGE_KEYS:
        if k in df.columns:
            df[k] = df[k].astype(str).str.strip()
    return df

base = load(raw_paths['ENG_Premier_League']['standard'], STD_RENAME)
sht  = load(raw_paths['ENG_Premier_League']['shooting'], SHOT_RENAME)
new_cols = [c for c in sht.columns if c not in MERGE_KEYS and c not in base.columns]
merged = base.merge(sht[MERGE_KEYS + new_cols].drop_duplicates(subset=MERGE_KEYS), on=MERGE_KEYS, how='left')

haaland = merged[(merged['player'].str.contains('Haaland', case=False)) & (merged['season'] == '2425')]
print()
if len(haaland):
    row = haaland.iloc[0]
    print('Haaland 2425:')
    print('  appearances:', row.get('appearances', '?'))
    print('  goals:', row.get('goals', '?'))
    print('  shots:', row.get('shots', '?'))
    print('  xg:', row.get('xg', 'not in shooting CSV'))
    print('  key_passes:', row.get('key_passes', 'MISSING - need passing CSV'))
    print('  tackles:', row.get('tackles', 'MISSING - need defense CSV'))
else:
    seasons = merged[merged['player'].str.contains('Haaland', case=False)]['season'].unique().tolist()
    print('Haaland not found at 2425 — seasons in data:', seasons)

print()
print('Total 2425 rows in EPL:', len(merged[merged['season'] == '2425']))
print('Total unique players (all seasons):', merged['player'].nunique())
