# Hermes Agent plugin installation message.
# Shown by `hermes plugins install` after a successful install.

## Job Reach installed ✓

Seven tools are now available: `job_search`, `job_ingest`, `job_list`,
`job_note`, `job_status`, `job_setup`, `job_cron`.

### 1. Enable the plugin

```bash
hermes plugins enable job-reach
```

### 2. One-time setup (usually downloads nothing)

```bash
hermes job-reach setup
```

If [Scrapling](https://github.com/D4Vinci/Scrapling) is installed anywhere the
plugin can find it (a `scrapling` binary on PATH, the plugin venv, or a known
local install), that install is adopted as-is: it solves Cloudflare challenges,
which is what makes Indeed Japan scrapable. Nothing is downloaded.

Only when Scrapling is absent does setup build the plugin's own venv under
`~/.hermes/plugin-data/job-reach/venv` with Playwright plus Chromium (~150 MB)
as a fallback. Either way it copies the skill into
`~/.hermes/skills/productivity/job-reach/` so it can auto-trigger.

You can also just ask the agent: *"run job_setup"*.

Wantedly needs no setup at all — it is read over its JSON API. LinkedIn needs
Chrome running with the [OpenCLI](https://github.com/) extension.

### 3. Check it

```bash
hermes job-reach doctor
```

Prints the active browser backend, the engine interpreter, and which boards can
actually run.

### 4. Try it

Ask the agent *"find design jobs in Tokyo"*, or from the shell:

```bash
hermes job-reach search --keyword "frontend engineer" --source wantedly -n 10
hermes job-reach search --keyword "データサイエンティスト" --source wantedly,indeed -n 10
```

### Optional: Obsidian notes

```bash
export OBSIDIAN_VAULT_PATH=~/Obsidian/MyVault   # add to ~/.hermes/.env
```

### Optional: daily monitoring

Ask the agent *"check for new design jobs every morning and message me on
Telegram"* — it will call `job_cron`. Monitoring only fires while the Hermes
gateway is running (`hermes gateway status`).
