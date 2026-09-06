# core/services/catalog_autopilot.py

import time
import logging
import sqlite3
from core.config import Config
from core.models import get_db
from core.services.spotify_extractor import spotify_extractor, API_BASE

logger = logging.getLogger(__name__)

# حافظه کش برای فید رادار اسپاتیفای جهت کاهش درخواست‌های تکراری
_RADAR_CACHE = {
    "data": None,
    "cached_at": 0
}
RADAR_CACHE_TTL = 30  # ۳۰ ثانیه کش هوشمند جهت نمایش زنده و سریع در پنل

class CatalogAutopilotService:
    """
    موتور هوشمند و خودکار گنجینه طلایی و رادار کشف اسپاتیفای (Spotify Radar & Vault Autopilot)
    مدیریت تزریق پیوسته (Drip-Feed) دیسکوگرافی هنرمندان و پلی‌لیست‌ها با سرعت بهینه و ایمن.
    """

    TARGET_GOAL = 25000  # هدف طلایی تعداد قطعات آرشیو

    def get_vault_metrics(self):
        """محاسبه شاخص‌های زنده و آماری گنجینه طلایی لایراز"""
        with sqlite3.connect(Config.DATABASE_URI) as conn:
            conn.row_factory = sqlite3.Row
            
            total_tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            total_size_bytes = conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM tracks").fetchone()[0]
            total_duration_sec = conn.execute("SELECT COALESCE(SUM(duration), 0) FROM tracks").fetchone()[0]
            
            # آمار قطعات جدید افزوده شده به گنجینه در تاریخ امروز
            ingested_today = conn.execute("""
                SELECT COUNT(*) FROM tracks 
                WHERE date(created_at, 'localtime') = date('now', 'localtime')
            """).fetchone()[0]

            campaign_artists_count = conn.execute("SELECT COUNT(*) FROM artist_campaigns").fetchone()[0]
            completed_campaigns = conn.execute("SELECT COUNT(*) FROM artist_campaigns WHERE status = 'completed'").fetchone()[0]
            
            # ۱. تعداد کل فایل‌های صوتی یکتا در دیتابیس و کلود (دقیقاً منطبق با هدر پنل)
            total_tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

            campaign_artists_count = conn.execute("SELECT COUNT(*) FROM artist_campaigns").fetchone()[0]
            completed_campaigns = conn.execute("SELECT COUNT(*) FROM artist_campaigns WHERE status = 'completed'").fetchone()[0]
            
            settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
            autopilot_enabled = bool(settings['autopilot_enabled']) if settings and 'autopilot_enabled' in settings.keys() else False

            # ۲. قطعات باقیمانده در صف انتظار یا در حال پردازش
            pending_tracks = conn.execute("""
                SELECT COUNT(*) FROM campaign_tracks WHERE status IN ('queued', 'downloading')
            """).fetchone()[0]

            # هدف پویا و یکدست: مجموع فایل‌های تکمیل‌شده + فایل‌های در حال انتظار صف
            target_goal = total_tracks + pending_tracks
            if target_goal == 0:
                target_goal = 1

            percent = round((total_tracks / max(1, target_goal)) * 100, 1)
            if percent > 100: percent = 100.0

            return {
                "total_tracks": total_tracks,
                "target_goal": target_goal,
                "progress_percent": percent,
                "total_size_mb": round(total_size_bytes / (1024 * 1024), 1),
                "total_size_gb": round(total_size_bytes / (1024 * 1024 * 1024), 2),
                "total_hours": round(total_duration_sec / 3600, 1),
                "ingested_today": ingested_today,
                "total_artists": campaign_artists_count,
                "completed_artists": completed_campaigns,
                "radar_artists": campaign_artists_count,
                "autopilot_enabled": autopilot_enabled
            }

    def get_radar_feed(self, force_refresh=False):
        """دریافت فید زنده رادار اسپاتیفای به همراه وضعیت سینک در دیتابیس محلی"""
        global _RADAR_CACHE
        now = time.time()
        
        if force_refresh or not _RADAR_CACHE["data"] or (now - _RADAR_CACHE["cached_at"]) > RADAR_CACHE_TTL:
            try:
                raw_radar = spotify_extractor.fetch_live_curated_radar()
                _RADAR_CACHE["data"] = raw_radar
                _RADAR_CACHE["cached_at"] = now
            except Exception as e:
                logger.error(f"Error fetching live Spotify radar: {e}")
                if not _RADAR_CACHE["data"]:
                    return {"categories": [], "playlists": []}

        radar_data = _RADAR_CACHE["data"]
        
        # تطبیق با دیتابیس محلی
        with sqlite3.connect(Config.DATABASE_URI) as conn:
            conn.row_factory = sqlite3.Row
            
            campaigns = conn.execute("SELECT id, artist_name, spotify_id, spotify_url, avatar_url, total_tracks, completed_tracks, status, hub_token FROM artist_campaigns").fetchall()
            campaign_map = {c['spotify_id']: dict(c) for c in campaigns if c['spotify_id']}
            artist_name_map = {c['artist_name'].lower().strip(): dict(c) for c in campaigns if c['artist_name']}

            enriched_categories = []
            matched_camp_ids = set()

            for cat in radar_data.get("categories", []):
                enriched_artists = []
                for a in cat.get("artists", []):
                    sp_id = a.get("id")
                    name = a.get("name", "")
                    
                    matched_camp = campaign_map.get(sp_id) or artist_name_map.get(name.lower().strip())
                    
                    artist_status = "ready" # آماده برای لانچ
                    camp_id = None
                    hub_token = None
                    completed_count = 0
                    total_count = 0
                    
                    if matched_camp:
                        camp_id = matched_camp['id']
                        matched_camp_ids.add(camp_id)
                        hub_token = matched_camp.get('hub_token')
                        completed_count = matched_camp['completed_tracks'] or 0
                        total_count = matched_camp['total_tracks'] or 0
                        if matched_camp['status'] == 'completed':
                            artist_status = "completed"
                        else:
                            artist_status = "in_progress"

                    enriched_artists.append({
                        **a,
                        "status": artist_status,
                        "campaign_id": camp_id,
                        "hub_token": hub_token,
                        "completed_tracks": completed_count,
                        "total_tracks": total_count
                    })

                enriched_categories.append({
                    **cat,
                    "artists": enriched_artists
                })

            # آرتیست‌هایی که خارج از دسته‌بندی‌های ثابت اولیه و به صورت خودکار کشف شده‌اند
            discovered_camps = [c for c in campaigns if c['id'] not in matched_camp_ids]
            if discovered_camps:
                discovered_artists = []
                for dc in discovered_camps:
                    dcd = dict(dc)
                    discovered_artists.append({
                        "id": dcd.get('spotify_id'),
                        "name": dcd.get('artist_name'),
                        "image": dcd.get('avatar_url'),
                        "followers": 0,
                        "genres": ["Autonomous Discovery"],
                        "spotify_url": dcd.get('spotify_url'),
                        "status": "completed" if dcd.get('status') == 'completed' else "in_progress",
                        "campaign_id": dcd.get('id'),
                        "hub_token": dcd.get('hub_token'),
                        "completed_tracks": dcd.get('completed_tracks') or 0,
                        "total_tracks": dcd.get('total_tracks') or 0
                    })

                enriched_categories.append({
                    "id": "auto_discoveries",
                    "key": "auto_discoveries",
                    "title": "🔮 AI Radar Discoveries",
                    "subtitle": "Autonomous machine-discovered artists from Spotify graph",
                    "is_default": False,
                    "artists": discovered_artists
                })

            # وضعیت زنده پلی‌لیست‌ها در دیتابیس
            raw_playlists = radar_data.get("playlists", [])
            existing_sources = {r[0]: r[1] for r in conn.execute("SELECT source, COUNT(*) FROM ingestion_logs WHERE source LIKE 'pl:%' GROUP BY source").fetchall()}
            active_sources = set(r[0] for r in conn.execute("SELECT DISTINCT source FROM ingestion_logs WHERE source LIKE 'pl:%' AND status IN ('queued', 'downloading')").fetchall())

            enriched_playlists = []
            for pl in raw_playlists:
                pld = dict(pl)
                pl_title = pld.get("title") or pld.get("name") or "playlist"
                source_label = f"pl:{pl_title[:15]}"
                if source_label in active_sources:
                    pld["status"] = "in_progress"
                elif source_label in existing_sources:
                    pld["status"] = "completed"
                else:
                    pld["status"] = "ready"
                enriched_playlists.append(pld)

            # استخراج ساختار یافته و شفاف برای پایپ‌لاین فعال (Active Pipeline Cockpit) با آمار زنده از جدول قطعات
            active_artist_camps = conn.execute("""
                SELECT ac.id, ac.artist_name, ac.avatar_url, ac.spotify_url, ac.spotify_id, ac.hub_token, ac.status,
                       COUNT(ct.id) as real_total,
                       SUM(CASE WHEN ct.status = 'completed' THEN 1 ELSE 0 END) as real_comp,
                       SUM(CASE WHEN ct.status IN ('queued', 'downloading') THEN 1 ELSE 0 END) as real_pending
                FROM artist_campaigns ac
                LEFT JOIN campaign_tracks ct ON ct.campaign_id = ac.id
                WHERE ac.status = 'processing'
                GROUP BY ac.id
                ORDER BY ac.id DESC
            """).fetchall()

            pipeline_artists = []
            for a in active_artist_camps:
                ad = dict(a)
                total = ad.get('real_total') or 1
                comp = ad.get('real_comp') or 0
                pending = ad.get('real_pending') or 0
                
                # اگر هیچ ترکی در صف انتظار نمانده باشد، کمپین تکمیل است و فوراً از بافر خارج می‌شود
                if pending == 0 and comp > 0:
                    conn.execute("UPDATE artist_campaigns SET status = 'completed', completed_tracks = ?, total_tracks = ? WHERE id = ?", (comp, total, ad['id']))
                    conn.commit()
                    continue

                pct = min(100.0, round((comp / max(1, total)) * 100, 1))
                pipeline_artists.append({
                    "id": ad.get('spotify_id'),
                    "campaign_id": ad.get('id'),
                    "name": ad.get('artist_name'),
                    "image": ad.get('avatar_url'),
                    "hub_token": ad.get('hub_token'),
                    "total_tracks": total,
                    "completed_tracks": comp,
                    "remaining_tracks": pending,
                    "percent": pct,
                    "status": "in_progress"
                })

            active_pl_sources = conn.execute("""
                SELECT source, 
                       COUNT(*) as total,
                       SUM(CASE WHEN status IN ('queued', 'downloading') THEN 1 ELSE 0 END) as pending,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed
                FROM ingestion_logs 
                WHERE source LIKE 'pl:%'
                GROUP BY source
                HAVING pending > 0
                ORDER BY total DESC
            """).fetchall()

            # استخراج کاورهای واقعی پلی‌لیست‌ها از جدول playlist_meta
            conn.execute("""
                CREATE TABLE IF NOT EXISTS playlist_meta (
                    source TEXT PRIMARY KEY,
                    title TEXT,
                    image_url TEXT,
                    spotify_id TEXT
                )
            """)
            meta_imgs = {r[0]: r[1] for r in conn.execute("SELECT source, image_url FROM playlist_meta WHERE image_url IS NOT NULL").fetchall()}

            pipeline_playlists = []
            for pl in active_pl_sources:
                pld = dict(pl)
                src = pld['source']
                clean_title = src.replace("pl:", "").strip()
                matched_meta = next((p for p in raw_playlists if clean_title.lower() in (p.get('title') or '').lower()), None)
                img = meta_imgs.get(src) or (matched_meta.get('image') if matched_meta else None)
                pipeline_playlists.append({
                    "source": src,
                    "title": clean_title,
                    "image": img,
                    "total": pld['total'],
                    "pending": pld['pending'],
                    "completed": pld['completed'],
                    "status": "in_progress"
                })

            exact_queued = conn.execute("SELECT COUNT(*) FROM ingestion_logs WHERE status IN ('queued', 'downloading')").fetchone()[0]

            # پر کردن تضمینی بافر در صورتی که اسلاتی خالی شده باشد و اتوپایلوت فعال باشد
            settings = conn.execute("SELECT autopilot_enabled FROM settings WHERE id = 1").fetchone()
            if settings and settings[0] and (len(pipeline_artists) < 10 or len(pipeline_playlists) < 6):
                try:
                    from core.tasks import check_autopilot_tick
                    check_autopilot_tick()
                except Exception:
                    pass

            return {
                "categories": enriched_categories,
                "playlists": enriched_playlists,
                "pipeline": {
                    "artists": pipeline_artists,
                    "playlists": pipeline_playlists,
                    "target_artists": 10,
                    "target_playlists": 6,
                    "active_artists_count": len(pipeline_artists),
                    "active_playlists_count": len(pipeline_playlists),
                    "total_queued": exact_queued
                }
            }

    def launch_artist_campaign(self, spotify_id, target_channel_id=None):
        """راه‌اندازی فوری کمپین استخراج و دانلود دیسکوگرافی یک خواننده از اسپاتیفای"""
        # ۱. واکشی دیسکوگرافی از اسپاتیفای با سقف ایمن ۲۵۰ قطعه
        disco = spotify_extractor.fetch_artist_discography(spotify_id, max_limit=250)
        if not disco or not disco.get("tracks"):
            raise ValueError("Could not extract discography or artist has no tracks.")

        artist_name = disco["artist_name"]
        avatar_url = disco.get("artist_image")
        spotify_url = disco.get("artist_url")
        tracks = disco["tracks"]
        if len(tracks) > 250:
            tracks = tracks[:250]

        target_ch = target_channel_id if target_channel_id and str(target_channel_id) != str(Config.STORAGE_CHANNEL_ID) else None
        
        with sqlite3.connect(Config.DATABASE_URI) as conn:
            existing = conn.execute("SELECT id FROM artist_campaigns WHERE spotify_id = ? OR LOWER(artist_name) = ?", (spotify_id, artist_name.lower().strip())).fetchone()
            if existing:
                campaign_id = existing[0]
                logger.info(f"Campaign #{campaign_id} already exists for {artist_name}, skipping new campaign creation.")
                return {
                    "campaign_id": campaign_id,
                    "artist_name": artist_name,
                    "total_tracks": len(tracks)
                }

            cur = conn.execute("""
                INSERT INTO artist_campaigns (artist_name, spotify_id, spotify_url, avatar_url, target_channel_id, total_tracks, completed_tracks, status)
                VALUES (?, ?, ?, ?, ?, ?, 0, 'processing')
            """, (
                artist_name,
                spotify_id,
                spotify_url,
                avatar_url,
                str(target_ch) if target_ch else None,
                len(tracks)
            ))
            campaign_id = cur.lastrowid

            # درج فوری تمامی قطعات در جدول campaign_tracks با وضعیت queued جهت نمایش آنی صف
            track_rows = []
            for t in tracks:
                t_title = t.get('title')
                t_artist = t.get('artist_string') or artist_name
                t_album = (t.get('album') or {}).get('name') or ''
                t_release = (t.get('album') or {}).get('release_date') or ''
                t_cover = (t.get('album') or {}).get('cover_url') or avatar_url
                t_dur = t.get('duration_seconds', 0)
                t_sp_url = t.get('spotify_url') or ''
                track_rows.append((campaign_id, t_title, t_artist, t_album, t_release, t_cover, t_dur, t_sp_url, 'queued'))

            conn.executemany("""
                INSERT INTO campaign_tracks (campaign_id, title, artist, album_name, release_date, cover_url, duration_seconds, spotify_url, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, track_rows)
            conn.commit()

        # پاکسازی کش فید رادار
        global _RADAR_CACHE
        _RADAR_CACHE["data"] = None
        _RADAR_CACHE["cached_at"] = 0

        # ۲. ارسال تسک به ورکر پس‌زمینه Huey
        from core.tasks import ingest_artist_campaign_task
        ingest_artist_campaign_task(
            campaign_id=campaign_id,
            tracks=tracks,
            artist_name=artist_name,
            target_channel_id=str(target_ch) if target_ch else None
        )

        return {
            "campaign_id": campaign_id,
            "artist_name": artist_name,
            "total_tracks": len(tracks)
        }

    def launch_playlist_ingestion(self, playlist_id, category_label="Playlist"):
        """استخراج تمام قطعات یک پلی‌لیست اسپاتیفای و ارسال تک‌به‌تک به صف ورکر با عدم تکرار"""
        from core.services.crawler import crawler_service
        
        pl_data = spotify_extractor.fetch_playlist_tracks(playlist_id)
        if not pl_data or not pl_data.get("tracks"):
            raise ValueError("Could not extract playlist tracks.")

        formatted_tracks = []
        for t in pl_data["tracks"]:
            formatted_tracks.append({
                "title": t.get("title"),
                "artist": t.get("artist_string"),
                "cover": (t.get("album") or {}).get("cover_url"),
                "duration": t.get("duration_seconds"),
                "search_query": f"{t.get('artist_string')} {t.get('title')}"
            })
        # تزریق ۳۵ قطعه برتر پلی‌لیست برای پردازش سریع و روان بدون مسدودی صف
        batch_tracks = formatted_tracks[:35]
        res = crawler_service.ingest_tracks_one_by_one(
            batch_tracks,
            source_label=f"pl:{pl_data.get('title', 'playlist')[:15]}",
            max_limit=35
        )

        # ذخیره متادیتای کاور پلی‌لیست
        with sqlite3.connect(Config.DATABASE_URI) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS playlist_meta (
                    source TEXT PRIMARY KEY,
                    title TEXT,
                    image_url TEXT,
                    spotify_id TEXT
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO playlist_meta (source, title, image_url, spotify_id)
                VALUES (?, ?, ?, ?)
            """, (f"pl:{pl_data.get('title', 'playlist')[:15]}", pl_data.get('title'), pl_data.get('image'), playlist_id))
            conn.commit()

        # پاکسازی کش فید رادار
        global _RADAR_CACHE
        _RADAR_CACHE["data"] = None
        _RADAR_CACHE["cached_at"] = 0

        return {
            "playlist_id": playlist_id,
            "title": pl_data.get("title"),
            "total_scanned": len(formatted_tracks),
            "queued": res.get("queued", 0),
            "skipped": res.get("skipped", 0)
        }

    # فهرست جامع و گلچین سوپراستارهای موسیقی ایران (پاپ، رپ، سنتی، تلفیقی، راک و کلاسیک)
    PERSIAN_SUPERSTAR_ROSTER = [
        "Alireza Ghorbani", "Reza Bahram", "Alireza Talischi", "Masoud Sadeghloo", 
        "Mehdi Ahmadvand", "Salar Aghili", "Ali Zand Vakili", "Macan Band", 
        "Hoorosh Band", "Garsha Rezaei", "Saman Jalili", "Hamid Hiraad", 
        "Sohrab MJ", "Alireza JJ", "Mazyar Fallahi", "Shohreh", "Leila Forouhar", 
        "Shahram Shabpareh", "Shahram Solati", "Andy", "Kamran Hooman", "Mansour", 
        "Bijan Mortazavi", "Hassan Shamaizadeh", "Kaveh Yaghmaei", "Kasra Zahedi", 
        "Naser Zeinali", "Sohrab Pakzad", "Ashvan", "Sogand", "Donya", "Talkdown", 
        "Vinak", "021kid", "Mohammad Motamedi", "Shahram Nazeri", "Kayhan Kalhor", 
        "Hossein Alizadeh", "Pouya", "Omid", "Benyamin Bahadori", "Ali Lohrasbi", 
        "Sina Shabankhani", "Sina Derakhshande", "Mohammad Alizadeh", "Hamed Homayoun",
        "Evan Band", "Puzzle Band", "Rastak", "Sina Sarlak", "Amirabbas Golab",
        "Meysam Ebrahimi", "Yousef Zamani", "Majid Kharatha", "Ali Abdolmaleki",
        "Ramin Bibak", "Amin Rostami", "Emad Talebzadeh", "Hamid Askari", "Behnam Safavi",
        "Morteza Ashrafi", "Pouya Bayati", "Ali Ashabi", "Shahab Mozaffari", "Mehdi Jahani",
        "Ashkan Khatibi", "Sina Parsian", "Danial", "Shayan Yo", "Dorcci", "021G", "Madgal",
        "Nooshafarin", "Susan Roshan", "Afshin", "Pyruz", "Shahyad", "Siavash Sahneh",
        "Farshid Amin", "Shahram Kashani", "Davood Behboodi", "Mehdi Moghaddam", "Mehdi Asadi",
        "Shahab Ramezan", "Mohammad Esfahani", "Alireza Eftekhari", "Abdolhossein Mokhtabad",
        "Parviz Meshkatian", "Jalal Zolfonun", "Kamyar", "Reza Yazdani", "Hessamoddin Seraj",
        "Nima Masiha", "Ghasem Afshar"
    ]

    def _discover_top_spotify_artists_dynamically(self, existing_sp_ids, existing_names, limit_needed=5):
        """کشف کاملاً پویا و هدفمند برترین خوانندگان ایرانی با اولویت فهرست سوپراستارها و فیلتر دقیق هویت ایرانی"""
        # واکشی بلک‌لیست دیسکاوری‌های ناموفق قبلی
        failed_sp_ids = set()
        try:
            with sqlite3.connect(Config.DATABASE_URI) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS failed_artist_discoveries (
                        spotify_id TEXT PRIMARY KEY,
                        artist_name TEXT,
                        reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                failed_rows = conn.execute("SELECT spotify_id FROM failed_artist_discoveries").fetchall()
                failed_sp_ids = set(r[0] for r in failed_rows if r[0])
        except Exception:
            pass

        candidates = {}

        # اولویت ۱: کشف سوپراستارهای اصیل موسیقی ایران از فهرست مرجع
        for art_name in self.PERSIAN_SUPERSTAR_ROSTER:
            if len(candidates) >= limit_needed:
                break
            norm_name = art_name.lower().strip()
            if norm_name in existing_names or any(norm_name in ex for ex in existing_names):
                continue

            try:
                # ابتدا جستجوی دقیق نام خواننده
                data = spotify_extractor.api_get(f"{API_BASE}/search", params={"q": f'artist:"{art_name}"', "type": "artist", "limit": 1})
                items = data.get("artists", {}).get("items", [])
                if not items:
                    data = spotify_extractor.api_get(f"{API_BASE}/search", params={"q": art_name, "type": "artist", "limit": 1})
                    items = data.get("artists", {}).get("items", [])

                if items:
                    a = items[0]
                    a_id = a.get("id")
                    if not a_id or a_id in existing_sp_ids or a_id in failed_sp_ids or a_id in candidates:
                        continue

                    found_name = (a.get("name") or "").lower().strip()
                    # بررسی اعتبارسنجی نام جهت پرهیز از نتایج تصادفی
                    first_word = norm_name.split()[0]
                    if first_word in found_name or found_name.replace(" ", "") in norm_name.replace(" ", ""):
                        candidates[a_id] = a
                        logger.info(f"✨ [Autopilot Discovery] Matched Persian superstar: {a.get('name')} (Spotify ID: {a_id})")
            except Exception as e:
                logger.warning(f"Error querying superstar '{art_name}': {e}")

        # اولویت ۲: در صورت نیاز به هنرمندان بیشتر، جستجوی ژانرهای اختصاصی پاپ و رپ فارسی با فیلتر ناموزون
        if len(candidates) < limit_needed:
            blacklisted_words = [
                "choir", "orchestra", "media", "various artists", "american", 
                "audiobook", "podcast", "karaoke", "instrumental", "soundtrack",
                "tribute", "compilation", "sound effects", "white noise", "beethoven", "mozart", "bach"
            ]
            genre_queries = ['genre:"persian pop"', 'genre:"persian hip hop"', 'genre:"classic persian pop"']
            for gq in genre_queries:
                if len(candidates) >= limit_needed:
                    break
                try:
                    data = spotify_extractor.api_get(f"{API_BASE}/search", params={"q": gq, "type": "artist", "limit": 20})
                    for a in data.get("artists", {}).get("items", []):
                        if not a or not a.get("id"):
                            continue
                        a_id = a["id"]
                        a_name = (a.get("name") or "").lower().strip()
                        if a_id in existing_sp_ids or a_name in existing_names or a_id in failed_sp_ids or a_id in candidates:
                            continue
                        if any(w in a_name for w in blacklisted_words):
                            continue
                        followers = (a.get("followers") or {}).get("total", 0)
                        if followers >= 3000:
                            candidates[a_id] = a
                            if len(candidates) >= limit_needed:
                                break
                except Exception as e:
                    logger.warning(f"Error querying genre '{gq}': {e}")

        return list(candidates.values())[:limit_needed]

    def _discover_top_spotify_playlists_dynamically(self, existing_sources, limit_needed=6):
        """کشف کاملاً پویا و خودکار برترین پلی‌لیست‌های ترند از اسپاتیفای با اولویت لیست‌های ایرانی"""
        queries = [
            "Top Hits Persian", "Persian Pop", "Radio Javan Hits", "Persian Rap", 
            "Golchin Shad", "Persian Dance Party", "Sonati Irani", "Persian Acoustic", 
            "Ahang Ghadimi", "Persian Hip Hop", "New Music Farsi", "Iran Top 50", 
            "Nostalgia Farsi", "Persian Remix", "Persian Chill", "Top 100 Iran"
        ]
        candidates = {}
        for q in queries:
            if len(candidates) >= limit_needed:
                break
            try:
                data = spotify_extractor.api_get(f"{API_BASE}/search", params={"q": q, "type": "playlist", "limit": 10})
                for p in data.get("playlists", {}).get("items", []):
                    if p and p.get("id") and p["id"] not in candidates:
                        pl_title = p.get("name") or "playlist"
                        source_label = f"pl:{pl_title[:15]}"
                        if source_label not in existing_sources:
                            candidates[p["id"]] = p
                            if len(candidates) >= limit_needed:
                                break
            except Exception as e:
                logger.warning(f"Error querying dynamic playlists for '{q}': {e}")

        # در صورت نیاز، افزودن از پلی‌لیست‌های برگزیده رادار اسپاتیفای
        if len(candidates) < limit_needed:
            try:
                raw_radar = spotify_extractor.fetch_live_curated_radar()
                for p in raw_radar.get("playlists", []):
                    if p and p.get("id") and p["id"] not in candidates:
                        pl_title = p.get("title") or "playlist"
                        source_label = f"pl:{pl_title[:15]}"
                        if source_label not in existing_sources:
                            candidates[p["id"]] = {
                                "id": p["id"],
                                "name": pl_title,
                                "images": [{"url": p.get("image")}] if p.get("image") else []
                            }
                            if len(candidates) >= limit_needed:
                                break
            except Exception as e:
                logger.warning(f"Error checking curated radar playlists: {e}")

        return list(candidates.values())[:limit_needed]

    def autopilot_tick(self):
        """
        تپش دوره‌ای اتوپایلوت (هر ۳ دقیقه یک‌بار):
        ۱. خودترمیمی قطعات ناموفق تا حداکثر ۲ بار تلاش
        ۲. همگام‌سازی وضعیت کمپین‌های تکمیل‌شده و خروج قطعی آن‌ها از صف
        ۳. تزریق و استخراج پیوسته دیسکوگرافی کمپین‌های فعال
        ۴. حفظ ظرفیت پایدار ۱۰ خواننده سوپراستار و ۶ پلی‌لیست ترند در صف
        """
        with sqlite3.connect(Config.DATABASE_URI) as conn:
            conn.row_factory = sqlite3.Row
            settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
            if not settings or 'autopilot_enabled' not in settings.keys() or not settings['autopilot_enabled']:
                return

            # ۰. همگام‌سازی خودکار وضعیت کمپین‌های تکمیل‌شده و پاکسازی لاگ‌های معلق قدیمی
            conn.execute("""
                UPDATE ingestion_logs 
                SET status = 'failed', error_msg = 'Worker timeout / interrupted'
                WHERE status = 'downloading' AND created_at < datetime('now', '-2 hours')
            """)
            # همگام‌سازی قطعات منقضی یا ناموفق در جدول campaign_tracks
            conn.execute("""
                UPDATE campaign_tracks
                SET status = 'failed', error_msg = 'Permanently unavailable [synced/timeout]'
                WHERE status IN ('queued', 'downloading')
                  AND (
                      youtube_id IN (SELECT youtube_id FROM ingestion_logs WHERE status = 'failed' AND youtube_id IS NOT NULL)
                      OR (youtube_id IS NULL AND created_at < datetime('now', '-2 hours'))
                      OR (status = 'queued' AND created_at < datetime('now', '-4 hours'))
                  )
            """)
            conn.execute("""
                UPDATE artist_campaigns 
                SET completed_tracks = (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id AND status = 'completed'),
                    total_tracks = (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id),
                    status = 'completed',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status != 'completed'
                  AND (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id AND status IN ('queued', 'downloading')) = 0
                  AND (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id AND status = 'completed') > 0
            """)
            conn.commit()

            # ۱. خودترمیمی هوشمند با سقف تلاش مجدد (حداکثر ۲ بار تلاش)
            stuck_tracks = conn.execute("""
                SELECT ct.id, ct.title, ct.artist, ct.youtube_id, ct.cover_url, ct.duration_seconds, ac.target_channel_id, ac.artist_name 
                FROM campaign_tracks ct
                JOIN artist_campaigns ac ON ct.campaign_id = ac.id
                WHERE ct.youtube_id IS NOT NULL 
                  AND (
                    (ct.status = 'failed' AND (ct.error_msg IS NULL OR ct.error_msg NOT LIKE '%[max_retries]%'))
                    OR (ct.status = 'downloading' AND ct.created_at < datetime('now', '-20 minutes'))
                  )
                LIMIT 5
            """).fetchall()

            if stuck_tracks:
                logger.info(f"🔄 [Autopilot Auto-Healer] Safely checking {len(stuck_tracks)} stuck/failed tracks...")
                from core.tasks import download_and_process_track
                for trk in stuck_tracks:
                    fail_count = conn.execute("SELECT COUNT(*) FROM ingestion_logs WHERE youtube_id = ? AND status = 'failed'", (trk['youtube_id'],)).fetchone()[0]
                    if fail_count >= 2:
                        conn.execute("UPDATE campaign_tracks SET status = 'failed', error_msg = 'Permanently unavailable [max_retries]' WHERE id = ?", (trk['id'],))
                        continue

                    conn.execute("UPDATE campaign_tracks SET status = 'queued', error_msg = NULL WHERE id = ?", (trk['id'],))
                    
                    cur = conn.execute("""
                        INSERT INTO ingestion_logs (title, performer, youtube_id, source, status)
                        VALUES (?, ?, ?, ?, 'queued')
                    """, (trk['title'], trk['artist'], trk['youtube_id'], f"auto_retry:{trk['artist_name'][:12]}"))
                    log_id = cur.lastrowid

                    target_ch = trk['target_channel_id']
                    distinct_target_ch = target_ch if target_ch and str(target_ch) != str(Config.STORAGE_CHANNEL_ID) else None

                    download_and_process_track(
                        video_id=trk['youtube_id'],
                        title=trk['title'],
                        artist=trk['artist'],
                        user_id=0,
                        user_first_name="AutoHealer",
                        session_token=None,
                        chat_id=None,
                        message_id=None,
                        quality=None,
                        cover_url=trk['cover_url'],
                        duration=trk['duration_seconds'],
                        log_id=log_id,
                        target_channel_id=distinct_target_ch,
                        priority=3  # اولویت بسیار پایین‌تر تا جلوی دانلودهای فعال را نگیرد
                    )
                conn.commit()

            # ۲. تزریق و استخراج پیوسته برای کمپین‌هایی که هنوز در انتظار استخراج یوتیوب هستند
            # علامت‌گذاری ترک‌های بدون یوتیوب آیدی که بیش از ۲ ساعت مانده‌اند به عنوان failed
            conn.execute("""
                UPDATE campaign_tracks
                SET status = 'failed', error_msg = 'No YouTube match available'
                WHERE youtube_id IS NULL AND status = 'queued' AND created_at < datetime('now', '-2 hours')
            """)
            conn.commit()

            unresolved_camps = conn.execute("""
                SELECT ac.id, ac.artist_name, ac.target_channel_id
                FROM artist_campaigns ac
                WHERE ac.status = 'processing'
                  AND EXISTS (SELECT 1 FROM campaign_tracks WHERE campaign_id = ac.id AND youtube_id IS NULL AND status = 'queued')
                LIMIT 2
            """).fetchall()

            for u_camp in unresolved_camps:
                cid = u_camp['id']
                unresolved_tracks = conn.execute("""
                    SELECT title, artist, cover_url, duration_seconds, spotify_url
                    FROM campaign_tracks
                    WHERE campaign_id = ? AND youtube_id IS NULL AND status = 'queued'
                    LIMIT 40
                """, (cid,)).fetchall()
                if unresolved_tracks:
                    from core.tasks import ingest_artist_campaign_task
                    formatted_trks = [{
                        'title': r[0],
                        'artist_string': r[1],
                        'cover_url': r[2],
                        'duration_seconds': r[3],
                        'spotify_url': r[4]
                    } for r in unresolved_tracks]
                    logger.info(f"⚡️ [Autopilot Resolution] Dispatching {len(formatted_trks)} tracks for campaign #{cid} ({u_camp['artist_name']})...")
                    ingest_artist_campaign_task(
                        campaign_id=cid,
                        tracks=formatted_trks,
                        artist_name=u_camp['artist_name'],
                        target_channel_id=u_camp['target_channel_id']
                    )

            # ۳. لیست موجودی برای جلوگیری قطعی از تکرار
            existing_camps = conn.execute("SELECT spotify_id, LOWER(artist_name) FROM artist_campaigns").fetchall()
            existing_sp_ids = set(r[0] for r in existing_camps if r[0])
            existing_names = set(r[1] for r in existing_camps if r[1])

            # ==========================================
            # 🎯 POOL 1: حفظ پایدار ۱۰ آرتیست فعال در صف
            # ==========================================
            # به‌روزرسانی آنی کمپین‌هایی که تمام قطعاتشان پردازش شده تا از صف فعال خارج شوند
            conn.execute("""
                UPDATE artist_campaigns
                SET status = 'completed',
                    completed_tracks = (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id AND status = 'completed'),
                    total_tracks = (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id)
                WHERE status = 'processing'
                  AND (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id AND status IN ('queued', 'downloading')) = 0
                  AND (SELECT COUNT(*) FROM campaign_tracks WHERE campaign_id = artist_campaigns.id AND status = 'completed') > 0
            """)
            conn.commit()

            active_artists_count = conn.execute("""
                SELECT COUNT(*) FROM artist_campaigns WHERE status = 'processing'
            """).fetchone()[0]

            TARGET_ACTIVE_ARTISTS = 10
            needed_artists = max(0, TARGET_ACTIVE_ARTISTS - active_artists_count)

            if needed_artists > 0:
                top_artists = self._discover_top_spotify_artists_dynamically(existing_sp_ids, existing_names, limit_needed=needed_artists)
                for c_art in top_artists:
                    followers = (c_art.get("followers") or {}).get("total", 0)
                    logger.info(f"👑 [Autopilot Active Pool: 10 Artists] Launching {c_art['name']} ({followers:,} followers) to maintain 10 active pool...")
                    try:
                        self.launch_artist_campaign(c_art["id"])
                        existing_sp_ids.add(c_art["id"])
                        existing_names.add((c_art.get("name") or "").lower().strip())
                    except Exception as e:
                        logger.error(f"Error launching dynamic artist {c_art.get('name')}: {e}")
                        existing_sp_ids.add(c_art["id"])
                        existing_names.add((c_art.get("name") or "").lower().strip())
                        try:
                            conn.execute("""
                                INSERT OR REPLACE INTO failed_artist_discoveries (spotify_id, artist_name, reason)
                                VALUES (?, ?, ?)
                            """, (c_art["id"], c_art.get("name"), str(e)[:100]))
                            conn.commit()
                        except Exception:
                            pass

            # ==========================================
            # 🎯 POOL 2: حفظ پایدار ۶ پلی‌لیست فعال در صف
            # ==========================================
            active_pl_count = conn.execute("""
                SELECT COUNT(DISTINCT source) FROM ingestion_logs 
                WHERE source LIKE 'pl:%' AND status IN ('queued', 'downloading')
            """).fetchone()[0]

            TARGET_ACTIVE_PLAYLISTS = 6
            needed_playlists = max(0, TARGET_ACTIVE_PLAYLISTS - active_pl_count)

            if needed_playlists > 0:
                existing_sources = set(r[0] for r in conn.execute("""
                    SELECT DISTINCT source FROM ingestion_logs 
                    WHERE source LIKE 'pl:%' AND (status IN ('queued', 'downloading') OR created_at > datetime('now', '-3 days'))
                """).fetchall())
                top_playlists = self._discover_top_spotify_playlists_dynamically(existing_sources, limit_needed=needed_playlists)
                for c_pl in top_playlists:
                    pl_name = c_pl.get("name") or "Playlist"
                    logger.info(f"🎧 [Autopilot Active Pool: 6 Playlists] Launching {pl_name} to maintain 6 active pool...")
                    try:
                        self.launch_playlist_ingestion(c_pl["id"])
                        source_label = f"pl:{pl_name[:15]}"
                        existing_sources.add(source_label)
                    except Exception as e:
                        logger.error(f"Error launching dynamic playlist {pl_name}: {e}")

catalog_autopilot = CatalogAutopilotService()
