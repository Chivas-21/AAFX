"""
AAFX - Cảnh báo mẫu nến 3-1-3 có lọc SMC về Telegram.

Mẫu cần bắt (xét 7 nến đã đóng):
  - 3 giảm – 1 tăng – 3 giảm  (nến tăng ở giữa = Order Block bán)
  - 3 tăng – 1 giảm – 3 tăng  (nến giảm ở giữa = Order Block mua)

Bộ lọc SMC (chỉ báo khi đạt cả 2):
  1. BOS: 3 nến sau đóng cửa phá đáy (mẫu giảm) / đỉnh (mẫu tăng) cấu trúc gần nhất còn nguyên.
  2. Vị trí: OB bán nằm ở vùng Premium, OB mua nằm ở vùng Discount của biên độ RANGE_LEN nến.
  Tin nhắn kèm thêm: vùng giá OB, % vị trí trong biên độ, có FVG hay không.

Dữ liệu:
  - Có token OANDA: cả 4 mã lấy từ OANDA (cùng nguồn chart OANDA trên TradingView).
  - Chưa có: XAUUSD lấy Yahoo GC=F, BTCUSD lấy Yahoo BTC-USD, EURUSD/GBPUSD tạm bỏ qua.

Cách dùng:
  python canh_bao_3_1_3.py            chạy theo dõi liên tục
  python canh_bao_3_1_3.py --chat-id  tìm chat_id sau khi bạn nhắn /start cho bot
  python canh_bao_3_1_3.py --test     gửi thử 1 tin nhắn Telegram
  python canh_bao_3_1_3.py --scan     in các mẫu gần đây và mẫu qua lọc SMC (không gửi)
  python canh_bao_3_1_3.py --once     kiểm tra 1 lần rồi thoát (dùng cho GitHub Actions)

Thông tin bí mật lấy từ biến môi trường (GitHub Secrets) nếu có:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OANDA_TOKEN
nếu không thì đọc từ config.ini.
"""

import configparser
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.ini"
STATE_FILE = HERE / "da_gui.json"

VN_TZ = timezone(timedelta(hours=7))
OANDA_URL = "https://api-fxpractice.oanda.com/v3"

# tên hiển thị -> (mã OANDA, mã Yahoo dự phòng hoặc None)
SYMBOLS = {
    "XAUUSD": ("XAU_USD", "GC=F"),
    "EURUSD": ("EUR_USD", None),
    "GBPUSD": ("GBP_USD", None),
    "BTCUSD": ("BTC_USD", "BTC-USD"),
}

# tên hiển thị -> (granularity OANDA, interval Yahoo, range Yahoo, số giây mỗi nến, chu kỳ kiểm tra)
TIMEFRAMES = {
    "1m":  ("M1",  "1m",  "1d", 60,   20),
    "5m":  ("M5",  "5m",  "5d", 300,  60),
    "15m": ("M15", "15m", "5d", 900,  60),
    "1H":  ("H1",  "60m", "1mo", 3600, 120),
}

# ── Thông số SMC ────────────────────────────────────────────────────
SMC_FILTER = True   # False = báo mọi mẫu 3-1-3 như bản cũ
SWING_K = 2         # swing high/low: cao/thấp hơn 2 nến mỗi bên
SWING_LOOKBACK = 50 # tìm đỉnh/đáy cấu trúc trong 50 nến trước OB
RANGE_LEN = 100     # biên độ để tính Premium/Discount


# ── Cấu hình ────────────────────────────────────────────────────────
def load_config():
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE, encoding="utf-8-sig")  # chấp nhận cả file Notepad lưu kèm BOM

    def pick(env, section, key):
        return (os.environ.get(env) or cfg.get(section, key, fallback="")).strip()

    conf = {
        "tg_token": pick("TELEGRAM_BOT_TOKEN", "telegram", "bot_token"),
        "chat_id":  pick("TELEGRAM_CHAT_ID", "telegram", "chat_id"),
        "oanda":    pick("OANDA_TOKEN", "oanda", "token"),
        # M = giá giữa, B = giá bid, A = giá ask
        "price":    pick("OANDA_PRICE", "oanda", "price") or "M",
    }
    if "DAN_TOKEN" in conf["oanda"]:
        conf["oanda"] = ""
    return cfg, conf


def need(conf, key, hint):
    if not conf[key] or "DAN_TOKEN" in conf[key]:
        sys.exit(hint)


def active_symbols(conf):
    """Mã nào đang theo dõi được, và lấy từ nguồn nào."""
    out = {}
    for sym, (oanda_code, yahoo_code) in SYMBOLS.items():
        if conf["oanda"]:
            out[sym] = ("OANDA", oanda_code)
        elif yahoo_code:
            out[sym] = (f"Yahoo {yahoo_code}", yahoo_code)
    return out


# ── Telegram ────────────────────────────────────────────────────────
def telegram(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.load(r)


def send(conf, text):
    telegram(conf["tg_token"], "sendMessage", {"chat_id": conf["chat_id"], "text": text})


def find_chat_id(cfg, conf):
    res = telegram(conf["tg_token"], "getUpdates", {})
    chats = {}
    for u in res.get("result", []):
        # tin nhắn thường, bài đăng kênh, hoặc sự kiện bot được thêm vào nhóm
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("first_name") or ""
    if not chats:
        print("Chưa thấy tin nhắn nào. Mở Telegram, nhắn /start cho bot rồi chạy lại.")
        return
    chat_id = list(chats)[-1]
    cfg.set("telegram", "chat_id", str(chat_id))
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)
    print(f"Đã lưu chat_id = {chat_id} ({chats[chat_id]}) vào config.ini")


# ── Dữ liệu nến ─────────────────────────────────────────────────────
def http_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_oanda(conf, code, tf):
    params = {"granularity": TIMEFRAMES[tf][0], "count": 200, "price": conf["price"], "dateTimeFormat": "UNIX"}
    data = http_json(f"{OANDA_URL}/instruments/{code}/candles?{urllib.parse.urlencode(params)}",
                     {"Authorization": f"Bearer {conf['oanda']}"})
    field = {"M": "mid", "B": "bid", "A": "ask"}[conf["price"]]
    candles = []
    for k in data.get("candles", []):
        if not k.get("complete"):  # nến đang chạy, chưa đóng
            continue
        p = k[field]
        candles.append({"t": int(float(k["time"])), "o": float(p["o"]), "h": float(p["h"]),
                        "l": float(p["l"]), "c": float(p["c"])})
    return candles


def fetch_yahoo(code, tf):
    _, interval, rng, seconds, _ = TIMEFRAMES[tf]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(code)}"
           f"?interval={interval}&range={rng}")
    result = http_json(url)["chart"]["result"]
    if not result:
        return []
    res = result[0]
    q = res["indicators"]["quote"][0]
    now = time.time()
    candles = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c) or ts + seconds > now:  # thiếu dữ liệu hoặc nến chưa đóng
            continue
        candles.append({"t": ts, "o": o, "h": h, "l": l, "c": c})
    return candles


def fetch_closed_candles(conf, sym, tf):
    source, code = active_symbols(conf)[sym]
    if source == "OANDA":
        return fetch_oanda(conf, code, tf)
    return fetch_yahoo(code, tf)


# ── Nhận diện mẫu 3-1-3 và lọc SMC ──────────────────────────────────
def color(candle):
    if candle["c"] > candle["o"]:
        return "U"
    if candle["c"] < candle["o"]:
        return "D"
    return "-"  # doji, không tính


def is_swing_low(cs, j):
    return all(cs[j]["l"] < cs[j - d]["l"] and cs[j]["l"] <= cs[j + d]["l"] for d in range(1, SWING_K + 1))


def is_swing_high(cs, j):
    return all(cs[j]["h"] > cs[j - d]["h"] and cs[j]["h"] >= cs[j + d]["h"] for d in range(1, SWING_K + 1))


def structure_level(cs, m, bearish):
    """Đáy (mẫu giảm) / đỉnh (mẫu tăng) cấu trúc gần nhất trước OB mà giá chưa đóng cửa phá."""
    for j in range(m - 1 - SWING_K, max(SWING_K, m - SWING_LOOKBACK) - 1, -1):
        if bearish and is_swing_low(cs, j):
            level = cs[j]["l"]
            if all(k["c"] >= level for k in cs[j + 1:m + 1]):
                return level
        if not bearish and is_swing_high(cs, j):
            level = cs[j]["h"]
            if all(k["c"] <= level for k in cs[j + 1:m + 1]):
                return level
    return None


def has_fvg(cs, m, bearish):
    """Có khoảng trống giá (FVG) trong nhịp từ OB qua 3 nến sau không."""
    for i in range(m, m + 2):
        a, c = cs[i], cs[i + 2]
        if bearish and a["l"] > c["h"]:
            return True
        if not bearish and a["h"] < c["l"]:
            return True
    return False


def smc_info(cs, m, bearish):
    """Phân tích SMC cho OB tại vị trí m. Trả về dict, 'ok' = qua bộ lọc."""
    level = structure_level(cs, m, bearish)
    impulse = cs[m + 1:m + 4]
    bos = level is not None and (any(k["c"] < level for k in impulse) if bearish
                                 else any(k["c"] > level for k in impulse))
    window = cs[max(0, m + 4 - RANGE_LEN):m + 4]
    hh, ll = max(k["h"] for k in window), min(k["l"] for k in window)
    ob = cs[m]
    pos = ((ob["o"] + ob["c"]) / 2 - ll) / (hh - ll) if hh > ll else 0.5
    zone_ok = pos > 0.5 if bearish else pos < 0.5
    return {"ok": bos and zone_ok, "bos": bos, "level": level, "pos": pos,
            "fvg": has_fvg(cs, m, bearish), "ob_hi": ob["h"], "ob_lo": ob["l"]}


def find_patterns(candles):
    """Danh sách mẫu 3-1-3: (nến giữa, nến thứ 7, tên mẫu, thông tin SMC)."""
    found = []
    for i in range(len(candles) - 6):
        s = "".join(color(k) for k in candles[i:i + 7])
        if s not in ("DDDUDDD", "UUUDUUU"):
            continue
        bearish = s == "DDDUDDD"
        name = "3 giảm – 1 tăng – 3 giảm" if bearish else "3 tăng – 1 giảm – 3 tăng"
        found.append((candles[i + 3], candles[i + 6], name, smc_info(candles, i + 3, bearish)))
    return found


# ── Tin nhắn ────────────────────────────────────────────────────────
def fmt_time(ts):
    return datetime.fromtimestamp(ts, VN_TZ).strftime("%d/%m %H:%M")


def closed_at(tf, last):
    return last["t"] + TIMEFRAMES[tf][3]


def fmt_price(x):
    return f"{x:,.5f}".rstrip("0").rstrip(".") if x < 10 else f"{x:,.2f}"


def build_message(sym, source, tf, mid, last, name, smc, now):
    bearish = name.startswith("3 giảm")
    icon = "🔻" if bearish else "🔺"
    zone = "Premium" if smc["pos"] > 0.5 else "Discount"
    msg = (f"{icon} {sym} ({source}) khung {tf}\n"
           f"Mẫu: {name}\n"
           f"Order Block {'bán' if bearish else 'mua'}: {fmt_price(smc['ob_lo'])} – {fmt_price(smc['ob_hi'])}"
           f" (nến {fmt_time(mid['t'])})\n"
           f"✅ BOS: đóng cửa phá {'đáy' if bearish else 'đỉnh'} {fmt_price(smc['level'])}\n"
           f"✅ Vùng {zone}: {smc['pos'] * 100:.0f}% biên độ {RANGE_LEN} nến\n"
           f"{'✅ Có FVG' if smc['fvg'] else '▫️ Không có FVG'}\n"
           f"Nến thứ 7 đóng lúc {fmt_time(closed_at(tf, last))} (giờ VN), giá {fmt_price(last['c'])}")
    delay = int((now - closed_at(tf, last)) // 60)
    if delay >= 2:
        msg += f"\n⏱ Báo trễ {delay} phút"
    return msg


# ── Lưu những mẫu đã báo để không gửi trùng ─────────────────────────
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    keep = {f"{s} {tf}" for s in SYMBOLS for tf in TIMEFRAMES}
    STATE_FILE.write_text(json.dumps({k: v for k, v in state.items() if k in keep}), encoding="utf-8")


# ── Chạy ────────────────────────────────────────────────────────────
MAX_AGE = 60 * 60  # mẫu đã hoàn thành quá 60 phút thì chỉ ghi nhận, không báo nữa


def check(conf, sym, tf, state):
    """Kiểm tra 1 mã ở 1 khung, gửi các mẫu mới qua được bộ lọc."""
    now = time.time()
    key = f"{sym} {tf}"
    source = active_symbols(conf)[sym][0]
    try:
        pats = find_patterns(fetch_closed_candles(conf, sym, tf))
    except Exception as e:  # mạng chập chờn hoặc mã không có ở nguồn: thử lại lần sau
        print(f"[{fmt_time(now)}] {key}: lỗi lấy dữ liệu: {e}")
        return
    # Lần đầu gặp mã/khung này chỉ ghi nhận mẫu cũ, không gửi lại lịch sử
    first_run = key not in state
    seen = set(state.get(key, []))
    for mid, last, name, smc in pats:
        mark = str(mid["t"])
        if mark in seen:
            continue
        seen.add(mark)
        if first_run or now - closed_at(tf, last) > MAX_AGE or (SMC_FILTER and not smc["ok"]):
            continue
        print(f"[{fmt_time(now)}] GỬI: {name} ({key})")
        try:
            send(conf, build_message(sym, source, tf, mid, last, name, smc, now))
        except Exception as e:
            print(f"   lỗi gửi Telegram: {e}")
            seen.discard(mark)  # gửi lại ở lần kiểm tra sau
    state[key] = sorted(seen)[-200:]


def scan(conf):
    for sym, (source, _) in active_symbols(conf).items():
        for tf in TIMEFRAMES:
            try:
                pats = find_patterns(fetch_closed_candles(conf, sym, tf))
            except Exception as e:
                print(f"== {sym} {tf}: lỗi {e}")
                continue
            good = [p for p in pats if p[3]["ok"]]
            last = ", ".join(f"{fmt_time(m['t'])} {n[:6]}" for m, _, n, _ in good[-2:])
            print(f"== {sym} {tf} ({source}): {len(pats)} mẫu 3-1-3, {len(good)} qua lọc SMC  {last}")


def run_once(conf):
    state = load_state()
    for sym in active_symbols(conf):
        for tf in TIMEFRAMES:
            check(conf, sym, tf, state)
    save_state(state)


def run(conf):
    state = load_state()
    syms = list(active_symbols(conf))
    next_check = {tf: 0 for tf in TIMEFRAMES}
    print(f"Đang theo dõi {', '.join(syms)} các khung {', '.join(TIMEFRAMES)}. Nhấn Ctrl+C để dừng.")
    send(conf, f"✅ AAFX bắt đầu theo dõi mẫu 3-1-3 (lọc SMC): {', '.join(syms)} - 1m, 5m, 15m, 1H")
    while True:
        for tf, (*_, every) in TIMEFRAMES.items():
            if time.time() < next_check[tf]:
                continue
            next_check[tf] = time.time() + every
            for sym in syms:
                check(conf, sym, tf, state)
            save_state(state)
        time.sleep(5)


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # để cửa sổ CMD hiện được tiếng Việt
    sys.stderr.reconfigure(encoding="utf-8")
    cfg, conf = load_config()
    if "--scan" in sys.argv:
        scan(conf)
        return
    need(conf, "tg_token", "Chưa điền bot_token Telegram trong config.ini.")
    if "--chat-id" in sys.argv:
        find_chat_id(cfg, conf)
        return
    need(conf, "chat_id", "Chưa có chat_id. Nhắn /start cho bot rồi chạy: python canh_bao_3_1_3.py --chat-id")
    if "--test" in sys.argv:
        send(conf, "🔔 Tin nhắn thử từ AAFX: kết nối Telegram OK")
        print("Đã gửi tin nhắn thử.")
    elif "--once" in sys.argv:
        run_once(conf)
    else:
        try:
            run(conf)
        except KeyboardInterrupt:
            print("Đã dừng.")


if __name__ == "__main__":
    main()
