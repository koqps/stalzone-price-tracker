# Oracle Always Free deployment

This keeps the StalZone website, Discord bot, and collectors running continuously on one Oracle Cloud VM while Supabase remains the persistent database.

## Recommended free VM

- Image: Ubuntu 24.04 (aarch64/Arm)
- Shape: VM.Standard.A1.Flex (Always Free eligible)
- OCPU: 1
- Memory: 6 GB
- Boot volume: default Always Free size
- Public IPv4: enabled

## OCI network rules

Allow inbound TCP:
- 22 from your own IP only (SSH)
- 80 from 0.0.0.0/0 (website)
- 443 from 0.0.0.0/0 later if a domain/TLS is configured

Do not expose port 8420 publicly; nginx proxies port 80 to FastAPI on localhost.

## Install

SSH into the VM, then:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone --branch deploy/oracle-always-free https://github.com/koqps/stalzone-price-tracker.git ~/stalzone-bootstrap
cd ~/stalzone-bootstrap
sudo bash deploy/oracle/install.sh
```

The installer asks for the existing secrets directly in the VM terminal. Secret input is never committed to GitHub. It creates `/etc/stalzone-tracker.env` with mode `600`.

Required values:
- `DISCORD_TOKEN`
- `SUPABASE_SYNC_URL`
- `SUPABASE_SYNC_SECRET`

Optional values:
- `DISCORD_ALERT_WEBHOOK`
- `CHANNEL_ID`

## Verify

```bash
systemctl status stalzone-tracker
journalctl -u stalzone-tracker -n 100 --no-pager
curl http://127.0.0.1:8420/health
```

Then open `http://PUBLIC_IP/`.

Do not shut down Render until the Oracle instance has restored Supabase history, completed a market scan, connected to Discord, and served the dashboard successfully.
