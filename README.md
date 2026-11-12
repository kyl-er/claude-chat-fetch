# claude-chat-fetch

Fetch your recent claude.ai conversations to markdown in a few seconds, using the
session you're already logged into in Chrome. No browser automation, no API key.

```bash
uv tool install git+https://github.com/kyl-er/claude-chat-fetch
claude-chat-fetch              # today's conversations (local time)
claude-chat-fetch --days 3     # today + the previous 2 days
```

Output:

```
claude-chats/2026-11-12/
  index.md          table: updated · title · message count
  index.jsonl       one record per conversation
  conversations/    one .md per conversation (tool calls/results + thinking included)
  raw/              verbatim API JSON (skip with --no-raw)
```

Re-running the same day refetches everything in range, so a chat you continued
since the last run is never stale.

## How it works

1. **Cookie** — reads Chrome's cookie DB through SQLite's `backup()` API (safe
   while Chrome is running). Then it decrypts the claude.ai cookies with Chrome's
   key from the GNOME keyring (`secret-tool`). The cookie stays in memory and is
   never written to disk.
2. **Cloudflare** — a plain HTTP client gets a 403 even with a valid cookie. The
   check is on the TLS fingerprint, so requests go through
   [`curl_cffi`](https://github.com/lexiforest/curl_cffi) impersonating Chrome.
3. **Endpoints** (claude.ai's own web API, undocumented):
   - `GET /api/organizations` — find your org
   - `GET /api/organizations/{org}/chat_conversations_v2?limit=100&offset=N` — list, newest update first
   - `GET /api/organizations/{org}/chat_conversations/{uuid}?tree=True&rendering_mode=messages&render_all_tools=true`
4. Because the list is sorted by last update, paging stops at the first conversation
   older than the cutoff. "Today" is usually one list request plus one request per
   conversation, fetched 8 at a time.

## Options

| flag | default | |
|---|---|---|
| `--days N` | 1 | today plus the previous N-1 days |
| `-o, --out DIR` | `claude-chats` | output root |
| `--org UUID` | first chat org | pick a specific organization |
| `--profile NAME` | `Default` | Chrome profile directory |
| `--workers N` | 8 | parallel fetches |
| `--no-raw` | off | don't keep raw JSON |

## Requirements and caveats

- Linux, Google Chrome (or Chromium) logged into claude.ai, an unlocked GNOME
  keyring, and `secret-tool` (libsecret).
- It only reads your own account, with your own session. It is read-only and sends
  GET requests only.
- These are private endpoints, so they can change or break without notice.
- Attached files and artifacts are listed by name but not downloaded.

## License

MIT
