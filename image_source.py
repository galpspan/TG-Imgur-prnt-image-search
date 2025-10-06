import asyncio
import imghdr
import random
from io import BytesIO

import aiohttp
from PIL import Image
from typing import List, Tuple, Optional
from config import Config
from logger import Logger

class IncompleteDownloadError(Exception):
    pass


class ImageSource:
    def __init__(self, bot):
        self.bot = bot
        self.logger = Logger()
        self.config = Config()

    async def generate_urls(self, batch_size: int) -> List[str]:
        raise NotImplementedError()

    async def extract_image_url(self, url: str) -> Optional[str]:
        return url

    async def get_actual_extension(self, data: bytes, url: str) -> str:
        # Попробуем определить тип по содержимому
        image_type = imghdr.what(None, h=data)
        if image_type:
            return image_type if image_type != 'jpeg' else 'jpg'

        # Проверим сигнатуры файлов
        for signature, ext in self.config.FILE_SIGNATURES.items():
            if data.startswith(signature):
                return ext

        # Попробуем определить по расширению в URL
        if '.' in url:
            ext = url.split('.')[-1].lower()
            if len(ext) <= 5:  # Расширения обычно короткие
                return ext

        return 'bin'  # Стандартное расширение для бинарных файлов

    async def check_image_size(self, data: bytes) -> Tuple[int, int]:
        try:
            img = Image.open(BytesIO(data))
            width, height = img.size

            # Если размеры (1,1) или меньше, попробуем проверить EXIF на наличие информации о повороте
            if width <= 1 and height <= 1:
                try:
                    exif = img._getexif()
                    # Если есть EXIF и в нем есть тег ориентации (274), то, вероятно, это не маленькое изображение, а ошибка?
                    if exif and 274 in exif:
                        # Вернем условно большие размеры, чтобы не отсеивать
                        return (100, 100)
                except Exception:
                    pass
            return (width, height)
        except Exception as e:
            self.logger.error(f"Ошибка определения размеров изображения: {str(e)}")
        return (0, 0)

    async def download_and_verify(self, url: str, user_info: str) -> Tuple[
        Optional[str], Optional[str], Optional[bytes]]:
        for attempt in range(self.config.MAX_RETRIES):
            try:
                session = await self.bot.get_session()
                headers = {"User-Agent": random.choice(self.bot.user_agents)}

                timeout_settings = aiohttp.ClientTimeout(
                    total=60 if "iili.io" in url else 15,
                    sock_connect=20,
                    sock_read=60 if "iili.io" in url else 15
                )

                async with session.get(url, headers=headers, timeout=timeout_settings) as response:
                    if response.status != 200:
                        return None, None, None

                    file_size = 0
                    content_length = response.headers.get('Content-Length')
                    if content_length:
                        try:
                            file_size = int(content_length)
                        except (TypeError, ValueError):
                            pass

                    if file_size > self.config.MAX_FILE_SIZE:
                        return url, 'too_big', None

                    downloaded = 0
                    chunks = []
                    async for chunk in response.content.iter_chunked(1024 * 512):
                        if not chunk:
                            raise IncompleteDownloadError("Получен пустой чанк")
                        chunks.append(chunk)
                        downloaded += len(chunk)

                        if downloaded > self.config.MAX_FILE_SIZE:
                            return url, 'too_big', None

                    data = b''.join(chunks)

                    if content_length and downloaded != int(content_length):
                        raise IncompleteDownloadError(
                            f"Ожидалось {content_length} байт, получено {downloaded}"
                        )

                    if not data:
                        raise IncompleteDownloadError("Получены пустые данные")

                    try:
                        img = Image.open(BytesIO(data))
                        width, height = img.size

                        # Отсеиваем слишком маленькие изображения
                        if width < self.config.MIN_WIDTH or height < self.config.MIN_HEIGHT:
                            return None, None, None

                        img.verify()
                    except Exception as e:
                        raise IncompleteDownloadError(f"Проверка изображения не удалась: {str(e)}")

                    extension = await self.get_actual_extension(data, url)
                    return url, extension, data

            except IncompleteDownloadError as e:
                if attempt == self.config.MAX_RETRIES - 1:
                    return None, None, None
                await asyncio.sleep(1)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == self.config.MAX_RETRIES - 1:
                    return None, None, None
                await asyncio.sleep(1)

            except Exception as e:
                if attempt == self.config.MAX_RETRIES - 1:
                    return None, None, None
                await asyncio.sleep(1)

        return None, None, None

    async def check_image(self, url: str, user_info: str) -> Tuple[Optional[str], Optional[str]]:
        for attempt in range(self.config.MAX_RETRIES):
            try:
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

                img_url, extension, _ = await self.download_and_verify(final_url, user_info)
                if not img_url or not extension:
                    continue
                return img_url, extension

            except Exception as e:
                if attempt < self.config.MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                else:
                    return None, None

        return None, None