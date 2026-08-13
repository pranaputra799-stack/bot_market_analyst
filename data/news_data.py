"""
News & Sentiment Data Fetcher - Mengambil berita dan sentimen pasar dari multiple sources.
Sources: Finnhub News (primary), Marketaux, NewsAPI, RSS Feeds.

Berita dan sentimen pasar adalah komponen krusial untuk analisis fundamental.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from data.http_session import get_aiohttp_session

from config.settings import FINNHUB_KEY, MARKETAUX_KEY, NEWSAPI_KEY, MORNING_BRIEF_TIMEZONE
from data.cache import cache

logger = logging.getLogger(__name__)


class NewsFetcher:
    """
    Fetcher untuk berita dan data sentimen pasar.
    """

    def __init__(self):
        self.finnhub_key = FINNHUB_KEY
        self.marketaux_key = MARKETAUX_KEY
        self.newsapi_key = NEWSAPI_KEY

    # ===================== FINNHUB NEWS (Primary) =====================

    async def get_finnhub_news(self, symbol: str = "FOREX", limit: int = 5) -> Dict:
        """
        Ambil berita dari Finnhub dengan sentiment scoring.

        Args:
            symbol: Kode untuk filter berita (FOREX, XAUUSD, GC, umum)
            limit: Jumlah berita maksimal

        Returns:
            Dict dengan berita dan sentimen
        """
        if not self.finnhub_key:
            return {"source": "Finnhub", "error": "No API key configured", "articles": []}

        try:
            # Mapping symbol for Finnhub
            symbol_map = {
                "GC=F": "XAUUSD",
                "EURUSD=X": "EUR/USD",
                "GBPUSD=X": "GBP/USD",
                "USDJPY=X": "USD/JPY",
                "FOREX": "FOREX",
            }
            fh_symbol = symbol_map.get(symbol, symbol)

            url = "https://finnhub.io/api/v1/news"
            params = {
                "category": fh_symbol,
                "token": self.finnhub_key,
                "minId": 0,
            }

            session = get_aiohttp_session()
            async with session.get(url, params=params, timeout=15) as resp:
                data = await resp.json()

            if not isinstance(data, list):
                return {"source": "Finnhub", "articles": []}

            articles = []
            for item in data[:limit]:
                sentiment = item.get("sentiment", 0)
                sentiment_label = "🟢 Positif" if sentiment > 0.2 else "🔴 Negatif" if sentiment < -0.2 else "⚪ Netral"

                # Finnhub mengirim timestamp epoch UTC. Nilai bisa berupa int/float,
                # tapi kadang string — konversi aman agar fromtimestamp tidak TypeError.
                raw_ts = item.get("datetime", 0)
                try:
                    ts = int(raw_ts)
                except (TypeError, ValueError):
                    ts = 0

                articles.append({
                    "headline": item.get("headline", ""),
                    "summary": item.get("summary", "")[:200],
                    "url": item.get("url", ""),
                    "source": item.get("source", ""),
                    # Konversi ke WIB agar waktu berita sesuai jam Indonesia,
                    # bukan waktu server.
                    "datetime": datetime.fromtimestamp(
                        ts, tz=ZoneInfo(MORNING_BRIEF_TIMEZONE)
                    ).strftime("%Y-%m-%d %H:%M WIB") if ts else "",
                    # Simpan raw epoch untuk pembobotan kedekatan waktu di sentiment analyzer
                    "datetime_ts": ts,
                    "sentiment": sentiment,
                    "sentiment_label": sentiment_label,
                    "related": item.get("related", ""),
                })

            # Average sentiment
            sentiments = [a["sentiment"] for a in articles if a["sentiment"] != 0]
            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0
            overall = "Positif 🟢" if avg_sentiment > 0.1 else "Negatif 🔴" if avg_sentiment < -0.1 else "Netral ⚪"

            return {
                "source": "Finnhub",
                "symbol": symbol,
                "total_articles": len(articles),
                "average_sentiment": round(avg_sentiment, 3),
                "overall_sentiment": overall,
                "articles": articles,
                "last_updated": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.warning(f"Finnhub news error: {e}")
            return {"source": "Finnhub", "error": str(e), "articles": []}

    # ===================== MARKETAUX NEWS (Secondary) =====================

    async def get_marketaux_news(self, symbols: str = "XAUUSD,EURUSD", limit: int = 5) -> Dict:
        """Ambil berita dari Marketaux dengan entity recognition."""
        if not self.marketaux_key:
            return {"source": "Marketaux", "error": "No API key configured", "articles": []}

        try:
            url = "https://api.marketaux.com/v1/news/all"
            params = {
                "symbols": symbols,
                "filter_entities": "true",
                "limit": limit,
                "published_after": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                "api_token": self.marketaux_key,
            }

            session = get_aiohttp_session()
            async with session.get(url, params=params, timeout=15) as resp:
                data = await resp.json()

            articles = []
            for item in data.get("data", []):
                articles.append({
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", ""),
                    "published_at": item.get("published_at", ""),
                    "entities": [e.get("name") for e in item.get("entities", [])],
                })

            return {
                "source": "Marketaux",
                "symbols": symbols,
                "total_articles": len(articles),
                "articles": articles,
            }

        except Exception as e:
            logger.warning(f"Marketaux error: {e}")
            return {"source": "Marketaux", "error": str(e), "articles": []}

    # ===================== RSS FEEDS =====================

    async def get_rss_news(self, limit: int = 5) -> List[Dict]:
        """Ambil berita dari RSS feeds Forex Factory dan Investing.com."""
        rss_feeds = [
            "https://www.investing.com/rss/news.rss",
            "https://www.forexfactory.com/news.xml",
        ]

        articles = []
        session = get_aiohttp_session()
        for feed_url in rss_feeds:
            try:
                async with session.get(feed_url, timeout=10) as resp:
                    content = await resp.text()

                root = ET.fromstring(content)
                for item in root.iter("item") if root.tag == "rss" else root.iter("entry"):
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "")
                    pub_date = item.findtext("pubDate", "")

                    if title:
                        articles.append({
                            "title": title,
                            "description": desc[:200],
                            "url": link,
                            "source": feed_url.split("/")[2],
                            "published": pub_date,
                        })

                    if len(articles) >= limit:
                        break

            except Exception as e:
                logger.warning(f"RSS error for {feed_url}: {e}")

        return articles[:limit]

    # ===================== NEWS SUMMARY =====================

    async def get_news_summary(self, symbol: str = "FOREX") -> str:
        """
        Mendapatkan ringkasan berita untuk morning brief.
        Menggabungkan dari multiple sources.
        """
        # Manual cache untuk async function
        cache_key = f"news_summary:{symbol}"
        cached_result = cache.get(cache_key)
        if cached_result:
            return cached_result

        finnhub = await self.get_finnhub_news(symbol, limit=3)
        lines = []

        # Finnhub news
        if "articles" in finnhub and finnhub["articles"]:
            lines.append("📰 *BERITA TERKINI*")
            lines.append(f"Sentimen Keseluruhan: {finnhub.get('overall_sentiment', 'N/A')}\n")
            for article in finnhub["articles"]:
                sentiment_str = article.get("sentiment_label", "")
                lines.append(f"{sentiment_str} *{article['headline']}*")
                if article.get("summary"):
                    lines.append(f"  _{article['summary'][:150]}..._")
                lines.append(f"  Sumber: {article.get('source', 'Unknown')} | [{article.get('datetime', '')}]")
                lines.append("")

        # Marketaux (jika ada)
        if self.marketaux_key:
            marketaux = await self.get_marketaux_news(limit=2)
            if "articles" in marketaux and marketaux["articles"]:
                for art in marketaux["articles"]:
                    lines.append(f"📌 *{art['title']}*")
                    if art.get("entities"):
                        lines.append(f"  Terkait: {', '.join(art['entities'][:3])}")
                    lines.append("")

        if not lines:
            lines.append("📰 *BERITA*")
            lines.append("Tidak ada berita terkini yang tersedia saat ini.")

        result = "\n".join(lines)
        # Cache result for 10 minutes
        cache.set(cache_key, result, 600)
        return result
