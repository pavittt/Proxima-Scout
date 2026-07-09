# ProximaScout ⚽

**Football Intelligence Platform** — a data-driven scouting 
tool that goes beyond basic stats to reveal where talent 
is going, not just where it is.



![Tech Stack](https://img.shields.io/badge/React-Vite-purple)




![Backend](https://img.shields.io/badge/Python-Flask-blue)




![Data](https://img.shields.io/badge/Data-Sofascore%20%2B%20FBref-green)



---

## What is ProximaScout?

ProximaScout is a football analytics platform built for 
scouts, analysts, and directors. It combines three seasons 
of historical data across nine leagues with original 
algorithms to surface insights that spreadsheets can't show.

Most tools tell you what a player did. ProximaScout tells 
you what kind of player they are becoming.

---

## Features

### 🧬 DNA Match
Statistical similarity engine across 4,521 players from 
nine leagues. Search any player and get their closest 
tactical twins — both in the Big 5 and hidden in lower 
leagues. Uses position-normalised percentile scoring with 
league difficulty translation curves built from 187 real 
transfers.

### 🌌 Statistical Gravity
Solar system visualisation of a club's output distribution. 
Each player is a planet — size and orbital distance 
determined by their share of team attacking, creative, and 
defensive output. Instantly shows if a team revolves around 
one player and what happens when they leave.

### 📡 Archetype Drift
Plots a player's tactical identity across three seasons 
(22/23, 23/24, 24/25) as a moving trail on a 2D coordinate 
map. X axis: Creative vs Defensive. Y axis: Direct vs 
Possession. See if a winger is drifting into a midfielder 
before anyone notices.

### 🗺️ Style Map
PCA + k-means clustering of every qualified player per 
role. See where a player sits in tactical space relative 
to their positional peers. Auto-named clusters with 
explained-variance stats.

### 🩺 Squad Gaps
Enter any club and get a minutes-weighted strength score 
per position group. Weakest position flagged with ranked 
recruitment targets. Algorithm uses 70% match-rating 
percentile + 30% stat-profile percentile to avoid 
possession-bias traps (a CB at a dominant team won't be 
penalised for low clearance volume).

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React + Vite |
| Backend | Python Flask |
| 3D Animations | Three.js |
| Scroll Effects | GSAP + ScrollTrigger |
| Styling | Tailwind CSS |
| Charts | SVG (custom) |
| Data Science | NumPy, Pandas |

---

## Data Sources

- **Sofascore CSVs** — 9 leagues, 2024/25 season, 
  4,521 players, ~50 stats per player
- **FBref Standard CSVs** — Big 5 leagues across 
  22/23, 23/24, 24/25 for 3-season historical features
- **Translation curves** — empirical data from 187 
  real cross-league transfers

Leagues covered: Premier League, La Liga, Bundesliga, 
Serie A, Ligue 1, Eredivisie, Primeira Liga, 
Championship, Belgian Pro League

Saudi Pro League excluded from all results.

---

## Running Locally

### Prerequisites
- Python 3.10+
- Node.js 18+
- pip

### Setup

```bash
# Clone the repo
git clone https://github.com/yourusername/proxima-scout
cd proxima-scout

# Install Python dependencies
pip install flask flask-cors pandas numpy

# Install Node dependencies
npm install

# Add your data files
# Place Sofascore CSVs in /data/
# Place FBref CSVs in /fbref_data/
