"""Unicode grapheme boundaries from the existing, explicitly pinned regex package."""
from collections.abc import Iterator

import regex

_CLUSTERS = regex.compile(r'\X')
_DEFAULT_EMOJI = regex.compile(r'\p{Emoji_Presentation}')
_EMOJI = regex.compile(r'\p{Emoji}')
_KEYCAP = regex.compile(r'^[0-9#*]\uFE0F?\u20E3$')


def clusters(text: str) -> Iterator[str]:
    return (match.group() for match in _CLUSTERS.finditer(text))


def is_emoji(cluster: str) -> bool:
    # Explicit text presentation and plain text-default symbols stay text.
    if '\ufe0e' in cluster:
        return False
    return bool(_DEFAULT_EMOJI.search(cluster) or _KEYCAP.fullmatch(cluster)
                or ('\ufe0f' in cluster and _EMOJI.search(cluster)))


def limit_emoji_clusters(text: str, maximum: object) -> str:
    if type(maximum) is not int or maximum < 0:
        raise ValueError('Invalid emoji limit')
    seen = 0
    kept: list[str] = []
    for cluster in clusters(text):
        if is_emoji(cluster):
            seen += 1
            if seen > maximum:
                continue
        kept.append(cluster)
    return ''.join(kept)


def truncate_clusters(text: str, max_characters: int, max_bytes: int | None = None, marker: str = '...') -> str:
    """Preserve existing codepoint/UTF-8 budgets, removing only whole clusters."""
    if max_characters < 0 or (max_bytes is not None and max_bytes < 0):
        raise ValueError('Invalid text budget')
    if len(text) <= max_characters and (max_bytes is None or len(text.encode('utf-8')) <= max_bytes):
        return text
    if len(marker) > max_characters or (max_bytes is not None and len(marker.encode('utf-8')) > max_bytes):
        marker = ''
    chars_left = max_characters - len(marker)
    bytes_left = None if max_bytes is None else max_bytes - len(marker.encode('utf-8'))
    kept: list[str] = []
    for cluster in clusters(text):
        size = len(cluster.encode('utf-8'))
        if len(cluster) > chars_left or (bytes_left is not None and size > bytes_left):
            break
        kept.append(cluster)
        chars_left -= len(cluster)
        if bytes_left is not None:
            bytes_left -= size
    return ''.join(kept) + marker
