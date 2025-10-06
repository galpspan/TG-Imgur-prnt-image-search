import random
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

from image_source import ImageSource


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