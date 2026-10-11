# Server setup (once)

What the Deploy workflow (`.github/workflows/deploy.yml`) needs on the Linux machine, and in
GitHub. About 15 minutes. Assumes a static public IPv4 address with ports 22, 80 and 443 reachable
from the internet, and an amd64 machine with at least 2 GB of RAM.

## 1. Docker and the deploy user

As root (or with `sudo`):

```sh
curl -fsSL https://get.docker.com | sh       # or your distribution's docker-ce packages
useradd --create-home --shell /bin/bash deploy
usermod -aG docker deploy
install -d -o deploy -g deploy -m 700 /opt/nevis
```

Membership of the `docker` group is root-equivalent, which is why the deploy key below may run
only `deploy.sh`.

## 2. Firewall

Open SSH, HTTP (Let's Encrypt validation and the redirect) and HTTPS (TCP, and UDP for HTTP/3).
With ufw:

```sh
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw enable
```

Docker publishes ports through its own iptables rules, bypassing ufw: only Caddy publishes any,
and Postgres and the api stay inside the Docker network.

## 3. Files in /opt/nevis

As `deploy` (`sudo -iu deploy`), copy `deploy/deploy.sh` and `deploy/.env.example` from the
repository:

```sh
cd /opt/nevis
install -m 700 /path/to/deploy.sh deploy.sh
install -m 600 /path/to/.env.example .env
openssl rand -base64 32 | tr -d '/+='       # → POSTGRES_PASSWORD
docker run --rm caddy:2.11.7 caddy hash-password --plaintext 'an API password'
                                             # → NEVIS_BASIC_AUTH_HASH, in single quotes
$EDITOR .env                                 # NEVIS_PUBLIC_IP, NEVIS_IMAGE_PREFIX, the two above
```

Keep the API password somewhere safe: the server stores only its hash. `deploy.sh` is not
updated by deploys; copy it again when it changes.

## 4. Registry access

The images are private packages on ghcr.io. Create a classic personal access token with only the
`read:packages` scope (GitHub → Settings → Developer settings → Personal access tokens →
Tokens (classic)), then as `deploy`:

```sh
docker login ghcr.io -u <your GitHub user>  # paste the token as the password
```

## 5. The deploy key

On your own machine, a key used for nothing else:

```sh
ssh-keygen -t ed25519 -N '' -C nevis-deploy -f nevis-deploy
ssh-keyscan -t ed25519 <public ip>           # → DEPLOY_KNOWN_HOSTS; check it against
                                             #   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`
                                             #   on the server
```

On the server, append the public key to `/home/deploy/.ssh/authorized_keys` (mode 600, directory
700) with a forced command and one permitted tunnel. Whatever command a client sends goes to
`deploy.sh` as text (which accepts only `prepare|backup|start <version>`), no shell or terminal is
possible, and port forwarding reaches only Postgres, for the migrations CI runs:

```text
command="/opt/nevis/deploy.sh",restrict,port-forwarding,permitopen="127.0.0.1:5432" ssh-ed25519 AAAA... nevis-deploy
```

The tunnel also needs sshd to allow forwarding (the default on Debian and Ubuntu, not
everywhere). To allow it for this user only, append to `/etc/ssh/sshd_config` and reload sshd:

```text
Match User deploy
    AllowTcpForwarding local
```

## 6. GitHub

Repository → Settings → Environments → New environment `production`:

- Deployment branches: **Selected branches → `main`**, so a workflow edited on another branch
  can't use these secrets.
- Optional: required reviewers, to approve each deploy.
- Variables: `DEPLOY_HOST` = the public IP, `DEPLOY_USER` = `deploy`.
- Secrets: `DEPLOY_SSH_KEY` = the contents of `nevis-deploy` (the private key),
  `DEPLOY_KNOWN_HOSTS` = the `ssh-keyscan` line, `POSTGRES_PASSWORD` = the one in the server's
  `.env` (the migrations run from CI, through the tunnel).

## 7. First deploy

Actions → Deploy → Run workflow → `latest`. The first start asks Let's Encrypt for the IP
certificate, which takes a few seconds once ports 80/443 are reachable. Then
`https://<public ip>/docs` asks for the API credentials.

## Operating it

In `/opt/nevis` as `deploy`, plain `docker compose` works (`ps`, `logs caddy`, `restart api`).

- **Which version runs:** `NEVIS_IMAGE_TAG` in `.env`; `DEPLOYED` lists every deploy.
- **What a deploy does:** `deploy.sh prepare` (stage the release's compose file, pull, start
  Postgres), `deploy.sh backup` (a dump into `backups/`), the migrations from CI through the
  tunnel (`deploy/migrate.sh`), `deploy.sh start` (switch, record, check). If the migrations fail,
  `start` never runs and the old release keeps serving; each migration file is a transaction.
- **Rollback:** deploy the previous version. If the bad release ran a migration, also restore
  the backup taken before it (the newest in `backups/`, named after the release it preceded):

  ```sh
  docker compose stop api worker caddy
  docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
    < backups/nevis-<timestamp>-before-<bad version>.dump
  ```

  then deploy the previous version.
- **Backups:** CI has the server take one before every deploy; the last 7 are kept. They never
  leave the machine (no personal data goes to GitHub), so they are not off-site either: copy them
  elsewhere if the data matters.
- **Certificates:** 6-day Let's Encrypt certificates for the IP, renewed by Caddy every couple of
  days. Ports 80/443 must stay open for renewals; `docker compose logs caddy` shows failures.
  Never `docker compose down -v`: the `caddy_data` volume holds the certificates and account.
- **New public IP:** update `NEVIS_PUBLIC_IP` in `.env` and `DEPLOY_HOST` in GitHub, then
  deploy again; Caddy fetches a certificate for the new address.
- **If IP certificates fail:** a hostname that resolves to the IP works without a domain, e.g.
  `203-0-113-10.sslip.io` (set `NEVIS_PUBLIC_IP` to it; `default_sni` then needs no change).
  To test issuance without touching Let's Encrypt's production rate limits, add
  `ca https://acme-staging-v02.api.letsencrypt.org/directory` inside the `issuer acme` block of
  `deploy/caddy/tls-public.caddy`.
