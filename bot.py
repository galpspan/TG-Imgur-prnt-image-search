import logging
import random
import string
import time
import asyncio
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ContextTypes,
)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("image_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

class ImageBot:
    def __init__(self):
        self.valid_extensions = [".jpg", ".jpeg", ".png", ".gif"]
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        ]
        self.sessions = {}
        self.last_commands = {}  # Для хранения последних команд пользователей

    def format_time(self, seconds: int) -> str:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60
        if hours > 0:
            return f"{hours}ч {minutes}м {seconds}с"
        elif minutes > 0:
            return f"{minutes}м {seconds}с"
        else:
            return f"{seconds}с"

    def generate_random_string(self, length: int) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    def check_image(self, url: str) -> str | None:
        try:
            headers = {"User-Agent": random.choice(self.user_agents)}
            head_response = requests.head(
                url, headers=headers, timeout=5, allow_redirects=True
            )
            if head_response.status_code != 200:
                return None

            content_type = head_response.headers.get("content-type", "")
            if not any(
                ext in content_type for ext in ["image/jpeg", "image/png", "image/gif"]
            ):
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
            logger.error(f"Ошибка при проверке {url}: {str(e)}")
            return None

    async def check_image_async(self, url):
        loop = asyncio.get_event_loop()
        return url, await loop.run_in_executor(None, self.check_image, url)

    def extract_prnt_image_url(self, code: str) -> str | None:
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
                elif img_url.startswith("http"):
                    pass
                else:
                    return None

                # Проверяем, что это не placeholder изображение
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

    async def show_main_menu(self, update: Update):
        reply_keyboard = [["PRNT.SC", "IMGUR"], ["ПОВТОРИТЬ", "СТОП"]]
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

Доступные команды:
/getimg <5|7> <1-50> - поиск на Imgur
/getprnt <1-50> - поиск на prnt.sc (длина всегда 6)
/stop - остановить текущий поиск
/repeat - повторить последний поиск

Примеры:
/getimg 5 10
/getprnt 5

Или используйте кнопки ниже:
"""
        )

    async def stop(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        session = self.sessions.get(user_id)

        if session and session.get("task"):
            session["stop"] = True
            session["task"].cancel()

            elapsed = int(time.time() - session.get("start_time", time.time()))
            found = session.get("found", 0)
            analyzed = session.get("analyzed", 0)
            length = session.get("length", 0)
            count = session.get("count", 0)
            service = "prnt.sc" if length == 6 else "imgur"

            logger.info(
                f"{service} Поиск пользователя {user_id} был отменён. "
                f"Длина: {length}, количество: {count}, "
                f"найдено: {found}, проверено: {analyzed}, "
                f"время: {self.format_time(elapsed)}"
            )

            await update.message.reply_text(
                f"⛔️ Поиск остановлен.\n"
                f"Сервис: {service}\n"
                f"Длина: {length}\n"
                f"Найдено: {found}/{count}\n"
                f"Проверено: {analyzed}\n"
                f"Время: {self.format_time(elapsed)}"
            )

            del self.sessions[user_id]
        else:
            logger.info(
                f"Пользователь {user_id} попытался использовать /stop, но поиск не был начат."
            )
            await update.message.reply_text("❗️Нет активного поиска.")

    async def repeat_last_command(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        last_command = self.last_commands.get(user_id)
        
        if not last_command:
            await update.message.reply_text("❗️Нет предыдущей команды для повторения.")
            return
            
        if last_command["type"] == "imgur":
            context.args = [str(last_command["length"]), str(last_command["count"])]
            await self.get_imgur_images(update, context)
        elif last_command["type"] == "prnt":
            context.args = [str(last_command["count"])]
            await self.get_prnt_images(update, context)

    async def get_imgur_images(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id

        if user_id in self.sessions:
            prev_session = self.sessions[user_id]
            if prev_session.get("task"):
                prev_session["stop"] = True
                prev_session["task"].cancel()
                logger.info(
                    f"Предыдущий поиск пользователя {user_id} был отменён перед началом нового"
                )

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

        # Сохраняем команду для возможного повторения
        self.last_commands[user_id] = {
            "type": "imgur",
            "length": length,
            "count": count,
            "timestamp": time.time()
        }

        start_time = time.time()
        analyzed = 0
        found = 0

        logger.info(
            f"Imgur поиск пользователя {user_id} начат. Длина: {length}, количество: {count}"
        )

        status_msg = await update.message.reply_text(
            f"🔍 Поиск Imgur начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )

        async def update_status():
            nonlocal analyzed, found
            elapsed = int(time.time() - start_time)
            await status_msg.edit_text(
                f"🔍 Поиск Imgur\n"
                f"Длина: {length}\n"
                f"Цель: {count} изображений\n"
                f"Найдено: {found}/{count}\n"
                f"Проверено: {analyzed}\n"
                f"Время: {self.format_time(elapsed)}"
            )

        async def search_loop():
            nonlocal analyzed, found
            while found < count and not self.sessions[user_id]["stop"]:
                tasks = []
                for _ in range(10):
                    code = self.generate_random_string(length)
                    url = f"https://i.imgur.com/{code}.jpg"
                    tasks.append(self.check_image_async(url))

                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if self.sessions[user_id]["stop"]:
                        return

                    if isinstance(result, Exception):
                        logger.error(f"Ошибка при проверке изображения: {str(result)}")
                        continue

                    url, ext = result
                    analyzed += 1
                    self.sessions[user_id]["analyzed"] = analyzed

                    if ext:
                        found += 1
                        self.sessions[user_id]["found"] = found
                        caption = f"({found}/{count}) Imgur: [{url.split('/')[-1].split('.')[0]}]({url})"
                        try:
                            if ext == "gif":
                                await update.message.reply_animation(
                                    animation=url, caption=caption, parse_mode="Markdown"
                                )
                            else:
                                await update.message.reply_photo(
                                    photo=url, caption=caption, parse_mode="Markdown"
                                )
                            await update_status()
                            await asyncio.sleep(1)
                        except Exception as e:
                            logger.error(f"Ошибка при отправке изображения: {str(e)}")
                            found -= 1
                            self.sessions[user_id]["found"] = found

                    if analyzed % 10 == 0 or (ext and found > 0):
                        await update_status()

                await asyncio.sleep(0.1)

            if not self.sessions[user_id]["stop"]:
                elapsed = int(time.time() - start_time)
                logger.info(
                    f"Imgur поиск пользователя {user_id} завершён. "
                    f"Длина: {length}, количество: {count}, "
                    f"найдено: {found}, проверено: {analyzed}, "
                    f"время: {self.format_time(elapsed)}"
                )
                await update.message.reply_text(
                    f"✅ Поиск Imgur завершён\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено: {found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                del self.sessions[user_id]

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
        }

    async def get_prnt_images(self, update: Update, context: CallbackContext):
        user_id = update.effective_user.id

        if user_id in self.sessions:
            prev_session = self.sessions[user_id]
            if prev_session.get("task"):
                prev_session["stop"] = True
                prev_session["task"].cancel()
                logger.info(
                    f"Предыдущий поиск пользователя {user_id} был отменён перед началом нового"
                )

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

        # Сохраняем команду для возможного повторения
        self.last_commands[user_id] = {
            "type": "prnt",
            "length": 6,  # Фиксированная длина для prnt.sc
            "count": count,
            "timestamp": time.time()
        }

        length = 6  # Фиксированная длина для prnt.sc
        start_time = time.time()
        analyzed = 0
        found = 0

        logger.info(
            f"Prnt.sc поиск пользователя {user_id} начат. Длина: {length}, количество: {count}"
        )

        status_msg = await update.message.reply_text(
            f"🔍 Поиск prnt.sc начат\n"
            f"Длина: {length}\n"
            f"Цель: {count} изображений\n"
            f"Найдено: 0/{count}\n"
            f"Проверено: 0\n"
            f"Время: 0с"
        )

        async def update_status():
            nonlocal analyzed, found
            elapsed = int(time.time() - start_time)
            await status_msg.edit_text(
                f"🔍 Поиск prnt.sc\n"
                f"Длина: {length}\n"
                f"Цель: {count} изображений\n"
                f"Найдено: {found}/{count}\n"
                f"Проверено: {analyzed}\n"
                f"Время: {self.format_time(elapsed)}"
            )

        async def search_loop():
            nonlocal analyzed, found
            while found < count and not self.sessions[user_id]["stop"]:
                tasks = []
                for _ in range(5):  # Меньше параллельных запросов из-за парсинга страниц
                    code = self.generate_random_string(length).lower()
                    tasks.append(self.extract_prnt_image_url_async(code))

                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if self.sessions[user_id]["stop"]:
                        return

                    if isinstance(result, Exception):
                        logger.error(f"Ошибка при проверке prnt.sc: {str(result)}")
                        continue

                    img_url = result
                    analyzed += 1
                    self.sessions[user_id]["analyzed"] = analyzed

                    if img_url:
                        # Проверяем само изображение
                        ext = await self.check_image_async(img_url)
                        if ext[1]:
                            found += 1
                            self.sessions[user_id]["found"] = found
                            caption = f"({found}/{count}) prnt.sc: [{img_url.split('/')[-1].split('.')[0]}]({img_url})"
                            try:
                                if ext[1] == "gif":
                                    await update.message.reply_animation(
                                        animation=img_url,
                                        caption=caption,
                                        parse_mode="Markdown",
                                    )
                                else:
                                    await update.message.reply_photo(
                                        photo=img_url,
                                        caption=caption,
                                        parse_mode="Markdown",
                                    )
                                await update_status()
                                await asyncio.sleep(1)
                            except Exception as e:
                                logger.error(f"Ошибка при отправке изображения: {str(e)}")
                                found -= 1
                                self.sessions[user_id]["found"] = found

                    if analyzed % 5 == 0 or (img_url and found > 0):
                        await update_status()

                await asyncio.sleep(0.5)  # Большая задержка из-за парсинга

            if not self.sessions[user_id]["stop"]:
                elapsed = int(time.time() - start_time)
                logger.info(
                    f"Prnt.sc поиск пользователя {user_id} завершён. "
                    f"Длина: {length}, количество: {count}, "
                    f"найдено: {found}, проверено: {analyzed}, "
                    f"время: {self.format_time(elapsed)}"
                )
                await update.message.reply_text(
                    f"✅ Поиск prnt.sc завершён\n"
                    f"Длина: {length}\n"
                    f"Цель: {count} изображений\n"
                    f"Найдено: {found}/{count}\n"
                    f"Проверено: {analyzed}\n"
                    f"Время: {self.format_time(elapsed)}"
                )
                del self.sessions[user_id]

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
            context.user_data["mode"] = "prnt_sc"  # Сохраняем режим работы

        elif text == "IMGUR":
            reply_keyboard = [["5", "7"], ["НАЗАД"]]
            await update.message.reply_text(
                "IMGUR - Выберите интервал:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "imgur_interval"  # Режим выбора интервала

        # Обработка выбора интервала для IMGUR (5 или 7)
        elif text in ["5", "7"] and context.user_data.get("mode") == "imgur_interval":
            context.user_data["imgur_interval"] = text  # Сохраняем интервал
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
            context.user_data["mode"] = "imgur_numbers"  # Переключаем в режим выбора чисел

        # Обработка выбора чисел (для обоих режимов)
        elif text in ["1", "3", "5", "10", "15", "25", "50"]:
            if context.user_data.get("mode") == "imgur_numbers":
                # Для IMGUR
                interval = context.user_data["imgur_interval"]
                # Вызываем функцию поиска с параметрами из кнопок
                context.args = [interval, text]
                await self.get_imgur_images(update, context)
            else:
                # Для PRNT.SC
                # Вызываем функцию поиска с параметрами из кнопок
                context.args = [text]
                await self.get_prnt_images(update, context)

            await self.show_main_menu(update)  # Возвращаем в главное меню
            context.user_data.clear()  # Очищаем контекст

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
    
    # Чтение токена из файла
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

    # Обработчики команд
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("getimg", bot.get_imgur_images))
    application.add_handler(CommandHandler("getprnt", bot.get_prnt_images))
    application.add_handler(CommandHandler("stop", bot.stop))
    application.add_handler(CommandHandler("repeat", bot.repeat_last_command))

    # Обработчик текстовых сообщений (для кнопок)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    logger.info("Бот запущен и готов к работе")
    print("Бот запущен. Нажмите Ctrl+C для остановки")
    application.run_polling()

if __name__ == "__main__":
    main()
