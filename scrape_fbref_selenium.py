import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import time
import os
from io import StringIO

os.makedirs("fbref_data", exist_ok=True)

LEAGUES = {
    "ENG_Premier_League": {
        "shooting":   "https://fbref.com/en/comps/9/shooting/Premier-League-Stats",
        "passing":    "https://fbref.com/en/comps/9/passing/Premier-League-Stats",
        "possession": "https://fbref.com/en/comps/9/possession/Premier-League-Stats",
        "defense":    "https://fbref.com/en/comps/9/defense/Premier-League-Stats",
        "misc":       "https://fbref.com/en/comps/9/misc/Premier-League-Stats",
    },
    "ESP_La_Liga": {
        "shooting":   "https://fbref.com/en/comps/12/shooting/La-Liga-Stats",
        "passing":    "https://fbref.com/en/comps/12/passing/La-Liga-Stats",
        "possession": "https://fbref.com/en/comps/12/possession/La-Liga-Stats",
        "defense":    "https://fbref.com/en/comps/12/defense/La-Liga-Stats",
        "misc":       "https://fbref.com/en/comps/12/misc/La-Liga-Stats",
    },
    "GER_Bundesliga": {
        "shooting":   "https://fbref.com/en/comps/20/shooting/Bundesliga-Stats",
        "passing":    "https://fbref.com/en/comps/20/passing/Bundesliga-Stats",
        "possession": "https://fbref.com/en/comps/20/possession/Bundesliga-Stats",
        "defense":    "https://fbref.com/en/comps/20/defense/Bundesliga-Stats",
        "misc":       "https://fbref.com/en/comps/20/misc/Bundesliga-Stats",
    },
    "ITA_Serie_A": {
        "shooting":   "https://fbref.com/en/comps/11/shooting/Serie-A-Stats",
        "passing":    "https://fbref.com/en/comps/11/passing/Serie-A-Stats",
        "possession": "https://fbref.com/en/comps/11/possession/Serie-A-Stats",
        "defense":    "https://fbref.com/en/comps/11/defense/Serie-A-Stats",
        "misc":       "https://fbref.com/en/comps/11/misc/Serie-A-Stats",
    },
    "FRA_Ligue_1": {
        "shooting":   "https://fbref.com/en/comps/13/shooting/Ligue-1-Stats",
        "passing":    "https://fbref.com/en/comps/13/passing/Ligue-1-Stats",
        "possession": "https://fbref.com/en/comps/13/possession/Ligue-1-Stats",
        "defense":    "https://fbref.com/en/comps/13/defense/Ligue-1-Stats",
        "misc":       "https://fbref.com/en/comps/13/misc/Ligue-1-Stats",
    },
}


def get_driver():
    options = Options()
    # NO headless — run visibly so FBref doesn't flag as bot
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def scrape_table(driver, url, league, stat_type):
    path = f"fbref_data/{league}_{stat_type}_2425.csv"
    if os.path.exists(path):
        print(f"  {stat_type} exists, skipping")
        return True

    print(f"  Scraping {stat_type}...", end=" ", flush=True)
    try:
        driver.get(url)
        time.sleep(4)  # wait for JS / any cookie banners to settle

        tables = pd.read_html(StringIO(driver.page_source))
        if not tables:
            print("no tables found")
            return False

        # Largest table is always the main player stats table
        main_table = max(tables, key=len)

        # Flatten multi-level columns
        if isinstance(main_table.columns, pd.MultiIndex):
            main_table.columns = [
                f"{a}_{b}" if b and a != b else a
                for a, b in main_table.columns
            ]

        # Drop repeated header rows FBref injects between alphabet sections
        if 'Player' in main_table.columns:
            main_table = main_table[main_table['Player'] != 'Player']

        # Rename identity columns to match server.py IDENTITY_RENAME keys
        main_table = main_table.rename(columns={
            'Player': 'player_',
            'Squad':  'team_',
            'Nation': 'nation_',
            'Pos':    'pos_',
            'Age':    'age_',
            'Born':   'born_',
            '90s':    '90s_',
        })

        main_table['league_'] = league
        main_table['season_'] = '2425'

        main_table.to_csv(path, index=False)
        print(f"saved {len(main_table)} rows → {path}")
        return True

    except Exception as e:
        print(f"failed: {e}")
        return False


print("Starting FBref scraper (Selenium / real browser)…")
print("Fetching 2024/25: passing, possession, defense, misc")
print()

driver = get_driver()

try:
    for league, urls in LEAGUES.items():
        print(f"\n{league}:")
        for stat_type, url in urls.items():
            scrape_table(driver, url, league, stat_type)
            time.sleep(8)  # polite delay — FBref rate-limits aggressive bots
finally:
    driver.quit()

print("\nDone! All files saved in fbref_data/")
print("Restart server.py to load the new data.")
