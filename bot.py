import os
import logging
import random
import string
import time
import asyncio
import aiohttp
from typing import List, Dict, Set, Tuple, Optional
from telegram import Update, ReplyKeyboardMarkup, InputMediaPhoto, InputMediaAnimation, InputMediaVideo, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import RetryAfter, BadRequest, TelegramError
from bs4 import BeautifulSoup
import sys
import imghdr
from io import BytesIO
from PIL import Image
import mimetypes

# ======================= НАСТРОЙКИ ЛОГИРОВАНИЯ =======================
# Фильтр для добавления user_info во все записи лога
class UserInfoFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'user_info'):
            record.user_info = "System"
        return True

# Настройка корневого логгера
root_logger = logging.getLogger()
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

user_info_filter = UserInfoFilter()

file_handler = logging.FileHandler("image_bot.log", encoding='utf-8')
file_handler.addFilter(user_info_filter)

stream_handler = logging.StreamHandler()
stream_handler.addFilter(user_info_filter)

formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s [User: %(user_info)s]"
)
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

root_logger.addHandler(file_handler)
root_logger.addHandler(stream_handler)
root_logger.setLevel(logging.INFO)

# Установим уровень для библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
# ======================================================================

# ======================= КОНФИГУРАЦИЯ БОТА ===========================
SOURCE_WEIGHTS = {
    'imgur5': 0.1,     # 10% - Imgur с 5-символьными кодами
    'imgur7': 0.2,     # 20% - Imgur с 7-символьными кодами 
    'prnt': 0.1,       # 20% - Prnt.sc
    'pastenow': 0.2,   # 20% - Paste.pics
    'freeimage': 0.2,  # 20% - Freeimage
    'kappa': 0.2       # 20% - Kappa.lol
}

BATCH_SIZES = {
    'imgur5': 5,      # 5 URL за раз для Imgur5
    'imgur7': 10,     # 10 URL за раз для Imgur7
    'prnt': 10,       # 10 кодов за раз для Prnt.sc
    'pastenow': 10,   # 10 кодов за раз для Paste.pics
    'freeimage': 10,  # 10 URL за раз для Freeimage
    'kappa': 10       # 10 URL за раз для Kappa.lol
}

# Время блокировки источника после ошибки (в секундах)
SOURCE_TIMEOUT = 600  # 10 минут
DNS_ERROR_TIMEOUT = 1800  # 30 минут для DNS ошибок

# Настройки группировки медиа при отправке
MAX_GROUP_SIZE = 10    # Макс. количество изображений в одном сообщении
GROUP_TIMEOUT = 60     # 1 минута - ждем наполнения группы перед отправкой

# Настройки обновления статуса
STATUS_UPDATE_INTERVAL = 10  # Обновлять статус каждые 10 секунд
UPDATE_ON_FOUND = 5          # Обновлять статус каждые 5 найденных изображений
UPDATE_ON_CHECKED = 50       # Обновлять статус каждые 50 проверенных URL

# Таймауты и ограничения
MEDIA_SEND_TIMEOUT = 60           # 60 сек на отправку медиа в Telegram
MAX_CONCURRENT_TASKS = 30         # Макс. одновременных задач проверки URL
MAX_RETRIES = 3                   # Макс. попыток при ошибках
COOLDOWN_DURATION = 5             # 5 секунд кулдауна между командами
REQUEST_TIMEOUT = 15              # 15 сек таймаут HTTP-запросов
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49 МБ - максимальный размер для Telegram
LARGE_FILE_IMAGE = "file50mb.png" # Путь к картинке-заглушке
MIN_WIDTH = 30                    # Минимальная ширина изображения
MIN_HEIGHT = 30                   # Минимальная высота изображения

# Типы ошибок при отправке фото
PHOTO_SEND_ERRORS = [
    "webpage_curl_failed",
    "Wrong type of the web page content",
    "Failed to get http url content",
    "Photo_invalid_dimensions"
]

# Сигнатуры файлов для определения типа
FILE_SIGNATURES = {
    b'\xFF\xD8\xFF': 'jpg',
    b'\x89PNG\r\n\x1a\n': 'png',
    b'GIF87a': 'gif',
    b'GIF89a': 'gif',
    b'BM': 'bmp',
    b'RIFF....WEBPVP8': 'webp',
    b'PK\x03\x04': 'zip',
    b'Rar!\x1a\x07': 'rar',
    b'\x1F\x8B\x08': 'gz',
    b'7z\xBC\xAF\x27\x1C': '7z',
    b'\x25\x50\x44\x46': 'pdf',
    b'\x50\x4B\x03\x04': 'docx',
    b'\xD0\xCF\x11\xE0': 'doc',  # MS Office
    b'\x49\x44\x33': 'mp3',      # ID3 MP3
    b'\xFF\xFB': 'mp3',          # MPEG layer 3
    b'\xFF\xF3': 'mp3',
    b'\xFF\xF2': 'mp3',
    b'fLaC': 'flac',
    b'OggS': 'ogg',
    b'\x1A\x45\xDF\xA3': 'webm', # Matroska/WebM
    b'\x52\x49\x46\x46....\x57\x45\x42\x50': 'webp',
    b'\x52\x49\x46\x46....\x41\x56\x49\x20': 'avi',
    b'\x00\x00\x00 ftyp': 'mp4',
    b'\x00\x00\x00\x18ftyp': 'mp4',
    b'\x00\x00\x00\x20ftyp': 'mp4',
}

# Кастомные исключения
class IncompleteDownloadError(Exception):
    """Исключение для неполных загрузок"""
    pass
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
        "kappa": "Kappa.lol",
        "all": "Все источники"
    }
    return names.get(source_type, source_type)

def user_info(update: Update) -> str:
    user = update.effective_user
    if user and user.username:
        return f"@{user.username} (ID: {user.id})"
    return f"ID: {user.id}" if user else "Unknown user"

class ImageSource:
    def __init__(self, bot):
        self.bot = bot
    
    async def generate_urls(self, batch_size: int) -> List[str]:
        raise NotImplementedError()
    
    async def extract_image_url(self, url: str) -> Optional[str]:
        return url
    
    async def get_actual_extension(self, data: bytes, url: str) -> str:
        """Определяет реальное расширение файла по его содержимому"""
        # Проверяем известные форматы
        image_type = imghdr.what(None, h=data)
        if image_type:
            return image_type if image_type != 'jpeg' else 'jpg'
        
        # Проверяем по сигнатурам
        for signature, ext in FILE_SIGNATURES.items():
            if data.startswith(signature):
                return ext
        
        # Если не удалось определить, берем из URL
        return mimetypes.guess_extension(mimetypes.types_map.get(url.split('.')[-1].lower(), '')) or 'bin'
    
    async def check_image_size(self, data: bytes) -> Tuple[int, int]:
        """Определяет размер изображения"""
        try:
            img = Image.open(BytesIO(data))
            return img.size
        except Exception as e:
            logger.error(f"Ошибка определения размеров изображения: {str(e)}")
        return (0, 0)
    
    async def download_and_verify(self, url: str, user_info: str) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
        """Скачивает файл с проверкой целостности"""
        for attempt in range(MAX_RETRIES):
            try:
                session = await self.bot.get_session()
                headers = {"User-Agent": random.choice(self.bot.user_agents)}
                
                # Увеличиваем таймаут для iili.io
                timeout_settings = aiohttp.ClientTimeout(
                    total=60 if "iili.io" in url else 15,  # Увеличен до 60 секунд
                    sock_connect=20,
                    sock_read=60 if "iili.io" in url else 15
                )
                
                async with session.get(url, headers=headers, timeout=timeout_settings) as response:
                    if response.status != 200:
                        return None, None, None
                    
                    # Проверяем размер файла
                    file_size = 0
                    content_length = response.headers.get('Content-Length')
                    if content_length:
                        try:
                            file_size = int(content_length)
                        except (TypeError, ValueError):
                            pass
                    
                    if file_size > MAX_FILE_SIZE:
                        return url, 'too_big', None
                    
                    # Скачиваем файл частями с контролем размера
                    downloaded = 0
                    chunks = []
                    async for chunk in response.content.iter_chunked(1024*512):  # 512KB блоки
                        if not chunk:
                            raise IncompleteDownloadError("Получен пустой чанк")
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        
                        # Проверка на превышение максимального размера
                        if downloaded > MAX_FILE_SIZE:
                            logger.warning(f"Файл превысил лимит во время скачивания: {url}", extra={'user_info': user_info})
                            return url, 'too_big', None
                    
                    data = b''.join(chunks)
                    
                    # Проверяем соответствие размера
                    if content_length and downloaded != int(content_length):
                        raise IncompleteDownloadError(
                            f"Ожидалось {content_length} байт, получено {downloaded}"
                        )
                    
                    # Проверяем что данные не пустые
                    if not data:
                        raise IncompleteDownloadError("Получены пустые данные")
                    
                    # Дополнительная проверка целостности изображений
                    try:
                        img = Image.open(BytesIO(data))
                        img.verify()  # Проверка целостности
                    except Exception as e:
                        raise IncompleteDownloadError(f"Проверка изображения не удалась: {str(e)}")
                    
                    # Определяем реальное расширение
                    extension = await self.get_actual_extension(data, url)
                    
                    # Проверяем размеры изображения
                    if extension in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']:
                        width, height = await self.check_image_size(data)
                        if width < MIN_WIDTH or height < MIN_HEIGHT:
                            logger.info(f"Изображение слишком маленькое: {width}x{height} < {MIN_WIDTH}x{MIN_HEIGHT}", extra={'user_info': user_info})
                            return None, None, None
                    
                    return url, extension, data
            
            except IncompleteDownloadError as e:
                logger.error(f"Неполная загрузка (попытка {attempt+1}/{MAX_RETRIES}): {url} - {str(e)}", extra={'user_info': user_info})
                if attempt == MAX_RETRIES - 1:
                    return None, None, None
                await asyncio.sleep(1)  # Задержка перед повторной попыткой
                
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка (попытка {attempt+1}/{MAX_RETRIES}): {url} - {str(e)}", extra={'user_info': user_info})
                if attempt == MAX_RETRIES - 1:
                    return None, None, None
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Неожиданная ошибка (попытка {attempt+1}/{MAX_RETRIES}): {url} - {str(e)}", extra={'user_info': user_info})
                if attempt == MAX_RETRIES - 1:
                    return None, None, None
                await asyncio.sleep(1)
        
        return None, None, None
    
    
    async def check_image(self, url: str, user_info: str) -> Tuple[Optional[str], Optional[str]]:
        """Проверяет изображение с несколькими попытками"""
        for attempt in range(MAX_RETRIES):
            try:
                # Сначала пробуем HEAD запрос для быстрой проверки
                session = await self.bot.get_session()
                headers = {"User-Agent": random.choice(self.bot.user_agents)}
                
                async with session.head(url, headers=headers, 
                                     allow_redirects=True,
                                     timeout=aiohttp.ClientTimeout(total=10)) as response:
                    
                    if response.status != 200:
                        return None, None
                        
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(x in content_type for x in ['image', 'video', 'audio', 'application']):
                        return None, None
                    
                    final_url = str(response.url)
                    if any(x in final_url.lower() for x in ["removed", "deleted", "error"]):
                        return None, None
                
                # Если HEAD успешен, делаем полную загрузку
                img_url, extension, _ = await self.download_and_verify(final_url, user_info)
                if not img_url or not extension:
                    continue
                return img_url, extension
                
            except Exception as e:
                logger.error(f"Ошибка проверки изображения {url} (попытка {attempt+1}/{MAX_RETRIES}): {str(e)}", extra={'user_info': user_info})
                if attempt == MAX_RETRIES - 1:
                    return None, None
                await asyncio.sleep(1)
        
        return None, None

class KappaLolSource(ImageSource):
    async def generate_urls(self, batch_size: int) -> List[str]:
        chars = string.ascii_letters + string.digits + "-_"
        urls = []
        for _ in range(batch_size):
            length = random.choice([5, 6])
            code = ''.join(random.choices(chars, k=length))
            urls.append(f"https://zov.gachi.gay/{code}")
        return urls

    async def check_image(self, url: str, user_info: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            session = await self.bot.get_session()
            headers = {"User-Agent": random.choice(self.bot.user_agents)}
            
            # Увеличиваем таймаут для DNS-запросов
            timeout = aiohttp.ClientTimeout(total=30, sock_connect=15)
            
            async with session.head(url, headers=headers, 
                                 allow_redirects=True,
                                 timeout=timeout) as response:
                
                if response.status == 404:
                    return None, None
                    
                if response.status != 200:
                    # Логируем HTTP ошибки как обычные ошибки источника
                    await self.bot.handle_source_error('kappa', 
                        Exception(f"HTTP status {response.status}"), url, user_info)
                    return None, None
                    
                content_type = response.headers.get("content-type", "").lower()
                supported_types = ['image', 'video', 'audio', 'application', 'text']
                
                if not any(x in content_type for x in supported_types) or 'wav' in content_type:
                    return None, None
                
                final_url = str(response.url)
                
                # Проверяем размер файла
                file_size = 0
                content_length = response.headers.get('Content-Length')
                if content_length:
                    try:
                        file_size = int(content_length)
                    except (TypeError, ValueError):
                        pass
                
                # Если файл слишком большой
                if file_size > MAX_FILE_SIZE:
                    logger.info(f"[KAPPA] Файл слишком большой ({file_size//1024//1024}MB): {final_url}", extra={'user_info': user_info})
                    return final_url, 'too_big'
                
                # Скачиваем и проверяем изображение
                img_url, extension, _ = await self.download_and_verify(final_url, user_info)
                if not img_url or not extension:
                    return None, None
                
                # Для изображений уже выполнена проверка размеров
                if 'image' in content_type:
                    return img_url, extension
                
                # Для других типов контента
                if 'gif' in content_type:
                    return final_url, 'gif'
                elif 'mp4' in content_type or 'webm' in content_type:
                    return final_url, 'video'
                elif 'octet-stream' in content_type or \
                     'application' in content_type or \
                     'text' in content_type:
                    logger.info(f"[KAPPA] Найден документ: {final_url}", extra={'user_info': user_info})
                    return final_url, 'document'
                else:
                    return final_url, 'file'
                    
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            # Специальная обработка для ошибок сети
            logger.debug(f"Сетевая ошибка Kappa: {type(e).__name__}", extra={'user_info': user_info})
            await self.bot.handle_source_error('kappa', e, url, user_info)
            return None, None
        except Exception as e:
            # Обработка всех остальных ошибок
            logger.error(f"Неизвестная ошибка в Kappa: {str(e)}", extra={'user_info': user_info})
            await self.bot.handle_source_error('kappa', e, url, user_info)
            return None, None

class ImgurSource(ImageSource):
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
            source_name = f'imgur{self.length}'
            await self.bot.handle_source_error(source_name, e, url, "System")
            return None

class PrntSource(ImageSource):
    async def generate_urls(self, batch_size: int) -> List[str]:
        return [f"https://prnt.sc/{self.bot.generate_random_string(6)}" 
                for _ in range(batch_size)]
    
    async def extract_image_url(self, url: str) -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                session = await self.bot.get_session()
                headers = {
                    "User-Agent": random.choice(self.bot.user_agents),
                    "Referer": "https://prnt.sc/",
                }
                
                async with session.get(url, headers=headers, 
                                     timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        continue
                        
                    text = await response.text()

                soup = BeautifulSoup(text, "html.parser")
                
                if soup.find('div', class_='no-image'):
                    return None

                img_url = None
                img_tag = soup.find("img", {"class": "screenshot-image"})
                if img_tag and img_tag.get("src"):
                    img_url = img_tag["src"]
                    # Обновленная проверка на плейсхолдер
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
                    
                # Дополнительная проверка на плейсхолдер
                if any(x in img_url.lower() for x in ["prnt.sc/placeholder", "st.prntscr.com", "prntscr.com/placeholder"]):
                    return None
                    
                if "removed.png" in img_url.lower():
                    return None
                    
                return img_url
                
            except Exception as e:
                logger.error(f"Ошибка извлечения URL из prnt.sc (попытка {attempt+1}): {str(e)}", extra={'user_info': "System"})
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                else:
                    await self.bot.handle_source_error('prnt', e, url, "System")
        return None

class PasteNowSource(ImageSource):
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
            await self.bot.handle_source_error('pastenow', e, url, "System")
            return None

class FreeImageSource(ImageSource):
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
            await self.bot.handle_source_error('freeimage', e, url, "System")
            return None

class ImageBot:
    def __init__(self):
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        ]
        
        self.sources = {
            'imgur5': ImgurSource(self, 5),
            'imgur7': ImgurSource(self, 7),
            'prnt': PrntSource(self),
            'pastenow': PasteNowSource(self),
            'freeimage': FreeImageSource(self),
            'kappa': KappaLolSource(self)
        }
        
        self.sessions = {}
        self.last_commands = {}
        self.media_groups = {}
        self.sent_image_ids = {}
        self.command_cooldowns = {}
        self.source_errors = {}
        self.last_status_update = {}
        self.dns_error_count = {}
        self.last_dns_error_log = {}
        
        self.send_semaphore = asyncio.Semaphore(5)
        self.session = None
        self._session_initialized = False

    def is_source_disabled(self, source: str) -> bool:
        """Проверяет временную блокировку источника с разными таймаутами"""
        if source in self.source_errors:
            error_time = self.source_errors[source]
            
            # Определяем время блокировки в зависимости от типа ошибки
            if "NameResolutionError" in str(self.source_errors.get(source, "")):
                timeout = DNS_ERROR_TIMEOUT
            else:
                timeout = SOURCE_TIMEOUT
            
            # Автоматически снимаем блокировку по истечении времени
            if time.time() - error_time < timeout:
                return True
            else:
                del self.source_errors[source]  # Разблокируем источник
        return False

    async def handle_source_error(self, source: str, error: Exception, url: Optional[str] = None, user_info: str = "System"):
        """Блокирует источник при ошибках с группировкой сообщений"""
        # Определяем тип ошибки
        error_type = type(error).__name__
        error_str = str(error)
        
        # Группируем ошибки DNS
        if "NameResolutionError" in error_type or "getaddrinfo failed" in error_str:
            current_time = time.time()
            
            # Проверяем, нужно ли логировать эту ошибку
            if source in self.last_dns_error_log:
                last_log_time = self.last_dns_error_log[source]
                if current_time - last_log_time < 60:  # 1 минута кэширования
                    return
                
            # Логируем и обновляем время последнего логирования
            logger.error(f"🚨 КРИТИЧЕСКАЯ ОШИБКА DNS: {source} - {error_str}", extra={'user_info': user_info})
            logger.warning(f"🔒 Источник {source} заблокирован на {DNS_ERROR_TIMEOUT//60} минут из-за ошибки DNS", extra={'user_info': user_info})
            self.last_dns_error_log[source] = current_time
            self.source_errors[source] = current_time
            return
        
        # Для обычных ошибок
        error_msg = f"Ошибка источника {source}"
        if url:
            error_msg += f" (URL: {url})"
        error_msg += f": {error_type}"
        if error_str:
            error_msg += f" - {error_str}"
        
        logger.error(error_msg, extra={'user_info': user_info})
        self.source_errors[source] = time.time()
        return source

    async def get_session(self):
        if not self._session_initialized or (self.session and self.session.closed):
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            connector = aiohttp.TCPConnector(limit=50, force_close=True)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            self._session_initialized = True
        return self.session

    async def cleanup(self):
        """Асинхронная очистка ресурсов с обработкой ошибок"""
        try:
            # Закрываем aiohttp сессию
            if self.session and not self.session.closed:
                try:
                    await self.session.close()
                except RuntimeError as e:
                    if "Event loop is closed" not in str(e):
                        logger.error(f"Ошибка при закрытии сессии: {str(e)}")
                except Exception as e:
                    logger.error(f"Ошибка при закрытии сессии: {str(e)}")
            
            # Останавливаем все активные сессии пользователей
            for key in list(self.sessions.keys()):
                try:
                    await self.stop_session(key, "shutdown")
                except Exception as e:
                    logger.error(f"Ошибка при остановке сессии пользователя: {str(e)}")
            
        except RuntimeError as e:
            if "Event loop is closed" in str(e):
                logger.info("Event loop закрыт, пропускаем асинхронную очистку")
            else:
                logger.error(f"RuntimeError при очистке: {str(e)}")
        except Exception as e:
            logger.error(f"Критическая ошибка при очистке: {str(e)}")
        finally:
            # Очищаем все структуры данных
            self.media_groups.clear()
            self.sent_image_ids.clear()
            self.command_cooldowns.clear()
            self.source_errors.clear()
            self.last_status_update.clear()
            self.dns_error_count.clear()
            self.last_dns_error_log.clear()
            self.sessions.clear()

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
        elif "zov.gachi.gay" in url:
            return url.split("/")[-1].split("?")[0]  # Удаляем query-параметры
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

    async def add_to_media_group(self, update: Update, key: Tuple[int, int], 
                               url: str, ext: str, count: int, found: int, source: str):
        try:
            # Получаем информацию о пользователе из сессии
            user_info = self.sessions.get(key, {}).get("user_info", "System")
            
            image_id = self.extract_image_id(url)
            display_url = f"[{image_id}]({url})"
            caption = f"({found}/{count}) {display_url} [{source.upper()}]"
            
            if key not in self.sent_image_ids:
                self.sent_image_ids[key] = set()
            
            if image_id in self.sent_image_ids[key]:
                return
            
            self.sent_image_ids[key].add(image_id)
            
            # Обработка слишком больших файлов (>49MB)
            if ext == "too_big":
                logger.info(f"[{source.upper()}] Файл слишком большой: {url}", extra={'user_info': user_info})
                caption = f"⚠️ Файл слишком большой для Telegram (максимум 50 МБ)\n{caption}"
                await self.send_large_file_image(update, key, caption, image_id)
                return
            
            # Обработка документов
            if ext in ['document', 'file', 'audio', 'flac', 'mp3']:
                logger.info(f"[{source.upper()}] Отправка документа: {url}", extra={'user_info': user_info})
                await self.send_document(update, key, url, caption)
                return
            
            if ext == "gif":
                media_item = InputMediaAnimation(media=url, caption=caption, parse_mode="Markdown")
                await self.send_media(update, key, [media_item])
                return
            elif ext == "video":
                media_item = InputMediaVideo(media=url, caption=caption, parse_mode="Markdown")
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
                    return
            
            media_item = InputMediaPhoto(media=url, caption=caption, parse_mode="Markdown")
            group["media"].append(media_item)
            group["last_added"] = time.time()
            
            if group["timer_task"] is None or group["timer_task"].done():
                group["timer_task"] = asyncio.create_task(self.group_timer(update, key))
            
            if len(group["media"]) >= MAX_GROUP_SIZE:
                media_to_send = group["media"]
                group["media"] = []
                logger.info(f"Достигнут максимальный размер группы ({MAX_GROUP_SIZE}). Отправка группы...", extra={'user_info': user_info})
                await self.send_media(update, key, media_to_send)
        except Exception as e:
            logger.error(f"Ошибка в add_to_media_group: {str(e)}", extra={'user_info': user_info})

    async def send_large_file_image(self, update: Update, key: Tuple[int, int], caption: str, url: str):
        """Отправляет картинку-заглушку для слишком больших файлов"""
        if not os.path.exists(LARGE_FILE_IMAGE):
            logger.error(f"Файл заглушки не найден: {LARGE_FILE_IMAGE}")
            # Если картинка не найдена, отправляем текстовое сообщение
            await update.message.reply_text(caption, parse_mode="Markdown")
            return
            
        async with self.send_semaphore:
            try:
                with open(LARGE_FILE_IMAGE, 'rb') as photo:
                    await update.message.reply_photo(
                        photo=photo,
                        caption=caption,
                        parse_mode="Markdown",
                        write_timeout=MEDIA_SEND_TIMEOUT,
                        connect_timeout=MEDIA_SEND_TIMEOUT
                    )
            except RetryAfter as e:
                wait_time = e.retry_after
                logger.warning(f"Превышен лимит отправки. Ожидание: {wait_time} сек")
                await asyncio.sleep(wait_time)
                with open(LARGE_FILE_IMAGE, 'rb') as photo:
                    await update.message.reply_photo(
                        photo=photo,
                        caption=caption,
                        parse_mode="Markdown",
                        write_timeout=MEDIA_SEND_TIMEOUT,
                        connect_timeout=MEDIA_SEND_TIMEOUT
                    )
            except BadRequest as e:
                logger.error(f"Ошибка BadRequest при отправке заглушки для {url}: {str(e)}")
                await update.message.reply_text(caption, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Ошибка отправки заглушки для {url}: {str(e)}")
                await update.message.reply_text(caption, parse_mode="Markdown")

    async def send_document(self, update: Update, key: Tuple[int, int], url: str, caption: str):
        """Отправляет документ как отдельное сообщение"""
        async with self.send_semaphore:
            try:
                await update.message.reply_document(
                    document=url,
                    caption=caption,
                    parse_mode="Markdown",
                    write_timeout=MEDIA_SEND_TIMEOUT,
                    connect_timeout=MEDIA_SEND_TIMEOUT
                )
            except RetryAfter as e:
                wait_time = e.retry_after
                logger.warning(f"Превышен лимит отправки документов. Ожидание: {wait_time} сек")
                await asyncio.sleep(wait_time)
                await update.message.reply_document(
                    document=url,
                    caption=caption,
                    parse_mode="Markdown",
                    write_timeout=MEDIA_SEND_TIMEOUT,
                    connect_timeout=MEDIA_SEND_TIMEOUT
                )
            except BadRequest as e:
                if "File is too big" in str(e):
                    logger.warning(f"Файл слишком большой: {url}")
                    # Отправляем картинку-заглушку
                    caption = f"⚠️ Файл слишком большой для Telegram (максимум 50 МБ)\n{caption}"
                    await self.send_large_file_image(update, key, caption, url)
                elif "Wrong file identifier" in str(e):
                    logger.warning(f"Неверный идентификатор документа: {url}")
                else:
                    logger.error(f"Ошибка BadRequest при отправке документа {url}: {str(e)}")
            except Exception as e:
                logger.error(f"Ошибка отправки документа {url}: {str(e)}")

    async def send_media(self, update: Update, key: Tuple[int, int], media_group: List):
        if not media_group:
            return
            
        # Получаем информацию о пользователе из сессии
        user_info = self.sessions.get(key, {}).get("user_info", "System")
        
        async with self.send_semaphore:
            try:
                photos = [m for m in media_group if not isinstance(m, (InputMediaAnimation, InputMediaVideo))]
                animations = [m for m in media_group if isinstance(m, InputMediaAnimation)]
                videos = [m for m in media_group if isinstance(m, InputMediaVideo)]
                
                sent_count = 0
                
                if photos:
                    try:
                        await update.message.reply_media_group(
                            media=photos,
                            parse_mode="Markdown",
                            write_timeout=MEDIA_SEND_TIMEOUT,
                            connect_timeout=MEDIA_SEND_TIMEOUT
                        )
                        sent_count += len(photos)
                        logger.info(f"Успешно отправлена группа из {len(photos)} фото", extra={'user_info': user_info})
                    except RetryAfter as e:
                        wait_time = e.retry_after
                        logger.warning(f"Превышен лимит отправки. Ожидание: {wait_time} сек", extra={'user_info': user_info})
                        await asyncio.sleep(wait_time)
                        await update.message.reply_media_group(
                            media=photos,
                            parse_mode="Markdown",
                            write_timeout=MEDIA_SEND_TIMEOUT,
                            connect_timeout=MEDIA_SEND_TIMEOUT
                        )
                        sent_count += len(photos)
                        logger.info(f"Успешно отправлена группа из {len(photos)} фото после ожидания", extra={'user_info': user_info})
                    except Exception as e:
                        logger.error(f"Ошибка отправки группы фото: {str(e)}", extra={'user_info': user_info})
                        logger.info("Попытка отправить фото по отдельности...", extra={'user_info': user_info})
                        for photo in photos:
                            try:
                                await update.message.reply_photo(
                                    photo=photo.media,
                                    caption=photo.caption,
                                    parse_mode="Markdown",
                                    write_timeout=MEDIA_SEND_TIMEOUT,
                                    connect_timeout=MEDIA_SEND_TIMEOUT
                                )
                                sent_count += 1
                                logger.info(f"Успешно отправлено фото #{sent_count}", extra={'user_info': user_info})
                            except BadRequest as e:
                                error_msg = str(e)
                                if any(err in error_msg for err in PHOTO_SEND_ERRORS):
                                    logger.error(f"Ошибка отправки фото {photo.media}: {error_msg}. Отправляю как документ.", extra={'user_info': user_info})
                                    await self.send_document(update, key, photo.media, photo.caption)
                                    sent_count += 1
                                else:
                                    logger.error(f"Ошибка отправки фото {photo.media}: {str(e)}", extra={'user_info': user_info})
                            except Exception as e2:
                                logger.error(f"Ошибка отправки фото {photo.media}: {str(e2)}", extra={'user_info': user_info})

                for animation in animations:
                    try:
                        await update.message.reply_animation(
                            animation=animation.media,
                            caption=animation.caption,
                            parse_mode="Markdown",
                            write_timeout=MEDIA_SEND_TIMEOUT,
                            connect_timeout=MEDIA_SEND_TIMEOUT
                        )
                        sent_count += 1
                        logger.info(f"Успешно отправлена анимация", extra={'user_info': user_info})
                    except Exception as e:
                        logger.error(f"Ошибка отправки анимации {animation.media}: {str(e)}", extra={'user_info': user_info})
                
                for video in videos:
                    try:
                        await update.message.reply_video(
                            video=video.media,
                            caption=video.caption,
                            parse_mode="Markdown",
                            write_timeout=MEDIA_SEND_TIMEOUT,
                            connect_timeout=MEDIA_SEND_TIMEOUT
                        )
                        sent_count += 1
                        logger.info(f"Успешно отправлено видео", extra={'user_info': user_info})
                    except Exception as e:
                        logger.error(f"Ошибка отправки видео {video.media}: {str(e)}", extra={'user_info': user_info})
                
                # Логируем результат отправки
                total_media = len(media_group)
                if sent_count < total_media:
                    logger.warning(f"Отправлено {sent_count}/{total_media} медиа из группы", extra={'user_info': user_info})
                else:
                    logger.info(f"Успешно отправлено {sent_count}/{total_media} медиа", extra={'user_info': user_info})
            except Exception as e:
                logger.error(f"Критическая ошибка отправки медиа: {str(e)}", extra={'user_info': user_info})

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
                    logger.info("Сработал таймер группировки. Отправка группы...")
                    await self.send_media(update, key, media_to_send)
                    break
        except Exception as e:
            logger.error(f"Ошибка в group_timer: {str(e)}")
        finally:
            if key in self.media_groups:
                self.media_groups[key]["timer_task"] = None

    async def show_main_menu(self, update: Update):
        reply_keyboard = [
            ["ВСЕ ИСТОЧНИКИ"],
            ["PRNT.SC", "IMGUR"],
            ["PASTENOW", "FREEIMAGE", "KAPPA"],
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
- Kappa.lol

Используйте кнопки ниже для начала поиска или команды:
/getimg <5|7> <1-50> - поиск на Imgur
/getprnt <1-50> - поиск на prnt.sc
/getpastenow <1-50> - поиск на paste.pics
/getfreeimage <1-50> - поиск на freeimage
/getkappa <1-50> - поиск на kappa.lol
/getall <1-50> - поиск на всех источниках
/stop - остановить текущий поиск
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
        logger.info(f"Поиск пользователя {user_info(update)} остановлен. Источник: {source_name}. Время: {format_time(elapsed)}")
        
        session["stop"] = True
        
        media_group = []
        if key in self.media_groups:
            group_data = self.media_groups.pop(key, {})
            media_group = group_data.get("media", [])
        
        if media_group:
            logger.info("Отправка оставшихся медиа в группе...")
            await self.send_media(update, key, media_group)
        
        # Собираем статистику перед удалением сессии
        stats_text = ""
        if session["source_type"] == "all":
            stats_lines = []
            for src, data in session.get("sources", {}).items():
                src_name = get_source_name(src)
                found_count = data.get("found", 0)
                status = data.get("status", "неизвестно")
                stats_lines.append(f"{src_name}: найдено {found_count}, статус: {status}")
            stats_text = "\nСтатистика по источникам:\n" + "\n".join(stats_lines)
        
        self.cleanup_user_session(key)
        
        if not silent:
            target = session["target_count"]
            found = session.get("found", 0)
            analyzed = session.get("analyzed", 0)
            stop_reason = session.get("stop_reason", "")
            
            message = f"🔴 Поиск остановлен\n"
            if stop_reason == "source_disabled":
                message += f"⚠️ Источник {source_name} временно недоступен\n"
            elif stop_reason == "all_sources_disabled":
                message += "⚠️ Все источники временно недоступны\n"
            message += (
                f"Цель: {target} изображений\n"
                f"Найдено: {found}/{target}\n"
                f"Проверено: {analyzed}\n"
                f"Время: {format_time(elapsed)}"
                f"{stats_text}"  # Добавляем статистику здесь
            )
            
            await update.message.reply_text(message)
            await self.show_main_menu(update)

    async def stop_session(self, key: Tuple[int, int], reason: str = "user"):
        """Безопасная остановка сессии с обработкой асинхронных задач"""
        session = self.sessions.get(key)
        if not session:
            return
            
        session["stop"] = True
        session["stop_reason"] = reason
        
        # Отменяем все связанные задачи
        if "tasks" in session:
            for task in session["tasks"]:
                if not task.done():
                    try:
                        task.cancel()
                        # Даем задаче шанс корректно завершиться
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"Ошибка при отмене задачи: {str(e)}")
        
        # Удаляем сессию из структур данных
        if key in self.sessions:
            del self.sessions[key]
        if key in self.media_groups:
            del self.media_groups[key]
        if key in self.sent_image_ids:
            del self.sent_image_ids[key]
        if key in self.last_status_update:
            del self.last_status_update[key]

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
        
        if key in self.last_status_update:
            del self.last_status_update[key]

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
        elif last_command["type"] == "kappa":
            context.args = [str(last_command["count"])]
            await self.get_kappa_images(update, context)
        elif last_command["type"] == "all":
            context.args = [str(last_command["count"])]
            await self.search_all_sources(update, context)

    async def update_status_message(self, key: Tuple[int, int], force: bool = False):
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
            
            last_text = session.get("last_status_text", "")
            if force or text != last_text:
                try:
                    await session["status_msg"].edit_text(text)
                    session["last_status_text"] = text
                    self.last_status_update[key] = current_time
                except RetryAfter as e:
                    wait_time = e.retry_after
                    await asyncio.sleep(wait_time)
                    await session["status_msg"].edit_text(text)
                    session["last_status_text"] = text
                    self.last_status_update[key] = time.time()
                except BadRequest:
                    pass
        except Exception:
            pass

    async def _generic_search(self, update: Update, key: Tuple[int, int], 
                            source_type: str, count: int, length: int = None):
        try:
            session = self.sessions[key]
            user_info_str = session.get("user_info", "System")
            source = self.sources[source_type]
            last_update_time = time.time()
            retries = 0
            
            # Проверка блокировки источника
            if self.is_source_disabled(source_type):
                logger.info(f"Источник {source_type} временно отключен", extra={'user_info': user_info_str})
                session["stop"] = True
                session["stop_reason"] = "source_disabled"
                source_name = get_source_name(source_type, length)
                logger.info(f"Поиск остановлен: источник {source_name} отключен", extra={'user_info': user_info_str})
                return
                
            await self.update_status_message(key, force=True)
            
            while not session.get("stop", False) and session["found"] < count:
                # Проверяем блокировку источника в начале каждой итерации
                if self.is_source_disabled(source_type):
                    logger.info(f"Источник {source_type} временно отключен", extra={'user_info': user_info_str})
                    session["stop"] = True
                    session["stop_reason"] = "source_disabled"
                    source_name = get_source_name(source_type, length)
                    logger.info(f"Поиск остановлен: источник {source_name} отключен", extra={'user_info': user_info_str})
                    break
                
                current_time = time.time()
                if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                    await self.update_status_message(key)
                    last_update_time = current_time
                
                try:
                    urls = await source.generate_urls(BATCH_SIZES[source_type])
                except Exception as e:
                    await self.handle_source_error(source_type, e, None, user_info_str)
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["stop"] = True
                        session["stop_reason"] = "error"
                    await asyncio.sleep(0.5)
                    continue
                
                for url in urls:
                    if session.get("stop", False) or session["found"] >= count:
                        break
                        
                    try:
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        
                        if analyzed % UPDATE_ON_CHECKED == 0:
                            await self.update_status_message(key, force=True)
                        
                        img_url = await source.extract_image_url(url)
                        if not img_url:
                            continue
                            
                        # Для iili.io делаем две попытки проверки
                        if "iili.io" in img_url:
                            final_url, ext = None, None
                            for _ in range(2):  # Дополнительная попытка для iili.io
                                final_url, ext = await source.check_image(img_url, user_info_str)
                                if final_url and ext:
                                    break
                                await asyncio.sleep(0.5)
                        else:
                            final_url, ext = await source.check_image(img_url, user_info_str)
                            
                        if not final_url or not ext:
                            continue
                            
                        session["found"] += 1
                        found = session["found"]
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                        
                        await self.add_to_media_group(
                            update, key, final_url, ext, count, found, source_type
                        )
                    
                    except Exception as e:
                        await self.handle_source_error(source_type, e, url, user_info_str)
                        retries += 1
                        if retries >= MAX_RETRIES:
                            session["stop"] = True
                            session["stop_reason"] = "error"
                            break
                        continue
                
                retries = 0  # Сброс счетчика после успешного batch
                await asyncio.sleep(0.1)
            
        except asyncio.CancelledError:
            pass
        finally:
            await self.update_status_message(key, force=True)
            
            if key in self.media_groups and self.media_groups[key].get("media"):
                logger.info("Отправка оставшихся медиа в группе...")
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                source_name = get_source_name(session["source_type"], session.get("length"))
                
                if session.get("stop", False) and found < target:
                    text = f"🔴 Поиск остановлен\n"
                    if stop_reason == "source_disabled":
                        text += f"⚠️ Источник {source_name} временно недоступен\n"
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
                except Exception:
                    pass
                
                # Логируем результаты поиска
                if stop_reason == "source_disabled":
                    status = "отключен"
                elif session.get("stop", False) and found < target:
                    status = "остановлен"
                else:
                    status = "завершен"
                
                logger.info(
                    f"Поиск {source_name} {status}. "
                    f"Цель: {target}, найдено: {found}, "
                    f"проверено: {analyzed}, время: {format_time(elapsed)}",
                    extra={'user_info': user_info_str}
                )
                
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

        source_type = f"imgur{length}"
        
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
            "length": length,
            "user_info": user_info(update)
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "imgur", "length": length, "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        logger.info(f"Поиск пользователя {user_info(update)} начат. Imgur ({length}). Цель: {count} изображений")
        
        asyncio.create_task(self._generic_search(update, key, source_type, count))

    async def get_prnt_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        if await self.check_cooldown(update): return
        
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

        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "prnt" and current_session["target_count"] == count:
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
            "source_type": "prnt",
            "user_info": user_info(update)
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "prnt", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        logger.info(f"Поиск пользователя {user_info(update)} начат. Prnt.sc. Цель: {count} изображений")
        
        asyncio.create_task(self._generic_search(update, key, "prnt", count))

    async def get_pastenow_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        if await self.check_cooldown(update): return
        
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

        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "pastenow" and current_session["target_count"] == count:
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
            "source_type": "pastenow",
            "user_info": user_info(update)
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "pastenow", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        logger.info(f"Поиск пользователя {user_info(update)} начат. Paste.pics. Цель: {count} изображений")
        
        asyncio.create_task(self._generic_search(update, key, "pastenow", count))

    async def get_freeimage_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        if await self.check_cooldown(update): return
        
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

        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "freeimage" and current_session["target_count"] == count:
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
            "source_type": "freeimage",
            "user_info": user_info(update)
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "freeimage", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        logger.info(f"Поиск пользователя {user_info(update)} начат. Freeimage. Цель: {count} изображений")
        
        asyncio.create_task(self._generic_search(update, key, "freeimage", count))

    async def get_kappa_images(self, update: Update, context: CallbackContext):
        key = self.get_key(update)
        if await self.check_cooldown(update): return
        
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Используйте: /getkappa <1-50>")
            return

        try: count = int(args[0])
        except ValueError:
            await update.message.reply_text("Количество должно быть числом")
            return

        if not 1 <= count <= 50:
            await update.message.reply_text("Можно запросить от 1 до 50 изображений за раз")
            return

        if key in self.sessions:
            current_session = self.sessions[key]
            if current_session["source_type"] == "kappa" and current_session["target_count"] == count:
                await update.message.reply_text("❗️ Идентичный поиск уже выполняется.")
                return
        
        if key in self.sessions:
            await self.stop(update, context, silent=True)
            await asyncio.sleep(1)
        
        status_msg = await update.message.reply_text(
            f"🔍 Поиск Kappa.lol начат\n"
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
            "source_type": "kappa",
            "user_info": user_info(update)
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "kappa", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        logger.info(f"Поиск пользователя {user_info(update)} начат. Kappa.lol. Цель: {count} изображений")
        
        asyncio.create_task(self._generic_search(update, key, "kappa", count))

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
                "freeimage": {"active": True, "found": 0},
                "kappa": {"active": True, "found": 0}
            },
            "user_info": user_info(update)
        }
        self.sessions[key] = session_data
        self.last_commands[key] = {"type": "all", "count": count}
        self.command_cooldowns[key] = time.time()

        await self.show_main_menu(update)
        logger.info(f"Поиск пользователя {user_info(update)} начат. Все источники. Цель: {count} изображений")
        
        await self.update_status_message(key, force=True)
        
        asyncio.create_task(self._search_all_sources(update, key, count))

    async def _search_all_sources(self, update: Update, key: Tuple[int, int], count: int):
        async def search_source(source_type: str):
            nonlocal session
            user_info_str = session.get("user_info", "System")
            source = self.sources[source_type]
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
                
                # Проверяем блокировку источника перед каждой итерацией
                if self.is_source_disabled(source_type):
                    logger.info(f"Источник {source_type} временно отключен", extra={'user_info': user_info_str})
                    session["sources"][source_type]["active"] = False
                    session["sources"][source_type]["status"] = "отключен"
                    break
                
                current_time = time.time()
                if current_time - last_update_time >= STATUS_UPDATE_INTERVAL:
                    await self.update_status_message(key)
                    last_update_time = current_time
                
                try:
                    urls = await source.generate_urls(batch_size)
                except Exception as e:
                    await self.bot.handle_source_error(source_type, e, None, user_info_str)
                    retries += 1
                    if retries >= MAX_RETRIES:
                        session["sources"][source_type]["active"] = False
                        session["sources"][source_type]["status"] = "отключен"
                        break
                    await asyncio.sleep(1)
                    continue
                
                for url in urls:
                    if session.get("stop", False) or session["found"] >= count:
                        break
                    if session["sources"][source_type]["found"] >= max_per_source:
                        break
                    
                    try:
                        session["analyzed"] += 1
                        analyzed = session["analyzed"]
                        
                        if analyzed % UPDATE_ON_CHECKED == 0:
                            await self.update_status_message(key, force=True)
                        
                        img_url = await source.extract_image_url(url)
                        if not img_url:
                            continue
                            
                        # Для iili.io делаем две попытки проверки
                        if "iili.io" in img_url:
                            final_url, ext = None, None
                            for _ in range(2):  # Дополнительная попытка для iili.io
                                final_url, ext = await source.check_image(img_url, user_info_str)
                                if final_url and ext:
                                    break
                                await asyncio.sleep(0.5)
                        else:
                            final_url, ext = await source.check_image(img_url, user_info_str)
                            
                        if not final_url or not ext:
                            continue
                            
                        session["found"] += 1
                        session["sources"][source_type]["found"] += 1
                        found = session["found"]
                        
                        if found % UPDATE_ON_FOUND == 0:
                            await self.update_status_message(key, force=True)
                        
                        await self.add_to_media_group(
                            update, key, final_url, ext, count, found, source_type
                        )
                    
                    except Exception as e:
                        await self.bot.handle_source_error(source_type, e, url, user_info_str)
                        retries += 1
                        if retries >= MAX_RETRIES:
                            session["sources"][source_type]["active"] = False
                            session["sources"][source_type]["status"] = "отключен"
                            break
                        continue
                
                retries = 0  # Сброс счетчика после успешного batch
                await asyncio.sleep(0.1)
        
        try:
            session = self.sessions[key]
            user_info_str = session.get("user_info", "System")
            
            tasks = []
            for source_type in SOURCE_WEIGHTS.keys():
                # Пропускаем заблокированные источники
                if not self.is_source_disabled(source_type):
                    session["sources"][source_type]["status"] = "активен"
                    tasks.append(asyncio.create_task(search_source(source_type)))
                else:
                    logger.info(f"Источник {source_type} отключен, пропускаем", extra={'user_info': user_info_str})
                    session["sources"][source_type]["status"] = "отключен"
            
            if not tasks:
                logger.error("Все источники отключены. Поиск невозможен.", extra={'user_info': user_info_str})
                session["stop"] = True
                session["stop_reason"] = "all_sources_disabled"
                return
                
            session["tasks"] = tasks
            await asyncio.gather(*tasks)
            
        except asyncio.CancelledError:
            pass
        finally:
            await self.update_status_message(key, force=True)
            
            if key in self.media_groups and self.media_groups[key].get("media"):
                logger.info("Отправка оставшихся медиа в группе...")
                await self.send_media(update, key, self.media_groups[key]["media"])
            
            if key in self.sessions:
                session = self.sessions[key]
                user_info_str = session.get("user_info", "System")
                elapsed = int(time.time() - session["start_time"])
                target = session["target_count"]
                found = session.get("found", 0)
                analyzed = session.get("analyzed", 0)
                stop_reason = session.get("stop_reason", "")
                
                # Собираем подробную статистику по источникам
                stats_lines = []
                for src, data in session.get("sources", {}).items():
                    src_name = get_source_name(src)
                    found_count = data.get("found", 0)
                    status = data.get("status", "неизвестно")
                    stats_lines.append(f"{src_name}: найдено {found_count}, статус: {status}")
                
                stats_text = "\n".join(stats_lines)
                
                if stop_reason == "source_disabled":
                    message = (
                        f"🔴 Поиск остановлен\n"
                        f"⚠️ Один или несколько источников временно недоступны\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}\n"
                        f"\nСтатистика по источникам:\n{stats_text}"
                    )
                elif stop_reason == "all_sources_disabled":
                    message = (
                        f"🔴 Поиск остановлен\n"
                        f"⚠️ Все источники временно недоступны\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}\n"
                        f"\nСтатистика по источникам:\n{stats_text}"
                    )
                else:
                    message = (
                        f"✅ Поиск завершен\n"
                        f"Цель: {target} изображений\n"
                        f"Найдено: {found}/{target}\n"
                        f"Проверено: {analyzed}\n"
                        f"Время: {format_time(elapsed)}\n"
                        f"\nСтатистика по источникам:\n{stats_text}"
                    )
                
                try:
                    await update.message.reply_text(message)
                except Exception:
                    pass
                
                # Логируем результаты поиска
                logger.info(
                    f"Поиск завершен. Цель: {target}, найдено: {found}, "
                    f"проверено: {analyzed}, время: {format_time(elapsed)}\n"
                    f"Статистика по источникам:\n{stats_text}",
                    extra={'user_info': user_info_str}
                )
                
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

        elif text == "KAPPA":
            reply_keyboard = [
                ["1", "3", "5"],
                ["10", "15", "25"],
                ["50", "НАЗАД"],
            ]
            await update.message.reply_text(
                "KAPPA - Выберите количество:",
                reply_markup=ReplyKeyboardMarkup(
                    reply_keyboard, resize_keyboard=True, is_persistent=True
                ),
            )
            context.user_data["mode"] = "kappa"

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
            elif context.user_data.get("mode") == "kappa":
                context.args = [text]
                await self.get_kappa_images(update, context)
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
        logger.error("Создайте файл token.txt с токеном бота")
        return
    except Exception as e:
        logger.error(f"Ошибка чтения token.txt: {str(e)}")
        return

    if not token:
        logger.error("Токен бота не найден в token.txt")
        return

    # Создаем Application
    application = Application.builder().token(token).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("getimg", bot.get_imgur_images))
    application.add_handler(CommandHandler("getprnt", bot.get_prnt_images))
    application.add_handler(CommandHandler("getpastenow", bot.get_pastenow_images))
    application.add_handler(CommandHandler("getfreeimage", bot.get_freeimage_images))
    application.add_handler(CommandHandler("getkappa", bot.get_kappa_images))
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
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            loop.run_until_complete(bot.cleanup())
        except RuntimeError as e:
            if "Event loop is closed" in str(e):
                logger.info("Event loop закрыт, пропускаем очистку")
            else:
                logger.error(f"Ошибка при очистке: {str(e)}")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при очистке: {str(e)}")

if __name__ == "__main__":
    main()
