import os
import asyncio
import math
import threading
from statistics import mean
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# TOUCHLINE TIPSTER v4.2
# ============================================================
# Core Telegram/backend baseline for the Touchline Tipster app.
#
# Environment variables:
#   TELEGRAM_BOT_TOKEN
#   FOOTBALL_API_KEY
#
# Optional:
#   PORT
#   FIXTURE_SEASON
# ============================================================

VERSION = "4.1"
API_BASE = "https://v3.football.api-sports.io"
PORT = int(os.getenv("PORT", "10000"))
FIXTURE_SEASON = int(os.getenv("FIXTURE_SEASON", "2024"))
SAMPLE_SIZE = 10
TIMEOUT = 20

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")


# ------------------------------------------------------------
# RENDER HEALTH SERVER
# ------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Touchline Tipster v4.2 is running."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


# ------------------------------------------------------------
# FOOTBALL API
# ------------------------------------------------------------

def api_get(endpoint, params=None):
    if not FOOTBALL_API_KEY:
        return None, "FOOTBALL_API_KEY is missing."

    try:
        response = requests.get(
            API_BASE + endpoint,
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            params=params or {},
            timeout=TIMEOUT,
        )

        if response.status_code != 200:
            return None, f"API HTTP {response.status_code}: {response.text[:250]}"

        data = response.json()

        if data.get("errors"):
            return None, str(data["errors"])

        return data, None

    except requests.RequestException as exc:
        return None, f"API connection error: {exc}"


def find_team(name):
    data, error = api_get("/teams", {"search": name.strip()})
    if error:
        return None, error

    results = data.get("response", [])
    if not results:
        return None, f"No team found for '{name}'."

    target = name.strip().lower()

    for item in results:
        team = item.get("team", {})
        if team.get("name", "").lower() == target:
            return team, None

    return results[0].get("team", {}), None


def get_team_fixtures(team_id, last=SAMPLE_SIZE):
    data, error = api_get(
        "/fixtures",
        {
            "team": team_id,
            "season": FIXTURE_SEASON,
            "last": last,
        },
    )

    if error:
        return [], error

    return data.get("response", []), None


def get_h2h(team1_id, team2_id, last=10):
    data, error = api_get(
        "/fixtures/headtohead",
        {
            "h2h": f"{team1_id}-{team2_id}",
            "last": last,
        },
    )

    if error:
        return [], error

    return data.get("response", []), None


# ------------------------------------------------------------
# STATISTICS
# ------------------------------------------------------------

def is_finished(fixture):
    return fixture.get("fixture", {}).get("status", {}).get("short") in {
        "FT", "AET", "PEN", "AWD", "WO"
    }


def team_score(fixture, team_id):
    teams = fixture.get("teams", {})
    goals = fixture.get("goals", {})

    home_id = teams.get("home", {}).get("id")
    away_id = teams.get("away", {}).get("id")
    home_goals = goals.get("home")
    away_goals = goals.get("away")

    if home_goals is None or away_goals is None:
        return None

    if team_id == home_id:
        return home_goals, away_goals
    if team_id == away_id:
        return away_goals, home_goals

    return None


def calculate_stats(fixtures, team_id):
    stats = {
        "games": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "scored": [],
        "conceded": [],
        "totals": [],
        "btts": 0,
        "clean_sheets": 0,
        "failed_to_score": 0,
    }

    for fixture in fixtures:
        if not is_finished(fixture):
            continue

        score = team_score(fixture, team_id)
        if score is None:
            continue

        gf, ga = score
        total = gf + ga

        stats["games"] += 1
        stats["scored"].append(gf)
        stats["conceded"].append(ga)
        stats["totals"].append(total)

        if gf > ga:
            stats["wins"] += 1
        elif gf == ga:
            stats["draws"] += 1
        else:
            stats["losses"] += 1

        stats["btts"] += int(gf > 0 and ga > 0)
        stats["clean_sheets"] += int(ga == 0)
        stats["failed_to_score"] += int(gf == 0)

    return stats


def summarize(stats):
    games = stats["games"]

    if games == 0:
        return {
            "games": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "avg_scored": 0,
            "avg_conceded": 0,
            "over15": 0,
            "over25": 0,
            "over35": 0,
            "under25": 0,
            "under35": 0,
            "btts": 0,
            "clean_sheet": 0,
            "failed_to_score": 0,
        }

    totals = stats["totals"]

    return {
        "games": games,
        "wins": stats["wins"],
        "draws": stats["draws"],
        "losses": stats["losses"],
        "avg_scored": mean(stats["scored"]),
        "avg_conceded": mean(stats["conceded"]),
        "over15": sum(x > 1.5 for x in totals) / games * 100,
        "over25": sum(x > 2.5 for x in totals) / games * 100,
        "over35": sum(x > 3.5 for x in totals) / games * 100,
        "under25": sum(x < 2.5 for x in totals) / games * 100,
        "under35": sum(x < 3.5 for x in totals) / games * 100,
        "btts": stats["btts"] / games * 100,
        "clean_sheet": stats["clean_sheets"] / games * 100,
        "failed_to_score": stats["failed_to_score"] / games * 100,
    }


def split_home_away(fixtures, team_id):
    home = []
    away = []

    for fixture in fixtures:
        teams = fixture.get("teams", {})
        if teams.get("home", {}).get("id") == team_id:
            home.append(fixture)
        elif teams.get("away", {}).get("id") == team_id:
            away.append(fixture)

    return home, away


def form_string(fixtures, team_id):
    form = []

    for fixture in fixtures:
        if not is_finished(fixture):
            continue

        score = team_score(fixture, team_id)
        if not score:
            continue

        gf, ga = score
        form.append("W" if gf > ga else "D" if gf == ga else "L")

    return "".join(form)


def poisson_probability(lam, k):
    lam = max(0.01, float(lam))
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def seven_outcomes(home, away):
    # Estimate expected goals from each team's scoring rate and opponent's
    # conceding rate, then derive the seven core outcome probabilities.
    home_xg = max(0.10, (home["avg_scored"] + away["avg_conceded"]) / 2)
    away_xg = max(0.10, (away["avg_scored"] + home["avg_conceded"]) / 2)

    matrix = {}
    home_win = draw = away_win = over25 = under25 = btts_yes = 0.0
    for hg in range(0, 9):
        for ag in range(0, 9):
            p = poisson_probability(home_xg, hg) * poisson_probability(away_xg, ag)
            matrix[(hg, ag)] = p
            if hg > ag:
                home_win += p
            elif hg == ag:
                draw += p
            else:
                away_win += p
            if hg + ag >= 3:
                over25 += p
            else:
                under25 += p
            if hg > 0 and ag > 0:
                btts_yes += p

    outcomes = [
        ("Home Win", home_win * 100),
        ("Draw", draw * 100),
        ("Away Win", away_win * 100),
        ("Over 2.5 Goals", over25 * 100),
        ("Under 2.5 Goals", under25 * 100),
        ("Both Teams To Score â Yes", btts_yes * 100),
        ("Both Teams To Score â No", (1 - btts_yes) * 100),
    ]

    scores = sorted(
        ((score, probability * 100) for score, probability in matrix.items()),
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "outcomes": [
            {"market": name, "confidence": max(1, min(97, prob)),
             "risk": risk(max(1, min(97, prob))),
             "advice": advice(max(1, min(97, prob)))}
            for name, prob in outcomes
        ],
        "scores": [
            {"score": f"{hg}-{ag}", "probability": prob}
            for (hg, ag), prob in scores
        ],
    }


# ------------------------------------------------------------
# MARKET ENGINE
# ------------------------------------------------------------

def confidence(home, away, market):
    hg = max(home["games"], 1)
    ag = max(away["games"], 1)

    if market == "Over 2.5":
        value = (home["over25"] + away["over25"]) / 2

    elif market == "Under 4.5":
        value = 100 - ((home["over35"] + away["over35"]) / 2) * 0.65

    elif market == "BTTS":
        value = (home["btts"] + away["btts"]) / 2

    elif market == "Home Win":
        value = 50 + (
            (home["wins"] / hg * 100 - 50) * 0.60
            + (away["losses"] / ag * 100 - 50) * 0.25
        )

    elif market == "Away Win":
        value = 50 + (
            (away["wins"] / ag * 100 - 50) * 0.60
            + (home["losses"] / hg * 100 - 50) * 0.25
        )

    elif market == "Double Chance 1X":
        value = 100 - (home["losses"] / hg * 100) * 0.65

    elif market == "Double Chance X2":
        value = 100 - (away["losses"] / ag * 100) * 0.65

    elif market == "Home Team Over 0.5":
        value = 100 - home["failed_to_score"] * 0.85

    elif market == "Away Team Over 0.5":
        value = 100 - away["failed_to_score"] * 0.85

    else:
        value = 50

    return max(1, min(97, value))


MARKETS = [
    "Under 4.5",
    "Double Chance 1X",
    "Double Chance X2",
    "Home Team Over 0.5",
    "Away Team Over 0.5",
    "Over 2.5",
    "BTTS",
    "Home Win",
    "Away Win",
]


def risk(conf):
    if conf >= 75:
        return "ð¢ HIGH"
    if conf >= 55:
        return "ð¡ MEDIUM"
    return "ð´ RISKY"


def advice(conf):
    if conf >= 75:
        return "BET"
    if conf >= 55:
        return "CAUTION"
    return "AVOID"


def rank_markets(home, away):
    rows = []

    for market in MARKETS:
        c = confidence(home, away, market)
        rows.append({
            "market": market,
            "confidence": c,
            "risk": risk(c),
            "advice": advice(c),
        })

    return sorted(rows, key=lambda row: row["confidence"], reverse=True)


# ------------------------------------------------------------
# COMPLETE MATCH ANALYSIS
# ------------------------------------------------------------

def analyze_match(home_name, away_name):
    home_team, error = find_team(home_name)
    if error:
        return None, error

    away_team, error = find_team(away_name)
    if error:
        return None, error

    home_id = home_team["id"]
    away_id = away_team["id"]

    home_fixtures, error = get_team_fixtures(home_id)
    if error:
        return None, error

    away_fixtures, error = get_team_fixtures(away_id)
    if error:
        return None, error

    h2h, _ = get_h2h(home_id, away_id)

    home_stats = summarize(calculate_stats(home_fixtures, home_id))
    away_stats = summarize(calculate_stats(away_fixtures, away_id))

    home_games, _ = split_home_away(home_fixtures, home_id)
    _, away_games = split_home_away(away_fixtures, away_id)

    home_venue = summarize(calculate_stats(home_games, home_id))
    away_venue = summarize(calculate_stats(away_games, away_id))

    return {
        "home": home_team,
        "away": away_team,
        "home_stats": home_stats,
        "away_stats": away_stats,
        "home_venue": home_venue,
        "away_venue": away_venue,
        "home_form": form_string(home_fixtures, home_id),
        "away_form": form_string(away_fixtures, away_id),
        "h2h": h2h,
        "markets": rank_markets(home_stats, away_stats),
        "seven_outcomes": seven_outcomes(home_stats, away_stats),
    }, None


# ------------------------------------------------------------
# SPORTYBET CODE SAFETY
# ------------------------------------------------------------

def analyze_sportybet_code(code):
    if not code.strip():
        return "â Please provide a SportyBet code."

    return (
        "ðï¸ SPORTYBET CODE ANALYSIS\n\n"
        f"Code: {code.strip()}\n\n"
        "â ï¸ I will never invent or guess selections inside a code.\n"
        "The actual selections must first be resolved by a supported "
        "SportyBet integration before they can be analyzed or rebuilt."
    )


# ------------------------------------------------------------
# NATURAL LANGUAGE PARSING
# ------------------------------------------------------------

def parse_match(text):
    lower = text.lower()

    for separator in (" vs ", " v ", " versus ", " - "):
        if separator in lower:
            index = lower.index(separator)
            a = text[:index].strip()
            b = text[index + len(separator):].strip()
            if a and b:
                return a, b

    return None


def requested_market(text):
    t = text.lower()

    if "over 2.5" in t:
        return "Over 2.5"
    if "under 4.5" in t:
        return "Under 4.5"
    if "btts" in t or "both teams to score" in t:
        return "BTTS"
    if "1x" in t or "home or draw" in t:
        return "Double Chance 1X"
    if "x2" in t or "draw or away" in t:
        return "Double Chance X2"
    if "home win" in t:
        return "Home Win"
    if "away win" in t:
        return "Away Win"

    return None


# ------------------------------------------------------------
# OUTPUT
# ------------------------------------------------------------

def stats_text(name, stats, form):
    return (
        f"{name}\n"
        f"Form: {form or 'N/A'}\n"
        f"W/D/L: {stats['wins']}/{stats['draws']}/{stats['losses']}\n"
        f"Games: {stats['games']}\n"
        f"Avg goals: {stats['avg_scored']:.2f}\n"
        f"Avg conceded: {stats['avg_conceded']:.2f}\n"
        f"Over 2.5: {stats['over25']:.0f}%\n"
        f"BTTS: {stats['btts']:.0f}%"
    )


def format_analysis(result):
    h = result["home"]
    a = result["away"]
    hs = result["home_stats"]
    aws = result["away_stats"]

    lines = [
        f"â½ TOUCHLINE TIPSTER v{VERSION}",
        "",
        f"ðï¸ {h['name']} vs {a['name']}",
        f"ð Season: {FIXTURE_SEASON}",
        f"ð¢ Sample: Last {SAMPLE_SIZE} available matches",
        "",
        "ââââââââââââââââââââ",
        "ð  HOME TEAM",
        "ââââââââââââââââââââ",
        stats_text(h["name"], hs, result["home_form"]),
        "",
        "ðï¸ HOME VENUE",
        f"Games: {result['home_venue']['games']}",
        f"Avg scored: {result['home_venue']['avg_scored']:.2f}",
        f"Avg conceded: {result['home_venue']['avg_conceded']:.2f}",
        f"Over 2.5: {result['home_venue']['over25']:.0f}%",
        f"BTTS: {result['home_venue']['btts']:.0f}%",
        "",
        "ââââââââââââââââââââ",
        "âï¸ AWAY TEAM",
        "ââââââââââââââââââââ",
        stats_text(a["name"], aws, result["away_form"]),
        "",
        "ð£ï¸ AWAY VENUE",
        f"Games: {result['away_venue']['games']}",
        f"Avg scored: {result['away_venue']['avg_scored']:.2f}",
        f"Avg conceded: {result['away_venue']['avg_conceded']:.2f}",
        f"Over 2.5: {result['away_venue']['over25']:.0f}%",
        f"BTTS: {result['away_venue']['btts']:.0f}%",
        "",
        "ââââââââââââââââââââ",
        "ð¯ SEVEN CORE OUTCOMES",
        "ââââââââââââââââââââ",
    ]

    for i, item in enumerate(result["seven_outcomes"]["outcomes"], 1):
        lines.append(
            f"{i}. {item['market']} â "
            f"{item['confidence']:.0f}% {item['risk']} â {item['advice']}"
        )

    lines.extend([
        "",
        "ð¯ POSSIBLE CORRECT SCORES",
    ])

    for item in result["seven_outcomes"]["scores"]:
        score_conf = item["probability"]
        lines.append(
            f"â¢ {item['score']} â {score_conf:.1f}% "
            f"{risk(score_conf)}"
        )

    lines.extend([
        "",
        "ââââââââââââââââââââ",
        "ð¯ MARKET RANKING",
        "ââââââââââââââââââââ",
    ])

    for i, item in enumerate(result["markets"], 1):
        lines.append(
            f"{i}. {item['market']} â "
            f"{item['confidence']:.0f}% "
            f"{item['risk']} â {item['advice']}"
        )

    lines.extend([
        "",
        f"ð¤ H2H matches available: {len(result['h2h'])}",
        "",
        "â ï¸ Statistical guidance only. No result is guaranteed.",
    ])

    return "\n".join(lines)


# ------------------------------------------------------------
# TELEGRAM COMMANDS
# ------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"â½ Welcome to Touchline Tipster v{VERSION}!\n\n"
        "ð§  AI football analysis and betting intelligence.\n\n"
        "Commands:\n"
        "/analyze Chelsea vs Arsenal\n"
        "/pick Chelsea vs Arsenal\n"
        "/markets Chelsea vs Arsenal\n"
        "/h2h Chelsea vs Arsenal\n"
        "/team Chelsea\n"
        "/fixtures Chelsea\n"
        "/code YOUR_SPORTYBET_CODE\n"
        "/apitest\n\n"
        "Natural language is also supported."
    )


async def team_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /team Chelsea")
        return

    team, error = find_team(" ".join(context.args))
    if error:
        await update.message.reply_text(f"â {error}")
        return

    await update.message.reply_text(
        f"â½ TEAM\n\n"
        f"Name: {team.get('name')}\n"
        f"ID: {team.get('id')}\n"
        f"Country: {team.get('country', 'N/A')}"
    )


async def fixtures_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /fixtures Chelsea")
        return

    team, error = find_team(" ".join(context.args))
    if error:
        await update.message.reply_text(f"â {error}")
        return

    fixtures, error = get_team_fixtures(team["id"])
    if error:
        await update.message.reply_text(f"â {error}")
        return

    if not fixtures:
        await update.message.reply_text(
            f"No fixtures found for {team['name']} in season {FIXTURE_SEASON}."
        )
        return

    lines = [
        f"ð {team['name']} â Last {SAMPLE_SIZE}",
        f"Season: {FIXTURE_SEASON}",
        "",
    ]

    for fixture in fixtures:
        teams = fixture.get("teams", {})
        goals = fixture.get("goals", {})
        date = fixture.get("fixture", {}).get("date", "")[:10]
        home = teams.get("home", {}).get("name", "?")
        away = teams.get("away", {}).get("name", "?")
        gh = goals.get("home")
        ga = goals.get("away")
        score = f"{gh}-{ga}" if gh is not None and ga is not None else "vs"
        lines.append(f"{date}  {home} {score} {away}")

    await update.message.reply_text("\n".join(lines))


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = parse_match(" ".join(context.args)) if context.args else None

    if not match:
        await update.message.reply_text(
            "Usage: /analyze Team A vs Team B"
        )
        return

    await update.message.reply_text("ð Analyzing historical statistics...")

    result, error = await asyncio.to_thread(
        analyze_match, match[0], match[1]
    )

    if error:
        await update.message.reply_text(f"â {error}")
        return

    await update.message.reply_text(format_analysis(result))


async def pick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = parse_match(" ".join(context.args)) if context.args else None

    if not match:
        await update.message.reply_text("Usage: /pick Team A vs Team B")
        return

    result, error = await asyncio.to_thread(
        analyze_match, match[0], match[1]
    )

    if error:
        await update.message.reply_text(f"â {error}")
        return

    best = result["markets"][0]

    await update.message.reply_text(
        f"ð¯ TOUCHLINE TIPSTER PICK\n\n"
        f"ðï¸ {result['home']['name']} vs {result['away']['name']}\n\n"
        f"ð {best['market']}\n"
        f"Confidence: {best['confidence']:.0f}%\n"
        f"{best['risk']}\n"
        f"Advice: {best['advice']}\n\n"
        "â ï¸ Statistical guidance only."
    )


async def markets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = parse_match(" ".join(context.args)) if context.args else None

    if not match:
        await update.message.reply_text("Usage: /markets Team A vs Team B")
        return

    result, error = await asyncio.to_thread(
        analyze_match, match[0], match[1]
    )

    if error:
        await update.message.reply_text(f"â {error}")
        return

    lines = [
        f"ð¯ MARKETS â {result['home']['name']} vs {result['away']['name']}",
        "",
    ]

    for item in result["markets"]:
        lines.append(
            f"{item['risk']} {item['market']}: "
            f"{item['confidence']:.0f}% â {item['advice']}"
        )

    await update.message.reply_text("\n".join(lines))


async def h2h_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = parse_match(" ".join(context.args)) if context.args else None

    if not match:
        await update.message.reply_text("Usage: /h2h Team A vs Team B")
        return

    home, error = find_team(match[0])
    if error:
        await update.message.reply_text(f"â {error}")
        return

    away, error = find_team(match[1])
    if error:
        await update.message.reply_text(f"â {error}")
        return

    fixtures, error = get_h2h(home["id"], away["id"])
    if error:
        await update.message.reply_text(f"â {error}")
        return

    if not fixtures:
        await update.message.reply_text("No H2H data was returned.")
        return

    lines = [
        f"ð¤ H2H â {home['name']} vs {away['name']}",
        "",
    ]

    for fixture in fixtures:
        teams = fixture.get("teams", {})
        goals = fixture.get("goals", {})
        date = fixture.get("fixture", {}).get("date", "")[:10]
        hn = teams.get("home", {}).get("name", "?")
        an = teams.get("away", {}).get("name", "?")
        lines.append(
            f"{date}  {hn} {goals.get('home')}-{goals.get('away')} {an}"
        )

    await update.message.reply_text("\n".join(lines))


async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /code YOUR_SPORTYBET_CODE")
        return

    await update.message.reply_text(
        analyze_sportybet_code(" ".join(context.args))
    )


async def apitest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data, error = await asyncio.to_thread(api_get, "/status", {})

    if error:
        await update.message.reply_text(
            f"â Football API connection failed.\n\n{error}"
        )
        return

    account = data.get("response", {}).get("account", {})
    subscription = data.get("response", {}).get("subscription", {})

    await update.message.reply_text(
        "â Football API connection works.\n\n"
        f"Plan: {subscription.get('plan', 'N/A')}\n"
        f"Active: {subscription.get('active', 'N/A')}\n"
        f"Account: {account.get('firstname', 'N/A')}"
    )


# ------------------------------------------------------------
# NATURAL LANGUAGE HANDLER
# ------------------------------------------------------------

async def natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    lower = text.lower()

    if not text:
        return

    if "sportybet code" in lower or lower.startswith("code "):
        code = text
        code = code.replace("SportyBet code", "").replace(
            "sportybet code", ""
        ).strip()

        if code.lower().startswith("code "):
            code = code[5:].strip()

        await update.message.reply_text(analyze_sportybet_code(code))
        return

    match = parse_match(text)

    if not match:
        return

    result, error = await asyncio.to_thread(
        analyze_match, match[0], match[1]
    )

    if error:
        await update.message.reply_text(f"â {error}")
        return

    market = requested_market(text)

    if market:
        selected = next(
            (item for item in result["markets"] if item["market"] == market),
            None,
        )

        if selected:
            await update.message.reply_text(
                f"ð¯ {market}\n\n"
                f"ðï¸ {result['home']['name']} vs {result['away']['name']}\n"
                f"Confidence: {selected['confidence']:.0f}%\n"
                f"{selected['risk']}\n"
                f"Advice: {selected['advice']}\n\n"
                "â ï¸ Statistical guidance only."
            )
            return

    await update.message.reply_text(format_analysis(result))


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    print(f"Touchline Tipster v{VERSION} running on port {PORT}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("team", team_command))
    app.add_handler(CommandHandler("fixtures", fixtures_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("pick", pick_command))
    app.add_handler(CommandHandler("markets", markets_command))
    app.add_handler(CommandHandler("h2h", h2h_command))
    app.add_handler(CommandHandler("code", code_command))
    app.add_handler(CommandHandler("apitest", apitest_command))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            natural_language,
        )
    )

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
