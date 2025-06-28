import sys
import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from rich.console import Console
from rich.text import Text
import questionary

BASE_URL = "https://a.4cdn.org"
IMAGE_URL = "https://i.4cdn.org"
COLORS = [
    "red", "green", "yellow", "blue", "magenta", "cyan",
    "bright_red", "bright_green", "bright_yellow", "bright_blue", "bright_magenta", "bright_cyan"
]
console = Console()

if sys.platform == 'win32':
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

def get_boards_by_category(category="all"):
    resp = requests.get(f"{BASE_URL}/boards.json")
    boards = resp.json()["boards"]
    if category == "sfw":
        return [b["board"] for b in boards if b["ws_board"] == 1]
    elif category == "nsfw":
        return [b["board"] for b in boards if b["ws_board"] == 0]
    else:
        return [b["board"] for b in boards]

def get_threads_with_titles(board):
    url = f"{BASE_URL}/{board}/catalog.json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        threads = []
        for page in resp.json():
            for thread in page["threads"]:
                threads.append({
                    "thread_id": thread["no"],
                    "subject": thread.get("sub", ""),
                    "comment": thread.get("com", "")
                })
        return threads
    except Exception:
        return []

def parse_keywords_from_regex(regex):
    pattern = r"^(?:\b\w+\b\|?)+$"
    if re.fullmatch(pattern, regex.replace("(", "").replace(")", "")):
        keywords = regex.replace("(", "").replace(")", "").split("|")
        return [k for k in keywords if k]
    else:
        return []

def highlight_keywords(text, keywords):
    rich_text = Text(text)
    for idx, kw in enumerate(keywords):
        color = COLORS[idx % len(COLORS)]
        matches = list(re.finditer(re.escape(kw), text, re.IGNORECASE))
        for match in matches:
            rich_text.stylize(color, match.start(), match.end())
    return rich_text

def download_media(media_url, save_dir):
    os.makedirs(save_dir, exist_ok=True)
    filename = os.path.join(save_dir, media_url.split("/")[-1])
    if os.path.exists(filename):
        return filename
    try:
        resp = requests.get(media_url, timeout=10)
        if resp.status_code == 200:
            with open(filename, "wb") as f:
                f.write(resp.content)
            return filename
    except Exception:
        pass
    return None

def get_thread_posts(board, thread_id, query_regex, keywords, download, download_dir):
    url = f"{BASE_URL}/{board}/thread/{thread_id}.json"
    result = []
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        posts = resp.json()["posts"]
        for post in posts:
            text = (post.get("com") or "") + " " + (post.get("sub") or "")
            if query_regex.search(text):
                post_url = f"https://boards.4channel.org/{board}/thread/{thread_id}#p{post['no']}"
                media = None
                media_file = None
                if "tim" in post and "ext" in post:
                    media = f"{IMAGE_URL}/{board}/{post['tim']}{post['ext']}"
                    if download:
                        media_file = download_media(media, os.path.join(download_dir, board, str(thread_id)))
                snippet = text[:200]
                result.append({
                    "board": board,
                    "thread": thread_id,
                    "post": post["no"],
                    "url": post_url,
                    "media": media,
                    "media_file": media_file,
                    "snippet": snippet
                })
    except Exception:
        pass
    return result

def download_all_media_from_thread(board, thread_id, download_dir="media"):
    url = f"{BASE_URL}/{board}/thread/{thread_id}.json"
    results = []
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        posts = resp.json()["posts"]
        for post in posts:
            post_url = f"https://boards.4channel.org/{board}/thread/{thread_id}#p{post['no']}"
            media = None
            media_file = None
            if "tim" in post and "ext" in post:
                media = f"{IMAGE_URL}/{board}/{post['tim']}{post['ext']}"
                media_file = download_media(media, os.path.join(download_dir, board, str(thread_id)))
            snippet = (post.get("com") or "")[:200]
            results.append({
                "board": board,
                "thread": thread_id,
                "post": post["no"],
                "url": post_url,
                "media": media,
                "media_file": media_file,
                "snippet": snippet
            })
    except Exception:
        pass
    return results

def search_4chan_live(query_regex, boards, keywords, max_threads_per_board=10, max_workers=20, download=False, download_dir="media", thread_title=None, thread_downloads=None):
    results = []
    thread_jobs = []
    keyword_jobs = []
    for board in boards:
        thread_infos = get_threads_with_titles(board)
        # For each thread, create jobs for both: keyword search and possible thread title match
        for thread in thread_infos[:max_threads_per_board]:
            # Thread download (if subject or comment matches the query)
            if thread_title and (thread_title in thread["subject"].lower() or thread_title in thread["comment"].lower()):
                thread_jobs.append((board, thread["thread_id"]))
                if thread_downloads is not None:
                    thread_downloads.append((board, thread["thread_id"], thread["subject"]))
            else:
                # Only add to keyword jobs if not already scheduled for full thread download
                keyword_jobs.append((board, thread["thread_id"]))

    # Download matched threads in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 1. Download all media from matched threads by subject/title
        futures = []
        for board, thread_id in thread_jobs:
            futures.append(executor.submit(download_all_media_from_thread, board, thread_id, download_dir))
        for f in tqdm(as_completed(futures), total=len(futures), desc="Downloading matched threads"):
            results.extend(f.result())

        # 2. Search for keyword matches in the rest
        futures = []
        for board, thread_id in keyword_jobs:
            futures.append(executor.submit(get_thread_posts, board, thread_id, query_regex, keywords, download, download_dir))
        for f in tqdm(as_completed(futures), total=len(futures), desc="Searching keyword matches"):
            results.extend(f.result())

    return results

def main():
    console.print("[bold magenta]4chan OSINT Scraper (Parallel Keyword and Thread Download)[/bold magenta]\n")
    regex_str = questionary.text(
        "Enter search query (will be used as both a post keyword and a thread subject):"
    ).ask()
    try:
        query_regex = re.compile(regex_str, re.IGNORECASE)
    except re.error as e:
        console.print(f"[red]Invalid regex: {e}[/red]")
        return

    keywords = parse_keywords_from_regex(regex_str)
    if keywords:
        console.print(f"Keywords detected for coloring: {', '.join(keywords)}", style="bold")
    else:
        console.print("Complex regex detected, all matches will be highlighted in [bold yellow].", style="bold")

    board_type = questionary.select(
        "Board selection:",
        choices=[
            "All",
            "SFW only",
            "NSFW only",
            "Custom (comma-separated)"
        ]
    ).ask()

    if board_type == "All":
        boards = get_boards_by_category("all")
    elif board_type == "SFW only":
        boards = get_boards_by_category("sfw")
    elif board_type == "NSFW only":
        boards = get_boards_by_category("nsfw")
    else:
        boards = questionary.text(
            "Enter comma-separated board list (e.g., g,pol,x):"
        ).ask().strip().replace(" ", "").split(",")

    max_threads = questionary.text(
        "Max threads per board (default 10):", default="10"
    ).ask()
    max_threads = int(max_threads) if max_threads.isdigit() else 10

    max_workers = questionary.text(
        "Max parallel workers (default 20):", default="20"
    ).ask()
    max_workers = int(max_workers) if max_workers.isdigit() else 20

    download = questionary.confirm(
        "Download all matched media (images/webms)?"
    ).ask()
    download_dir = "media"

    # Lowercase query for thread title matching
    thread_title = regex_str.strip().lower()
    thread_downloads = []

    all_results = search_4chan_live(
        query_regex, boards, keywords, max_threads_per_board=max_threads,
        max_workers=max_workers, download=download, download_dir=download_dir,
        thread_title=thread_title, thread_downloads=thread_downloads
    )

    # User feedback on detected threads
    if thread_downloads:
        for board, thread_id, subject in thread_downloads:
            console.print(
                f"[yellow]Detected thread match on /{board}/: {subject or '(no subject)'} (thread {thread_id}) - Downloaded all media from thread[/yellow]"
            )

    console.print(f"\n[bold green]Found {len(all_results)} results:[/bold green]\n")
    for res in all_results:
        console.print(f"{res['url']}", style="bold underline")
        # Only highlight if this wasn't a thread download
        if (res.get('snippet') and keywords and not any(
            res['thread'] == tid and res['board'] == brd
            for brd, tid, _ in thread_downloads
        )):
            console.print(highlight_keywords(res['snippet'], keywords))
        elif res.get('snippet') and not any(
            res['thread'] == tid and res['board'] == brd
            for brd, tid, _ in thread_downloads
        ):
            snippet = res['snippet']
            for match in query_regex.finditer(snippet):
                snippet = snippet.replace(match.group(0), f"[yellow]{match.group(0)}[/yellow]")
            console.print(Text.from_markup(snippet))
        if res['media']:
            console.print(f"Media: [blue]{res['media']}[/blue]")
        if res.get("media_file"):
            console.print(f"Downloaded to: [green]{res['media_file']}[/green]")
        console.print("-" * 40, style="dim")

if __name__ == "__main__":
    main()