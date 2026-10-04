import os
import math
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# TOUCHLINE TIPSTER v3.9
# AI SAFE-TICKET BUILDER
# ============================================================

VERSION = "3.9"
API = "https://openfootapi.com/v1"
SEASON = os.getenv("FOOTBALL_SEASON", "2026/27")
SAMPLE_SIZE = 5
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
KEY = os.getenv("OPENFOOT_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

MIN_PICK_CONFIDENCE = 62
MAX_PICKS = 20


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"Touchline Tipster v{VERSION} is running.".encode())

    def log_message(self, *args):
        pass


def api_get(path, params=None):
    try:
        headers = {
            "Authorization": f"Bearer {KEY or ''}",
            "x-api-key": KEY or "",
            "Accept": "application/json",
        }
        r = requests.get(API + path, headers=headers, params=params or {}, timeout=20)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        if r.status_code < 400:
            return data, None

        return None, f"HTTP {r.status_code}: {data}"
    except Exception as e:
        return None, str(e)


def arr(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "response", "teams", "fixtures", "results", "matches"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def team_name(obj):
    if not isinstance(obj, dict):
        return str(obj or "")
    for key in ("name", "team_name", "teamName"):
        if obj.get(key):
            return str(obj[key])
    if isinstance(obj.get("team"), dict):
        return team_name(obj["team"])
    return ""


def team_id(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("id", "team_id", "teamId"):
        if obj.get(key) is not None:
            return obj[key]
    if isinstance(obj.get("team"), dict):
        return team_id(obj["team"])
    return None


ALIASES = {
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "man city": "Manchester City",
    "spurs": "Tottenham",
    "wolves": "Wolverhampton",
    "west ham": "West Ham United",
    "newcastle": "Newcastle United",
    "forest": "Nottingham Forest",
    "psg": "Paris Saint Germain",
    "inter milan": "Inter",
    "barca": "Barcelona",
    "atletico": "Atletico Madrid",
}


def norm(value):
    return " ".join(
        str(value).lower().replace("-", " ").replace("_", " ").split()
    )


def find_team(query):
    q = ALIASES.get(norm(query), query)
    data, error = api_get("/teams", {"search": q})
    if error:
        return [], error

    scored = []
    nq = norm(q)

    for item in arr(data):
        name = team_name(item)
        ident = team_id(item)
        if not name or ident is None:
            continue

        nn = norm(name)
        score = 0
        if nq == nn:
            score = 100
        elif nq in nn:
            score = 90
        elif nn in nq:
            score = 80
        elif any(word in nn.split() for word in nq.split()):
            score = 60

        if score:
            scored.append((score, name, ident))

    scored.sort(reverse=True)
    return scored[:10], None


def match_parts(match):
    if not isinstance(match, dict):
        return "", "", None, None, None, None

    teams = match.get("teams", {})
    if not isinstance(teams, dict):
        teams = {}

    home = teams.get("home", {})
    away = teams.get("away", {})

    goals = match.get("goals", {})
    if not isinstance(goals, dict):
        goals = {}

    hg = goals.get("home")
    ag = goals.get("away")

    if hg is None or ag is None:
        score = match.get("score", {})
        ft = score.get("fulltime", {}) if isinstance(score, dict) else {}
        hg = ft.get("home", hg)
        ag = ft.get("away", ag)

    return (
        team_name(home) or str(match.get("home", "")),
        team_name(away) or str(match.get("away", "")),
        hg,
        ag,
        team_id(home),
        team_id(away),
    )


def get_recent_matches(team_id_value):
    data, error = api_get(
        "/matches",
        {
            "team": team_id_value,
            "season": SEASON,
            "status": "finished",
        },
    )
    if error:
        # Some API deployments use fixtures for historical matches.
        data, error = api_get(
            "/fixtures",
            {"team": team_id_value, "season": SEASON},
        )

    if error:
        return [], error

    rows = []
    for match in arr(data):
        home, away, hg, ag, _, _ = match_parts(match)
        if hg is None or ag is None:
            continue
        rows.append(match)

    rows.sort(
        key=lambda x: str(
            x.get("kickoffAt")
            or x.get("date")
            or x.get("fixture", {}).get("date", "")
        ),
        reverse=True,
    )
    return rows[:SAMPLE_SIZE], None


def stats(matches, ident, side=None):
    s = {
        "g": 0, "w": 0, "d": 0, "l": 0,
        "gf": 0, "ga": 0,
        "o15": 0, "o25": 0, "u35": 0, "u45": 0,
        "btts": 0, "sc": 0, "cs": 0,
        "form": [],
    }

    for match in matches:
        hn, an, hg, ag, home_id, away_id = match_parts(match)

        where = None
        if str(home_id) == str(ident):
            where = "home"
        elif str(away_id) == str(ident):
            where = "away"

        if where is None or (side and where != side):
            continue

        gf, ga = (hg, ag) if where == "home" else (ag, hg)
        total = hg + ag

        s["g"] += 1
        s["gf"] += gf
        s["ga"] += ga
        s["o15"] += total > 1
        s["o25"] += total > 2
        s["u35"] += total < 4
        s["u45"] += total < 5
        s["btts"] += gf > 0 and ga > 0
        s["sc"] += gf > 0
        s["cs"] += ga == 0

        if gf > ga:
            s["w"] += 1
            s["form"].append("W")
        elif gf == ga:
            s["d"] += 1
            s["form"].append("D")
        else:
            s["l"] += 1
            s["form"].append("L")

    return s


def pct(n, d):
    return round(100 * n / d) if d else 0


def avg(n, d):
    return n / d if d else 0


def poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def probability_model(home_stats, away_stats, home_venue, away_venue):
    ha = avg(home_stats["gf"], home_stats["g"])
    hd = avg(home_stats["ga"], home_stats["g"])
    aa = avg(away_stats["gf"], away_stats["g"])
    ad = avg(away_stats["ga"], away_stats["g"])

    if home_venue["g"] and away_venue["g"]:
        home_xg = (
            0.65 * ((ha + ad) / 2)
            + 0.35 * (
                (avg(home_venue["gf"], home_venue["g"])
                 + avg(away_venue["ga"], away_venue["g"])) / 2
            )
        )
        away_xg = (
            0.65 * ((aa + hd) / 2)
            + 0.35 * (
                (avg(away_venue["gf"], away_venue["g"])
                 + avg(home_venue["ga"], home_venue["g"])) / 2
            )
        )
    else:
        home_xg = (ha + ad) / 2
        away_xg = (aa + hd) / 2

    home_xg = max(0.15, min(4.5, home_xg))
    away_xg = max(0.15, min(4.5, away_xg))

    hp = [poisson(i, home_xg) for i in range(11)]
    ap = [poisson(i, away_xg) for i in range(11)]

    result = [0.0, 0.0, 0.0]
    scores = []

    for i in range(11):
        for j in range(11):
            p = hp[i] * ap[j]
            result[0 if i > j else 1 if i == j else 2] += p
            scores.append((p, i, j))

    scores.sort(reverse=True)

    total_xg = home_xg + away_xg

    markets = {
        "Double Chance 1X": result[0] + result[1],
        "Double Chance X2": result[1] + result[2],
        "Double Chance 12": result[0] + result[2],
        "Over 0.5 Goals": 1 - poisson(0, total_xg),
        "Over 1.5 Goals": 1 - poisson(0, total_xg) - poisson(1, total_xg),
        "Over 2.5 Goals": 1 - sum(poisson(i, total_xg) for i in range(3)),
        "Under 3.5 Goals": sum(poisson(i, total_xg) for i in range(4)),
        "Under 4.5 Goals": sum(poisson(i, total_xg) for i in range(5)),
        "BTTS": (1 - poisson(0, home_xg)) * (1 - poisson(0, away_xg)),
        "Home Team To Score": 1 - poisson(0, home_xg),
        "Away Team To Score": 1 - poisson(0, away_xg),
        "Home DNB": result[0] / max(result[0] + result[2], 1e-9),
        "Away DNB": result[2] / max(result[0] + result[0], 1e-9),
    }

    return home_xg, away_xg, result, scores, markets


def model_confidence(h, a, hv, av, markets):
    sample_strength = min(100, ((h["g"] + a["g"]) / (SAMPLE_SIZE * 2)) * 100)

    venue_games = hv["g"] + av["g"]
    venue_strength = min(100, venue_games / (SAMPLE_SIZE * 2) * 100)

    consistency = (
        max(
            pct(h["o15"], h["g"]),
            pct(a["o15"], a["g"]),
            pct(h["u45"], h["g"]),
            pct(a["u45"], a["g"]),
        )
        if h["g"] and a["g"] else 0
    )

    confidence = (
        0.35 * sample_strength
        + 0.25 * venue_strength
        + 0.40 * consistency
    )

    return round(max(25, min(90, confidence)))


def pick_for_match(home, away, h, a, hv, av):
    hx, ax, result, scores, markets = probability_model(h, a, hv, av)
    confidence = model_confidence(h, a, hv, av, markets)

    # Safer markets are preferred first. The bot does NOT force a pick.
    priority = [
        "Over 1.5 Goals",
        "Under 4.5 Goals",
        "Double Chance 1X",
        "Double Chance X2",
        "Home Team To Score",
        "Away Team To Score",
        "BTTS",
        "Over 2.5 Goals",
    ]

    candidates = []
    for market in priority:
        probability = markets.get(market, 0)
        if probability < 0.60:
            continue

        market_conf = round(
            max(
                45,
                min(
                    90,
                    0.65 * probability * 100
                    + 0.35 * confidence,
                ),
            )
        )

        if market_conf >= MIN_PICK_CONFIDENCE:
            candidates.append(
                {
                    "market": market,
                    "probability": probability,
                    "confidence": market_conf,
                    "fair_odds": round(1 / probability, 2),
                    "hx": hx,
                    "ax": ax,
                    "markets": markets,
                }
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (x["confidence"], x["probability"]),
        reverse=True,
    )
    best = candidates[0]

    # Very short odds are useful for a safe-ticket builder.
    # The displayed odds are model fair odds unless a bookmaker feed
    # is connected; they are NOT claimed bookmaker prices.
    best["home"] = home
    best["away"] = away
    best["reason"] = build_reason(best["market"], h, a, hv, av)

    return best


def build_reason(market, h, a, hv, av):
    if market == "Over 1.5 Goals":
        return (
            f"{pct(h['o15'], h['g'])}% of {h['g']} recent {'' if not h['g'] else ''}"
            f" matches for {h['sc'] and 'the home team' or 'this team'} cleared 1.5; "
            f"{pct(a['o15'], a['g'])}% for the away side. "
            f"Scoring rates: {pct(h['sc'], h['g'])}% and {pct(a['sc'], a['g'])}%."
        )

    if market == "Under 4.5 Goals":
        return (
            f"Under 4.5 landed in {pct(h['u45'], h['g'])}% of the home team's "
            f"sample and {pct(a['u45'], a['g'])}% of the away team's sample."
        )

    if market == "Double Chance 1X":
        return (
            f"{h['w']}W/{h['d']}D in the recent sample gives the home side "
            f"strong protection against defeat."
        )

    if market == "Double Chance X2":
        return (
            f"{a['w']}W/{a['d']}D in the recent sample gives the away side "
            f"strong protection against defeat."
        )

    if market == "Home Team To Score":
        return f"Home scoring rate: {pct(h['sc'], h['g'])}%."

    if market == "Away Team To Score":
        return f"Away scoring rate: {pct(a['sc'], a['g'])}%."

    if market == "BTTS":
        return (
            f"Both teams scored in {pct(h['btts'], h['g'])}% and "
            f"{pct(a['btts'], a['g'])}% of their recent matches."
        )

    return "Model probability is above the safe-ticket threshold."


def today_fixtures():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data, error = api_get(
        "/matches",
        {
            "date": today,
            "status": "scheduled",
        },
    )

    if error:
        return [], error, today

    return arr(data), None, today


def build_safe_ticket(target_odds):
    matches, error, today = today_fixtures()
    if error:
        return None, error

    if not matches:
        return None, f"No scheduled matches were returned for {today}."

    picks = []

    for match in matches:
        home, away, hg, ag, home_id, away_id = match_parts(match)
        if not home or not away or home_id is None or away_id is None:
            continue

        hm, he = get_recent_matches(home_id)
        am, ae = get_recent_matches(away_id)

        if he or ae or not hm or not am:
            continue

        h = stats(hm, home_id)
        a = stats(am, away_id)
        hv = stats(hm, home_id, "home")
        av = stats(am, away_id, "away")

        pick = pick_for_match(home, away, h, a, hv, av)
        if pick:
            pick["kickoff"] = (
                match.get("kickoffAt")
                or match.get("date")
                or match.get("fixture", {}).get("date", "TBC")
            )
            picks.append(pick)

    # Best confidence first, then shorter fair odds.
    picks.sort(
        key=lambda x: (x["confidence"], x["probability"]),
        reverse=True,
    )

    # Avoid taking two selections from the same fixture.
    selected = []
    seen_matches = set()
    running = 1.0

    for pick in picks:
        key = (pick["home"], pick["away"])
        if key in seen_matches:
            continue

        # Keep the ticket conservative. Do not add a pick that makes
        # the combined fair odds jump excessively beyond the target.
        new_running = running * pick["fair_odds"]

        if new_running <= target_odds * 1.35 or not selected:
            selected.append(pick)
            seen_matches.add(key)
            running = new_running

        if running >= target_odds * 0.90 or len(selected) >= MAX_PICKS:
            break

    if not selected:
        return None, "No match reached the safe-pick threshold today."

    return {
        "date": today,
        "target": target_odds,
        "picks": selected,
        "total_fair_odds": running,
    }, None


def ticket_text(ticket):
    picks = ticket["picks"]

    lines = [
        "ðï¸ TOUCHLINE TIPSTER AI",
        "",
        f"ð Today's safe ticket â target {ticket['target']:.2f} odds",
        f"ð {len(picks)} games",
        f"ð Combined MODEL fair odds: {ticket['total_fair_odds']:.2f}",
        "",
        "â ï¸ These are model fair odds, not bookmaker prices.",
        "Connect a bookmaker odds feed if you want live bookmaker odds.",
        "",
        "ð PICKS",
    ]

    for i, p in enumerate(picks, 1):
        lines.extend(
            [
                "",
                f"{i}. {p['home']} vs {p['away']}",
                f"ð¯ {p['market']} @ {p['fair_odds']:.2f}",
                f"ð¢ Confidence: {p['confidence']}%",
                f"ð§  {p['reason']}",
            ]
        )

    lines.extend(
        [
            "",
            "ââââââââââââââââ",
            "ð¡ï¸ TICKET RISK: LOW/MODERATE",
            "ââââââââââââââââ",
            "",
            "â¹ï¸ Statistical model only â no bet is guaranteed.",
            "â¹ï¸ Confidence is model reliability, not a guarantee of winning.",
        ]
    )

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"â½ Welcome to Touchline Tipster AI v{VERSION}!\n\n"
        "ðï¸ SAFE TICKET\n"
        "/safe 10 â build a safe 10-odds model ticket\n"
        "/safe 5 â build a safe 5-odds model ticket\n"
        "/today â show today's scheduled games\n\n"
        "ð ANALYSIS\n"
        "/team Chelsea\n"
        "/fixtures Chelsea\n"
        "/analyze Chelsea vs Arsenal\n"
        "/apitest"
    )


async def safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args).strip()

    try:
        target = float(raw) if raw else 5.0
    except ValueError:
        return await update.message.reply_text(
            "Usage: /safe 10\n\nExample: /safe 10"
        )

    if target < 1.5 or target > 100:
        return await update.message.reply_text(
            "Choose a target between 1.5 and 100 odds."
        )

    await update.message.reply_text(
        f"ð Scanning today's scheduled games for a model ticket near {target:.2f} odds...\n"
        "This may take a little time because recent form is checked for each candidate."
    )

    ticket, error = build_safe_ticket(target)

    if error:
        return await update.message.reply_text(
            f"â SAFE TICKET\n\n{error}"
        )

    await update.message.reply_text(ticket_text(ticket))


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches, error, date = today_fixtures()

    if error:
        return await update.message.reply_text(
            f"â Today's fixture scan failed:\n{error}"
        )

    if not matches:
        return await update.message.reply_text(
            f"ð No scheduled matches returned for {date}."
        )

    lines = [f"ð TODAY'S GAMES â {date}", ""]
    for i, match in enumerate(matches[:30], 1):
        home, away, _, _, _, _ = match_parts(match)
        kickoff = (
            match.get("kickoffAt")
            or match.get("date")
            or match.get("fixture", {}).get("date", "TBC")
        )
        lines.append(f"{i}. {home} vs {away}\n   ð {kickoff}")

    await update.message.reply_text("\n".join(lines))


async def team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        return await update.message.reply_text("Usage: /team Chelsea")

    rows, error = find_team(query)
    if error:
        return await update.message.reply_text(f"â Team search error:\n{error}")

    if not rows:
        return await update.message.reply_text("â No teams found.")

    text = "ð TEAM SEARCH RESULTS\n\n"
    text += "\n".join(
        f"{i}. {name} â ID {ident}"
        for i, (_, name, ident) in enumerate(rows, 1)
    )
    await update.message.reply_text(text)


async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        return await update.message.reply_text("Usage: /fixtures Chelsea")

    rows, error = find_team(query)
    if error:
        return await update.message.reply_text(f"â Search error:\n{error}")

    if not rows:
        return await update.message.reply_text("â No team found.")

    _, name, ident = rows[0]
    data, error = api_get("/matches", {"team": ident, "season": SEASON})

    if error:
        return await update.message.reply_text(f"â Fixture error:\n{error}")

    completed = []
    for match in arr(data):
        home, away, hg, ag, _, _ = match_parts(match)
        if hg is not None and ag is not None:
            completed.append(match)

    completed.sort(
        key=lambda x: str(
            x.get("kickoffAt")
            or x.get("date")
            or x.get("fixture", {}).get("date", "")
        ),
        reverse=True,
    )

    if not completed:
        return await update.message.reply_text(
            f"ð No completed fixtures found for {name}."
        )

    lines = [f"ð {name} â Season {SEASON}", ""]
    for match in completed[:10]:
        home, away, hg, ag, _, _ = match_parts(match)
        date = str(
            match.get("kickoffAt")
            or match.get("date")
            or match.get("fixture", {}).get("date", "")
        )[:10]
        lines.append(f"{date} | {home} {hg}-{ag} {away}")

    await update.message.reply_text("\n".join(lines))


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args).strip()
    lower = raw.lower()

    if " vs " not in lower:
        return await update.message.reply_text(
            "Usage: /analyze Chelsea vs Arsenal"
        )

    index = lower.find(" vs ")
    home_query = raw[:index].strip()
    away_query = raw[index + 4:].strip()

    home_rows, home_error = find_team(home_query)
    away_rows, away_error = find_team(away_query)

    if home_error or away_error:
        return await update.message.reply_text(
            f"â Search error:\n{home_error or away_error}"
        )

    if not home_rows:
        return await update.message.reply_text(
            f"â Could not find: {home_query}"
        )

    if not away_rows:
        return await update.message.reply_text(
            f"â Could not find: {away_query}"
        )

    _, home_name, home_id = home_rows[0]
    _, away_name, away_id = away_rows[0]

    home_matches, he = get_recent_matches(home_id)
    away_matches, ae = get_recent_matches(away_id)

    if he or ae:
        return await update.message.reply_text(
            f"â Fixture/statistics error:\n{he or ae}"
        )

    if not home_matches or not away_matches:
        return await update.message.reply_text(
            "â Not enough completed matches for this analysis."
        )

    h = stats(home_matches, home_id)
    a = stats(away_matches, away_id)
    hv = stats(home_matches, home_id, "home")
    av = stats(away_matches, away_id, "away")

    hx, ax, result, scores, markets = probability_model(h, a, hv, av)
    confidence = model_confidence(h, a, hv, av, markets)

    ranked = sorted(
        markets.items(),
        key=lambda x: x[1],
        reverse=True,
    )[:6]

    lines = [
        f"â½ TOUCHLINE TIPSTER v{VERSION}",
        "",
        f"ðï¸ {home_name} vs {away_name}",
        "",
        f"Season: {SEASON}",
        f"Sample: Last {SAMPLE_SIZE} available matches",
        "",
        f"ð  {home_name} recent:",
        f"{''.join(h['form']) or 'N/A'} | W/D/L {h['w']}/{h['d']}/{h['l']}",
        f"GF {h['gf']} GA {h['ga']} | Avg {avg(h['gf'], h['g']):.2f}/{avg(h['ga'], h['g']):.2f}",
        f"O1.5 {pct(h['o15'], h['g'])}% | O2.5 {pct(h['o25'], h['g'])}% | BTTS {pct(h['btts'], h['g'])}%",
        "",
        f"âï¸ {away_name} recent:",
        f"{''.join(a['form']) or 'N/A'} | W/D/L {a['w']}/{a['d']}/{a['l']}",
        f"GF {a['gf']} GA {a['ga']} | Avg {avg(a['gf'], a['g']):.2f}/{avg(a['ga'], a['g']):.2f}",
        f"O1.5 {pct(a['o15'], a['g'])}% | O2.5 {pct(a['o25'], a['g'])}% | BTTS {pct(a['btts'], a['g'])}%",
        "",
        "ð EXPECTED GOALS MODEL",
        f"{home_name}: {hx:.2f} xG",
        f"{away_name}: {ax:.2f} xG",
        f"Total: {hx + ax:.2f} xG",
        "",
        "ð¯ TOP MODEL MARKETS",
    ]

    for market, probability in ranked:
        lines.append(
            f"â¢ {market}: {probability * 100:.0f}%"
        )

    lines.extend(
        [
            "",
            f"ð§  MODEL CONFIDENCE: {confidence}%",
            "",
            "â¹ï¸ Confidence measures model/data reliability, not a guarantee.",
        ]
    )

    await update.message.reply_text("\n".join(lines))


async def apitest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data, error = api_get("/health")

    if error:
        return await update.message.reply_text(
            f"ð§ OPENFOOT API TEST\n\nâ {error}"
        )

    await update.message.reply_text(
        "ð§ OPENFOOT API TEST\n\n"
        "â Connection works.\n"
        f"Season: {SEASON}\n"
        f"API: {API}\n"
        f"Health response received: {'YES' if data is not None else 'NO'}"
    )


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    if not KEY:
        raise RuntimeError("OPENFOOT_API_KEY is missing.")

    threading.Thread(
        target=lambda: HTTPServer(
            ("0.0.0.0", PORT), HealthHandler
        ).serve_forever(),
        daemon=True,
    ).start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("safe", safe))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("team", team))
    app.add_handler(CommandHandler("fixtures", fixtures))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("apitest", apitest))

    print(f"Touchline Tipster v{VERSION} is running on port {PORT}.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
