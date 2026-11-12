#!/usr/bin/env python3
"""Fetch your recent claude.ai conversations to markdown, fast.

Auth comes from your own logged-in Chrome on Linux: the claude.ai cookies are
decrypted from Chrome's cookie store in memory (key from the GNOME keyring) and
never written to disk. Requests go through curl_cffi with a Chrome TLS
fingerprint, which is what gets past Cloudflare.

The conversation list is ordered newest-update first, so paging stops at the
first conversation older than the cutoff -- "today" is usually one request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from curl_cffi import requests as cr

API = "https://claude.ai/api"
CHROME_DIR = Path.home() / ".config/google-chrome"
MAX_BLOCK = 24000  # chars of any single tool call/result kept verbatim

# ------------------------------------------------------------------ auth


def keyring_secret() -> bytes:
    for app in ("chrome", "chromium"):
        try:
            out = subprocess.run(["secret-tool", "lookup", "application", app],
                                 capture_output=True, timeout=20, check=False)
        except FileNotFoundError:
            sys.exit("secret-tool not found (install libsecret)")
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    sys.exit("no Chrome Safe Storage secret in the keyring (is the keyring unlocked?)")


def decrypt(enc: bytes, secret: bytes, host: str) -> str:
    from Crypto.Cipher import AES

    if enc[:3] in (b"v10", b"v11"):
        enc = enc[3:]
    key = hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1, 16)
    raw = AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(enc)
    pad = raw[-1]
    raw = raw[:-pad] if 0 < pad <= 16 else raw
    # Chrome 130+ prefixes SHA256(host_key) to the plaintext
    if raw[:32] == hashlib.sha256(host.encode()).digest():
        raw = raw[32:]
    return raw.decode("utf-8", "replace")


def chrome_cookie(profile: str) -> str:
    src = CHROME_DIR / profile / "Cookies"
    if not src.exists():
        sys.exit(f"no Chrome cookie store at {src}")
    tmp = Path(tempfile.mkdtemp(prefix="ccf-"))
    try:
        # backup() gives a consistent snapshot while Chrome holds the DB open
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        d = sqlite3.connect(tmp / "Cookies")
        with d:
            s.backup(d)
        s.close()
        rows = d.execute("select host_key, name, encrypted_value from cookies "
                         "where host_key like '%claude.ai'").fetchall()
        d.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    secret, seen, parts = keyring_secret(), set(), []
    for host, name, enc in rows:
        try:
            val = decrypt(enc, secret, host)
        except Exception:  # noqa: BLE001
            continue
        if name in seen or not val.isascii():
            continue
        seen.add(name)
        parts.append(f"{name}={val}")
    if not any(p.startswith("sessionKey=") for p in parts):
        sys.exit("no claude.ai sessionKey in Chrome -- log in to claude.ai in Chrome first")
    return "; ".join(parts)


# ------------------------------------------------------------------ http

_local = threading.local()


class Client:
    def __init__(self, cookie: str):
        self.cookie = cookie

    def get(self, path: str, tries: int = 4):
        s = getattr(_local, "s", None)
        if s is None:  # curl_cffi sessions are not shared across threads
            s = _local.s = cr.Session(impersonate="chrome", headers={
                "cookie": self.cookie, "referer": "https://claude.ai/recents", "accept": "*/*"})
        r = None
        for i in range(tries):
            try:
                r = s.get(f"{API}{path}", timeout=60)
            except Exception:  # noqa: BLE001
                if i == tries - 1:
                    raise
                time.sleep(2 ** i)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** i * 2)
                continue
            return r
        return r

    def json(self, path: str):
        r = self.get(path)
        if r is None or r.status_code != 200:
            code = getattr(r, "status_code", "no response")
            hint = " (cookie expired? reload claude.ai in Chrome)" if code in (401, 403) else ""
            raise SystemExit(f"GET {path.split('?')[0]} -> HTTP {code}{hint}")
        return r.json()


def pick_org(c: Client, want: str | None) -> str:
    orgs = c.json("/organizations")
    if want:
        return want
    chat = [o for o in orgs if "chat" in (o.get("capabilities") or [])] or orgs
    if not chat:
        sys.exit("no organizations on this account")
    return chat[0]["uuid"]


# ------------------------------------------------------------------ render


def slugify(s: str, n: int = 70) -> str:
    s = re.sub(r"[^\w\s.-]", "", s or "untitled").strip()
    s = re.sub(r"[\s_]+", "-", s)
    return (s[:n].strip("-.") or "untitled").lower()


def block_text(b) -> str:
    if isinstance(b, str):
        return b
    if isinstance(b, dict):
        if isinstance(b.get("text"), str):
            return b["text"]
        c = b.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(block_text(x) for x in c)
        if isinstance(c, dict):
            return block_text(c)
    return json.dumps(b, ensure_ascii=False)


def clip(s: str) -> str:
    return s if len(s) <= MAX_BLOCK else s[:MAX_BLOCK] + f"\n… [{len(s) - MAX_BLOCK} chars truncated]"


def render(conv: dict) -> str:
    def ts(v):
        return (v or "")[:19].replace("T", " ")

    title = conv.get("name") or "Untitled"
    out = ["---", f"title: {json.dumps(title, ensure_ascii=False)}", f"uuid: {conv.get('uuid')}",
           f"created: {ts(conv.get('created_at'))}", f"updated: {ts(conv.get('updated_at'))}",
           f"model: {conv.get('model')}", f"messages: {len(conv.get('chat_messages') or [])}",
           f"source: https://claude.ai/chat/{conv.get('uuid')}", "---", "", f"# {title}", ""]
    for m in conv.get("chat_messages") or []:
        who = "Human" if m.get("sender") == "human" else "Assistant"
        out += [f"## {who} · {ts(m.get('created_at'))}", ""]
        for a in m.get("attachments") or []:
            out += [f"> attachment: `{a.get('file_name')}` ({a.get('file_type')}, {a.get('file_size')}B)", ""]
        for f in m.get("files") or []:
            out += [f"> file: `{f.get('file_name')}` ({f.get('file_kind')})", ""]
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or m.get("text") or ""}]
        for b in blocks:
            t = b.get("type") if isinstance(b, dict) else "text"
            if t == "text":
                out.append(block_text(b))
            elif t == "thinking":
                think = (b.get("thinking") or "").strip()
                if think:
                    out.append(f"<details><summary>thinking</summary>\n\n{think}\n\n</details>")
            elif t == "tool_use":
                inp = json.dumps(b.get("input") or {}, indent=1, ensure_ascii=False)
                out.append(f"<details><summary>tool call: <code>{b.get('name')}</code></summary>\n\n"
                           f"```json\n{clip(inp)}\n```\n\n</details>")
            elif t == "tool_result":
                out.append(f"<details><summary>tool result</summary>\n\n```\n{clip(block_text(b))}\n```\n\n</details>")
            elif t == "image":
                out.append("*[inline image]*")
            else:
                out.append(f"```json\n{json.dumps(b, ensure_ascii=False)[:4000]}\n```")
        out.append("")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(prog="claude-chat-fetch", description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=1, help="1 = today only (local time), 3 = today + previous 2 days")
    ap.add_argument("-o", "--out", default="claude-chats", help="output root (default ./claude-chats)")
    ap.add_argument("--org", help="organization uuid (default: your first chat org)")
    ap.add_argument("--profile", default="Default", help="Chrome profile dir (default: Default)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-raw", action="store_true", help="skip saving raw API JSON")
    args = ap.parse_args()

    t0 = time.time()
    start = (datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=args.days - 1))
    c = Client(chrome_cookie(args.profile))
    org = pick_org(c, args.org)

    picked, offset = [], 0
    while True:
        data = c.json(f"/organizations/{org}/chat_conversations_v2?limit=100&offset={offset}").get("data") or []
        stop = False
        for row in data:
            if datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")) < start:
                stop = True
                break
            picked.append(row["uuid"])
        if stop or len(data) < 100:
            break
        offset += 100

    day = Path(args.out) / start.date().isoformat()
    conv_dir, raw_dir = day / "conversations", day / "raw"
    if conv_dir.exists():
        shutil.rmtree(conv_dir)  # titles change; rebuild this run's set
    conv_dir.mkdir(parents=True)
    if not args.no_raw:
        raw_dir.mkdir(exist_ok=True)

    def one(uuid: str) -> dict:
        try:
            conv = c.json(f"/organizations/{org}/chat_conversations/{uuid}"
                          "?tree=True&rendering_mode=messages&render_all_tools=true")
        except SystemExit as exc:
            return {"uuid": uuid, "error": str(exc)}
        if not args.no_raw:
            (raw_dir / f"{uuid}.json").write_text(json.dumps(conv, ensure_ascii=False))
        name = f"{(conv.get('created_at') or '')[:10]}-{slugify(conv.get('name') or uuid)}-{uuid[:8]}.md"
        (conv_dir / name).write_text(render(conv))
        return {"uuid": uuid, "md": name, "title": conv.get("name") or "Untitled",
                "updated": conv.get("updated_at"), "messages": len(conv.get("chat_messages") or [])}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, picked))

    ok = sorted((r for r in results if "md" in r), key=lambda r: r["updated"] or "", reverse=True)
    lines = [f"# claude.ai conversations since {start.date()}", "", "| updated | title | msgs |", "|---|---|---|"]
    lines += [f"| {(r['updated'] or '')[:16].replace('T', ' ')} | [{r['title'].replace('|', '/')}]"
              f"(conversations/{r['md']}) | {r['messages']} |" for r in ok]
    (day / "index.md").write_text("\n".join(lines) + "\n")
    (day / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ok))

    fails = [r for r in results if "error" in r]
    for r in fails:
        print(f"FAIL {r['uuid']}: {r['error']}", file=sys.stderr)
    print(f"{len(ok)} conversations since {start.date()} -> {day} "
          f"({len(fails)} failed, {time.time() - t0:.1f}s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
