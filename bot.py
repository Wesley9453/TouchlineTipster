import os
import math
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

VERSION = "3.8"
API_BASE = "https://openfootapi.com/v1"
SEASON = os.getenv("OPENFOOT_SEASON", "2026/27")
SAMPLE_SIZE = 5
MAX_CONFIDENCE = 72
MIN_CONFIDENCE = 58
TIMEOUT = 20
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_KEY = os.getenv("OPENFOOT_API_KEY", "")

ALIASES = {
    "man utd": "Manchester United", "man united": "Manchester United",
    "man city": "Manchester City", "spurs": "Tottenham Hotspur",
    "tottenham": "Tottenham Hotspur", "wolves": "Wolverhampton Wanderers",
    "west ham": "West Ham United", "brighton": "Brighton & Hove Albion",
    "palace": "Crystal Palace", "villa": "Aston Villa", "atletico": "Atletico Madrid",
    "inter": "Inter Milan", "psg": "Paris Saint-Germain", "psv": "PSV Eindhoven",
    "barca": "Barcelona", "real": "Real Madrid", "bayern": "Bayern Munich",
    "dortmund": "Borussia Dortmund", "fener": "Fenerbahce",
}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"Touchline Tipster v{VERSION} is running.".encode())
    def log_message(self, *_):
        pass

def health_server():
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
    
def norm(s):
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    for c in "-_.,'â&/()": s = s.replace(c, " ")
    return " ".join(s.split())

def headers():
    h = {"Accept": "application/json"}
    if API_KEY: h["Authorization"] = f"Bearer {API_KEY}"
    return h

def api_get(path, params=None):
    r = requests.get(API_BASE + path, params=params or {}, headers=headers(), timeout=TIMEOUT)
    try: data = r.json()
    except ValueError: data = {}
    if not r.ok:
        raise RuntimeError(str(data.get("message") or data.get("error") or f"HTTP {r.status_code}"))
    return data.get("data", data), data.get("meta", {}) if isinstance(data, dict) else {}

def name_id(team):
    return (str(team.get("name") or team.get("displayName") or team.get("shortName") or ""),
            str(team.get("id") or team.get("teamId") or ""))

def score_name(q, n):
    q, n = norm(q), norm(n)
    if q == n: return 100
    if norm(ALIASES.get(q, q)) == n: return 98
    if q in n or n in q: return 90
    a, b = set(q.split()), set(n.split())
    return min(88, round(60 + 28 * len(a & b) / max(1, len(a))))

def search_teams(query):
    terms = [query]
    if ALIASES.get(norm(query)): terms.append(ALIASES[norm(query)])
    found = {}
    for term in terms:
        try: data, _ = api_get("/search", {"q": term})
        except Exception: continue
        rows = data if isinstance(data, list) else data.get("teams", data.get("data", [data]) if isinstance(data, dict) else [])
        if isinstance(rows, dict): rows = [rows]
        for item in rows or []:
            t = item.get("team", item) if isinstance(item, dict) else {}
            if not isinstance(t, dict): continue
            name, tid = name_id(t)
            if name and tid:
                s = score_name(query, name)
                if tid not in found or s > found[tid]["score"]:
                    found[tid] = {"id": tid, "name": name, "score": s}
    return sorted(found.values(), key=lambda x: (-x["score"], x["name"]))

async def resolve(query, update):
    teams = search_teams(query)
    if not teams:
        await update.message.reply_text(f"â Team not found: {query}")
        return None
    if teams[0]["score"] >= 88: return teams[0]
    await update.message.reply_text("ð Multiple/unclear team match. Try a fuller club name:\n" + "\n".join(f"â¢ {x['name']}" for x in teams[:6]))
    return None

def score_of(m):
    s = m.get("score", {})
    if isinstance(s, dict):
        h, a = s.get("home"), s.get("away")
        if isinstance(h, dict): h = h.get("goals", h.get("score"))
        if isinstance(a, dict): a = a.get("goals", a.get("score"))
        h = s.get("homeGoals", h); a = s.get("awayGoals", a)
    else:
        h, a = m.get("homeScore", m.get("homeGoals")), m.get("awayScore", m.get("awayGoals"))
    try: return int(h), int(a)
    except (TypeError, ValueError): return None

def team_obj(m, side):
    x = m.get("homeTeam" if side == "home" else "awayTeam", {})
    if not isinstance(x, dict): return {"id":"", "name":str(x)}
    return {"id":x.get("id") or x.get("teamId") or "", "name":x.get("name") or x.get("displayName") or x.get("shortName") or ""}

def matches(team_id):
    data, _ = api_get("/matches", {"team": team_id, "season": SEASON, "status":"finished"})
    rows = []
    for m in data if isinstance(data, list) else []:
        s = score_of(m)
        if s: rows.append(m)
    rows.sort(key=lambda x: str(x.get("kickoffAt") or x.get("date") or ""), reverse=True)
    return rows[:SAMPLE_SIZE]

def stats(ms, tid, venue=None):
    rows=[]
    for m in ms:
        s=score_of(m)
        if not s: continue
        h,a=s; home=team_obj(m,"home"); away=team_obj(m,"away")
        ih=str(home["id"])==str(tid)
        v="home" if ih else "away"
        if venue and v != venue: continue
        gf,ga=(h,a) if ih else (a,h)
        rows.append((gf,ga))
    n=len(rows)
    if not n: return dict(n=0,w=0,d=0,l=0,gf=0,ga=0,agf=0,aga=0,o15=0,o25=0,u45=0,btts=0,score=0,cs=0,form="")
    wins=sum(g>c for g,c in rows); draws=sum(g==c for g,c in rows)
    return dict(n=n,w=wins,d=draws,l=n-wins-draws,gf=sum(g for g,c in rows),ga=sum(c for g,c in rows),
                agf=sum(g for g,c in rows)/n,aga=sum(c for g,c in rows)/n,
                o15=100*sum(g+c>1.5 for g,c in rows)/n,o25=100*sum(g+c>2.5 for g,c in rows)/n,
                u45=100*sum(g+c<4.5 for g,c in rows)/n,btts=100*sum(g>0 and c>0 for g,c in rows)/n,
                score=100*sum(g>0 for g,c in rows)/n,cs=100*sum(c==0 for g,c in rows)/n,
                form="".join("W" if g>c else "D" if g==c else "L" for g,c in rows))

def pois(lam,k): return math.exp(-lam)*lam**k/math.factorial(k)

def model(h,a):
    hl=max(.15,h["agf"]*.6+a["aga"]*.4); al=max(.15,a["agf"]*.6+h["aga"]*.4)
    mat=[(x,y,pois(hl,x)*pois(al,y)) for x in range(7) for y in range(7)]
    p=lambda f: sum(v for x,y,v in mat if f(x,y))
    return {"mat":mat,"home":p(lambda x,y:x>y),"draw":p(lambda x,y:x==y),"away":p(lambda x,y:x<y),
            "o15":p(lambda x,y:x+y>1),"o25":p(lambda x,y:x+y>2),"u45":p(lambda x,y:x+y<4.5),
            "btts":p(lambda x,y:x>0 and y>0),"hs":p(lambda x,y:x>0),"as":p(lambda x,y:y>0),
            "1x":p(lambda x,y:x>=y),"x2":p(lambda x,y:y>=x),"12":p(lambda x,y:x!=y),
            "hdnb":p(lambda x,y:x>y)/(max(p(lambda x,y:x>y)+p(lambda x,y:x<y),.0001)),
            "adnb":p(lambda x,y:x<y)/(max(p(lambda x,y:x>y)+p(lambda x,y:x<y),.0001)),
            "hp1":p(lambda x,y:x+1>y),"ap1":p(lambda x,y:y+1>x),"hm1":p(lambda x,y:x-1>y),"am1":p(lambda x,y:y-1>x)}

def conservative_confidence(probability, sample_sizes):
    """Convert model probability into deliberately conservative confidence.

    v3.8 avoids treating raw probabilities as confidence. A 90% raw model
    probability does not become 90% confidence; the transformation also
    accounts for the small-sample problem and keeps 72% as an absolute cap.
    """
    p = max(0.0, min(1.0, probability))
    base = 50.0 + ((p - 0.50) * 55.0)

    # Five-match samples are still small. Reduce confidence when fewer
    # completed matches are available on either side.
    n = min(sample_sizes) if sample_sizes else SAMPLE_SIZE
    reliability_penalty = max(0.0, 5.0 - float(n)) * 2.0

    return max(0, min(MAX_CONFIDENCE, round(base - reliability_penalty)))

def markets(h,a,m):
    def blend(x,*e): return .65*x+.35*(sum(e)/len(e))
    raw=[("Home Win",m["home"]),("Draw",m["draw"]),("Away Win",m["away"]),
         ("1X",blend(m["1x"],(h["w"]+h["d"])/max(1,h["n"]))),
         ("X2",blend(m["x2"],(a["w"]+a["d"])/max(1,a["n"]))),("12",m["12"]),
         ("Over 1.5 Goals",blend(m["o15"],h["o15"]/100,a["o15"]/100)),
         ("Over 2.5 Goals",blend(m["o25"],h["o25"]/100,a["o25"]/100)),
         ("Under 4.5 Goals",blend(m["u45"],h["u45"]/100,a["u45"]/100)),
         ("BTTS",blend(m["btts"],h["btts"]/100,a["btts"]/100)),
         ("Home Team to Score",blend(m["hs"],h["score"]/100,1-a["cs"]/100)),
         ("Away Team to Score",blend(m["as"],a["score"]/100,1-h["cs"]/100)),
         ("Home DNB",m["hdnb"]),("Away DNB",m["adnb"]),
         ("Home +1 Handicap",m["hp1"]),("Away +1 Handicap",m["ap1"]),
         ("Home -1 Handicap",m["hm1"]),("Away -1 Handicap",m["am1"])]
    return sorted(
        (
            {
                "name": n,
                "confidence": conservative_confidence(v, (h["n"], a["n"])),
                "prob": round(v * 100),
            }
            for n, v in raw
        ),
        key=lambda z: (-z["confidence"], -z["prob"]),
    )

def analysis(home,away,hs,as_,m):
    ms=markets(hs,as_,m); qualifying=[x for x in ms if x["confidence"]>=MIN_CONFIDENCE]
    best=qualifying[0] if qualifying else None
    safety_priority={"Under 4.5 Goals":5,"1X":4,"X2":4,"Home +1 Handicap":4,"Away +1 Handicap":4,"Home DNB":3,"Away DNB":3}
    safe=max(qualifying,key=lambda x:(x["confidence"],safety_priority.get(x["name"],0))) if qualifying else None
    scores=sorted([(x,y,p*100) for x,y,p in m["mat"]],key=lambda z:-z[2])[:3]
    lines=[f"â½ TOUCHLINE TIPSTER v{VERSION}",f"ðï¸ {home['name']} vs {away['name']}",f"Season: {SEASON}",f"Sample: Last {SAMPLE_SIZE} available matches","",
           f"ð  {home['name']}: {hs['form']} | W/D/L {hs['w']}/{hs['d']}/{hs['l']} | GF {hs['gf']} GA {hs['ga']}",
           f"Avg goals {hs['agf']:.2f} | Avg conceded {hs['aga']:.2f} | O2.5 {hs['o25']:.0f}% | BTTS {hs['btts']:.0f}%",
           f"âï¸ {away['name']}: {as_['form']} | W/D/L {as_['w']}/{as_['d']}/{as_['l']} | GF {as_['gf']} GA {as_['ga']}",
           f"Avg goals {as_['agf']:.2f} | Avg conceded {as_['aga']:.2f} | O2.5 {as_['o25']:.0f}% | BTTS {as_['btts']:.0f}%","","ð¤ TOP MODEL MARKETS"]
    lines += [f"â¢ {x['name']}: {x['confidence']}%" for x in ms[:8]]
    lines += ["","ð¥ TOP 3 CORRECT SCORES"] + [f"â¢ {x}-{y}: {p:.1f}%" for x,y,p in scores]
    lines += ["",f"ð¥ BEST BET: {best['name']} â {best['confidence']}%" if best else "ð¥ BEST BET: AVOID â no market reached threshold",
              f"ð¡ï¸ SAFEST MODEL MARKET: {safe['name']} â {safe['confidence']}%" if safe else "ð¡ï¸ SAFEST MODEL MARKET: AVOID"]
    if not best: lines += ["","â ï¸ AVOID",f"No market reached {MIN_CONFIDENCE}%. The model will not force a recommendation."]
    lines += ["",f"ð Confidence cap: {MAX_CONFIDENCE}%","â¹ï¸ Model confidence is statistical, not a guarantee."]
    return "\n".join(lines)

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"â½ Welcome to Touchline Tipster v{VERSION}!\n\nCommands:\n/team Chelsea\n/fixtures Chelsea\n/analyze Chelsea vs Arsenal\n/apitest")

async def team(update,context):
    q=" ".join(context.args).strip()
    if not q: return await update.message.reply_text("Usage: /team Chelsea")
    rows=search_teams(q)
    await update.message.reply_text("ð TEAM SEARCH\n"+("\n".join(f"{i}. {x['name']}" for i,x in enumerate(rows[:8],1)) if rows else "â No team found."))

async def fixtures(update,context):
    q=" ".join(context.args).strip()
    if not q: return await update.message.reply_text("Usage: /fixtures Chelsea")
    t=await resolve(q,update)
    if not t:return
    try: data,_=api_get("/matches",{"team":t["id"],"season":SEASON,"status":"scheduled"})
    except Exception as e:return await update.message.reply_text(f"â API error: {e}")
    rows=data if isinstance(data,list) else []
    rows.sort(key=lambda x:str(x.get("kickoffAt") or x.get("date") or ""))
    out=[f"ð {t['name']} â upcoming fixtures",f"Season: {SEASON}",""]
    for m in rows[:5]: out.append(f"â¢ {team_obj(m,'home')['name']} vs {team_obj(m,'away')['name']}\n  {m.get('kickoffAt') or m.get('date') or 'TBC'}")
    await update.message.reply_text("\n".join(out) if len(out)>3 else f"ð No scheduled fixtures found for {t['name']}.")

async def analyze(update,context):
    raw=" ".join(context.args).strip()
    parts=raw.split(" vs ",1) if " vs " in raw.lower() else []
    if len(parts)!=2:
        i=raw.lower().find(" vs "); parts=[raw[:i],raw[i+4:]] if i>=0 else []
    if len(parts)!=2:return await update.message.reply_text("Usage: /analyze Chelsea vs Arsenal")
    h=await resolve(parts[0].strip(),update); a=await resolve(parts[1].strip(),update)
    if not h or not a:return
    try: hm,am=matches(h["id"]),matches(a["id"])
    except Exception as e:return await update.message.reply_text(f"â API error: {e}")
    if not hm or not am:return await update.message.reply_text(f"â Not enough completed {SEASON} matches for this analysis.")
    hs,as_=stats(hm,h["id"]),stats(am,a["id"])
    await update.message.reply_text(analysis(h,a,hs,as_,model(hs,as_)))

async def apitest(update,context):
    try:
        _,meta=api_get("/health")
        await update.message.reply_text(f"ð§ OPENFOOT API TEST\n\nâ Connection works.\nSeason: {SEASON}\nAPI: {API_BASE}")
    except Exception as e: await update.message.reply_text(f"â OpenFootAPI test failed.\n\n{e}")

def main():
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")
    threading.Thread(target=health_server,daemon=True).start()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("team",team))
    app.add_handler(CommandHandler("fixtures",fixtures)); app.add_handler(CommandHandler("analyze",analyze)); app.add_handler(CommandHandler("apitest",apitest))
    print(f"Touchline Tipster v{VERSION} starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
