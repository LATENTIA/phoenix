# Deploying Phoenix to EC2 with GitHub Actions

This guide walks through the one-time setup needed to deploy Phoenix to an EC2
instance via the `Deploy to EC2` GitHub Actions workflow (see
`.github/workflows/deploy.yml`).

The deploy model:

- **GitHub Actions** rsyncs the source code, renders `.env.runtime` from
  GitHub Secrets, and runs `docker compose up -d --build` over SSH.
- **EC2 instance** runs Docker + docker-compose, with the application's
  persistent data living **outside** the container in a stable host
  directory (default `/var/phoenix-data`). The container is disposable;
  the data is not.
- **Every secret** (SSH key, basic-auth hash, Flask secret key) lives in
  GitHub Secrets. Nothing sensitive is ever committed to the repo.
- **Redeploys** rebuild the image and recreate the container. The bind-
  mounted data directory is reattached untouched, so trade history,
  accounts, and dividend records survive every deploy.

---

## 1. Provision the EC2 instance (one time)

Pick a small Linux box (`t3.small` or larger; `t2.micro` works but pandas
build will be slow). Ubuntu 24.04 LTS or Amazon Linux 2023 are both fine.
The instructions below use the Ubuntu commands; adjust `apt` to `yum` /
`dnf` for AL2023.

### 1a. Install Docker + Compose plugin

```bash
# As the default user (ubuntu / ec2-user):
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

# Docker official install (truncated; copy from https://docs.docker.com/engine/install/ubuntu/):
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Let your SSH user run docker without sudo:
sudo usermod -aG docker $USER
# Log out and back in for the group change to take effect.
exit
```

Verify after reconnect:
```bash
docker version
docker compose version
```

### 1b. Create the persistent data directory

This directory is **outside the container** and survives every deploy.
The Phoenix container runs as UID 1000 inside, so the host directory
must be owned by UID 1000 too.

```bash
sudo mkdir -p /var/phoenix-data/downloaded /var/phoenix-data/logs
sudo chown -R 1000:1000 /var/phoenix-data
sudo chmod 750 /var/phoenix-data
```

For better durability, mount an EBS volume at `/var/phoenix-data` instead
of using instance storage. That way you can detach the volume, attach to
a new instance, and your data follows. Use the standard `mkfs.ext4` →
`mount` → `/etc/fstab` flow.

### 1c. (Optional) Reserve a fixed username for the deploy SSH key

The GitHub Action SSHes in as `${EC2_USER}`. By default that's `ubuntu`
(Ubuntu) or `ec2-user` (Amazon Linux). You can keep that user or create
a dedicated `deploy` user with a smaller permission surface:

```bash
sudo useradd -m -s /bin/bash deploy
sudo usermod -aG docker deploy
sudo mkdir -p /home/deploy/.ssh
sudo chown deploy:deploy /home/deploy/.ssh
sudo chmod 700 /home/deploy/.ssh
# Add your deploy SSH PUBLIC key (~/.ssh/deploy_key.pub) here:
sudo nano /home/deploy/.ssh/authorized_keys
sudo chown deploy:deploy /home/deploy/.ssh/authorized_keys
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

The first `docker compose up` from GitHub Actions will create
`/home/deploy/phoenix/` and put the source there.

### 1d. Open the right inbound ports in the security group

- **SSH (port 22)** open to GitHub Actions runner IPs. The simplest
  option is "Anywhere" (0.0.0.0/0); the deploy user can only do anything
  with the right private key, which only lives in your GitHub Secrets.
  For tighter control, use the GitHub-published IP ranges:
  https://docs.github.com/en/actions/using-github-hosted-runners/about-github-hosted-runners/about-github-hosted-runners#ip-addresses
- **Phoenix port (5000)** should NOT be open to the world. Either:
  - Put an ALB / Nginx / Caddy in front of port 5000 and only open the
    proxy port (80/443). The container already binds 127.0.0.1:5000 only
    on the host, so this is the default.
  - Use AWS SSM Session Manager to port-forward 5000 to your laptop.
  - Use an SSH tunnel: `ssh -L 5000:127.0.0.1:5000 deploy@<ec2-host>`.

For your initial test, an SSH tunnel is easiest. TLS termination + ALB
is the EC2 best practice you'd add later.

---

## 2. Generate the secrets you'll put into GitHub

Do these on your **local** machine (not on EC2; you want the private key
on the laptop, not on the instance).

### 2a. SSH key pair just for deploys

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/phoenix_deploy_key -C "phoenix-deploy"
# Public key → put in ~/.ssh/authorized_keys on the EC2 instance (step 1c).
# Private key → goes into GitHub Secrets as EC2_SSH_KEY (next section).

cat ~/.ssh/phoenix_deploy_key.pub      # this goes ON the EC2 instance
cat ~/.ssh/phoenix_deploy_key          # this whole PEM goes in GitHub Secrets
```

### 2b. Phoenix auth hash and secret key

```bash
# Web UI basic-auth hash for the password of your choice:
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('CHOOSE-A-STRONG-PASSWORD'))"

# Flask secret key (signs session cookies + CSRF tokens):
python -c "import secrets; print(secrets.token_hex(32))"
```

Save the password you typed into `generate_password_hash` — you'll need it
to log in. The hash is what goes in GitHub Secrets.

---

## 3. Configure GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**.

Add exactly these. Names matter; the workflow looks them up by name:

| Secret | Example value | Notes |
|---|---|---|
| `EC2_HOST` | `ec2-3-120-45-78.eu-west-1.compute.amazonaws.com` | The public DNS or Elastic IP of the instance. |
| `EC2_USER` | `deploy` (or `ubuntu`, `ec2-user`) | The SSH user the workflow logs in as. |
| `EC2_SSH_KEY` | (paste the full PEM, including `-----BEGIN` and `-----END` lines) | The PRIVATE key. Treat like a password. |
| `PHOENIX_AUTH_USER` | `admin` | The HTTP-basic-auth username. |
| `PHOENIX_AUTH_PASS_HASH` | `scrypt:32768:8:1$ABCsalt$DEFhash...` | Paste raw with **single** `$`. The workflow doubles them automatically before writing `.env.runtime`. |
| `PHOENIX_SECRET_KEY` | (64-char hex string from `secrets.token_hex(32)`) | Signs Flask session cookies and CSRF tokens. |
| `PHOENIX_DATA_DIR` *(optional)* | `/var/phoenix-data` | Defaults to `/var/phoenix-data` if unset. Set this if you mount an EBS volume elsewhere. |
| `PHOENIX_SECRETS_BACKEND` *(optional)* | `plaintext` or `aws` | Defaults to `plaintext`. Set to `aws` only if you've also given the EC2 IAM role `secretsmanager:*` permissions on `arn:aws:secretsmanager:*:*:secret:phoenix/*`. |

After adding them, you should see something like this in the secrets list:

```
EC2_HOST                     Updated ...
EC2_SSH_KEY                  Updated ...
EC2_USER                     Updated ...
PHOENIX_AUTH_PASS_HASH       Updated ...
PHOENIX_AUTH_USER            Updated ...
PHOENIX_SECRET_KEY           Updated ...
```

GitHub Secrets are encrypted at rest and only decrypted into the workflow
runner at job execution time. They're never visible to anyone with repo
read access, only to repo admins (and only as masked values in logs).

---

## 4. First deploy

Two ways to trigger the workflow:

1. **Push to `main`** — the deploy fires automatically on every push.
2. **Manually** — Actions tab → "Deploy to EC2" → "Run workflow".

Watch the run in the Actions UI. You should see all eight steps go green
in 2-5 minutes (first build is slow; subsequent builds use Docker layer
cache and finish in ~30 seconds).

After it's green, log in via the SSH tunnel (or whatever you set up in
step 1d):

```bash
ssh -L 5000:127.0.0.1:5000 deploy@<ec2-host>
# Then in your browser: http://127.0.0.1:5000/
# Log in with PHOENIX_AUTH_USER + the plaintext password you set.
```

---

## 5. Updating the app

Just push to `main`. The workflow:

1. rsyncs the new code.
2. Re-renders `.env.runtime` from the same GitHub Secrets (idempotent —
   it's only relevant if you rotate secrets).
3. Runs `docker compose up -d --build`.
4. The bind mount reattaches automatically. `phoenix-data/` is untouched.

**Your data is safe because:**
- The bind mount config in `docker-compose.yml` points at `PHOENIX_DATA_DIR`
  on the HOST, not at any path inside the container or inside the image.
- `docker compose up -d --build` only rebuilds the IMAGE layer and recreates
  the CONTAINER. Bind mounts are reattached, not recreated.
- `rsync --delete` in the workflow is restricted by `--exclude` patterns
  that never touch `/var/phoenix-data/`.
- Even `docker compose down -v` (which the workflow does NOT call) would
  only delete Docker-managed volumes. Bind mounts are owned by the host
  filesystem and are unaffected.

---

## 6. Backups

The whole of `/var/phoenix-data/` is your data. Snapshot it:

```bash
# Manual snapshot to /var/phoenix-backups/:
sudo cp -a /var/phoenix-data /var/phoenix-backups/phoenix-data-$(date +%Y%m%d-%H%M%S)
```

For production, use EBS snapshots (point-in-time, fast, AWS-native):

```bash
aws ec2 create-snapshot \
  --volume-id vol-XXXXXXXX \
  --description "phoenix-data nightly $(date -u +%Y-%m-%d)"
```

Schedule via AWS Data Lifecycle Manager (DLM) for daily/weekly retention.

---

## 7. Rolling back

If a bad deploy ships, two options:

1. **Revert the commit on `main`** and push. The workflow runs and ships
   the previous version. The data is unaffected.
2. **Manual SSH** and `git reset --hard <good-commit-sha>` inside
   `/home/<user>/phoenix/`, then `docker compose up -d --build`. Faster
   but bypasses the GitHub Actions audit log.

---

## 8. Common gotchas

- **First deploy hangs on Docker build**: pandas wheel install pulls
  ~50 MB. A `t2.micro` (1 GB RAM) can OOM; bump to `t3.small`.
- **`/license` returns 200 (no auth)**: GitHub Secrets `PHOENIX_AUTH_USER`
  or `PHOENIX_AUTH_PASS_HASH` is empty. The workflow's final smoke-test
  step catches this and fails the run.
- **Hash arrives mangled at the container**: the workflow's Python step
  already escapes `$` → `$$`. If it still mangles, paste the hash into
  the GitHub Secret again — sometimes copy/paste introduces trailing
  whitespace that throws off the format.
- **Container won't start after deploy**: `ssh` to EC2, `docker compose
  logs phoenix --tail 50`. Most failures are missing env vars or a perms
  issue on `/var/phoenix-data/` (re-run the chown in step 1b).
