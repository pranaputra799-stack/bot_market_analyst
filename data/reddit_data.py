"""
Reddit Data Fetcher - Mengambil berita dan sentimen dari Reddit secara GRATIS.

Reddit menyediakan akses JSON publik tanpa API key untuk data baca.
Cocok untuk mendapatkan sentimen komunitas dari subreddit forex, crypto, wallstreetbets.

Cara kerja:
- Reddit endpoint JSON: https://www.reddit.com/r/{subreddit}/hot.json
- User-Agent wajib di-set (Reddit block bot tanpa User-Agent)
- Rate limit: 1 request per detik (untuk anonim)
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from data.http_session import get_aiohttp_session
from data.cache import cache

logger = logging.getLogger(__name__)

# Subreddit yang relevan untuk analisis pasar finansial
RELEVANT_SUBREDDITS = {
    "forex": "r/forex",
    "wallstreetbets": "r/wallstreetbets",
    "investing": "r/investing",
    "stocks": "r/stocks",
    "gold": "r/Gold",
    "bitcoin": "r/Bitcoin",
    "cryptocurrency": "r/CryptoCurrency",
    "economics": "r/Economics",
}

# Mapping instrumen → subreddit yang paling relevan
INSTRUMENT_SUBREDDITS = {
    "FOREX": ["forex", "wallstreetbets"],
    "XAUUSD": ["gold", "wallstreetbets", "investing"],
    "GC=F": ["gold", "wallstreetbets"],
    "EURUSD=X": ["forex", "investing"],
    "GBPUSD=X": ["forex", "investing"],
    "USDJPY=X": ["forex"],
    "BTC-USD": ["bitcoin", "cryptocurrency", "wallstreetbets"],
    "ETH-USD": ["cryptocurrency", "ethereum"],
}


class RedditFetcher:
    """
    Fetcher untuk data Reddit (berita + sentimen komunitas).

    Menggunakan Reddit's public JSON API (tanpa API key).
    Akses anonim terbatas: rate limit ~1 request/detik, tapi untuk
    bot yang hanya fetch beberapa kali per 10 menit, ini cukup.
    """

    # User-Agent untuk Reddit (WAJIB — Reddit block request tanpa UA)
    USER_AGENT = "Mozilla/5.0 (compatible; MarketAI-Bot/1.0; +https://github.com/marketai)"

    # Search queries untuk financial topics (fallback bila subreddit tidak cukup)
    SEARCH_QUERIES = {
        "FOREX": "forex market gold dollar",
        "XAUUSD": "gold price XAU",
        "GC=F": "gold futures price",
        "EURUSD=X": "EUR USD forex",
        "BTC-USD": "bitcoin BTC price",
        "ETH-USD": "ethereum ETH price",
    }

    def __init__(self):
        self.base_url = "https://www.reddit.com"

    async def get_subreddit_posts(
        self,
        subreddit: str,
        sort: str = "hot",
        limit: int = 5,
        time_filter: str = "day",
    ) -> List[Dict]:
        """
        Ambil post terbaru dari subreddit tertentu.

        Args:
            subreddit: Nama subreddit (tanpa 'r/')
            sort: 'hot', 'new', 'top', 'rising'
            limit: Jumlah post maksimal
            time_filter: Filter waktu untuk 'top' (hour, day, week, month, year, all)

        Returns:
            List of dict dengan data post
        """
        cache_key = f"reddit:{subreddit}:{sort}:{limit}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        try:
            session = get_aiohttp_session()
            url = f"{self.base_url}/r/{subreddit}/{sort}.json"
            params = {"limit": limit, "t": time_filter}
            headers = {"User-Agent": self.USER_AGENT}

            async with session.get(url, params=params, headers=headers, timeout=15) as resp:
                if resp.status == 429:
                    logger.warning(f"Reddit rate limit for r/{subreddit}")
                    return []
                if resp.status != 200:
                    logger.warning(f"Reddit r/{subreddit} returned {resp.status}")
                    return []
                data = await resp.json()

            posts = []
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                if post.get("stickied"):  # Skip pinned posts
                    continue
                posts.append({
                    "title": post.get("title", ""),
                    "selftext": (post.get("selftext", "") or "")[:300],
                    "score": post.get("score", 0),  # upvotes - downvotes
                    "num_comments": post.get("num_comments", 0),
                    "url": post.get("url", ""),
                    "permalink": f"https://reddit.com{post.get('permalink', '')}",
                    "author": post.get("author", ""),
                    "subreddit": post.get("subreddit", subreddit),
                    "created_utc": datetime.utcfromtimestamp(
                        post.get("created_utc", 0)
                    ).strftime("%Y-%m-%d %H:%M UTC") if post.get("created_utc") else "",
                    "upvote_ratio": post.get("upvote_ratio", 0),
                    "is_self": post.get("is_self", False),
                    # Sentiment proxy: score positif = bullish, negatif = bearish
                    # upvote_ratio > 0.7 = konsensus kuat
                    "sentiment_proxy": self._compute_sentiment_proxy(post),
                })

            # Cache 10 menit
            cache.set(cache_key, posts, 600)
            return posts

        except Exception as e:
            logger.warning(f"Reddit fetch error for r/{subreddit}: {e}")
            return []

    async def search_reddit(
        self,
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        limit: int = 5,
        time_filter: str = "day",
    ) -> List[Dict]:
        """
        Cari post di Reddit berdasarkan query.

        Args:
            query: Kata kunci pencarian
            subreddit: Filter ke subreddit tertentu (opsional)
            sort: 'relevance', 'hot', 'new', 'top'
            limit: Jumlah post maksimal
            time_filter: Filter waktu

        Returns:
            List of dict dengan data post
        """
        cache_key = f"reddit_search:{query}:{subreddit}:{sort}:{limit}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        try:
            session = get_aiohttp_session()
            if subreddit:
                url = f"{self.base_url}/r/{subreddit}/search.json"
                params = {"q": query, "restrict_sr": "on", "sort": sort, "t": time_filter, "limit": limit}
            else:
                url = f"{self.base_url}/search.json"
                params = {"q": query, "sort": sort, "t": time_filter, "limit": limit}

            headers = {"User-Agent": self.USER_AGENT}

            async with session.get(url, params=params, headers=headers, timeout=15) as resp:
                if resp.status == 429:
                    logger.warning(f"Reddit rate limit for search '{query}'")
                    return []
                if resp.status != 200:
                    logger.warning(f"Reddit search returned {resp.status}")
                    return []
                data = await resp.json()

            posts = []
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                if post.get("stickied"):
                    continue
                posts.append({
                    "title": post.get("title", ""),
                    "selftext": (post.get("selftext", "") or "")[:300],
                    "score": post.get("score", 0),
                    "num_comments": post.get("num_comments", 0),
                    "url": post.get("url", ""),
                    "permalink": f"https://reddit.com{post.get('permalink', '')}",
                    "author": post.get("author", ""),
                    "subreddit": post.get("subreddit", ""),
                    "created_utc": datetime.utcfromtimestamp(
                        post.get("created_utc", 0)
                    ).strftime("%Y-%m-%d %H:%M UTC") if post.get("created_utc") else "",
                    "upvote_ratio": post.get("upvote_ratio", 0),
                    "sentiment_proxy": self._compute_sentiment_proxy(post),
                })

            cache.set(cache_key, posts, 600)
            return posts

        except Exception as e:
            logger.warning(f"Reddit search error for '{query}': {e}")
            return []

    async def get_financial_sentiment(self, symbol: str = "FOREX", limit: int = 5) -> Dict:
        """
        Ambil sentimen finansial dari Reddit untuk instrumen tertentu.

        Menggabungkan post dari subreddit relevan + search results.

        Args:
            symbol: Simbol instrumen
            limit: Jumlah post per subreddit

        Returns:
            Dict dengan aggregated sentiment dan posts
        """
        cache_key = f"reddit_sentiment:{symbol}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        subreddits = INSTRUMENT_SUBREDDITS.get(symbol, ["forex", "wallstreetbets"])
        all_posts = []

        # Ambil dari subreddit yang relevan
        for sub in subreddits[:2]:  # Max 2 subreddit agar tidak spam
            posts = await self.get_subreddit_posts(sub, sort="hot", limit=limit)
            all_posts.extend(posts)

        # Jika kurang dari 3 post, coba search
        if len(all_posts) < 3 and symbol in self.SEARCH_QUERIES:
            query = self.SEARCH_QUERIES[symbol]
            search_posts = await self.search_reddit(query, limit=limit)
            all_posts.extend(search_posts)

        # Aggregate sentiment
        if not all_posts:
            result = {
                "source": "Reddit",
                "symbol": symbol,
                "total_posts": 0,
                "average_sentiment": 0,
                "overall_sentiment": "Netral ⚪",
                "top_posts": [],
                "error": None,
            }
            cache.set(cache_key, result, 600)
            return result

        sentiments = [p["sentiment_proxy"] for p in all_posts]
        avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0
        overall = (
            "Positif 🟢" if avg_sentiment > 0.1
            else "Negatif 🔴" if avg_sentiment < -0.1
            else "Netral ⚪"
        )

        # Sort by score (engagement) dan ambil top
        sorted_posts = sorted(all_posts, key=lambda x: x.get("score", 0), reverse=True)

        result = {
            "source": "Reddit",
            "symbol": symbol,
            "subreddits_queried": subreddits,
            "total_posts": len(all_posts),
            "average_sentiment": round(avg_sentiment, 3),
            "overall_sentiment": overall,
            "top_posts": sorted_posts[:5],
            "last_updated": datetime.now().isoformat(),
        }

        cache.set(cache_key, result, 600)
        return result

    @staticmethod
    def _compute_sentiment_proxy(post: Dict) -> float:
        """
        Hitung sentiment proxy dari Reddit post data.

        Menggunakan kombinasi:
        - upvote_ratio (> 0.7 = positif, < 0.3 = negatif)
        - score relatif (pos = bullish, neg = bearish)
        - engagement density (comments/posts yang banyak = conviction kuat)

        Returns:
            float antara -1.0 (sangat bearish) s/d 1.0 (sangat bullish)
        """
        ratio = post.get("upvote_ratio", 0.5)
        score = post.get("score", 0)

        # Score contribution: positif = bullish, negatif = bearish
        # Normalisasi dengan log agar score besar tidak mendominasi
        import math
        if score > 0:
            score_component = min(0.5, math.log1p(score) / 20)
        elif score < 0:
            score_component = max(-0.5, -math.log1p(abs(score)) / 20)
        else:
            score_component = 0.0

        # Ratio contribution
        ratio_component = (ratio - 0.5) * 2  # -1 to +1, tapi dibatasi

        # Weighted average
        sentiment = 0.4 * ratio_component + 0.6 * score_component
        return max(-1.0, min(1.0, sentiment))

    def format_report(self, result: Dict, display_name: str = "") -> str:
        """Format laporan sentimen Reddit untuk Telegram."""
        if result.get("error"):
            return f"📱 *Reddit Sentiment* — {display_name or result.get('symbol', '')}\n\n⚠️ Data tidak tersedia saat ini."

        score = result.get("average_sentiment", 0)
        count = result.get("total_posts", 0)
        overall = result.get("overall_sentiment", "Netral ⚪")
        subs = result.get("subreddits_queried", [])

        lines = [
            f"📱 *REDDIT SENTIMENT — {display_name or result.get('symbol', '')}*",
            f"📊 Skor: *{score:+.2f}* ({overall})",
            f"📝 {count} post dari {', '.join(f'r/{s}' for s in subs)}",
            "",
        ]

        top_posts = result.get("top_posts", [])
        if top_posts:
            lines.append("*Post Populer:*")
            for p in top_posts[:3]:
                title = p.get("title", "")[:80]
                s = p.get("sentiment_proxy", 0)
                icon = "🟢" if s > 0.1 else "🔴" if s < -0.1 else "⚪"
                lines.append(f"• {icon} {title}")
                lines.append(f"  _{p.get('score', 0)} upvotes, {p.get('num_comments', 0)} comments_")
            lines.append("")

        lines.append("---")
        lines.append("⚠️ *Disclaimer:* Sentimen Reddit = opini publik, bukan sinyal trading.")
        return "\n".join(lines)
