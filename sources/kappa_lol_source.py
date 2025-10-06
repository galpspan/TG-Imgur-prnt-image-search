import asyncio
import random
import string
from typing import List, Tuple, Optional

import aiohttp

from image_source import ImageSource
from config import Config

class KappaLolSource(ImageSource):
    def __init__(self, bot):
        super().__init__(bot)
        self.config = Config()

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

            timeout = aiohttp.ClientTimeout(total=30, sock_connect=15)

            async with session.head(url, headers=headers,
                                    allow_redirects=True,
                                    timeout=timeout) as response:

                if response.status == 404:
                    return None, None

                if response.status != 200:
                    await self.bot.handle_source_error('kappa',
                                                       Exception(f"HTTP status {response.status}"), url, user_info)
                    return None, None

                content_type = response.headers.get("content-type", "").lower()
                supported_types = ['image', 'video', 'audio', 'application', 'text']

                if not any(x in content_type for x in supported_types) or 'wav' in content_type:
                    return None, None

                final_url = str(response.url)

                file_size = 0
                content_length = response.headers.get('Content-Length')
                if content_length:
                    try:
                        file_size = int(content_length)
                    except (TypeError, ValueError):
                        pass

                if file_size > self.config.MAX_FILE_SIZE:
                    return final_url, 'too_big'

                img_url, extension, _ = await self.download_and_verify(final_url, user_info)
                if not img_url or not extension:
                    return None, None

                if 'image' in content_type:
                    return img_url, extension

                if 'gif' in content_type:
                    return final_url, 'gif'
                elif 'mp4' in content_type or 'webm' in content_type:
                    return final_url, 'video'
                elif 'octet-stream' in content_type or \
                        'application' in content_type or \
                        'text' in content_type:
                    return final_url, 'document'
                else:
                    return final_url, 'file'

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            await self.bot.handle_source_error('kappa', e, url, user_info)
            return None, None
        except Exception as e:
            await self.bot.handle_source_error('kappa', e, url, user_info)
            return None, None