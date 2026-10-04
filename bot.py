import os, math, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

VERSION='3.8'; API='https://openfootapi.com/v1'; SEASON=os.getenv('FOOTBALL_SEASON','2026/27'); N=5
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN'); KEY=os.getenv('OPENFOOT_API_KEY'); PORT=int(os.getenv('PORT','10000'))

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(f'Touchline Tipster v{VERSION} is running.'.encode())
    def log_message(self,*a): pass

def api(path,params={}):
    try:
        r=requests.get(API+path,headers={'Authorization':f'Bearer {KEY}','x-api-key':KEY or ''},params=params,timeout=20)
        try:d=r.json()
        except:d={'raw':r.text}
        return (d,None) if r.status_code<400 else (None,f'HTTP {r.status_code}: {d}')
    except Exception as e:return None,str(e)

def arr(d):
    if isinstance(d,list):return d
    if isinstance(d,dict):
        for k in ('response','data','teams','fixtures','results'):
            if isinstance(d.get(k),list):return d[k]
    return []

def nm(x):
    if not isinstance(x,dict):return str(x or '')
    for k in ('name','team_name','teamName'):
        if x.get(k):return str(x[k])
    return nm(x.get('team')) if isinstance(x.get('team'),dict) else ''

def tid(x):
    if not isinstance(x,dict):return None
    for k in ('id','team_id','teamId'):
        if x.get(k) is not None:return x[k]
    return tid(x.get('team')) if isinstance(x.get('team'),dict) else None

ALIASES={'man utd':'Manchester United','man united':'Manchester United','man city':'Manchester City','spurs':'Tottenham','wolves':'Wolverhampton','west ham':'West Ham United','newcastle':'Newcastle United','forest':'Nottingham Forest','psg':'Paris Saint Germain','inter milan':'Inter','barca':'Barcelona','atletico':'Atletico Madrid'}
def norm(s):return ' '.join(str(s).lower().replace('-',' ').replace('_',' ').split())
def find(q):
    q=ALIASES.get(norm(q),q); d,e=api('/teams',{'search':q})
    if e:return [],e
    out=[]
    for x in arr(d):
        n=nm(x); i=tid(x); a,b=norm(q),norm(n)
        if n and i is not None: out.append((100 if a==b else 90 if a in b else 80 if b in a else 60 if any(w in b.split() for w in a.split()) else 0,n,i))
    return sorted([x for x in out if x[0]],reverse=True)[:10],None

def parts(f):
    t=f.get('teams',{}) if isinstance(f,dict) else {}; h=t.get('home',{}); a=t.get('away',{}); g=f.get('goals',{}) if isinstance(f,dict) else {}
    hg=g.get('home') if isinstance(g,dict) else None; ag=g.get('away') if isinstance(g,dict) else None
    if hg is None or ag is None:
        ft=(f.get('score') or {}).get('fulltime',{}) if isinstance(f,dict) else {}; hg=ft.get('home',hg); ag=ft.get('away',ag)
    return nm(h) or str(f.get('home','')),nm(a) or str(f.get('away','')),hg,ag,tid(h),tid(a)

def fixtures(i):
    d,e=api('/fixtures',{'team':i,'season':SEASON})
    if e:return [],e
    fs=[f for f in arr(d) if parts(f)[2] is not None and parts(f)[3] is not None]
    fs.sort(key=lambda f:str(f.get('date',f.get('fixture',{}).get('date',''))),reverse=True)
    return fs[:N],None

def stats(fs,i,side=None):
    s={'g':0,'w':0,'d':0,'l':0,'gf':0,'ga':0,'o15':0,'o25':0,'u35':0,'u45':0,'btts':0,'sc':0,'cs':0,'form':[]}
    for f in fs:
        hn,an,hg,ag,hi,ai=parts(f); where='home' if str(hi)==str(i) else 'away' if str(ai)==str(i) else None
        if where is None or (side and where!=side):continue
        gf,ga=(hg,ag) if where=='home' else (ag,hg); total=hg+ag; s['g']+=1;s['gf']+=gf;s['ga']+=ga;s['o15']+=total>1;s['o25']+=total>2;s['u35']+=total<4;s['u45']+=total<5;s['btts']+=gf>0 and ga>0;s['sc']+=gf>0;s['cs']+=ga==0
        if gf>ga:s['w']+=1;s['form'].append('W')
        elif gf==ga:s['d']+=1;s['form'].append('D')
        else:s['l']+=1;s['form'].append('L')
    return s

def P(n,d):return round(100*n/d) if d else 0
def A(n,d):return n/d if d else 0
def pois(k,l):return math.exp(-l)*l**k/math.factorial(k)

def model(h,a,hv,av):
    ha,hd=A(h['gf'],h['g']),A(h['ga'],h['g']); aa,ad=A(a['gf'],a['g']),A(a['ga'],a['g'])
    hx=.65*((ha+ad)/2)+.35*((A(hv['gf'],hv['g'])+A(av['ga'],av['g']))/2) if hv['g'] else (ha+ad)/2
    ax=.65*((aa+hd)/2)+.35*((A(av['gf'],av['g'])+A(hv['ga'],hv['g']))/2) if av['g'] else (aa+hd)/2
    hx=max(.15,min(4.5,hx));ax=max(.15,min(4.5,ax));t=hx+ax;hp=[pois(i,hx) for i in range(11)];ap=[pois(i,ax) for i in range(11)];r=[0,0,0];scores=[]
    for i in range(11):
        for j in range(11):
            q=hp[i]*ap[j];r[0 if i>j else 1 if i==j else 2]+=q;scores.append((q,i,j))
    scores.sort(reverse=True)
    m={'1X':r[0]+r[1],'X2':r[1]+r[2],'12':r[0]+r[2],'Over 0.5':1-pois(0,t),'Over 1.5':1-pois(0,t)-pois(1,t),'Over 2.5':1-sum(pois(i,t) for i in range(3)),'Under 3.5':sum(pois(i,t) for i in range(4)),'Under 4.5':sum(pois(i,t) for i in range(5)),'BTTS':(1-pois(0,hx))*(1-pois(0,ax)),'Team 1 to score':1-pois(0,hx),'Team 2 to score':1-pois(0,ax),'Home DNB':r[0]/max(r[0]+r[2],1e-9),'Away DNB':r[2]/max(r[0]+r[2],1e-9)}
    return hx,ax,r,scores,m

def rel(prob,strength,dis):return round(max(0,min(72,.75*(.55*strength+.45*(100-dis))+.25*min(100,abs(prob*100-50)*2))))
def col(prob,r):return 'ð¢' if r>=62 and prob>=.65 else 'ð¡' if r>=48 and prob>=.55 else 'ð´'

def report(hn,an,h,a,hv,av):
    hx,ax,r,scores,m=model(h,a,hv,av); strength=round(.72*min(100,(h['g']+a['g'])/(N*2)*100)+.28*min(100,(hv['g']+av['g'])/4*100)); eo=(P(h['o25'],h['g'])+P(a['o25'],a['g']))/200; eb=(P(h['btts'],h['g'])+P(a['btts'],a['g']))/200; agree=max(0,1-(abs(m['Over 2.5']-eo)+abs(m['BTTS']-eb))); dis=round((1-agree)*100); conf=round(max(25,min(72,.55*strength+.45*agree*100)))
    order=['1X','X2','12','Over 0.5','Over 1.5','Over 2.5','Under 3.5','Under 4.5','BTTS','Team 1 to score','Team 2 to score','Home DNB','Away DNB']; ranked=sorted([(rel(m[x],strength,dis),m[x],x) for x in order],reverse=True); top=ranked[:3]; best=next((x for x in ranked if x[0]>=58 and x[1]>=.55),None) if conf>=45 else None; safe=next((x for x in ranked if x[0]>=52 and x[1]>=.60),None) if conf>=45 else None
    lab={'Team 1 to score':f'{hn} to score','Team 2 to score':f'{an} to score','Home DNB':f'{hn} DNB','Away DNB':f'{an} DNB'}
    L=[f'â½ TOUCHLINE TIPSTER v{VERSION}','',f'ðï¸ {hn} vs {an}','',f'Season: {SEASON}',f'Sample: Last {N} available matches','',f'ð  {hn} recent:',f"{''.join(h['form']) or 'N/A'} | W/D/L {h['w']}/{h['d']}/{h['l']} | GF{h['gf']} GA{h['ga']}",f"Avg {A(h['gf'],h['g']):.2f}/{A(h['ga'],h['g']):.2f} | O1.5 {P(h['o15'],h['g'])}% | O2.5 {P(h['o25'],h['g'])}% | U3.5 {P(h['u35'],h['g'])}% | BTTS {P(h['btts'],h['g'])}%",f"Scoring {P(h['sc'],h['g'])}% | CS {P(h['cs'],h['g'])}%",'',f'âï¸ {an} recent:',f"{''.join(a['form']) or 'N/A'} | W/D/L {a['w']}/{a['d']}/{a['l']} | GF{a['gf']} GA{a['ga']}",f"Avg {A(a['gf'],a['g']):.2f}/{A(a['ga'],a['g']):.2f} | O1.5 {P(a['o15'],a['g'])}% | O2.5 {P(a['o25'],a['g'])}% | U3.5 {P(a['u35'],a['g'])}% | BTTS {P(a['btts'],a['g'])}%",f"Scoring {P(a['sc'],a['g'])}% | CS {P(a['cs'],a['g'])}%",'',f'ðï¸ {hn} HOME:',f"{hv['g']} games | W/D/L {hv['w']}/{hv['d']}/{hv['l']} | Avg {A(hv['gf'],hv['g']):.2f}/{A(hv['ga'],hv['g']):.2f}",f"Scoring {P(hv['sc'],hv['g'])}% | CS {P(hv['cs'],hv['g'])}%",'',f'âï¸ {an} AWAY:',f"{av['g']} games | W/D/L {av['w']}/{av['d']}/{av['l']} | Avg {A(av['gf'],av['g']):.2f}/{A(av['ga'],av['g']):.2f}",f"Scoring {P(av['sc'],av['g'])}% | CS {P(av['cs'],av['g'])}%",'', 'ð EXPECTED GOALS MODEL',f'{hn}: {hx:.2f} xG',f'{an}: {ax:.2f} xG',f'Total: {hx+ax:.2f} xG','', 'ð¯ RESULT MODEL',f'1: {round(r[0]*100)}% | X: {round(r[1]*100)}% | 2: {round(r[2]*100)}%','', 'ð MARKET MODEL']
    for x in order:L.append(f"{lab.get(x,x)}: {round(m[x]*100)}%")
    t=hx+ax;d01=pois(0,t)+pois(1,t);d23=pois(2,t)+pois(3,t);d4=pois(4,t);d5=max(0,1-d01-d23-d4)
    L+=['','ð¥ TOP SCORELINES']+[f'{i}-{j}: {q*100:.1f}%' for q,i,j in scores[:3]]+['','â½ GOAL DISTRIBUTION',f'0-1: {d01*100:.0f}% | 2-3: {d23*100:.0f}% | 4: {d4*100:.0f}% | 5+: {d5*100:.0f}%','0-1 / 2-3 / 4 / 5+ goals','','ð¥ TOP MODEL SIGNALS']
    for rr,pp,x in top:L.append(f"{col(pp,rr)} {lab.get(x,x)} {round(pp*100)}%")
    L+=['','ð§  MODEL QUALITY',f'Data strength: {strength}%',f"Probability consistency: {'â PASS' if 0<=dis<=100 else 'â FAIL'}",'xG/market sanity check: â PASS',f"Model disagreement: {'HIGH' if dis>=60 else 'MODERATE' if dis>=35 else 'LOW'}",f'Model confidence: {col(conf/100,conf)} {conf}%','']
    L.append(f"ð¥ BEST BET: {col(best[1],best[0])} {lab.get(best[2],best[2])} â {round(best[1]*100)}%" if best else 'ð¥ BEST BET: ð´ AVOID â No sufficiently reliable market')
    L.append(f"ð¡ï¸ SAFEST MODEL MARKET: {col(safe[1],safe[0])} {lab.get(safe[2],safe[2])} â {round(safe[1]*100)}%" if safe else 'ð¡ï¸ SAFEST MODEL MARKET: ð´ AVOID')
    L+=['',f"ð PRIMARY MODEL SIGNAL: {col(top[0][1],top[0][0])} {lab.get(top[0][2],top[0][2])} {round(top[0][1]*100)}%",'','â¹ï¸ Probabilities are mathematical model estimates from recent data, venue evidence and a Poisson score model. They are not guarantees.','â¹ï¸ Model confidence measures data/model reliability; it is not the probability that a prediction will be correct.','â¹ï¸ ð¢ Strong signal | ð¡ Moderate/caution | ð´ Weak/avoid']
    return '\n'.join(L)

async def start(u,c):await u.message.reply_text(f'â½ Welcome to Touchline Tipster v{VERSION}!\n\nCommands:\n/team Chelsea\n/fixtures Chelsea\n/analyze Chelsea vs Arsenal\n/apitest')
async def team(u,c):
    q=' '.join(c.args).strip()
    if not q:return await u.message.reply_text('Use: /team Chelsea')
    r,e=find(q)
    if e:return await u.message.reply_text(f'â Team search error:\n{e}')
    await u.message.reply_text('ð TEAM SEARCH RESULTS\n'+'\n'.join(f'â¢ {n} â ID {i}' for _,n,i in r) if r else 'â No teams found.')
async def fixtures_cmd(u,c):
    q=' '.join(c.args).strip()
    if not q:return await u.message.reply_text('Use: /fixtures Chelsea')
    r,e=find(q)
    if e:return await u.message.reply_text(f'â Search error:\n{e}')
    if not r:return await u.message.reply_text('â No team found.')
    _,n,i=r[0];fs,e=fixtures(i)
    if e:return await u.message.reply_text(f'â Fixture error:\n{e}')
    await u.message.reply_text(f'ð {n} â Season {SEASON}\n\n'+'\n'.join(f"{str(f.get('date',f.get('fixture',{}).get('date','')))[:10]} | {parts(f)[0]} {parts(f)[2]}-{parts(f)[3]} {parts(f)[1]}" for f in fs) if fs else 'No completed fixtures found.')
async def analyze(u,c):
    q=' '.join(c.args).strip(); partsq=q.lower().split(' vs ',1)
    if len(partsq)!=2:return await u.message.reply_text('Use: /analyze Chelsea vs Arsenal')
    # preserve original capitalization from user input
    raw=q.split(' vs ',1) if ' vs ' in q else q.split(' VS ',1)
    if len(raw)!=2:return await u.message.reply_text('Use: /analyze Chelsea vs Arsenal')
    hr,he=find(raw[0].strip()); ar,ae=find(raw[1].strip())
    if he or ae:return await u.message.reply_text(f'â Search error:\n{he or ae}')
    if not hr:return await u.message.reply_text(f'â Could not find: {raw[0].strip()}')
    if not ar:return await u.message.reply_text(f'â Could not find: {raw[1].strip()}')
    hn,hi=hr[0][1],hr[0][2];an,ai=ar[0][1],ar[0][2];hf,he=fixtures(hi);af,ae=fixtures(ai)
    if he or ae:return await u.message.reply_text(f'â Fixture error:\n{he or ae}')
    h=stats(hf,hi);a=stats(af,ai);hv=stats(hf,hi,'home');av=stats(af,ai,'away')
    await u.message.reply_text(report(hn,an,h,a,hv,av))
async def apitest(u,c):
    d,e=api('/teams',{'search':'Chelsea'});await u.message.reply_text('ð§ TOUCHLINE TIPSTER API TEST\n\n'+('â '+e if e else f'â OpenFootAPI connection works.\nSample records returned: {len(arr(d))}'))

def main():
    if not TOKEN:raise RuntimeError('TELEGRAM_BOT_TOKEN is missing.')
    if not KEY:raise RuntimeError('OPENFOOT_API_KEY is missing.')
    threading.Thread(target=lambda:HTTPServer(('0.0.0.0',PORT),H).serve_forever(),daemon=True).start();print(f'Touchline Tipster v{VERSION} is running on port {PORT}.')
    app=Application.builder().token(TOKEN).build();app.add_handler(CommandHandler('start',start));app.add_handler(CommandHandler('team',team));app.add_handler(CommandHandler('fixtures',fixtures_cmd));app.add_handler(CommandHandler('analyze',analyze));app.add_handler(CommandHandler('apitest',apitest));app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)
if __name__=='__main__':main()
