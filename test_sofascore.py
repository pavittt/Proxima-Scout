import ScraperFC as sfc
import pandas as pd
import os

os.makedirs("fbref_data", exist_ok=True)
scraper = sfc.Sofascore()

leagues = {
    'ENG_Premier_League': ('England Premier League', '24/25'),
    'ESP_La_Liga': ('Spain La Liga', '24/25'),
    'GER_Bundesliga': ('Germany Bundesliga', '24/25'),
    'ITA_Serie_A': ('Italy Serie A', '24/25'),
    'FRA_Ligue_1': ('France Ligue 1', '24/25'),
    'ENG_Championship': ('England EFL Championship', '24/25'),
    'NED_Eredivisie': ('Netherlands Eredivisie', '24/25'),
    'POR_Primeira': ('Portugal Primeira Liga', '24/25'),
    'TUR_Super_Lig': ('Turkiye Super Lig', '24/25'),
    'SAU_Pro_League': ('Saudi Arabia Pro League', '24/25'),
}

for filename, (league, year) in leagues.items():
    print(f"\nFetching {league} {year}...")
    try:
        df = scraper.scrape_player_league_stats(
            year=year,
            league=league,
            accumulation='total',
            selected_positions=[
                'Goalkeepers',
                'Defenders',
                'Midfielders',
                'Forwards'
            ]
        )
        path = f"fbref_data/{filename}_sofascore_2425.csv"
        df.to_csv(path, index=False)
        print(f"Saved {len(df)} players → {path}")
        if len(df) > 0:
            print("Columns:", df.columns.tolist())
    except Exception as e:
        print(f"Failed: {e}")

print("\nAll done!")
