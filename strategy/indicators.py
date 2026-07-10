import pandas as pd
import numpy as np


def ema(series, period):
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    """Relative Strength Index — methode Wilder (EMA)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series, fast=12, slow=26, signal=9):
    """MACD line, signal line, et histogramme."""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series, window=20, n_std=2):
    """Bollinger Bands : upper, middle, lower."""
    middle = series.rolling(window=window).mean()
    std = series.rolling(window=window).std()
    upper = middle + n_std * std
    lower = middle - n_std * std
    return upper, middle, lower


def vwap(df):
    """VWAP avec reset journalier.
    Le df doit contenir 'close', 'high', 'low', 'volume', et 'timestamp' (ms).
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    tpv = typical_price * df["volume"]

    if "timestamp" in df.columns:
        days = pd.to_datetime(df["timestamp"], unit="ms").dt.date
        cum_tpv = tpv.groupby(days).cumsum()
        cum_vol = df["volume"].groupby(days).cumsum()
    else:
        cum_tpv = tpv.cumsum()
        cum_vol = df["volume"].cumsum()

    return cum_tpv / cum_vol.replace(0, np.nan)


def atr(df, period=14):
    """Average True Range — mesure la volatilite."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean()


def bb_width(upper, lower, middle):
    """Bollinger Band Width — detecte les squeezes."""
    return (upper - lower) / middle.replace(0, np.nan)


def bb_percent_b(close, upper, lower):
    """Bollinger %B — position relative dans les bandes (0 = lower, 1 = upper)."""
    band_range = upper - lower
    return (close - lower) / band_range.replace(0, np.nan)


def volume_ratio(volume, period=20):
    """Ratio volume actuel / moyenne mobile du volume.
    > 1.5 = spike de volume significatif.
    """
    avg_vol = volume.rolling(window=period).mean()
    return volume / avg_vol.replace(0, np.nan)


def adx(df, period=14):
    """Average Directional Index — mesure la force de la tendance.
    > 20 = tendance, < 20 = range/chop.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_val = atr(df, period)

    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr_val.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr_val.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(span=period, adjust=False).mean()

    return adx_val, plus_di, minus_di


def ema_slope(ema_series, lookback=3):
    """Pente de l'EMA sur N periodes — detecte l'acceleration de tendance.
    Retourne un pourcentage. Protege contre division par zero/NaN."""
    prev = ema_series.shift(lookback).replace(0, float("nan"))
    return (ema_series - prev) / prev * 100


if __name__ == "__main__":
    # Test rapide
    data = pd.DataFrame({
        "open": np.random.uniform(100, 110, 100),
        "high": np.random.uniform(110, 115, 100),
        "low": np.random.uniform(95, 100, 100),
        "close": np.random.uniform(100, 110, 100),
        "volume": np.random.uniform(1000, 5000, 100),
        "timestamp": pd.date_range("2025-01-01", periods=100, freq="1min").astype(int) // 10**6
    })
    print("EMA9:", ema(data["close"], 9).iloc[-1])
    print("RSI:", rsi(data["close"]).iloc[-1])
    m, s, h = macd(data["close"])
    print(f"MACD: {m.iloc[-1]:.4f}, Signal: {s.iloc[-1]:.4f}, Hist: {h.iloc[-1]:.4f}")
    u, mid, l = bollinger_bands(data["close"])
    print(f"BB: upper={u.iloc[-1]:.2f}, mid={mid.iloc[-1]:.2f}, lower={l.iloc[-1]:.2f}")
    print("VWAP:", vwap(data).iloc[-1])
    print("ATR:", atr(data).iloc[-1])
    print("BB Width:", bb_width(u, l, mid).iloc[-1])
    print("Volume Ratio:", volume_ratio(data["volume"]).iloc[-1])
