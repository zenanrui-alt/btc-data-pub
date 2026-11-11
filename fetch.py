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

_LIQMAP_RAW = None
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
                    if key == "liq_map_agg": _LIQMAP_RAW = d
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
ACCTS = [("大神1", "OKX_1"), ("大神2", "OKX_2"), ("大神3", "OKX_3"), ("OKX4", "OKX_4")]
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
_aave_scan = GRAPH and (dt.datetime.utcnow().minute < 15 or os.environ.get("FORCE_BRIEF") == "1")
_aave_cache = []
if GRAPH and not _aave_scan:
    try:
        _aave_cache = (json.load(open("alerts_state.json")) if os.path.exists("alerts_state.json") else {}).get("aave_cache", [])
        _whales.extend(_aave_cache)
    except Exception: pass
if _aave_scan:
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
if _aave_scan: _aave_cache = [w for w in _whales if w["src"].startswith("Aave")]
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

# 5c2) High-leverage fuel within ±5%: exchange liquidation map (Coinglass) + on-chain whales near liquidation
try:
    def _find_map(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "liqMapV2" and isinstance(v, dict): return v
                r_ = _find_map(v)
                if r_: return r_
        elif isinstance(o, list):
            for v in o:
                r_ = _find_map(v)
                if r_: return r_
        return None
    pxb = _px_now.get("BTC")
    fuel = {"ex_long": 0.0, "ex_short": 0.0, "oc_long": 0.0, "oc_short": 0.0}
    lm = _find_map(_LIQMAP_RAW) if _LIQMAP_RAW else None
    if lm and pxb:
        for k, rows in lm.items():
            for row in rows:
                pr, usd = float(row[0]), float(row[1] or 0)
                dpc = (pr / pxb - 1) * 100
                if -5 <= dpc < 0: fuel["ex_long"] += usd
                elif 0 <= dpc <= 5: fuel["ex_short"] += usd
    for w in _whales:
        if w["sym"] == "BTC" and pxb:
            dpc = (w["liq"] / pxb - 1) * 100
            if w["side"] == "long" and -5 <= dpc < 0: fuel["oc_long"] += w["usd"]
            elif w["side"] == "short" and 0 <= dpc <= 5: fuel["oc_short"] += w["usd"]
    summary["fuel5"] = {k: round(v) for k, v in fuel.items()}; summary["fuel5"]["has_exmap"] = bool(lm)
    try:
        summary["okx_top_pos"] = float(get("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader?instId=BTC-USDT-SWAP&period=1H")["data"][0][1])
    except Exception: pass
    ent = {}
    for side in ("long", "short"):
        ws = [w for w in _whales if w["sym"] == "BTC" and w["side"] == side and w.get("entry") and w["usd"] >= 2e7]
        if ws:
            tot = sum(w["usd"] for w in ws); ent[side] = {"avg": round(sum(w["entry"] * w["usd"] for w in ws) / tot), "usd": round(tot), "n": len(ws)}
    summary["whale_entry"] = ent
except Exception as e:
    summary["errors"].append(f"fuel5 -> {str(e)[:100]}")
# 5d) Whale/liquidation news relay (余烬 / Lookonchain / Onchain Lens via Chinese newsflash feeds)
_news = []
import re as _re, hashlib as _hl, html as _html
_clean = lambda t: _re.sub(r"\s+", " ", _re.sub("<[^>]+>", "", _html.unescape(_html.unescape(t or "")))).strip()
NEWS_KW = _re.compile(r"余烬|Ember|Lookonchain|Onchain ?Lens|链上(数据|分析)|巨鲸.{0,20}(清算|爆仓|强平|补仓|抵押|借出|转入|抛售)")
try:
    r = None
    for _u in ("https://api.theblockbeats.info/v1/open-api/open-flash?size=50&page=1&type=push", "https://api.theblockbeats.news/v1/open-api/open-flash?size=50&page=1&type=push"):
        try: r = get(_u); break
        except Exception: r = None
    if r is None: raise Exception("blockbeats unreachable")
    for x in ((r.get("data") or {}).get("data") or []):
        t = _clean(x.get("title")) + " " + _clean(x.get("content"))
        if NEWS_KW.search(t): _news.append({"src": "律动", "title": _clean(x.get("title")), "body": _clean(x.get("content"))[:220], "link": x.get("link") or x.get("url") or ""})
except Exception as e:
    pass  # blockbeats optional
try:
    req = urllib.request.Request("https://rss.odaily.news/rss/newsflash", headers={"User-Agent": UA})
    xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    for item in _re.findall(r"<item>(.*?)</item>", xml, _re.S)[:50]:
        g = lambda tag: (_re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, _re.S) or [None, ""])[1]
        title, desc, link = _clean(g("title")), _clean(g("description")), g("link")
        if NEWS_KW.search(title + " " + desc): _news.append({"src": "Odaily", "title": title, "body": desc[:220], "link": link})
except Exception as e:
    summary["errors"].append(f"news odaily -> {str(e)[:100]}")
_news = [n for n in _news if not _re.search(r"HYPE|Hyperliquid 代币", n["title"]) and (_re.search(r"\d+(\.\d+)?\s*亿|\d{4,}(\.\d+)?\s*万|余烬", n["title"] + n["body"]))]
for n in _news: n["id"] = _hl.md5((n["title"][:40]).encode()).hexdigest()[:12]
summary["whale_news"] = _news[:10]

# 5e) Moving averages, reversal checklist, Jin10 flash, CPI auto-readout
def okx_candles(inst, bar, pages=3):
    out = []; after = ""
    for _ in range(pages):
        d = get(f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar={bar}&limit=100" + (f"&after={after}" if after else ""))["data"]
        if not d: break
        out += d; after = d[-1][0]
    return out[::-1]
def _ema(vals, n):
    k = 2 / (n + 1); e = sum(vals[:n]) / n
    for v in vals[n:]: e = v * k + e * (1 - k)
    return e
try:
    dc = [float(x[4]) for x in okx_candles("BTC-USDT", "1Dutc", 10)]
    wc = [float(x[4]) for x in okx_candles("BTC-USDT", "1Wutc", 5)]
    summary["ma"] = {"d_ema200": round(_ema(dc, 200)) if len(dc) >= 250 else None, "d_sma200": round(sum(dc[-200:]) / 200) if len(dc) >= 200 else None,
                     "d_sma50": round(sum(dc[-50:]) / 50) if len(dc) >= 50 else None, "d_sma365": round(sum(dc[-365:]) / 365) if len(dc) >= 365 else None, "w_ema200": round(_ema(wc, 200)) if len(wc) >= 220 else None, "n_d": len(dc), "n_w": len(wc)}
except Exception as e:
    summary["errors"].append(f"ma -> {str(e)[:100]}")
REV_OI_BASE = float(os.environ.get("REV_OI_BASE", "38.3e8"))   # OKX BTC OI (USD) before the 11/08 knife
try:
    oi_now = float(get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=5m")["data"][0][1])
    ls_now = float(get("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=5m")["data"][0][1])
    c15 = get("https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=15m&limit=8")["data"]
    wick = None
    for c in c15[1:5]:
        o_, h_, l_, cl_, v_ = map(float, c[1:6]); body = abs(cl_ - o_); lw = min(o_, cl_) - l_
        if v_ >= 1_000_000 and lw >= 2 * max(body, 1) and (h_ > l_ and lw / (h_ - l_) >= 0.5):
            wick = {"ts": dt.datetime.utcfromtimestamp(int(c[0]) / 1000).strftime("%H:%M"), "low": l_, "vol": v_}; break
    tk = get("https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=SPOT&period=5m")["data"][:3]
    tb = sum(float(x[2]) for x in tk); ts_ = sum(float(x[1]) for x in tk); spot_buy = tb / (tb + ts_) * 100 if (tb + ts_) else None
    conds = {"oi": oi_now <= REV_OI_BASE - 2e8, "ls": ls_now < 2.3, "wick": bool(wick) and (spot_buy or 0) >= 60}
    summary["reversal"] = {"oi_yi": round(oi_now / 1e8, 2), "oi_target_yi": round((REV_OI_BASE - 2e8) / 1e8, 2), "retail_ls": ls_now, "wick": wick,
                           "spot_buy_15m": round(spot_buy, 1) if spot_buy is not None else None, "conds": conds, "n_ok": sum(conds.values())}
except Exception as e:
    summary["errors"].append(f"reversal -> {str(e)[:100]}")
# Jin10 flash (unofficial web endpoint; may change)
JIN_KW = _re.compile(r"CPI|PPI|非农|就业|失业|美联储|鲍威尔|FOMC|降息|加息|利率|伊朗|以色列|霍尔木兹|特朗普|关税|比特币|BTC|加密|ETF|MSCI|微策略|Strategy|贝森特|美债|停火|开战|袭击|制裁")
try:
    r = get("https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1",
            {"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0", "Referer": "https://www.jin10.com/", "Origin": "https://www.jin10.com"})
    jin = []
    for x in (r.get("data") or [])[:80]:
        dd = x.get("data") or {}
        txt = _clean(dd.get("content") or dd.get("title") or "")
        if not txt and dd.get("name"):
            txt = f"{dd.get('name')} 公布 {dd.get('actual')} 预期 {dd.get('consensus')} 前值 {dd.get('previous')}"
        imp = int(x.get("important") or 0)
        if txt and JIN_KW.search(txt) and (imp == 1 or x.get("type") == 1):
            jin.append({"id": str(x.get("id")), "time": x.get("time"), "text": txt[:260], "imp": imp})
    summary["jin10"] = jin[:15]
except Exception as e:
    summary["jin10"] = []; summary["errors"].append(f"jin10 -> {str(e)[:100]}")
# Stablecoin supply (DefiLlama public API): USDT and all stablecoins, 7/30/60-day change
try:
    sc = {}
    for nm, u in (("USDT", "https://stablecoins.llama.fi/stablecoincharts/all?stablecoin=1"), ("ALL", "https://stablecoins.llama.fi/stablecoincharts/all")):
        arr = get(u)
        vals = [(int(x["date"]), float((x.get("totalCirculatingUSD") or x.get("totalCirculating") or {}).get("peggedUSD") or 0)) for x in arr]
        vals = [v for v in vals if v[1] > 0]
        if len(vals) > 61:
            now_v = vals[-1][1]
            sc[nm] = {"now_b": round(now_v / 1e9, 2), "chg7_b": round((now_v - vals[-8][1]) / 1e9, 2), "chg30_b": round((now_v - vals[-31][1]) / 1e9, 2), "chg60_b": round((now_v - vals[-61][1]) / 1e9, 2)}
    summary["stablecoins"] = sc
except Exception as e:
    summary["errors"].append(f"stablecoins -> {str(e)[:100]}")
# CPI auto-readout (BLS official API) — polls inside the release window
CPI_DAYS = {"2026-11-10": {"head": 0.3, "core": 0.3, "period": "October"}}   # consensus MoM (RBC 0.3/0.3; Cleveland nowcast 0.24/0.21)
_cpi_msg = None
def _bls():
    r = _post("https://api.bls.gov/publicAPI/v2/timeseries/data/", {"seriesid": ["CUSR0000SA0", "CUSR0000SA0L1E"], "startyear": "2026", "endyear": "2026"})
    out = {}
    for s in r["Results"]["series"]:
        d = [x for x in s["data"] if x.get("value") not in (None, "-", "")]
        d.sort(key=lambda x: (x["year"], x["period"]), reverse=True)
        out[s["seriesID"]] = (d[0]["periodName"], (float(d[0]["value"]) / float(d[1]["value"]) - 1) * 100)
    return out
def _yahoo_chg(sym, since_ts):
    try:
        r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1d&interval=1m")["chart"]["result"][0]
        t = r["timestamp"]; c = r["indicators"]["quote"][0]["close"]
        base = [cc for tt, cc in zip(t, c) if tt <= since_ts and cc is not None]; last = [cc for cc in c if cc is not None]
        return (last[-1], (last[-1] / base[-1] - 1) * 100) if base and last else None
    except Exception:
        return None
try:
    _utc = dt.datetime.utcnow(); _day = _utc.strftime("%Y-%m-%d")
    if _day in CPI_DAYS:
        rel = _utc.replace(hour=13, minute=30, second=0, microsecond=0); exp = CPI_DAYS[_day]
        if rel - dt.timedelta(minutes=20) <= _utc <= rel + dt.timedelta(minutes=75):
            got = None; tries = 0
            while tries < 18:
                if dt.datetime.utcnow() >= rel:
                    tries += 1
                    try:
                        b = _bls()
                        if b.get("CUSR0000SA0L1E", ("",))[0] == exp["period"]: got = b; break
                    except Exception as e:
                        summary["errors"].append(f"bls -> {str(e)[:80]}")
                if dt.datetime.utcnow() > rel + dt.timedelta(minutes=75): break
                time.sleep(40 if dt.datetime.utcnow() >= rel else 20)
            if got:
                head = got["CUSR0000SA0"][1]; core = got["CUSR0000SA0L1E"][1]; cr = round(core, 1)
                verdict = ("高于预期 → 偏鹰：留意 73.2k(SMA200) / 72k 链上强平堆" if cr >= exp["core"] + 0.1 else
                           ("低于预期 → 偏鸽：看能否站上 75.4k(EMA200)，再看 76.7k 起飞带" if cr <= exp["core"] - 0.1 else "符合预期 → 方向交给资金面，区间 73.2k–76.7k"))
                rts = int(rel.timestamp()) - 60; lines = [f"🔴 美国{exp['period']} CPI 已公布（BLS）",
                         f"核心 CPI 环比 {core:.2f}% ≈ {cr:.1f}%（预期 {exp['core']}%）| 整体 {head:.2f}% ≈ {round(head,1):.1f}%（预期 {exp['head']}%）", verdict]
                try:
                    k = get("https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1m&limit=60")["data"]
                    base = [float(c[1]) for c in k if int(c[0]) // 1000 <= rts + 60]; now_ = float(k[0][4])
                    if base: lines.append(f"BTC {now_:,.0f}（公布后 {(now_/base[0]-1)*100:+.2f}%）")
                except Exception: pass
                mk = []
                for nm, sy in (("纳指期货", "NQ=F"), ("美元指数", "DX-Y.NYB"), ("10年美债", "^TNX"), ("黄金", "GC=F"), ("原油", "CL=F")):
                    v = _yahoo_chg(sy, rts)
                    if v: mk.append(f"{nm} {v[1]:+.2f}%")
                if mk: lines.append(" · ".join(mk))
                _cpi_msg = "\n".join(lines); summary["cpi"] = {"head_mom": round(head, 3), "core_mom": round(core, 3), "verdict": verdict}
except Exception as e:
    summary["errors"].append(f"cpi -> {str(e)[:100]}")

# 5f) Cross-asset panel (Yahoo), Deribit options (DVOL, 10% OTM skew, big trades), alt-coin positioning (OKX), Fear & Greed
def _yh(sym):
    r = None
    for host in ("query1", "query2"):
        try:
            r = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=2d&interval=5m")["chart"]["result"][0]; break
        except Exception:
            time.sleep(1)
    if r is None: raise Exception("yahoo unavailable")
    t = r["timestamp"]; c = r["indicators"]["quote"][0]["close"]
    pts = [(tt, cc) for tt, cc in zip(t, c) if cc is not None]
    last_t, last = pts[-1]
    prev = r["meta"].get("chartPreviousClose") or r["meta"].get("previousClose")
    h1 = [cc for tt, cc in pts if tt <= last_t - 3600]
    day = [cc for tt, cc in pts if tt <= last_t - 86400]
    base_d = day[-1] if day else prev
    return {"last": round(last, 3), "chg1h": round((last / h1[-1] - 1) * 100, 2) if h1 else None, "chg24h": round((last / base_d - 1) * 100, 2) if base_d else None}
try:
    mk = {}
    try:
        _st = json.load(open("alerts_state.json")) if os.path.exists("alerts_state.json") else {}
    except Exception:
        _st = {}
    _hist = _st.get("mk_hist", {})
    def _stooq(sym):
        req = urllib.request.Request(f"https://stooq.com/q/d/l/?s={sym}&i=d", headers={"User-Agent": UA})
        rows = [r.split(",") for r in urllib.request.urlopen(req, timeout=20).read().decode().strip().splitlines()[1:] if r.count(",") >= 4]
        return float(rows[-1][4]), float(rows[-2][4])
    srcs = (("NQ", "okx", "US100-USDT-SWAP"), ("NVDA", "okx", "NVDA-USDT-SWAP"), ("GOLD", "okx", "XAU-USDT-SWAP"), ("OIL", "okx", "CL-USDT-SWAP"), ("DXY", "stooq", "dx.f"), ("US10Y", "stooq", "10usy.b"))
    for nm, src, sy in srcs:
        try:
            if src == "okx":
                t = get(f"https://www.okx.com/api/v5/market/ticker?instId={sy}")["data"][0]; last = float(t["last"]); prev = float(t["open24h"])
            else:
                last, prev = _stooq(sy)
            h = [x for x in _hist.get(nm, []) if time.time() - x[0] >= 3300]
            mk[nm] = {"last": round(last, 3), "chg24h": round((last / prev - 1) * 100, 2) if prev else None, "chg1h": round((last / h[-1][1] - 1) * 100, 2) if h else None}
            _hist[nm] = ([x for x in _hist.get(nm, []) if time.time() - x[0] < 7200] + [[time.time(), last]])[-30:]
        except Exception as e:
            summary["errors"].append(f"mkt {nm} -> {str(e)[:60]}")
    summary["macro_mkt"] = mk; summary["_mk_hist"] = _hist
except Exception as e:
    summary["errors"].append(f"macro_mkt -> {str(e)[:80]}")
try:
    opt = {}
    nowms = int(time.time() * 1000)
    dv = get(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={nowms-26*3600*1000}&end_timestamp={nowms}")["result"]["data"]
    if dv: opt["dvol"] = round(dv[-1][4], 1); opt["dvol_24h"] = round(dv[-1][4] - dv[0][4], 1)
    bs = get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option")["result"]
    spot = bs[0].get("underlying_price") or bs[0].get("estimated_delivery_price")
    import collections as _co
    exp = _co.defaultdict(list)
    for x in bs:
        p = x["instrument_name"].split("-")
        if len(p) == 4 and x.get("mark_iv"): exp[p[1]].append((float(p[2]), p[3], float(x["mark_iv"]), float(x.get("open_interest") or 0)))
    def _dte(s):
        try: return (dt.datetime.strptime(s, "%d%b%y") - dt.datetime.utcnow()).days
        except Exception: return 999
    cand = sorted([e for e in exp if 6 <= _dte(e) <= 40], key=_dte)
    if cand and spot:
        rows = exp[cand[0]]
        put = min([r for r in rows if r[1] == "P"], key=lambda r: abs(r[0] - spot * 0.9))
        call = min([r for r in rows if r[1] == "C"], key=lambda r: abs(r[0] - spot * 1.1))
        opt["skew_exp"] = cand[0]; opt["put10_iv"] = put[2]; opt["call10_iv"] = call[2]; opt["skew10"] = round(put[2] - call[2], 1)
    try:
        big_exp = max([e for e in exp if 0 <= _dte(e) <= 40], key=lambda e: sum(r[3] for r in exp[e]))
        rows = exp[big_exp]
        opt["key_exp"] = big_exp
        opt["key_puts"] = [int(r[0]) for r in sorted([r for r in rows if r[1] == "P"], key=lambda r: -r[3])[:3]]
        opt["key_calls"] = [int(r[0]) for r in sorted([r for r in rows if r[1] == "C"], key=lambda r: -r[3])[:3]]
    except Exception: pass
    tr = get("https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency=BTC&kind=option&count=1000&sorting=desc")["result"]["trades"]
    big = []; pb = cb = 0.0
    for t_ in tr:
        if nowms - t_["timestamp"] > 3600 * 1000: continue
        side = t_["instrument_name"][-1]; amt = float(t_["amount"])
        if t_["direction"] == "buy":
            if side == "P": pb += amt
            else: cb += amt
        if amt >= 100: big.append({"inst": t_["instrument_name"], "dir": t_["direction"], "amt": amt, "iv": t_.get("iv"), "block": bool(t_.get("block_trade_id"))})
    opt["buy_puts_1h"] = round(pb); opt["buy_calls_1h"] = round(cb); opt["big_1h"] = big[:10]
    summary["options"] = opt
except Exception as e:
    summary["errors"].append(f"options -> {str(e)[:100]}")
try:
    alts = {}
    for c_ in ("ETH", "SOL", "XRP", "ADA", "DOGE", "UNI", "SUI", "LTC"):
        try:
            oi = get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy={c_}&period=1H")["data"]
            ls = get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={c_}&period=1H")["data"]
            fr = get(f"https://www.okx.com/api/v5/public/funding-rate?instId={c_}-USDT-SWAP")["data"][0]
            alts[c_] = {"oi_m": round(float(oi[0][1]) / 1e6, 1), "oi_chg24": round((float(oi[0][1]) / float(oi[24][1]) - 1) * 100, 1) if len(oi) > 24 else None,
                        "ls": float(ls[0][1]), "fund": round(float(fr["fundingRate"]) * 100, 4)}
        except Exception as e:
            summary["errors"].append(f"alt {c_} -> {str(e)[:60]}")
    summary["alts"] = alts
except Exception as e:
    summary["errors"].append(f"alts -> {str(e)[:80]}")
try:
    fg = get("https://api.alternative.me/fng/?limit=2")["data"]
    summary["fear_greed"] = {"now": int(fg[0]["value"]), "label": fg[0]["value_classification"], "prev": int(fg[1]["value"])}
except Exception as e:
    summary["errors"].append(f"fng -> {str(e)[:60]}")

# 5g) Whale Alert: large BTC exchange flows and stablecoin mints/burns (needs WHALE_ALERT_KEY)
WA = os.environ.get("WHALE_ALERT_KEY", "").strip()
if WA:
    try:
        r = get(f"https://api.whale-alert.io/v1/transactions?api_key={WA}&min_value=5000000&start={int(time.time())-3500}&limit=100")
        txs = r.get("transactions") or []
        agg = {"btc_to_ex": 0.0, "btc_from_ex": 0.0, "mint_usd": 0.0, "burn_usd": 0.0}; big = []
        for t_ in txs:
            sym = (t_.get("symbol") or "").upper(); typ = t_.get("transaction_type"); amt = float(t_.get("amount") or 0); usd = float(t_.get("amount_usd") or 0)
            fr_ = t_.get("from") or {}; to_ = t_.get("to") or {}
            if sym == "BTC" and typ == "transfer":
                if to_.get("owner_type") == "exchange" and fr_.get("owner_type") != "exchange": agg["btc_to_ex"] += amt; dirn = "in"
                elif fr_.get("owner_type") == "exchange" and to_.get("owner_type") != "exchange": agg["btc_from_ex"] += amt; dirn = "out"
                else: dirn = "other"
                if amt >= 500: big.append({"h": t_.get("hash", "")[:16], "sym": sym, "amt": round(amt), "usd": round(usd), "dir": dirn, "from": fr_.get("owner") or fr_.get("owner_type"), "to": to_.get("owner") or to_.get("owner_type")})
            elif sym in ("USDT", "USDC") and typ in ("mint", "burn"):
                agg["mint_usd" if typ == "mint" else "burn_usd"] += usd
                if usd >= 5e7: big.append({"h": t_.get("hash", "")[:16], "sym": sym, "amt": round(amt), "usd": round(usd), "dir": typ, "from": fr_.get("owner"), "to": to_.get("owner")})
        summary["whale_alert"] = {k: round(v) for k, v in agg.items()}; summary["whale_alert"]["big"] = big[:15]
    except Exception as e:
        summary["errors"].append(f"whale_alert -> {str(e)[:100]}")

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
            if sym not in ("BTC", "ETH", "SOL"): continue
            for side, rows in (("支撑", h["below"]), ("起飞", h["above"])):
                for b in rows[:2]:
                    if b["strength"] >= 0.9 and abs(b["dist_pct"]) <= 0.5:
                        fire(f"hm_{sym}_{side}_{round(b['price'], 0 if b['price']>=100 else 3)}",
                             (f"🔥 {sym} {fp(h['price'])} 逼近短期支撑 {fp(b['price'])}（{b['dist_pct']:+.2f}%，强度{b['strength']}）" if side=="支撑" else f"🚀 {sym} {fp(h['price'])} 逼近突破起飞点 {fp(b['price'])}（{b['dist_pct']:+.2f}%，强度{b['strength']}）"), 3)
        # ETF big flow (latest day)
        if summary.get("etf"):
            e = summary["etf"][-1]; fu = e.get("flow_usd")
            if fu is not None and abs(fu) >= 3e8:
                d = dt.datetime.utcfromtimestamp(e["timestamp"]/1000).strftime("%m-%d")
                fire(f"etf_{d}", f"🏦 ETF {d} 净{'流入' if fu>0 else '流出'} {abs(fu)/1e6:.0f}M", 20)
        cq = summary.get("cryptoquant") or {}
        if cq.get("exch_netflow_day"):
            r = cq["exch_netflow_day"][1] if len(cq["exch_netflow_day"]) > 1 else cq["exch_netflow_day"][0]
            if r["netflow_total"] >= 5000: fire(f"nf_{r['date']}", f"⚠️ 链上 {r['date'][5:]} 交易所净流入 +{r['netflow_total']:.0f} BTC（进所抛压）", 20)
        if cq.get("miner_outflow"):
            r = cq["miner_outflow"][1] if len(cq["miner_outflow"]) > 1 else cq["miner_outflow"][0]
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
        MACRO = [("2026-11-10 13:30", "美国 10 月 CPI（只看环比；预期核心 0.3%）"), ("2026-11-13 13:30", "美国 PPI"), ("2026-11-18 19:00", "FOMC 纪要"), ("2026-11-18 21:20", "英伟达财报"), ("2026-11-25 13:30", "美国 PCE"), ("2026-11-27 08:00", "Deribit 月度交割"), ("2026-12-04 13:30", "美国 11 月非农"), ("2026-12-09 19:00", "FOMC 利率决议（市场押加息）")]
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
                pass  # below-cost alerts removed (no action value)
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
                        fire(f"oc_{sym}_{round(b['price'])}", f"🐋 链上强平集中区 {sym} {fp(b['price'])}（{b['dist_pct']:+.1f}%）≈{b['usd']/1e8:.2f}亿美元 / {b['n']}个大户{'多单' if b['long']>=b['short'] else '空单'}", 6)
            prev = state.get("whales", {}); cur = {}
            for w in _whales:
                if w["usd"] < 3e7 or w["sym"] not in ("BTC", "ETH", "SOL") or not _px_now.get(w["sym"]): continue
                w["dist_pct"] = round((w["liq"] / _px_now[w["sym"]] - 1) * 100, 2)
                if abs(w["dist_pct"]) > 15: continue
                wid = f"{w['src']}|{w['addr']}|{w['sym']}|{w['side']}"; cur[wid] = [round(w["liq"], 2), round(w["usd"])]
                tag = f"{w['sym']} {'多' if w['side']=='long' else '空'} {w['usd']/1e8:.2f}亿（{w['src']} {w['addr'][:6]}…）"
                if prev and wid not in prev:
                    fire(f"wnew_{wid}", f"🐋 新巨鲸 {tag} 强平 {fp(w['liq'])}（{w.get('dist_pct', 0):+.1f}%）", 12)
                elif wid in prev and prev[wid][0] and abs(w["liq"]/prev[wid][0]-1) >= 0.03:
                    fire(f"wmove_{wid}", f"🐋 巨鲸调仓 {tag} 强平 {fp(prev[wid][0])}→{fp(w['liq'])}（{'补保证金/减仓' if (w['liq']<prev[wid][0])==(w['side']=='long') else '加仓/提走保证金'}）", 6)
            for wid, (lq, usd) in prev.items():
                _sy = wid.split("|")[2]
                if _sy not in ("BTC", "ETH", "SOL") or not _px_now.get(_sy) or abs(lq / _px_now[_sy] - 1) > 0.15: continue
                if wid not in cur and usd >= 3e7 and _whales:
                    fire(f"wgone_{wid}", f"🐋 巨鲸消失 {wid.split('|')[2]} {'多' if wid.split('|')[3]=='long' else '空'} {usd/1e8:.2f}亿（原强平 {fp(lq)}）= 平仓或被清算", 12)
            if _whales: state["whales"] = cur
            if _aave_scan: state["aave_cache"] = _aave_cache
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
        # reversal checklist, MA proximity, CPI readout, Jin10 relay
        try:
            if _cpi_msg: fire(f"cpi_{dt.datetime.utcnow().strftime('%Y%m%d')}", _cpi_msg, 20)
            rv = summary.get("reversal") or {}
            if rv.get("n_ok") == 3:
                fire("rev_all", f"🟢 反转三条件同时满足：OI {rv['oi_yi']}亿≤{rv['oi_target_yi']} · 散户多空 {rv['retail_ls']:.2f}<2.3 · 放量长下影 {rv['wick']['ts']} 低点 {rv['wick']['low']:,.0f} + 现货买 {rv['spot_buy_15m']}% → 洗盘完成信号（现货第三批/新一轮机器人可开始考虑）", 12)
            elif rv.get("n_ok") == 2:
                fire("rev_two", "🟡 反转条件 2/3：" + " · ".join(f"{n}{'✅' if ok else '❌'}" for n, ok in (("OI", rv['conds']['oi']), ("散户<2.3", rv['conds']['ls']), ("长下影+现货买", rv['conds']['wick']))), 6)
            ma = summary.get("ma") or {}; bpx = (summary["heatmap"].get("BTC") or {}).get("price")
            if bpx:
                for nm, key in (("日线EMA200", "d_ema200"), ("日线SMA200", "d_sma200"), ("周线EMA200", "w_ema200")):
                    lv = ma.get(key)
                    if lv and abs(bpx / lv - 1) <= 0.003:
                        fire(f"ma_{key}_{lv}", f"📏 BTC {bpx:,.0f} 贴近{nm} {lv:,}（{(bpx/lv-1)*100:+.2f}%）", 6)
            jseen = state.get("jin_seen")
            if jseen is None:
                state["jin_seen"] = [j["id"] for j in summary.get("jin10") or []]
            else:
                sent = 0
                for j in reversed(summary.get("jin10") or []):
                    if j["id"] in jseen or sent >= 4: continue
                    alerts.append(f"⚡ 金十｜{j['text']}"); jseen.append(j["id"]); sent += 1
                state["jin_seen"] = jseen[-400:]
        except Exception as e: summary["errors"].append(f"rev/ma/jin alerts -> {str(e)[:120]}")
        # cross-asset shocks and big option trades
        try:
            mk = summary.get("macro_mkt") or {}
            nq = mk.get("NQ") or {}; dx = mk.get("DXY") or {}
            if nq.get("chg1h") is not None and nq["chg1h"] <= -1.2:
                fire(f"nq_{int(now//3600)}", f"🌐 纳指期货 1 小时 {nq['chg1h']:+.2f}%（外部风险资产在跌，BTC 易被拖）", 3)
            if dx.get("chg1h") is not None and dx["chg1h"] >= 0.4:
                fire(f"dxy_{int(now//3600)}", f"🌐 美元指数 1 小时 {dx['chg1h']:+.2f}% → {dx['last']}（美元急升，压风险资产）", 3)
            for b in (summary.get("options") or {}).get("big_1h", []):
                if b["amt"] >= 300 and b["dir"] == "buy":
                    fire(f"opt_{b['inst']}_{int(b['amt'])}", f"🎯 期权大单：买入 {b['inst']} {b['amt']:.0f} BTC" + ("（大宗）" if b["block"] else "") + ("——有人在买下跌保护" if b["inst"].endswith("P") else "——有人在押上涨"), 6)
        except Exception as e: summary["errors"].append(f"xasset alerts -> {str(e)[:80]}")
        # Whale Alert pushes
        try:
            for b in (summary.get("whale_alert") or {}).get("big", []):
                if b["sym"] == "BTC" and b["amt"] >= 1000 and b["dir"] in ("in", "out"):
                    fire(f"wa_{b['h']}", f"{'⬆️' if b['dir']=='in' else '⬇️'} {b['amt']:,} BTC（${b['usd']/1e6:.0f}M）{b['from']} → {b['to']}" + ("——转入交易所，可能要卖" if b["dir"] == "in" else "——从交易所提走，偏囤币"), 24)
                elif b["sym"] in ("USDT", "USDC") and b["usd"] >= 1e8:
                    fire(f"wa_{b['h']}", f"💵 {b['sym']} {'增发' if b['dir']=='mint' else '销毁'} ${b['usd']/1e6:.0f}M" + ("——新子弹进场" if b["dir"] == "mint" else "——资金离场"), 24)
        except Exception as e: summary["errors"].append(f"wa alerts -> {str(e)[:80]}")
        # periodic brief every 30 min (multi-line)
        force_brief = os.environ.get("FORCE_BRIEF") == "1"
        if force_brief or now - state.get("brief", 0) >= 29 * 60:
            try:
                L = [f"📊 {dt.datetime.utcnow().strftime('%H:%M')}Z", "━━━━━━━━━━"]
                small = []
                for sym, h in summary["heatmap"].items():
                    b = max(h["below"], key=lambda x: x["strength"]) if h["below"] else None
                    a = max(h["above"], key=lambda x: x["strength"]) if h["above"] else None
                    if sym in ("BTC", "ETH", "SOL"):
                        L.append(f"{sym} {fp(h['price'])}  " + (f"支撑 {fp(b['price'])} ({b['dist_pct']:+.1f}%)" if b else "支撑 —") + "  " + (f"起飞 {fp(a['price'])} ({a['dist_pct']:+.1f}%)" if a else "起飞 —"))
                    else:
                        small.append(f"{sym} {fp(h['price'])}({fp(b['price']) if b else '—'}/{fp(a['price']) if a else '—'})")
                if small: L.append("山寨(支撑/起飞): " + " · ".join(small))
                cgx = summary.get("cg") or {}
                try:
                    # ---------- 机构 / 大户 ----------
                    L.append("━━━ 机构/大户 ━━━")
                    if summary.get("etf"):
                        e = summary["etf"][-1]; L.append(f"ETF {dt.datetime.utcfromtimestamp(e['timestamp']/1000).strftime('%m-%d')}: {e.get('flow_usd',0)/1e6:+.0f}M")
                    cb = summary.get("coinbase") or {}
                    row = []
                    if cb.get("premium_pct") is not None: row.append(f"Coinbase 溢价 {cb.get('premium_pct')}%")
                    if summary.get("okx_top_pos") is not None: row.append(f"OKX大户持仓比 {summary['okx_top_pos']:.2f}(<1偏空)")
                    if row: L.append(" | ".join(row))
                    op = summary.get("options") or {}
                    if op.get("key_puts"): L.append(f"期权({op.get('key_exp')}) 防守位 {'/'.join(f'{k//1000}k' for k in op['key_puts'])} · 押注位 {'/'.join(f'{k//1000}k' for k in op.get('key_calls', []))} · 看跌溢价 {op.get('skew10','—')}")
                    we = summary.get("whale_entry") or {}
                    if we: L.append("链上大户均价: " + " · ".join(f"{'多' if sd=='long' else '空'} {v['avg']:,}（{v['usd']/1e8:.1f}亿/{v['n']}个）" for sd, v in we.items()))
                    wa = summary.get("whale_alert")
                    if wa: L.append(f"大额转账1h: BTC 进所 {wa['btc_to_ex']:,} / 出所 {wa['btc_from_ex']:,} · 稳定币 增发 ${wa['mint_usd']/1e6:.0f}M / 销毁 ${wa['burn_usd']/1e6:.0f}M")
                    row = []
                    if cq.get("exch_netflow_day"):
                        r0 = cq["exch_netflow_day"][0]; row.append(f"交易所净流 {r0['date'][5:]} {r0['netflow_total']:+,.0f}")
                    if cq.get("miner_outflow"): row.append(f"矿工 {cq['miner_outflow'][0]['outflow_total']:,.0f}")
                    scb = (summary.get("stablecoins") or {}).get("USDT")
                    if scb: row.append(f"USDT 7天 {scb['chg7_b']:+.1f}B")
                    if row: L.append(" | ".join(row))
                    # ---------- 散户 / 高杠杆 ----------
                    L.append("━━━ 散户/高杠杆 ━━━")
                    row = []
                    try:
                        if cgx.get("ls_global_1h"): row.append(f"散户多空 {float(cgx['ls_global_1h'][-1].get('global_account_long_short_ratio', 0)):.2f}")
                    except Exception: pass
                    rv = summary.get("reversal") or {}
                    if rv: row.append(f"OKX OI {rv['oi_yi']}亿")
                    try:
                        fr = cgx.get("funding_oi_weighted")
                        if fr: row.append(f"费率 {float(fr[-1]['close']):.4f}%")
                    except Exception: pass
                    if row: L.append(" | ".join(row))
                    fu5 = summary.get("fuel5")
                    if fu5:
                        dl = fu5["ex_long"] + fu5["oc_long"]; up = fu5["ex_short"] + fu5["oc_short"]
                        L.append(f"燃料±5%: 下方多单≈{dl/1e8:.1f}亿 | 上方空单≈{up/1e8:.1f}亿")
                    ocl = []
                    for sym, o in (summary.get("onchain_liq") or {}).items():
                        bs = sorted([b for b in o.get("below", []) + o.get("above", []) if abs(b["dist_pct"]) <= 6], key=lambda b: -b["usd"])[:2]
                        if bs: ocl.append(f"{sym} " + " · ".join(f"{fp(b['price'])} {b['usd']/1e8:.1f}亿({b['dist_pct']:+.1f}%)" for b in bs))
                    if ocl: L.append("链上强平(近): " + " | ".join(ocl))
                    al = summary.get("alts") or {}
                    if al: L.append("山寨 OI24h/散户/费率: " + " · ".join(f"{c} {v['oi_chg24']:+.0f}%/{v['ls']:.1f}/{v['fund']:.3f}%" for c, v in al.items() if c in ("ETH", "SOL", "XRP", "ADA") and v.get("oi_chg24") is not None))
                    if rv: L.append(f"反转: OI {rv['oi_yi']}/{rv['oi_target_yi']}{'✅' if rv['conds']['oi'] else '❌'} · 散户 {rv['retail_ls']:.2f}/2.3{'✅' if rv['conds']['ls'] else '❌'} · 长下影{'✅' if rv['conds']['wick'] else '❌'}")
                    # ---------- 宏观 / 技术 ----------
                    L.append("━━━ 宏观/技术 ━━━")
                    ma = summary.get("ma") or {}
                    mt = [f"{n} {ma[k]:,}" for n, k in (("日EMA200", "d_ema200"), ("365日", "d_sma365"), ("日SMA200", "d_sma200"), ("周EMA200", "w_ema200")) if ma.get(k)]
                    if mt: L.append("均线: " + " · ".join(mt))
                    mk = summary.get("macro_mkt") or {}
                    xs = [f"{n} {mk[k]['chg24h']:+.1f}%" for n, k in (("纳指", "NQ"), ("英伟达", "NVDA"), ("美元", "DXY"), ("黄金", "GOLD"), ("原油", "OIL")) if mk.get(k) and mk[k].get("chg24h") is not None]
                    if mk.get("US10Y"): xs.append(f"10Y {mk['US10Y']['last']:.2f}%")
                    if xs: L.append("外部24h: " + " · ".join(xs))
                    fgv = summary.get("fear_greed")
                    if fgv: L.append(f"恐慌贪婪 {fgv['now']}（{fgv['label']}）")
                except Exception as e: summary["errors"].append(f"brief body -> {str(e)[:80]}")
                accts = summary.get("accounts") or {}
                if accts:
                    L.append("━━━ 账户 ━━━")
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
        # auto-subscribe: whitelisted Telegram usernames who press Start on the bot get added (no Secret edit needed)
        TG_ALLOW = {u.strip().lstrip("@").lower() for u in os.environ.get("TG_ALLOW_USERS", "U992026,AXG688,YKs666").split(",") if u.strip()}
        subs = state.get("tg_subs", {})
        try:
            upd = get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset=-100&limit=100").get("result", [])
            for u in upd:
                m = u.get("message") or {}; fr = m.get("from") or {}; ch = m.get("chat") or {}
                un = (fr.get("username") or "").lower(); cid = str(ch.get("id") or "")
                if cid and un in TG_ALLOW and cid not in subs and cid not in TG_CHAT:
                    subs[cid] = un
                    alerts_new_sub = f"✅ 已加入行情提醒（@{fr.get('username')}）。之后的简报和提醒会同步发到这里。"
                    try:
                        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                            data=urllib.parse.urlencode({"chat_id": cid, "text": alerts_new_sub}).encode(), headers={"User-Agent": UA}), timeout=20).read()
                    except Exception: pass
                    alerts.append(f"👤 新成员已自动加入：@{fr.get('username')}")
            state["tg_subs"] = subs
        except Exception as e: summary["errors"].append(f"tg subs -> {str(e)[:80]}")
        if alerts:
            MKT_ONLY = {u.strip().lstrip("@").lower() for u in os.environ.get("TG_MARKET_ONLY_USERS", "AXG688,YKs666").split(",") if u.strip()}
            acct_labels = [lb for lb, _ in ACCTS] + ["OKX", "账户", "逐仓", "全仓"]
            short = [x for x in alerts if not x.startswith(("📊", "🔴 美国"))]; briefs = [x for x in alerts if x.startswith(("📊", "🔴 美国"))]
            def _msgs(market_only):
                sh = [x for x in short if not (market_only and any(lb in x for lb in acct_labels))]
                br = [(x.split("━━━ 账户 ━━━")[0].rstrip() if market_only else x) for x in briefs]
                return (["\n".join(sh)] if sh else []) + br
            full_msgs = _msgs(False); mkt_msgs = _msgs(True)
            recips = [c.strip() for c in TG_CHAT.split(",") if c.strip()] + [c for c in subs if c not in TG_CHAT]
            for cid in recips:
                mo = (subs.get(cid) or "").lower() in MKT_ONLY
                for text in (mkt_msgs if mo else full_msgs):
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                            data=urllib.parse.urlencode({"chat_id": cid, "text": text}).encode(), headers={"User-Agent": UA}), timeout=20).read()
                    except Exception as e:
                        summary["errors"].append(f"tg {cid[:4]}.. -> {e}")
        state["mk_hist"] = summary.pop("_mk_hist", state.get("mk_hist", {}))
        json.dump(state, open("alerts_state.json", "w"))
        summary["alerts_sent"] = alerts
except Exception as _e:
    summary['errors'].append(f'alerts block -> {_e}')

with open("summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)
print("ok", summary["ts_utc"], "heatmap=", ",".join(summary["heatmap"].keys()), "errors=", len(summary["errors"]))
for e in summary["errors"]: print("  ", e[:200])
