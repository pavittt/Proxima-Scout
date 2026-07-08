import soccerdata as sd
import pandas as pd
import os

os.makedirs("fbref_data", exist_ok=True)

leagues = [
    "ENG-Premier League",
    "ESP-La Liga",
    "GER-Bundesliga",
    "ITA-Serie A",
    "FRA-Ligue 1"
]

seasons = [2022, 2023, 2024]

# Only download missing stat types
stat_types = ["passing", "possession", "defense"]

for league in leagues:
    print(f"\nDownloading {league}...")
    try:
        fbref = sd.FBref(
            leagues=league,
            seasons=seasons
        )
        for stat_type in stat_types:
            league_clean = league.replace(
                " ", "_").replace("-", "_")
            path = f"fbref_data/{league_clean}_{stat_type}.csv"

            # Skip if already exists
            if os.path.exists(path):
                print(f"  {stat_type} already exists, skipping")
                continue

            print(f"  Downloading {stat_type}...", end=" ")
            try:
                df = fbref.read_player_season_stats(
                    stat_type=stat_type
                )
                df = df.reset_index()
                df.columns = [
                    '_'.join(col).strip()
                    if isinstance(col, tuple)
                    else col
                    for col in df.columns
                ]
                df.to_csv(path, index=False)
                print(f"saved {len(df)} rows")
            except Exception as e:
                print(f"failed: {e}")
    except Exception as e:
        print(f"League failed: {e}")

print("\nDone! Check fbref_data/ folder.")
