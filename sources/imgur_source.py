import random
from typing import List, Optional

import aiohttp

from image_source import ImageSource


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