"""网页正文提取：trafilatura 优先，BeautifulSoup 兜底。输出统一 WebDocument。"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MIN_TEXT_LEN = 30

# 兜底时剔除的明显非正文元素
_KILL_TAGS = [
    "script", "style", "noscript", "template", "nav", "footer", "header",
    "aside", "form", "iframe", "svg", "button", "select", "dialog",
]
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)


@dataclass
class WebDocument:
    url: str
    title: str
    text: str


class ExtractionError(Exception):
    """网页可访问但提取不到有效正文。"""


def extract(html: str, url: str) -> WebDocument:
    title = ""
    text = ""

    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True) or ""
        try:
            meta = trafilatura.extract_metadata(html)
            if meta and getattr(meta, "title", None):
                title = str(meta.title)
        except Exception:  # noqa: BLE001 —— 标题提取失败不影响正文，降级到 bs4
            logger.debug("trafilatura 标题提取失败")
    except ImportError:
        pass  # trafilatura 不可用时走 BeautifulSoup 兜底

    if len(text.strip()) < MIN_TEXT_LEN:
        bs_title, bs_text = _bs4_extract(html)
        title = title or bs_title
        text = bs_text if len(bs_text.strip()) >= len(text.strip()) else text

    text = text.strip()
    if len(text) < MIN_TEXT_LEN:
        raise ExtractionError()

    return WebDocument(url=url, title=title.strip(), text=text)


def _bs4_extract(html: str) -> tuple[str, str]:
    """过滤 script/style/nav/footer/header/广告/隐藏元素后取正文文本。"""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 —— lxml 解析失败时退回标准库解析器
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(_KILL_TAGS):
        tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    for tag in soup.find_all(style=_HIDDEN_STYLE):
        tag.decompose()
    for tag in soup.find_all(class_=re.compile(r"\b(ad|ads|advert|banner|menu|sidebar|share)\b", re.IGNORECASE)):
        tag.decompose()

    title = soup.title.get_text(strip=True) if soup.title else ""
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines = (ln.strip() for ln in root.get_text("\n").splitlines())
    text = "\n".join(ln for ln in lines if ln)
    return title, text
