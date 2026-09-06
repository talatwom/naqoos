# core/services/youtube.py

import os
import re
import time
import shutil
import asyncio
import logging
import requests
import yt_dlp
from ytmusicapi import YTMusic
from core.config import Config

# ایمپورت‌های مربوط به Mutagen برای تزریق متادیتا در سطح باینری
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, USLT, TIT2, TPE1, error

logger = logging.getLogger(__name__)

class YouTubeService:
    def __init__(self, download_sub_dir="yt_cache"):
        # مسیر دانلود در پوشه instance
        self.download_dir = os.path.join(Config.INSTANCE_PATH, download_sub_dir)
        
        # ساخت پوشه اگر نباشد
        if not os.path.exists(self.download_dir):
            try:
                os.makedirs(self.download_dir)
            except OSError:
                pass
            
        self.yt = YTMusic()
        self.ffmpeg_path = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg" or "/opt/homebrew/bin/ffmpeg"

    def clean_artist_name(self, artist):
        """حذف پسوند متداول - Topic از کانال‌های اتوماتیک یوتیوب"""
        if not artist:
            return "Unknown Artist"
        artist = re.sub(r'\s*-\s*Topic$', '', artist, flags=re.IGNORECASE).strip()
        return artist if artist else "Unknown Artist"

    def search(self, query):
        try:
            res = self.yt.search(query, filter="songs", limit=10)
            if res:
                return res
        except Exception as e:
            logger.warning(f"YTMusic API Search Error for '{query}': {e}, falling back to yt-dlp...")

        # پلن پشتیبان سریع و پایدار با yt-dlp مجهز به POT Provider (بدون خطای 403 یا مسدودی یوتیوب)
        try:
            import yt_dlp
            ydl_opts = {
                'extract_flat': True,
                'quiet': True,
                'skip_download': True,
                'no_warnings': True,
                'socket_timeout': 5,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['mweb', 'web'],
                    },
                    'youtubepot-bgutilhttp': {
                        'base_url': ['http://Lyraz_pot:4416', 'http://pot:4416', 'http://172.17.0.1:4416', 'http://127.0.0.1:4416']
                    }
                }
            }
            if os.path.exists(Config.YT_COOKIES_PATH):
                ydl_opts['cookiefile'] = Config.YT_COOKIES_PATH
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                entries = info.get('entries', [])
                if entries and entries[0]:
                    e = entries[0]
                    return [{
                        'videoId': e.get('id'),
                        'title': e.get('title'),
                        'duration_seconds': e.get('duration')
                    }]
        except Exception as ydl_err:
            logger.error(f"yt-dlp fallback search error for '{query}': {ydl_err}")

        return []

    def get_video_info(self, video_id):
        """
        دریافت مستقیم و دقیق مشخصات ویدیو/موزیک با استراتژی چندمرحله‌ای (oEmbed -> YTMusic -> yt_dlp)
        """
        # ۱. استراتژی اول: استفاده از YouTube oEmbed (سریع‌ترین و ۱۰۰٪ بدون بلاک آی‌پی یا خطای بات)
        try:
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            res = requests.get(oembed_url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                title = data.get('title', 'Unknown Track')
                author = self.clean_artist_name(data.get('author_name', 'Unknown Artist'))
                
                # اگر نام ویدیو به صورت Artist - Title بود و نام کانال عمومی/تاپیک بود
                if ' - ' in title and (author in ['Unknown Artist', 'YouTube Track'] or 'topic' in author.lower() or 'records' in author.lower() or 'music' in author.lower()):
                    parts = title.split(' - ', 1)
                    if len(parts) == 2 and len(parts[0].strip()) > 0 and len(parts[1].strip()) > 0:
                        author = parts[0].strip()
                        title = parts[1].strip()
                        
                return {'title': title, 'artist': author, 'videoId': video_id}
        except Exception as e:
            logger.warning(f"YouTube oEmbed failed: {e}")

        # ۲. استراتژی دوم: YTMusic
        try:
            song = self.yt.get_song(video_id)
            if song and 'videoDetails' in song:
                details = song['videoDetails']
                title = details.get('title', 'Unknown Track')
                author = self.clean_artist_name(details.get('author', 'Unknown Artist'))
                return {'title': title, 'artist': author, 'videoId': video_id}
        except Exception as e:
            logger.warning(f"YT get_song info failed: {e}")

        # ۳. استراتژی سوم: yt_dlp
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'socket_timeout': 5,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['mweb', 'web'],
                    },
                    'youtubepot-bgutilhttp': {
                        'base_url': ['http://Lyraz_pot:4416', 'http://pot:4416', 'http://172.17.0.1:4416', 'http://127.0.0.1:4416']
                    }
                }
            }
            if os.path.exists(Config.YT_COOKIES_PATH):
                ydl_opts['cookiefile'] = Config.YT_COOKIES_PATH
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                if info:
                    title = info.get('title', 'Unknown Track')
                    raw_artist = info.get('artist') or info.get('uploader') or info.get('channel') or 'Unknown Artist'
                    artist = self.clean_artist_name(raw_artist)
                    if ' - ' in title and artist in ['Unknown Artist', info.get('uploader'), info.get('channel')]:
                        parts = title.split(' - ', 1)
                        artist = parts[0].strip()
                        title = parts[1].strip()
                    return {'title': title, 'artist': artist, 'videoId': video_id}
        except Exception as e:
            logger.error(f"yt_dlp get_video_info error: {e}")

        return {'title': 'YouTube Track', 'artist': 'Unknown Artist', 'videoId': video_id}

    def apply_metadata_to_file(self, file_path, metadata):
        """
        تزریق کاور، لیریک و مشخصات دقیق به هدر فایل MP3 با استفاده از ID3v2.
        این کار باعث می‌شود فایل در تمامی پلیرهای آفلاین هویت کامل داشته باشد.
        """
        if not metadata:
            return

        try:
            audio = MP3(file_path, ID3=ID3)
            
            # اگر فایل تگ ID3 نداشت، آن را بساز
            try:
                audio.add_tags()
            except error:
                pass  # تگ از قبل وجود دارد (توسط ffmpeg ساخته شده)

            # ۱. اصلاح نام آهنگ و خواننده
            if metadata.get('title'):
                audio.tags.add(TIT2(encoding=3, text=metadata['title']))
            if metadata.get('artist'):
                audio.tags.add(TPE1(encoding=3, text=metadata['artist']))

            # ۲. تزریق کاور با کیفیت (APIC)
            if metadata.get('cover_bytes'):
                audio.tags.add(
                    APIC(
                        encoding=3,  # UTF-8
                        mime='image/jpeg',
                        type=3,  # نوع 3 یعنی کاور جلوی آلبوم (Front Cover)
                        desc=u'Cover',
                        data=metadata['cover_bytes']
                    )
                )

            # ۳. تزریق متن لیریک (USLT)
            if metadata.get('lyrics'):
                audio.tags.add(
                    USLT(
                        encoding=3,  # UTF-8 برای پشتیبانی کامل از فارسی
                        lang=u'eng', # زبان (تثبیت‌شده روی eng یا und برای سازگاری بهتر)
                        desc=u'Lyrics',
                        text=metadata['lyrics']
                    )
                )

            audio.save()
            logger.info(f"[+] Metadata stitched successfully: {os.path.basename(file_path)}")
            
        except Exception as e:
            logger.error(f"[-] Mutagen Stitching Error: {e}")


    async def download(self, video_id, quality=None, metadata=None):
        target_quality = str(quality) if quality else str(Config.AUDIO_QUALITY)
        final_path = os.path.join(self.download_dir, f"{video_id}.mp3")

        # اگر فایل از قبل بود، فقط متادیتا را دوباره چک/تزریق کن و برگردان
        if os.path.exists(final_path):
            logger.info(f"[+] Cached: {final_path}")
            if metadata:
                self.apply_metadata_to_file(final_path, metadata)
            return final_path

        # ۱. آماده‌سازی منابع دانلود (لینک مستقیم یوتیوب / ساندکلاد -> جستجوهای پشتیبان)
        raw_artist = metadata.get('artist', '') if metadata else ''
        raw_title = metadata.get('title', '') if metadata else ''
        source_url = metadata.get('source_url', '') if metadata else ''
        clean_art = re.sub(r'\s*-\s*Topic$', '', raw_artist, flags=re.IGNORECASE).strip()
        search_query = f"{clean_art} {raw_title}".strip()
        if search_query in ['Unknown Artist Unknown Track', 'Unknown Artist YouTube Track', 'Unknown Track']:
            search_query = ""

        if source_url and ('soundcloud.com' in source_url or video_id.startswith('sc_')):
            sources = [source_url]
            if search_query:
                sources.append(f"ytsearch1:{search_query}")
                sources.append(f"scsearch1:{search_query}")
        elif video_id.startswith('sc_'):
            sources = []
            if search_query:
                sources.append(f"ytsearch1:{search_query}")
                sources.append(f"scsearch1:{search_query}")
        else:
            sources = [f"https://www.youtube.com/watch?v={video_id}"]
            if search_query:
                sources.append(f"ytsearch1:{search_query}")
                sources.append(f"scsearch1:{search_query}")

        logger.info(f"[*] Starting Multi-Source Download for [{video_id}] | Quality: {target_quality}kbps")

        for source in sources:
            output_template = os.path.join(self.download_dir, f"{video_id}.%(ext)s")
            ydl_opts = {
                'format': 'ba/ba*',
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'nocheckcertificate': True,
                'geo_bypass': True,
                'socket_timeout': 5,
                'retries': 2,
                'fragment_retries': 2,
                'concurrent_fragment_downloads': 8,
                'buffersize': 1024 * 1024,
                'http_chunk_size': 10485760,
                'extractor_args': {
                    'youtube': {
                        'player_client': ['mweb', 'web'],
                    },
                    'youtubepot-bgutilhttp': {
                        'base_url': ['http://Lyraz_pot:4416', 'http://pot:4416', 'http://172.17.0.1:4416', 'http://127.0.0.1:4416']
                    }
                },
                'postprocessors': [
                    {
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': target_quality,
                    },
                    {'key': 'FFmpegMetadata', 'add_metadata': True},
                ],
                'postprocessor_args': {
                    'ffmpeg': ['-threads', '1', '-vn'],
                    'ExtractAudio': ['-threads', '1', '-vn']
                }
            }

            if self.ffmpeg_path and os.path.exists(self.ffmpeg_path):
                ydl_opts['ffmpeg_location'] = self.ffmpeg_path

            # لود کردن کوکی‌ها برای سورس‌های وب در صورت وجود
            if os.path.exists(Config.YT_COOKIES_PATH) and not source.startswith('scsearch'):
                try:
                    if os.path.getsize(Config.YT_COOKIES_PATH) > 50:
                        ydl_opts['cookiefile'] = Config.YT_COOKIES_PATH
                except Exception: pass

            try:
                def run_dl(src=source, opts=ydl_opts):
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(src, download=True)

                info = await asyncio.to_thread(run_dl)
                if info and os.path.exists(final_path):
                    logger.info(f"[+] Successfully downloaded via [{source}]: {final_path}")
                    if metadata:
                        self.apply_metadata_to_file(final_path, metadata)
                    return final_path
            except Exception as e:
                logger.warning(f"[-] Source failed [{source}]: {e}")
                continue

        logger.error(f"[-] All download sources exhausted for: {video_id}")
        return None

    def purge_stale_cache(self, max_age_seconds=3600):
        """پاکسازی خودکار فایل‌های واسط و کش‌های موقت yt_cache که بیش از ۱ ساعت از ساخت آن‌ها گذشته است"""
        try:
            if not os.path.exists(self.download_dir):
                return
            now = time.time()
            for fname in os.listdir(self.download_dir):
                fpath = os.path.join(self.download_dir, fname)
                if os.path.isfile(fpath):
                    try:
                        if (now - os.path.getmtime(fpath)) > max_age_seconds:
                            os.remove(fpath)
                    except Exception:
                        pass
        except Exception:
            pass

    def cleanup(self, file_path=None, video_id=None):
        """پاکسازی کامل فایل اصلی و کلیه فایل‌های موقت واسط (.webm, .m4a, .part, cmp_*)"""
        try:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass

            vid = video_id
            if not vid and file_path:
                base = os.path.basename(file_path)
                vid = os.path.splitext(base)[0]
                if vid.startswith('cmp_'):
                    vid = vid[4:]

            if vid and os.path.exists(self.download_dir):
                for fname in os.listdir(self.download_dir):
                    if vid in fname:
                        fpath = os.path.join(self.download_dir, fname)
                        if os.path.isfile(fpath):
                            try: os.remove(fpath)
                            except: pass
        except Exception:
            pass