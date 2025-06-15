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
    """Форматирует время в читаемый вид (чч:мм:сс)"""
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
    """Возвращает читаемое имя источника"""
    names = {
        "imgur": f"Imgur ({length} симв.)" if length else "Imgur",
        "prnt": "Prnt.sc",
        "pastenow": "Paste.pics",
        "freeimage": "Freeimage",
        "all": "Все источники"
    }
    return names.get(source_type, source_type)

class ImageSource:
    """Базовый класс для источников изображений"""
    def __init__(self, bot):
        self.bot = bot
    
    async def generate_urls(self, batch_size: int) -> List[str]:
        """Генерирует список URL для проверки"""
        raise NotImplementedError()
    
    async def extract_image_url(self, url: str) -> Optional[str]:
        """Извлекает URL изображения (для источников с парсингом)"""
        return url
    
    async def check_image(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Проверяет валидность изображения"""
        try:
            session = await self.bot.get_session()
            headers = {"User-Agent": random.choice(self.bot.user_agents)}
            
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

class ImgurSource(ImageSource):
    """Источник изображений Imgur"""
    def __init__(self, bot, length: int):
        super().__init__(bot)
        self.length = length
        
    async def generate_urls(self, batch_size: int) -> List[str]:
        urls = []
        for _ in range(batch_size):
            code = self.bot.generate_random_string(self.length)
            urls.append(f"https://i.imgur.com/{code}.jpg")
        return urls
    
    async def extract_image_url(self, url: str) -> Optional[str]:
        try:
            session = await self.bot.get_session()
            headers = {"User-Agent": random.choice(self.bot.user_agents)}
            
            async with session.head(url, headers=headers, 
                                 timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 404:
                    return None
                    
                final_url = str(response.url)
                if "removed" in final_url.lower():
                    return None
                    
            return url
        except Exception as e:
            logger.error(f"Imgur check error: {str(e)}")
            return None

class PrntSource(ImageSource):
    """Источник изображений Prnt.sc"""
    async def generate_urls(self, batch_size: int) -> List[str]:
        return [f"https://prnt.sc/{self.bot.generate_random_string(6)}" 
                for _ in range(batch_size)]
    
    async def extract_image_url(self, url: str) -> Optional[str]:
        try:
            session = await self.bot.get_session()
            headers = {
                "User-Agent": random.choice(self.bot.user_agents),
                "Referer": "https://prnt.sc/",
            }
            
            async with session.get(url, headers=headers, 
                                 timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return None
                    
                text = await response.text()

            soup = BeautifulSoup(text, "html.parser")
            
            # Проверка на отсутствие изображения
            if soup.find('div', class_='no-image'):
                return None

            # Поиск URL изображения
            img_url = None
            img_tag = soup.find("img", {"class": "screenshot-image"})
            if img_tag and img_tag.get("src"):
                img_url = img_tag["src"]
                # Фильтрация URL от imgur
                if "imgur.com" in img_url:
                    return None
            else:
                meta_image = soup.find("meta", property="og:image")
                if meta_image and meta_image.get("content"):
                    img_url = meta_image["content"]
                    # Фильтрация URL от imgur
                    if "imgur.com" in img_url:
                        return None

            # Если URL не найден или не прошел фильтрацию
            if not img_url:
                return None
                
            # Нормализация URL
            if img_url.startswith("//"):
                img_url = f"https:{img_url}"
            elif not img_url.startswith("http"):
                return None
                
            # Дополнительная фильтрация плохих URL
            if any(x in img_url.lower() for x in ["placeholder", "st.prntscr.com"]):
                return None
                
            if "removed.png" in img_url.lower():
                return None
                
            # Проверка, что URL принадлежит prnt.sc
            if "prntscr.com" not in img_url and "prnt.sc" not in img_url:
                return None
                
            return img_url
            
        except Exception as e:
            logger.error(f"Prnt.sc parsing error: {str(e)}")
            return None

class PasteNowSource(ImageSource):
    """Источник изображений Paste.pics"""
    async def generate_urls(self, batch_size: int) -> List[str]:
        return [f"https://paste.pics/{self.bot.generate_random_string(5)}" 
                for _ in range(batch_size)]
    
    async def extract_image_url(self, url: str) -> Optional[str]:
        try:
            session = await self.bot.get_session()
            headers = {
                "User-Agent": random.choice(self.bot.user_agents),
                "Referer": "https://paste.pics/",
            }
            
            async with session.get(url, headers=headers, 
                                 timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return None
                    
                text = await response.text()

            soup = BeautifulSoup(text, "html.parser")
            
            # Поиск URL изображения в тегах
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
                
            # Нормализация URL
            if img_url.startswith("//"):
                img_url = f"https:{img_url}"
            elif not img_url.startswith("http"):
                return None
                
            return img_url
            
        except Exception as e:
            logger.error(f"PasteNow parsing error: {str(e)}")
            return None

class FreeImageSource(ImageSource):
    """Источник изображений Freeimage"""
    async def generate_urls(self, batch_size: int) -> List[str]:
        return [f"https://iili.io/{self.bot.generate_random_string(7)}.jpg" 
                for _ in range(batch_size)]
    
    async def extract_image_url(self, url: str) -> Optional[str]:
        try:
            session = await self.bot.get_session()
            headers = {"User-Agent": random.choice(self.bot.user_agents)}
            
            async with session.head(url, headers=headers, 
                                 timeout=aiohttp.ClientTimeout(total=10)) as response:
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

class ImageBot:
    def __init__(self):
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        ]
        
        # Инициализация источников
        self.sources = {
            'imgur5': ImgurSource(self, 5),
            'imgur7': ImgurSource(self, 7),
            'prnt': PrntSource(self),
            'pastenow': PasteNowSource(self),
            'freeimage': FreeImageSource(self)
        }
        
        self.sessions = {}
        self.last_commands = {}
        self.media_groups = {}
        self.sent_image_ids = {}
        self.command_cooldowns = {}
        self.source_errors = {}
        self.last_status_update = {}  # Время последнего обновления статуса
        
        self.send_semaphore = asyncio.Semaphore(5)
        self.session = None
        self._session_initialized = False

    async def get_session(self):
        """Возвращает сессию aiohttp, создавая при необходимости"""
        if not self._session_initialized or (self.session and self.session.closed):
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            connector = aiohttp.TCPConnector(limit=50, force_close=True)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            self._session_initialized = True
        return self.session

    async def cleanup(self):
        """Очистка ресурсов при завершении"""
        if self.session and not self.session.closed:
            await self.session.close()
        for key in list(self.sessions.keys()):
            await self.stop_session(key, "shutdown")

    def get_key(self, update: Update) -> Tuple[int, int]:
        """Генерирует ключ пользователя (chat_id, user_id)"""
        return (update.effective_chat.id, update.effective_user.id)

    def extract_image_id(self, url: str) -> str:
        """Извлекает ID изображения из URL"""
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
        """Проверяет кулдаун между командами"""
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
        """Генерирует случайную строку указанной длины"""
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choices(chars, k=length))

    async def add_to_media_group(self, update: Update, key: Tuple[int, int], 
                               url: str, ext: str, count: int, found: int, source: str):
        """Добавляет изображение в медиагруппу для отправки"""
        try:
            image_id = self.extract_image_id(url)
            display_url = f"[{image_id}]({url})"
            caption = f"({found}/{count}) {display_url} [{source.upper()}]"
            
            # Проверка дубликатов
            if key not in self.sent_image_ids:
                self.sent_image_ids[key] = set()
            
            if image_id in self.sent_image_ids[key]:
                logger.info(f"Изображение {image_id} уже отправлено, пропускаем")
                return
            
            self.sent_image_ids[key].add(image_id)
            
            # Отдельная обработка GIF
            if ext == "gif":
                media_item = InputMediaAnimation(media=url, caption=caption, parse_mode="Markdown")
                await self.send_media(update, key, [media_item])
                return
            
            # Инициализация медиагруппы
            if key not in self.media_groups:
                self.media_groups[key] = {
                    "media": [],
                    "last_added": time.time(),
                    "timer_task": None
                }
            
            group = self.media_groups[key]
            
            # Проверка дубликатов в группе
            for media in group["media"]:
                media_id = self.extract_image_id(media.media)
                if media_id == image_id:
                    logger.info(f"Изображение {image_id} уже в группе, пропускаем")
                    return
            
            # Добавление в группу
            media_item = InputMediaPhoto(media=url, caption=caption, parse_mode="Markdown")
            group["media"].append(media_item)
            group["last_added"] = time.time()
            
            # Запуск таймера для отправки группы
            if group["timer_task"] is None or group["timer_task"].done():
                group["timer_task"] = asyncio.create_task(self.group_timer(update, key))
            
            # Отправка при достижении максимального размера группы
            if len(group["media"]) >= MAX_GROUP_SIZE:
                media_to_send = group["media"]
                group["media"] = []
                await self.send_media(update, key, media_to_send)
        except Exception as e:
            logger.error(f"Ошибка добавления в медиагруппу: {str(e)}")

    async def send_media(self, update: Update, key: Tuple[int, int], media_group: List):
        """Отправляет группу медиа-файлов"""
        if not media_group:
            return
            
        async with self.send_semaphore:
            try:
                # Разделение фото и анимаций
                photos = [m for m in media_group if not isinstance(m, InputMediaAnimation)]
                animations = [m for m in media_group if isinstance(m, InputMediaAnimation)]
                
                # Отправка фото
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
                        # Попытка отправить по одному
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

                # Отправка анимаций
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
        """Таймер для отправки неполной медиагруппы"""
        try:
            while True:
                await asyncio.sleep(GROUP_TIMEOUT)
                
                if key not in self.media_groups:
                    break
                    
                group = self.media_groups[key]
                
                if not group["media"]:
                    break
                
                current_time = time.time()
                
                # Отправка если прошло достаточно времени с последнего добавления
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
        """Показывает главное меню с кнопками"""
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
        """Обработчик команды /start"""
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

В некоторых источниках могут встречаться неприятные, шокирующие или NSFW-материалы. 
Используйте бота на свой страх и риск.
"""
        )

    async def stop(self, update: Update, context: CallbackContext, silent: bool = False):
        """Остановка активного поиска"""
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
        
        # Отправка оставшихся изображений в группе
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
        """Внутренняя остановка сессии поиска"""
        session = self.sessions.get(key)
        if not session:
            return
            
        session["stop"] = True
        session["stop_reason"] = reason
        self.cleanup_user_session(key)

    def cleanup_user_session(self, key: Tuple[int, int]):
        """Очистка данных сессии пользователя"""
        if key in self.sessions:
            session = self.sessions[key]
            # Отмена всех задач
            if "tasks" in session:
                for task in session["tasks"]:
                    if not task.done():
                        task.cancel()
            elif "task" in session and session["task"]:
                if not session["task"].done():
                    session["task"].cancel()
            del self.sessions[key]
        
        # Очистка медиагрупп
        if key in self.media_groups:
            del self.media_groups[key]
        
        # Очистка истории отправленных изображений
        if key in self.sent_image_ids:
            del self.sent_image_ids[key]
        
        # Очистка времени обновления статуса
        if key in self.last_status_update:
            del self.last_status_update[key]

    async def repeat_last_command(self, update: Update, context: CallbackContext):
        """Повтор последней команды пользователя"""
        key = self.get_key(update)
        last_command = self.last_commands.get(key)
        if not last_command:
            await update.message.reply_text("❗️ Нет предыдущей команды для повторения.")
            return
        
        # Проверка активного идентичного поиска
        if key in self.sessions:
            current_session = self.sessions[key]
            if (current_session["source_type"] == last_command["type"] and 
                current_session.get("length", 0) == last_command.get("length", 0) and 
                current_session["target_count"] == last_command["count"]):
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        # Остановка текущего поиска
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        # Выполнение последней команды
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
        """Обновление статусного сообщения с троттлингом"""
        # Проверка минимального интервала между обновлениями (2 секунды)
        current_time = time.time()
        if not force and current_time - self.last_status_update.get(key, 0) < 2:
            return
            
        session = self.sessions.get(key)
        if not session or not session.get("status_msg"):
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
            
            # Обновление только при изменении текста
            last_text = session.get("last_status_text", "")
            if force or text != last_text:
                try:
                    await session["status_msg"].edit_text(text)
                    session["last_status_text"] = text
                    self.last_status_update[key] = current_time  # Сохраняем время обновления
                except RetryAfter as e:
                    wait_time = e.retry_after
                    logger.warning(f"Flood control при обновлении статуса. Ждем {wait_time} сек.")
                    await asyncio.sleep(wait_time)
                    await session["status_msg"].edit_text(text)
                    session["last_status_text"] = text
                    self.last_status_update[key] = time.time()
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.error(f"Ошибка при обновлении статуса: {str(e)}")
                except Exception as e:
                    logger.error(f"Ошибка при обновлении статуса: {str(e)}")
        except Exception as e:
            logger.error(f"Ошибка при подготовке статуса: {str(e)}")

    def is_source_disabled(self, source: str) -> bool:
        """Проверяет, отключен ли источник из-за ошибок"""
        if source in self.source_errors:
            return (time.time() - self.source_errors[source]) < SOURCE_TIMEOUT
        return False

    async def handle_source_error(self, source: str, message: str):
        """Обрабатывает ошибку источника"""
        logger.error(f"Ошибка источника {source}: {message}")
        self.source_errors[source] = time.time()
        return source

    async def _generic_search(self, update: Update, key: Tuple[int, int], 
                            source_type: str, count: int, length: int = None):
        """Общая логика поиска для всех источников"""
        try:
            session = self.sessions[key]
            source = self.sources[source_type]
            last_update_time = time.time()  # Время последнего обновления статуса
            
            # Гарантированное начальное обновление
            await self.update_status_message(key, force=True)
            
            while not session.get("stop", False) and session["found"] < count:
                # Проверка доступности источника
                if self.is_source_disabled(source_type):
                    logger.info(f"Источник {source_type} временно отключен")
                    session["stop"] = True
                    session["stop_reason"] = "source_disabled"
                    break
                
                # Проверка интервала времени для обновления статуса
                current_time = time.time()
                if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                    await self.update_status_message(key)
                    last_update_time = current_time
                
                # Генерация и проверка URL
                try:
                    urls = await source.generate_urls(BATCH_SIZES[source_type])
                    for url in urls:
                        if session.get("stop", False) or session["found"] >= count:
                            break
                            
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        
                        # Обновление статуса каждые N проверок
                        if analyzed % UPDATE_ON_CHECKED == 0:
                            await self.update_status_message(key, force=True)
                        
                        # Извлечение реального URL (для источников с парсингом)
                        img_url = await source.extract_image_url(url)
                        if not img_url:
                            continue
                            
                        # Проверка валидности изображения
                        final_url, ext = await source.check_image(img_url)
                        if not final_url or not ext:
                            continue
                            
                        session["found"] += 1
                        found = session["found"]
                        
                        # Обновление статуса каждые N найденных изображений
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                        
                        # Добавление в медиагруппу
                        await self.add_to_media_group(
                            update, key, final_url, ext, count, found, source_type
                        )
                    
                    retries = 0
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Ошибка при поиске в {source_type}: {str(e)}")
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["stop"] = True
                        session["stop_reason"] = "error"
                        await self.handle_source_error(source_type, str(e))
                    await asyncio.sleep(0.5)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск {source_type} для {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске {source_type}: {str(e)}")
        finally:
            # Гарантированное обновление статуса перед завершением
            await self.update_status_message(key, force=True)
            
            # Отправка оставшихся изображений
            if key in self.media_groups and self.media_groups[key].get("media"):
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                source_name = get_source_name(session["source_type"], session.get("length"))
                
                # Формирование итогового сообщения
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
                        f"✅ Поиск {source_name} завершен\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                
                try:
                    await update.message.reply_text(text)
                except Exception as e:
                    logger.error(f"Ошибка при отправке финального сообщения: {str(e)}")
                
                self.cleanup_user_session(key)
                await self.show_main_menu(update)

    async def search_all_sources(self, update: Update, context: CallbackContext):
        """Поиск по всем источникам одновременно"""
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

        # Проверка активного идентичного поиска
        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "all" and current_session["target_count"] == count:
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        # Остановка текущего поиска
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        # Создание статусного сообщения
        status_msg = await update.message.reply_text(
            f"🔍 Поиск всех источников начат\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )
        
        # Инициализация сессии
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
        
        # Гарантированное начальное обновление
        await self.update_status_message(key, force=True)
        
        # Запуск задач для каждого источника
        asyncio.create_task(self._search_all_sources(update, key, count))

    async def _search_all_sources(self, update: Update, key: Tuple[int, int], count: int):
        """Асинхронный поиск по всем источникам"""
        async def search_source(source_type: str):
            nonlocal session
            source = self.sources[source_type]
            batch_size = BATCH_SIZES.get(source_type, 10)
            weight = SOURCE_WEIGHTS.get(source_type, 0.2)
            max_per_source = min(50, max(1, round(weight * count * 1.5)))
            last_update_time = time.time()  # Время последнего обновления статуса
            
            while not session.get("stop", False):
                if session["found"] >= count:
                    break
                if session["sources"][source_type]["found"] >= max_per_source:
                    break
                if self.is_source_disabled(source_type):
                    logger.info(f"Источник {source_type} временно отключен")
                    session["sources"][source_type]["active"] = False
                    break
                
                # Проверка интервала времени для обновления статуса
                current_time = time.time()
                if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                    await self.update_status_message(key)
                    last_update_time = current_time
                
                try:
                    # Генерация URL
                    urls = await source.generate_urls(batch_size)
                    
                    for url in urls:
                        if session.get("stop", False) or session["found"] >= count:
                            break
                            
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        
                        # Обновление статуса каждые N проверок
                        if analyzed % UPDATE_ON_CHECKED == 0:
                            await self.update_status_message(key, force=True)
                        
                        # Извлечение реального URL
                        img_url = await source.extract_image_url(url)
                        if not img_url:
                            continue
                            
                        # Проверка изображения
                        final_url, ext = await source.check_image(img_url)
                        if not final_url or not ext:
                            continue
                            
                        session["found"] += 1
                        session["sources"][source_type]["found"] += 1
                        found = session["found"]
                        
                        # Обновление статуса каждые N найденных изображений
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                        
                        # Добавление в медиагруппу
                        await self.add_to_media_group(
                            update, key, final_url, ext, count, found, source_type
                        )
                    
                    retries = 0
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
            
            # Запуск задач для каждого активного источника
            tasks = []
            for source_type in SOURCE_WEIGHTS.keys():
                if not self.is_source_disabled(source_type):
                    tasks.append(asyncio.create_task(search_source(source_type)))
            
            session["tasks"] = tasks
            await asyncio.gather(*tasks)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск всех источников для {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске всех источников: {str(e)}")
        finally:
            # Гарантированное обновление статуса перед завершением
            await self.update_status_message(key, force=True)
            
            # Отправка оставшихся изображений
            if key in self.media_groups and self.media_groups[key].get("media"):
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                
                # Формирование итогового сообщения
                if stop_reason == "source_disabled":
                    logger.info(f"Поиск пользователя {key} остановлен из-за недоступности источника. Время: {format_time(elapsed)}")
                    message = (
                        f"🔴 Поиск остановлен\n"
                        f"⚠️ Один или несколько источников временно недоступны\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                else:
                    logger.info(f"Поиск пользователя {key} завершен. Найдено: {found}/{target}. Время: {format_time(elapsed)}")
                    message = (
                        f"✅ Поиск завершен\n"
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

    # Обработчики для конкретных источников
    async def get_imgur_images(self, update: Update, context: CallbackContext):
        """Поиск изображений на Imgur"""
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

        # Определение типа источника
        source_type = f"imgur{length}"
        
        # Проверка активного идентичного поиска
        if key in self.sessions:
            current_session = self.sessions[key]
            if (current_session["source_type"] == "imgur" and 
                current_session.get("length", 0) == length and 
                current_session["target_count"] == count):
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        # Остановка текущего поиска
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        # Создание статусного сообщения
        status_msg = await update.message.reply_text(
            f"🔍 Поиск Imgur начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )
        
        # Инициализация сессии
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
        
        # Запуск поиска
        asyncio.create_task(self._generic_search(update, key, source_type, count))

    async def get_prnt_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        if await self.check_cooldown(update): return
        
        # Валидация аргументов
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getprnt <1-50>")
            return

        try: count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        # Проверка активного поиска
        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "prnt" and current_session["target_count"] == count:
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        # Остановка текущего поиска
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        # Инициализация сессии
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
        
        # Запуск поиска
        asyncio.create_task(self._generic_search(update, key, "prnt", count))

    async def get_pastenow_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        if await self.check_cooldown(update): return
        
        # Валидация аргументов
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getpastenow <1-50>")
            return

        try: count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        # Проверка активного поиска
        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "pastenow" and current_session["target_count"] == count:
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        # Остановка текущего поиска
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        # Инициализация сессии
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
        
        # Запуск поиска
        asyncio.create_task(self._generic_search(update, key, "pastenow", count))

    async def get_freeimage_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        if await self.check_cooldown(update): return
        
        # Валидация аргументов
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getfreeimage <1-50>")
            return

        try: count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        # Проверка активного поиска
        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "freeimage" and current_session["target_count"] == count:
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        # Остановка текущего поиска
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        # Инициализация сессии
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
        
        # Запуск поиска
        asyncio.create_task(self._generic_search(update, key, "freeimage", count))

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений (кнопок)"""
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
    """Обработчик сигналов завершения работы"""
    logger.info("Получен сигнал остановки")
    sys.exit(0)

def main():
    """Основная функция запуска бота"""
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

    # Регистрация обработчиков сигналов
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Создание и настройка приложения
    application = Application.builder().token(token).build()

    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("getimg", bot.get_imgur_images))
    application.add_handler(CommandHandler("getprnt", bot.get_prnt_images))
    application.add_handler(CommandHandler("getpastenow", bot.get_pastenow_images))
    application.add_handler(CommandHandler("getfreeimage", bot.get_freeimage_images))
    application.add_handler(CommandHandler("getall", bot.search_all_sources))
    application.add_handler(CommandHandler("stop", bot.stop))
    application.add_handler(CommandHandler("repeat", bot.repeat_last_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    # Запуск бота
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
        # Очистка ресурсов
        asyncio.get_event_loop().run_until_complete(bot.cleanup())
        sys.exit(0)

if __name__ == "__main__":
    main()
