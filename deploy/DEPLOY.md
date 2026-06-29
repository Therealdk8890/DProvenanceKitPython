# Deploy the hosted backend to a VM (with automatic HTTPS)

Stand up a durable, TLS-terminated instance on any small Linux VM (DigitalOcean, Hetzner,
Lightsail, EC2, …) using Docker + Caddy. ~10 minutes.

What you get: the backend behind **Caddy**, which obtains and renews a Let's Encrypt
certificate automatically. The backend itself is never exposed to the internet — only Caddy
(ports 80/443) is. Data persists in Docker volumes.

## 1. A domain + a VM

1. Create a VM (Ubuntu 22.04+ is fine; the smallest \$5–10/mo size works).
2. Point a DNS **A record** (e.g. `api.yourdomain.com`) at the VM's public IP. Let's Encrypt
   verifies over HTTP, so the domain must resolve to this box before you start.

## 2. Install Docker (on the VM)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker   # run docker without sudo
```

## 3. Get the code + configure

```bash
git clone https://github.com/Therealdk8890/DProvenanceKitPython.git
cd DProvenanceKitPython
cp deploy/.env.example deploy/.env
# edit deploy/.env: set DOMAIN=api.yourdomain.com  (+ Stripe vars later)
```

## 4. Open the firewall + launch

```bash
sudo ufw allow 80 && sudo ufw allow 443        # or the cloud provider's security group
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
```

Caddy fetches the certificate on first request. Verify (give it ~30s for the cert):

```bash
curl https://api.yourdomain.com/api/health      # -> {"status":"ok",...}
```

The dashboard is at `https://api.yourdomain.com/`.

## 5. Create real API keys

First boot seeds a throwaway `demo` project + key (printed in the logs:
`docker compose -f deploy/docker-compose.yml logs dprovenancekit`). Create proper ones with
the admin CLI **inside the container** so it uses the same tenancy DB:

```bash
C="docker compose -f deploy/docker-compose.yml exec dprovenancekit"
$C python server/admin.py create-project "Acme" --plan pro          # -> proj_xxxx
$C python server/admin.py create-key --project proj_xxxx --role write --name ci   # -> dpk_… (save it)
```

## 6. Point your app + CI at it

- SDK: `CloudTraceStore(MyEvent, endpoint="https://api.yourdomain.com", api_key="dpk_…")`
- CI gate: `DPROV_URL=https://api.yourdomain.com DPROV_KEY=dpk_… python dprov_gate.py …`

## 7. Stripe (optional, when you turn on billing)

1. In Stripe, add a webhook endpoint → `https://api.yourdomain.com/webhooks/stripe`; copy the
   signing secret (`whsec_…`).
2. Put it in `deploy/.env` as `DPROV_STRIPE_WEBHOOK_SECRET=whsec_…` (and optionally
   `DPROV_STRIPE_PRICE_PLANS="price_abc:pro"`), then `up -d` again to apply.
3. Attach `metadata.project_id` to your Stripe Checkout/subscription so the webhook knows
   which project to upgrade.

## Operating it

- **Update:** `git pull && docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build`
- **Logs:** `docker compose -f deploy/docker-compose.yml logs -f`
- **Back up:** the `dprov-data` volume holds every run + the tenancy DB. Snapshot it (e.g.
  `docker run --rm -v dprovenancekitpython_dprov-data:/d -v "$PWD":/b alpine tar czf /b/dprov-backup.tgz -C /d .`).

## Limits (by design, for now)

- **Single instance.** The SQLite/in-process design is not safe behind multiple replicas —
  run exactly one backend container. Horizontal scale needs a Postgres backend (not yet
  built; the in-memory/SQLite stores are held at parity, so it's a store swap).
- **TLS is required** (Caddy handles it) — API keys are Bearer tokens and must not travel
  over plain HTTP.
