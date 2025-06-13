# -*- coding: utf-8 -*-
import logging
import random
import string
import time
import asyncio
import requests
from typing import List, Dict, Set, Tuple, Optional, Union
from telegram import Update, ReplyKeyboardMarkup, InputMediaPhoto, Message, InputMediaAnimation
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import RetryAfter, BadRequest
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("image_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ======================= НАСТРОЙКИ ИСТОЧНИКОВ =======================
# Весовые коэффициенты для распределения поиска между источниками.
# Определяют, какая доля изображений будет искаться в каждом источнике.
# Сумма всех значений должна быть равна 1.0 (100%).
SOURCE_WEIGHTS = {
    'imgur5': 0.1,   # 10% изображений будет искаться на Imgur с 5-символьными кодами
    'imgur7': 0.2,   # 20% изображений будет искаться на Imgur с 7-символьными кодами
    'prnt': 0.2,     # 20% изображений будет искаться на prnt.sc
    'pastenow': 0.2, # 20% изображений будет искаться на paste.pics
    'freeimage': 0.3 # 30% изображений будет искаться на freeimage
}

# Размеры пакетов для проверки изображений в каждом источнике.
# Определяет, сколько URL/кодов генерируется и проверяется за один запрос.
BATCH_SIZES = {
    'imgur5': 5,     # Для Imgur 5-символьных - 5 URL за раз
    'imgur7': 10,    # Для Imgur 7-символьных - 10 URL за раз
    'prnt': 10,      # Для prnt.sc - 10 кодов за раз
    'pastenow': 10,  # Для paste.pics - 10 кодов за раз
    'freeimage': 10  # Для freeimage - 10 URL за раз
}

# Время (в секундах), на которое отключается источник после ошибки.
# Если источник возвращает ошибку, он будет временно отключен на это время.
SOURCE_TIMEOUT = 600  # 10 минут (600 секунд)

# Максимальный размер группы медиа при отправке в Telegram.
# Telegram позволяет отправлять до 10 медиа в одной группе.
MAX_GROUP_SIZE = 10

# Таймаут (в секундах) для отправки неполной группы медиа.
# Если в течение этого времени не найдено новых изображений для группы,
# неполная группа будет отправлена.
GROUP_TIMEOUT = 60  # 1 минута

# Интервал (в секундах) для обновления статусного сообщения.
# Как часто обновляется сообщение с прогрессом поиска.
STATUS_UPDATE_INTERVAL = 10  # 10 секунд

# Частота обновления статуса при нахождении изображений.
# Статус будет обновляться каждые N найденных изображений.
UPDATE_ON_FOUND = 5  # Обновлять статус каждые 5 найденных изображений

# Частота обновления статуса при проверке URL.
# Статус будет обновляться каждые N проверенных URL.
UPDATE_ON_CHECKED = 50  # Обновлять статус каждые 50 проверенных URL
# =====================================================================

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

class ImageBot:
    def __init__(self):
        self.valid_extensions = [".jpg", ".jpeg", ".png", ".gif"]
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        ]
        # Словарь активных сессий пользователей.
        # Ключ: кортеж (chat_id, user_id) - идентификатор чата и пользователя
        # Значение: словарь с данными текущей сессии поиска
        self.sessions: Dict[Tuple[int, int], Dict] = {}
        # Словарь последних выполненных команд пользователей.
        # Ключ: кортеж (chat_id, user_id)
        # Значение: словарь с параметрами последней команды (тип, количество и т.д.)
        self.last_commands: Dict[Tuple[int, int], Dict] = {}
        # Группы медиа для отправки пользователям.
        # Ключ: кортеж (chat_id, user_id)
        # Значение: список InputMediaPhoto/InputMediaAnimation для групповой отправки
        self.media_groups: Dict[Tuple[int, int], List] = {}
        # Уникальные идентификаторы уже отправленных изображений.
        # Ключ: кортеж (chat_id, user_id)
        # Значение: множество строковых ID изображений (для предотвращения дубликатов)
        self.sent_image_ids: Dict[Tuple[int, int], Set[str]] = {}
        # Блокировка источников при флуд-контроле.
        # Ключ: название источника ('imgur', 'prnt' и т.д.)
        # Значение: временная метка (timestamp) до которой источник заблокирован
        self.flood_lock: Dict[str, float] = {}
        # Временные метки последних команд пользователей.
        # Ключ: user_id
        # Значение: временная метка последней команды (для кулдауна)
        self.command_cooldowns: Dict[Tuple[int, int], float] = {}
        # Длительность кулдауна между командами (в секундах).
        # Пользователь не может отправлять команды чаще, чем раз в это время
        self.cooldown_duration: int = 180
        # Исполнитель для запуска синхронных задач в отдельных потоках.
        # Позволяет выполнять блокирующие операции (например, HTTP-запросы)
        # без блокировки основного event loop'а
        self.executor = ThreadPoolExecutor(max_workers=20)
        # Блокировка для синхронизации доступа к общим ресурсам
        # при работе с асинхронным кодом (защита от race conditions)
        self.lock = asyncio.Lock()
        # Временные ошибки источников.
        # Ключ: название источника ('imgur', 'prnt' и т.д.)
        # Значение: временная метка последней ошибки (для временного отключения источника)
        self.source_errors: Dict[str, float] = {}
        # Количество попыток повтора при неудачной отправке медиагруппы
        self.retry_attempts: int = 3

    def get_key(self, update: Update) -> Tuple[int, int]:
        return (update.effective_chat.id, update.effective_user.id)

    def extract_image_id(self, caption: str) -> Optional[str]:
        if not caption:
            return None
        try:
            parts = caption.split('[')
            if len(parts) > 1:
                return parts[1].split(']')[0]
        except Exception:
            pass
        return None

    async def check_cooldown(self, update: Update) -> bool:
        key = self.get_key(update)
        last_command_time = self.command_cooldowns.get(key, 0)
        current_time = time.time()
        remaining = (last_command_time + self.cooldown_duration) - current_time
        
        if remaining > 0:
            await update.message.reply_text(
                f"⚠️ Пожалуйста, подождите {format_time(int(remaining))} "
                "перед отправкой следующей команды."
            )
            return True
        return False

    def generate_random_string(self, length: int) -> str:
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    async def check_image(self, url: str, source: str = "any") -> Tuple[Optional[str], Optional[str]]:
        try:
            loop = asyncio.get_running_loop()
            headers = {"User-Agent": random.choice(self.user_agents)}
            
            head_response = await loop.run_in_executor(
                self.executor, 
                lambda: requests.head(url, headers=headers, timeout=10, allow_redirects=True)
            )
            
            if head_response.status_code != 200:
                return url, None
                
            content_type = head_response.headers.get("content-type", "").lower()
            if 'image' not in content_type and 'video' not in content_type:
                return url, None
                
            if 'imgur' in source and "removed" in head_response.url:
                return url, None
                
            if 'gif' in content_type:
                return url, 'gif'
            elif 'jpeg' in content_type or 'jpg' in content_type:
                return url, 'jpg'
            elif 'png' in content_type:
                return url, 'png'
                
            return url, None
        except Exception as e:
            logger.error(f"Ошибка при проверке изображения: {str(e)}")
            return url, None

    async def extract_prnt_image_url(self, code: str) -> Optional[str]:
        try:
            url = f"https://prnt.sc/{code}"
            headers = {
                "User-Agent": random.choice(self.user_agents),
                "Referer": "https://prnt.sc/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1"
            }
            
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                self.executor, 
                lambda: requests.get(url, headers=headers, timeout=10)
            )
            
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, "html.parser")
            no_image_div = soup.find('div', class_='no-image')
            if no_image_div:
                return None

            img_url = None
            img_tag = soup.find("img", {"class": "screenshot-image"})
            if img_tag and "src" in img_tag.attrs:
                img_url = img_tag["src"]
                if img_url.startswith("//"):
                    img_url = f"https:{img_url}"
                elif not img_url.startswith("http"):
                    img_url = None

            if not img_url:
                meta = soup.find("meta", {"property": "og:image"})
                if meta and meta.get("content"):
                    img_url = meta["content"]
                    if img_url.startswith("//"):
                        img_url = f"https:{img_url}"

            if not img_url:
                return None
                
            if "prntscr.com/placeholder" in img_url.lower() or "st.prntscr.com" in img_url.lower():
                return None

            return img_url
        except Exception as e:
            logger.error(f"Ошибка при парсинге prnt.sc: {str(e)}")
            self.source_errors["prnt"] = time.time()
            return None

    async def extract_pastenow_image_url(self, code: str) -> Optional[str]:
        try:
            url = f"https://ru.paste.pics/{code}"
            headers = {"User-Agent": random.choice(self.user_agents)}
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                self.executor, 
                lambda: requests.get(url, headers=headers, timeout=10)
            )
            
            if response.status_code == 404:
                return None
                
            soup = BeautifulSoup(response.text, "html.parser")
            content_div = soup.find('div', id='content')
            if content_div:
                img_tag = content_div.find('img', src=True)
                if img_tag:
                    img_url = img_tag['src']
                    if not img_url.startswith('http'):
                        img_url = 'https:' + img_url
                    if "placeholder" in img_url or "logo" in img_url:
                        return None
                    return img_url
                    
            meta = soup.find("meta", {"property": "og:image"})
            if meta and meta.get("content"):
                return meta["content"]
                
            return None
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return None
            logger.error(f"HTTP ошибка при парсинге paste.pics: {str(e)}")
            self.source_errors["pastenow"] = time.time()
            return None
        except Exception as e:
            logger.error(f"Ошибка при парсинге paste.pics: {str(e)}")
            self.source_errors["pastenow"] = time.time()
            return None

    async def send_media_group(self, update: Update, media_group: List, key: Tuple[int, int]) -> bool:
        if not media_group:
            return True
            
        attempts = 0
        while attempts < self.retry_attempts:
            try:
                await update.message.reply_media_group(media=media_group, parse_mode="Markdown")
                logger.info(f"Отправлена группа из {len(media_group)} медиа пользователю {key}")
                return True
            except RetryAfter as e:
                logger.warning(f"Rate limit exceeded для пользователя {key}. Waiting {e.retry_after} seconds")
                await asyncio.sleep(e.retry_after)
                attempts += 1
            except BadRequest as e:
                logger.error(f"Ошибка при отправке группы медиа: {str(e)}")
                for media in media_group:
                    try:
                        if isinstance(media, InputMediaAnimation):
                            await update.message.reply_animation(animation=media.media, caption=media.caption, parse_mode="Markdown")
                        else:
                            await update.message.reply_photo(photo=media.media, caption=media.caption, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Ошибка при отправке отдельного медиа: {str(e)}")
                return True
            except Exception as e:
                logger.error(f"Ошибка при отправке группы медиа: {str(e)}")
                attempts += 1
                await asyncio.sleep(1)
                
        logger.warning(f"Не удалось отправить группу пользователю {key} после {self.retry_attempts} попыток")
        return False

    async def send_single_media(self, update: Update, url: str, caption: str, is_gif: bool, key: Tuple[int, int]):
        try:
            if is_gif:
                await update.message.reply_animation(animation=url, caption=caption, parse_mode="Markdown")
            else:
                await update.message.reply_photo(photo=url, caption=caption, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка при отправке одиночного медиа: {str(e)}")

    async def add_to_media_group(self, update: Update, key: Tuple[int, int], url: str, ext: str, count: int, found: int, source: str):
        if source == "pastenow":
            image_id = url.split('/')[-1].split('?')[0].split('.')[0]
        else:
            image_id = url.split('/')[-1].split('.')[0]
        
        display_url = f"[{image_id}]({url})"
        caption = f"({found}/{count}) {display_url} [{source.upper()}]"
        
        if key not in self.sent_image_ids:
            self.sent_image_ids[key] = set()
        
        if image_id and image_id in self.sent_image_ids[key]:
            logger.info(f"Изображение {image_id} уже отправлено, пропускаем")
            return
        
        if ext == "gif":
            await self.send_single_media(update, url, caption, True, key)
            if image_id:
                self.sent_image_ids[key].add(image_id)
            return
        
        if key not in self.media_groups:
            self.media_groups[key] = []
            asyncio.create_task(self.group_timer(update, key))
        
        for media in self.media_groups[key]:
            media_id = self.extract_image_id(media.caption)
            if media_id == image_id:
                logger.info(f"Изображение {image_id} уже в группе, пропускаем")
                return
        
        media_item = InputMediaPhoto(media=url, caption=caption, parse_mode="Markdown")
        self.media_groups[key].append(media_item)
        if image_id:
            self.sent_image_ids[key].add(image_id)
        
        if len(self.media_groups[key]) >= MAX_GROUP_SIZE:
            media_to_send = self.media_groups[key]
            self.media_groups[key] = []
            await self.send_media_group(update, media_to_send, key)

    async def group_timer(self, update: Update, key: Tuple[int, int]):
        while True:
            await asyncio.sleep(5)
            
            if key not in self.media_groups or not self.media_groups[key]:
                break
                
            async with self.lock:
                session = self.sessions.get(key, {})
                last_found_time = session.get("last_found_time", 0)
            
            current_time = time.time()
            if (current_time - last_found_time) > GROUP_TIMEOUT:
                if key in self.media_groups and self.media_groups[key]:
                    media_to_send = self.media_groups[key]
                    self.media_groups[key] = []
                    await self.send_media_group(update, media_to_send, key)
                break

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
Привет! Я бот для поиска случайных изображений с различных сервисов.

Я создан @memory_not_found и могу искать изображения на:
- Imgur (5 и 7 символов)
- Prnt.sc
- Paste.pics
- FreeImage

Используйте кнопки ниже для начала поиска или команды:
/getimg <5|7> <1-50> - поиск на Imgur
/getprnt <1-50> - поиск на prnt.sc
/getpastenow <1-50> - поиск на paste.pics
/getfreeimage <1-50> - поиск на freeimage
/getall <1-50> - поиск на всех источниках
/stop - остановить текущий поиск
/repeat - повторить последний поиск
"""
        )

    async def stop(self, update: Update, context: CallbackContext, silent: bool = False):
        key = self.get_key(update)
        async with self.lock:
            if key not in self.sessions:
                if not silent:
                    await update.message.reply_text("❗️ Нет активного поиска.")
                return
    
            session = self.sessions[key]
            session["stop"] = True
    
            if key in self.media_groups and self.media_groups[key]:
                media_to_send = self.media_groups[key]
                self.media_groups[key] = []
                await self.send_media_group(update, media_to_send, key)
    
            if not silent:
                elapsed = int(time.time() - session["start_time"])
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
            
            self.cleanup_user_session(key)
            if not silent:
                await self.show_main_menu(update)

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
            
        async with self.lock:
            if key in self.sessions:
                current_session = self.sessions[key]
                if (current_session["source_type"] == last_command["type"] and 
                    current_session.get("length", 0) == last_command.get("length", 0) and 
                    current_session["target_count"] == last_command["count"]):
                    await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                    return
                else:
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
        async with self.lock:
            if key not in self.sessions:
                return
                
            session = self.sessions[key]
            current_time = time.time()
            session["last_update"] = current_time
            self.sessions[key] = session
            
            status_msg = session.get("status_msg")
            if not status_msg:
                return
                
            try:
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                
                source_name = {
                    "imgur": f"Imgur ({session.get('length', 5)})",
                    "prnt": "Prnt.sc",
                    "pastenow": "Paste.pics",
                    "freeimage": "Freeimage",
                    "all": "Все источники"
                }.get(session["source_type"], "Неизвестный источник")
                
                text = (
                    f"🔍 Поиск {source_name} в процессе\n"
                    f"Цель: {target} изображений\n"
                    f"Найдено: {found}/{target}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {format_time(int(current_time - session['start_time']))}"
                )
                
                await status_msg.edit_text(text)
            except Exception as e:
                logger.error(f"Ошибка при обновлении статуса для {key}: {str(e)}")

    def is_source_disabled(self, source: str) -> bool:
        if source in self.source_errors:
            return (time.time() - self.source_errors[source]) < SOURCE_TIMEOUT
        return False

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

        async with self.lock:
            if key in self.sessions:
                current_session = self.sessions[key]
                if current_session["source_type"] == "all" and current_session["target_count"] == count:
                    await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                    return
                else:
                    await self.stop(update, context, silent=True)
                    await asyncio.sleep(1)
            
            logger.info(f"Поиск по всем источникам начат для пользователя {key}. Цель: {count} изображений")
            
            self.last_commands[key] = {
                "type": "all",
                "count": count,
                "timestamp": time.time()
            }

            status_msg = await update.message.reply_text(
                f"🔍 Поиск всех источников начат\n"
                f"Цель: {count} изображений\n"
                f"Найдено: 0/{count}\n"
                f"Проверено: 0\n"
                f"Время: 0с"
            )
            
            self.sessions[key] = {
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
            
            self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        asyncio.create_task(self._search_all_sources(update, key, count))

    async def _search_all_sources(self, update: Update, key: Tuple[int, int], count: int):
        async def search_source(source_type: str, length: int = None):
            nonlocal session
            batch_size = BATCH_SIZES[source_type]
            weight = SOURCE_WEIGHTS[source_type]
            max_per_source = min(50, max(1, round(weight * count * 1.5)))
            last_update_time = time.time()
            
            while not session.get("stop", False):
                async with self.lock:
                    if session["found"] >= count:
                        break
                        
                if session["sources"][source_type]["found"] >= max_per_source:
                    break
                    
                if self.is_source_disabled(source_type):
                    logger.info(f"Источник {source_type} временно отключен")
                    async with self.lock:
                        session["sources"][source_type]["active"] = False
                        self.sessions[key] = session
                    break
                
                urls_to_check = []
                for _ in range(batch_size):
                    if source_type in ["imgur5", "imgur7"]:
                        code = self.generate_random_string(length)
                        urls_to_check.append(f"https://i.imgur.com/{code}.jpg")
                    elif source_type == "prnt":
                        code = self.generate_random_string(6)
                        urls_to_check.append(code)
                    elif source_type == "pastenow":
                        code = self.generate_random_string(5)
                        urls_to_check.append(code)
                    elif source_type == "freeimage":
                        code = self.generate_random_string(7)
                        urls_to_check.append(f"https://iili.io/{code}.jpg")
                
                try:
                    tasks = []
                    for url_or_code in urls_to_check:
                        if source_type in ["imgur5", "imgur7", "freeimage"]:
                            tasks.append(self.check_image(url_or_code, source_type))
                        elif source_type == "prnt":
                            tasks.append(self.extract_prnt_image_url(url_or_code))
                        elif source_type == "pastenow":
                            tasks.append(self.extract_pastenow_image_url(url_or_code))
                    
                    results = await asyncio.gather(*tasks)
                    
                    for result in results:
                        async with self.lock:
                            session["analyzed"] += 1
                            analyzed = session["analyzed"]
                            self.sessions[key] = session
                        
                        if analyzed % UPDATE_ON_CHECKED == 0:
                            await self.update_status_message(key, force=True)
                        
                        if source_type in ["imgur5", "imgur7", "freeimage"]:
                            url, ext = result
                            if not ext:
                                continue
                        else:
                            url = result
                            if not url:
                                continue
                            _, ext = await self.check_image(url, source_type)
                            if not ext:
                                continue
                        
                        async with self.lock:
                            session["found"] += 1
                            session["sources"][source_type]["found"] += 1
                            session["last_found_time"] = time.time()
                            found = session["found"]
                            self.sessions[key] = session
                            
                        await self.add_to_media_group(
                            update, key, url, ext, count, found, source_type
                        )
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                        
                        if session["found"] >= count:
                            break
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Ошибка при поиске в {source_type}: {str(e)}")
                    async with self.lock:
                        session["sources"][source_type]["active"] = False
                        self.sessions[key] = session
                    break
        
        try:
            async with self.lock:
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
            
            async with self.lock:
                session["tasks"] = tasks
                self.sessions[key] = session
            
            await asyncio.gather(*tasks)
            
        except asyncio.CancelledError:
            logger.info(f"Поиск всех источников для пользователя {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске всех источников: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key]:
                media_to_send = self.media_groups[key]
                self.media_groups[key] = []
                await self.send_media_group(update, media_to_send, key)
            
            async with self.lock:
                if key in self.sessions:
                    session = self.sessions[key]
                    elapsed = int(time.time() - session["start_time"])
                    target = session["target_count"]
                    found = session.get("found", 0)
                    analyzed = session.get("analyzed", 0)
                    
                    logger.info(
                        f"Поиск по всем источникам завершен для пользователя {key}. "
                        f"Найдено: {found}/{target}, Проверено: {analyzed}, Время: {elapsed}с"
                    )
                    
                    message = (
                        f"✅ Поиск всех источников завершен\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                    
                    await update.message.reply_text(message)
                    
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

        async with self.lock:
            if key in self.sessions:
                current_session = self.sessions[key]
                if current_session["source_type"] == "imgur" and current_session.get("length", 0) == length and current_session["target_count"] == count:
                    await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                    return
                else:
                    await self.stop(update, context, silent=True)
                    await asyncio.sleep(1)
                    
            self.last_commands[key] = {
                "type": "imgur",
                "length": length,
                "count": count,
                "timestamp": time.time()
            }

            status_msg = await update.message.reply_text(
                f"🔍 Поиск Imgur начат\n"
                f"Длина: {length}\n"
                f"Цель: {count} изображений\n"
                f"Найдено: 0/{count}\n"
                f"Проверено: 0\n"
                f"Время: 0с"
            )
            
            self.sessions[key] = {
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
            
            self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        asyncio.create_task(self._search_imgur(update, key, length, count))

    async def _search_imgur(self, update: Update, key: Tuple[int, int], length: int, count: int):
        try:
            async with self.lock:
                session = self.sessions[key]
                session["task"] = asyncio.current_task()
                self.sessions[key] = session
            
            last_update_time = time.time()
            
            while not session.get("stop", False) and session["found"] < count:
                code = self.generate_random_string(length)
                url = f"https://i.imgur.com/{code}.jpg"
                
                try:
                    _, ext = await self.check_image(url, "imgur")
                    
                    async with self.lock:
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        self.sessions[key] = session
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    if ext:
                        async with self.lock:
                            session["found"] += 1
                            session["last_found_time"] = time.time()
                            found = session["found"]
                            self.sessions[key] = session
                        
                        await self.add_to_media_group(
                            update, key, url, ext, count, found, "imgur"
                        )
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                        
                        if session["found"] >= count:
                            break
                    
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения Imgur: {str(e)}")
            
        except asyncio.CancelledError:
            logger.info(f"Поиск Imgur для пользователя {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске Imgur: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key]:
                media_to_send = self.media_groups[key]
                self.media_groups[key] = []
                await self.send_media_group(update, media_to_send, key)
            
            async with self.lock:
                if key in self.sessions:
                    session = self.sessions[key]
                    elapsed = int(time.time() - session["start_time"])
                    target = session["target_count"]
                    found = session.get("found", 0)
                    analyzed = session.get("analyzed", 0)
                    
                    message = (
                        f"✅ Поиск Imgur завершен\n"
                        f"Длина: {length}\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}"
                    )
                    
                    await update.message.reply_text(message)
                    
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

        async with self.lock:
            if key in self.sessions:
                current_session = self.sessions[key]
                if current_session["source_type"] == "prnt" and current_session["target_count"] == count:
                    await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                    return
                else:
                    await self.stop(update, context, silent=True)
                    await asyncio.sleep(1)
                    
            self.last_commands[key] = {
                "type": "prnt",
                "count": count,
                "timestamp": time.time()
            }

            status_msg = await update.message.reply_text(
                f"🔍 Поиск prnt.sc начат\n"
                f"Цель: {count} изображений\n"
                f"Найдено: 0/{count}\n"
                f"Проверено: 0\n"
                f"Время: 0с"
            )
            
            self.sessions[key] = {
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
            
            self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        asyncio.create_task(self._search_prnt(update, key, count))

    async def _search_prnt(self, update: Update, key: Tuple[int, int], count: int):
        try:
            async with self.lock:
                session = self.sessions[key]
                session["task"] = asyncio.current_task()
                self.sessions[key] = session
            
            last_update_time = time.time()
            
            while not session.get("stop", False) and session["found"] < count:
                if self.is_source_disabled("prnt"):
                    logger.info("Источник prnt.sc временно отключен")
                    async with self.lock:
                        session["stop"] = True
                        session["stop_reason"] = "source_disabled"
                    break
                
                code = self.generate_random_string(6)
                
                try:
                    url = await self.extract_prnt_image_url(code)
                    if not url:
                        continue
                        
                    _, ext = await self.check_image(url, "prnt")
                    
                    async with self.lock:
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        self.sessions[key] = session
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    if ext:
                        async with self.lock:
                            session["found"] += 1
                            session["last_found_time"] = time.time()
                            found = session["found"]
                            self.sessions[key] = session
                            
                        await self.add_to_media_group(
                            update, key, url, ext, count, found, "prnt"
                        )
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                    
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения prnt.sc: {str(e)}")
                    async with self.lock:
                        session["stop"] = True
                        session["stop_reason"] = "source_disabled"
                    break
            
        except asyncio.CancelledError:
            logger.info(f"Поиск prnt.sc для пользователя {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске prnt.sc: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key]:
                media_to_send = self.media_groups[key]
                self.media_groups[key] = []
                await self.send_media_group(update, media_to_send, key)
            
            async with self.lock:
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

        async with self.lock:
            if key in self.sessions:
                current_session = self.sessions[key]
                if current_session["source_type"] == "pastenow" and current_session["target_count"] == count:
                    await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                    return
                else:
                    await self.stop(update, context, silent=True)
                    await asyncio.sleep(1)
                    
            self.last_commands[key] = {
                "type": "pastenow",
                "count": count,
                "timestamp": time.time()
            }

            status_msg = await update.message.reply_text(
                f"🔍 Поиск paste.pics начат\n"
                f"Цель: {count} изображений\n"
                f"Найдено: 0/{count}\n"
                f"Проверено: 0\n"
                f"Время: 0с"
            )
            
            self.sessions[key] = {
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
            
            self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        asyncio.create_task(self._search_pastenow(update, key, count))

    async def _search_pastenow(self, update: Update, key: Tuple[int, int], count: int):
        try:
            async with self.lock:
                session = self.sessions[key]
                session["task"] = asyncio.current_task()
                self.sessions[key] = session
            
            last_update_time = time.time()
            
            while not session.get("stop", False) and session["found"] < count:
                if self.is_source_disabled("pastenow"):
                    logger.info("Источник paste.pics временно отключен")
                    async with self.lock:
                        session["stop"] = True
                        session["stop_reason"] = "source_disabled"
                    break
                
                code = self.generate_random_string(5)
                
                try:
                    url = await self.extract_pastenow_image_url(code)
                    if not url:
                        continue
                        
                    _, ext = await self.check_image(url, "pastenow")
                    
                    async with self.lock:
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        self.sessions[key] = session
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    if ext:
                        async with self.lock:
                            session["found"] += 1
                            session["last_found_time"] = time.time()
                            found = session["found"]
                            self.sessions[key] = session
                            
                        await self.add_to_media_group(
                            update, key, url, ext, count, found, "pastenow"
                        )
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                    
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения paste.pics: {str(e)}")
                    async with self.lock:
                        session["stop"] = True
                        session["stop_reason"] = "source_disabled"
                    break
            
        except asyncio.CancelledError:
            logger.info(f"Поиск paste.pics для пользователя {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске paste.pics: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key]:
                media_to_send = self.media_groups[key]
                self.media_groups[key] = []
                await self.send_media_group(update, media_to_send, key)
            
            async with self.lock:
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

        async with self.lock:
            if key in self.sessions:
                current_session = self.sessions[key]
                if current_session["source_type"] == "freeimage" and current_session["target_count"] == count:
                    await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                    return
                else:
                    await self.stop(update, context, silent=True)
                    await asyncio.sleep(1)
                    
            self.last_commands[key] = {
                "type": "freeimage",
                "count": count,
                "timestamp": time.time()
            }

            status_msg = await update.message.reply_text(
                f"🔍 Поиск freeimage начат\n"
                f"Цель: {count} изображений\n"
                f"Найдено: 0/{count}\n"
                f"Проверено: 0\n"
                f"Время: 0с"
            )
            
            self.sessions[key] = {
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
            
            self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        
        asyncio.create_task(self._search_freeimage(update, key, count))

    async def _search_freeimage(self, update: Update, key: Tuple[int, int], count: int):
        try:
            async with self.lock:
                session = self.sessions[key]
                session["task"] = asyncio.current_task()
                self.sessions[key] = session
            
            last_update_time = time.time()
            
            while not session.get("stop", False) and session["found"] < count:
                if self.is_source_disabled("freeimage"):
                    logger.info("Источник freeimage временно отключен")
                    async with self.lock:
                        session["stop"] = True
                        session["stop_reason"] = "source_disabled"
                    break
                
                code = self.generate_random_string(7)
                url = f"https://iili.io/{code}.jpg"
                
                try:
                    _, ext = await self.check_image(url, "freeimage")
                    
                    async with self.lock:
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        self.sessions[key] = session
                    
                    if analyzed % UPDATE_ON_CHECKED == 0:
                        await self.update_status_message(key, force=True)
                    
                    current_time = time.time()
                    if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                        await self.update_status_message(key)
                        last_update_time = current_time
                    
                    if ext:
                        async with self.lock:
                            session["found"] += 1
                            session["last_found_time"] = time.time()
                            found = session["found"]
                            self.sessions[key] = session
                            
                        await self.add_to_media_group(
                            update, key, url, ext, count, found, "freeimage"
                        )
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                    
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Ошибка при проверке изображения freeimage: {str(e)}")
                    async with self.lock:
                        session["stop"] = True
                        session["stop_reason"] = "source_disabled"
                    break
            
        except asyncio.CancelledError:
            logger.info(f"Поиск freeimage для пользователя {key} отменен")
        except Exception as e:
            logger.error(f"Ошибка при поиске freeimage: {str(e)}")
        finally:
            if key in self.media_groups and self.media_groups[key]:
                media_to_send = self.media_groups[key]
                self.media_groups[key] = []
                await self.send_media_group(update, media_to_send, key)
            
            async with self.lock:
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

def main():
    bot = ImageBot()
    try:
        with open("token.txt", "r") as f:
            token = f.read().strip()
    except FileNotFoundError:
        logger.error("Файл token.txt не найден. Создайте файл с токеном бота.")
        return
    except Exception as e:
        logger.error(f"Ошибка при чтении token.txt: {str(e)}")
        return

    if not token:
        logger.error("Токен бота не найден в файле token.txt")
        return

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

    logger.info("Бот запущен и готов к работе")
    print("Бот запущен. Нажмите Ctrl+C для остановки")
    application.run_polling()

if __name__ == "__main__":
    main()
