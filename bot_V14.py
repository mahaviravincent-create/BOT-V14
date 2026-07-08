"""
V14 FINAL - XAUUSD S/D + H1 Trend + News Filter
=================================================
Modal: $500 | Risk: 1.5% | H1 UP full, H1 DOWN half
Telegram 24/7 | News Filter | ATR Spike Protection
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, json, os, threading
from bs4 import BeautifulSoup

# =============================================================
# KONFIGURASI
# =============================================================
TELEGRAM_TOKEN = "8851160871:AAFXqrTFBniik_brWx1-VOihItp0wocukl0"
TWELVE_API_KEY = "d86c5e6638c84f209cb28132c60631bb"
SYMBOL = "XAU/USD"

INITIAL_BALANCE = 500.0
RISK_FULL = 7.50      # 1.5% - H1 UP
RISK_HALF = 3.75      # 0.75% - H1 DOWN
REWARD_RATIO = 2.0
DAILY_LOSS_LIMIT = 25.0
MAX_POSITIONS = 2
SL_MIN, SL_MAX = 5.0, 20.0
MAX_LOSS_STREAK = 5
ATR_SPIKE_MULT = 2.5
FAST_MA, SLOW_MA = 10, 30
ADX_TREND_MIN = 25

STATE_FILE = "v14_state.json"
MC_FILE = "v14_mc.json"
TZ = ZoneInfo("UTC")
CHECK_INTERVAL = 300

# =============================================================
# UTILS
# =============================================================
def load_json(fp, d=None):
    if d is None: d = {}
    if os.path.exists(fp):
        with open(fp) as f: return json.load(f)
    return d

def save_json(fp, data):
    with open(fp, "w") as f: json.dump(data, f, indent=2, default=str)

def init_state():
    if not os.path.exists(STATE_FILE):
        s = {"balance": INITIAL_BALANCE, "equity": INITIAL_BALANCE, "daily_pnl": 0.0,
             "last_trade_date": str(datetime.now(TZ).date()), "open_positions": [],
             "trade_history": [], "mc_events": [], "mc_count": 0,
             "last_entry_candle_time": None, "chat_ids": [], "loss_streak": 0,
             "paused_until": None,
             "stats": {"buys": {"wins":0,"losses":0}, "sells": {"wins":0,"losses":0}}}
        save_json(STATE_FILE, s)
    return load_json(STATE_FILE)

def send_tg(chat_id, text, parse_mode="HTML"):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     json={"chat_id":chat_id,"text":text,"parse_mode":parse_mode}, timeout=10)
    except: pass

def get_updates(offset=None):
    try:
        return requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                          params={"timeout":30,"offset":offset}, timeout=35).json()
    except: return None

# =============================================================
# NEWS FILTER (No API Key Needed)
# =============================================================
def check_high_impact_news():
    """Cek news USD high impact via TradingView calendar."""
    try:
        url = "https://economic-calendar.tradingview.com/events"
        r = requests.get(url, timeout=5).json()
        now = datetime.now(TZ)
        
        for event in r.get("result", []):
            currency = event.get("currency", "")
            impact = event.get("impact", "")
            title = event.get("title", "")
            event_time = datetime.fromtimestamp(event.get("date", 0), tz=TZ)
            diff = (event_time - now).total_seconds()
            
            if currency == "USD" and impact == "high" and 0 < diff < 1800:
                return True, title
        return False, None
    except:
        # Fallback: hardcoded jadwal NFP (Jumat pertama, 12:30 UTC)
        now = datetime.now(TZ)
        if now.weekday() == 4 and 12 <= now.hour <= 14:
            return True, "Possible NFP Friday"
        return False, None

# =============================================================
# DATA
# =============================================================
def fetch_ohlc(interval="15min", outputsize=150):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": SYMBOL, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVE_API_KEY, "format": "JSON"}
    try:
        r = requests.get(url, params=params, timeout=10).json()
        if "values" not in r: return None
        df = pd.DataFrame(r["values"])
        df = df.rename(columns={"datetime":"time","open":"open","high":"high","low":"low","close":"close"})
        for c in ["open","high","low","close"]: df[c] = pd.to_numeric(df[c])
        df["time"] = pd.to_datetime(df["time"])
        return df.sort_values("time").reset_index(drop=True)
    except: return None

# =============================================================
# INDIKATOR
# =============================================================
def calc_indicators(df, is_h1=False):
    df = df.copy()
    df["ma_fast"] = df["close"].ewm(span=FAST_MA, adjust=False).mean()
    df["ma_slow"] = df["close"].ewm(span=SLOW_MA, adjust=False).mean()
    
    if is_h1:
        h,l,c = df["high"],df["low"],df["close"]
        tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
        df["atr"] = tr.ewm(span=14,adjust=False).mean()
        pdm = h.diff().clip(lower=0); ndm = (l.diff()*-1).clip(lower=0)
        pdm[pdm<=ndm]=0; ndm[ndm<=pdm]=0
        pdi = 100*(pdm.ewm(span=14,adjust=False).mean()/(df["atr"]+1e-10))
        ndi = 100*(ndm.ewm(span=14,adjust=False).mean()/(df["atr"]+1e-10))
        df["adx"] = (100*(pdi-ndi).abs()/(pdi+ndi+1e-10)).ewm(span=14,adjust=False).mean()
        df["di_plus"], df["di_minus"] = pdi, ndi
        df["trend_up"] = (df["ma_fast"]>df["ma_slow"])&(df["adx"]>=ADX_TREND_MIN)&(df["di_plus"]>df["di_minus"])
        df["trend_down"] = (df["ma_fast"]<df["ma_slow"])&(df["adx"]>=ADX_TREND_MIN)&(df["di_minus"]>df["di_plus"])
    else:
        df["atr"] = (df["high"]-df["low"]).ewm(span=14,adjust=False).mean()
        df["atr_avg"] = df["atr"].rolling(100).mean()
        df["atr_spike"] = df["atr"] > (df["atr_avg"]*ATR_SPIKE_MULT)
        delta = df["close"].diff()
        gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
        df["rsi"] = 100-(100/(1+(gain.ewm(span=14,adjust=False).mean()/(loss.ewm(span=14,adjust=False).mean()+1e-10))))
        df["candle_body"] = abs(df["close"]-df["open"])
        df["wick_down"] = df[["close","open"]].min(axis=1)-df["low"]
        df["wick_up"] = df["high"]-df[["close","open"]].max(axis=1)
        df["bull"] = df["close"]>df["open"]
        df["pinbar_bull"] = (df["wick_down"]>df["candle_body"]*2)&(df["wick_up"]<df["candle_body"]*0.5)
        df["pinbar_bear"] = (df["wick_up"]>df["candle_body"]*2)&(df["wick_down"]<df["candle_body"]*0.5)
        bs = df["bull"].shift().fillna(False)
        df["engulf_bull"] = (df["close"]>df["open"].shift())&(df["open"]<df["close"].shift())&df["bull"]&(~bs)
        df["engulf_bear"] = (df["close"]<df["open"].shift())&(df["open"]>df["close"].shift())&(~df["bull"])&bs
    return df

# =============================================================
# S/D ZONES
# =============================================================
def find_zones(df_slice):
    hh,ll = df_slice["high"].values, df_slice["low"].values
    sh,sl = [],[]
    for i in range(2,len(df_slice)-2):
        if hh[i]>hh[i-1] and hh[i]>hh[i-2] and hh[i]>hh[i+1] and hh[i]>hh[i+2]: sh.append(hh[i])
        if ll[i]<ll[i-1] and ll[i]<ll[i-2] and ll[i]<ll[i+1] and ll[i]<ll[i+2]: sl.append(ll[i])
    return sh[-5:],sl[-5:]

# =============================================================
# TRADING
# =============================================================
def open_position(jenis, entry, sl, tp, lot, risk, h1_dir, desc):
    st = load_json(STATE_FILE)
    pos = {"id":len(st["trade_history"])+1,"jenis":jenis,"entry_price":entry,
           "sl_price":sl,"tp_price":tp,"lot":lot,"risk_dollar":risk,
           "h1_dir":h1_dir,"entry_time":str(datetime.now(TZ)),"status":"OPEN","desc":desc}
    st["open_positions"].append(pos)
    save_json(STATE_FILE,st)
    return pos

def check_exits(current_price):
    st = load_json(STATE_FILE)
    closed = []
    for pos in st["open_positions"][:]:
        hit = False; ep = current_price
        if pos["jenis"]=="BUY":
            if current_price<=pos["sl_price"]: ep=pos["sl_price"]; hit=True; result="SL"
            elif current_price>=pos["tp_price"]: ep=pos["tp_price"]; hit=True; result="TP"
        else:
            if current_price>=pos["sl_price"]: ep=pos["sl_price"]; hit=True; result="SL"
            elif current_price<=pos["tp_price"]: ep=pos["tp_price"]; hit=True; result="TP"
        
        if hit:
            profit = ((ep-pos["entry_price"])*pos["lot"]*100) if pos["jenis"]=="BUY" else ((pos["entry_price"]-ep)*pos["lot"]*100)
            pos["exit_price"]=ep; pos["exit_time"]=str(datetime.now(TZ))
            pos["profit"]=round(profit,2); pos["status"]=result
            st["balance"]=round(st["balance"]+profit,2); st["equity"]=st["balance"]
            
            if profit>0: st["loss_streak"]=0
            else: st["loss_streak"]=st.get("loss_streak",0)+1
            
            # Stats
            if pos["jenis"]=="BUY":
                if profit>0: st["stats"]["buys"]["wins"]+=1
                else: st["stats"]["buys"]["losses"]+=1
            else:
                if profit>0: st["stats"]["sells"]["wins"]+=1
                else: st["stats"]["sells"]["losses"]+=1
            
            st["trade_history"].append(pos.copy())
            closed.append(pos.copy())
            st["open_positions"].remove(pos)
            
            td = str(datetime.now(TZ).date())
            if st.get("last_trade_date")!=td: st["daily_pnl"]=0.0; st["last_trade_date"]=td
            st["daily_pnl"]=round(st["daily_pnl"]+profit,2)
            
            if st["balance"]<5.0:
                st["mc_count"]+=1
                st["mc_events"].append({"mc_ke":st["mc_count"],"waktu":str(datetime.now(TZ)),
                    "trades":len(st["trade_history"]),"balance":st["balance"],
                    "snapshot":st["trade_history"][-30:]})
                save_json(MC_FILE,{"mc_events":st["mc_events"]})
    
    upnl = 0
    for pos in st["open_positions"]:
        upnl += ((current_price-pos["entry_price"])*pos["lot"]*100) if pos["jenis"]=="BUY" else ((pos["entry_price"]-current_price)*pos["lot"]*100)
    st["equity"] = round(st["balance"]+upnl,2)
    save_json(STATE_FILE,st)
    return closed

# =============================================================
# TELEGRAM HANDLER
# =============================================================
def handle_cmd(chat_id, cmd, args):
    st = load_json(STATE_FILE)
    if cmd=="/start":
        if chat_id not in st.get("chat_ids",[]):
            st.setdefault("chat_ids",[]).append(chat_id)
            save_json(STATE_FILE,st)
        paused = st.get("paused_until")
        pause_text = f"\n⏸️ PAUSED until {paused[:16]}" if paused and datetime.fromisoformat(paused)>datetime.now(TZ) else ""
        s = st["stats"]
        send_tg(chat_id,f"""🤖 <b>V14 FINAL XAUUSD</b>

💰 Balance: ${st['balance']:.2f}
📈 Posisi: {len(st['open_positions'])}/{MAX_POSITIONS}
🔄 Trades: {len(st['trade_history'])} | MC: {st['mc_count']}
📉 Loss Streak: {st.get('loss_streak',0)}/{MAX_LOSS_STREAK}
📅 Daily PnL: ${st.get('daily_pnl',0):.2f}{pause_text}

📊 BUYs: W{s['buys']['wins']} L{s['buys']['losses']}
📊 SELLs: W{s['sells']['wins']} L{s['sells']['losses']}

/balance /sinyal /history /rekap /mclog""")
    
    elif cmd=="/balance":
        send_tg(chat_id,f"💰 Balance: ${st['balance']:.2f}\n📈 Equity: ${st['equity']:.2f}\n📅 Daily: ${st.get('daily_pnl',0):.2f}")
    
    elif cmd=="/sinyal":
        h1 = fetch_ohlc("1h", 100)
        m15 = fetch_ohlc("15min", 100)
        if h1 is not None and m15 is not None:
            h1 = calc_indicators(h1, is_h1=True)
            m15 = calc_indicators(m15)
            h1c = h1.iloc[-1]
            m15c = m15.iloc[-2]
            sup,dem = find_zones(m15.iloc[-50:])
            send_tg(chat_id,f"""📊 XAUUSD
H1: {'🟢 UP' if h1c['trend_up'] else '🔴 DOWN' if h1c['trend_down'] else '⚪ RANGE'}
M15: ${m15c['close']:.2f} | RSI:{m15c['rsi']:.0f}
Supply: {[f'${z:.0f}' for z in sup[-3:]]}
Demand: {[f'${z:.0f}' for z in dem[-3:]]}
Pinbar: {'✅' if m15c['pinbar_bull'] or m15c['pinbar_bear'] else '❌'}
Engulf: {'✅' if m15c['engulf_bull'] or m15c['engulf_bear'] else '❌'}""")
    
    elif cmd=="/history":
        hist = st["trade_history"]
        if not hist: send_tg(chat_id,"📭 No trades yet")
        else:
            txt = "📜 <b>Last 5:</b>"
            for t in hist[-5:]:
                em = "🟢" if t.get("profit",0)>0 else "🔴"
                txt += f"\n{em} {t['jenis']} | {t.get('h1_dir','?')} | ${t.get('profit',0):.2f}"
            send_tg(chat_id,txt)
    
    elif cmd=="/rekap":
        hist = st["trade_history"]
        if len(hist)<1: send_tg(chat_id,"📭 No data")
        else:
            week = [t for t in hist if datetime.fromisoformat(t["entry_time"])>datetime.now(TZ)-timedelta(days=7)]
            wins = sum(1 for t in week if t.get("profit",0)>0)
            pnl = sum(t.get("profit",0) for t in week)
            send_tg(chat_id,f"📊 <b>7-Day Recap</b>\nTrades: {len(week)}\nWin Rate: {(wins/len(week)*100):.0f}%\nPnL: ${pnl:.2f}")
    
    elif cmd=="/mclog":
        mc = load_json(MC_FILE,{"mc_events":[]})
        if not mc["mc_events"]: send_tg(chat_id,"✅ No MC yet")
        else:
            txt = f"⚠️ <b>MC ({len(mc['mc_events'])}):</b>"
            for m in mc["mc_events"][-3:]:
                txt += f"\nMC #{m['mc_ke']} | {m['trades']} trades | ${m['balance']:.2f}"
            send_tg(chat_id,txt)

# =============================================================
# MAIN LOOP
# =============================================================
def trading_cycle():
    st = load_json(STATE_FILE)
    
    # Daily reset
    td = str(datetime.now(TZ).date())
    if st.get("last_trade_date")!=td: st["daily_pnl"]=0.0; st["last_trade_date"]=td; save_json(STATE_FILE,st)
    
    # Daily loss limit
    if st.get("daily_pnl",0)<=-DAILY_LOSS_LIMIT: return
    
    # Pause check
    paused = st.get("paused_until")
    if paused and datetime.fromisoformat(paused)>datetime.now(TZ): return
    elif paused: st["loss_streak"]=0; st["paused_until"]=None; save_json(STATE_FILE,st)
    
    # News filter
    news, news_title = check_high_impact_news()
    if news:
        for cid in st.get("chat_ids",[]): send_tg(cid,f"📰 News: {news_title}\n⏭️ Skipping entry")
        return
    
    # Data
    h1 = fetch_ohlc("1h", 100)
    m15 = fetch_ohlc("15min", 150)
    if h1 is None or m15 is None: return
    
    h1 = calc_indicators(h1, is_h1=True)
    m15 = calc_indicators(m15)
    
    h1c = h1.iloc[-1]
    cp = m15.iloc[-1]["close"]
    
    # Exits
    closed = check_exits(cp)
    for pos in closed:
        for cid in st.get("chat_ids",[]):
            em = "🟢" if pos.get("profit",0)>0 else "🔴"
            send_tg(cid,f"{em} {pos['jenis']} | {pos['h1_dir']} | {pos['status']} | ${pos.get('profit',0):.2f}\nBalance: ${load_json(STATE_FILE)['balance']:.2f}")
    
    st = load_json(STATE_FILE)
    
    # MC check
    if st["balance"]<5.0:
        for cid in st.get("chat_ids",[]): send_tg(cid,f"⚠️ MC #{st['mc_count']}! ${st['balance']:.2f}")
        return
    
    # Loss streak pause
    if st.get("loss_streak",0)>=MAX_LOSS_STREAK:
        st["paused_until"]=str(datetime.now(TZ)+timedelta(hours=24))
        save_json(STATE_FILE,st)
        for cid in st.get("chat_ids",[]): send_tg(cid,f"⏸️ Paused 24h - {MAX_LOSS_STREAK}x loss streak")
        return
    
    if len(st["open_positions"])>=MAX_POSITIONS: return
    
    # Skip if H1 ranging
    if not h1c["trend_up"] and not h1c["trend_down"]: return
    
    # Skip ATR spike
    if m15.iloc[-2]["atr_spike"]: return
    
    last_entry = st.get("last_entry_candle_time")
    ct = m15.iloc[-2]["time"]
    if last_entry and ct==pd.to_datetime(last_entry): return
    
    sup,dem = find_zones(m15.iloc[-100:])
    row = m15.iloc[-2]
    cp_entry = cp
    
    # BUY - H1 UP only
    if h1c["trend_up"]:
        for d in dem:
            if abs(cp_entry-d)/(d+1e-10)<0.003 and row["rsi"]<45:
                rej = row["pinbar_bull"] or row["engulf_bull"] or (row["bull"] and row["wick_down"]>row["candle_body"])
                if rej:
                    sl = max(min(row["atr"]*1.0,SL_MAX),SL_MIN)
                    lot = round(RISK_FULL/(sl*100)/0.01)*0.01
                    lot = max(0.01,min(lot,1.0))
                    open_position("BUY",cp_entry,cp_entry-sl,cp_entry+(sl*REWARD_RATIO),lot,RISK_FULL,"UP",f"Demand ${d:.0f}")
                    st=load_json(STATE_FILE); st["last_entry_candle_time"]=str(ct); save_json(STATE_FILE,st)
                    for cid in st.get("chat_ids",[]): send_tg(cid,f"🚀 <b>BUY</b> | H1 UP\n@{cp_entry:.2f} SL:{cp_entry-sl:.2f} TP:{cp_entry+(sl*REWARD_RATIO):.2f}\nRisk:${RISK_FULL}")
                    break
    
    # SELL - H1 DOWN only (half risk)
    if h1c["trend_down"]:
        for sp in sup:
            if abs(cp_entry-sp)/(sp+1e-10)<0.003 and row["rsi"]>55:
                rej = row["pinbar_bear"] or row["engulf_bear"] or (not row["bull"] and row["wick_up"]>row["candle_body"])
                if rej:
                    sl = max(min(row["atr"]*1.0,SL_MAX),SL_MIN)
                    lot = round(RISK_HALF/(sl*100)/0.01)*0.01
                    lot = max(0.01,min(lot,1.0))
                    open_position("SELL",cp_entry,cp_entry+sl,cp_entry-(sl*REWARD_RATIO),lot,RISK_HALF,"DOWN",f"Supply ${sp:.0f}")
                    st=load_json(STATE_FILE); st["last_entry_candle_time"]=str(ct); save_json(STATE_FILE,st)
                    for cid in st.get("chat_ids",[]): send_tg(cid,f"🚀 <b>SELL</b> | H1 DOWN\n@{cp_entry:.2f} SL:{cp_entry+sl:.2f} TP:{cp_entry-(sl*REWARD_RATIO):.2f}\nRisk:${RISK_HALF} (½)")
                    break

# =============================================================
# TELEGRAM LISTENER
# =============================================================
def telegram_listener():
    print("👂 Listener started")
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            if updates and updates.get("ok") and updates.get("result"):
                for u in updates["result"]:
                    offset = u["update_id"]+1
                    if "message" in u and "text" in u["message"]:
                        msg = u["message"]; cid=msg["chat"]["id"]; txt=msg["text"]
                        if txt.startswith("/"):
                            parts = txt.split()
                            handle_cmd(cid,parts[0].lower(),parts[1:] if len(parts)>1 else [])
            time.sleep(1)
        except Exception as e: print(f"Listener error: {e}"); time.sleep(5)

# =============================================================
# MAIN
# =============================================================
if __name__=="__main__":
    print("="*60)
    print("V14 FINAL - XAUUSD S/D + H1 + News Filter")
    print(f"   Modal: ${INITIAL_BALANCE} | Risk: ${RISK_FULL} (BUY) / ${RISK_HALF} (SELL)")
    print(f"   Daily Loss Limit: ${DAILY_LOSS_LIMIT}")
    print("="*60)
    init_state()
    threading.Thread(target=telegram_listener, daemon=True).start()
    print("✅ Bot siap. /start di Telegram.")
    while True:
        try:
            trading_cycle()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt: print("\nStop"); break
        except Exception as e: print(f"Error: {e}"); time.sleep(60)