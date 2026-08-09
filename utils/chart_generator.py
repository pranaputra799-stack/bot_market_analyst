"""
Chart Generator - Membuat grafik harga secara LOKAL.

Sebelumnya memakai QuickChart.io (layanan eksternal) yang ternyata merender
gambar candlestick KOSONG (0 pixel candle) dan rawan rate-limit. Sekarang chart
digambar langsung di server dan dikirim sebagai file PNG ke Telegram — tanpa
ketergantungan layanan pihak ketiga.

Rendering candlestick (upgrade mplfinance — repo matplotlib/mplfinance, MIT):
- Style dark theme profesional (matching palet warna bot).
- Panel volume di bawah chart + overlay MA (5/10/20) bila data cukup.
- API contract TIDAK berubah: build_candlestick_chart(ohlcv_data, symbol) → path PNG.
- Fallback otomatis ke penggambaran manual (matplotlib Rectangle) bila
  mplfinance tidak terpasang — hasil visual tetap sama baiknya.

Mendukung:
- Candlestick chart (forex, gold, index, crypto)
- Line chart (data time-series)
"""
import logging
import os
import tempfile
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Library plot (matplotlib + pandas + mplfinance ≈ 150MB RAM) di-load SECARA
# LAZY — hanya saat chart pertama diminta. Sebelumnya di-import saat startup
# sehingga bot start lebih lambat & makan memori besar di container kecil.
mpf = None  # type: ignore
pd = None  # type: ignore
plt = None  # type: ignore
Rectangle = None  # type: ignore
FuncFormatter = None  # type: ignore
_PLOT_LOCK = threading.Lock()
_MPF_ATTEMPTED = False


def _ensure_plot_libs() -> bool:
    """Muat matplotlib & mplfinance secara lazy (thread-safe).

    Semua nama module-global (plt, Rectangle, FuncFormatter, mpf, pd) diisi
    di sini dan dipakai fungsi-fungsi penggambar. Test lama yang menonaktifkan
    mplfinance cukup mengeset `chart_generator.mpf = None` — lazy loader TIDAK
    meng-import ulang bila _MPF_ATTEMPTED sudah True.
    """
    global plt, Rectangle, FuncFormatter, mpf, pd, _MPF_ATTEMPTED
    if plt is None:
        with _PLOT_LOCK:
            if plt is None:
                try:
                    # Backend Agg (headless) WAJIB sebelum import pyplot
                    os.environ.setdefault("MPLBACKEND", "Agg")
                    import matplotlib
                    matplotlib.use("Agg", force=True)
                    import matplotlib.pyplot as _plt
                    from matplotlib.patches import Rectangle as _Rectangle
                    from matplotlib.ticker import FuncFormatter as _FuncFormatter
                    plt, Rectangle, FuncFormatter = _plt, _Rectangle, _FuncFormatter
                except Exception as e:
                    logger.error(f"matplotlib gagal dimuat: {e}")
                    return False
    if not _MPF_ATTEMPTED:
        with _PLOT_LOCK:
            if not _MPF_ATTEMPTED:
                _MPF_ATTEMPTED = True
                try:
                    import mplfinance as _mpf  # type: ignore
                    import pandas as _pd  # type: ignore
                    mpf, pd = _mpf, _pd
                except ImportError:  # pragma: no cover - jalur fallback
                    mpf, pd = None, None
    return True

# Nama display untuk setiap simbol
SYMBOL_DISPLAY_NAMES = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "USDCHF=X": "USD/CHF",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "NZDUSD=X": "NZD/USD",
    "GC=F": "XAU/USD (Gold)",
    "SI=F": "XAG/USD (Silver)",
    "USDIDR=X": "USD/IDR",
    "EURIDR=X": "EUR/IDR",
    "GBPIDR=X": "GBP/IDR",
    "BTC-USD": "Bitcoin (BTC/USD)",
    "ETH-USD": "Ethereum (ETH/USD)",
    "DX-Y.NYB": "US Dollar Index (DXY)",
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ",
    "^DJI": "Dow Jones",
    "^VIX": "VIX",
}


class ChartGenerator:
    """
    Generator grafik harga lokal (matplotlib + mplfinance).
    Menghasilkan file PNG yang langsung dikirim ke Telegram.
    """

    # Warna untuk bullish/bearish candle
    CANDLE_UP_COLOR = "#26a69a"   # Hijau (bullish)
    CANDLE_DOWN_COLOR = "#ef5350" # Merah (bearish)
    CHART_BG_COLOR = "#1a1a2e"    # Dark theme background
    GRID_COLOR = "#2a2a4a"        # Grid lines
    TEXT_COLOR = "#e0e0e0"        # Text color

    @staticmethod
    def _get_display_name(symbol: str) -> str:
        """Dapatkan nama display untuk simbol."""
        return SYMBOL_DISPLAY_NAMES.get(symbol, symbol.replace("=X", "").replace("=F", ""))

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """
        Parse tanggal dari berbagai format yfinance:
        "2026-08-05", "2026-08-05 13:00", "2026-08-05T13:00:00Z",
        "2026-08-05T13:00:00+00:00", dll.
        """
        if not date_str:
            return None
        # Normalisasi separator T -> spasi, buang Z / offset timezone
        cleaned = date_str.strip().replace("T", " ").replace("Z", "")
        offset = cleaned.find("+", 10)
        if offset == -1:
            offset = cleaned.find("-", 10)
        if offset != -1:
            cleaned = cleaned[:offset].strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(cleaned, fmt)
            except (ValueError, TypeError):
                continue
        # Fallback terakhir: fromisoformat
        try:
            return datetime.fromisoformat(date_str.strip().replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _price_formatter(decimals: int):
        """Formatter sumbu Y dengan jumlah desimal sesuai instrumen."""
        if FuncFormatter is None:  # pragma: no cover - hanya bila lib belum di-load
            return lambda x, _: f"{x:,.{decimals}f}"
        return FuncFormatter(lambda x, _: f"{x:,.{decimals}f}")

    @staticmethod
    def _decimals_for_price(price: float) -> int:
        """Jumlah desimal yang pas untuk range harga (forex 4-5, indeks 2, dll)."""
        if price < 1:
            return 5
        if price < 20:
            return 4
        if price < 1000:
            return 2
        return 0

    def _style_axes(self, ax):
        """Terapkan dark theme ke axes."""
        ax.set_facecolor(self.CHART_BG_COLOR)
        ax.tick_params(colors=self.TEXT_COLOR, labelsize=10)
        ax.grid(True, color=self.GRID_COLOR, alpha=0.6, linewidth=0.6)
        for spine in ax.spines.values():
            spine.set_color(self.GRID_COLOR)

    def _save_figure(self, fig) -> Optional[str]:
        """
        Simpan figure ke file PNG temp dan return path-nya.
        Gunakan layout ketat agar judul/label tidak terpotong.
        """
        try:
            fig.set_facecolor(self.CHART_BG_COLOR)
            fig.tight_layout(pad=1.2)
            fd, path = tempfile.mkstemp(suffix=".png", prefix="chart_")
            os.close(fd)
            fig.savefig(path, format="png", dpi=130, facecolor=fig.get_facecolor())
            logger.info(f"Chart saved: {path} ({os.path.getsize(path)} bytes)")
            return path
        except Exception as e:
            logger.error(f"Failed to save chart: {e}")
            return None

    # ===================== CANDLESTICK (mplfinance + fallback manual) =====================

    @staticmethod
    def _parse_candles(ohlcv_data: List[Dict], max_points: int) -> List[Dict]:
        """
        Parse OHLCV → list candle {dt, o, h, l, c, v} (urutan asli data).

        Baris dengan tanggal/harga tidak valid dibuang (bukan dibatalkan
        seluruhnya — satu baris rusak tidak boleh menggagalkan chart).
        """
        candles = []
        for row in ohlcv_data[-max_points:]:
            dt = ChartGenerator._parse_date(row.get("date", ""))
            try:
                candles.append({
                    "dt": dt,
                    "o": float(row.get("open", 0)),
                    "h": float(row.get("high", 0)),
                    "l": float(row.get("low", 0)),
                    "c": float(row.get("close", 0)),
                    "v": ChartGenerator._safe_volume(row.get("volume")),
                })
            except (TypeError, ValueError):
                continue
        return candles

    @staticmethod
    def _safe_volume(value) -> float:
        """Volume → float aman (None/NaN/negatif → 0)."""
        try:
            v = float(value)
            return v if v > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def build_candlestick_chart(
        self,
        ohlcv_data: List[Dict],
        symbol: str,
        width: int = 11,
        height: int = 6,
        max_points: int = 40,
    ) -> Optional[str]:
        """
        Generate candlestick chart LOKAL dari data OHLCV.

        Primary: mplfinance (dark theme, panel volume, overlay MA). Bila
        mplfinance tidak tersedia / gagal → fallback penggambaran manual.

        Args:
            ohlcv_data: List of {date, open, high, low, close, volume}
            symbol: Simbol Yahoo Finance (e.g. EURUSD=X)
            width/height: Ukuran figure (inch)
            max_points: Max data points yang digambar

        Returns:
            Path file PNG, atau None jika data tidak cukup / gagal
        """
        if not ohlcv_data or len(ohlcv_data) < 2:
            logger.warning(f"Not enough OHLCV data for {symbol}")
            return None

        candles = self._parse_candles(ohlcv_data, max_points)
        if len(candles) < 2:
            logger.warning(f"Not enough valid candles for {symbol}")
            return None

        if not _ensure_plot_libs():
            logger.error(f"Library plot tidak tersedia untuk {symbol}")
            return None

        if mpf is not None:
            try:
                path = self._build_candlestick_mpf(candles, symbol, width, height)
                if path:
                    return path
                logger.info(f"mplfinance menghasilkan output kosong utk {symbol} — fallback manual")
            except Exception as e:
                logger.warning(f"mplfinance error utk {symbol}, fallback manual: {e}")

        return self._build_candlestick_manual(candles, symbol, width, height)

    def _build_candlestick_mpf(
        self,
        candles: List[Dict],
        symbol: str,
        width: int,
        height: int,
    ) -> Optional[str]:
        """
        Candlestick via mplfinance — dark theme + panel volume + MA overlay.

        Index harus DatetimeIndex naik ketat (tidak ada duplikat / NaT).
        """
        rows = [
            (c["dt"], c["o"], c["h"], c["l"], c["c"], c["v"])
            for c in candles
            if c["dt"] is not None
        ]
        if len(rows) < 2:
            return None

        df = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close", "Volume"])
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        # mplfinance butuh index unik & ascending — duplikat dibuang (ambil terakhir)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df["Volume"] = df["Volume"].fillna(0.0)
        if len(df) < 2:
            return None

        display_name = self._get_display_name(symbol)

        # Style dark theme custom — warna candle mengikuti palet bot
        marketcolors = mpf.make_marketcolors(
            up=self.CANDLE_UP_COLOR,
            down=self.CANDLE_DOWN_COLOR,
            edge="inherit",
            wick="inherit",
            volume="inherit",
        )
        style = mpf.make_mpf_style(
            base_mpf_style="charles",
            marketcolors=marketcolors,
            facecolor=self.CHART_BG_COLOR,
            figcolor=self.CHART_BG_COLOR,
            gridcolor=self.GRID_COLOR,
            gridstyle="--",
            rc={
                "axes.titlecolor": self.TEXT_COLOR,
                "axes.labelcolor": self.TEXT_COLOR,
                "xtick.color": self.TEXT_COLOR,
                "ytick.color": self.TEXT_COLOR,
            },
        )

        # MA overlay hanya bila data cukup (rolling mean butuh window + 1 bar)
        mav = None
        if len(df) >= 30:
            mav = (5, 10, 20)
        elif len(df) >= 12:
            mav = (5, 10)

        fd, path = tempfile.mkstemp(suffix=".png", prefix="chart_")
        os.close(fd)
        try:
            plot_kwargs = dict(
                type="candle",
                style=style,
                volume=True,
                figsize=(width, height),
                tight_layout=True,
                title=display_name,
                ylabel="Harga",
                ylabel_lower="Volume",
                datetime_format="%d/%m",
                xrotation=30,
                savefig=dict(fname=path, dpi=130),
            )
            if mav:
                plot_kwargs["mav"] = mav
            mpf.plot(df, **plot_kwargs)

            if os.path.getsize(path) > 0:
                logger.info(f"Chart saved (mplfinance): {path} ({os.path.getsize(path)} bytes)")
                return path
        except Exception:
            # File temp dibersihkan bila render gagal di tengah jalan
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    def _build_candlestick_manual(
        self,
        candles: List[Dict],
        symbol: str,
        width: int,
        height: int,
    ) -> Optional[str]:
        """
        Fallback penggambaran candlestick manual (matplotlib Rectangle) —
        perilaku lama, dipakai bila mplfinance tidak tersedia/gagal.
        """
        display_name = self._get_display_name(symbol)
        # Gambar dari candle terbaru ke kiri (urutan visual seperti versi lama)
        ordered = list(reversed(candles))

        try:
            fig, ax = plt.subplots(figsize=(width, height))
            n = len(ordered)

            last_price = ordered[-1]["c"]
            decimals = self._decimals_for_price(last_price)

            for i, c in enumerate(ordered):
                color = self.CANDLE_UP_COLOR if c["c"] >= c["o"] else self.CANDLE_DOWN_COLOR
                # Sumbu (wick)
                ax.plot([i, i], [c["l"], c["h"]], color=color, linewidth=1.2, zorder=2)
                # Body candle
                body_lo = min(c["o"], c["c"])
                body_hi = max(c["o"], c["c"])
                body_h = body_hi - body_lo
                if body_h == 0:
                    body_h = max((c["h"] - c["l"]) * 0.05, (c["h"] or 1) * 1e-5)
                ax.add_patch(Rectangle(
                    (i - 0.35, body_lo), 0.7, body_h,
                    facecolor=color, edgecolor=color, linewidth=0.5, zorder=3,
                ))

            # Label sumbu X: tanggal (tampilkan ~6 label)
            tick_step = max(1, n // 6)
            tick_positions = list(range(0, n, tick_step))
            if tick_positions[-1] != n - 1:
                tick_positions.append(n - 1)
            ax.set_xticks(tick_positions)
            ax.set_xticklabels([
                (ordered[i]["dt"].strftime("%d/%m") if ordered[i]["dt"] else str(i))
                for i in tick_positions
            ], rotation=30, ha="right")

            ax.set_title(display_name, color=self.TEXT_COLOR, fontsize=15, fontweight="bold")
            ax.set_ylabel("Harga", color=self.TEXT_COLOR, fontsize=11)
            ax.yaxis.set_major_formatter(self._price_formatter(decimals))
            self._style_axes(ax)

            # Warna label grid + legenda warna candle
            legend_lines = [
                plt.Line2D([0], [0], color=self.CANDLE_UP_COLOR, lw=4, label="Naik"),
                plt.Line2D([0], [0], color=self.CANDLE_DOWN_COLOR, lw=4, label="Turun"),
            ]
            ax.legend(handles=legend_lines, loc="upper left", fontsize=9,
                      facecolor=self.CHART_BG_COLOR, edgecolor=self.GRID_COLOR,
                      labelcolor=self.TEXT_COLOR)

            return self._save_figure(fig)
        except Exception as e:
            logger.error(f"Candlestick chart (manual) failed for {symbol}: {e}")
            return None
        finally:
            try:
                plt.close(fig)
            except Exception:
                pass

    # ===================== LINE CHART (fallback) =====================

    def build_line_chart(
        self,
        data_points: List[float],
        labels: List[str],
        symbol: str,
        width: int = 11,
        height: int = 5.5,
        max_points: int = 40,
    ) -> Optional[str]:
        """
        Generate line chart LOKAL (fallback jika candlestick tidak tersedia).
        """
        if not data_points or len(data_points) < 2:
            return None

        display_name = self._get_display_name(symbol)

        data_points = data_points[-max_points:]
        labels = labels[-max_points:]
        n = len(data_points)

        if not _ensure_plot_libs():
            return None

        try:
            fig, ax = plt.subplots(figsize=(width, height))

            # Deteksi trend utk warna garis
            is_up = data_points[-1] >= data_points[0]
            line_color = self.CANDLE_UP_COLOR if is_up else self.CANDLE_DOWN_COLOR

            ax.plot(range(n), data_points, color=line_color, linewidth=2.2,
                    marker="o", markersize=3, zorder=3)
            ax.fill_between(range(n), data_points, min(data_points),
                            color=line_color, alpha=0.15, zorder=1)

            last_price = data_points[-1]
            decimals = self._decimals_for_price(last_price)

            tick_step = max(1, n // 6)
            tick_positions = list(range(0, n, tick_step))
            if tick_positions[-1] != n - 1:
                tick_positions.append(n - 1)
            ax.set_xticks(tick_positions)
            ax.set_xticklabels([labels[i] for i in tick_positions], rotation=30, ha="right")

            ax.set_title(display_name, color=self.TEXT_COLOR, fontsize=15, fontweight="bold")
            ax.set_ylabel("Harga", color=self.TEXT_COLOR, fontsize=11)
            ax.yaxis.set_major_formatter(self._price_formatter(decimals))
            self._style_axes(ax)

            return self._save_figure(fig)
        except Exception as e:
            logger.error(f"Line chart failed for {symbol}: {e}")
            return None
        finally:
            try:
                plt.close(fig)
            except Exception:
                pass

    # ===================== SYMBOL DETECTION =====================

    @staticmethod
    def get_chart_symbol_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Deteksi simbol dari teks user.

        Args:
            text: Input user (e.g. "chart eurusd", "grafik gold")

        Returns:
            Tuple of (yahoo_symbol, display_name) or (None, None)
        """
        text_lower = text.lower().replace("/chart", "").replace("/grafik", "").strip()

        # Mapping input ke symbol
        keyword_map = {
            "eurusd": "EURUSD=X", "eur/usd": "EURUSD=X", "euro": "EURUSD=X",
            "gbpusd": "GBPUSD=X", "gbp/usd": "GBPUSD=X", "pound": "GBPUSD=X",
            "usdjpy": "USDJPY=X", "usd/jpy": "USDJPY=X",
            "gold": "GC=F", "xauusd": "GC=F", "xau/usd": "GC=F", "emas": "GC=F",
            "silver": "SI=F", "xagusd": "SI=F", "perak": "SI=F",
            "usdidr": "USDIDR=X", "usd/idr": "USDIDR=X", "dolar": "USDIDR=X",
            "btc": "BTC-USD", "bitcoin": "BTC-USD",
            "eth": "ETH-USD", "ethereum": "ETH-USD",
            "dxy": "DX-Y.NYB", "dollar index": "DX-Y.NYB",
            "sp500": "^GSPC", "s&p": "^GSPC", "snp": "^GSPC",
            "nasdaq": "^IXIC",
            "vix": "^VIX",
        }

        # Check exact match
        if text_lower in keyword_map:
            symbol = keyword_map[text_lower]
            return symbol, SYMBOL_DISPLAY_NAMES.get(symbol, symbol)

        # Check partial match
        for keyword, symbol in keyword_map.items():
            if keyword in text_lower:
                return symbol, SYMBOL_DISPLAY_NAMES.get(symbol, symbol)

        return None, None
