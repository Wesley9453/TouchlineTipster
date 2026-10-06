import os
import re
import math
import threading
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
# TOUCHLINE TIPSTER v4.4
# Fixed API-key handling for Render
# ============================================================

VERSION = "4.4"
API_BASE = "https://v3.football.api-sports.io"
FIXTURE_SEASON = 2024
SAMPLE_SIZE = 5
REQUEST_TIMEOUT = 20
PORT = int(os.environ.get("PORT", "10000"))


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Touchline Tipster v4.4 is running.")

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


# ============================================================
# FIXED API KEY HANDLING
# ============================================================

def get_api_key():
    """
    Reads FOOTBALL_API_KEY directly from the Render environment.

    Handles accidental:
      - leading/trailing spaces
      - surrounding quotes
      - FOOTBALL_API_KEY= pasted into the value

    Never prints the secret.
    """
    key = os.environ.get("FOOTBALL_API_KEY")

    if key is None:
        return None

    key = key.strip()

    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        key = key[1:-1].strip()

    if key.upper().startswith("FOOTBALL_API_KEY="):
        key = key.split("=", 1)[1].strip().strip("'\"")

    return key or None


def api_headers():
    key = get_api_key()

    if not key:
        return None

    return {
        "x-apisports-key": key,
        "Accept": "application/json",
        "User-Agent": "TouchlineTipster/4.4",
    }


def api_get(endpoint, params=None):
    """
    Central API-Sports request function.
    Returns (data, error).
    """
    headers = api_headers()

    if headers is None:
        return None, (
            "FOOTBALL_API_KEY is missing.\n\n"
            "The running Render service cannot see the environment "
            "variable named FOOTBALL_API_KEY."
        )

    try:
        response = requests.get(
            API_BASE + endpoint,
            headers=headers,
            params=params or {},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, f"API connection error: {exc}"

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.status_code == 403:
        errors = data.get("errors", {}) if isinstance(data, dict) else {}
        token_error = (
            errors.get("token")
            or errors.get("error")
            or "Access forbidden by API-Sports."
        )
        return None, f"API HTTP 403: {token_error}"

    if response.status_code == 401:
        return None, "API HTTP 401: authentication rejected."

    if response.status_code >= 400:
        errors = data.get("errors", {}) if isinstance(data, dict) else {}
        if errors:
            return None, f"API HTTP {response.status_code}: {errors}"
        return None, f"API HTTP {response.status_code}: request failed."

    if not isinstance(data, dict):
        return None, "API returned an invalid response."

    errors = data.get("errors", {})
    if errors:
        return None, f"API error: {errors}"

    return data, None


# ============================================================
# API TEST
# ============================================================

def api_status_test():
    """
    Tests API-Sports /status without exposing the API key.
    """
    key = get_api_key()

    if not key:
        return {
            "ok": False,
            "message": "FOOTBALL_API_KEY is missing.",
            "key_found": False,
        }

    data, error = api_get("/status")

    if error:
        return {
            "ok": False,
            "message": error,
            "key_found": True,
        }

    response = data.get("response", {})
    account = response.get("account", {}) if isinstance(response, dict) else {}
    subscription = (
        response.get("subscription", {})
        if isinstance(response, dict)
        else {}
    )

    return {
        "ok": True,
        "message": "Football API connection works.",
        "key_found": True,
        "account": account,
        "subscription": subscription,
    }


# ============================================================
# BASIC HELPERS
# ============================================================

def clamp(value, low, high):
    return max(low, min(high, value))


def poisson_probability(lam, goals):
    if lam <= 0:
        return 1.0 if goals == 0 else 0.0

    return math.exp(-lam) * (lam ** goals) / math.factorial(goals)


def poisson_distribution(lam, max_goals=10):
    values = [
        poisson_probability(lam, goals)
        for goals in range(max_goals + 1)
    ]

    total = sum(values)

    if total == 0:
        return values

    return [value / total for value in values]


# ============================================================
# TEAM SEARCH
# ============================================================

def find_team(team_name):
    data, error = api_get(
        "/teams",
        {"search": team_name},
    )

    if error:
        return [], error

    results = []

    for item in data.get("response", []):
        team = item.get("team", {})

        if team.get("id") and team.get("name"):
            results.append({
                "id": team["id"],
                "name": team["name"],
                "country": team.get("country", ""),
            })

    return results, None


# ============================================================
# FIXTURES
# ============================================================

def get_team_fixtures(team_id, season=FIXTURE_SEASON, last=None):
    params = {
        "team": team_id,
        "season": season,
    }

    if last:
        params["last"] = last

    data, error = api_get("/fixtures", params)

    if error:
        return [], error

    return data.get("response", []), None


# ============================================================
# H2H
# ============================================================

def get_h2h(home_id, away_id):
    data, error = api_get(
        "/fixtures/headtohead",
        {
            "h2h": f"{home_id}-{away_id}",
            "season": FIXTURE_SEASON,
        },
    )

    if error:
        return [], error

    return data.get("response", []), None


# ============================================================
# MATCH NORMALIZATION
# ============================================================

def normalize_fixture(fixture, team_id):
    teams = fixture.get("teams", {})
    goals = fixture.get("goals", {})

    home = teams.get("home", {})
    away = teams.get("away", {})

    is_home = home.get("id") == team_id

    if is_home:
        gf = goals.get("home")
        ga = goals.get("away")
        venue = "HOME"
    else:
        gf = goals.get("away")
        ga = goals.get("home")
        venue = "AWAY"

    if gf is None or ga is None:
        return None

    gf = int(gf)
    ga = int(ga)

    if gf > ga:
        result = "W"
    elif gf == ga:
        result = "D"
    else:
        result = "L"

    return {
        "date": fixture.get("fixture", {}).get("date", ""),
        "venue": venue,
        "gf": gf,
        "ga": ga,
        "result": result,
        "total": gf + ga,
        "btts": gf > 0 and ga > 0,
        "over25": gf + ga > 2,
        "clean_sheet": ga == 0,
    }


def collect_matches(team_id, fixtures, venue=None):
    matches = []

    for fixture in fixtures:
        match = normalize_fixture(fixture, team_id)

        if match is None:
            continue

        if venue and match["venue"] != venue:
            continue

        matches.append(match)

    matches.sort(
        key=lambda item: item.get("date", ""),
        reverse=True,
    )

    return matches


# ============================================================
# STATISTICS
# ============================================================

def calculate_stats(matches):
    if not matches:
        return {
            "games": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "gf": 0,
            "ga": 0,
            "avg_gf": 0.0,
            "avg_ga": 0.0,
            "over25": 0.0,
            "btts": 0.0,
            "clean_sheet": 0.0,
            "form": "",
        }

    wins = sum(m["result"] == "W" for m in matches)
    draws = sum(m["result"] == "D" for m in matches)
    losses = sum(m["result"] == "L" for m in matches)

    gf = sum(m["gf"] for m in matches)
    ga = sum(m["ga"] for m in matches)

    total = len(matches)

    return {
        "games": total,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "gf": gf,
        "ga": ga,
        "avg_gf": gf / total,
        "avg_ga": ga / total,
        "over25": 100 * sum(m["over25"] for m in matches) / total,
        "btts": 100 * sum(m["btts"] for m in matches) / total,
        "clean_sheet": 100 * sum(m["clean_sheet"] for m in matches) / total,
        "form": "".join(m["result"] for m in matches),
    }


def get_team_analysis(team_id):
    fixtures, error = get_team_fixtures(
        team_id,
        FIXTURE_SEASON,
        SAMPLE_SIZE,
    )

    if error:
        return None, error

    all_matches = collect_matches(team_id, fixtures)
    home_matches = collect_matches(team_id, fixtures, "HOME")
    away_matches = collect_matches(team_id, fixtures, "AWAY")

    return {
        "last5": calculate_stats(all_matches[:5]),
        "last10": calculate_stats(all_matches[:10]),
        "home": calculate_stats(home_matches[:5]),
        "away": calculate_stats(away_matches[:5]),
    }, None


# ============================================================
# MATHEMATICAL MODEL
# ============================================================

def calculate_xg(home_analysis, away_analysis):
    home = home_analysis["home"]
    away = away_analysis["away"]

    home_xg = (home["avg_gf"] + away["avg_ga"]) / 2
    away_xg = (away["avg_gf"] + home["avg_ga"]) / 2

    # Blend venue statistics with recent overall scoring.
    home_xg = (
        0.70 * home_xg
        + 0.30 * home_analysis["last5"]["avg_gf"]
    )

    away_xg = (
        0.70 * away_xg
        + 0.30 * away_analysis["last5"]["avg_gf"]
    )

    return (
        clamp(home_xg, 0.05, 5.0),
        clamp(away_xg, 0.05, 5.0),
    )


def calculate_probabilities(home_xg, away_xg):
    home_dist = poisson_distribution(home_xg)
    away_dist = poisson_distribution(away_xg)

    home_win = 0
    draw = 0
    away_win = 0

    over15 = 0
    over25 = 0
    over35 = 0
    under45 = 0
    btts = 0
    home_score = 0
    away_score = 0

    scorelines = {}

    for home_goals, home_probability in enumerate(home_dist):
        for away_goals, away_probability in enumerate(away_dist):
            probability = home_probability * away_probability
            total_goals = home_goals + away_goals

            scorelines[(home_goals, away_goals)] = probability

            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability

            if total_goals >= 2:
                over15 += probability

            if total_goals >= 3:
                over25 += probability

            if total_goals >= 4:
                over35 += probability

            if total_goals <= 4:
                under45 += probability

            if home_goals > 0 and away_goals > 0:
                btts += probability

            if home_goals > 0:
                home_score += probability

            if away_goals > 0:
                away_score += probability

    return {
        "home_win": home_win * 100,
        "draw": draw * 100,
        "away_win": away_win * 100,
        "1x": (home_win + draw) * 100,
        "x2": (draw + away_win) * 100,
        "over15": over15 * 100,
        "over25": over25 * 100,
        "over35": over35 * 100,
        "under45": under45 * 100,
        "btts": btts * 100,
        "home_score": home_score * 100,
        "away_score": away_score * 100,
        "scorelines": scorelines,
    }


# ============================================================
# CONFIDENCE / RISK
# ============================================================

def confidence(probability, consistency=70):
    value = (
        probability * 0.75
        + consistency * 0.15
        + 100 * 0.10
    )

    return round(clamp(value, 0, 100))


def risk_label(conf):
    if conf >= 75:
        return "ð¢ GREEN â Strong"
    if conf >= 60:
        return "ð¡ YELLOW â Moderate"
    return "ð´ RED â High Risk"


def consistency_score(home_stats, away_stats):
    values = [
        home_stats["over25"],
        away_stats["over25"],
        home_stats["btts"],
        away_stats["btts"],
    ]

    spread = max(values) - min(values)

    return clamp(100 - spread * 0.7, 40, 95)


def build_markets(
    home_name,
    away_name,
    home_analysis,
    away_analysis,
    probabilities,
):
    consistency = consistency_score(
        home_analysis["last5"],
        away_analysis["last5"],
    )

    raw = [
        (f"{home_name} or Draw (1X)", probabilities["1x"]),
        (f"{away_name} or Draw (X2)", probabilities["x2"]),
        ("Over 1.5 Goals", probabilities["over15"]),
        ("Under 4.5 Goals", probabilities["under45"]),
        ("Both Teams To Score", probabilities["btts"]),
        (f"{home_name} To Score", probabilities["home_score"]),
        (f"{away_name} To Score", probabilities["away_score"]),
        (f"{home_name} Win", probabilities["home_win"]),
        ("Draw", probabilities["draw"]),
        (f"{away_name} Win", probabilities["away_win"]),
        ("Over 2.5 Goals", probabilities["over25"]),
        ("Over 3.5 Goals", probabilities["over35"]),
    ]

    markets = []

    for market, probability in raw:
        conf = confidence(probability, consistency)

        markets.append({
            "market": market,
            "probability": round(probability, 1),
            "confidence": conf,
            "risk": risk_label(conf),
        })

    markets.sort(
        key=lambda item: item["confidence"],
        reverse=True,
    )

    return markets


# ============================================================
# NATURAL LANGUAGE MATCH PARSING
# ============================================================

def parse_match(text):
    text = text.strip()

    text = re.sub(
        r"^(analyze|analyse|pick|select|predict|tip)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Remove common market suffixes when user asks:
    # Chelsea vs Arsenal Over 2.5
    text = re.sub(
        r"\s+(over|under)\s+\d+(?:\.\d+)?\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s+btts\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    patterns = [
        r"^(.+?)\s+vs\.?\s+(.+)$",
        r"^(.+?)\s+v\s+(.+)$",
        r"^(.+?)\s+-\s+(.+)$",
    ]

    for pattern in patterns:
        match = re.match(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            home = match.group(1).strip()
            away = match.group(2).strip()

            if home and away:
                return home, away

    return None, None


# ============================================================
# FORMATTING
# ============================================================

def format_stats(title, stats):
    return (
        f"{title}\n"
        f"Form: {stats['form'] or 'N/A'} | "
        f"W/D/L: {stats['wins']}/{stats['draws']}/{stats['losses']}\n"
        f"GF: {stats['gf']} | GA: {stats['ga']} | "
        f"Avg GF: {stats['avg_gf']:.2f} | "
        f"Avg GA: {stats['avg_ga']:.2f}\n"
        f"Over 2.5: {stats['over25']:.0f}% | "
        f"BTTS: {stats['btts']:.0f}% | "
        f"Clean sheets: {stats['clean_sheet']:.0f}%"
    )


def format_analysis(
    home_name,
    away_name,
    home_analysis,
    away_analysis,
    h2h,
    probabilities,
    markets,
):
    home_xg, away_xg = calculate_xg(
        home_analysis,
        away_analysis,
    )

    top_scores = sorted(
        probabilities["scorelines"].items(),
        key=lambda item: item[1],
        reverse=True,
    )[:3]

    lines = [
        f"â½ TOUCHLINE TIPSTER v{VERSION}",
        "",
        f"ðï¸ {home_name} vs {away_name}",
        f"Season: {FIXTURE_SEASON}",
        f"Sample: Last {SAMPLE_SIZE} available matches",
        "",
        format_stats(
            f"ð  {home_name} â Recent",
            home_analysis["last5"],
        ),
        "",
        format_stats(
            f"ð  {home_name} â Home",
            home_analysis["home"],
        ),
        "",
        format_stats(
            f"âï¸ {away_name} â Recent",
            away_analysis["last5"],
        ),
        "",
        format_stats(
            f"âï¸ {away_name} â Away",
            away_analysis["away"],
        ),
        "",
        "ð§® MATHEMATICAL MODEL",
        f"Expected goals: {home_name} {home_xg:.2f} "
        f"â {away_name} {away_xg:.2f}",
        "",
        "ð MODEL PROBABILITIES",
        f"{home_name} Win: {probabilities['home_win']:.1f}%",
        f"Draw: {probabilities['draw']:.1f}%",
        f"{away_name} Win: {probabilities['away_win']:.1f}%",
        f"1X: {probabilities['1x']:.1f}%",
        f"X2: {probabilities['x2']:.1f}%",
        f"Over 1.5: {probabilities['over15']:.1f}%",
        f"Over 2.5: {probabilities['over25']:.1f}%",
        f"Over 3.5: {probabilities['over35']:.1f}%",
        f"Under 4.5: {probabilities['under45']:.1f}%",
        f"BTTS: {probabilities['btts']:.1f}%",
        "",
        "ð¯ TOP BETTING OPTIONS",
    ]

    for item in markets[:6]:
        lines.append(
            f"{item['risk']} | {item['market']} | "
            f"Model: {item['probability']:.1f}% | "
            f"Confidence: {item['confidence']}%"
        )

    lines.extend([
        "",
        "ð¯ TOP SCORELINES",
    ])

    for (home_goals, away_goals), probability in top_scores:
        lines.append(
            f"{home_goals}-{away_goals}: "
            f"{probability * 100:.1f}%"
        )

    lines.extend([
        "",
        f"ð¤ H2H records found: {len(h2h)}",
        "",
        "â ï¸ Confidence is model-based, not a guarantee.",
        "Odds are not treated as probability.",
    ])

    return "\n".join(lines)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

WELCOME = f"""â½ Welcome to Touchline Tipster v{VERSION}

Deep football statistics, mathematical modelling,
confidence/risk scoring and betting-market analysis.

Commands:

/team Chelsea
/fixtures Chelsea
/analyze Chelsea vs Arsenal
/pick Chelsea vs Arsenal
/apitest

Or simply send:

Chelsea vs Arsenal
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def apitest_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    result = api_status_test()

    if not result["key_found"]:
        await update.message.reply_text(
            "â Football API connection failed.\n\n"
            "FOOTBALL_API_KEY is missing.\n\n"
            "The running Render service cannot see the variable.\n\n"
            "Go to:\n"
            "Render â Your Service â Environment\n\n"
            "Make sure the variable is exactly:\n"
            "FOOTBALL_API_KEY\n\n"
            "Then save/redeploy."
        )
        return

    if not result["ok"]:
        await update.message.reply_text(
            "â Football API connection failed.\n\n"
            f"{result['message']}\n\n"
            "Environment variable: FOUND\n"
            "API key value: HIDDEN\n"
        )
        return

    subscription = result.get("subscription", {})
    plan = ""

    if isinstance(subscription, dict):
        plan = subscription.get("plan", "")

    message = (
        "â Football API connection works.\n\n"
        "Environment variable: FOUND\n"
        "Authentication: ACCEPTED\n"
        "API endpoint: /status\n"
    )

    if plan:
        message += f"Plan: {plan}\n"

    await update.message.reply_text(message)


async def team_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.args:
        await update.message.reply_text(
            "Usage: /team Chelsea"
        )
        return

    query = " ".join(context.args)

    teams, error = find_team(query)

    if error:
        await update.message.reply_text(
            f"â {error}"
        )
        return

    if not teams:
        await update.message.reply_text(
            f"No teams found for: {query}"
        )
        return

    lines = [
        f"ð Team results for: {query}",
        "",
    ]

    for team in teams[:10]:
        lines.append(
            f"{team['name']} â ID {team['id']} "
            f"({team['country']})"
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


async def fixtures_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.args:
        await update.message.reply_text(
            "Usage: /fixtures Chelsea"
        )
        return

    query = " ".join(context.args)

    teams, error = find_team(query)

    if error:
        await update.message.reply_text(
            f"â {error}"
        )
        return

    if not teams:
        await update.message.reply_text(
            f"No team found for: {query}"
        )
        return

    team = teams[0]

    fixtures, error = get_team_fixtures(
        team["id"],
        FIXTURE_SEASON,
    )

    if error:
        await update.message.reply_text(
            f"â {error}"
        )
        return

    if not fixtures:
        await update.message.reply_text(
            f"No fixtures found for {team['name']} "
            f"in season {FIXTURE_SEASON}."
        )
        return

    lines = [
        f"â½ {team['name']} fixtures",
        f"Season: {FIXTURE_SEASON}",
        "",
    ]

    for fixture in fixtures[:10]:
        fixture_teams = fixture.get("teams", {})

        home = fixture_teams.get(
            "home",
            {},
        ).get("name", "?")

        away = fixture_teams.get(
            "away",
            {},
        ).get("name", "?")

        date = fixture.get(
            "fixture",
            {},
        ).get("date", "?")

        lines.append(
            f"{date[:10]} â {home} vs {away}"
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


async def run_match_analysis(
    update: Update,
    text: str,
):
    home_query, away_query = parse_match(text)

    if not home_query or not away_query:
        await update.message.reply_text(
            "Give me a match like: Chelsea vs Arsenal\n\n"
            "Examples:\n"
            "â¢ Analyze Chelsea vs Arsenal\n"
            "â¢ Pick Chelsea vs Arsenal\n"
            "â¢ Chelsea vs Arsenal Over 2.5"
        )
        return

    await update.message.reply_text(
        f"ð Analyzing {home_query} vs {away_query}..."
    )

    home_teams, error = find_team(home_query)

    if error:
        await update.message.reply_text(
            f"â {error}"
        )
        return

    away_teams, error = find_team(away_query)

    if error:
        await update.message.reply_text(
            f"â {error}"
        )
        return

    if not home_teams:
        await update.message.reply_text(
            f"â Could not find: {home_query}"
        )
        return

    if not away_teams:
        await update.message.reply_text(
            f"â Could not find: {away_query}"
        )
        return

    home = home_teams[0]
    away = away_teams[0]

    home_analysis, error = get_team_analysis(
        home["id"]
    )

    if error:
        await update.message.reply_text(
            f"â {error}"
        )
        return

    away_analysis, error = get_team_analysis(
        away["id"]
    )

    if error:
        await update.message.reply_text(
            f"â {error}"
        )
        return

    h2h, _ = get_h2h(
        home["id"],
        away["id"],
    )

    home_xg, away_xg = calculate_xg(
        home_analysis,
        away_analysis,
    )

    probabilities = calculate_probabilities(
        home_xg,
        away_xg,
    )

    markets = build_markets(
        home["name"],
        away["name"],
        home_analysis,
        away_analysis,
        probabilities,
    )

    message = format_analysis(
        home["name"],
        away["name"],
        home_analysis,
        away_analysis,
        h2h,
        probabilities,
        markets,
    )

    await update.message.reply_text(message)


async def analyze_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.args:
        await update.message.reply_text(
            "Usage: /analyze Chelsea vs Arsenal"
        )
        return

    await run_match_analysis(
        update,
        " ".join(context.args),
    )


async def pick_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.args:
        await update.message.reply_text(
            "Usage: /pick Chelsea vs Arsenal"
        )
        return

    await run_match_analysis(
        update,
        " ".join(context.args),
    )


async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = update.message.text or ""

    if (
        re.search(r"\s+vs\.?\s+", text, flags=re.IGNORECASE)
        or re.search(r"\s+v\s+", text, flags=re.IGNORECASE)
    ):
        await run_match_analysis(update, text)


# ============================================================
# MAIN
# ============================================================

def main():
    start_health_server()

    telegram_token = os.environ.get(
        "TELEGRAM_BOT_TOKEN",
        "",
    ).strip()

    if not telegram_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from Render."
        )

    print(
        f"Touchline Tipster v{VERSION} "
        f"is running on port {PORT}."
    )

    print(
        "FOOTBALL_API_KEY:",
        "FOUND" if get_api_key() else "MISSING",
    )

    application = (
        Application.builder()
        .token(telegram_token)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("apitest", apitest_command)
    )

    application.add_handler(
        CommandHandler("team", team_command)
    )

    application.add_handler(
        CommandHandler("fixtures", fixtures_command)
    )

    application.add_handler(
        CommandHandler("analyze", analyze_command)
    )

    application.add_handler(
        CommandHandler("pick", pick_command)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message,
        )
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
