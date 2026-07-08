import pandas as pd
import time
import os

os.makedirs("fbref_data", exist_ok=True)

LEAGUE_URLS = {
    "ENG_Premier_League": {
        "passing":    "https://fbref.com/en/comps/9/passing/Premier-League-Stats",
        "possession": "https://fbref.com/en/comps/9/possession/Premier-League-Stats",
        "defense":    "https://fbref.com/en/comps/9/defense/Premier-League-Stats",
    },
    "ESP_La_Liga": {
        "passing":    "https://fbref.com/en/comps/12/passing/La-Liga-Stats",
        "possession": "https://fbref.com/en/comps/12/possession/La-Liga-Stats",
        "defense":    "https://fbref.com/en/comps/12/defense/La-Liga-Stats",
    },
    "GER_Bundesliga": {
        "passing":    "https://fbref.com/en/comps/20/passing/Bundesliga-Stats",
        "possession": "https://fbref.com/en/comps/20/possession/Bundesliga-Stats",
        "defense":    "https://fbref.com/en/comps/20/defense/Bundesliga-Stats",
    },
    "ITA_Serie_A": {
        "passing":    "https://fbref.com/en/comps/11/passing/Serie-A-Stats",
        "possession": "https://fbref.com/en/comps/11/possession/Serie-A-Stats",
        "defense":    "https://fbref.com/en/comps/11/defense/Serie-A-Stats",
    },
    "FRA_Ligue_1": {
        "passing":    "https://fbref.com/en/comps/13/passing/Ligue-1-Stats",
        "possession": "https://fbref.com/en/comps/13/possession/Ligue-1-Stats",
        "defense":    "https://fbref.com/en/comps/13/defense/Ligue-1-Stats",
    },
}


def scrape_table(url, league_name, stat_type):
    print(f"  Scraping {stat_type}...", end=" ", flush=True)
    try:
        tables = pd.read_html(url, header=[0, 1])

        # Pick the largest table — it's the main player stats table
        main_table = max(tables, key=len)

        # Flatten multi-level columns
        main_table.columns = [
            '_'.join(col).strip()
            if isinstance(col, tuple) and col[1] != '' and col[0] != col[1]
            else col[0] if isinstance(col, tuple)
            else col
            for col in main_table.columns
        ]

        # Drop repeated header rows (FBref inserts "Player" rows between pages)
        if 'Player' in main_table.columns:
            main_table = main_table[main_table['Player'] != 'Player']

        # Add league / season identifiers
        main_table['league_'] = league_name
        main_table['season_'] = '2425'

        # Rename identity columns to match the server.py IDENTITY_RENAME keys
        main_table = main_table.rename(columns={
            'Player': 'player_',
            'Squad':  'team_',
            'Nation': 'nation_',
            'Pos':    'pos_',
            'Age':    'age_',
        })

        path = f"fbref_data/{league_name}_{stat_type}_2425.csv"
        main_table.to_csv(path, index=False)
        print(f"saved {len(main_table)} rows → {path}")
        return True

    except Exception as e:
        print(f"failed: {e}")
        return False


print("Scraping FBref for 2024/25 season stats…")
print("Fetching: passing, possession, defense")
print()

for league_name, stat_urls in LEAGUE_URLS.items():
    print(f"\n{league_name}:")
    for stat_type, url in stat_urls.items():
        path = f"fbref_data/{league_name}_{stat_type}_2425.csv"
        if os.path.exists(path):
            print(f"  {stat_type} already exists, skipping")
            continue
        scrape_table(url, league_name, stat_type)
        time.sleep(4)  # be polite — FBref rate-limits aggressive scrapers

print("\nDone! Files saved in fbref_data/")
print("Restart server.py to load the new data.")
