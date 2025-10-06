import random
from typing import List, Optional

import aiohttp

from image_source import ImageSource


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