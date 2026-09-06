# core/services/spotify_extractor.py

import os
import base64
import time
import logging
import requests
from urllib.parse import urlparse
from core.config import Config

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

class SpotifyExtractorService:
    """
    سرویس جامع استخراج اطلاعات، متادیتا و فول دیسکوگرافی خوانندگان از اسپاتیفای
    با پشتیبانی از توکن رسمی Client Credentials و سیستم کش داخلی.
    """
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json'
        })
        self._token = None
        self._expires_at = 0

    def get_access_token(self):
        """دریافت و کش کردن توکن دسترسی اسپاتیفای"""
        now = time.time()
        if self._token and self._expires_at > now + 60:
            return self._token

        client_id = Config.SPOTIFY_CLIENT_ID or os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = Config.SPOTIFY_CLIENT_SECRET or os.getenv("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            logger.warning("SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET is missing.")
            return None

        try:
            basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            resp = self.session.post(
                TOKEN_URL,
                headers={"Authorization": f"Basic {basic}"},
                data={"grant_type": "client_credentials"},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error(f"Spotify token request failed ({resp.status_code}): {resp.text}")
                return None

            data = resp.json()
            self._token = data.get("access_token")
            self._expires_at = now + data.get("expires_in", 3600)
            return self._token
        except Exception as e:
            logger.error(f"Error fetching Spotify access token: {e}")
            return None

    def api_get(self, url, params=None):
        """درخواست ایمن به اسپاتیفای با ریت‌لیمیت خودکار"""
        token = self.get_access_token()
        if not token:
            raise RuntimeError("Spotify API credentials not configured or failed to authenticate.")

        for attempt in range(5):
            try:
                resp = self.session.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    timeout=25,
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", "2")) + 1
                    logger.warning(f"Spotify rate limited. Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    time.sleep(1 + attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt == 4:
                    raise RuntimeError(f"Failed to fetch {url} after retries: {e}")
                time.sleep(1)

        raise RuntimeError(f"Failed to fetch {url} after 5 retries")

    def search_artist(self, query, limit=6):
        """جستجوی سریع خواننده بر اساس نام فارسی یا انگلیسی در اسپاتیفای"""
        query = (query or "").strip()
        if not query:
            return []
        data = self.api_get(f"{API_BASE}/search", params={
            "q": query,
            "type": "artist",
            "limit": limit
        })
        artists = []
        for a in data.get("artists", {}).get("items", []):
            images = a.get("images", [])
            artists.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "image": images[0]["url"] if images else None,
                "followers": (a.get("followers") or {}).get("total", 0),
                "genres": a.get("genres", []),
                "spotify_url": (a.get("external_urls") or {}).get("spotify")
            })
        return artists

    def suggest_artists_by_genre(self, genre_query, limit=12):
        """پیشنهاد خودکار هنرمندان برتر یک سبک یا کلیدواژه از اسپاتیفای"""
        genre_query = (genre_query or "").strip()
        if not genre_query:
            return []
        
        try:
            data = self.api_get(f"{API_BASE}/search", params={
                "q": genre_query,
                "type": "artist",
                "limit": limit
            })
            items = data.get("artists", {}).get("items", [])
        except Exception:
            return []

        artists = []
        for a in items:
            images = a.get("images", [])
            artists.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "image": images[0]["url"] if images else None,
                "followers": (a.get("followers") or {}).get("total", 0),
                "genres": a.get("genres", []),
                "spotify_url": (a.get("external_urls") or {}).get("spotify")
            })
        return artists

    def parse_spotify_link(self, link):
        """تشخیص نوع لینک (آرتیست، پلی‌لیست، آلبوم، ترک) و استخراج شناسه ID"""
        link = (link or "").strip()
        if not link:
            raise ValueError("URL cannot be empty")

        if link.startswith("spotify:"):
            parts = link.split(":")
            if len(parts) >= 3:
                return parts[1], parts[2]

        if "open.spotify.com" in link or link.startswith("http"):
            segments = [s for s in urlparse(link).path.split("/") if s]
            for t in ["artist", "playlist", "album", "track"]:
                if t in segments:
                    idx = segments.index(t)
                    if idx + 1 < len(segments):
                        return t, segments[idx + 1]

        # اگر رشته فقط ۲۲ کاراکتر عددی/حروفی باشد
        if len(link) == 22 and link.isalnum():
            return "artist", link

        raise ValueError(f"Could not recognize a valid Spotify URL or ID: {link}")

    def fetch_artist_discography(self, artist_id, deduplicate_by_name=True, max_limit=250):
        """
        واکشی کامل دیسکوگرافی آرتیست شامل تمام آلبوم‌ها و سینگل‌ها
        با قابلیت تجمیع بالک، حذف آهنگ‌های همنام و استخراج کاورها.
        سقف پیش‌فرض ۲۵۰ قطعه برای جلوگیری از ارکسترهای ۱۸ هزار قطعه‌ای و هنگ کردن صف.
        """
        # ۱. اطلاعات پروفایل خواننده
        artist_data = self.api_get(f"{API_BASE}/artists/{artist_id}")
        artist_name = artist_data.get("name", "Unknown Artist")
        artist_images = artist_data.get("images", [])
        artist_image = artist_images[0]["url"] if artist_images else None

        # ۲. واکشی لیست تمام آلبوم‌ها و سینگل‌ها با Pagination
        albums = []
        offset = 0
        max_albums_to_scan = 50  # حداکثر ۵۰ آلبوم/سینگل برای جلوگیری از حلقه‌های بی‌پایان
        while True:
            res = self.api_get(
                f"{API_BASE}/artists/{artist_id}/albums",
                params={"include_groups": "album,single", "limit": 50, "offset": offset}
            )
            items = res.get("items", [])
            albums.extend(items)
            if len(items) < 50 or offset + 50 >= res.get("total", 0) or len(albums) >= max_albums_to_scan:
                break
            offset += 50

        # ۳. دریافت بالک ترک‌های آلبوم‌ها (در دسته‌های ۲۰ تایی برای حداکثر سرعت)
        all_tracks = []
        seen_ids = set()
        seen_titles = set()

        for i in range(0, len(albums), 20):
            chunk = albums[i:i+20]
            ids = ",".join(alb["id"] for alb in chunk)
            bulk = self.api_get(f"{API_BASE}/albums", params={"ids": ids})
            
            for alb in bulk.get("albums", []):
                if not alb:
                    continue
                alb_id = alb.get("id")
                alb_name = alb.get("name")
                alb_type = alb.get("album_type")
                release_date = alb.get("release_date")
                cover_img = alb.get("images", [{}])[0].get("url") if alb.get("images") else None

                for trk in alb.get("tracks", {}).get("items", []):
                    t_id = trk.get("id")
                    t_name = trk.get("name", "").strip()
                    if not t_id or not t_name:
                        continue

                    # فیلتر تکراری بر اساس نام یا ID
                    if deduplicate_by_name:
                        norm = t_name.lower()
                        if norm in seen_titles:
                            continue
                        seen_titles.add(norm)
                    else:
                        if t_id in seen_ids:
                            continue
                        seen_ids.add(t_id)

                    duration_ms = trk.get("duration_ms", 0)
                    sec = round(duration_ms / 1000)
                    duration_str = f"{sec // 60}:{sec % 60:02d}"

                    all_tracks.append({
                        "id": t_id,
                        "title": t_name,
                        "duration_ms": duration_ms,
                        "duration_seconds": sec,
                        "duration_formatted": duration_str,
                        "track_number": trk.get("track_number"),
                        "disc_number": trk.get("disc_number"),
                        "explicit": trk.get("explicit", False),
                        "spotify_url": (trk.get("external_urls") or {}).get("spotify"),
                        "preview_url": trk.get("preview_url"),
                        "artists": [a.get("name") for a in trk.get("artists", [])],
                        "artist_string": ", ".join(a.get("name") for a in trk.get("artists", [])) if trk.get("artists") else artist_name,
                        "album": {
                            "id": alb_id,
                            "name": alb_name,
                            "type": alb_type,
                            "release_date": release_date,
                            "cover_url": cover_img
                        }
                    })

                    if max_limit and len(all_tracks) >= max_limit:
                        break
            if max_limit and len(all_tracks) >= max_limit:
                break

        return {
            "artist_id": artist_id,
            "artist_name": artist_name,
            "artist_image": artist_image,
            "artist_url": (artist_data.get("external_urls") or {}).get("spotify"),
            "genres": artist_data.get("genres", []),
            "followers": (artist_data.get("followers") or {}).get("total", 0),
            "total_albums": len(albums),
            "total_tracks": len(all_tracks),
            "tracks": all_tracks
        }

    def fetch_playlist_tracks(self, playlist_id, max_limit=None):
        """استخراج تمام آهنگ‌های یک پلی‌لیست با متادیتا و کاورها"""
        pl_data = self.api_get(f"{API_BASE}/playlists/{playlist_id}")
        pl_name = pl_data.get("name", "Custom Playlist")
        pl_images = pl_data.get("images", [])
        pl_image = pl_images[0]["url"] if pl_images else None

        items = []
        offset = 0
        while True:
            page = self.api_get(
                f"{API_BASE}/playlists/{playlist_id}/tracks",
                params={"limit": 100, "offset": offset}
            )
            batch = page.get("items", [])
            items.extend(batch)
            total = page.get("total", len(items))
            offset += 100
            if len(batch) < 100 or offset >= total or (max_limit and len(items) >= max_limit):
                break

        tracks = []
        for item in items:
            trk = item.get("track")
            if not trk or trk.get("type") == "episode" or not trk.get("id"):
                continue

            duration_ms = trk.get("duration_ms", 0)
            sec = round(duration_ms / 1000)
            duration_str = f"{sec // 60}:{sec % 60:02d}"
            alb = trk.get("album") or {}
            cover_img = alb.get("images", [{}])[0].get("url") if alb.get("images") else None

            tracks.append({
                "id": trk.get("id"),
                "title": trk.get("name"),
                "duration_ms": duration_ms,
                "duration_seconds": sec,
                "duration_formatted": duration_str,
                "spotify_url": (trk.get("external_urls") or {}).get("spotify"),
                "artists": [a.get("name") for a in trk.get("artists", [])],
                "artist_string": ", ".join(a.get("name") for a in trk.get("artists", [])) if trk.get("artists") else "Various Artists",
                "album": {
                    "id": alb.get("id"),
                    "name": alb.get("name"),
                    "cover_url": cover_img,
                    "release_date": alb.get("release_date")
                }
            })
            if max_limit and len(tracks) >= max_limit:
                break

        return {
            "playlist_id": playlist_id,
            "title": pl_name,
            "image": pl_image,
            "total_tracks": len(tracks),
            "tracks": tracks
        }

    def fetch_related_artists(self, artist_id):
        """
        کشف آرتیست‌های هم‌سبک و مرتبط بر اساس متدهای استاندارد و فعال اسپاتیفای
        (جایگزین اندپوینت منسوخ‌شده related-artists که از نوامبر ۲۰۲۴ توسط اسپاتیفای ۴۰۴ می‌دهد)
        """
        try:
            art = self.api_get(f"{API_BASE}/artists/{artist_id}")
            if not art:
                return []
            
            art_name = art.get("name", "")
            genres = art.get("genres", [])
            
            # ۱. جستجو بر اساس ژانر تخصصی خواننده در صورت وجود
            peers = []
            if genres:
                peers = self.suggest_artists_by_genre(f'genre:"{genres[0]}"', limit=12)

            # ۲. در صورت خالی بودن ژانر (مانند اکثر خوانندگان ایرانی)، جستجو بر اساس کلیدواژه نام خواننده
            if not peers and art_name:
                peers = self.suggest_artists_by_genre(art_name, limit=12)

            # حذف خود خواننده از لیست نتایج
            results = [p for p in peers if p.get("id") != artist_id and p.get("name", "").lower() != art_name.lower()]
            return results
        except Exception as e:
            logger.warning(f"Error discovering peer artists for {artist_id}: {e}")
            return []

    def fetch_live_curated_radar(self):
        """
        رادار زنده کشف و دسته‌بندی هوشمند برترین هنرمندان و پلی‌لیست‌های ترند از اسپاتیفای
        با قابلیت خواندن دسته‌بندی‌ها و کلیدواژه‌های سفارشی از دیتابیس.
        """
        import sqlite3
        radar_categories = []
        
        try:
            with sqlite3.connect(Config.DATABASE_URI) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM radar_categories ORDER BY is_default DESC, id ASC").fetchall()
                for r in rows:
                    raw_q = r['search_queries'] or ''
                    queries = [q.strip() for q in raw_q.split(',') if q.strip()]
                    if queries:
                        radar_categories.append({
                            "id": r['category_key'],
                            "title": r['title'],
                            "subtitle": r['subtitle'] or '',
                            "is_default": bool(r['is_default']),
                            "queries": queries
                        })
        except Exception as e:
            logger.warning(f"Could not load radar categories from DB, falling back: {e}")

        # در صورت خالی بودن جدول، خواندن مستقیم از فایل جیسون
        if not radar_categories:
            try:
                import json
                from pathlib import Path
                json_path = Path(__file__).resolve().parent.parent / "data" / "radar_categories.json"
                if json_path.exists():
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    for cat in data.get("categories", []):
                        radar_categories.append({
                            "id": cat.get("key"),
                            "title": cat.get("title"),
                            "subtitle": cat.get("subtitle") or '',
                            "is_default": bool(cat.get("is_default")),
                            "queries": cat.get("queries", [])
                        })
            except Exception as e:
                logger.warning(f"Fallback to radar_categories.json failed: {e}")

        # واکشی لیست مگا پلی‌لیست‌ها از فایل جیسون
        featured_playlists_queries = []
        try:
            import json
            from pathlib import Path
            json_path = Path(__file__).resolve().parent.parent / "data" / "radar_categories.json"
            if json_path.exists():
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                featured_playlists_queries = data.get("featured_playlists", [])
        except Exception:
            pass

        if not featured_playlists_queries:
            featured_playlists_queries = [
                {"query": "Persian Pop Essentials", "category": "Persian Hits"},
                {"query": "Top 50 Global", "category": "Charts"},
                {"query": "Today's Top Hits", "category": "Charts"},
                {"query": "Persian Hip Hop", "category": "Persian Rap"},
                {"query": "Nostalgia Persian", "category": "Classics"},
                {"query": "Billboard Hot 100", "category": "Global Hits"},
                {"query": "Viral 50 Global", "category": "Viral"}
            ]

        output = {
            "categories": [],
            "playlists": []
        }

        from concurrent.futures import ThreadPoolExecutor

        # ۱. واکشی موازی و پرسرعت هنرمندان
        with ThreadPoolExecutor(max_workers=8) as executor:
            for cat in radar_categories:
                futures = [executor.submit(self.search_artist, q, 1) for q in cat["queries"]]
                cat_artists = []
                for f in futures:
                    try:
                        res = f.result(timeout=10)
                        if res:
                            cat_artists.append(res[0])
                    except Exception:
                        pass

                output["categories"].append({
                    "id": cat["id"],
                    "title": cat["title"],
                    "subtitle": cat["subtitle"],
                    "total_artists": len(cat_artists),
                    "artists": cat_artists
                })

            # ۲. واکشی موازی پلی‌لیست‌ها
            def _fetch_pl(pl_q):
                try:
                    res = self.api_get(f"{API_BASE}/search", params={"q": pl_q["query"], "type": "playlist", "limit": 5})
                    items = [p for p in res.get("playlists", {}).get("items", []) if p]
                    if items:
                        p = items[0]
                        imgs = p.get("images", [])
                        return {
                            "id": p.get("id"),
                            "title": p.get("name"),
                            "category": pl_q["category"],
                            "description": p.get("description", ""),
                            "image": imgs[0]["url"] if imgs else None,
                            "owner": (p.get("owner") or {}).get("display_name", "Spotify"),
                            "tracks_count": (p.get("tracks") or {}).get("total", 50),
                            "spotify_url": (p.get("external_urls") or {}).get("spotify")
                        }
                except Exception as e:
                    logger.warning(f"Error fetching playlist {pl_q['query']}: {e}")
                return None

            pl_futures = [executor.submit(_fetch_pl, pl_q) for pl_q in featured_playlists_queries]
            for pf in pl_futures:
                try:
                    r = pf.result(timeout=10)
                    if r: output["playlists"].append(r)
                except Exception:
                    pass

        return output

spotify_extractor = SpotifyExtractorService()
