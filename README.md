# 🛰️ NodeSentinel Proactive Assistant
**Proactive Bitcoin & Lightning Node Assistant**

NodeSentinel is a **Python-based Telegram and monitoring assistant** designed to proactively analyze and alert Bitcoin/Lightning node operators about:
- **Network fees (Mempool)** and **on-chain conditions**
- **BTC price volatility**, macroeconomic context (via Gemini AI)
- Optional **LND integration via gRPC** for advanced Lightning metrics

---

## 🧩 Features
- Real-time monitoring of **fees, difficulty, and price**
- AI contextual analysis of market movements (Gemini API)
- 24h periodic macro reports
- Alerts via Telegram
- Compatible with both **local** (same machine as node) or **remote** setups

---

## 🖥️ Recommended OS
- **Ubuntu 22.04 LTS**
- Python ≥ 3.9

---

## 📦 Installation

### 1. Clone Repository
```bash
git clone https://github.com/asyscom/NodeSentinel-Proactive-Assistant.git
cd NodeSentinel-Proactive-Assistant
```

### 2. Create Virtual Environment & Install Dependencies
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## ⚙️ Environment Configuration

Create a `.env` file in the project root with:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
GEMINI_API_KEY=your_gemini_api_key_here
NODE_MODE=local   # or remote
LND_GRPC_HOST=127.0.0.1:10009
LND_TLS_PATH=/path/to/tls.cert
LND_MACAROON_PATH=/path/to/admin.macaroon
```

---

## 🧠 Generating LND gRPC Files

If your bot interacts directly with `lnd`, you must generate the following files:
- `lightning_pb2.py`
- `lightning_pb2_grpc.py`

Use the included script:

```bash
chmod +x generate_lnd_proto.sh
./generate_lnd_proto.sh
```

This script automatically clones the LND repo and builds the Python bindings.

---

## 🛰️ Running NodeSentinel

### Option 1: Run directly
```bash
source venv/bin/activate
python3 nodesentinel.py
```

### Option 2: Run as systemd service

Example unit file: `/etc/systemd/system/nodesentinel.service`

```ini
[Unit]
Description=NodeSentinel Proactive Assistant
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/NodeSentinel-Proactive-Assistant
ExecStart=/home/ubuntu/NodeSentinel-Proactive-Assistant/venv/bin/python3 nodesentinel.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable nodesentinel
sudo systemctl start nodesentinel
```

---

## 🌍 Remote Setup (Bot on Separate Machine)

When NodeSentinel runs on a **different host** than your LND node:

1. **Generate SSH key** on the bot machine:
```bash
ssh-keygen -t ed25519
ssh-copy-id user@node-ip
```

2. **Use pull-secrets.sh** (optional helper script) to securely copy:
```bash
./pull-secrets.sh user@node-ip:/path/to/lnd
```

This will retrieve `tls.cert` and `admin.macaroon` securely and adjust permissions automatically.

3. Set correct paths in `.env` accordingly.

---

## 🧾 Logging

Logs are written to `nodesentinel.log` in the project directory.

---



## 🧰 Troubleshooting

| Issue | Possible Fix |
|-------|---------------|
| `Mempool API Error` | Ensure HTTPS and endpoint accessibility |
| `Price API Error` | CoinGecko rate limit, wait 10s |
| `Contextual Analysis Failed` | Check `GEMINI_API_KEY` |
| `gRPC Error: Unavailable` | Verify LND port (10009) and TLS/macaroons |

---

## 🧱 Requirements Summary

See `requirements.txt` for all dependencies.

---
## Support / Donate

If you like this work and want to support it, you can donate via Lightning to `davidebtc@walletofsatoshi.com` or via BTC on-chain to `bc1qqksvzgksjgmffmggyg836h45le3d5aq5d5xqj0`. ⚡💰

---
## 💡 Contributing
Pull requests welcome! For issues, open a GitHub ticket.

---
© 2025 NodeSentinel Project — Bitcoin-native monitoring & intelligence tool.
