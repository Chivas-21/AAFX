"""
AAFX - Cảnh báo mẫu nến 3-1-3 về Telegram.

Mẫu cần bắt (xét 7 nến đã đóng gần nhất):
  - 3 giảm – 1 tăng – 3 giảm  (nến tăng lẻ giữa nhịp giảm, giảm tiếp diễn)
  - 3 tăng – 1 giảm – 3 tăng  (nến giảm lẻ giữa nhịp tăng, tăng tiếp diễn)

Dữ liệu: Yahoo Finance, mã GC=F (vàng tương lai COMEX, hướng nến bám sát XAUUSD).

Cách dùng:
  python canh_bao_3_1_3.py            chạy theo dõi liên tục
  python canh_bao_3_1_3.py --chat-id  tìm chat_id sau khi bạn nhắn /start cho bot
  python canh_bao_3_1_3.py --test     gửi thử 1 tin nhắn Telegram
  python canh_bao_3_1_3.py --scan     in các mẫu 3-1-3 trong dữ liệu gần đây (không gửi)
  python canh_bao_3_1_3.py --once     kiểm tra 1 lần rồi thoát (dùng cho GitHub Actions)

Token và chat_id lấy từ biến môi trường TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
nếu có (GitHub Secrets), nếu không thì đọc từ config.ini.
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

SYMBOL = "GC=F"
VN_TZ = timezone(timedelta(hours=7))

# tên hiển thị -> (interval của Yahoo, range tải về, số giây mỗi nến, chu kỳ kiểm tra)
TIMEFRAMES = {
    "1m":  ("1m",  "1d", 60,   20),
    "5m":  ("5m",  "1d", 300,  60),
    "15m": ("15m", "5d", 900,  60),
    "1H":  ("60m", "5d", 3600, 120),
}


# ── Cấu hình ────────────────────────────────────────────────────────
def load_config():
    cfg = configparser.ConfigParser()
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env_token:
        return cfg, env_token, os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not CONFIG_FILE.exists():
        sys.exit(f"Không thấy {CONFIG_FILE.name}. Hãy tạo file này theo hướng dẫn.")
    cfg.read(CONFIG_FILE, encoding="utf-8-sig")  # chấp nhận cả file Notepad lưu kèm BOM
    token = cfg.get("telegram", "bot_token", fallback="").strip()
    chat_id = cfg.get("telegram", "chat_id", fallback="").strip()
    if not token or "DAN_TOKEN" in token:
        sys.exit("Chưa điền bot_token trong config.ini.")
    return cfg, token, chat_id


# ── Telegram ────────────────────────────────────────────────────────
def telegram(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.load(r)


def send(token, chat_id, text):
    telegram(token, "sendMessage", {"chat_id": chat_id, "text": text})


def find_chat_id(cfg, token):
    res = telegram(token, "getUpdates", {})
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
def fetch_closed_candles(tf):
    interval, rng, seconds, _ = TIMEFRAMES[tf]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(SYMBOL)}"
           f"?interval={interval}&range={rng}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        result = json.load(r)["chart"]["result"]
    if not result:
        return []
    res = result[0]
    q = res["indicators"]["quote"][0]
    now = time.time()
    candles = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, c = q["open"][i], q["close"][i]
        if o is None or c is None:
            continue
        if ts + seconds > now:  # nến đang chạy, chưa đóng
            continue
        candles.append({"t": ts, "o": o, "c": c})
    return candles


def color(candle):
    if candle["c"] > candle["o"]:
        return "U"
    if candle["c"] < candle["o"]:
        return "D"
    return "-"  # doji, không tính


def match_313(last7):
    """Trả về tên mẫu nếu 7 nến khớp 3-1-3, ngược lại None."""
    s = "".join(color(k) for k in last7)
    if s == "DDDUDDD":
        return "3 giảm – 1 tăng – 3 giảm"
    if s == "UUUDUUU":
        return "3 tăng – 1 giảm – 3 tăng"
    return None


def find_patterns(candles):
    found = []
    for i in range(len(candles) - 6):
        name = match_313(candles[i:i + 7])
        if name:
            found.append((candles[i + 3], candles[i + 6], name))
    return found


def fmt_time(ts):
    return datetime.fromtimestamp(ts, VN_TZ).strftime("%d/%m %H:%M")


def closed_at(tf, last):
    return last["t"] + TIMEFRAMES[tf][2]


def build_message(tf, mid, last, name, now):
    icon = "🔻" if name.startswith("3 giảm") else "🔺"
    msg = (f"{icon} XAUUSD (GC=F) khung {tf}\n"
           f"Mẫu: {name}\n"
           f"Nến giữa: {fmt_time(mid['t'])} (giờ VN)\n"
           f"Nến thứ 7 đóng lúc: {fmt_time(closed_at(tf, last))}, giá {last['c']:.2f}")
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
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


# ── Chạy ────────────────────────────────────────────────────────────
def scan():
    for tf in TIMEFRAMES:
        pats = find_patterns(fetch_closed_candles(tf))
        print(f"== {tf}: {len(pats)} mẫu")
        for mid, last, name in pats[-5:]:
            print(f"   {fmt_time(mid['t'])}  {name}")


MAX_AGE = 60 * 60  # mẫu đã hoàn thành quá 60 phút thì chỉ ghi nhận, không báo nữa


def check_tf(tf, state, token, chat_id):
    """Kiểm tra 1 khung thời gian, gửi các mẫu mới. Trả về False nếu lỗi lấy dữ liệu."""
    now = time.time()
    try:
        pats = find_patterns(fetch_closed_candles(tf))
    except Exception as e:  # mạng chập chờn: thử lại lần sau
        print(f"[{fmt_time(now)}] {tf}: lỗi lấy dữ liệu: {e}")
        return False
    # Lần đầu gặp khung này chỉ ghi nhận mẫu cũ, không gửi lại lịch sử
    first_run = tf not in state
    seen = set(state.get(tf, []))
    for mid, last, name in pats:
        key = str(mid["t"])
        if key in seen:
            continue
        seen.add(key)
        if first_run or now - closed_at(tf, last) > MAX_AGE:
            continue
        msg = build_message(tf, mid, last, name, now)
        print(f"[{fmt_time(now)}] GỬI: {name} ({tf})")
        try:
            send(token, chat_id, msg)
        except Exception as e:
            print(f"   lỗi gửi Telegram: {e}")
            seen.discard(key)  # gửi lại ở lần kiểm tra sau
    state[tf] = sorted(seen)[-200:]
    return True


def require_chat_id(chat_id):
    if not chat_id:
        sys.exit("Chưa có chat_id. Nhắn /start cho bot rồi chạy: python canh_bao_3_1_3.py --chat-id")


def run_once(token, chat_id):
    require_chat_id(chat_id)
    state = load_state()
    for tf in TIMEFRAMES:
        check_tf(tf, state, token, chat_id)
    save_state(state)


def run(token, chat_id):
    require_chat_id(chat_id)
    state = load_state()
    next_check = {tf: 0 for tf in TIMEFRAMES}
    print(f"Đang theo dõi {SYMBOL} các khung {', '.join(TIMEFRAMES)}. Nhấn Ctrl+C để dừng.")
    send(token, chat_id, "✅ AAFX bắt đầu theo dõi mẫu 3-1-3: XAUUSD 1m, 5m, 15m, 1H")

    while True:
        for tf, (_, _, _, every) in TIMEFRAMES.items():
            if time.time() < next_check[tf]:
                continue
            next_check[tf] = time.time() + every
            check_tf(tf, state, token, chat_id)
            save_state(state)
        time.sleep(5)


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # để cửa sổ CMD hiện được tiếng Việt
    sys.stderr.reconfigure(encoding="utf-8")
    if "--scan" in sys.argv:
        scan()
        return
    cfg, token, chat_id = load_config()
    if "--chat-id" in sys.argv:
        find_chat_id(cfg, token)
    elif "--test" in sys.argv:
        send(token, chat_id, "🔔 Tin nhắn thử từ AAFX: kết nối Telegram OK")
        print("Đã gửi tin nhắn thử.")
    elif "--once" in sys.argv:
        run_once(token, chat_id)
    else:
        try:
            run(token, chat_id)
        except KeyboardInterrupt:
            print("Đã dừng.")


if __name__ == "__main__":
    main()
