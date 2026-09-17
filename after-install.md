# Hermes Agent plugin installation message.
# Shown by `hermes plugins install` after a successful install.

## Job Reach installed ✓

Seven tools are now available: `job_search`, `job_ingest`, `job_list`,
`job_note`, `job_status`, `job_setup`, `job_cron`.

### 1. Enable the plugin

```bash
hermes plugins enable job-reach
```

### 2. One-time setup (needed for Wantedly and Mynavi)

```bash
hermes job-reach setup
```

This creates a dedicated venv under `~/.hermes/plugin-data/job-reach/venv`,
installs Playwright plus its Chromium build (~150 MB), and copies the skill
into `~/.hermes/skills/productivity/job-reach/` so it can auto-trigger.

You can also just ask the agent: *"run job_setup"*.

LinkedIn needs no setup beyond Chrome running with the
[OpenCLI](https://github.com/) extension; Indeed Japan needs no local install
at all (the agent fetches it with a real browser).

### 3. Check it

```bash
hermes job-reach doctor
```

### 4. Try it

Ask the agent *"find design jobs in Tokyo"*, or from the shell:

```bash
hermes job-reach search --keyword "frontend engineer" --source wantedly -n 10
```

### Optional: Obsidian notes

```bash
export OBSIDIAN_VAULT_PATH=~/Obsidian/MyVault   # add to ~/.hermes/.env
```

### Optional: daily monitoring

Ask the agent *"check for new design jobs every morning and message me on
Telegram"* — it will call `job_cron`. Monitoring only fires while the Hermes
gateway is running (`hermes gateway status`).
