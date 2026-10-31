from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Post:
    channel_id: str = ""
    post_id: Optional[int] = None
    text: str = ""
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    url: Optional[str] = None
