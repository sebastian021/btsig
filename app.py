from flask import Flask, jsonify, render_template, request
import requests
import sqlite3
import time
import os

app = Flask(__name__)

BINANCE = "https://api.binance.com"
SYMBOL = "BTCUSDT"
DATABASE = "btc_data.db"

INTERVALS = {
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
    "1M": "1M",
}


def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def table_name(interval):
    safe_name = interval.replace("M", "month")
    return f"candles_{safe_name}"


def init_database():
    connection = get_db()

    for interval in INTERVALS.values():
        table = table_name(interval)

        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                time INTEGER PRIMARY KEY,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL
            )
        """)

    connection.commit()
    connection.close()


def save_candles(interval, candles):
    table = table_name(interval)
    connection = get_db()

    connection.executemany(
        f"""
        INSERT OR REPLACE INTO {table}
        (time, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                candle["time"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
            )
            for candle in candles
        ],
    )

    connection.commit()
    connection.close()


def load_candles(interval, limit=1000):
    table = table_name(interval)
    connection = get_db()

    rows = connection.execute(
        f"""
        SELECT time, open, high, low, close, volume
        FROM {table}
        ORDER BY time DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    connection.close()

    return [dict(row) for row in reversed(rows)]


def fetch_binance_candles(interval, limit=1000, start_time=None):
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "limit": min(limit, 1000),
    }

    if start_time is not None:
        params["startTime"] = start_time

    response = requests.get(
        f"{BINANCE}/api/v3/klines",
        params=params,
        timeout=20,
    )

    response.raise_for_status()
    raw_data = response.json()

    candles = []

    for candle in raw_data:
        candles.append({
            "time": int(candle[0] // 1000),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        })

    return candles


def update_candles(interval):
    stored = load_candles(interval, 1000)

    if stored:
        last_time_ms = stored[-1]["time"] * 1000
        fresh = fetch_binance_candles(
            interval,
            limit=1000,
            start_time=last_time_ms,
        )
    else:
        fresh = fetch_binance_candles(interval, limit=1000)

    if fresh:
        save_candles(interval, fresh)

    return load_candles(interval, 1000)


def ema(values, length):
    if not values:
        return []

    multiplier = 2 / (length + 1)
    result = [values[0]]

    for value in values[1:]:
        result.append(
            value * multiplier + result[-1] * (1 - multiplier)
        )

    return result


def calculate_rsi(values, length=14):
    if len(values) <= length:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    average_gain = sum(gains[:length]) / length
    average_loss = sum(losses[:length]) / length

    for gain, loss in zip(gains[length:], losses[length:]):
        average_gain = (
            average_gain * (length - 1) + gain
        ) / length

        average_loss = (
            average_loss * (length - 1) + loss
        ) / length

    if average_loss == 0:
        return 100.0

    rs = average_gain / average_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_atr(candles, length=14):
    if len(candles) < 2:
        return 0.0

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )

        true_ranges.append(true_range)

    recent = true_ranges[-length:]

    if not recent:
        return 0.0

    return sum(recent) / len(recent)


def get_trend(candles):
    if len(candles) < 50:
        return "خنثی"

    closes = [candle["close"] for candle in candles]

    ema20 = ema(closes, 20)[-1]
    ema50 = ema(closes, 50)[-1]
    price = closes[-1]

    if price > ema20 > ema50:
        return "صعودی"

    if price < ema20 < ema50:
        return "نزولی"

    return "خنثی"


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/candles")
def candles_api():
    timeframe = request.args.get("tf", "1h")

    if timeframe not in INTERVALS:
        return jsonify({
            "error": "تایم‌فریم نامعتبر است"
        }), 400

    try:
        candles = update_candles(INTERVALS[timeframe])

        if not candles:
            return jsonify({
                "error": "هیچ داده‌ای برای این تایم‌فریم دریافت نشد"
            }), 502

        return jsonify(candles)

    except requests.RequestException as error:
        app.logger.error(error)

        return jsonify({
            "error": (
                "دریافت داده از Binance ناموفق بود. "
                "دسترسی اینترنت یا API را بررسی کن."
            )
        }), 502

    except Exception as error:
        app.logger.exception(error)

        return jsonify({
            "error": "خطای داخلی هنگام آماده‌سازی کندل‌ها"
        }), 500


@app.route("/api/analysis")
def analysis_api():
    try:
        frames = {}

        for timeframe, interval in INTERVALS.items():
            frames[timeframe] = update_candles(interval)

        trends = {
            timeframe: get_trend(candles)
            for timeframe, candles in frames.items()
        }

        entry_candles = frames["30m"]

        if len(entry_candles) < 60:
            return jsonify({
                "error": "برای تحلیل هنوز کندل کافی ذخیره نشده است"
            }), 503

        closes = [
            candle["close"]
            for candle in entry_candles
        ]

        price = closes[-1]
        rsi_30m = calculate_rsi(closes)
        atr_30m = calculate_atr(entry_candles)

        recent = entry_candles[-48:]

        swing_low = min(
            candle["low"]
            for candle in recent
        )

        swing_high = max(
            candle["high"]
            for candle in recent
        )

        current_volume = entry_candles[-1]["volume"]

        previous_volumes = [
            candle["volume"]
            for candle in entry_candles[-21:-1]
        ]

        average_volume = (
            sum(previous_volumes) / len(previous_volumes)
            if previous_volumes
            else 0
        )

        if average_volume > 0:
            volume_ratio = round(
                current_volume / average_volume,
                2,
            )
        else:
            volume_ratio = 0.0

        higher_timeframe_bullish = (
            trends["1M"] == "صعودی"
            and trends["1w"] == "صعودی"
        )

        higher_timeframe_bearish = (
            trends["1M"] == "نزولی"
            and trends["1w"] == "نزولی"
        )

        signal = "WAIT"
        reason = (
            "شرایط کامل ورود تأیید نشده است. "
            "ورود وسط رنج یا خلاف روند ممنوع است."
        )

        entry = None
        stop = None
        tp1 = None
        tp2 = None

        long_ready = (
            higher_timeframe_bullish
            and trends["1d"] != "نزولی"
            and trends["4h"] == "صعودی"
            and 40 <= rsi_30m <= 68
            and atr_30m > 0
            and price > swing_low + atr_30m
            and volume_ratio >= 0.8
        )

        short_ready = (
            higher_timeframe_bearish
            and trends["1d"] != "صعودی"
            and trends["4h"] == "نزولی"
            and 32 <= rsi_30m <= 60
            and atr_30m > 0
            and price < swing_high - atr_30m
            and volume_ratio >= 0.8
        )

        if long_ready:
            signal = "LONG"
            entry = round(price, 2)
            stop = round(
                swing_low - atr_30m * 0.25,
                2,
            )

            risk = entry - stop
            tp1 = round(entry + risk * 2, 2)
            tp2 = round(entry + risk * 3, 2)

            reason = (
                "روند ماهانه و هفتگی صعودی و ساختار "
                "۴ساعته نیز صعودی است. پس از بسته‌شدن "
                "کندل تأییدی و retest بررسی شود."
            )

        elif short_ready:
            signal = "SHORT"
            entry = round(price, 2)
            stop = round(
                swing_high + atr_30m * 0.25,
                2,
            )

            risk = stop - entry
            tp1 = round(entry - risk * 2, 2)
            tp2 = round(entry - risk * 3, 2)

            reason = (
                "روند ماهانه و هفتگی نزولی و ساختار "
                "۴ساعته نیز نزولی است. پس از بسته‌شدن "
                "کندل تأییدی و retest بررسی شود."
            )

        return jsonify({
            "symbol": SYMBOL,
            "price": price,
            "updated": int(time.time()),
            "trends": trends,
            "signal": signal,
            "reason": reason,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "rsi30m": rsi_30m,
            "atr30m": round(atr_30m, 2),
            "swing_low": round(swing_low, 2),
            "swing_high": round(swing_high, 2),
            "volume_ratio": volume_ratio,
            "risk_note": (
                "این ابزار سفارش باز نمی‌کند. "
                "برای هر معامله بیشتر از ۰.۵ تا ۱ درصد "
                "سرمایه ریسک نکن."
            ),
        })

    except requests.RequestException as error:
        app.logger.error(error)

        return jsonify({
            "error": "دریافت داده از Binance ناموفق بود."
        }), 502

    except Exception as error:
        app.logger.exception(error)

        return jsonify({
            "error": "خطای داخلی هنگام تحلیل بازار"
        }), 500


init_database()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )