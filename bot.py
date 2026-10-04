import os
import math
import asyncio
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

VERSION = "3.6"
API_BASE = "https://openfootapi.com/v1"
SAMPLE_SIZE = 5

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENFOOT_API_KEY = os.getenv("OPENFOOT_API_KEY")
PORT = int(os.getenv("PORT", "10000"))


# ============================================================
# NAME NORMALIZATION
# ============================================================

def normalize_name(value):

    value = str(value or "").strip().lower()

    value = unicodedata.normalize(
        "NFKD",
        value,
    )

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    for ch in ".-_/'’":
        value = value.replace(ch, " ")

    return " ".join(value.split())


# ============================================================
# HEALTH SERVER
# ============================================================

def health_response():

    return (
        f"Touchline Tipster v{VERSION} "
        "is running."
    )


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain",
        )

        self.end_headers()

        self.wfile.write(
            health_response().encode()
        )

    def log_message(self, format, *args):

        return


def start_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler,
    )

    print(
        f"🌐 Health server running on port {PORT}.",
        flush=True,
    )

    server.serve_forever()


threading.Thread(
    target=start_health_server,
    daemon=True,
).start()


# ============================================================
# OPENFOOT API
# ============================================================

def api_get(endpoint, params=None):

    if not OPENFOOT_API_KEY:

        return (
            None,
            "OPENFOOT_API_KEY is missing.",
        )

    try:

        response = requests.get(
            f"{API_BASE}{endpoint}",
            headers={
                "Authorization": (
                    f"Bearer {OPENFOOT_API_KEY}"
                ),
                "Accept": "application/json",
            },
            params=params or {},
            timeout=20,
        )

        if response.status_code != 200:

            return (
                None,
                f"API HTTP {response.status_code}: "
                f"{response.text[:300]}",
            )

        return response.json(), None

    except Exception as e:

        return (
            None,
            f"API request error: {e}",
        )


# ============================================================
# GLOBAL TEAM SEARCH
# ============================================================

def search_team(query):

    variants = []

    raw = str(query or "").strip()

    if raw:

        variants.append(raw)

    normalized = normalize_name(raw)

    if normalized and normalized not in [
        normalize_name(x)
        for x in variants
    ]:

        variants.append(normalized)

    last_error = None

    for variant in variants:

        data, error = api_get(
            "/search",
            {
                "q": variant,
            },
        )

        if error:

            last_error = error

            continue

        results = data.get(
            "data",
            [],
        )

        if isinstance(results, list) and results:

            return results, None

    return [], last_error


def team_name(team):

    if not isinstance(team, dict):

        return str(team)

    return (
        team.get("name")
        or team.get("shortName")
        or team.get("displayName")
        or team.get("title")
        or "Unknown"
    )


def team_id(team):

    if not isinstance(team, dict):

        return None

    return (
        team.get("id")
        or team.get("teamId")
        or team.get("team_id")
    )


def team_country(team):

    if not isinstance(team, dict):

        return ""

    country = team.get("country")

    if isinstance(country, dict):

        return (
            country.get("name")
            or ""
        )

    return str(
        country or ""
    )


def find_team(query):

    teams, error = search_team(
        query
    )

    q = normalize_name(query)

    if teams:

        exact = [
            t
            for t in teams
            if normalize_name(
                team_name(t)
            ) == q
        ]

        if exact:

            return exact[0], None

        partial = [
            t
            for t in teams
            if (
                q in normalize_name(
                    team_name(t)
                )
                or normalize_name(
                    team_name(t)
                ) in q
            )
        ]

        if partial:

            return partial[0], None

        return teams[0], None

    if error:

        return None, error

    return (
        None,
        f"No team found for '{query}'.",
    )


# ============================================================
# MATCH DATA
# ============================================================

def number_value(value):

    if isinstance(value, bool):

        return None

    if isinstance(value, int):

        return value

    if isinstance(value, float):

        return int(value)

    if isinstance(value, str):

        try:

            return int(
                float(
                    value.strip()
                )
            )

        except Exception:

            return None

    return None


def extract_score_side(obj, keys):

    if not isinstance(obj, dict):

        return None

    for key in keys:

        if key in obj:

            value = number_value(
                obj.get(key)
            )

            if value is not None:

                return value

    return None


def extract_scores(match):

    if not isinstance(match, dict):

        return None, None

    home = extract_score_side(
        match,
        [
            "homeScore",
            "home_score",
            "homeGoals",
            "home_goals",
        ],
    )

    away = extract_score_side(
        match,
        [
            "awayScore",
            "away_score",
            "awayGoals",
            "away_goals",
        ],
    )

    if (
        home is not None
        and away is not None
    ):

        return home, away

    for container_name in (
        "score",
        "scores",
    ):

        container = match.get(
            container_name
        )

        if isinstance(
            container,
            dict,
        ):

            home = extract_score_side(
                container,
                [
                    "home",
                    "homeScore",
                    "home_score",
                    "homeGoals",
                ],
            )

            away = extract_score_side(
                container,
                [
                    "away",
                    "awayScore",
                    "away_score",
                    "awayGoals",
                ],
            )

            if (
                home is not None
                and away is not None
            ):

                return home, away

            for side, keys in (
                (
                    "home",
                    [
                        "score",
                        "goals",
                        "value",
                        "current",
                    ],
                ),
                (
                    "away",
                    [
                        "score",
                        "goals",
                        "value",
                        "current",
                    ],
                ),
            ):

                obj = container.get(
                    side
                )

                value = (
                    extract_score_side(
                        obj,
                        keys,
                    )
                    if isinstance(
                        obj,
                        dict,
                    )
                    else None
                )

                if side == "home":

                    if value is not None:

                        home = value

                else:

                    if value is not None:

                        away = value

            if (
                home is not None
                and away is not None
            ):

                return home, away

    return None, None


def match_kickoff(match):

    return (
        match.get("kickoffAt")
        or match.get("kickoff")
        or match.get("date")
        or ""
    )


def match_status(match):

    status = match.get(
        "status"
    )

    if isinstance(
        status,
        dict,
    ):

        return str(
            status.get("type")
            or status.get("name")
            or status.get("short")
            or ""
        ).lower()

    return str(
        status or ""
    ).lower()


def is_completed(match):

    home, away = extract_scores(
        match
    )

    if (
        home is None
        or away is None
    ):

        return False

    return not any(
        word in match_status(match)
        for word in [
            "scheduled",
            "upcoming",
            "pending",
            "postponed",
            "cancelled",
            "canceled",
            "live",
            "inplay",
            "not_started",
        ]
    )


def get_match_home_team(match):

    home = match.get(
        "homeTeam"
    )

    return (
        team_name(home)
        if isinstance(
            home,
            dict,
        )
        else str(
            home or "Unknown"
        )
    )


def get_match_away_team(match):

    away = match.get(
        "awayTeam"
    )

    return (
        team_name(away)
        if isinstance(
            away,
            dict,
        )
        else str(
            away or "Unknown"
        )
    )


# ============================================================
# SEASON DETECTION
# ============================================================

def season_text(value):

    if isinstance(value, dict):

        return str(
            value.get("label")
            or value.get("name")
            or value.get("season")
            or value.get("id")
            or ""
        )

    if value is None:

        return ""

    return str(value)


def match_season(match):

    if not isinstance(
        match,
        dict,
    ):

        return ""

    possible = [
        match.get("season"),
        match.get("seasonLabel"),
        match.get("season_name"),
        match.get("seasonName"),
    ]

    competition = match.get(
        "competition"
    )

    if isinstance(
        competition,
        dict,
    ):

        possible.extend(
            [
                competition.get(
                    "season"
                ),
                competition.get(
                    "seasonLabel"
                ),
                competition.get(
                    "season_name"
                ),
                competition.get(
                    "seasonName"
                ),
            ]
        )

    for value in possible:

        text = season_text(value).strip()

        if text:

            return text

    return ""


def detect_season(matches):

    seasons = [
        match_season(m)
        for m in matches
        if match_season(m)
    ]

    if not seasons:

        return "Current season"

    counts = Counter(
        seasons
    )

    return counts.most_common(1)[0][0]


# ============================================================
# CURRENT SEASON CANDIDATES
# ============================================================

def current_season_candidates():

    year = datetime.now(
        timezone.utc
    ).year

    next_year = year + 1
    previous_year = year - 1

    candidates = [
        f"{year}/{str(next_year)[-2:]}",
        str(year),
        f"{previous_year}/{str(year)[-2:]}",
        str(previous_year),
        f"{year}-{str(next_year)[-2:]}",
        f"{previous_year}-{str(year)[-2:]}",
    ]

    unique = []

    for season in candidates:

        if season not in unique:

            unique.append(season)

    return unique


def season_matches_requested(
    matches,
    requested_season,
):

    labeled = [
        match_season(m)
        for m in matches
        if match_season(m)
    ]

    if not labeled:

        # Some API responses may omit the season
        # even when the season filter was accepted.
        return True

    normalized_requested = normalize_name(
        requested_season
    )

    for label in labeled:

        normalized_label = normalize_name(
            label
        )

        if normalized_label == normalized_requested:

            return True

    return False


# ============================================================
# GLOBAL MATCH RETRIEVAL
# ============================================================

def get_team_matches(
    team_id_value,
):

    if not team_id_value:

        return (
            [],
            "Team ID is missing.",
        )

    candidates = (
        current_season_candidates()
    )

    print(
        f"🔎 Checking current seasons for "
        f"{team_id_value}: "
        f"{', '.join(candidates)}",
        flush=True,
    )

    last_error = None

    for season in candidates:

        print(
            f"🔎 Trying season {season} "
            f"for {team_id_value}...",
            flush=True,
        )

        data, error = api_get(
            "/matches",
            {
                "team": team_id_value,
                "season": season,
            },
        )

        if error:

            print(
                f"⚠️ Season {season} request failed: "
                f"{error}",
                flush=True,
            )

            last_error = error

            continue

        matches = data.get(
            "data",
            [],
        )

        if not isinstance(
            matches,
            list,
        ):

            print(
                f"⚠️ Unexpected response for "
                f"season {season}.",
                flush=True,
            )

            continue

        if not season_matches_requested(
            matches,
            season,
        ):

            print(
                f"⚠️ API returned a different "
                f"season instead of {season}. "
                f"Ignoring that response.",
                flush=True,
            )

            continue

        completed = [
            m
            for m in matches
            if is_completed(m)
        ]

        completed.sort(
            key=match_kickoff,
            reverse=True,
        )

        if completed:

            print(
                f"✅ Found {len(completed)} "
                f"completed matches for "
                f"{team_id_value} in season "
                f"{season}.",
                flush=True,
            )

            return completed, None

        print(
            f"ℹ️ No completed matches found "
            f"for {team_id_value} in "
            f"season {season}.",
            flush=True,
        )

    return (
        [],
        (
            f"No completed current-season "
            f"matches found for this team. "
            f"Checked: "
            f"{', '.join(candidates)}."
        )
        if not last_error
        else
        (
            f"No completed current-season "
            f"matches found. Last API error: "
            f"{last_error}"
        ),
    )


def same_team(a, b):

    return bool(
        a and b
    ) and (
        normalize_name(a)
        == normalize_name(b)
    )


def team_oriented_matches(
    matches,
    target_team_id,
    target_team_name,
):

    result = []

    for match in matches:

        home_obj = match.get(
            "homeTeam"
        )

        away_obj = match.get(
            "awayTeam"
        )

        home_id = (
            team_id(home_obj)
            if isinstance(
                home_obj,
                dict,
            )
            else None
        )

        away_id = (
            team_id(away_obj)
            if isinstance(
                away_obj,
                dict,
            )
            else None
        )

        home_name = (
            get_match_home_team(
                match
            )
        )

        away_name = (
            get_match_away_team(
                match
            )
        )

        belongs = (
            home_id == target_team_id
            or away_id == target_team_id
            or same_team(
                home_name,
                target_team_name,
            )
            or same_team(
                away_name,
                target_team_name,
            )
        )

        if not belongs:

            continue

        hs, aas = extract_scores(
            match
        )

        if (
            hs is None
            or aas is None
        ):

            continue

        is_home = (
            home_id == target_team_id
            or same_team(
                home_name,
                target_team_name,
            )
        )

        result.append(
            {
                "match": match,
                "is_home": is_home,
                "gf": (
                    hs
                    if is_home
                    else aas
                ),
                "ga": (
                    aas
                    if is_home
                    else hs
                ),
                "total": hs + aas,
                "date": match_kickoff(
                    match
                ),
                "opponent": (
                    away_name
                    if is_home
                    else home_name
                ),
                "season": match_season(
                    match
                ),
            }
        )

    result.sort(
        key=lambda x: x["date"],
        reverse=True,
    )

    return result


# ============================================================
# STATISTICS
# ============================================================

def recency_weights(count):

    if count <= 0:

        return []

    if count == 1:

        return [1.0]

    step = 0.40 / (
        count - 1
    )

    return [
        0.60 + step * i
        for i in range(count)
    ]


def weighted_average(values):

    if not values:

        return 0.0

    weights = recency_weights(
        len(values)
    )

    return sum(
        v * w
        for v, w in zip(
            values,
            weights,
        )
    ) / sum(weights)


def weighted_rate(values):

    return weighted_average(
        [
            1.0 if v else 0.0
            for v in values
        ]
    )


def calculate_stats(matches):

    if not matches:

        return None

    wins = []
    draws = []
    losses = []

    gf = []
    ga = []

    o15 = []
    o25 = []
    u35 = []

    btts = []
    scoring = []
    cs = []

    for item in matches:

        f = item["gf"]
        g = item["ga"]

        wins.append(
            f > g
        )

        draws.append(
            f == g
        )

        losses.append(
            f < g
        )

        gf.append(f)
        ga.append(g)

        total = f + g

        o15.append(
            total >= 2
        )

        o25.append(
            total >= 3
        )

        u35.append(
            total <= 3
        )

        btts.append(
            f > 0
            and g > 0
        )

        scoring.append(
            f > 0
        )

        cs.append(
            g == 0
        )

    return {
        "count": len(matches),
        "wins": sum(wins),
        "draws": sum(draws),
        "losses": sum(losses),
        "gf": sum(gf),
        "ga": sum(ga),
        "avg_gf": weighted_average(gf),
        "avg_ga": weighted_average(ga),
        "over15": (
            weighted_rate(o15)
            * 100
        ),
        "over25": (
            weighted_rate(o25)
            * 100
        ),
        "under35": (
            weighted_rate(u35)
            * 100
        ),
        "btts": (
            weighted_rate(btts)
            * 100
        ),
        "scoring": (
            weighted_rate(scoring)
            * 100
        ),
        "clean_sheet": (
            weighted_rate(cs)
            * 100
        ),
    }


def form_string(matches):

    return "".join(
        "W"
        if x["gf"] > x["ga"]
        else "D"
        if x["gf"] == x["ga"]
        else "L"
        for x in matches
    )


def get_home_matches(matches):

    return [
        x
        for x in matches
        if x["is_home"]
    ]


def get_away_matches(matches):

    return [
        x
        for x in matches
        if not x["is_home"]
    ]


def blend(a, b, weight_a):

    return (
        a * weight_a
        + b * (
            1.0 - weight_a
        )
    )


def estimate_xg(
    team_stats,
    opponent_stats,
    venue_team,
    venue_opponent,
):

    base = (
        team_stats["avg_gf"]
        * 0.55
        + opponent_stats["avg_ga"]
        * 0.45
    )

    venue = (
        venue_team["avg_gf"]
        * 0.55
        + venue_opponent["avg_ga"]
        * 0.45
    )

    n = venue_team["count"]

    vw = (
        0.40 if n >= 5
        else 0.30 if n == 4
        else 0.24 if n == 3
        else 0.18 if n == 2
        else 0.10 if n == 1
        else 0.0
    )

    xg = blend(
        base,
        venue,
        vw,
    )

    return max(
        0.15,
        min(
            xg * 0.88
            + 1.35 * 0.12,
            4.0,
        ),
    )


# ============================================================
# POISSON MODEL
# ============================================================

def poisson_probability(
    lmbda,
    k,
):

    try:

        return (
            math.exp(-lmbda)
            * (
                lmbda ** k
            )
            / math.factorial(k)
        )

    except Exception:

        return 0.0


def poisson_matrix(
    home_xg,
    away_xg,
    max_goals=8,
):

    matrix = []
    total = 0.0

    for h in range(
        max_goals + 1
    ):

        row = []

        for a in range(
            max_goals + 1
        ):

            p = (
                poisson_probability(
                    home_xg,
                    h,
                )
                * poisson_probability(
                    away_xg,
                    a,
                )
            )

            row.append(p)

            total += p

        matrix.append(row)

    if total > 0:

        matrix = [
            [
                p / total
                for p in row
            ]
            for row in matrix
        ]

    return matrix


def matrix_markets(matrix):

    hw = 0.0
    d = 0.0
    aw = 0.0

    o05 = 0.0
    o15 = 0.0
    o25 = 0.0

    u35 = 0.0
    u45 = 0.0

    bt = 0.0

    hs = 0.0
    as_ = 0.0

    for h, row in enumerate(
        matrix
    ):

        for a, p in enumerate(
            row
        ):

            t = h + a

            if h > a:

                hw += p

            elif h == a:

                d += p

            else:

                aw += p

            if t >= 1:

                o05 += p

            if t >= 2:

                o15 += p

            if t >= 3:

                o25 += p

            if t <= 3:

                u35 += p

            if t <= 4:

                u45 += p

            if h > 0 and a > 0:

                bt += p

            if h > 0:

                hs += p

            if a > 0:

                as_ += p

    return {
        "home_win": hw * 100,
        "draw": d * 100,
        "away_win": aw * 100,
        "1x": (
            hw + d
        ) * 100,
        "x2": (
            d + aw
        ) * 100,
        "over05": o05 * 100,
        "over15": o15 * 100,
        "over25": o25 * 100,
        "under35": u35 * 100,
        "under45": u45 * 100,
        "btts": bt * 100,
        "home_score": hs * 100,
        "away_score": as_ * 100,
    }


def top_scorelines(
    matrix,
    limit=3,
):

    scores = [
        (
            matrix[h][a] * 100,
            h,
            a,
        )
        for h in range(
            len(matrix)
        )
        for a in range(
            len(matrix[h])
        )
    ]

    scores.sort(
        reverse=True
    )

    return scores[:limit]


def grouped_goal_distribution(
    matrix,
):

    d = {
        "zero_one": 0.0,
        "two_three": 0.0,
        "four": 0.0,
        "five_plus": 0.0,
    }

    for h, row in enumerate(
        matrix
    ):

        for a, p in enumerate(
            row
        ):

            t = h + a

            if t <= 1:

                d["zero_one"] += p

            elif t <= 3:

                d["two_three"] += p

            elif t == 4:

                d["four"] += p

            else:

                d["five_plus"] += p

    return {
        k: v * 100
        for k, v in d.items()
    }


def calculate_data_strength(
    hr,
    ar,
    hv,
    av,
):

    recent = (
        min(hr, SAMPLE_SIZE)
        + min(ar, SAMPLE_SIZE)
    ) / (
        SAMPLE_SIZE * 2
    )

    venue = (
        min(hv, 5)
        + min(av, 5)
    ) / 10

    return round(
        (
            recent * 0.65
            + venue * 0.35
        ) * 100
    )


def calculate_disagreement(
    hr,
    ar,
    hv,
    av,
):

    diffs = []

    for r, v in (
        (hr, hv),
        (ar, av),
    ):

        diffs.append(
            abs(
                (
                    r["wins"]
                    / r["count"]
                    if r["count"]
                    else 0
                )
                - (
                    v["wins"]
                    / v["count"]
                    if v["count"]
                    else 0
                )
            )
        )

        diffs.append(
            abs(
                r["scoring"]
                - v["scoring"]
            ) / 100
        )

    m = max(diffs)

    return (
        "HIGH"
        if m >= 0.60
        else "MEDIUM"
        if m >= 0.40
        else "LOW"
    )


def calculate_model_confidence(
    data_strength,
    disagreement,
):

    c = (
        50
        + data_strength * 0.32
    )

    if disagreement == "MEDIUM":

        c -= 5

    elif disagreement == "HIGH":

        c -= 10

    return round(
        max(
            50,
            min(c, 82),
        )
    )


def probability_audit(m):

    return (
        abs(
            m["home_win"]
            + m["draw"]
            + m["away_win"]
            - 100
        ) <= 0.2
        and m["over05"]
        >= m["over15"]
        >= m["over25"]
        and m["under35"]
        <= m["under45"]
        and m["btts"]
        <= (
            m["home_score"]
            + 0.2
        )
        and m["btts"]
        <= (
            m["away_score"]
            + 0.2
        )
    )


def xg_sanity_check(
    total_xg,
    over05,
):

    return abs(
        (
            1
            - math.exp(
                -total_xg
            )
        ) * 100
        - over05
    ) <= 1.0


def pct(v):

    return f"{round(v):.0f}%"


# ============================================================
# FORMATTING
# ============================================================

def format_stats(
    title,
    s,
    matches,
):

    return (
        f"{title}:\n"
        f"{form_string(matches)} | "
        f"W/D/L "
        f"{s['wins']}/"
        f"{s['draws']}/"
        f"{s['losses']} | "
        f"GF{s['gf']} GA{s['ga']}\n"
        f"Avg "
        f"{s['avg_gf']:.2f}/"
        f"{s['avg_ga']:.2f} | "
        f"O1.5 "
        f"{pct(s['over15'])} | "
        f"O2.5 "
        f"{pct(s['over25'])} | "
        f"U3.5 "
        f"{pct(s['under35'])} | "
        f"BTTS "
        f"{pct(s['btts'])}\n"
        f"Scoring "
        f"{pct(s['scoring'])} | "
        f"CS "
        f"{pct(s['clean_sheet'])}"
    )


def format_venue(
    title,
    s,
):

    return (
        f"{title}:\n"
        f"{s['count']} games | "
        f"W/D/L "
        f"{s['wins']}/"
        f"{s['draws']}/"
        f"{s['losses']} | "
        f"Avg "
        f"{s['avg_gf']:.2f}/"
        f"{s['avg_ga']:.2f}\n"
        f"Scoring "
        f"{pct(s['scoring'])} | "
        f"CS "
        f"{pct(s['clean_sheet'])}"
    )


# ============================================================
# MATCH ANALYSIS
# ============================================================

def analyze_match(
    home_team,
    away_team,
    home_matches,
    away_matches,
):

    hr = home_matches[
        :SAMPLE_SIZE
    ]

    ar = away_matches[
        :SAMPLE_SIZE
    ]

    hs = calculate_stats(hr)
    as_ = calculate_stats(ar)

    if not hs or not as_:

        return (
            None,
            "Not enough completed data.",
        )

    hvm = get_home_matches(
        hr
    )

    avm = get_away_matches(
        ar
    )

    hv = calculate_stats(
        hvm
    )

    av = calculate_stats(
        avm
    )

    if not hv:

        hv = {
            "count": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "gf": 0,
            "ga": 0,
            "avg_gf": hs["avg_gf"],
            "avg_ga": hs["avg_ga"],
            "scoring": hs["scoring"],
            "clean_sheet": hs[
                "clean_sheet"
            ],
        }

    if not av:

        av = {
            "count": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "gf": 0,
            "ga": 0,
            "avg_gf": as_["avg_gf"],
            "avg_ga": as_["avg_ga"],
            "scoring": as_["scoring"],
            "clean_sheet": as_[
                "clean_sheet"
            ],
        }

    hx = estimate_xg(
        hs,
        as_,
        hv,
        av,
    )

    ax = estimate_xg(
        as_,
        hs,
        av,
        hv,
    )

    tx = hx + ax

    matrix = poisson_matrix(
        hx,
        ax,
    )

    m = matrix_markets(
        matrix
    )

    scores = top_scorelines(
        matrix
    )

    goals = grouped_goal_distribution(
        matrix
    )

    strength = calculate_data_strength(
        len(hr),
        len(ar),
        len(hvm),
        len(avm),
    )

    disagreement = calculate_disagreement(
        hs,
        as_,
        hv,
        av,
    )

    confidence = calculate_model_confidence(
        strength,
        disagreement,
    )

    consistency = probability_audit(
        m
    )

    xcheck = xg_sanity_check(
        tx,
        m["over05"],
    )

    signals = [
        (
            "Over 0.5",
            m["over05"],
        ),
        (
            "Over 1.5",
            m["over15"],
        ),
        (
            "Over 2.5",
            m["over25"],
        ),
        (
            "Under 3.5",
            m["under35"],
        ),
        (
            "Under 4.5",
            m["under45"],
        ),
        (
            "BTTS",
            m["btts"],
        ),
        (
            "Team 1 to score",
            m["home_score"],
        ),
        (
            "Team 2 to score",
            m["away_score"],
        ),
        (
            "1X",
            m["1x"],
        ),
        (
            "X2",
            m["x2"],
        ),
    ]

    signals.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    season_home = detect_season(
        hr
    )

    season_away = detect_season(
        ar
    )

    if (
        season_home == season_away
        and season_home != "Current season"
    ):

        display_season = season_home

    elif season_home != "Current season":

        display_season = (
            f"{season_home} / "
            f"{season_away}"
        )

    else:

        display_season = (
            "Current season"
        )

    lines = [
        f"⚽ TOUCHLINE TIPSTER v{VERSION}",
        "",
        f"🏟️ "
        f"{team_name(home_team)} "
        f"vs "
        f"{team_name(away_team)}",
        "",
        f"Season: {display_season}",
        f"Sample: Last "
        f"{SAMPLE_SIZE} "
        f"available matches",
        "",
        format_stats(
            f"🏠 "
            f"{team_name(home_team)} recent",
            hs,
            hr,
        ),
        "",
        format_stats(
            f"✈️ "
            f"{team_name(away_team)} recent",
            as_,
            ar,
        ),
        "",
        format_venue(
            f"🏟️ "
            f"{team_name(home_team)} HOME",
            hv,
        ),
        "",
        format_venue(
            f"✈️ "
            f"{team_name(away_team)} AWAY",
            av,
        ),
        "",
        "📊 EXPECTED GOALS MODEL",
        f"{team_name(home_team)}: "
        f"{hx:.2f} xG",
        f"{team_name(away_team)}: "
        f"{ax:.2f} xG",
        f"Total: {tx:.2f} xG",
        "",
        "🎯 RESULT MODEL",
        f"1: {pct(m['home_win'])} | "
        f"X: {pct(m['draw'])} | "
        f"2: {pct(m['away_win'])}",
        "",
        "📈 MARKET MODEL",
        f"1X: {pct(m['1x'])}",
        f"X2: {pct(m['x2'])}",
        f"Over 0.5: {pct(m['over05'])}",
        f"Over 1.5: {pct(m['over15'])}",
        f"Over 2.5: {pct(m['over25'])}",
        f"Under 3.5: {pct(m['under35'])}",
        f"Under 4.5: {pct(m['under45'])}",
        f"BTTS: {pct(m['btts'])}",
        f"Team 1 to score: "
        f"{pct(m['home_score'])}",
        f"Team 2 to score: "
        f"{pct(m['away_score'])}",
        "",
        "🥅 TOP SCORELINES",
    ]

    lines += [
        f"{h}-{a}: {pct(p)}"
        for p, h, a in scores
    ]

    lines += [
        "",
        "⚽ GOAL DISTRIBUTION",
        f"0-1: "
        f"{pct(goals['zero_one'])} | "
        f"2-3: "
        f"{pct(goals['two_three'])} | "
        f"4: "
        f"{pct(goals['four'])} | "
        f"5+: "
        f"{pct(goals['five_plus'])}",
        "0-1 / 2-3 / 4 / 5+ goals",
        "",
        "🔥 TOP MODEL SIGNALS",
    ]

    lines += [
        f"{'🟢' if p >= 65 else '🟡'} "
        f"{n} {pct(p)}"
        for n, p in signals[:3]
    ]

    lines += [
        "",
        "🧠 MODEL QUALITY",
        f"Data strength: {strength}%",
        "Probability consistency: "
        + (
            "✅ PASS"
            if consistency
            else "⚠️ CHECK"
        ),
        "xG/market sanity check: "
        + (
            "✅ PASS"
            if xcheck
            else "⚠️ CHECK"
        ),
        f"Model disagreement: "
        f"{disagreement}",
        f"Model confidence: "
        f"{'🟢 HIGH' if confidence >= 75 else '🟡 MODERATE' if confidence >= 65 else '🔴 LOW'} "
        f"{confidence}%",
        "",
        f"📌 PRIMARY MODEL SIGNAL: "
        f"{signals[0][0]} "
        f"{pct(signals[0][1])}",
        "",
        "ℹ️ Probabilities are mathematical "
        "model estimates from recent data, "
        "venue evidence and a Poisson score "
        "model. They are not guarantees.",
        "ℹ️ Model confidence measures "
        "data/model reliability; it is not "
        "the probability that a prediction "
        "will be correct.",
    ]

    return (
        "\n".join(lines),
        None,
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "📩 Received /start",
        flush=True,
    )

    if update.message:

        await update.message.reply_text(
            f"⚽ Welcome to Touchline Tipster "
            f"v{VERSION}!\n\n"
            "I analyze football matches across "
            "OpenFootAPI's supported global "
            "competitions using recent form, "
            "home/away evidence, weighted "
            "statistics, expected goals and a "
            "Poisson score model.\n\n"
            "Commands:\n"
            "/team Chelsea\n"
            "/fixtures Chelsea\n"
            "/analyze Chelsea vs Arsenal\n"
            "/apitest"
        )


async def apitest_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "📩 Received /apitest",
        flush=True,
    )

    teams, error = search_team(
        "Chelsea"
    )

    if error:

        await update.message.reply_text(
            "🔧 FOOTBALL API TEST\n\n❌ "
            + error
        )

        return

    if not teams:

        await update.message.reply_text(
            "🔧 FOOTBALL API TEST\n\n"
            "❌ Chelsea was not found."
        )

        return

    t = teams[0]

    await update.message.reply_text(
        f"🔧 FOOTBALL API TEST\n\n"
        f"OpenFoot API connection works.\n"
        f"Found {team_name(t)}.\n"
        f"Team ID: {team_id(t)}"
    )


async def team_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "📩 Received /team",
        flush=True,
    )

    q = " ".join(
        context.args
    ).strip()

    if not q:

        await update.message.reply_text(
            "Usage: /team Chelsea"
        )

        return

    t, error = find_team(q)

    if error:

        await update.message.reply_text(
            "❌ " + error
        )

        return

    country = team_country(
        t
    )

    msg = (
        f"🔎 TEAM SEARCH\n\n"
        f"Name: {team_name(t)}\n"
        f"ID: {team_id(t)}"
    )

    if country:

        msg += (
            f"\nCountry: {country}"
        )

    await update.message.reply_text(
        msg
    )


async def fixtures_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "📩 Received /fixtures",
        flush=True,
    )

    q = " ".join(
        context.args
    ).strip()

    if not q:

        await update.message.reply_text(
            "Usage: /fixtures Chelsea"
        )

        return

    t, error = find_team(q)

    if error:

        await update.message.reply_text(
            "❌ " + error
        )

        return

    matches, error = get_team_matches(
        team_id(t)
    )

    if error:

        await update.message.reply_text(
            "❌ " + error
        )

        return

    oriented = team_oriented_matches(
        matches,
        team_id(t),
        team_name(t),
    )

    recent = oriented[
        :SAMPLE_SIZE
    ]

    if not recent:

        await update.message.reply_text(
            f"❌ No completed current "
            f"season matches found "
            f"for {team_name(t)}."
        )

        return

    display_season = detect_season(
        [
            x["match"]
            for x in recent
        ]
    )

    lines = [
        f"📅 {team_name(t)}",
        "",
        f"Season: {display_season}",
        "",
        f"Showing last "
        f"{len(recent)} "
        f"completed matches",
    ]

    for x in recent:

        r = (
            "W"
            if x["gf"] > x["ga"]
            else "D"
            if x["gf"] == x["ga"]
            else "L"
        )

        lines.append(
            f"{x['date'][:10]} | "
            f"{'HOME' if x['is_home'] else 'AWAY'} | "
            f"{r} | "
            f"{x['gf']}-{x['ga']} vs "
            f"{x['opponent']}"
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


async def analyze_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "📩 Received /analyze",
        flush=True,
    )

    text = " ".join(
        context.args
    ).strip()

    if " vs " not in text.lower():

        await update.message.reply_text(
            "Usage:\n"
            "/analyze Chelsea vs Arsenal"
        )

        return

    home_q, away_q = text.lower().split(
        " vs ",
        1,
    )

    home_q = home_q.strip()
    away_q = away_q.strip()

    if (
        not home_q
        or not away_q
    ):

        await update.message.reply_text(
            "Usage:\n"
            "/analyze Chelsea vs Arsenal"
        )

        return

    ht, error = find_team(
        home_q
    )

    if error:

        await update.message.reply_text(
            f"❌ Home team error: "
            f"{error}"
        )

        return

    at, error = find_team(
        away_q
    )

    if error:

        await update.message.reply_text(
            f"❌ Away team error: "
            f"{error}"
        )

        return

    hm, error = get_team_matches(
        team_id(ht)
    )

    if error:

        await update.message.reply_text(
            f"❌ {team_name(ht)}: "
            f"{error}"
        )

        return

    am, error = get_team_matches(
        team_id(at)
    )

    if error:

        await update.message.reply_text(
            f"❌ {team_name(at)}: "
            f"{error}"
        )

        return

    hm = team_oriented_matches(
        hm,
        team_id(ht),
        team_name(ht),
    )

    am = team_oriented_matches(
        am,
        team_id(at),
        team_name(at),
    )

    if not hm:

        await update.message.reply_text(
            f"❌ No recent current "
            f"season data found "
            f"for {team_name(ht)}."
        )

        return

    if not am:

        await update.message.reply_text(
            f"❌ No recent current "
            f"season data found "
            f"for {team_name(at)}."
        )

        return

    result, error = analyze_match(
        ht,
        at,
        hm,
        am,
    )

    if error:

        await update.message.reply_text(
            "❌ Analysis error: "
            + error
        )

        return

    await update.message.reply_text(
        result
    )


# ============================================================
# TELEGRAM ERROR LOGGING
# ============================================================

async def error_handler(
    update,
    context,
):

    print(
        "❌ Telegram handler error:",
        flush=True,
    )

    print(
        repr(context.error),
        flush=True,
    )


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def start_telegram():

    print(
        "🔵 Creating Telegram application...",
        flush=True,
    )

    application = (
        Application.builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

    print(
        "🔵 Telegram application created.",
        flush=True,
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "apitest",
            apitest_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "team",
            team_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "fixtures",
            fixtures_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "analyze",
            analyze_command,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "🔵 Telegram handlers registered.",
        flush=True,
    )

    print(
        "🔵 Initializing Telegram application...",
        flush=True,
    )

    await application.initialize()

    print(
        "✅ Telegram application initialized.",
        flush=True,
    )

    print(
        "🔵 Checking Telegram bot connection...",
        flush=True,
    )

    bot_info = await application.bot.get_me()

    print(
        f"✅ Telegram connection successful: "
        f"@{bot_info.username}",
        flush=True,
    )

    print(
        "🔵 Clearing Telegram webhook...",
        flush=True,
    )

    await application.bot.delete_webhook(
        drop_pending_updates=True
    )

    print(
        "✅ Telegram webhook cleared.",
        flush=True,
    )

    print(
        "🔵 Starting Telegram application...",
        flush=True,
    )

    await application.start()

    print(
        "✅ Telegram application started.",
        flush=True,
    )

    print(
        "🔵 Starting Telegram polling updater...",
        flush=True,
    )

    await application.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

    print(
        "🟢 Telegram polling started successfully.",
        flush=True,
    )

    print(
        "🟢 Touchline Tipster is now listening for commands.",
        flush=True,
    )

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    finally:

        print(
            "🟡 Stopping Telegram polling...",
            flush=True,
        )

        await application.updater.stop()

        await application.stop()

        await application.shutdown()


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        f"⚽ Touchline Tipster v{VERSION} "
        "Telegram bot is starting.",
        flush=True,
    )

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "❌ TELEGRAM_BOT_TOKEN is missing."
        )

    if not OPENFOOT_API_KEY:

        print(
            "⚠️ WARNING: "
            "OPENFOOT_API_KEY is missing.",
            flush=True,
        )

    print(
        "🔵 Starting Telegram event loop...",
        flush=True,
    )

    asyncio.run(
        start_telegram()
    )


if __name__ == "__main__":

    main()
