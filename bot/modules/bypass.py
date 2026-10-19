from html import escape
from io import BytesIO
from json import dumps
from re import match as re_match
from re import search as re_search
from urllib.parse import urlparse

from niquests import AsyncSession
from pyrogram.enums import ButtonStyle

from .. import LOGGER
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message

MAX_MSG = 3800
MAX_DEPTH = 3
MAX_LIST_ITEMS = 8
MAX_LINKS = 15
MAX_VALUE_LEN = 300
SKIP_KEYS = {"url", "headers", "credits", "cookies", "cookie"}
LINK_KEYS = (
    "direct_link",
    "direct_url",
    "download_url",
    "download_link",
    "download",
    "dl",
    "link",
    "file_url",
    "stream_url",
    "video_url",
)


def extract_url_from_text(text: str) -> str | None:
    if not text:
        return None
    match = re_search(r"https?://\S+", text)
    return match.group(0) if match else None


def is_url(v) -> bool:
    return isinstance(v, str) and re_match(r"https?://\S+$", v.strip()) is not None


def is_empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def is_container(v) -> bool:
    return isinstance(v, (dict, list))


def label_of(key) -> str:
    return escape(str(key).replace("_", " ").replace("-", " ").strip().title())


def short(text, limit: int = MAX_VALUE_LEN) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def anchor(url: str, text: str = "Link") -> str:
    return f'<a href="{escape(url.strip(), quote=True)}">{escape(text)}</a>'


def scalar_text(v) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return escape(short(v))


def walk_links(node, label="Link", found=None):
    if found is None:
        found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in SKIP_KEYS:
                continue
            walk_links(v, str(k), found)
    elif isinstance(node, list):
        for i, v in enumerate(node, 1):
            walk_links(v, f"{label} {i}", found)
    elif is_url(node):
        found.append((label, node.strip()))
    return found


def pick_primary_links(data: dict) -> list[tuple[str, str]]:
    all_links = walk_links(data)
    prioritized = [
        (lbl, u)
        for lbl, u in all_links
        if any(k in lbl.lower() for k in LINK_KEYS)
    ]
    chosen = prioritized or all_links
    seen, unique = set(), []
    for lbl, u in chosen:
        if u not in seen:
            seen.add(u)
            unique.append((lbl, u))
    return unique


def render(node, depth: int, shown: set) -> list[str]:
    pad = "  " * depth
    out = []

    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in SKIP_KEYS or is_empty(v):
                continue
            lbl = f"<b>{label_of(k)}:</b>"
            if is_url(v):
                if v.strip() not in shown:
                    out.append(f"{pad}{lbl} {anchor(v)}")
            elif is_container(v):
                if depth >= MAX_DEPTH:
                    raw = dumps(v, ensure_ascii=False, default=str)
                    out.append(f"{pad}{lbl} <code>{escape(short(raw, 150))}</code>")
                elif isinstance(v, list) and all(
                    not is_container(i) and not is_url(i) for i in v
                ):
                    items = [scalar_text(i) for i in v if not is_empty(i)]
                    more = len(items) - MAX_LIST_ITEMS
                    text = ", ".join(items[:MAX_LIST_ITEMS])
                    if more > 0:
                        text += f" … (+{more})"
                    out.append(f"{pad}{lbl} {text}")
                else:
                    sub = render(v, depth + 1, shown)
                    if sub:
                        out.append(f"{pad}{lbl}")
                        out.extend(sub)
            else:
                out.append(f"{pad}{lbl} {scalar_text(v)}")

    elif isinstance(node, list):
        items = [i for i in node if not is_empty(i)]
        for i, item in enumerate(items[:MAX_LIST_ITEMS], 1):
            if is_url(item):
                if item.strip() not in shown:
                    out.append(f"{pad}• {anchor(item)}")
            elif is_container(item):
                sub = render(item, depth + 1, shown)
                if sub:
                    out.append(f"{pad}<b>#{i}</b>")
                    out.extend(sub)
            else:
                out.append(f"{pad}• {scalar_text(item)}")
        if len(items) > MAX_LIST_ITEMS:
            out.append(f"{pad}<i>… +{len(items) - MAX_LIST_ITEMS} more</i>")

    return out


def format_bypass_result(data, url: str) -> tuple[str, bool]:
    if not isinstance(data, dict):
        data = {"result": data}

    links = pick_primary_links(data)
    shown = {u for _, u in links}

    head = ["<b>✺ Direct Link:</b>"]
    if not links:
        head.append("<code>No direct link found.</code>")
    elif len(links) == 1:
        head.append(anchor(links[0][1], "Click Here"))
    else:
        for i, (lbl, u) in enumerate(links[:MAX_LINKS], 1):
            head.append(f"{i}. {anchor(u, short(lbl.replace('_', ' ').title(), 40))}")
        if len(links) > MAX_LINKS:
            head.append(f"<i>… +{len(links) - MAX_LINKS} more</i>")

    footer = ["", "<b>✺ Original URL:</b>", f"<code>{escape(short(url, 500))}</code>"]
    details = render(data, 0, shown)

    truncated = False
    budget = MAX_MSG - len("\n".join(head + footer)) - 60
    body, used = [], 0
    for line in details:
        if used + len(line) + 1 > budget:
            truncated = True
            body.append("<i>… truncated (full response attached)</i>")
            break
        body.append(line)
        used += len(line) + 1

    lines = head + (["", *body] if body else []) + footer
    return "\n".join(lines), truncated


@new_task
async def bypass(_, message):
    url = None

    if len(message.command) >= 2:
        url = message.command[1].strip()
    elif message.reply_to_message:
        replied = message.reply_to_message
        url = extract_url_from_text(replied.text or replied.caption)

    if not url:
        return await send_message(
            message,
            "<b>Usage:</b> <code>/bypass https://example.com/file</code>\n\n"
            "Send a supported DDL URL after <code>/bypass</code> or <code>/b</code>, "
            "or reply to a message containing a link.",
        )

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return await send_message(
            message,
            "<b>Invalid URL.</b> Send a complete http(s) link.",
        )

    api_url = (
        Config.POSTER_API_URL
        or "https://thezakeapi-git-beta-ftsatyam.vercel.app"
    ).rstrip("/")
    token = (Config.POSTER_API_TOKEN or "thezake").strip()

    if not token:
        LOGGER.error("POSTER_API_TOKEN is not configured")
        return await send_message(
            message,
            "<b>Bypass service is not configured.</b> "
            "Please contact the bot owner.",
        )

    waiting = await send_message(
        message,
        f"<i>Resolving direct link for:</i>\n<code>{escape(url)}</code>",
    )

    raw_data = None
    truncated = False

    try:
        async with AsyncSession(timeout=30) as client:
            response = await client.get(
                f"{api_url}/bypass",
                params={"url": url},
                headers={"Authorization": f"Bearer {token}"},
            )

        if response.status_code == 401:
            text = (
                "<b>Error:</b> <code>Poster API authentication failed. "
                "Check POSTER_API_TOKEN.</code>"
            )
        elif response.status_code == 404:
            text = "<b>Error:</b> <code>Unsupported DDL host.</code>"
        elif response.status_code >= 400:
            text = f"<b>Error:</b> <code>Poster API error {response.status_code}</code>"
        else:
            raw_data = response.json()
            text, truncated = format_bypass_result(raw_data, url)

    except ValueError as error:
        LOGGER.error("Bypass API invalid JSON: %s", error)
        text = "<b>Error:</b> <code>API ne invalid response diya.</code>"
    except Exception:
        LOGGER.exception("Unexpected /bypass command failure")
        text = "<b>Error:</b> <code>Could not resolve the direct link right now.</code>"

    buttons = ButtonMaker()
    buttons.url_button("Developer", "https://t.me/TheZake", style=ButtonStyle.PRIMARY)
    buttons.url_button(
        "⭐ Source Code",
        "https://github.com/ImKrishana/Poster-Scraper-Bot",
        style=ButtonStyle.SUCCESS,
    )

    sent = None
    if waiting:
        try:
            sent = await waiting.edit(
                text=text,
                reply_markup=buttons.build_menu(2),
            )
        except Exception:
            sent = None

    if sent is None:
        sent = await send_message(
            message,
            text,
            reply_markup=buttons.build_menu(2),
        )

    if truncated and raw_data is not None:
        try:
            doc = BytesIO(
                dumps(raw_data, indent=2, ensure_ascii=False, default=str).encode()
            )
            doc.name = "bypass_response.json"
            await message.reply_document(document=doc, caption="Full API response")
        except Exception:
            LOGGER.exception("Failed to send full response file")

    return sent
