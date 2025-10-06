import asyncio
import random
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

from image_source import ImageSource
from config import Config

class PrntSource(ImageSource):
    def __init__(self, bot):
        super().__init__(bot)
        self.config = Config()

    async def generate_urls(self, batch_size: int) -> List[str]:
        return [f"https://prnt.sc/{self.bot.generate_random_string(6)}"
                for _ in range(batch_size)]

    async def extract_image_url(self, url: str) -> Optional[str]:
        for attempt in range(self.config.MAX_RETRIES):
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

                if any(x in img_url.lower() for x in
                       ["prnt.sc/placeholder", "st.prntscr.com", "prntscr.com/placeholder"]):
                    return None

                if "removed.png" in img_url.lower():
                    return None

                return img_url

            except Exception as e:
                if attempt < self.config.MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                else:
                    await self.bot.handle_source_error('prnt', e, url, "System")
        return None