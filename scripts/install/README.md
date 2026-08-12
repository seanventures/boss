# BOSS Community installer

Idiot-proof, one-line Community edition setup — OpenClaw style.

## One-liner

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash
```

With a model key already in the environment:

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash -s -- \
  --openai-key "$OPENAI_API_KEY"
```

Non-interactive (CI / automation):

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash -s -- \
  -y --openai-key "$OPENAI_API_KEY" --dir ~/boss
```

From a local checkout:

```bash
./install.sh --dir "$(pwd)" --openai-key "$OPENAI_API_KEY"
```

## What it does

1. Checks OS tools (`git`, `docker`, Compose, disk, ports)  
2. Prompts for install directory and **BYOK** model key (optional skip)  
3. Clones/updates the BOSS repo  
4. Writes a private `.env` (API key generated, mode 600)  
5. `docker compose` builds and starts **Postgres + API + worker** (+ Mission Control if selected)  
6. Waits for `/health`, runs DB seed  
7. Verifies Community edition + ProviderOps surface  

No BOSS Cloud / Account Connect required.

## Files

| Path | Role |
|---|---|
| `install.sh` | One-line entry (repo root) |
| `scripts/install/community-compose.yml` | Self-contained Community stack |
| `.env` (generated) | Secrets + ports — never commit |

## Requirements

- Docker Desktop (macOS/Windows) or Docker Engine + Compose (Linux)  
- `git`, `curl`  
- ~5GB free disk for first image build  

## After install

```bash
source ~/boss/boss-community.env.sh
curl -sS -H "X-API-Key: $BOSS_API_KEY" "$BOSS_API_URL/health"
# Mission Control: http://127.0.0.1:3000
```

Stop:

```bash
cd ~/boss
docker compose -f scripts/install/community-compose.yml --env-file .env down
```

## Flags

See `./install.sh --help`.
