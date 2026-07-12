# Deploying to Oracle Cloud "Always Free" (Mumbai) — 24/7 for free

Why Oracle + Mumbai: it's a genuine always-on Linux VM, free forever, and the
**Indian IP is what keeps NSE from blocking the market-wide filing feed** (NSE
blocks most foreign/datacenter IPs — this is the whole reason we pick Mumbai).

Total time ≈ 30–40 min. You need a credit/debit card for signup verification —
Always Free resources are **not charged**.

---

## 1. Create the Oracle Cloud account (pick India region!)

1. Go to <https://www.oracle.com/cloud/free/> → "Start for free".
2. **Home Region: choose `India Central (Hyderabad)` or `India West (Mumbai)`.**
   ⚠️ This is chosen ONCE at signup and **can never be changed** — if you pick a
   non-India region, NSE will likely block you and you'd have to re-register.
3. Verify with card (no charge). Account provisioning takes a few minutes.

## 2. Create the Always Free VM

1. Console → hamburger menu → **Compute → Instances → Create instance**.
2. **Image & shape → Change shape → Ampere (ARM) `VM.Standard.A1.Flex`**
   (1 OCPU / 6 GB is plenty and Always Free). If it says "out of capacity",
   switch to **`VM.Standard.E2.1.Micro`** (x86, always available, 1 GB RAM — also
   fine, our app is tiny since the LLM runs remotely on Groq).
3. **Image: Canonical Ubuntu 22.04**.
4. **Add SSH keys → Generate a key pair for me → download the private key**
   (or paste your own public key). Save the private key as `oracle_key`.
5. Leave networking default (it creates a VCN with a public IP and SSH open).
6. **Create**. Wait for it to reach "Running", note the **Public IP address**.

## 3. Connect

```bash
chmod 600 oracle_key
ssh -i oracle_key ubuntu@<PUBLIC_IP>
```

## 4. Install and set up the app (on the VM)

```bash
sudo apt update && sudo apt install -y python3-venv git sqlite3

# Get the code onto the VM — pick ONE:
#  (a) git (recommended): push this project to a PRIVATE GitHub repo, then:
git clone https://github.com/<you>/stock-news-alerts.git
#  (b) or from your laptop (no GitHub), run this on your LAPTOP instead:
#      rsync -av --exclude nenv --exclude '*.db' -e "ssh -i oracle_key" \
#        ~/Downloads/Projects/stock-news-alerts/ ubuntu@<PUBLIC_IP>:~/stock-news-alerts/

cd stock-news-alerts

# Recreate the venv ON THE VM (do NOT copy nenv/ from the laptop — arch differs)
python3 -m venv nenv
nenv/bin/pip install -r requirements.txt
```

## 5. Add your secrets (never commit .env)

```bash
cp .env.example .env
nano .env      # paste GROQ_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
chmod 600 .env
```
Keep `COVERAGE_MODE=market_wide` and `POLL_INTERVAL_MINUTES=2`. `DRY_RUN=false`.

## 6. CRITICAL: verify NSE actually responds from this IP

This is the make-or-break test — confirm the Mumbai IP isn't blocked before
relying on it:

```bash
nenv/bin/python init_db.py
DRY_RUN=true nenv/bin/python -m src.pipeline --once
```
You want a log line like `nse_announcements: N market-wide filing(s)` with **N > 0**
during/after market hours. If N is always 0 or you see fetch errors, the IP is
blocked — try the other India region or an E2 shape. (If it works, great — proceed.)

Also send a real Telegram test:
```bash
nenv/bin/python -m src.alerting.telegram_bot
```

## 7. Run it 24/7 with systemd

```bash
sudo cp deploy/stock-news-alerts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-news-alerts
```

Check it:
```bash
systemctl status stock-news-alerts
journalctl -u stock-news-alerts -f      # live logs (Ctrl+C to stop watching)
```

You should get the "🟢 stock-news-alerts is online" Telegram message, then
high-impact filing alerts as they hit the exchange.

## Managing it

| Action | Command |
|---|---|
| Live logs | `journalctl -u stock-news-alerts -f` |
| Stop | `sudo systemctl stop stock-news-alerts` |
| Start | `sudo systemctl start stock-news-alerts` |
| Restart (after config/code change) | `sudo systemctl restart stock-news-alerts` |
| Disable autostart | `sudo systemctl disable stock-news-alerts` |
| Update code | `git pull` (or rsync again) → `sudo systemctl restart stock-news-alerts` |

## Notes
- The SQLite `stock_news.db` lives on the VM disk, so dedup state survives
  restarts and reboots. Nothing else to persist.
- The scheduler pins the daily-summary job to `Asia/Kolkata`, so the VM's own
  timezone doesn't matter.
- Only outbound network is used — no inbound ports beyond SSH need opening.
- Free tier is genuinely free forever for these shapes; you won't be billed as
  long as you stay on Always-Free-eligible shapes (the ones marked "Always Free"
  in the create screen).
