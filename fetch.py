#!/usr/bin/env python3
"""btc-data collector for GitHub Actions. Keys come from env (repo Secrets).
Writes summary.json (same schema as upload.ps1) into the repo root."""
import os, json, time, datetime as dt, urllib.request, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
CG = os.environ.get("COINGLASS_KEY", "").strip()
CQ = os.environ.get("CRYPTOQUANT_KEY", "").strip()
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC,ETH,SOL,UNI,SUI,ADA,XRP,DOGE,LTC").split(",") if s.strip()]

def fp(x):
    return f"{x:.0f}" if x>=1000 else (f"{x:.2f}" if x>=10 else (f"{x:.3f}" if x>=1 else f"{x:.4f}"))
summary = {"ts_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "heatmap": {}, "coinbase": {}, "etf": None, "errors": [], "source": "github-actions"}

def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def cb_price(sym):
    try:
        return float(get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker")["price"])
    except Exception:
        return None

# 1) Coinglass heatmap
if CG:
    for sym in SYMBOLS:
        got = None
        for u in [f"https://open-api-v4.coinglass.com/api/futures/liquidation/aggregated-heatmap/model1?symbol={sym}&range=24h",
                  f"https://open-api-v4.coinglass.com/api/futures/liquidation/heatmap/model1?exchange=Binance&symbol={sym}USDT&range=24h"]:
            try:
                r = get(u, {"CG-API-KEY": CG})
                if str(r.get("code")) == "0" and r.get("data"):
                    got = r; break
                summary["errors"].append(f"coinglass {sym} {u} -> code={r.get('code')} msg={r.get('msg')}")
            except Exception as e:
                summary["errors"].append(f"coinglass {sym} {u} -> {e}")
        if not got: continue
        try:
            d = got["data"]; ys = [float(y) for y in d["y_axis"]]; liq = d["liquidation_leverage_data"]
            px = None
            if d.get("price_candlesticks"):
                px = float(d["price_candlesticks"][-1][4])
            if not px: px = cb_price(sym)
            if not px: px = (ys[0] + ys[-1]) / 2
            acc = {}
            for cell in liq:
                yi = int(cell[1]); acc[yi] = acc.get(yi, 0.0) + float(cell[2])
            rows = [(ys[yi], v) for yi, v in acc.items() if yi < len(ys)]
            maxv = max((v for _, v in rows), default=1.0) or 1.0
            def pack(rs):
                return [{"price": round(p, 2 if p>=10 else 5), "dist_pct": round((p / px - 1) * 100, 2), "strength": round(v / maxv, 2)} for p, v in rs]
            below = sorted([r for r in rows if px * 0.94 < r[0] < px], key=lambda r: -r[1])[:5]
            above = sorted([r for r in rows if px < r[0] < px * 1.06], key=lambda r: -r[1])[:5]
            summary["heatmap"][sym] = {"price": px, "below": pack(below), "above": pack(above), "window": "24h", "levels": len(ys)}
        except Exception as e:
            summary["errors"].append(f"heatmap parse {sym} -> {e}")
else:
    summary["errors"].append("coinglass key empty, skipped")

# 2) Coinbase spot
try:
    trades = get("https://api.exchange.coinbase.com/products/BTC-USD/trades?limit=1000")
    buy = sell = 0.0; tmin = tmax = None
    for t in trades:
        sz = float(t["size"])
        if t["side"] == "sell": buy += sz
        else: sell += sz
        tt = t["time"]
        tmin = tt if tmin is None or tt < tmin else tmin
        tmax = tt if tmax is None or tt > tmax else tmax
    cbpx = cb_price("BTC"); bnpx = None; ref = "binance"
    try: bnpx = float(get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])
    except Exception:
        try: bnpx = float(get("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT")["data"][0]["last"]); ref = "okx"
        except Exception as e: summary["errors"].append(f"ref price -> {e}")
    def ts(s): return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    span = round((ts(tmax) - ts(tmin)).total_seconds() / 60, 1) if tmin and tmax else None
    summary["coinbase"] = {"price": cbpx, "ref": ref, "binance_price": bnpx, "premium_pct": round((cbpx / bnpx - 1) * 100, 3) if (cbpx and bnpx) else None,
                           "last1000_taker_buy_btc": round(buy, 3), "last1000_taker_sell_btc": round(sell, 3),
                           "buy_ratio": round(buy / (buy + sell) * 100, 1) if buy + sell else None, "span_min": span}
except Exception as e:
    summary["errors"].append(f"coinbase -> {e}")

# 3) ETF flows (Coinglass)
if CG:
    try:
        e = get("https://open-api-v4.coinglass.com/api/etf/bitcoin/flow-history", {"CG-API-KEY": CG})
        if str(e.get("code")) == "0" and e.get("data"):
            arr = e["data"]; summary["etf"] = arr[-7:]; summary["etf_source"] = "coinglass"
        else: summary["errors"].append(f"coinglass etf -> code={e.get('code')} msg={e.get('msg')}")
    except Exception as ex:
        summary["errors"].append(f"coinglass etf -> {ex}")

# 4) CryptoQuant on-chain
if CQ:
    summary["cryptoquant"] = {}
    for k, u in [
        ("exch_netflow_day", "https://api.cryptoquant.com/v1/btc/exchange-flows/netflow?exchange=all_exchange&window=day&limit=3"),
        ("exch_reserve_day", "https://api.cryptoquant.com/v1/btc/exchange-flows/reserve?exchange=all_exchange&window=day&limit=3"),
        ("taker_buy_sell", "https://api.cryptoquant.com/v1/btc/market-data/taker-buy-sell-stats?exchange=all_exchange&window=day&limit=3"),
        ("lth_sopr", "https://api.cryptoquant.com/v1/btc/market-indicator/sopr-ratio?window=day&limit=3"),
        ("miner_outflow", "https://api.cryptoquant.com/v1/btc/miner-flows/outflow?miner=all_miner&window=day&limit=3"),
    ]:
        try:
            r = get(u, {"Authorization": f"Bearer {CQ}"})
            if r.get("status", {}).get("code") == 200 and r.get("result", {}).get("data"):
                summary["cryptoquant"][k] = r["result"]["data"]
            else: summary["errors"].append(f"cq {k} -> {json.dumps(r)[:160]}")
        except Exception as e:
            summary["errors"].append(f"cq {k} -> {e}")

# 5) Coinglass extras: global OI / long-short / top traders / liquidations / liquidation map / orderbook / options / stablecoin
if CG:
    summary["cg"] = {}
    B = "https://open-api-v4.coinglass.com"
    extras = [
        ("oi_agg_1h",      [f"{B}/api/futures/open-interest/aggregated-history?symbol=BTC&interval=1h&limit=24",
                            f"{B}/api/futures/open-interest/aggregated-history?symbol=BTC&interval=h1&limit=24"]),
        ("ls_global_1h",   [f"{B}/api/futures/global-long-short-account-ratio/history?exchange=Binance&symbol=BTCUSDT&interval=1h&limit=24",
                            f"{B}/api/futures/global-long-short-account-ratio/history?symbol=BTCUSDT&exchange=Binance&interval=h1&limit=24"]),
        ("ls_top_acct_1h", [f"{B}/api/futures/top-long-short-account-ratio/history?exchange=Binance&symbol=BTCUSDT&interval=1h&limit=24"]),
        ("ls_top_pos_1h",  [f"{B}/api/futures/top-long-short-position-ratio/history?exchange=Binance&symbol=BTCUSDT&interval=1h&limit=24"]),
        ("liq_agg_1h",     [f"{B}/api/futures/liquidation/aggregated-history?symbol=BTC&exchange_list=Binance,OKX,Bybit,Bitget,Gate&interval=1h&limit=24"]),
        ("liq_by_exch_24h",[f"{B}/api/futures/liquidation/exchange-list?symbol=BTC&range=24h",
                            f"{B}/api/futures/liquidation/exchange-list?symbol=BTC&range=1d"]),
        ("liq_map_agg",    [f"{B}/api/futures/liquidation/aggregated-map?symbol=BTC&range=1d",
                            f"{B}/api/futures/liquidation/aggregated-map?symbol=BTC&range=24h"]),
        ("orderbook_hist", [f"{B}/api/spot/orderbook/history?exchange=Binance&symbol=BTCUSDT&interval=1h&limit=12",
                            f"{B}/api/spot/orderbook/history?exchange=Coinbase&symbol=BTC-USD&interval=1h&limit=12"]),
        ("opt_max_pain",   [f"{B}/api/option/max-pain?symbol=BTC&exchange=Deribit",
                            f"{B}/api/option/max-pain?symbol=BTC"]),
        ("opt_info",       [f"{B}/api/option/info?symbol=BTC"]),
        ("funding_oi_weighted", [f"{B}/api/futures/funding-rate/oi-weight-history?symbol=BTC&interval=1h&limit=6"]),
        ("stablecoin_mcap",[f"{B}/api/index/stableCoin-marketCap-history",
                            f"{B}/api/index/stableCoin-marketCap-history?interval=1d&limit=14"]),
    ]
    for key, urls in extras:
        ok = False
        for u in urls:
            try:
                r = get(u, {"CG-API-KEY": CG})
                if str(r.get("code")) == "0" and r.get("data") not in (None, [], {}):
                    d = r["data"]
                    if isinstance(d, list) and len(d) > 8:
                        d = d[-8:]
                    txt = json.dumps(d, ensure_ascii=False)
                    summary["cg"][key] = d if len(txt) < 6000 else json.loads(txt[:0] or "null") or {"truncated_sample": txt[:3000]}
                    ok = True; break
                else:
                    summary["errors"].append(f"cg {key} {u.split('?')[0].split('/api/')[1]} -> code={r.get('code')} msg={str(r.get('msg'))[:80]}")
            except Exception as e:
                summary["errors"].append(f"cg {key} -> {str(e)[:100]}")
        if not ok: summary["cg"][key] = None

# 5b) OKX accounts (read-only API). Only ratios/distances are published; no balances.
import hmac, hashlib, base64
def okx_get(path, key, sec, pwd):
    ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.datetime.utcnow().microsecond//1000:03d}Z"
    sign = base64.b64encode(hmac.new(sec.encode(), (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    req = urllib.request.Request("https://www.okx.com" + path, headers={"OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": pwd, "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read().decode())
ACCTS = [("OKX1", "OKX_1"), ("OKX2", "OKX_2"), ("OKX3", "OKX_3"), ("OKX4", "OKX_4")]
summary["accounts"] = {}
for label, pre in ACCTS:
    k, sc, pw = (os.environ.get(pre+"_KEY","").strip(), os.environ.get(pre+"_SECRET","").strip(), os.environ.get(pre+"_PASS","").strip())
    if not (k and sc and pw): continue
    try:
        bal = okx_get("/api/v5/account/balance", k, sc, pw)["data"][0]
        pos = okx_get("/api/v5/account/positions?instType=SWAP", k, sc, pw)["data"]
        eq = float(bal.get("totalEq") or 0)
        mmr = 0.0
        for src in (bal.get("mgnRatio"), (bal.get("details") or [{}])[0].get("mgnRatio") if bal.get("details") else None):
            try:
                if src not in (None, ""): mmr = float(src); break
            except Exception: pass
        if not mmr:
            try:
                for p_ in pos:
                    if p_.get("mgnRatio") not in (None, ""): mmr = float(p_["mgnRatio"]); break
            except Exception: pass
        avail = float(bal.get("availEq") or 0) or sum(float(d.get("availEq") or 0) for d in (bal.get("details") or []))
        notional = 0.0; coins = {}
        for p_ in pos:
            n = abs(float(p_.get("notionalUsd") or 0)); notional += n
            c = p_["instId"].split("-")[0]
            coins[c] = {"notional_pct": 0, "vs_avg_pct": round((float(p_.get("markPx") or 0)/float(p_.get("avgPx") or 1)-1)*100, 2), "liq": float(p_.get("liqPx") or 0), "mark": float(p_.get("markPx") or 0), "avg": float(p_.get("avgPx") or 0)}
        for c, v in coins.items(): v["notional_pct"] = round(abs(float([q for q in pos if q["instId"].split("-")[0]==c][0].get("notionalUsd") or 0)) / notional * 100, 1) if notional else 0
        # cushion: equity - maintenance margin, as % of notional = how far整体 can drop before liquidation
        mm = sum(float(p_.get("mmr") or 0) for p_ in pos) or (eq / mmr if mmr else 0)
        cushion_pct = round((eq - mm) / notional * 100, 2) if notional else None
        summary["accounts"][label] = {"mgn_ratio_pct": round(mmr*100, 1), "leverage": round(notional/eq, 2) if eq else None, "drop_to_liq_pct": cushion_pct, "coins": coins, "avail_pct": round(avail/eq*100, 1) if eq else None}
    except Exception as e:
        summary["errors"].append(f"okx {label} -> {str(e)[:120]}")

# 6) Telegram alerts (market only). State kept in alerts_state.json to avoid repeats.
try:
    TG_TOKEN = os.environ.get("TG_TOKEN", "").strip(); TG_CHAT = os.environ.get("TG_CHAT_ID", "").strip()
    if TG_TOKEN and TG_CHAT:
        try:
            state = json.load(open("alerts_state.json")) if os.path.exists("alerts_state.json") else {}
        except Exception:
            state = {}
        now = time.time(); alerts = []
        def fire(key, text, cooldown_h=4):
            if now - state.get(key, 0) > cooldown_h * 3600:
                alerts.append(text); state[key] = now
        # heatmap proximity: strongest band within 0.6%
        for sym, h in summary["heatmap"].items():
            for side, rows in (("支撑", h["below"]), ("起飞", h["above"])):
                for b in rows[:2]:
                    if b["strength"] >= 0.8 and abs(b["dist_pct"]) <= 0.6:
                        fire(f"hm_{sym}_{side}_{round(b['price'], 0 if b['price']>=100 else 3)}",
                             (f"🔥 {sym} {fp(h['price'])} 逼近短期支撑 {fp(b['price'])}（{b['dist_pct']:+.2f}%，强度{b['strength']}）跌破=多头止损连锁" if side=="支撑" else f"🚀 {sym} {fp(h['price'])} 逼近突破起飞点 {fp(b['price'])}（{b['dist_pct']:+.2f}%，强度{b['strength']}）站上=空头止损连锁"), 3)
        # ETF big flow (latest day)
        if summary.get("etf"):
            e = summary["etf"][-1]; fu = e.get("flow_usd")
            if fu is not None and abs(fu) >= 3e8:
                d = dt.datetime.utcfromtimestamp(e["timestamp"]/1000).strftime("%m-%d")
                fire(f"etf_{d}", f"🏦 ETF {d} 净{'流入' if fu>0 else '流出'} {abs(fu)/1e6:.0f}M", 20)
        cq = summary.get("cryptoquant") or {}
        if cq.get("exch_netflow_day"):
            r = cq["exch_netflow_day"][0]
            if r["netflow_total"] >= 5000: fire(f"nf_{r['date']}", f"⚠️ 链上 {r['date'][5:]} 交易所净流入 +{r['netflow_total']:.0f} BTC（进所抛压）", 20)
        if cq.get("miner_outflow"):
            r = cq["miner_outflow"][0]
            if r["outflow_total"] >= 8000: fire(f"mo_{r['date']}", f"⛏️ 矿工 {r['date'][5:]} 流出 {r['outflow_total']:.0f} BTC", 20)
        # 2) ladder feet proximity (main account): coin -> (减仓脚/止盈, 补仓脚)
        FEET = {"BTC": (82474.08, 76441.91), "SOL": (106.31, 93.89), "UNI": (8.0167, 6.9749), "SUI": (0.9645, 0.8792), "ADA": (0.2209, 0.1994)}
        for sym, (tp, foot) in FEET.items():
            h = summary["heatmap"].get(sym); px = h["price"] if h else None
            if not px: continue
            for name, lvl in (("减仓脚", tp), ("补仓脚", foot)):
                if lvl and abs(px/lvl-1) <= 0.005:
                    fire(f"foot_{sym}_{name}", f"🎯 {sym} {fp(px)} 距主账户{name} {fp(lvl)} 仅 {(lvl/px-1)*100:+.2f}%", 2)
        # 4) funding extremes
        try:
            fr = (summary.get("cg") or {}).get("funding_oi_weighted")
            if fr:
                v = fr[-1]; rate = float(v.get("close", v.get("fundingRate", v.get("funding_rate", 0))))
                if rate >= 0.05 or rate <= -0.02:
                    fire(f"fund_{int(now//3600)}", f"💸 BTC 全网费率 {rate:.3f}%（{'多头过热' if rate>0 else '空头过热'}）", 4)
        except Exception as e: summary["errors"].append(f"funding -> {e}")
        # 5) Coinbase premium flip (negative -> positive, persisted)
        try:
            cbp = (summary.get("coinbase") or {}).get("premium_pct")
            if cbp is not None:
                hist = state.get("cbp_hist", []); hist = (hist + [cbp])[-4:]; state["cbp_hist"] = hist
                if len(hist) == 4 and all(x > 0.02 for x in hist) and state.get("cbp_sign", -1) < 0:
                    fire("cbp_flip_pos", f"🇺🇸 Coinbase 溢价转正并持续 1 小时（{cbp:+.3f}%）= 美国买盘回来", 6); state["cbp_sign"] = 1
                if len(hist) == 4 and all(x < -0.02 for x in hist) and state.get("cbp_sign", 1) > 0:
                    fire("cbp_flip_neg", f"🇺🇸 Coinbase 溢价转负并持续 1 小时（{cbp:+.3f}%）", 6); state["cbp_sign"] = -1
        except Exception as e: summary["errors"].append(f"cbp -> {e}")
        # 3) Deribit weekly expiry (Fri 08:00Z) reminder 2h before, with max pain
        try:
            utc = dt.datetime.utcnow()
            if utc.weekday() == 4 and 5 <= utc.hour < 8:
                mp = (summary.get("cg") or {}).get("opt_max_pain"); mptxt = ""
                if mp:
                    m0 = mp[0] if isinstance(mp, list) else mp
                    mptxt = f"，最大痛点 {m0.get('max_pain_price', m0.get('maxPain', '?'))}"
                fire(f"expiry_{utc.strftime('%Y%m%d')}", f"⏰ 周五 08:00Z Deribit 周交割{mptxt}，前后 15 分钟易出针", 24)
        except Exception as e: summary["errors"].append(f"expiry -> {e}")
        # 6) macro calendar (UTC): remind 30 min before
        MACRO = [("2026-11-10 13:30", "美国 10 月 CPI"), ("2026-11-13 13:30", "美国 PPI"), ("2026-11-18 19:00", "FOMC 纪要"), ("2026-11-18 21:20", "英伟达财报"), ("2026-11-25 13:30", "美国 PCE"), ("2026-11-27 08:00", "Deribit 月度交割"), ("2026-12-04 13:30", "美国 11 月非农"), ("2026-12-10 19:00", "FOMC 利率决议")]
        for when, what in MACRO:
            t = dt.datetime.strptime(when, "%Y-%m-%d %H:%M"); delta = (t - dt.datetime.utcnow()).total_seconds()
            if 0 < delta <= 35*60:
                fire(f"macro_{when}", f"📅 30 分钟后 {t.strftime('%H:%M')}Z：{what}。数据前仓位保持锁定。", 12)
        # account thresholds
        for label, a in (summary.get("accounts") or {}).items():
            r = a.get("mgn_ratio_pct"); d = a.get("drop_to_liq_pct")
            if r is not None:
                if r < 400: fire(f"acct_{label}_400", f"🚨 OKX {label}账户 维持保证金率 {r:.0f}% <400%，距强平≈{d}%", 2)
                elif r < 600: fire(f"acct_{label}_600", f"⚠️ OKX {label}账户 维持保证金率 {r:.0f}% <600%，距强平≈{d}%", 4)
            for c, v in (a.get("coins") or {}).items():
                if v.get("vs_avg_pct") is not None and v["vs_avg_pct"] <= -20: fire(f"acct_{label}_{c}_20", f"📉 {label} {c} 标记价低于成本 {v['vs_avg_pct']}%", 12)
        # BTC 15m move via Deribit
        try:
            r = get("https://www.deribit.com/api/v2/public/get_tradingview_chart_data?instrument_name=BTC-PERPETUAL&resolution=15&start_timestamp=%d&end_timestamp=%d" % (int((now-1800)*1000), int(now*1000)))["result"]
            if r.get("close") and len(r["close"]) >= 2:
                o, c = r["open"][-2], r["close"][-2]; chg = (c/o-1)*100
                if abs(chg) >= 1.0: fire(f"mv_{int(now//900)}", f"⚡ BTC 15m {chg:+.2f}% → {c:.0f}（量{r['volume'][-2]:.0f}）", 0)
        except Exception as e:
            summary["errors"].append(f"deribit -> {e}")
        # periodic brief every 30 min
        if now - state.get("brief", 0) >= 29 * 60:
            try:
                parts = [f"📊 {dt.datetime.utcnow().strftime('%H:%M')}Z（支撑=下方最厚清算带，跌破加速；起飞=上方最厚清算带，站上加速）"]
                for sym, h in summary["heatmap"].items():
                    b = max(h["below"], key=lambda x: x["strength"]) if h["below"] else None
                    a = max(h["above"], key=lambda x: x["strength"]) if h["above"] else None
                    parts.append(f"{sym} {fp(h['price'])}" + (f" 支撑{fp(b['price'])}({b['dist_pct']:+.1f}%)" if b else "") + (f" 起飞{fp(a['price'])}({a['dist_pct']:+.1f}%)" if a else ""))
                cb = summary.get("coinbase") or {}
                if cb.get("buy_ratio") is not None: parts.append(f"CB吃单买{cb['buy_ratio']}% 溢价{cb.get('premium_pct')}%")
                if summary.get("etf"):
                    e = summary["etf"][-1]; parts.append(f"ETF {dt.datetime.utcfromtimestamp(e['timestamp']/1000).strftime('%m-%d')}:{e.get('flow_usd',0)/1e6:+.0f}M")
                if cq.get("exch_netflow_day"):
                    r0 = cq["exch_netflow_day"][0]; parts.append(f"链上{r0['date'][5:]}净流入{r0['netflow_total']:+.0f}")
                if cq.get("miner_outflow"):
                    parts.append(f"矿工{cq['miner_outflow'][0]['outflow_total']:.0f}")
                cgx = summary.get("cg") or {}
                try:
                    if cgx.get("ls_global_1h"): parts.append(f"散户多空{float(cgx['ls_global_1h'][-1].get('global_account_long_short_ratio', cgx['ls_global_1h'][-1].get('longShortRatio', 0))):.2f}")
                except Exception: pass
                try:
                    if cgx.get("ls_top_pos_1h"): parts.append(f"大户持仓比{float(cgx['ls_top_pos_1h'][-1].get('top_position_long_short_ratio', cgx['ls_top_pos_1h'][-1].get('longShortRatio', 0))):.2f}")
                except Exception: pass
                try:
                    fr = cgx.get("funding_oi_weighted")
                    if fr: parts.append(f"费率{float(fr[-1]['close']):.4f}%")
                except Exception: pass
                for label, a in (summary.get("accounts") or {}).items():
                    parts.append(f"{label}户 {a.get('mgn_ratio_pct')}% 杠杆{a.get('leverage')}x 距强平{a.get('drop_to_liq_pct')}%")
                alerts.append(" | ".join(parts)); state["brief"] = now
            except Exception as e:
                summary["errors"].append(f"brief -> {e}")
        if alerts:
            text = "\n".join(alerts)
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    data=urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode(), headers={"User-Agent": UA}), timeout=20).read()
            except Exception as e:
                summary["errors"].append(f"tg -> {e}")
        json.dump(state, open("alerts_state.json", "w"))
        summary["alerts_sent"] = alerts
except Exception as _e:
    summary['errors'].append(f'alerts block -> {_e}')

with open("summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)
print("ok", summary["ts_utc"], "heatmap=", ",".join(summary["heatmap"].keys()), "errors=", len(summary["errors"]))
for e in summary["errors"]: print("  ", e[:200])
