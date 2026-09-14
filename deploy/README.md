# Deploying review_server to the Azure VM

Push to `main` → GitHub Actions restarts the service on the VM. The runner
is **self-hosted on the VM** (pulls jobs over outbound HTTPS), so no inbound
SSH access or SSH secrets are needed for the deploy itself.

## One-time setup

### 1. Run the setup script on the VM
SSH into the VM, clone this repo if you haven't, then:
```bash
cd ~/9jalingo_dataset     # wherever you cloned it
bash deploy/setup_vm.sh
```
This creates the venv, the `9jalingo-review` systemd service, and
`/etc/9jalingo-review/env` for secrets. **Edit that file** and fill in
`HF_TOKEN` (and `REVIEW_TOKEN` if you use one) — it's `chmod 600`, outside
git, and never touched by the deploy workflow. Re-run
`sudo systemctl start 9jalingo-review` once it's filled in.

### 2. Register the GitHub Actions self-hosted runner
This step needs a short-lived token from the GitHub UI, so it can't be
scripted ahead of time:

1. On GitHub: **9jaLingo/9jalingo_dataset → Settings → Actions → Runners →
   New self-hosted runner** → Linux, x64.
2. Copy the `./config.sh --url ... --token ...` command it gives you and run
   it on the VM:
   ```bash
   mkdir ~/actions-runner && cd ~/actions-runner
   curl -o runner.tar.gz -L <url from the GitHub page>
   tar xzf runner.tar.gz
   ./config.sh --url https://github.com/9jaLingo/9jalingo_dataset --token <TOKEN>
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```
   `svc.sh install` registers it as a systemd service so it survives reboots
   and comes back after the VM restarts.
3. Confirm it shows **Idle** (green) under Settings → Actions → Runners.

### 3. Network access to the app
Pick one:
- **Quick/testing:** open port 8787 inbound in the VM's NSG (Azure Portal →
  this VM → Networking → Inbound port rules), then use
  `http://20.84.96.50:8787/`.
- **Proper:** put nginx or Caddy in front on 80/443 using the VM's existing
  DNS name `naijalingo-annotate.eastus.cloudapp.azure.com`, proxying to
  `127.0.0.1:8787`. Ask Claude to draft this config when you're ready — it's
  a five-minute follow-up once the deploy pipeline itself is confirmed
  working.

## Every push after that
Nothing to do — `git push` to `main` (touching anything under
`review_server/`) triggers `.github/workflows/deploy.yml`, which reinstalls
dependencies and does `systemctl restart 9jalingo-review` on the VM, then
health-checks it. Watch it under the repo's **Actions** tab.

## Security note (public repo)
The runner is self-hosted, so anything it executes runs directly on this
VM. The workflow is intentionally restricted to `push` on `main` only — do
**not** add a `pull_request` trigger, since that would let anyone opening a
PR from a fork run code on the VM. Also turn on branch protection for `main`
(require a PR + review before merge) so a compromised or careless direct
push can't do the same thing.

## Troubleshooting
```bash
sudo systemctl status 9jalingo-review     # is it running?
sudo journalctl -u 9jalingo-review -f     # live logs
sudo systemctl status actions.runner.*    # is the runner online?
```
