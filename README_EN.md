# QQOps

**Self-hosted QQ group message collector, summarizer, AI query console and ops dashboard.**
Pure-stdlib Python — no pip dependencies, no frontend build step.

[中文说明](README.md) · **Docs site: <https://mayuxiao2021.github.io/qqops/>** · [Quickstart](docs/guide/quickstart.md) · [Deployment](docs/DEPLOY.md) · [Architecture](docs/ARCHITECTURE.md) · [Security](SECURITY.md)

## What it does

QQOps sits behind [NapCat](https://github.com/NapNeko/NapCatQQ) (OneBot v11) and turns group chatter into
something you can actually use:

- **Collect** every message into a local SQLite DB (dedup, recall marks, clock-skew guard, HMAC option).
- **Summarize** on a schedule: events, todos, links, per-group rolling memory, profile-aware relevance —
  reports as Markdown / self-contained HTML / XLSX / DOCX, optionally pushed to your own QQ.
- **Ask** questions in natural language ("who mentioned the report yesterday?"). Answers must cite sources
  like `[M1]`, and every citation opens the original message.
- **Chat** with it directly in QQ: private messages are answered by default (allow-listed users only),
  group Q&A is opt-in behind a trigger prefix.
- **Operate** it from a mobile-first console: service health, crash ledger, live logs, resource/power
  profiles, one-click self-test.

Everything stays on your device. Only the records you allow are ever sent to your configured model endpoint.

## Highlights

| Area | What you get |
|---|---|
| Runtime | One process for all services (saves ~74% RAM on Android) or four isolated processes; `performance` / `balanced` / `saver` power profiles |
| Fault tolerance | Crash ledger, five isolation layers, exponential backoff + circuit breaker, port pre-check and reclaim, real health checks |
| Plugin system | AstrBot-aligned layout: `@register / @route / @command / @task / @on_event / @on_stage`, hot reload, console pages from plugins |
| Console | 15 pages, zero build: dashboard, chat, messages (FTS5 highlight), reports, tasks, plugins, data, ops, settings, help |
| Dependencies | Standard library only (SQLite+FTS5, `http.server`, `urllib`, `threading`); optional `pypdf` for PDF |

## Quick start

Termux / Linux — copy-paste all four lines (the URL is fixed, no version number to fill in):

```bash
pkg update && pkg install -y python curl      # Termux; Linux usually ships python3 already
curl -L -o ~/qqops.tar.gz https://github.com/mayuxiao2021/qqops/releases/latest/download/qqops-latest.tar.gz
rm -rf ~/qqops-src && mkdir -p ~/qqops-src && tar xzf ~/qqops.tar.gz -C ~/qqops-src --strip-components=1
bash ~/qqops-src/install.sh
```

The installer copies to `~/qqops`, generates config and a random token, starts the services and
**prints the console URL (token included)**. Then:

```bash
qqops url             # print the console URL again
qqops doctor          # verify collector + NapCat API + storage + model
qqops status          # services, ports, message counts, schedule
```

Windows needs no bash: `python -m qqops.tools init` then `python -m qqops.svc start`
(see [docs/DEPLOY.md](docs/DEPLOY.md#3-windows)).

Wire NapCat once (`httpClients` → `http://127.0.0.1:8080/`, `httpServers` → port 3000) —
see [docs/DEPLOY.md](docs/DEPLOY.md).

## Development

```bash
python3 tests/smoke.py            # smoke tests (no network needed)
node tools/console_probe.js       # render every console page in a stub DOM
python3 tools/check_secrets.py    # privacy/secret scan (required before commit)
python3 pack.py                   # build a versioned tarball into dist/
python3 tools/verify_package.py   # assert package == worktree == commit
```

## License

[AGPL-3.0](LICENSE) © QQOps contributors. Not affiliated with Tencent.
You are responsible for complying with QQ/NapCat/model-provider terms and with your group's rules.
