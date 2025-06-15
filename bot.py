import logging
import random
import string
import time
import asyncio
import aiohttp
from typing import List, Dict, Set, Tuple, Optional
from telegram import Update, ReplyKeyboardMarkup, InputMediaPhoto, InputMediaAnimation
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import RetryAfter, BadRequest
from bs4 import BeautifulSoup
import signal
import sys

# ======================= НАСТРОЙКИ ЛОГИРОВАНИЯ =======================
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("image_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ======================= КОНФИГУРАЦИЯ БОТА ===========================
# Весовые коэффициенты для распределения поиска между источниками
# Определяют вероятность выбора каждого источника при поиске
# Сумма всех значений должна быть равна 1.0 (100%)
SOURCE_WEIGHTS = {
    'imgur5': 0.1,   # 10% - Imgur с 5-символьными кодами
    'imgur7': 0.2,   # 20% - Imgur с 7-символьными кодами  
    'prnt': 0.2,     # 20% - Prnt.sc
    'pastenow': 0.2, # 20% - Paste.pics
    'freeimage': 0.3 # 30% - Freeimage (самый высокий приоритет)
}

# Размеры пакетов для проверки изображений
# Сколько URL генерировать и проверять за один раз для каждого источника
BATCH_SIZES = {
    'imgur5': 5,    # 5 URL за раз для Imgur5
    'imgur7': 10,   # 10 URL за раз для Imgur7
    'prnt': 10,     # 10 кодов за раз для Prnt.sc
    'pastenow': 10, # 10 кодов за раз для Paste.pics
    'freeimage': 10 # 10 URL за раз для Freeimage
}

# Время блокировки источника после ошибки (в секундах)
SOURCE_TIMEOUT = 600  # 10 минут

# Настройки группировки медиа при отправке
MAX_GROUP_SIZE = 10    # Макс. количество изображений в одном сообщении
GROUP_TIMEOUT = 60     # 1 минута - ждем наполнения группы перед отправкой

# Настройки обновления статуса
STATUS_UPDATE_INTERVAL = 10  # Обновлять статус каждые 10 секунд
UPDATE_ON_FOUND = 5          # Обновлять статус каждые 5 найденных изображений
UPDATE_ON_CHECKED = 50       # Обновлять статус каждые 50 проверенных URL

# Таймауты и ограничения
MEDIA_SEND_TIMEOUT = 60     # 60 сек на отправку медиа в Telegram
MAX_CONCURRENT_TASKS = 30   # Макс. одновременных задач проверки URL
MAX_RETRIES = 3             # Макс. попыток при ошибках
COOLDOWN_DURATION = 180     # 3 минуты кулдауна между командами
REQUEST_TIMEOUT = 15        # 15 сек таймаут HTTP-запросов
# ======================================================================

def format_time(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours > 0:
        return f"{hours}ч {minutes}м {seconds}с"
    elif minutes > 0:
        return f"{minutes}м {seconds}с"
    else:
        return f"{seconds}с"

def get_source_name(source_type: str, length: int = None) -> str:
    names = {
        "imgur": f"Imgur ({length} симв.)" if length else "Imgur",
        "prnt": "Prnt.sc",
        "pastenow": "Paste.pics",
        "freeimage": "Freeimage",
        "all": "Все источники"
    }
    return names.get(source_type, source_type)

class ImageBot:
    def __init__(self):
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        ]
        
        self.sessions = {}
        self.last_commands = {}
        self.media_groups = {}
        self.sent_image_ids = {}
        self.command_cooldowns = {}
        self.source_errors = {}
        
        self.send_semaphore = asyncio.Semaphore(5)
        self.session = None
        self._session_initialized = False

    async def get_session(self):
        if not self._session_initialized or (self.session and self.session.closed):
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            connector = aiohttp.TCPConnector(limit=50, force_close=True)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            self._session_initialized = True
        return self.session

    async def cleanup(self):
        if self.session and not self.session.closed:
            await self.session.close()
        for key in list(self.sessions.keys()):
            await self.stop_session(key, "shutdown")

    def get_key(self, update: Update) -> Tuple[int, int]:
        return (update.effective_chat.id, update.effective_user.id)

    def extract_image_id(self, url: str) -> str:
        if "imgur.com" in url:
            return url.split("/")[-1].split(".")[0]
        elif "prnt.sc" in url or "prntscr.com" in url:
            return url.split("/")[-1]
        elif "paste.pics" in url:
            return url.split("/")[-1].split("?")[0]
        elif "iili.io" in url:
            return url.split("/")[-1].split(".")[0]
        return url.split("/")[-1][:10]

    async def check_cooldown(self, update: Update) -> bool:
        key = self.get_key(update)
        last_time = self.command_cooldowns.get(key, 0)
        remaining = (last_time + COOLDOWN_DURATION) - time.time()
        
        if remaining > 0:
            await update.message.reply_text(
                f"⚠️ Подождите {format_time(int(remaining))} перед следующей командой."
            )
            return True
        return False

    def generate_random_string(self, length: int) -> str:
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choices(chars, k=length))

    async def check_image(self, url: str, source: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            session = await self.get_session()
            headers = {"User-Agent": random.choice(self.user_agents)}
            
            if any(x in url.lower() for x in ["placeholder", "removed", "error", "404"]):
                return None, None
                
            try:
                async with session.head(url, headers=headers, 
                                     allow_redirects=True,
                                     timeout=aiohttp.ClientTimeout(total=10)) as response:
                    
                    if response.status != 200:
                        return None, None
                        
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(x in content_type for x in ['image', 'video']):
                        return None, None
                    
                    final_url = str(response.url)
                    if any(x in final_url.lower() for x in ["removed", "deleted", "error"]):
                        return None, None
                    
                    if 'gif' in content_type:
                        return final_url, 'gif'
                    elif 'jpeg' in content_type or 'jpg' in content_type:
                        return final_url, 'jpg'
                    elif 'png' in content_type:
                        return final_url, 'png'
                        
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"Timeout/connection error for {url}: {str(e)}")
                return None, None
                
        except Exception as e:
            logger.error(f"Error checking image {url}: {str(e)}")
        return None, None

    async def extract_prnt_image_url(self, code: str) -> Optional[str]:
        try:
            if self.is_source_disabled("prnt"):
                return None
                
            session = await self.get_session()
            url = f"https://prnt.sc/{code}"
            headers = {
                "User-Agent": random.choice(self.user_agents),
                "Referer": "https://prnt.sc/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1"
            }
            
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return None
                        
                    text = await response.text()
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"Prnt.sc connection error: {str(e)}")
                self.source_errors["prnt"] = time.time()
                return None

            soup = BeautifulSoup(text, "html.parser")
            
            no_image_div = soup.find('div', class_='no-image')
            if no_image_div:
                return None

            img_url = None
            img_tag = soup.find("img", {"class": "screenshot-image"})
            
            if img_tag and img_tag.get("src"):
                img_url = img_tag["src"]
                if any(x in img_url.lower() for x in ["placeholder", "st.prntscr.com"]):
                    return None
            else:
                meta_image = soup.find("meta", property="og:image")
                if meta_image and meta_image.get("content"):
                    img_url = meta_image["content"]

            if not img_url:
                return None
                
            if img_url.startswith("//"):
                img_url = f"https:{img_url}"
            elif not img_url.startswith("http"):
                return None
                
            if any(x in img_url.lower() for x in ["prnt.sc", "prntscr.com/placeholder", "st.prntscr.com"]):
                return None
                
            if "removed.png" in img_url.lower():
                return None
                
            return img_url
            
        except Exception as e:
            logger.error(f"Prnt.sc parsing error: {str(e)}")
            self.source_errors["prnt"] = time.time()
            return None

    async def extract_pastenow_image_url(self, code: str) -> Optional[str]:
        try:
            if self.is_source_disabled("pastenow"):
                return None
                
            session = await self.get_session()
            url = f"https://paste.pics/{code}"
            headers = {
                "User-Agent": random.choice(self.user_agents),
                "Referer": "https://paste.pics/",
            }
            
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return None
                        
                    text = await response.text()
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"PasteNow connection error: {str(e)}")
                self.source_errors["pastenow"] = time.time()
                return None

            soup = BeautifulSoup(text, "html.parser")
            
            img_url = None
            for tag in soup.find_all(['img', 'meta']):
                if tag.name == 'img' and tag.get('src'):
                    img_url = tag['src']
                    if any(x in img_url.lower() for x in ["logo", "placeholder"]):
                        continue
                    break
                elif tag.name == 'meta' and tag.get('property') == 'og:image':
                    img_url = tag.get('content')
                    break
                    
            if not img_url:
                return None
                
            if img_url.startswith("//"):
                img_url = f"https:{img_url}"
            elif not img_url.startswith("http"):
                return None
                
            return img_url
            
        except Exception as e:
            logger.error(f"PasteNow parsing error: {str(e)}")
            self.source_errors["pastenow"] = time.time()
            return None

    async def extract_imgur_image_url(self, code: str, length: int) -> Optional[str]:
        try:
            url = f"https://i.imgur.com/{code}.jpg"
            
            session = await self.get_session()
            headers = {"User-Agent": random.choice(self.user_agents)}
            
            async with session.head(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 404:
                    return None
                    
                final_url = str(response.url)
                if "removed" in final_url.lower():
                    return None
                    
            return url
            
        except Exception as e:
            logger.error(f"Imgur check error: {str(e)}")
            return None

    async def extract_freeimage_url(self, code: str) -> Optional[str]:
        try:
            url = f"https://iili.io/{code}.jpg"
            
            session = await self.get_session()
            headers = {"User-Agent": random.choice(self.user_agents)}
            
            async with session.head(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 404:
                    return None
                    
                final_url = str(response.url)
                if any(x in final_url.lower() for x in ["error", "404", "removed"]):
                    return None
                    
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("image/"):
                    return None
                    
            return url
            
        except Exception as e:
            logger.error(f"FreeImage check error: {str(e)}")
            return None

    async def add_to_media_group(self, update: Update, key: Tuple[int, int], url: str, ext: str, count: int, found: int, source: str):
        try:
            image_id = self.extract_image_id(url)
            display_url = f"[{image_id}]({url})"
            caption = f"({found}/{count}) {display_url} [{source.upper()}]"
            
            if key not in self.sent_image_ids:
                self.sent_image_ids[key] = set()
            
            if image_id in self.sent_image_ids[key]:
                logger.info(f"Изображение {image_id} уже отправлено, пропускаем")
                return
            
            self.sent_image_ids[key].add(image_id)
            
            if ext == "gif":
                media_item = InputMediaAnimation(media=url, caption=caption, parse_mode="Markdown")
                await self.send_media(update, key, [media_item])
                return
            
            if key not in self.media_groups:
                self.media_groups[key] = {
                    "media": [],
                    "last_added": time.time(),
                    "timer_task": None
                }
            
            group = self.media_groups[key]
            
            for media in group["media"]:
                media_id = self.extract_image_id(media.media)
                if media_id == image_id:
                    logger.info(f"Изображение {image_id} уже в группе, пропускаем")
                    return
            
            media_item = InputMediaPhoto(media=url, caption=caption, parse_mode="Markdown")
            group["media"].append(media_item)
            group["last_added"] = time.time()
            
            if group["timer_task"] is None or group["timer_task"].done():
                group["timer_task"] = asyncio.create_task(self.group_timer(update, key))
            
            if len(group["media"]) >= MAX_GROUP_SIZE:
                media_to_send = group["media"]
                group["media"] = []
                await self.send_media(update, key, media_to_send)
        except Exception as e:
            logger.error(f"Ошибка добавления в медиагруппу: {str(e)}")

    async def send_media(self, update: Update, key: Tuple[int, int], media_group: List):
        if not media_group:
            return
            
        async with self.send_semaphore:
            try:
                photos = [m for m in media_group if not isinstance(m, InputMediaAnimation)]
                animations = [m for m in media_group if isinstance(m, InputMediaAnimation)]
                
                if photos:
                    try:
                        await update.message.reply_media_group(
                            media=photos,
                            parse_mode="Markdown",
                            write_timeout=MEDIA_SEND_TIMEOUT,
                            connect_timeout=MEDIA_SEND_TIMEOUT
                        )
                        logger.info(f"Отправлена группа из {len(photos)} фото пользователю {key}")
                    except RetryAfter as e:
                        wait_time = e.retry_after
                        logger.warning(f"Flood control. Ждем {wait_time} сек.")
                        await asyncio.sleep(wait_time)
                        await update.message.reply_media_group(
                            media=photos,
                            parse_mode="Markdown",
                            write_timeout=MEDIA_SEND_TIMEOUT,
                            connect_timeout=MEDIA_SEND_TIMEOUT
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки медиагруппы: {str(e)}")
                        for photo in photos:
                            try:
                                await update.message.reply_photo(
                                    photo=photo.media,
                                    caption=photo.caption,
                                    parse_mode="Markdown",
                                    write_timeout=MEDIA_SEND_TIMEOUT,
                                    connect_timeout=MEDIA_SEND_TIMEOUT
                                )
                            except Exception as e:
                                logger.error(f"Ошибка отправки фото: {str(e)}")

                for animation in animations:
                    try:
                        await update.message.reply_animation(
                            animation=animation.media,
                            caption=animation.caption,
                            parse_mode="Markdown",
                            write_timeout=MEDIA_SEND_TIMEOUT,
                            connect_timeout=MEDIA_SEND_TIMEOUT
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки анимации: {str(e)}")
            except Exception as e:
                logger.error(f"Общая ошибка отправки медиа: {str(e)}")

    async def group_timer(self, update: Update, key: Tuple[int, int]):
        try:
            while True:
                await asyncio.sleep(GROUP_TIMEOUT)
                
                if key not in self.media_groups:
                    break
                    
                group = self.media_groups[key]
                
                if not group["media"]:
                    break
                
                current_time = time.time()
                
                if (current_time - group["last_added"]) >= GROUP_TIMEOUT:
                    media_to_send = group["media"]
                    group["media"] = []
                    await self.send_media(update, key, media_to_send)
                    break
        except Exception as e:
            logger.error(f"Ошибка таймера группы: {str(e)}")
        finally:
            if key in self.media_groups:
                self.media_groups[key]["timer_task"] = None

    async def show_main_menu(self, update: Update):
        reply_keyboard = [
            ["ВСЕ ИСТОЧНИКИ"],
            ["PRNT.SC", "IMGUR"],
            ["PASTENOW", "FREEIMAGE"],
            ["ПОВТОРИТЬ", "СТОП"]
        ]
        await update.message.reply_text(
            "Выберите источник изображений:",
            reply_markup=ReplyKeyboardMarkup(
                reply_keyboard,
                resize_keyboard=True,
                is_persistent=True,
                one_time_keyboard=False,
            ),
        )

    async def start(self, update: Update, context: CallbackContext):
        await self.show_main_menu(update)
        await update.message.reply_text(
            """
Привет! Я бот для поиска случайных изображений.

Используйте кнопки или команды:
/getimg <5|7> <1-50> - поиск на Imgur
/getprnt <1-50> - поиск на prnt.sc
/getpastenow <1-50> - поиск на paste.pics
/getfreeimage <1-50> - поиск на freeimage
/getall <1-50> - поиск на всех источниках
/stop - остановить поиск
/repeat - повторить последний поиск

⚠️ Важно!

В некоторых источниках могут встречаться неприятные, шокирующие или NSFW-материалы. Используйте бота на свой страх и риск.

Если вам попался нежелательный контент – просто пропустите его. Будьте осторожны!
"""
        )

    async def stop(self, update: Update, context: CallbackContext, silent: bool = False):
        key = self.get_key(update)
        session = self.sessions.get(key)
        
        if not session:
            if not silent:
                await update.message.reply_text("❗️ Нет активного поиска.")
            return
    
        source_name = get_source_name(session["source_type"], session.get("length"))
        elapsed = int(time.time() - session["start_time"])
        logger.info(f"Поиск пользователя {key} остановлен. Источник: {source_name}. Время: {format_time(elapsed)}")
        
        session["stop"] = True
        
        media_group = []
        if key in self.media_groups:
            group_data = self.media_groups.pop(key, {})
            media_group = group_data.get("media", [])
        
        if media_group:
            await self.send_media(update, key, media_group)
        
        self.cleanup_user_session(key)
        
        if not silent:
            target = session["target_count"]
            found = session.get("found", 0)
            analyzed = session.get("analyzed", 0)
            stop_reason = session.get("stop_reason", "")
            
            message = f"🔴 Поиск остановлен\n"
            if stop_reason == "source_disabled":
                message += "⚠️ Источник временно недоступен\n"
            message += (
                f"Цель: {target} изображений\n"
                f"Найдено: {found}/{target}\n"
                f"Проверено: {analyzed}\n"
                f"Время: {format_time(elapsed)}"
            )
            
            await update.message.reply_text(message)
            await self.show_main_menu(update)

    async def stop_session(self, key: Tuple[int, int], reason: str = "user"):
        session = self.sessions.get(key)
        if not session:
            return
            
        source_name = get_source_name(session["source_type"], session.get("length"))
        elapsed = int(time.time() - session["start_time"])
        logger.info(f"Поиск пользователя {key} остановлен. Причина: {reason}. Источник: {source_name}. Время: {format_time(elapsed)}")
        
        session["stop"] = True
        session["stop_reason"] = reason
        
        self.cleanup_user_session(key)

    def cleanup_user_session(self, key: Tuple[int, int]):
        if key in self.sessions:
            session = self.sessions[key]
            if "tasks" in session:
                for task in session["tasks"]:
                    if not task.done():
                        task.cancel()
            elif "task" in session and session["task"]:
                if not session["task"].done():
                    session["task"].cancel()
            del self.sessions[key]
        
        if key in self.media_groups:
            del self.media_groups[key]
        
        if key in self.sent_image_ids:
            del self.sent_image_ids[key]

    async def repeat_last_command(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        last_command = self.last_commands.get(key)
        if not last_command:
            await update.message.reply_text("❗️ Нет предыдущей команды для повторения.")
            return
        
        if key in self.sessions:
            current_session = self.sessions[key]
            if (current_session["source_type"] == last_command["type"] and 
                current_session.get("length", 0) == last_command.get("length", 0) and 
                current_session["target_count"] == last_command["count"]):
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        if last_command["type"] == "imgur":
            context.args = [str(last_command["length"]), str(last_command["count"])]
            await self.get_imgur_images(update, context)
        elif last_command["type"] == "prnt":
            context.args = [str(last_command["count"])]
            await self.get_prnt_images(update, context)
        elif last_command["type"] == "pastenow":
            context.args = [str(last_command["count"])]
            await self.get_pastenow_images(update, context)
        elif last_command["type"] == "freeimage":
            context.args = [str(last_command["count"])]
            await self.get_freeimage_images(update, context)
        elif last_command["type"] == "all":
            context.args = [str(last_command["count"])]
            await self.search_all_sources(update, context)

    async def update_status_message(self, key: Tuple[int, int], force: bool = False):
        session = self.sessions.get(key)
        if not session:
            return
            
        status_msg = session.get("status_msg")
        if not status_msg:
            return
            
        try:
            target = session["target_count"]
            found = session.get("found", 0)
            analyzed = session.get("analyzed", 0)
            source_name = get_source_name(session["source_type"], session.get("length"))
            
            text = (
                f"🔍 Поиск {source_name} в процессе\n"
                f"Цель: {target} изображений\n"
                f"Найдено: {found}/{target}\n"
                f"Проверено: {analyzed}\n"
                f"Время: {format_time(int(time.time() - session['start_time']))}"
            )
            
            last_text = session.get("last_status_text", "")
            if force or text != last_text:
                try:
                    await status_msg.edit_text(text)
                    session["last_status_text"] = text
                except RetryAfter as e:
                    logger.warning(f"Flood control при обновлении статуса. Ждем {e.retry_after} сек.")
                    await asyncio.sleep(e.retry_after)
                    await status_msg.edit_text(text)
                    session["last_status_text"] = text
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.error(f"Ошибка при обновлении статуса: {str(e)}")
                except Exception as e:
                    logger.error(f"Ошибка при обновлении статуса: {str(e)}")
        except Exception as e:
            logger.error(f"Ошибка при подготовке статуса: {str(e)}")

    def is_source_disabled(self, source: str) -> bool:
        if source in self.source_errors:
            return (time.time() - self.source_errors[source]) < SOURCE_TIMEOUT
        return False

    async def handle_source_error(self, source: str, message: str):
        logger.error(f"Ошибка источника {source}: {message}")
        self.source_errors[source] = time.time()
        return source

    async def search_all_sources(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        
        if await self.check_cooldown(update):
            return
            
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getall <1-50>")
            return

        try:
            count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "all" and current_session["target_count"] == count:
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        status_msg = await update.message.reply_text(
            f"🔍 Поиск всех источников начат\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )
        
        session_data = {
            "tasks": [],
            "stop": False,
            "start_time": time.time(),
            "last_update": time.time(),
            "last_found_time": time.time(),
            "status_msg": status_msg,
            "target_count": count,
            "found": 0,
            "analyzed": 0,
            "source_type": "all",
            "sources": {
                "imgur5": {"active": True, "found": 0},
                "imgur7": {"active": True, "found": 0},
                "prnt": {"active": True, "found": 0},
                "pastenow": {"active": True, "found": 0},
                "freeimage": {"active": True, "found": 0}
            }
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "all", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        logger.info(f"Поиск пользователя {key} начат. Все источники. Цель: {count} изображений")
        
        asyncio.create_task(self._search_all_sources(update, key, count))

    async def _search_all_sources(self, update: Update, key: Tuple[int, int], count: int):
        async def search_source(source_type: str, length: int = None):
            nonlocal session
            batch_size = BATCH_SIZES.get(source_type, 10)
            weight = SOURCE_WEIGHTS.get(source_type, 0.2)
            max_per_source = min(50, max(1, round(weight * count * 1.5)))
            last_update_time = time.time()
            retries = 0
            
            while not session.get("stop", False):
                if session["found"] >= count:
                    break
                if session["sources"][source_type]["found"] >= max_per_source:
                    break
                if self.is_source_disabled(source_type):
                    logger.info(f"Источник {source_type} временно отключен")
                    session["sources"][source_type]["active"] = False
                    break
                
                urls_to_check = []
                if source_type in ["imgur5", "imgur7"]:
                    tasks = [self.extract_imgur_image_url(self.generate_random_string(length), length) 
                            for _ in range(batch_size)]
                elif source_type == "prnt":
                    tasks = [self.extract_prnt_image_url(self.generate_random_string(6))
                            for _ in range(batch_size)]
                elif source_type == "pastenow":
                    tasks = [self.extract_pastenow_image_url(self.generate_random_string(5))
                            for _ in range(batch_size)]
                elif source_type == "freeimage":
                    tasks = [self.extract_freeimage_url(self.generate_random_string(7))
                            for _ in range(batch_size)]
                
                try:
                    results = await asyncio.gather(*tasks)
                    
                    for url in results:
                        if session.get("stop", False) or session["found"] >= count:
                            break
                            
                        if not url:
                            continue
                            
                        _, ext = await self.check_image(url, source_type)
                        if not ext:
                            continue
                            
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        
                        if analyzed % UPDATE_ON_CHECKED == 0:
                            await self.update_status_message(key, force=True)
                        
                        current_time = time.time()
                        if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                            await self.update_status_message(key)
                            last_update_time = current_time
                        
                        session["found"] += 1
                        session["sources"][source_type]["found"] += 1
                        session["last_found_time"] = time.time()
                        found = session["found"]
                        
                        await self.add_to_media_group(
                            update, key, url, ext, count, found, source_type
                        )
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                    
                    retries = 0
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Ошибка при поиске в {source_type}: {str(e)}")
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["sources"][source_type]["active"] = False
                        await self.handle_source_error(source_type, str(e))
                        break
                    await asyncio.sleep(1)
        
        try:
            session = self.sessions[key]
            
            tasks = []
            if not self.is_source_disabled("imgur5"):
                tasks.append(asyncio.create_task(search_source("imgur5", 5)))
            if not self.is_source_disabled("imgur7"):
                tasks.append(asyncio.create_task(search_source("imgur7", 7)))
            if not self.is_source_disabled("prnt"):
                tasks.append(asyncio.create_task(search_source("prnt")))
            if not self.is_source_disabled("pastenow"):
                tasks.append(asyncio.create_task(search_source("pastenow")))
            if not self.is_source_disabled("freeimage"):
                tasks.append(asyncio.create_task(search_source("freeimage")))
            
            session["tasks"] = tasks
            await asyncio.gather(*tasks)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск всех источников для {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске всех источников: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key].get("media"):
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                
                if stop_reason == "source_disabled":
                    logger.info(f"Поиск пользователя {key} остановлен из-за недоступности источника. Время: {format_time(elapsed)}")
                else:
                    logger.info(f"Поиск пользователя {key} завершен. Найдено: {found}/{target}. Время: {format_time(elapsed)}")
                
                source_name = get_source_name(session["source_type"])
                message = (
                    f"✅ Поиск {source_name} завершен\n"
                    f"Цель: {target} изображений\n"
                    f"Найдено: {found}/{target}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {format_time(elapsed)}"
                )
                
                try:
                    await update.message.reply_text(message)
                except Exception as e:
                    logger.error(f"Ошибка при отправке финального сообщения: {str(e)}")
                
                self.cleanup_user_session(key)
                await self.show_main_menu(update)

    async def get_imgur_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        
        if await self.check_cooldown(update):
            return
            
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Используйте: /getimg <5|7> <1-50>")
            return

        try:
            length = int(args[0])
            count = int(args[1])
        except ValueError:
            await update.message.reply_text("Длина и количество должны быть числами")
            return

        if length not in [5, 7]:
            await update.message.reply_text("Длина может быть только 5 или 7 символов")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        if key in self.sessions:
            current_session = self.sessions[key]
            if (current_session["source_type"] == "imgur" and 
                current_session.get("length", 0) == length and 
                current_session["target_count"] == count):
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        status_msg = await update.message.reply_text(
            f"🔍 Поиск Imgur начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )
        
        session_data = {
            "task": None,
            "stop": False,
            "start_time": time.time(),
            "last_update": time.time(),
            "last_found_time": time.time(),
            "status_msg": status_msg,
            "target_count": count,
            "found": 0,
            "analyzed": 0,
            "source_type": "imgur",
            "length": length
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "imgur", "length": length, "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        logger.info(f"Поиск пользователя {key} начат. Imgur ({length}). Цель: {count} изображений")
        
        asyncio.create_task(self._search_imgur(update, key, length, count))

    async def _search_imgur(self, update: Update, key: Tuple[int, int], length: int, count: int):
        try:
            session = self.sessions[key]
            session["task"] = asyncio.current_task()
            last_update_time = time.time()
            retries = 0
            
            while not session.get("stop", False) and session["found"] < count:
                code = self.generate_random_string(length)
                
                try:
                    url = await self.extract_imgur_image_url(code, length)
                    if not url:
                        continue
                        
                    _, ext = await self.check_image(url, "imgur")
                    if not ext:
                        continue
                        
                    session["analyzed"] += 1
                    analyzed = session["analyzed"]
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    session["found"] += 1
                    session["last_found_time"] = time.time()
                    found = session["found"]
                    
                    await self.add_to_media_group(
                        update, key, url, ext, count, found, "imgur"
                    )
                    
                    if found % UPDATE_ON_FOUND == 0:
                        await self.update_status_message(key, force=True)
                    
                    retries = 0
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения Imgur: {str(e)}")
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["stop"] = True
                        session["stop_reason"] = "error"
                        await self.handle_source_error("imgur", str(e))
                    await asyncio.sleep(0.5)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск Imgur для {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске Imgur: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key].get("media"):
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                
                if stop_reason == "source_disabled":
                    logger.info(f"Поиск пользователя {key} остановлен из-за недоступности источника. Время: {format_time(elapsed)}")
                else:
                    logger.info(f"Поиск пользователя {key} завершен. Найдено: {found}/{target}. Время: {format_time(elapsed)}")
                
                source_name = get_source_name(session["source_type"], session["length"])
                message = (
                    f"✅ Поиск {source_name} завершен\n"
                    f"Цель: {target} изображений\n"
                    f"Найдено: {found}/{target}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {format_time(elapsed)}"
                )
                
                try:
                    await update.message.reply_text(message)
                except Exception as e:
                    logger.error(f"Ошибка при отправке финального сообщения: {str(e)}")
                
                self.cleanup_user_session(key)
                await self.show_main_menu(update)

    async def get_prnt_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        
        if await self.check_cooldown(update):
            return
            
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getprnt <1-50>")
            return

        try:
            count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        if key in self.sessions:
            current_session = self.sessions[key]
            if (current_session["source_type"] == "prnt" and 
                current_session["target_count"] == count):
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        status_msg = await update.message.reply_text(
            f"🔍 Поиск prnt.sc начат\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )
        
        session_data = {
            "task": None,
            "stop": False,
            "start_time": time.time(),
            "last_update": time.time(),
            "last_found_time": time.time(),
            "status_msg": status_msg,
            "target_count": count,
            "found": 0,
            "analyzed": 0,
            "source_type": "prnt"
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "prnt", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        logger.info(f"Поиск пользователя {key} начат. Prnt.sc. Цель: {count} изображений")
        
        asyncio.create_task(self._search_prnt(update, key, count))

    async def _search_prnt(self, update: Update, key: Tuple[int, int], count: int):
        try:
            session = self.sessions[key]
            session["task"] = asyncio.current_task()
            last_update_time = time.time()
            retries = 0
            
            while not session.get("stop", False) and session["found"] < count:
                if self.is_source_disabled("prnt"):
                    logger.info("Источник prnt.sc временно отключен")
                    session["stop"] = True
                    session["stop_reason"] = "source_disabled"
                    break
                
                code = self.generate_random_string(6)
                
                try:
                    url = await self.extract_prnt_image_url(code)
                    if not url:
                        continue
                        
                    _, ext = await self.check_image(url, "prnt")
                    if not ext:
                        continue
                        
                    session["analyzed"] += 1
                    analyzed = session["analyzed"]
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    session["found"] += 1
                    session["last_found_time"] = time.time()
                    found = session["found"]
                    
                    await self.add_to_media_group(
                        update, key, url, ext, count, found, "prnt"
                    )
                    
                    if found % UPDATE_ON_FOUND == 0:
                        await self.update_status_message(key, force=True)
                    
                    retries = 0
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения prnt.sc: {str(e)}")
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["stop"] = True
                        session["stop_reason"] = "error"
                        await self.handle_source_error("prnt", str(e))
                    await asyncio.sleep(0.5)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск prnt.sc для {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске prnt.sc: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key].get("media"):
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                
                if session.get("stop", False) and found < target:
                    text = f"🔴 Поиск остановлен\n"
                    if stop_reason == "source_disabled":
                        text += "⚠️ Источник временно недоступен\n"
                    text += (
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                else:
                    text = (
                        f"✅ Поиск завершен\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                
                await update.message.reply_text(text)
                
                self.cleanup_user_session(key)
                await self.show_main_menu(update)

    async def get_pastenow_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        
        if await self.check_cooldown(update):
            return
            
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getpastenow <1-50>")
            return

        try:
            count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        if key in self.sessions:
            current_session = self.sessions[key]
            if (current_session["source_type"] == "pastenow" and 
                current_session["target_count"] == count):
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        status_msg = await update.message.reply_text(
            f"🔍 Поиск paste.pics начат\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )
        
        session_data = {
            "task": None,
            "stop": False,
            "start_time": time.time(),
            "last_update": time.time(),
            "last_found_time": time.time(),
            "status_msg": status_msg,
            "target_count": count,
            "found": 0,
            "analyzed": 0,
            "source_type": "pastenow"
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "pastenow", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        logger.info(f"Поиск пользователя {key} начат. Paste.pics. Цель: {count} изображений")
        
        asyncio.create_task(self._search_pastenow(update, key, count))

    async def _search_pastenow(self, update: Update, key: Tuple[int, int], count: int):
        try:
            session = self.sessions[key]
            session["task"] = asyncio.current_task()
            last_update_time = time.time()
            retries = 0
            
            while not session.get("stop", False) and session["found"] < count:
                if self.is_source_disabled("pastenow"):
                    logger.info("Источник paste.pics временно отключен")
                    session["stop"] = True
                    session["stop_reason"] = "source_disabled"
                    break
                
                code = self.generate_random_string(5)
                
                try:
                    url = await self.extract_pastenow_image_url(code)
                    if not url:
                        continue
                        
                    _, ext = await self.check_image(url, "pastenow")
                    if not ext:
                        continue
                        
                    session["analyzed"] += 1
                    analyzed = session["analyzed"]
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    session["found"] += 1
                    session["last_found_time"] = time.time()
                    found = session["found"]
                    
                    await self.add_to_media_group(
                        update, key, url, ext, count, found, "pastenow"
                    )
                    
                    if found % UPDATE_ON_FOUND == 0:
                        await self.update_status_message(key, force=True)
                    
                    retries = 0
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения paste.pics: {str(e)}")
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["stop"] = True
                        session["stop_reason"] = "error"
                        await self.handle_source_error("pastenow", str(e))
                    await asyncio.sleep(0.5)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск paste.pics для {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске paste.pics: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key].get("media"):
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                
                if session.get("stop", False) and found < target:
                    text = f"🔴 Поиск остановлен\n"
                    if stop_reason == "source_disabled":
                        text += "⚠️ Источник временно недоступен\n"
                    text += (
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                else:
                    text = (
                        f"✅ Поиск завершен\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                
                await update.message.reply_text(text)
                
                self.cleanup_user_session(key)
                await self.show_main_menu(update)

    async def get_freeimage_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        
        if await self.check_cooldown(update):
            return
            
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getfreeimage <1-50>")
            return

        try:
            count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        if key in self.sessions:
            current_session = self.sessions[key]
            if (current_session["source_type"] == "freeimage" and 
                current_session["target_count"] == count):
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        status_msg = await update.message.reply_text(
            f"🔍 Поиск freeimage начат\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )
        
        session_data = {
            "task": None,
            "stop": False,
            "start_time": time.time(),
            "last_update": time.time(),
            "last_found_time": time.time(),
            "status_msg": status_msg,
            "target_count": count,
            "found": 0,
            "analyzed": 0,
            "source_type": "freeimage"
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "freeimage", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        logger.info(f"Поиск пользователя {key} начат. Freeimage. Цель: {count} изображений")
        
        asyncio.create_task(self._search_freeimage(update, key, count))

    async def _search_freeimage(self, update: Update, key: Tuple[int, int], count: int):
        try:
            session = self.sessions[key]
            session["task"] = asyncio.current_task()
            last_update_time = time.time()
            retries = 0
            
            while not session.get("stop", False) and session["found"] < count:
                if self.is_source_disabled("freeimage"):
                    logger.info("Источник freeimage временно отключен")
                    session["stop"] = True
                    session["stop_reason"] = "source_disabled"
                    break
                
                code = self.generate_random_string(7)
                
                try:
                    url = await self.extract_freeimage_url(code)
                    if not url:
                        continue
                        
                    _, ext = await self.check_image(url, "freeimage")
                    if not ext:
                        continue
                        
                    session["analyzed"] += 1
                    analyzed = session["analyzed"]
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    session["found"] += 1
                    session["last_found_time"] = time.time()
                    found = session["found"]
                    
                    await self.add_to_media_group(
                        update, key, url, ext, count, found, "freeimage"
                    )
                    
                    if found % UPDATE_ON_FOUND == 0:
                        await self.update_status_message(key, force=True)
                    
                    retries = 0
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения freeimage: {str(e)}")
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["stop"] = True
                        session["stop_reason"] = "error"
                        await self.handle_source_error("freeimage", str(e))
                    await asyncio.sleep(0.5)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск freeimage для {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске freeimage: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key].get("media"):
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                
                if session.get("stop", False) and found < target:
                    text = f"🔴 Поиск остановлен\n"
                    if stop_reason == "source_disabled":
                        text += "⚠️ Источник временно недоступен\n"
                    text += (
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                else:
                    text = (
                        f"✅ Поиск завершен\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                
                await update.message.reply_text(text)
                
                self.cleanup_user_session(key)
                await self.show_main_menu(update)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        key = self.get_key(update)

        if text not in ["СТОП", "НАЗАД"] and await self.check_cooldown(update):
            return

        if text == "PRNT.SC":
            reply_keyboard = [
                ["1", "3", "5"],
                ["10", "15", "25"],
                ["50", "НАЗАД"],
            ]
            await update.message.reply_text(
                "PRNT.SC - Выберите количество:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "prnt_sc"

        elif text == "IMGUR":
            reply_keyboard = [["5", "7"], ["НАЗАД"]]
            await update.message.reply_text(
                "IMGUR - Выберите длину ссылки:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "imgur_interval"

        elif text == "PASTENOW":
            reply_keyboard = [
                ["1", "3", "5"],
                ["10", "15", "25"],
                ["50", "НАЗАД"],
            ]
            await update.message.reply_text(
                "PASTENOW - Выберите количество:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "pastenow"

        elif text == "FREEIMAGE":
            reply_keyboard = [
                ["1", "3", "5"],
                ["10", "15", "25"],
                ["50", "НАЗАД"],
            ]
            await update.message.reply_text(
                "FREEIMAGE - Выберите количество:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "freeimage"

        elif text == "ВСЕ ИСТОЧНИКИ":
            reply_keyboard = [
                ["1", "3", "5"],
                ["10", "15", "25"],
                ["50", "НАЗАД"],
            ]
            await update.message.reply_text(
                "ВСЕ ИСТОЧНИКИ - Выберите количество:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "all_sources"

        elif text in ["5", "7"] and context.user_data.get("mode") == "imgur_interval":
            context.user_data["imgur_interval"] = text
            reply_keyboard = [
                ["1", "3", "5"],
                ["10", "15", "25"],
                ["50", "НАЗАД"],
            ]
            await update.message.reply_text(
                f"IMGUR - Выбрана длина {text}. Теперь выберите количество:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "imgur_numbers"

        elif text in ["1", "3", "5", "10", "15", "25", "50"]:
            if context.user_data.get("mode") == "imgur_numbers":
                interval = context.user_data["imgur_interval"]
                context.args = [interval, text]
                await self.get_imgur_images(update, context)
            elif context.user_data.get("mode") == "pastenow":
                context.args = [text]
                await self.get_pastenow_images(update, context)
            elif context.user_data.get("mode") == "freeimage":
                context.args = [text]
                await self.get_freeimage_images(update, context)
            elif context.user_data.get("mode") == "all_sources":
                context.args = [text]
                await self.search_all_sources(update, context)
            else:
                context.args = [text]
                await self.get_prnt_images(update, context)

            context.user_data.clear()

        elif text == "НАЗАД":
            await self.show_main_menu(update)
            context.user_data.clear()

        elif text == "СТОП":
            await self.stop(update, context)

        elif text == "ПОВТОРИТЬ":
            await self.repeat_last_command(update, context)

def shutdown_handler(signum, frame):
    logger.info("Получен сигнал остановки")
    sys.exit(0)

def main():
    bot = ImageBot()
    try:
        with open("token.txt", "r") as f:
            token = f.read().strip()
    except FileNotFoundError:
        logger.error("Создайте файл token.txt с токеном бота")
        return
    except Exception as e:
        logger.error(f"Ошибка чтения token.txt: {str(e)}")
        return

    if not token:
        logger.error("Токен бота не найден в token.txt")
        return

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("getimg", bot.get_imgur_images))
    application.add_handler(CommandHandler("getprnt", bot.get_prnt_images))
    application.add_handler(CommandHandler("getpastenow", bot.get_pastenow_images))
    application.add_handler(CommandHandler("getfreeimage", bot.get_freeimage_images))
    application.add_handler(CommandHandler("getall", bot.search_all_sources))
    application.add_handler(CommandHandler("stop", bot.stop))
    application.add_handler(CommandHandler("repeat", bot.repeat_last_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    logger.info("Бот запущен")
    print("Бот запущен. Нажмите Ctrl+C для остановки")
    
    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info("Получен сигнал KeyboardInterrupt")
    except Exception as e:
        logger.error(f"Ошибка при работе бота: {str(e)}")
    finally:
        logger.info("Завершение работы бота")
        sys.exit(0)

if __name__ == "__main__":
    main()
