import os
import re
import math
import asyncio
import threading
from datetime import datetime, timezone
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
# TOUCHLINE TIPSTER v4.0
# ============================================================

VERSION = "4.0"
OPENFOOT_BASE = os.getenv("OPENFOOT_API_BASE", "https://openfootapi.com/v1")
OPENFOOT_API_KEY = os.getenv("OPENFOOT_API_KEY", os.getenv("FOOTBALL_API_KEY", ""))
SPORTYBET_BASE = os.getenv("SPORTYBET_API_BASE_URL", "https://www.sportybet.com")
SPORTYBET_REGION = os.getenv("SPORTYBET_REGION", "gh").lower()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

PORT = int(os.getenv("PORT", "10000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
MAX_MATCHES = 100

# Target odds supported by the command engine.
MIN_ODDS = 1.50
MAX_ODDS = 100_000_000.00


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"Touchline Tipster v{VERSION} is running."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Touchline Tipster v{VERSION} is running on port {PORT}.")
    server.serve_forever()


# ============================================================
# HTTP HELPERS
# ============================================================

def get_json(url, params=None, headers=None):
    try:
        h = {"Accept": "application/json"}
        if headers:
            h.update(headers)

        response = requests.get(
            url,
            params=params,
            headers=h,
            timeout=REQUEST_TIMEOUT,
        )

        try:
            data = response.json()
        except Exception:
            data = {}

        if not response.ok:
            return None, f"HTTP {response.status_code}: {data}"

        return data, None

    except requests.RequestException as exc:
        return None, str(exc)


def openfoot_get(path, params=None):
    headers = {}
    if OPENFOOT_API_KEY:
        headers["Authorization"] = f"Bearer {OPENFOOT_API_KEY}"

    return get_json(f"{OPENFOOT_BASE.rstrip('/')}/{path.lstrip('/')}", params, headers)


def sportybet_get(path, params=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Current-Country": SPORTYBET_REGION.upper(),
    }

    return get_json(
        f"{SPORTYBET_BASE.rstrip('/')}/{path.lstrip('/')}",
        params,
        headers,
    )


# ============================================================
# GENERAL UTILITIES
# ============================================================

def clean_name(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def fmt_odds(value):
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "N/A"


def clamp(value, low, high):
    return max(low, min(high, value))


def risk_from_odds(odds):
    """A simple risk label based on individual odds, not a guarantee."""
    try:
        o = float(odds)
    except Exception:
        return "ð´ HIGH"

    if o <= 1.35:
        return "ð¢ LOW"
    if o <= 1.75:
        return "ð¡ MEDIUM"
    return "ð´ HIGH"


def confidence_from_odds(odds):
    """
    Converts individual decimal odds to an indicative probability.
    This is not a bookmaker guarantee and does not remove margin.
    """
    try:
        o = float(odds)
        if o <= 1:
            return 50
        return round(clamp((1 / o) * 100, 5, 95))
    except Exception:
        return 0


def extract_code(text):
    # Supports normal six-character SportyBet booking codes and longer
    # custom codes. Avoids treating common words as codes.
    candidates = re.findall(r"\b[A-Za-z0-9]{6,24}\b", text.upper())
    blocked = {
        "ANALYZE", "ANALYSE", "SELECT", "PICK", "MATCHES", "SELECTIONS",
        "SPORTYBET", "TOUCHLINE", "TIPSTER", "BETTING", "AROUND",
        "SAFEST", "CODE", "ODDS", "GAMES",
    }
    for item in candidates:
        if item not in blocked and re.search(r"[A-Z]", item) and re.search(r"\d", item):
            return item
    return None


def parse_number(value):
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def parse_command_requirements(text):
    """
    Distinction locked into v4.0:
      '5 odds'       -> target total odds 5.00
      '5.00 odds'    -> target total odds 5.00
      '5 matches'    -> 5 selections
      '5 games'      -> 5 selections
      '5 selections' -> 5 selections
    """
    low = text.lower()

    target_odds = None
    selection_count = None

    # Number + odds
    odds_patterns = [
        r"\b(?:around|about|at|of|near|target(?:ing)?|make it|give me|pick|select)?\s*"
        r"(\d+(?:[.,]\d+)?)\s*(?:total\s*)?odds\b",
        r"\bodds\s*(?:of|around|about|=|:)?\s*(\d+(?:[.,]\d+)?)\b",
    ]

    for pattern in odds_patterns:
        match = re.search(pattern, low)
        if match:
            target_odds = parse_number(match.group(1))
            break

    # Number + matches/games/selections
    match_patterns = [
        r"\b(\d+)\s*(?:matches|match|games|game|selections|selection|picks|legs)\b",
        r"\b(?:matches|games|selections|picks|legs)\s*(?:of|=|:)?\s*(\d+)\b",
    ]

    for pattern in match_patterns:
        match = re.search(pattern, low)
        if match:
            selection_count = int(match.group(1))
            break

    if target_odds is not None:
        target_odds = clamp(target_odds, MIN_ODDS, MAX_ODDS)

    return target_odds, selection_count


def parse_risk_level(text):
    low = text.lower()

    if any(x in low for x in ("safe", "safest", "low risk", "lower risk", "conservative")):
        return "LOW"
    if any(x in low for x in ("aggressive", "risky", "high risk", "highest")):
        return "HIGH"
    return "BALANCED"


# ============================================================
# SPORTYBET CODE ENGINE
# ============================================================

def flatten_booking_outcomes(payload):
    """
    SportyBet's undocumented web payload can change shape. This parser
    accepts several common shapes and only returns fields it can actually
    identify.
    """
    if not payload:
        return []

    data = payload.get("data", payload)

    possible = []
    if isinstance(data, dict):
        for key in ("outcomes", "selections", "bets", "legs"):
            value = data.get(key)
            if isinstance(value, list):
                possible.extend(value)

    if isinstance(data, list):
        possible.extend(data)

    result = []

    for item in possible:
        if not isinstance(item, dict):
            continue

        event_name = (
            item.get("eventName")
            or item.get("matchName")
            or item.get("eventDesc")
            or item.get("event")
            or ""
        )

        market = (
            item.get("marketName")
            or item.get("marketDesc")
            or item.get("market")
            or item.get("desc")
            or ""
        )

        outcome = (
            item.get("outcomeName")
            or item.get("outcomeDesc")
            or item.get("selection")
            or item.get("outcome")
            or ""
        )

        odds = (
            item.get("odds")
            or item.get("odd")
            or item.get("price")
        )

        # Sometimes outcome is nested.
        nested = item.get("outcome")
        if isinstance(nested, dict):
            odds = odds or nested.get("odds") or nested.get("price")
            outcome = (
                nested.get("desc")
                or nested.get("name")
                or nested.get("outcomeName")
                or outcome
            )

        try:
            odds_float = float(odds)
        except Exception:
            continue

        result.append({
            "event": clean_name(event_name) or "Unknown match",
            "market": clean_name(market) or "Unknown market",
            "selection": clean_name(outcome) or "Unknown selection",
            "odds": odds_float,
        })

    return result


def resolve_sportybet_code(code):
    path = f"/api/{SPORTYBET_REGION}/orders/share/{code}"
    payload, error = sportybet_get(path)

    if error:
        return [], error

    selections = flatten_booking_outcomes(payload)

    if not selections:
        return [], (
            "The code was reached, but SportyBet did not return readable "
            "selection details in the response."
        )

    return selections, None


def selection_score(selection, risk_level="BALANCED"):
    odds = selection["odds"]

    # Lower odds are generally less volatile, but don't call them guaranteed.
    base = confidence_from_odds(odds)

    if risk_level == "LOW":
        risk_bonus = max(0, 20 - odds * 5)
    elif risk_level == "HIGH":
        risk_bonus = min(20, odds * 2)
    else:
        risk_bonus = max(0, 12 - odds * 2)

    return base + risk_bonus


def select_best_selections(selections, count=None, target_odds=None, risk_level="BALANCED"):
    if not selections:
        return []

    ranked = sorted(
        selections,
        key=lambda x: selection_score(x, risk_level),
        reverse=True,
    )

    if target_odds is None:
        return ranked[:count] if count else ranked

    # Build a combination close to the target while prioritizing stronger
    # selections. For very large targets, greedily add strong selections.
    selected = []
    combined = 1.0

    candidates = ranked[:]

    if risk_level == "LOW":
        candidates = sorted(candidates, key=lambda x: x["odds"])

    for item in candidates:
        if count is not None and len(selected) >= count:
            break

        new_total = combined * item["odds"]

        # Prefer not to overshoot when alternatives exist.
        if new_total <= target_odds * 1.08 or not selected:
            selected.append(item)
            combined = new_total

        if combined >= target_odds * 0.97:
            break

    # If a requested count is larger, fill it with best unused selections.
    if count and len(selected) < count:
        used = {id(x) for x in selected}
        for item in ranked:
            if id(item) in used:
                continue
            selected.append(item)
            if len(selected) >= count:
                break

    return selected


def combined_odds(selections):
    total = 1.0
    for item in selections:
        try:
            total *= float(item["odds"])
        except Exception:
            pass
    return total


def format_code_analysis(code, selections):
    lines = [
        f"ðï¸ TOUCHLINE TIPSTER v{VERSION}",
        "",
        f"SportyBet code: `{code}`",
        f"Selections detected: {len(selections)}",
        "",
        "ð AVAILABLE SELECTIONS",
        "ââââââââââââââââââ",
    ]

    for i, item in enumerate(selections, 1):
        lines.append(
            f"{i}. {item['event']}\n"
            f"   {item['market']} â {item['selection']} "
            f"@ {fmt_odds(item['odds'])} "
            f"{risk_from_odds(item['odds'])}"
        )

    total = combined_odds(selections)
    lines.extend([
        "",
        f"ð Combined odds of all detected selections: {fmt_odds(total)}",
        "â ï¸ Odds/risk indicators are informational, not guarantees.",
    ])

    return "\n".join(lines)


def format_selected_ticket(selected, target_odds=None, count=None, risk_level="BALANCED"):
    total = combined_odds(selected)

    lines = [
        f"ð TOUCHLINE TIPSTER v{VERSION}",
        "",
        "ð¯ TICKET OPTIMIZER",
        f"Risk mode: {risk_level}",
    ]

    if count:
        lines.append(f"Requested matches: {count}")
    if target_odds:
        lines.append(f"Target odds: {fmt_odds(target_odds)}")

    lines.extend([
        "",
        "SELECTED",
        "ââââââââââââââââââ",
    ])

    for i, item in enumerate(selected, 1):
        confidence = confidence_from_odds(item["odds"])
        lines.append(
            f"{i}. {item['event']}\n"
            f"   {item['market']} â {item['selection']}\n"
            f"   Odds: {fmt_odds(item['odds'])} | {risk_from_odds(item['odds'])} | "
            f"Indicative probability: {confidence}%"
        )

    lines.extend([
        "",
        f"ð Built combined odds: {fmt_odds(total)}",
    ])

    if target_odds:
        difference = total - target_odds
        lines.append(f"ð Difference from target: {difference:+.2f}")

        if total >= target_odds * 0.97 and total <= target_odds * 1.08:
            lines.append("â Target range achieved closely.")
        elif total < target_odds:
            lines.append("â¹ï¸ Target could not be reached closely with the available selections.")
        else:
            lines.append("â ï¸ Result is above the requested target.")

    if total >= 1000:
        lines.append("ð´ VERY HIGH RISK: combined odds are extremely high.")
    elif total >= 100:
        lines.append("ð´ HIGH RISK: combined odds are very high.")
    elif total >= 20:
        lines.append("ð¡ HIGH-VARIANCE TICKET: combined odds are high.")
    else:
        lines.append("â ï¸ No betting outcome is guaranteed.")

    return "\n".join(lines)


# ============================================================
# OPENFOOT MATCH ENGINE
# ============================================================

def search_team(team_name):
    payload, error = openfoot_get("/search", {"q": team_name})

    if error or not payload:
        return None, error or "No response from OpenFoot."

    data = payload.get("data", [])
    if isinstance(data, dict):
        data = data.get("results", data.get("teams", []))

    if not isinstance(data, list) or not data:
        return None, f"No team found for {team_name}."

    # Prefer a team result.
    for item in data:
        if isinstance(item, dict) and item.get("type", "team") in ("team", "club"):
            return item, None

    return data[0], None


def get_team_matches(team_id, status="finished", limit=10):
    params = {
        "team": team_id,
        "status": status,
    }

    payload, error = openfoot_get("/matches", params)

    if error or not payload:
        return [], error or "No response from OpenFoot."

    data = payload.get("data", [])
    if not isinstance(data, list):
        return [], "Unexpected matches response."

    # For finished matches, newest first is normally returned. Sort safely.
    def kickoff_key(item):
        return item.get("kickoffAt", "")

    data = sorted(data, key=kickoff_key, reverse=True)
    return data[:limit], None


def match_goals(match):
    score = match.get("score") or match.get("scores") or {}

    if isinstance(score, dict):
        home = (
            score.get("home")
            or score.get("homeScore")
            or score.get("homeGoals")
        )
        away = (
            score.get("away")
            or score.get("awayScore")
            or score.get("awayGoals")
        )
        if isinstance(home, dict):
            home = home.get("current") or home.get("fullTime")
        if isinstance(away, dict):
            away = away.get("current") or away.get("fullTime")

    else:
        home = away = None

    try:
        return int(home), int(away)
    except Exception:
        return None, None


def calculate_team_stats(matches, team_id):
    rows = []

    for match in matches:
        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})

        home_id = home.get("id")
        away_id = away.get("id")
        hg, ag = match_goals(match)

        if hg is None or ag is None:
            continue

        if home_id == team_id:
            gf, ga = hg, ag
        elif away_id == team_id:
            gf, ga = ag, hg
        else:
            continue

        result = "W" if gf > ga else "D" if gf == ga else "L"
        rows.append({
            "gf": gf,
            "ga": ga,
            "result": result,
            "total": gf + ga,
            "btts": gf > 0 and ga > 0,
            "over15": gf + ga > 1,
            "over25": gf + ga > 2,
            "clean": ga == 0,
        })

    if not rows:
        return None

    n = len(rows)

    return {
        "form": "".join(x["result"] for x in rows),
        "wins": sum(x["result"] == "W" for x in rows),
        "draws": sum(x["result"] == "D" for x in rows),
        "losses": sum(x["result"] == "L" for x in rows),
        "avg_gf": sum(x["gf"] for x in rows) / n,
        "avg_ga": sum(x["ga"] for x in rows) / n,
        "over15": sum(x["over15"] for x in rows) / n * 100,
        "over25": sum(x["over25"] for x in rows) / n * 100,
        "btts": sum(x["btts"] for x in rows) / n * 100,
        "clean": sum(x["clean"] for x in rows) / n * 100,
        "sample": n,
    }


def format_team_stats(name, stats):
    if not stats:
        return f"{name}: insufficient completed-match data."

    return (
        f"ð  {name}\n"
        f"Form: {stats['form']} | W/D/L: "
        f"{stats['wins']}/{stats['draws']}/{stats['losses']}\n"
        f"Goals: {stats['avg_gf']:.2f} scored / {stats['avg_ga']:.2f} conceded\n"
        f"Over 1.5: {stats['over15']:.0f}% | "
        f"Over 2.5: {stats['over25']:.0f}% | "
        f"BTTS: {stats['btts']:.0f}% | "
        f"Clean sheet: {stats['clean']:.0f}%"
    )


async def analyze_match(home_name, away_name):
    home_team, home_error = await asyncio.to_thread(search_team, home_name)
    away_team, away_error = await asyncio.to_thread(search_team, away_name)

    if not home_team:
        return f"â Could not find the home team: {home_name}\n{home_error or ''}"
    if not away_team:
        return f"â Could not find the away team: {away_name}\n{away_error or ''}"

    home_id = home_team.get("id")
    away_id = away_team.get("id")

    home_matches, e1 = await asyncio.to_thread(get_team_matches, home_id, "finished", 10)
    away_matches, e2 = await asyncio.to_thread(get_team_matches, away_id, "finished", 10)

    hs = calculate_team_stats(home_matches, home_id)
    aws = calculate_team_stats(away_matches, away_id)

    if not hs or not aws:
        return (
            f"â½ TOUCHLINE TIPSTER v{VERSION}\n\n"
            f"ðï¸ {home_name} vs {away_name}\n\n"
            "â Not enough completed-match data was returned."
        )

    # Market signals are descriptive, not guaranteed predictions.
    over15 = (hs["over15"] + aws["over15"]) / 2
    over25 = (hs["over25"] + aws["over25"]) / 2
    btts = (hs["btts"] + aws["btts"]) / 2
    under45 = 100 - (
        (
            sum(x["total"] > 4 for x in [
                {"total": 0}
            ])
        )
    )

    # Under 4.5 from available average goal rates as a simple indicator.
    expected_total = hs["avg_gf"] + hs["avg_ga"] + aws["avg_gf"] + aws["avg_ga"]
    under45_indicator = clamp(100 - max(0, expected_total - 3) * 20, 45, 95)

    markets = [
        ("Over 1.5 Goals", over15),
        ("Over 2.5 Goals", over25),
        ("BTTS", btts),
        ("Under 4.5 Goals", under45_indicator),
    ]

    markets.sort(key=lambda x: x[1], reverse=True)
    best_market, best_conf = markets[0]

    if best_conf >= 80:
        risk = "ð¢ LOW"
    elif best_conf >= 65:
        risk = "ð¡ MEDIUM"
    else:
        risk = "ð´ HIGH"

    lines = [
        f"â½ TOUCHLINE TIPSTER v{VERSION}",
        "",
        f"ðï¸ {home_name} vs {away_name}",
        "",
        format_team_stats(home_name, hs),
        "",
        format_team_stats(away_name, aws),
        "",
        "ð MARKET RANKING",
        "ââââââââââââââââââ",
    ]

    for i, (market, confidence) in enumerate(markets, 1):
        icon = "ð¢" if confidence >= 80 else "ð¡" if confidence >= 65 else "ð´"
        lines.append(f"{icon} {i}. {market} â {confidence:.0f}%")

    lines.extend([
        "",
        f"ð BEST PICK: {best_market}",
        f"Confidence indicator: {best_conf:.0f}%",
        f"Risk: {risk}",
        "",
        "â ï¸ Statistical indicators are not guarantees.",
    ])

    return "\n".join(lines)


# ============================================================
# COMMAND PARSER
# ============================================================

def extract_matchup(text):
    match = re.search(
        r"(.+?)\s+(?:vs\.?|v\.?)\s+(.+?)(?:$|[.!?])",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, None

    home = clean_name(match.group(1))
    away = clean_name(match.group(2))

    # Remove command words from the first side if present.
    home = re.sub(
        r"^(?:analyze|analyse|check|review|study|pick|select)\s+",
        "",
        home,
        flags=re.IGNORECASE,
    )

    return home, away


async def handle_code_request(update, text, code):
    target_odds, selection_count = parse_command_requirements(text)
    risk_level = parse_risk_level(text)

    selections, error = await asyncio.to_thread(resolve_sportybet_code, code)

    if error:
        await update.message.reply_text(
            f"â I couldn't fully read SportyBet code `{code}`.\n\n"
            f"{error}\n\n"
            "The code must be readable through SportyBet's booking-code service. "
            "I will not invent missing selections."
        )
        return

    low = text.lower()

    is_analysis = any(
        word in low for word in ("analyze", "analyse", "check", "review", "study")
    )

    wants_selection = any(
        word in low for word in ("pick", "select", "choose", "give me", "keep")
    )

    if is_analysis and not wants_selection and not target_odds and not selection_count:
        await update.message.reply_text(
            format_code_analysis(code, selections)
        )
        return

    if not wants_selection and not target_odds and not selection_count:
        await update.message.reply_text(
            format_code_analysis(code, selections)
        )
        return

    selected = select_best_selections(
        selections,
        count=selection_count,
        target_odds=target_odds,
        risk_level=risk_level,
    )

    if not selected:
        await update.message.reply_text("â I couldn't build a selection from the code.")
        return

    await update.message.reply_text(
        format_selected_ticket(
            selected,
            target_odds=target_odds,
            count=selection_count,
            risk_level=risk_level,
        )
    )


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"""
â½ TOUCHLINE TIPSTER v{VERSION}

Your football analysis and ticket-selection assistant.

ð§  I understand:
â¢ Analyze
â¢ Pick
â¢ Select
â¢ Choose
â¢ Check
â¢ Review

ðï¸ SPORTYBET
Send a booking code and ask:
â¢ Analyze this code
â¢ Pick the best 5 matches
â¢ Pick the best 5 odds
â¢ Pick 5 matches around 10 odds
â¢ Make it safer

ð ODDS RULE
5 odds = 5.00 total odds
5 matches = 5 selections

ð¯ Supported target:
1.50 â 100,000,000.00 odds

â½ MATCH ANALYSIS
/analyze Chelsea vs Arsenal

Other commands:
/help
/apitest
/version
"""
    await update.message.reply_text(text.strip())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"""
â½ TOUCHLINE TIPSTER v{VERSION}

EXAMPLES

ð Match:
Analyze Chelsea vs Arsenal

ðï¸ SportyBet:
Analyze this code ABC123

ð¯ Number of matches:
Pick 5 matches

ð Total odds:
Pick 5 odds

ð Both:
Pick 5 matches around 10 odds

ð¡ï¸ Risk:
Pick the safest 5 matches
Pick 10 matches, 20 odds, balanced
Make this code safer

ð IMPORTANT
â5 oddsâ means 5.00 combined odds.
â5 matchesâ means 5 selections.

The bot only uses selections it can actually resolve.
No missing SportyBet selection will be invented.
""".strip()
    )


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"â½ Touchline Tipster v{VERSION}\n"
        "Command engine: ON\n"
        "SportyBet engine: ON\n"
        "Ticket optimizer: ON\n"
        "Risk ranking: ON"
    )


async def apitest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload, error = await asyncio.to_thread(
        openfoot_get, "/health"
    )

    if error:
        await update.message.reply_text(
            f"â Football API test failed.\n{error}"
        )
        return

    await update.message.reply_text(
        "â Football API connection works.\n"
        f"OpenFoot response received successfully."
    )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/analyze Chelsea vs Arsenal"
        )
        return

    text = " ".join(context.args)
    home, away = extract_matchup(text)

    if not home or not away:
        await update.message.reply_text(
            "Please use:\n/analyze Team A vs Team B"
        )
        return

    result = await analyze_match(home, away)
    await update.message.reply_text(result)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # SportyBet booking-code flow.
    code = extract_code(text)
    sporty_words = (
        "sportybet", "booking code", "code", "odds", "matches",
        "selections", "pick", "select", "analyze", "analyse"
    )

    if code and any(word in text.lower() for word in sporty_words):
        await handle_code_request(update, text, code)
        return

    # Natural-language match analysis.
    low = text.lower()
    if any(word in low for word in ("analyze", "analyse", "check", "review", "study")):
        home, away = extract_matchup(text)
        if home and away:
            result = await analyze_match(home, away)
            await update.message.reply_text(result)
            return

    if "vs" in low or " v " in low:
        home, away = extract_matchup(text)
        if home and away and any(
            word in low for word in ("pick", "select", "choose")
        ):
            result = await analyze_match(home, away)
            await update.message.reply_text(result)
            return

    await update.message.reply_text(
        "â½ I didn't understand that request.\n\n"
        "Try:\n"
        "â¢ Analyze Chelsea vs Arsenal\n"
        "â¢ Analyze this SportyBet code ABC123\n"
        "â¢ Pick the best 5 matches\n"
        "â¢ Pick the best 5 odds\n"
        "â¢ Pick 5 matches around 10 odds"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Add it to Render Environment Variables."
        )

    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("version", version_command))
    application.add_handler(CommandHandler("apitest", apitest_command))
    application.add_handler(CommandHandler("analyze", analyze_command))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    print(f"Touchline Tipster v{VERSION} starting Telegram polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
