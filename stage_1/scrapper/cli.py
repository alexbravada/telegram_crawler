import argparse
import asyncio
import logging
import random
import re
import sys
from typing import Optional

import httpx

from . import messages
from .config import Config, load_config
from .models import Post
from .providers.base import ChannelProvider
from .providers.web import WebProvider
from .errors import ChannelError

logger = logging.getLogger("scrapper")


def extract_channel(source: str) -> str:
    """Accepts https://t.me/<username> or a plain <username>."""
    source = source.strip()
    match = re.match(r"^(?:https?://)?t\.me/(@?[A-Za-z0-9_]+?)/?$", source)
    if match:
        return match.group(1).lstrip("@")
    if re.fullmatch(r"@?[A-Za-z0-9_]{4,}", source):
        return source.lstrip("@")
    raise ValueError(messages.invalid_link(source))


def format_post(post: Post, index: int) -> str:
    lines = [f"=== Post {index} ==="]
    if post.channel_id and post.post_id is not None:
        lines.append(f"[{post.channel_id}/{post.post_id}]")
    elif post.url:
        lines.append(f"[{post.url}]")
    if post.text:
        lines.append(post.text)
    if post.images:
        lines.append("Images:")
        lines.extend(f"  {url}" for url in post.images)
    if post.videos:
        lines.append("Videos:")
        lines.extend(f"  {url}" for url in post.videos)
    if len(lines) == 1:
        lines.append("(post has no text or media)")
    return "\n".join(lines)


async def crawl(
    provider: ChannelProvider,
    channels: list,
    limit: int,
    config: Config,
) -> dict:
    """Fetches channels in parallel: Semaphore(MAX_WORKERS) + pause between requests."""
    semaphore = asyncio.Semaphore(config.max_workers)

    async def worker(channel: str):
        async with semaphore:
            # pause between worker requests to avoid hammering t.me
            delay = random.uniform(config.min_timeout, config.max_timeout)
            await asyncio.sleep(delay)
            try:
                return channel, await provider.fetch_posts(channel, limit)
            except ChannelError as exc:
                logger.warning(messages.channel_skipped(channel, exc.message))
                return channel, []

    results = await asyncio.gather(*(worker(c) for c in channels))
    return dict(results)


async def run(provider: Optional[ChannelProvider] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scrapper",
        description="Prints the latest posts of public Telegram channels: "
        "text, image and video links.",
    )
    parser.add_argument(
        "channels",
        nargs="+",
        help="channel links, e.g. https://t.me/durov [https://t.me/telegram]",
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        default=1,
        help="how many posts to show per channel (default: 1)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging (shows retry attempts)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        channels = [extract_channel(source) for source in args.channels]
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    config = load_config()
    provider = provider or WebProvider()
    posts_by_channel = await crawl(provider, channels, max(args.number, 1), config)

    if not any(posts_by_channel.values()):
        logger.error(messages.no_posts())
        return 1

    for channel, posts in posts_by_channel.items():
        print(f"### {channel}")
        #for index, post in enumerate(reversed(posts), start=1):
        for index, post in enumerate(posts, start=1):
            print(format_post(post, index))
            print()
    return 0


def main(provider: Optional[ChannelProvider] = None) -> int:
    return asyncio.run(run(provider))


if __name__ == "__main__":
    sys.exit(main())
