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
ACCTS = [("大神1", "OKX_1"), ("OKX2", "OKX_2"), ("OKX3", "OKX_3"), ("OKX4", "OKX_4")]
summary["accounts"] = {}
_priv_acct = {}
_priv_pos = {}
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
            mark_ = float(p_.get("markPx") or 0); liq_ = float(p_.get("liqPx") or 0)
            coins[c] = {"notional_pct": 0, "mode": p_.get("mgnMode"), "vs_avg_pct": round((mark_/float(p_.get("avgPx") or 1)-1)*100, 2), "liq": liq_, "mark": mark_, "avg": float(p_.get("avgPx") or 0),
                        "liq_dist_pct": round((liq_/mark_-1)*100, 2) if (mark_ and liq_) else None,
                        "iso_mgn_ratio_pct": round(float(p_.get("mgnRatio") or 0)*100, 1) if p_.get("mgnMode")=="isolated" and p_.get("mgnRatio") else None}
            _priv_pos.setdefault(label, {})[c] = {"margin": float(p_.get("margin") or 0), "notional": abs(float(p_.get("notionalUsd") or 0)), "mmr": float(p_.get("mmr") or 0), "mode": p_.get("mgnMode")}
        for c, v in coins.items(): v["notional_pct"] = round(abs(float([q for q in pos if q["instId"].split("-")[0]==c][0].get("notionalUsd") or 0)) / notional * 100, 1) if notional else 0
        # cushion: equity - maintenance margin, as % of notional = how far整体 can drop before liquidation
        cross_pos = [p_ for p_ in pos if p_.get("mgnMode") == "cross"]
        iso_margin = sum(float(p_.get("margin") or 0) for p_ in pos if p_.get("mgnMode") == "isolated")
        cross_notional = sum(abs(float(p_.get("notionalUsd") or 0)) for p_ in cross_pos)
        mm = sum(float(p_.get("mmr") or 0) for p_ in cross_pos) or (eq / mmr if mmr else 0)
        cross_eq = eq - iso_margin
        # robust cushion: if mm is tiny/odd, derive maintenance margin from OKX mgnRatio (eq/mmr) instead
        if mmr and (not mm or mm <= 0 or mm > cross_eq):
            mm = cross_eq / mmr if mmr else mm
        cushion_pct = round((cross_eq - mm) / cross_notional * 100, 2) if cross_notional else None
        if cushion_pct is not None and (cushion_pct < 0 or cushion_pct > 95): cushion_pct = None
        n_iso = len(pos) - len(cross_pos)
        _priv_acct[label] = {"eq": cross_eq, "notional": cross_notional, "mm": mm}
        summary["accounts"][label] = {"n_isolated": n_iso, "n_cross": len(cross_pos), "mgn_ratio_pct": round(mmr*100, 1), "leverage": round(notional/eq, 2) if eq else None, "drop_to_liq_pct": cushion_pct, "coins": coins, "avail_pct": round(avail/eq*100, 1) if eq else None}
    except Exception as e:
        summary["errors"].append(f"okx {label} -> {str(e)[:120]}")

# 5c) On-chain whale scan: Hyperliquid (via Coinglass) + Aave V3 (via The Graph). Builds an on-chain liquidation map.
#     Addresses are public on-chain data; only truncated addresses are published.
def _post(url, payload, headers=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"User-Agent": UA, "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))
WHALE_MIN = float(os.environ.get("WHALE_MIN_USD", "5000000"))
_px_now = {}
for _s in ("BTC", "ETH", "SOL"):
    try:
        _px_now[_s] = float(get(f"https://www.okx.com/api/v5/market/ticker?instId={_s}-USDT-SWAP")["data"][0]["last"])
    except Exception:
        h_ = summary["heatmap"].get(_s); _px_now[_s] = h_["price"] if h_ else None
_whales = []   # {src, addr, sym, side, usd, liq, entry, lev, hf}
def _f(v):
    try: return float(v)
    except Exception: return None
# --- Hyperliquid whales (Coinglass) ---
if CG:
    hl_ok = False
    for u in ("https://open-api-v4.coinglass.com/api/hyperliquid/whale-position",
              "https://open-api-v4.coinglass.com/api/hyperliquid/whale-alert"):
        try:
            r = get(u, {"CG-API-KEY": CG})
            if str(r.get("code")) != "0" or not r.get("data"):
                summary["errors"].append(f"hl {u.rsplit('/',1)[1]} -> code={r.get('code')} msg={str(r.get('msg'))[:80]}"); continue
            rows = r["data"] if isinstance(r["data"], list) else (r["data"].get("list") or [])
            if rows: summary["hl_sample_keys"] = sorted(rows[0].keys())[:40]
            for x in rows:
                sym = str(x.get("symbol") or x.get("coin") or "").upper().replace("-USD", "").replace("USDT", "")
                size = _f(x.get("position_size") or x.get("size") or x.get("szi"))
                usd = abs(_f(x.get("position_value_usd") or x.get("position_value") or x.get("positionValue") or 0) or 0)
                liq = _f(x.get("liq_price") or x.get("liquidation_price") or x.get("liquidationPx"))
                entry = _f(x.get("entry_price") or x.get("entryPx"))
                side = x.get("position_side") or x.get("side")
                if size is not None and not side: side = "long" if size > 0 else "short"
                side = "long" if str(side).lower() in ("long", "buy", "1") else "short"
                addr = str(x.get("user") or x.get("address") or "")
                if sym and usd >= WHALE_MIN and liq and liq > 0:
                    _whales.append({"src": "HL", "addr": addr, "sym": sym, "side": side, "usd": usd, "liq": liq, "entry": entry, "lev": _f(x.get("leverage")), "hf": None})
            hl_ok = True
            break
        except Exception as e:
            summary["errors"].append(f"hl {u.rsplit('/',1)[1]} -> {str(e)[:100]}")
# --- Aave V3 borrowers (The Graph) ---
GRAPH = os.environ.get("GRAPH_KEY", "").strip()
FAM = {"ETH": {"WETH", "WSTETH", "WEETH", "CBETH", "RETH", "OSETH", "ETHX", "RSETH", "EZETH", "ETH"},
       "BTC": {"WBTC", "CBBTC", "TBTC", "LBTC", "FBTC", "BTC.B"}}
STABLE = {"USDT", "USDC", "USDC.E", "USDBC", "DAI", "USDS", "GHO", "USDE", "SUSDE", "PYUSD", "FRAX", "LUSD", "CRVUSD", "RLUSD", "USDTB", "USDT0"}
def _fam(s):
    s = s.upper()
    for k, v in FAM.items():
        if s in v: return k
    return "STABLE" if s in STABLE else "OTHER"
if GRAPH:
    SUBG = [x.split(":") for x in os.environ.get("AAVE_SUBGRAPHS",
            "eth:Cd2gEDVeqnjBn1hSeqFMitw8Q1iiyV9FYUZkLNRcL87g,arb:DLuE98kEb5pQNXAcKFQGQgfSQ57Xdou4jnVbAEqMfy3B,base:GQFbb95cE6d8mV989mL5figjaGaKCQB3xqYrr1bRyXqF").split(",")]
    for chain, sid in SUBG:
        url = f"https://gateway.thegraph.com/api/{GRAPH}/subgraphs/id/{sid}"
        try:
            res = _post(url, {"query": "{ reserves(first: 100) { id symbol decimals reserveLiquidationThreshold price { priceInEth } } }"})
            if res.get("errors"): summary["errors"].append(f"aave {chain} reserves -> {str(res['errors'])[:120]}"); continue
            reserves = res["data"]["reserves"]
            usdc = [r for r in reserves if r["symbol"].upper() in ("USDC", "USDT")]
            unit = float(usdc[0]["price"]["priceInEth"]) if usdc else 1e8
            info = {r["id"]: {"sym": r["symbol"], "dec": int(r["decimals"]), "lt": int(r["reserveLiquidationThreshold"]) / 1e4,
                               "usd": float(r["price"]["priceInEth"]) / unit} for r in reserves}
            users = set()
            for rid, ri in info.items():
                if _fam(ri["sym"]) != "STABLE" and _fam(ri["sym"]) not in ("ETH", "BTC"): continue
                thr = int(WHALE_MIN * 0.5 / max(ri["usd"], 1e-9) * 10 ** ri["dec"])
                q = '{ userReserves(first: 60, orderBy: currentTotalDebt, orderDirection: desc, where: {reserve: "%s", currentTotalDebt_gt: "%d"}) { user { id } } }' % (rid, thr)
                rr = _post(url, {"query": q})
                for x in (rr.get("data") or {}).get("userReserves") or []: users.add(x["user"]["id"])
            users = list(users)[:300]
            for i in range(0, len(users), 100):
                q = '{ userReserves(first: 1000, where: {user_in: %s}) { user { id } reserve { id } currentATokenBalance currentTotalDebt usageAsCollateralEnabledOnUser } }' % json.dumps(users[i:i+100])
                rr = _post(url, {"query": q})
                pos = {}
                for x in (rr.get("data") or {}).get("userReserves") or []:
                    ri = info.get(x["reserve"]["id"]);  u_ = x["user"]["id"]
                    if not ri: continue
                    p = pos.setdefault(u_, {"coll": {}, "debt": {}})
                    f = _fam(ri["sym"])
                    cv = int(x["currentATokenBalance"]) / 10 ** ri["dec"] * ri["usd"] if x.get("usageAsCollateralEnabledOnUser") else 0
                    dv = int(x["currentTotalDebt"]) / 10 ** ri["dec"] * ri["usd"]
                    if cv: p["coll"].setdefault(f, [0, 0]); p["coll"][f][0] += cv; p["coll"][f][1] += cv * ri["lt"]
                    if dv: p["debt"][f] = p["debt"].get(f, 0) + dv
                for u_, p in pos.items():
                    debt = sum(p["debt"].values()); collv = sum(v[0] for v in p["coll"].values())
                    if debt < WHALE_MIN or not collv: continue
                    adj = sum(v[1] for v in p["coll"].values()); hf = adj / debt
                    vol_c = {k: v for k, v in p["coll"].items() if k in ("ETH", "BTC")}
                    other = p["coll"].get("OTHER", [0, 0])[0]
                    if other > 0.1 * collv: continue
                    st_adj = p["coll"].get("STABLE", [0, 0])[1]; st_debt = p["debt"].get("STABLE", 0)
                    if len(vol_c) == 1 and st_debt >= 0.9 * debt:
                        fam = list(vol_c)[0]; cadj = vol_c[fam][1]
                        ratio = (st_debt - st_adj) / cadj if cadj else None
                        if ratio and 0 < ratio < 1.5 and _px_now.get(fam):
                            _whales.append({"src": f"Aave-{chain}", "addr": u_, "sym": fam, "side": "long", "usd": vol_c[fam][0],
                                            "liq": _px_now[fam] * ratio, "entry": None, "lev": round(collv / (collv - debt), 1) if collv > debt else None, "hf": round(hf, 3)})
                    elif st_adj and len(p["debt"]) == 1 and list(p["debt"])[0] in ("ETH", "BTC"):
                        fam = list(p["debt"])[0]; ratio = st_adj / debt
                        if 1 < ratio < 3 and _px_now.get(fam):
                            _whales.append({"src": f"Aave-{chain}", "addr": u_, "sym": fam, "side": "short", "usd": debt,
                                            "liq": _px_now[fam] * ratio, "entry": None, "lev": None, "hf": round(hf, 3)})
        except Exception as e:
            summary["errors"].append(f"aave {chain} -> {str(e)[:120]}")
# --- build map ---
try:
    oc = {}
    for sym in ("BTC", "ETH", "SOL"):
        px = _px_now.get(sym)
        ws = [w for w in _whales if w["sym"] == sym]
        if not px or not ws: continue
        for w in ws: w["dist_pct"] = round((w["liq"] / px - 1) * 100, 2)
        step = px * 0.005; buckets = {}
        for w in ws:
            if abs(w["dist_pct"]) > 15: continue
            k = round(w["liq"] / step) * step
            b = buckets.setdefault(k, {"price": k, "usd": 0, "n": 0, "long": 0, "short": 0})
            b["usd"] += w["usd"]; b["n"] += 1; b["long" if w["side"] == "long" else "short"] += w["usd"]
        bl = sorted(buckets.values(), key=lambda b: -b["usd"])
        for b in bl: b["dist_pct"] = round((b["price"] / px - 1) * 100, 2)
        top = sorted(ws, key=lambda w: -w["usd"])[:15]
        oc[sym] = {"price": px, "n_whales": len(ws), "total_usd": round(sum(w["usd"] for w in ws)),
                   "below": [b for b in bl if b["price"] < px][:6], "above": [b for b in bl if b["price"] >= px][:6],
                   "top": [{**{k: w[k] for k in ("src", "side", "usd", "liq", "lev", "hf", "dist_pct")}, "addr": (w["addr"][:6] + "…" + w["addr"][-4:]) if len(w["addr"]) > 12 else w["addr"]} for w in top]}
    summary["onchain_liq"] = oc
except Exception as e:
    summary["errors"].append(f"onchain map -> {str(e)[:120]}")

# 5d) Whale/liquidation news relay (余烬 / Lookonchain / Onchain Lens via Chinese newsflash feeds)
_news = []
import re as _re, hashlib as _hl
NEWS_KW = _re.compile(r"余烬|Ember|Lookonchain|Onchain ?Lens|链上(数据|分析)|巨鲸.{0,20}(清算|爆仓|强平|补仓|抵押|借出|转入|抛售)")
try:
    r = get("https://api.theblockbeats.news/v1/open-api/open-flash?size=50&page=1&type=push&lang=cn")
    for x in ((r.get("data") or {}).get("data") or []):
        t = (x.get("title") or "") + " " + _re.sub("<[^>]+>", "", x.get("content") or "")
        if NEWS_KW.search(t): _news.append({"src": "律动", "title": x.get("title") or "", "body": _re.sub("<[^>]+>", "", x.get("content") or "")[:220], "link": x.get("link") or x.get("url") or ""})
except Exception as e:
    summary["errors"].append(f"news blockbeats -> {str(e)[:100]}")
try:
    req = urllib.request.Request("https://rss.odaily.news/rss/newsflash", headers={"User-Agent": UA})
    xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    for item in _re.findall(r"<item>(.*?)</item>", xml, _re.S)[:50]:
        g = lambda tag: (_re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, _re.S) or [None, ""])[1]
        title, desc, link = g("title"), _re.sub("<[^>]+>", "", g("description")), g("link")
        if NEWS_KW.search(title + " " + desc): _news.append({"src": "Odaily", "title": title, "body": desc[:220], "link": link})
except Exception as e:
    summary["errors"].append(f"news odaily -> {str(e)[:100]}")
for n in _news: n["id"] = _hl.md5((n["title"][:40]).encode()).hexdigest()[:12]
summary["whale_news"] = _news[:10]

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
        # account thresholds + margin-add advice (amounts only go to TG, never published)
        for label, a in (summary.get("accounts") or {}).items():
            r = a.get("mgn_ratio_pct"); d = a.get("drop_to_liq_pct")
            pv = _priv_acct.get(label) or {}
            def need_to(target_pct):
                # equity needed so that (eq - mm)/notional = target
                n = pv.get("notional") or 0; mm_ = pv.get("mm") or 0; eq_ = pv.get("eq") or 0
                return max(0.0, target_pct/100*n + mm_ - eq_)
            if d is not None:
                if d < 10:
                    fire(f"acct_{label}_d10", f"🚨 {label} 距强平仅 {d}%：立即加保证金 ≈{need_to(20):,.0f} USDT（回到 20% 余量），或减仓 ≈{max(0,(1-(d/20)))*100:.0f}%", 1)
                elif d < 15:
                    fire(f"acct_{label}_d15", f"⚠️ {label} 距强平 {d}%：建议准备保证金 ≈{need_to(20):,.0f} USDT，跌破下方支撑带前加入", 3)
            # pre-warn: if the strongest support band of the largest position is broken, how much cushion remains?
            try:
                big = max((a.get("coins") or {}).items(), key=lambda kv: kv[1].get("notional_pct", 0))
                sym = big[0]; h = summary["heatmap"].get(sym)
                if h and h["below"] and d is not None:
                    b = max(h["below"], key=lambda x: x["strength"]); drop = -b["dist_pct"]  # % drop to that band
                    # approx: altcoin basket falls ~1.5x the band drop for the whole account
                    after = d - drop*1.5
                    if after < 12 and 0 < drop <= 3:
                        fire(f"acct_{label}_pre_{sym}", f"🧭 {label}：{sym} 支撑带 {fp(b['price'])} 若跌破（−{drop:.1f}%），整体余量会从 {d}% 缩到 ≈{after:.0f}%，提前备好 ≈{need_to(20):,.0f} USDT", 6)
            except Exception: pass
            # isolated positions: each has its own liquidation
            for c, v in (a.get("coins") or {}).items():
                if v.get("mode") == "isolated" and v.get("liq_dist_pct") is not None:
                    dist = -v["liq_dist_pct"]  # positive % drop to liq for longs
                    pp = (_priv_pos.get(label) or {}).get(c) or {}
                    # margin needed so liq distance becomes 20%: approx add = notional*(0.20 - dist/100)
                    add = max(0.0, pp.get("notional", 0) * (0.20 - dist/100))
                    if 0 < dist < 8:
                        fire(f"iso_{label}_{c}_8", f"🚨 {label} {c}[逐仓] 距强平仅 {dist:.1f}%（强平价 {fp(v['liq'])}）：立即给该仓加 ≈{add:,.0f} USDT 或减仓", 1)
                    elif 0 < dist < 12:
                        fire(f"iso_{label}_{c}_12", f"⚠️ {label} {c}[逐仓] 距强平 {dist:.1f}%（强平价 {fp(v['liq'])}），建议给该仓加 ≈{add:,.0f} USDT", 3)
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
        # on-chain whale map alerts + news relay
        try:
            for sym, o in (summary.get("onchain_liq") or {}).items():
                for b in (o.get("below", []) + o.get("above", [])):
                    if b["usd"] >= 5e7 and abs(b["dist_pct"]) <= 5:
                        fire(f"oc_{sym}_{round(b['price'])}", f"🐋 链上强平堆 {sym} {fp(b['price'])}（{b['dist_pct']:+.1f}%）≈{b['usd']/1e8:.2f}亿美元 / {b['n']}个大户{'多单' if b['long']>=b['short'] else '空单'}——明牌目标，易被打", 6)
            prev = state.get("whales", {}); cur = {}
            for w in _whales:
                if w["usd"] < 3e7: continue
                wid = f"{w['src']}|{w['addr']}|{w['sym']}|{w['side']}"; cur[wid] = [round(w["liq"], 2), round(w["usd"])]
                tag = f"{w['sym']} {'多' if w['side']=='long' else '空'} {w['usd']/1e8:.2f}亿（{w['src']} {w['addr'][:6]}…）"
                if prev and wid not in prev:
                    fire(f"wnew_{wid}", f"🐋 新巨鲸 {tag} 强平 {fp(w['liq'])}（{w.get('dist_pct', 0):+.1f}%）", 12)
                elif wid in prev and prev[wid][0] and abs(w["liq"]/prev[wid][0]-1) >= 0.03:
                    fire(f"wmove_{wid}", f"🐋 巨鲸调仓 {tag} 强平 {fp(prev[wid][0])}→{fp(w['liq'])}（{'补保证金/减仓' if (w['liq']<prev[wid][0])==(w['side']=='long') else '加仓/提走保证金'}）", 6)
            for wid, (lq, usd) in prev.items():
                if wid not in cur and usd >= 3e7 and _whales:
                    fire(f"wgone_{wid}", f"🐋 巨鲸消失 {wid.split('|')[2]} {'多' if wid.split('|')[3]=='long' else '空'} {usd/1e8:.2f}亿（原强平 {fp(lq)}）= 平仓或被清算", 12)
            if _whales: state["whales"] = cur
            seen = state.get("news_seen")
            if seen is None:
                state["news_seen"] = [n["id"] for n in _news]
            else:
                sent = 0
                for n in _news:
                    if n["id"] in seen or sent >= 4: continue
                    alerts.append(f"📰 {n['src']}｜{n['title']}\n{n['body']}" + (f"\n{n['link']}" if n['link'] else "")); seen.append(n["id"]); sent += 1
                state["news_seen"] = seen[-300:]
        except Exception as e: summary["errors"].append(f"whale alerts -> {str(e)[:120]}")
        # periodic brief every 30 min (multi-line)
        force_brief = os.environ.get("FORCE_BRIEF") == "1"
        if force_brief or now - state.get("brief", 0) >= 29 * 60:
            try:
                L = [f"📊 {dt.datetime.utcnow().strftime('%H:%M')}Z", "━━━━━━━━━━"]
                for sym, h in summary["heatmap"].items():
                    b = max(h["below"], key=lambda x: x["strength"]) if h["below"] else None
                    a = max(h["above"], key=lambda x: x["strength"]) if h["above"] else None
                    L.append(f"{sym} {fp(h['price'])}")
                    L.append("  " + (f"支撑 {fp(b['price'])} ({b['dist_pct']:+.1f}%)" if b else "支撑 —") + "  " + (f"起飞 {fp(a['price'])} ({a['dist_pct']:+.1f}%)" if a else "起飞 —"))
                L.append("━━━━━━━━━━")
                cb = summary.get("coinbase") or {}
                if cb.get("buy_ratio") is not None: L.append(f"CB 吃单买 {cb['buy_ratio']}%  溢价 {cb.get('premium_pct')}%")
                if summary.get("etf"):
                    e = summary["etf"][-1]; L.append(f"ETF {dt.datetime.utcfromtimestamp(e['timestamp']/1000).strftime('%m-%d')}: {e.get('flow_usd',0)/1e6:+.0f}M")
                row = []
                if cq.get("exch_netflow_day"):
                    r0 = cq["exch_netflow_day"][0]; row.append(f"链上 {r0['date'][5:]} 净流入 {r0['netflow_total']:+,.0f}")
                if cq.get("miner_outflow"): row.append(f"矿工 {cq['miner_outflow'][0]['outflow_total']:,.0f}")
                if row: L.append(" | ".join(row))
                row = []
                cgx = summary.get("cg") or {}
                try:
                    if cgx.get("ls_global_1h"): row.append(f"散户多空 {float(cgx['ls_global_1h'][-1].get('global_account_long_short_ratio', cgx['ls_global_1h'][-1].get('longShortRatio', 0))):.2f}")
                except Exception: pass
                try:
                    if cgx.get("ls_top_pos_1h"): row.append(f"大户持仓比 {float(cgx['ls_top_pos_1h'][-1].get('top_position_long_short_ratio', cgx['ls_top_pos_1h'][-1].get('longShortRatio', 0))):.2f}")
                except Exception: pass
                try:
                    fr = cgx.get("funding_oi_weighted")
                    if fr: row.append(f"费率 {float(fr[-1]['close']):.4f}%")
                except Exception: pass
                if row: L.append(" | ".join(row))
                try:
                    ocl = []
                    for sym, o in (summary.get("onchain_liq") or {}).items():
                        bs = sorted([b for b in o.get("below", []) + o.get("above", []) if abs(b["dist_pct"]) <= 10], key=lambda b: -b["usd"])[:2]
                        if bs: ocl.append(f"{sym} " + " · ".join(f"{fp(b['price'])} {b['usd']/1e8:.1f}亿({b['dist_pct']:+.1f}%)" for b in bs))
                    if ocl: L.append("链上强平: " + " | ".join(ocl))
                except Exception: pass
                accts = summary.get("accounts") or {}
                if accts:
                    L.append("━━━━━━━━━━")
                    for label, a in accts.items():
                        cross_coins = [(c, v) for c, v in (a.get("coins") or {}).items() if v.get("mode") == "cross" and v.get("liq_dist_pct") is not None]
                        cr_txt = f"全仓距强平 {a.get('drop_to_liq_pct')}%" if a.get('drop_to_liq_pct') is not None else ("全仓 " + " ".join(f"{c} {-v['liq_dist_pct']:.0f}%" for c, v in cross_coins) if cross_coins else "")
                        L.append(f"{label}  {a.get('mgn_ratio_pct')}%  杠杆 {a.get('leverage')}x  {cr_txt}" + (f"  逐仓{a.get('n_isolated')}个" if a.get('n_isolated') else ""))
                        iso = [(c, v) for c, v in (a.get("coins") or {}).items() if v.get("mode") == "isolated" and v.get("liq_dist_pct") is not None]
                        if iso:
                            iso.sort(key=lambda kv: -kv[1]["liq_dist_pct"])
                            L.append("  逐仓距强平: " + "  ".join(f"{c} {-v['liq_dist_pct']:.0f}%" for c, v in iso))
                            tips = []
                            for c, v in iso:
                                dist = -v["liq_dist_pct"]; pp = (_priv_pos.get(label) or {}).get(c) or {}
                                if dist < 20:
                                    add = max(0.0, pp.get("notional", 0) * (0.20 - dist/100))
                                    tag = "🚨" if dist < 10 else ("⚠️" if dist < 15 else "·")
                                    tips.append(f"{tag} {c} 加≈{add:,.0f}U→20%（强平{fp(v['liq'])}）")
                            if tips: L.append("  建议: " + "  ".join(tips))
                            # reduce suggestions: only when far from liq, support band not within 3%, and not within 24h before a macro event
                            macro_soon = any(0 < (dt.datetime.strptime(w, "%Y-%m-%d %H:%M") - dt.datetime.utcnow()).total_seconds() <= 24*3600 for w, _ in MACRO)
                            cuts = []
                            if not macro_soon:
                                for c, v in iso:
                                    dist = -v["liq_dist_pct"]; pp = (_priv_pos.get(label) or {}).get(c) or {}
                                    h = summary["heatmap"].get(c); near = False
                                    if h and h["below"]:
                                        b = max(h["below"], key=lambda x: x["strength"]); near = (-b["dist_pct"]) <= 3.0 and b["strength"] >= 0.6
                                    if dist > 30 and not near:
                                        cut = max(0.0, pp.get("notional", 0) * (dist/100 - 0.25))
                                        if cut >= 300: cuts.append(f"{c} 可减≈{cut:,.0f}U→25%")
                            if cuts: L.append("  可挪: " + "  ".join(cuts) + "（挪给薄的仓，不要提走）")
                            elif macro_soon: L.append("  可挪: 数据日前24h不建议减保证金")
                        cr = a.get("drop_to_liq_pct")
                        if cr is not None and cr < 20:
                            pv = _priv_acct.get(label) or {}
                            addc = max(0.0, 0.20*pv.get("notional",0) + pv.get("mm",0) - pv.get("eq",0))
                            L.append(f"  全仓建议: 加≈{addc:,.0f}U→20%")
                alerts.append("\n".join(L)); state["brief"] = now
            except Exception as e:
                summary["errors"].append(f"brief -> {e}")
        if alerts:
            short = [x for x in alerts if not x.startswith("📊")]; briefs = [x for x in alerts if x.startswith("📊")]
            msgs = ([ "\n".join(short) ] if short else []) + briefs
            for text in msgs:
                for cid in [c.strip() for c in TG_CHAT.split(",") if c.strip()]:
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                            data=urllib.parse.urlencode({"chat_id": cid, "text": text}).encode(), headers={"User-Agent": UA}), timeout=20).read()
                    except Exception as e:
                        summary["errors"].append(f"tg {cid[:4]}.. -> {e}")
        json.dump(state, open("alerts_state.json", "w"))
        summary["alerts_sent"] = alerts
except Exception as _e:
    summary['errors'].append(f'alerts block -> {_e}')

with open("summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)
print("ok", summary["ts_utc"], "heatmap=", ",".join(summary["heatmap"].keys()), "errors=", len(summary["errors"]))
for e in summary["errors"]: print("  ", e[:200])
