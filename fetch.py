#!/usr/bin/env python3
"""btc-data collector for GitHub Actions. Keys come from env (repo Secrets).
Writes summary.json (same schema as upload.ps1) into the repo root."""
import os, json, time, datetime as dt, urllib.request, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
CG = os.environ.get("COINGLASS_KEY", "").strip()
CQ = os.environ.get("CRYPTOQUANT_KEY", "").strip()
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC,ETH,SOL").split(",") if s.strip()]

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
                return [{"price": round(p, 2), "dist_pct": round((p / px - 1) * 100, 2), "strength": round(v / maxv, 2)} for p, v in rs]
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
        ("liq_agg_1h",     [f"{B}/api/futures/liquidation/aggregated-history?symbol=BTC&interval=1h&limit=24",
                            f"{B}/api/futures/liquidation/aggregated-history?symbol=BTC&exchange_list=ALL&interval=1h&limit=24"]),
        ("liq_by_exch_24h",[f"{B}/api/futures/liquidation/exchange-list?symbol=BTC&range=24h",
                            f"{B}/api/futures/liquidation/exchange-list?symbol=BTC&range=1d"]),
        ("liq_map_agg",    [f"{B}/api/futures/liquidation/aggregated-map?symbol=BTC&range=1d",
                            f"{B}/api/futures/liquidation/aggregated-map?symbol=BTC&range=24h"]),
        ("orderbook_hist", [f"{B}/api/spot/orderbook/history?exchange=Binance&symbol=BTCUSDT&interval=1h&limit=12",
                            f"{B}/api/spot/orderbook/history?exchange=Coinbase&symbol=BTC-USD&interval=1h&limit=12"]),
        ("opt_max_pain",   [f"{B}/api/option/max-pain?symbol=BTC&exchange=Deribit",
                            f"{B}/api/option/max-pain?symbol=BTC"]),
        ("opt_info",       [f"{B}/api/option/info?symbol=BTC"]),
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
            for side, rows in (("下", h["below"]), ("上", h["above"])):
                for b in rows[:2]:
                    if b["strength"] >= 0.8 and abs(b["dist_pct"]) <= 0.6:
                        fire(f"hm_{sym}_{side}_{int(b['price']//(50 if sym=='BTC' else 1))}",
                             f"🔥 {sym} {h['price']:.0f} 距{side}方厚带 {b['price']:.0f} 仅 {b['dist_pct']:+.2f}%（强度{b['strength']}）", 3)
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
        # BTC 15m move via Deribit
        try:
            r = get("https://www.deribit.com/api/v2/public/get_tradingview_chart_data?instrument_name=BTC-PERPETUAL&resolution=15&start_timestamp=%d&end_timestamp=%d" % (int((now-1800)*1000), int(now*1000)))["result"]
            if r.get("close") and len(r["close"]) >= 2:
                o, c = r["open"][-2], r["close"][-2]; chg = (c/o-1)*100
                if abs(chg) >= 1.0: fire(f"mv_{int(now//900)}", f"⚡ BTC 15m {chg:+.2f}% → {c:.0f}（量{r['volume'][-2]:.0f}）", 0)
        except Exception as e:
            summary["errors"].append(f"deribit -> {e}")
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
