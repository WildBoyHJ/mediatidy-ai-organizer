"""文件名解析与名称规范化；不依赖宿主 SDK，便于单独测试。"""
from __future__ import annotations

import re
import unicodedata

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".rmvb", ".webm", ".mpg", ".mpeg", ".iso", ".m4v", ".strm",
}

# 出现在标题之后的画质/来源/编码等标记，命中即认为标题结束。
QUALITY_TOKENS = {
    "2160p", "1080p", "1080i", "720p", "576p", "480p", "4k", "8k", "uhd",
    "bluray", "blu-ray", "bdrip", "brrip", "bdremux", "remux", "webrip",
    "web-dl", "webdl", "web", "hdtv", "dvdrip", "dvd", "hdrip", "hdcam",
    "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "xvid", "divx",
    "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "flac", "mp3", "opus",
    "hdr", "hdr10", "hdr10+", "dovi", "dv", "sdr", "10bit", "8bit",
    "proper", "repack", "rerip", "internal", "limited", "extended",
    "unrated", "remastered", "directors", "imax", "criterion", "complete",
    "multi", "dual", "subs", "sub", "chs", "cht", "eng", "jpn", "kor",
    "v2", "v3", "fin", "uncensored", "bd", "tv", "web",
    "中字", "双语", "国英", "简繁", "内封", "内嵌", "字幕", "国语", "粤语", "日语",
}

_SEASON_EPISODE = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})"),
    re.compile(r"(?<!\d)(\d{1,2})[xX](\d{1,3})(?!\d)"),
    re.compile(r"第\s*(\d{1,2})\s*季[^\d]{0,4}(\d{1,3})\s*[集话話]"),
    re.compile(r"[Ss]eason\s*(\d{1,2})[^\d]{0,6}[Ee]pisode\s*(\d{1,3})", re.IGNORECASE),
]
_SEASON_ONLY = [
    # S01 后面紧跟 .2025 / 空格年份时，那是季号而不是年份；(?!\d) 已排除 S01E01
    re.compile(r"(?<![A-Za-z])[Ss](\d{1,2})(?![\dEe])"),
    re.compile(r"第\s*(\d{1,2})\s*季"),
    re.compile(r"[Ss]eason\s*(\d{1,2})", re.IGNORECASE),
]
_EPISODE_ONLY = [
    # 番剧绝对集号： "Frieren - 12" / "Frieren - 12v2"
    re.compile(r"\s-\s*(\d{1,4})(?:v\d)?(?!\d)"),
    re.compile(r"(?<![A-Za-z])[Ee][Pp]?\s*(\d{1,3})(?!\d)"),
    re.compile(r"第\s*(\d{1,3})\s*[集话話]"),
]
# 没有季集标记、但以 " - 12" 结尾的独立数字，视为番剧集号
_ANIME_TAIL = re.compile(r"\s-\s*(\d{1,4})(?:v\d)?\s*$")
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
# 目录名里的显式 TMDB ID，如 {tmdb-1291658} / [tmdbid-12345]
_TMDB_ID = re.compile(r"tmdb[-_ ]?(?:id)?[-_ ]?(\d{2,9})", re.IGNORECASE)
_BRACKETS = re.compile(r"[\[【（(]([^\]】）)]*)[\]】）)]")
_LEADING_GROUP = re.compile(r"^\s*[\[【][^\]】]{1,40}[\]】]\s*")
_RESOLUTION = re.compile(r"^(2160|1080|720|576|480)[pi]$", re.IGNORECASE)


def stem(name: str) -> str:
    """去掉扩展名。"""
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


def normalize(text: str) -> str:
    """用于比对的规范化：NFKC、小写、去掉标点与空白。"""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def tmdb_id_from_path(*parts: str) -> int:
    """从路径片段里找显式标注的 TMDB ID；找不到返回 0。"""
    for part in parts:
        if not part:
            continue
        match = _TMDB_ID.search(str(part))
        if match:
            value = int(match.group(1))
            if 1 <= value <= 99_999_999:
                return value
    return 0


def is_video(name: str) -> bool:
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in VIDEO_EXT


def _find_year(text: str) -> int:
    for match in _YEAR.finditer(text):
        year = int(match.group(1))
        if 1900 <= year <= 2100:
            return year
    return 0


def _cut_title(text: str) -> str:
    """截断到第一个画质/来源/编码标记或四位数年份，保留原有分隔符。"""
    separator = re.compile(r"[.\s_\-]+")
    position = 0
    end = len(text)
    while position < len(text):
        match = separator.match(text, position)
        if match:
            position = match.end()
            continue
        following = separator.search(text, position)
        stop = following.start() if following else len(text)
        token = text[position:stop]
        bare = token.strip().lower()
        if bare in QUALITY_TOKENS or _RESOLUTION.match(bare) or _YEAR.fullmatch(bare):
            end = position
            break
        position = stop
    return text[:end].strip(" ._-·")


def parse(name: str) -> dict:
    """从文件名解析出 title/year/mediaType/season/episode，纯本地规则。"""
    base = stem(name)
    # 开头的 [字幕组] / 【字幕组】 是发布组标记，不属于标题
    working = _LEADING_GROUP.sub("", base)
    working = _BRACKETS.sub(lambda m: " " + m.group(1) + " ", working)
    working = working.replace("_", " ").replace(".", " ")

    season = episode = 0
    anime = False
    cut_at = -1
    for pattern in _SEASON_EPISODE:
        match = pattern.search(working)
        if match:
            season, episode = int(match.group(1)), int(match.group(2))
            working = working[: match.start()] + " " + working[match.end():]
            break
    if not season:
        for pattern in _SEASON_ONLY:
            match = pattern.search(working)
            if match:
                season = int(match.group(1))
                working = working[: match.start()] + " " + working[match.end():]
                break
    if not episode:
        tail = _ANIME_TAIL.search(working)
        if tail and 0 < int(tail.group(1)) <= 999:
            episode = int(tail.group(1))
            anime = True
            cut_at = tail.start()
            working = working[: tail.start()]
    if not episode:
        for pattern in _EPISODE_ONLY:
            match = pattern.search(working)
            if match:
                candidate = int(match.group(1))
                if 0 < candidate <= 999:
                    episode = candidate
                    if match.group(0).lstrip().startswith("-"):
                        # "Frieren - 12"：标题在集号之前，后面的发布组标签不算标题
                        anime = True
                        cut_at = match.start()
                    working = working[: match.start()] + " " + working[match.end():]
                    break

    year = _find_year(working if cut_at < 0 else working[:cut_at])
    title = _cut_title(working if cut_at < 0 else working[:cut_at])
    title = re.sub(r"\s{2,}", " ", title).strip(" .-_·")
    media_type = "tv" if (season or episode) else "movie"
    if not season and media_type == "tv":
        season = 1
    return {
        "title": title,
        "year": year,
        "mediaType": media_type,
        "season": season,
        "episode": episode,
        "anime": anime,
    }
