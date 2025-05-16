# -*- coding: utf-8 -*-
import logging
import random
import string
import time
import asyncio
import requests
from bs4 import BeautifulSoup
from typing import Union, List, Dict, Set
from telegram import Update, ReplyKeyboardMarkup, InputMediaPhoto, Message
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import RetryAfter
from io import BytesIO

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("image_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

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

def add_flood_control_reserve(retry_in: int) -> int:
    if retry_in >= 3600:
        return retry_in + 3600
    elif retry_in >= 600:
        return retry_in + 600
    elif retry_in >= 240:
        return retry_in + 240
    elif retry_in >= 60:
        return retry_in + 60
    else:
        return retry_in + 20

def format_time_full(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    out = []
    if h > 0:
        out.append(f"{h} ч")
    if m > 0:
        out.append(f"{m} мин")
    if s > 0 or not out:
        out.append(f"{s} сек")
    return " ".join(out)

class FloodControlException(Exception):
    def __init__(self, retry_in: int):
        self.retry_in = retry_in
        super().__init__(f"Flood control exceeded. Retry in {retry_in} seconds")

class ImageBot:
    def __init__(self):
        self.valid_extensions: List[str] = [".jpg", ".jpeg", ".png", ".gif"]
        self.user_agents: List[str] = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        ]
        self.sessions: Dict[int, Dict] = {}
        self.last_commands: Dict[int, Dict] = {}
        self.media_groups: Dict[int, List[InputMediaPhoto]] = {}
        self.sent_image_ids: Dict[int, Set[str]] = {}
        self.sent_single_messages: Dict[int, Dict[str, Message]] = {}
        self.max_group_size: int = 10
        self.group_timeout: int = 30
        self.search_timeout: int = 30
        self.retry_attempts: int = 3
        self.flood_lock: Dict[str, float] = {}

    def format_time(self, seconds: int) -> str:
        return format_time(seconds)

    def generate_random_string(self, length: int) -> str:
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    def check_image(self, url: str, source: str = "any") -> Union[str, None]:
        try:
            if source == "prnt" and not any(d in url for d in ["prnt.sc", "prntscr.com"]):
                return None
            if source == "imgur" and "imgur.com" not in url:
                return None
            if source == "pastenow" and "paste.pics" not in url:
                return None
            if source == "freeimage" and "iili.io" not in url:
                return None

            headers = {"User-Agent": random.choice(self.user_agents)}
            head_response = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
            if head_response.status_code != 200:
                return None

            content_type = head_response.headers.get("content-type", "")
            if not any(ext in content_type for ext in ["image/jpeg", "image/png", "image/gif"]):
                return None

            get_response = requests.get(url, headers=headers, stream=True, timeout=5)
            if get_response.status_code != 200:
                return None

            content_length = int(get_response.headers.get("content-length", 0))
            if content_length < 1024 or content_length > 20 * 1024 * 1024:
                return None

            first_chunk = next(get_response.iter_content(4))
            if first_chunk.startswith(b"\xFF\xD8\xFF"):
                return "jpg"
            elif first_chunk.startswith(b"\x89PNG"):
                return "png"
            elif first_chunk.startswith(b"GIF8"):
                return "gif"

            return None
        except Exception as e:
            msg = str(e)
            if "Flood control exceeded" in msg and "Retry in " in msg:
                try:
                    retry_in = int(msg.split("Retry in ")[1].split(" ")[0])
                    raise FloodControlException(retry_in)
                except Exception:
                    raise
            return None

    async def check_image_async(self, url, source="any"):
        loop = asyncio.get_event_loop()
        try:
            ext = await loop.run_in_executor(None, self.check_image, url, source)
            return url, ext
        except FloodControlException as fce:
            return fce
        except Exception as e:
            return e

    def extract_prnt_image_url(self, code: str) -> Union[str, None]:
        try:
            url = f"https://prnt.sc/{code}"
            headers = {"User-Agent": random.choice(self.user_agents)}
            response = requests.get(url, headers=headers, timeout=5)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            img_tag = soup.find("img", {"class": "screenshot-image"})
            if img_tag and "src" in img_tag.attrs:
                img_url = img_tag["src"]
                if img_url.startswith("//"):
                    img_url = f"https:{img_url}"
                elif not img_url.startswith("http"):
                    return None
                if "prntscr.com/placeholder" in img_url:
                    return None
                return img_url
            return None
        except Exception as e:
            logger.error(f"Ошибка при парсинге prnt.sc: {str(e)}")
            return None

    async def extract_prnt_image_url_async(self, code):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.extract_prnt_image_url, code)

    def extract_pastenow_image_url(self, code: str) -> Union[str, None]:
        try:
            url = f"https://ru.paste.pics/{code}"
            headers = {"User-Agent": random.choice(self.user_agents)}
            response = requests.get(url, headers=headers, timeout=8)
            if response.status_code == 404:
                return None
            response.raise_for_status()
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
            if hasattr(e.response, "status_code") and e.response.status_code == 404:
                return None
            logger.debug(f"404 для ru.paste.pics/{code}")
            return None
        except Exception as e:
            logger.error(f"Ошибка при парсинге ru.paste.pics: {str(e)}")
            return None

    async def extract_pastenow_image_url_async(self, code):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.extract_pastenow_image_url, code)
    
    def extract_image_id(self, caption: str) -> str:
        if not caption:
            return ""
        try:
            start = caption.find("[") + 1
            end = caption.find("]")
            return caption[start:end] if start > 0 and end > start else ""
        except Exception:
            return ""

    async def cleanup_duplicate_singles(self, user_id: int, group_image_ids: Set[str]) -> int:
        if user_id not in self.sent_single_messages:
            return 0
        deleted = 0
        single_ids = list(self.sent_single_messages[user_id].keys())
        for image_id in single_ids:
            if image_id in group_image_ids:
                msg = self.sent_single_messages[user_id][image_id]
                try:
                    await msg.delete()
                    del self.sent_single_messages[user_id][image_id]
                    deleted += 1
                    logger.info(f"Удалено дублирующееся одиночное сообщение {image_id} для пользователя {user_id}")
                except Exception as e:
                    logger.error(f"Ошибка при удалении сообщения {image_id}: {str(e)}")
        return deleted

    async def check_and_send_timeout(self, update: Update, user_id: int):
        while not self.sessions.get(user_id, {}).get("stop", True):
            await asyncio.sleep(1)
            current_time = time.time()
            if (user_id in self.media_groups and self.media_groups[user_id] and
                current_time - self.sessions[user_id].get("last_found_time", 0) > self.group_timeout):
                new_media = []
                for media in self.media_groups[user_id]:
                    image_id = self.extract_image_id(media.caption)
                    if not image_id or image_id not in self.sent_image_ids.get(user_id, set()):
                        new_media.append(media)
                if new_media:
                    logger.info(f"Таймаут достигнут, отправка {len(new_media)} новых изображений пользователю {user_id}")
                    await self.send_media_group(update, new_media, user_id)
                    self.sessions[user_id]["last_found_time"] = current_time
                self.media_groups[user_id] = []

    async def send_media_group(self, update: Update, media_group: List[InputMediaPhoto], user_id: int) -> bool:
        attempts = 0
        group_image_ids = set()
        for media in media_group:
            image_id = self.extract_image_id(media.caption)
            if image_id:
                group_image_ids.add(image_id)
        await self.cleanup_duplicate_singles(user_id, group_image_ids)
        new_ids = [img_id for img_id in group_image_ids if user_id not in self.sent_image_ids or img_id not in self.sent_image_ids[user_id]]
        while attempts < self.retry_attempts:
            try:
                await update.message.reply_media_group(media=media_group)
                logger.info(f"Пользователю {user_id} успешно отправлена группа из {len(media_group)} изображений")
                if user_id not in self.sent_image_ids:
                    self.sent_image_ids[user_id] = set()
                self.sent_image_ids[user_id].update(group_image_ids)
                session = self.sessions.get(user_id)
                if session:
                    if "actual_found" not in session:
                        session["actual_found"] = 0
                    session["actual_found"] += len(new_ids)
                    session["last_found_time"] = time.time()
                return True
            except RetryAfter as e:
                logger.warning(f"Rate limit exceeded для пользователя {user_id}. Waiting {e.retry_after} seconds")
                await asyncio.sleep(e.retry_after)
                attempts += 1
            except Exception as e:
                logger.error(f"Ошибка при отправке группы пользователю {user_id}: {str(e)}")
                attempts += 1
                await asyncio.sleep(1)
        logger.warning(f"Не удалось отправить группу пользователю {user_id} после {self.retry_attempts} попыток")
        return False

    async def send_single_media(self, update: Update, url: str, caption: str, is_gif: bool, user_id: int) -> bool:
        try:
            image_id = self.extract_image_id(caption)
            if not is_gif and image_id and user_id in self.sent_image_ids and image_id in self.sent_image_ids[user_id]:
                logger.info(f"Изображение {image_id} уже было отправлено, пропускаем")
                return True

            if is_gif:
                msg = await update.message.reply_animation(animation=url, caption=caption, parse_mode="Markdown")
            else:
                msg = await update.message.reply_photo(photo=url, caption=caption, parse_mode="Markdown")

            if not is_gif and image_id:
                if user_id not in self.sent_single_messages:
                    self.sent_single_messages[user_id] = {}
                self.sent_single_messages[user_id][image_id] = msg

            if user_id not in self.sent_image_ids:
                self.sent_image_ids[user_id] = set()
            if image_id:
                self.sent_image_ids[user_id].add(image_id)

            session = self.sessions.get(user_id)
            if session and image_id and (image_id not in session.get("_real_sent_ids", set())):
                if "actual_found" not in session:
                    session["actual_found"] = 0
                session["actual_found"] += 1
                if "_real_sent_ids" not in session:
                    session["_real_sent_ids"] = set()
                session["_real_sent_ids"].add(image_id)

            logger.info(f"Пользователю {user_id} отправлено {'GIF' if is_gif else 'одиночное изображение'} {image_id}")
            return True
        except RetryAfter as e:
            logger.warning(f"Rate limit exceeded для пользователя {user_id}. Waiting {e.retry_after} seconds")
            await asyncio.sleep(e.retry_after)
            return await self.send_single_media(update, url, caption, is_gif, user_id)
        except Exception as e:
            logger.error(f"Ошибка при отправке {'GIF' if is_gif else 'одиночного медиа'} пользователю {user_id}: {str(e)}")
            return False

    async def add_to_media_group(self, update: Update, user_id: int, url: str, ext: str, count: int, found: int, source: str):
        image_id = url.split('/')[-1].split('.')[0]
        display_url = f"[{image_id}]({url})"
        if source == "pastenow":
            image_id = url.split('/')[-1].split('?')[0].split('.')[0]
            display_url = f"[{image_id}]({url})"
        if image_id and (user_id not in self.sent_image_ids or image_id not in self.sent_image_ids[user_id]):
            caption = f"({found}/{count}) {display_url}"
        else:
            caption = f"(дубликат) {display_url}"
            return
        if ext == "gif":
            await self.send_single_media(update, url, caption, True, user_id)
            return
        if user_id not in self.media_groups:
            self.media_groups[user_id] = []
        media_item = InputMediaPhoto(media=url, caption=caption, parse_mode="Markdown")
        self.media_groups[user_id].append(media_item)
        if len(self.media_groups[user_id]) >= self.max_group_size:
            if not await self.send_media_group(update, self.media_groups[user_id], user_id):
                for media in self.media_groups[user_id]:
                    try:
                        await self.send_single_media(
                            update,
                            media.media,
                            media.caption,
                            False,
                            user_id
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке одиночного изображения: {str(e)}")
            self.media_groups[user_id] = []

    async def show_main_menu(self, update: Update):
        reply_keyboard = [
            ["PRNT.SC", "IMGUR"],
            ["PASTENOW", "FREEIMAGE"],
            ["ПОВТОРИТЬ", "СТОП"]
        ]
        await update.message.reply_text(
            "Выберите действие:",
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
Привет! Я ищу случайные изображения с разных сервисов.

Команды:
  /getimg <5|7> <1-50> — поиск на Imgur
  /getprnt <1-50> — поиск на prnt.sc (код всегда 6 символов)
  /getpastenow <1-50> — поиск на paste.pics (код всегда 5 символов)
  /getfreeimage <1-50> — поиск на freeimage (код всегда 7 символов)
  /stop — остановить текущий поиск
  /repeat — повторить последний поиск

Или используйте кнопки ниже:
"""
        )

    async def stop(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        if user_id not in self.sessions:
            await update.message.reply_text("❗️ Нет активного поиска.")
            return

        session = self.sessions[user_id]
        session["stop"] = True

        if user_id in self.media_groups and self.media_groups[user_id]:
            new_media = []
            for media in self.media_groups[user_id]:
                image_id = self.extract_image_id(media.caption)
                if not image_id or image_id not in self.sent_image_ids.get(user_id, set()):
                    new_media.append(media)
            if new_media:
                await self.send_media_group(update, new_media, user_id)

        task = session.get("task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Ошибка при завершении задачи поиска: {str(e)}")

        self.cleanup_user_session(user_id)

    def cleanup_user_session(self, user_id: int):
        if user_id in self.sessions:
            if self.sessions[user_id].get("task"):
                self.sessions[user_id]["task"].cancel()
            del self.sessions[user_id]
        if user_id in self.media_groups:
            del self.media_groups[user_id]
        if user_id in self.sent_single_messages:
            del self.sent_single_messages[user_id]
        if user_id in self.sent_image_ids:
            del self.sent_image_ids[user_id]

    async def repeat_last_command(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        last_command = self.last_commands.get(user_id)
        if not last_command:
            await update.message.reply_text("❗️Нет предыдущей команды для повторения.")
            return
        active_session = self.sessions.get(user_id)
        if active_session and not active_session.get("stop", True):
            await update.message.reply_text("❗️Идентичный поиск уже выполняется.")
            return
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

    async def handle_flood_control(self, update, retry_in, scope="imgur"):
        now = time.time()
        retry_with_reserve = add_flood_control_reserve(retry_in)
        self.flood_lock[scope] = now + retry_with_reserve
        formatted_time = format_time_full(retry_with_reserve)
        logger.warning(f"Flood control: ожидание {retry_with_reserve} секунд (до {time.ctime(self.flood_lock[scope])})")
        await update.message.reply_text(
            f"⚠️ Flood control! Поиск приостановлен примерно на {formatted_time}.\n"
            f"Пожалуйста, подождите, бот занят или превысил лимит запросов."
        )
        await asyncio.sleep(retry_with_reserve)

    def is_locked_by_flood(self, scope="imgur"):
        now = time.time()
        return (scope in self.flood_lock) and (self.flood_lock[scope] > now)

    async def get_imgur_images(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id
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

        if self.is_locked_by_flood("imgur"):
            wait_sec = int(self.flood_lock["imgur"] - time.time())
            await update.message.reply_text(
                f"🔒 Поиск временно заблокирован из-за flood control!\n"
                f"Осталось ждать: {format_time_full(wait_sec)}."
            )
            return

        last_command = self.last_commands.get(user_id)
        active_session = self.sessions.get(user_id)
        if (
            active_session and not active_session.get("stop", True)
            and last_command
            and last_command["type"] == "imgur"
            and last_command["length"] == length
            and last_command["count"] == count
        ):
            await update.message.reply_text("❗️Идентичный поиск уже выполняется.")
            return

        if active_session and active_session.get("task"):
            active_session["stop"] = True
            old_task = active_session["task"]
            try:
                old_task.cancel()
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Ошибка при завершении предыдущего поиска: {str(e)}")
            self.cleanup_user_session(user_id)

        self.last_commands[user_id] = {
            "type": "imgur",
            "length": length,
            "count": count,
            "timestamp": time.time()
        }

        start_time = time.time()
        analyzed = 0
        found = 0
        last_found_time = time.time()
        last_status_update = 0

        logger.info(f"Imgur поиск пользователя {user_id} начат. Длина: {length}, количество: {count}")

        status_msg = await update.message.reply_text(
            f"🔍 Поиск Imgur начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )

        async def update_status(force=False):
            nonlocal last_status_update
            current_time = time.time()
            if force or current_time - last_status_update >= 10:
                elapsed = int(current_time - start_time)
                await status_msg.edit_text(
                    f"🔍 Поиск Imgur\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено: {found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                last_status_update = current_time

        async def search_loop():
            nonlocal analyzed, found, last_found_time
            timeout_task = None
            try:
                session = self.sessions.get(user_id)
                if not session:
                    return
                session["actual_found"] = 0
                session["_real_sent_ids"] = set()
                session["last_found_time"] = time.time()
                timeout_task = asyncio.create_task(self.check_and_send_timeout(update, user_id))
                last_status_update = 0
                last_progress_analyzed = 0
                while session.get("actual_found", 0) < count and not session.get("stop", False):
                    if self.is_locked_by_flood("imgur"):
                        wait_sec = int(self.flood_lock["imgur"] - time.time())
                        await update.message.reply_text(
                            f"🔒 Поиск временно заблокирован из-за flood control!\n"
                            f"Осталось ждать: {format_time_full(wait_sec)}."
                        )
                        await asyncio.sleep(wait_sec)
                        continue
                    tasks = []
                    for _ in range(10):
                        code = self.generate_random_string(length)
                        url = f"https://i.imgur.com/{code}.jpg"
                        tasks.append(self.check_image_async(url, "imgur"))
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        session = self.sessions.get(user_id)
                        if not session or session.get("stop", False):
                            break
                        if session.get("stop", False) or session.get("actual_found", 0) >= count:
                            break
                        if isinstance(result, FloodControlException):
                            await self.handle_flood_control(update, result.retry_in, "imgur")
                            break
                        if isinstance(result, Exception):
                            msg = str(result)
                            if "Flood control exceeded" in msg and "Retry in " in msg:
                                try:
                                    retry_in = int(msg.split("Retry in ")[1].split(" ")[0])
                                    await self.handle_flood_control(update, retry_in, "imgur")
                                    break
                                except Exception:
                                    pass
                            logger.error(f"Ошибка в check_image_async: {result}")
                            continue
                        url, ext = result
                        analyzed += 1
                        session["analyzed"] = analyzed
                        if ext:
                            found += 1
                            last_found_time = time.time()
                            session["found"] = found
                            session["last_found_time"] = last_found_time
                            await self.add_to_media_group(
                                update, user_id, url, ext, count, found, "imgur"
                            )
                        if analyzed - last_progress_analyzed >= 10:
                            await update_status()
                            last_progress_analyzed = analyzed
                    await update_status()
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info(f"Поиск Imgur для пользователя {user_id} отменён")
            except Exception as e:
                logger.error(f"Ошибка в поиске Imgur для пользователя {user_id}: {str(e)}")
                await asyncio.sleep(10)
            finally:
                if timeout_task:
                    timeout_task.cancel()
                    try:
                        await timeout_task
                    except:
                        pass
                session = self.sessions.get(user_id, {})
                actual_found = session.get("actual_found", 0)
                if user_id in self.media_groups and self.media_groups[user_id]:
                    new_media = []
                    for media in self.media_groups[user_id]:
                        image_id = self.extract_image_id(media.caption)
                        if not image_id or image_id not in self.sent_image_ids.get(user_id, set()):
                            new_media.append(media)
                    if new_media:
                        logger.info(f"Финальная отправка {len(new_media)} изображений пользователю {user_id}")
                        await self.send_media_group(update, new_media, user_id)
                elapsed = int(time.time() - start_time)
                logger.info(
                    f"Imgur поиск пользователя {user_id} завершён. "
                    f"Длина: {length}, количество: {count}, "
                    f"найдено: {actual_found}, проверено: {analyzed}, "
                    f"время: {self.format_time(elapsed)}"
                )
                await update.message.reply_text(
                    f"✅ Поиск Imgur завершён\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено уникальных: {actual_found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                self.cleanup_user_session(user_id)

        task = asyncio.create_task(search_loop())
        self.sessions[user_id] = {
            "task": task,
            "stop": False,
            "start_time": start_time,
            "analyzed": analyzed,
            "found": found,
            "length": length,
            "count": count,
            "status_msg": status_msg,
            "actual_found": 0,
            "last_found_time": time.time()
        }

    async def get_prnt_images(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id

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

        if self.is_locked_by_flood("prnt"):
            wait_sec = int(self.flood_lock["prnt"] - time.time())
            await update.message.reply_text(
                f"🔒 Поиск временно заблокирован из-за flood control!\n"
                f"Осталось ждать: {format_time_full(wait_sec)}."
            )
            return

        last_command = self.last_commands.get(user_id)
        active_session = self.sessions.get(user_id)
        if (
            active_session and not active_session.get("stop", True)
            and last_command
            and last_command["type"] == "prnt"
            and last_command["count"] == count
        ):
            await update.message.reply_text("❗️Идентичный поиск уже выполняется.")
            return

        if active_session and active_session.get("task"):
            active_session["stop"] = True
            old_task = active_session["task"]
            try:
                old_task.cancel()
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Ошибка при завершении предыдущего поиска: {str(e)}")
            self.cleanup_user_session(user_id)

        self.last_commands[user_id] = {
            "type": "prnt",
            "length": 6,
            "count": count,
            "timestamp": time.time()
        }

        length = 6
        start_time = time.time()
        analyzed = 0
        found = 0
        last_found_time = time.time()
        last_status_update = 0

        logger.info(f"Prnt.sc поиск пользователя {user_id} начат. Количество: {count}")

        status_msg = await update.message.reply_text(
            f"🔍 Поиск prnt.sc начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )

        async def update_status(force=False):
            nonlocal last_status_update
            current_time = time.time()
            if force or current_time - last_status_update >= 10:
                elapsed = int(current_time - start_time)
                await status_msg.edit_text(
                    f"🔍 Поиск prnt.sc\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено: {found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                last_status_update = current_time

        async def search_loop():
            nonlocal analyzed, found, last_found_time
            timeout_task = None
            try:
                session = self.sessions.get(user_id)
                if not session:
                    return
                session["actual_found"] = 0
                session["_real_sent_ids"] = set()
                session["last_found_time"] = time.time()
                timeout_task = asyncio.create_task(self.check_and_send_timeout(update, user_id))
                last_status_update = 0
                last_progress_analyzed = 0
                while session.get("actual_found", 0) < count and not session.get("stop", False):
                    if self.is_locked_by_flood("prnt"):
                        wait_sec = int(self.flood_lock["prnt"] - time.time())
                        await update.message.reply_text(
                            f"🔒 Поиск временно заблокирован из-за flood control!\n"
                            f"Осталось ждать: {format_time_full(wait_sec)}."
                        )
                        await asyncio.sleep(wait_sec)
                        continue
                    tasks = []
                    for _ in range(5):
                        code = self.generate_random_string(length)
                        tasks.append(self.extract_prnt_image_url_async(code))
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        session = self.sessions.get(user_id)
                        if not session or session.get("stop", False):
                            break
                        if session.get("stop", False) or session.get("actual_found", 0) >= count:
                            break
                        if isinstance(result, FloodControlException):
                            await self.handle_flood_control(update, result.retry_in, "prnt")
                            break
                        if isinstance(result, Exception):
                            msg = str(result)
                            if "Flood control exceeded" in msg and "Retry in " in msg:
                                try:
                                    retry_in = int(msg.split("Retry in ")[1].split(" ")[0])
                                    await self.handle_flood_control(update, retry_in, "prnt")
                                    break
                                except Exception:
                                    pass
                            logger.error(f"Ошибка в extract_prnt_image_url_async: {result}")
                            continue
                        img_url = result
                        analyzed += 1
                        session["analyzed"] = analyzed
                        if img_url:
                            ext = await self.check_image_async(img_url, "prnt")
                            if isinstance(ext, FloodControlException):
                                await self.handle_flood_control(update, ext.retry_in, "prnt")
                                break
                            if ext and ext[1]:
                                found += 1
                                last_found_time = time.time()
                                session["found"] = found
                                session["last_found_time"] = last_found_time
                                await self.add_to_media_group(
                                    update, user_id, img_url, ext[1], count, found, "prnt"
                                )
                        if analyzed - last_progress_analyzed >= 5:
                            await update_status()
                            last_progress_analyzed = analyzed
                    await update_status()
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info(f"Поиск prnt.sc для пользователя {user_id} отменён")
            except Exception as e:
                logger.error(f"Ошибка в поиске prnt.sc для пользователя {user_id}: {str(e)}")
                await asyncio.sleep(10)
            finally:
                if timeout_task:
                    timeout_task.cancel()
                    try:
                        await timeout_task
                    except:
                        pass
                session = self.sessions.get(user_id, {})
                actual_found = session.get("actual_found", 0)
                if user_id in self.media_groups and self.media_groups[user_id]:
                    new_media = []
                    for media in self.media_groups[user_id]:
                        image_id = self.extract_image_id(media.caption)
                        if not image_id or image_id not in self.sent_image_ids.get(user_id, set()):
                            new_media.append(media)
                    if new_media:
                        logger.info(f"Финальная отправка {len(new_media)} изображений пользователю {user_id}")
                        await self.send_media_group(update, new_media, user_id)
                elapsed = int(time.time() - start_time)
                logger.info(
                    f"Prnt.sc поиск пользователя {user_id} завершён. "
                    f"Количество: {count}, найдено: {actual_found}, проверено: {analyzed}, время: {self.format_time(elapsed)}"
                )
                await update.message.reply_text(
                    f"✅ Поиск prnt.sc завершён\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено уникальных: {actual_found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                self.cleanup_user_session(user_id)

        task = asyncio.create_task(search_loop())
        self.sessions[user_id] = {
            "task": task,
            "stop": False,
            "start_time": start_time,
            "analyzed": analyzed,
            "found": found,
            "length": length,
            "count": count,
            "status_msg": status_msg,
            "actual_found": 0,
            "last_found_time": time.time()
        }

    async def get_pastenow_images(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id

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

        if self.is_locked_by_flood("pastenow"):
            wait_sec = int(self.flood_lock["pastenow"] - time.time())
            await update.message.reply_text(
                f"🔒 Поиск временно заблокирован из-за flood control!\n"
                f"Осталось ждать: {format_time_full(wait_sec)}."
            )
            return

        last_command = self.last_commands.get(user_id)
        active_session = self.sessions.get(user_id)
        if (
            active_session and not active_session.get("stop", True)
            and last_command
            and last_command["type"] == "pastenow"
            and last_command["count"] == count
        ):
            await update.message.reply_text("❗️Идентичный поиск уже выполняется.")
            return

        if active_session and active_session.get("task"):
            active_session["stop"] = True
            old_task = active_session["task"]
            try:
                old_task.cancel()
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Ошибка при завершении предыдущего поиска: {str(e)}")
            self.cleanup_user_session(user_id)

        self.last_commands[user_id] = {
            "type": "pastenow",
            "length": 5,
            "count": count,
            "timestamp": time.time()
        }

        length = 5
        start_time = time.time()
        analyzed = 0
        found = 0
        last_found_time = time.time()
        last_status_update = 0

        logger.info(f"Pastenow.ru поиск пользователя {user_id} начат. Количество: {count}")

        status_msg = await update.message.reply_text(
            f"🔍 Поиск pastenow.ru начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )

        async def update_status(force=False):
            nonlocal last_status_update
            current_time = time.time()
            if force or current_time - last_status_update >= 10:
                elapsed = int(current_time - start_time)
                await status_msg.edit_text(
                    f"🔍 Поиск pastenow.ru\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено: {found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                last_status_update = current_time

        async def search_loop():
            nonlocal analyzed, found, last_found_time
            timeout_task = None
            try:
                session = self.sessions.get(user_id)
                if not session:
                    return
                session["actual_found"] = 0
                session["_real_sent_ids"] = set()
                session["last_found_time"] = time.time()
                timeout_task = asyncio.create_task(self.check_and_send_timeout(update, user_id))
                last_status_update = 0
                last_progress_analyzed = 0
                while session.get("actual_found", 0) < count and not session.get("stop", False):
                    if self.is_locked_by_flood("pastenow"):
                        wait_sec = int(self.flood_lock["pastenow"] - time.time())
                        await update.message.reply_text(
                            f"🔒 Поиск временно заблокирован из-за flood control!\n"
                            f"Осталось ждать: {format_time_full(wait_sec)}."
                        )
                        await asyncio.sleep(wait_sec)
                        continue
                    tasks = []
                    for _ in range(5):
                        code = self.generate_random_string(length)
                        tasks.append(self.extract_pastenow_image_url_async(code))
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        session = self.sessions.get(user_id)
                        if not session or session.get("stop", False):
                            break
                        if session.get("stop", False) or session.get("actual_found", 0) >= count:
                            break
                        if isinstance(result, FloodControlException):
                            await self.handle_flood_control(update, result.retry_in, "pastenow")
                            break
                        if isinstance(result, Exception):
                            msg = str(result)
                            if "Flood control exceeded" in msg and "Retry in " in msg:
                                try:
                                    retry_in = int(msg.split("Retry in ")[1].split(" ")[0])
                                    await self.handle_flood_control(update, retry_in, "pastenow")
                                    break
                                except Exception:
                                    pass
                            logger.error(f"Ошибка в extract_pastenow_image_url_async: {result}")
                            continue
                        img_url = result
                        analyzed += 1
                        session["analyzed"] = analyzed
                        if img_url:
                            ext = await self.check_image_async(img_url, "pastenow")
                            if isinstance(ext, FloodControlException):
                                await self.handle_flood_control(update, ext.retry_in, "pastenow")
                                break
                            if ext and ext[1]:
                                found += 1
                                last_found_time = time.time()
                                session["found"] = found
                                session["last_found_time"] = last_found_time
                                await self.add_to_media_group(
                                    update, user_id, img_url, ext[1], count, found, "pastenow"
                                )
                        if analyzed - last_progress_analyzed >= 5:
                            await update_status()
                            last_progress_analyzed = analyzed
                    await update_status()
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info(f"Поиск pastenow.ru для пользователя {user_id} отменён")
            except Exception as e:
                logger.error(f"Ошибка в поиске pastenow.ru для пользователя {user_id}: {str(e)}")
                await asyncio.sleep(10)
            finally:
                if timeout_task:
                    timeout_task.cancel()
                    try:
                        await timeout_task
                    except:
                        pass
                session = self.sessions.get(user_id, {})
                actual_found = session.get("actual_found", 0)
                if user_id in self.media_groups and self.media_groups[user_id]:
                    new_media = []
                    for media in self.media_groups[user_id]:
                        image_id = self.extract_image_id(media.caption)
                        if not image_id or image_id not in self.sent_image_ids.get(user_id, set()):
                            new_media.append(media)
                    if new_media:
                        logger.info(f"Финальная отправка {len(new_media)} изображений пользователю {user_id}")
                        await self.send_media_group(update, new_media, user_id)
                elapsed = int(time.time() - start_time)
                logger.info(
                    f"pastenow.ru поиск пользователя {user_id} завершён. "
                    f"Количество: {count}, найдено: {actual_found}, проверено: {analyzed}, время: {self.format_time(elapsed)}"
                )
                await update.message.reply_text(
                    f"✅ Поиск pastenow.ru завершён\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено уникальных: {actual_found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                self.cleanup_user_session(user_id)

        task = asyncio.create_task(search_loop())
        self.sessions[user_id] = {
            "task": task,
            "stop": False,
            "start_time": start_time,
            "analyzed": analyzed,
            "found": found,
            "length": length,
            "count": count,
            "status_msg": status_msg,
            "actual_found": 0,
            "last_found_time": time.time()
        }

    async def get_freeimage_images(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id
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

        if self.is_locked_by_flood("freeimage"):
            wait_sec = int(self.flood_lock["freeimage"] - time.time())
            await update.message.reply_text(
                f"🔒 Поиск временно заблокирован из-за flood control!\n"
                f"Осталось ждать: {format_time_full(wait_sec)}."
            )
            return

        last_command = self.last_commands.get(user_id)
        active_session = self.sessions.get(user_id)
        if (
            active_session and not active_session.get("stop", True)
            and last_command
            and last_command["type"] == "freeimage"
            and last_command["count"] == count
        ):
            await update.message.reply_text("❗️Идентичный поиск уже выполняется.")
            return

        if active_session and active_session.get("task"):
            active_session["stop"] = True
            old_task = active_session["task"]
            try:
                old_task.cancel()
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Ошибка при завершении предыдущего поиска: {str(e)}")
            self.cleanup_user_session(user_id)

        self.last_commands[user_id] = {
            "type": "freeimage",
            "length": 7,
            "count": count,
            "timestamp": time.time()
        }

        length = 7
        start_time = time.time()
        analyzed = 0
        found = 0
        last_found_time = time.time()
        last_status_update = 0

        logger.info(f"Freeimage поиск пользователя {user_id} начат. Количество: {count}")

        status_msg = await update.message.reply_text(
            f"🔍 Поиск freeimage начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )

        async def update_status(force=False):
            nonlocal last_status_update
            current_time = time.time()
            if force or current_time - last_status_update >= 10:
                elapsed = int(current_time - start_time)
                await status_msg.edit_text(
                    f"🔍 Поиск freeimage\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено: {found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                last_status_update = current_time

        async def search_loop():
            nonlocal analyzed, found, last_found_time
            timeout_task = None
            try:
                session = self.sessions.get(user_id)
                if not session:
                    return
                session["actual_found"] = 0
                session["_real_sent_ids"] = set()
                session["last_found_time"] = time.time()
                timeout_task = asyncio.create_task(self.check_and_send_timeout(update, user_id))
                last_status_update = 0
                last_progress_analyzed = 0
                while session.get("actual_found", 0) < count and not session.get("stop", False):
                    if self.is_locked_by_flood("freeimage"):
                        wait_sec = int(self.flood_lock["freeimage"] - time.time())
                        await update.message.reply_text(
                            f"🔒 Поиск временно заблокирован из-за flood control!\n"
                            f"Осталось ждать: {format_time_full(wait_sec)}."
                        )
                        await asyncio.sleep(wait_sec)
                        continue
                    tasks = []
                    for _ in range(5):
                        code = self.generate_random_string(length)
                        url = f"https://iili.io/{code}.jpg"
                        tasks.append(self.check_image_async(url, "freeimage"))
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        session = self.sessions.get(user_id)
                        if not session or session.get("stop", False):
                            break
                        if session.get("stop", False) or session.get("actual_found", 0) >= count:
                            break
                        if isinstance(result, FloodControlException):
                            await self.handle_flood_control(update, result.retry_in, "freeimage")
                            break
                        if isinstance(result, Exception):
                            msg = str(result)
                            if "Flood control exceeded" in msg and "Retry in " in msg:
                                try:
                                    retry_in = int(msg.split("Retry in ")[1].split(" ")[0])
                                    await self.handle_flood_control(update, retry_in, "freeimage")
                                    break
                                except Exception:
                                    pass
                            logger.error(f"Ошибка в check_image_async (freeimage): {result}")
                            continue
                        url, ext = result
                        analyzed += 1
                        session["analyzed"] = analyzed
                        if ext:
                            found += 1
                            last_found_time = time.time()
                            session["found"] = found
                            session["last_found_time"] = last_found_time
                            await self.add_to_media_group(
                                update, user_id, url, ext, count, found, "freeimage"
                            )
                        if analyzed - last_progress_analyzed >= 5:
                            await update_status()
                            last_progress_analyzed = analyzed
                    await update_status()
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info(f"Поиск freeimage для пользователя {user_id} отменён")
            except Exception as e:
                logger.error(f"Ошибка в поиске freeimage для пользователя {user_id}: {str(e)}")
                await asyncio.sleep(10)
            finally:
                if timeout_task:
                    timeout_task.cancel()
                    try:
                        await timeout_task
                    except:
                        pass
                session = self.sessions.get(user_id, {})
                actual_found = session.get("actual_found", 0)
                if user_id in self.media_groups and self.media_groups[user_id]:
                    new_media = []
                    for media in self.media_groups[user_id]:
                        image_id = self.extract_image_id(media.caption)
                        if not image_id or image_id not in self.sent_image_ids.get(user_id, set()):
                            new_media.append(media)
                    if new_media:
                        logger.info(f"Финальная отправка {len(new_media)} изображений пользователю {user_id}")
                        await self.send_media_group(update, new_media, user_id)
                elapsed = int(time.time() - start_time)
                logger.info(
                    f"Freeimage поиск пользователя {user_id} завершён. "
                    f"Количество: {count}, найдено: {actual_found}, проверено: {analyzed}, время: {self.format_time(elapsed)}"
                )
                await update.message.reply_text(
                    f"✅ Поиск freeimage завершён\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено уникальных: {actual_found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                self.cleanup_user_session(user_id)

        task = asyncio.create_task(search_loop())
        self.sessions[user_id] = {
            "task": task,
            "stop": False,
            "start_time": start_time,
            "analyzed": analyzed,
            "found": found,
            "length": length,
            "count": count,
            "status_msg": status_msg,
            "actual_found": 0,
            "last_found_time": time.time()
        }

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text

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
                "IMGUR - Выберите интервал:",
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

        elif text in ["5", "7"] and context.user_data.get("mode") == "imgur_interval":
            context.user_data["imgur_interval"] = text
            reply_keyboard = [
                ["1", "3", "5"],
                ["10", "15", "25"],
                ["50", "НАЗАД"],
            ]
            await update.message.reply_text(
                f"IMGUR - Выбран интервал {text}. Теперь выберите количество:",
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
            else:
                context.args = [text]
                await self.get_prnt_images(update, context)

            await self.show_main_menu(update)
            context.user_data.clear()

        elif text == "НАЗАД":
            await self.show_main_menu(update)
            context.user_data.clear()

        elif text == "СТОП":
            await self.stop(update, context)
            await self.show_main_menu(update)

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
    application.add_handler(CommandHandler("stop", bot.stop))
    application.add_handler(CommandHandler("repeat", bot.repeat_last_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    logger.info("Бот запущен и готов к работе")
    print("Бот запущен. Нажмите Ctrl+C для остановки")
    application.run_polling()

if __name__ == "__main__":
    main()
