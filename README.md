NodeSentinel: Proactive Assistant for Bitcoin & Lightning Nodes
NodeSentinel is a resilient, Python-based Telegram assistant designed to proactively monitor the health of your remote Bitcoin/LND node, its hardware, and critical crypto market conditions. It leverages the Gemini AI API for stable, real-time contextual analysis on price movements.

🚀 Key Features
Proactive AI Surveillance: Alerts on price volatility ($3,000+ swings) with instant, AI-generated contextual analysis (using your GEMINI_API_KEY).

Stateful Fee Monitoring: Notifies on changes in fee state (LOW, MEDIUM, HIGH) without repeating the same alert.

Remote Hardware Monitoring: Checks CPU, RAM, Load Average, and Disks on your node via SSH with persistence filters (3 minutes) to eliminate false positives.

Diagnostics & Remote Action: Secure commands (/diagnose, /restartlnd, /restartbtc) to manage services remotely.

Network Scanner: Use /netscan for lightweight network discovery on your local LAN.

📋 1. Local Setup and Requirements
Prerequisites
Python 3.8+

SSH Key Access: The user running this bot (e.g., ubuntu or admin) must have an SSH key configured to access the remote Bitcoin node without a password.

LND Credentials: You must obtain copies of your tls.cert and admin.macaroon files from your LND node.

Installation
Clone your repository and install dependencies. We recommend using a virtual environment (venv):

# Create and activate the virtual environment
python3 -m venv venv_nodesentinel
source venv_nodesentinel/bin/activate

# Install all required dependencies
pip install lndgrpc python-telegram-bot psutil python-dotenv httpx

🔐 2. Configuration Scenarios (Crucial Step)
The performance of NodeSentinel depends on setting the correct paths and IPs in the .env file based on your setup.

A. Credential Files Placement (MUST COPY)
Regardless of where the bot runs, you must copy your LND credentials (tls.cert and admin.macaroon) from the Node Machine to the Bot Machine. The paths in your .env must point to these local copies.

B. Scenario: Bot on REMOTE Machine (Recommended)
This is the most common setup for security and performance.

Variable

Value

Description

LND_HOST

XX.XX.XX.XX:10009

External or LAN IP of the Node.

CERT_PATH

/path/to/local/copy/tls.cert

Path on the Bot Machine.

SSH_HOST

XX.XX.XX.XX

IP of the Node. Used for hardware monitoring and remote actions.

C. Scenario: Bot on SAME Machine as the Node
This requires all IPs to be local.

Variable

Value

Description

LND_HOST

127.0.0.1:10009

Localhost IP.

SSH_HOST

127.0.0.1

Localhost IP.

CERT_PATH

/home/admin/.lnd/tls.cert

Actual absolute path on the Node.

💻 3. Service Management (systemd)
For automated startup and reliability, NodeSentinel must run as a systemd service.

Step A: Create Service File
Create the service file (adjust the paths to your project location):

sudo nano /etc/systemd/system/nodesentinel.service

Paste the configuration below (ensure WorkingDirectory and ExecStart paths are correct):

[Unit]
Description=NodeSentinel Telegram Bot (LND/BTC Monitoring)
After=network.target

[Service]
User=ubuntu
Group=ubuntu

WorkingDirectory=/home/ubuntu/lnd_credentials
ExecStart=/home/ubuntu/lnd_credentials/venv_nodesentinel/bin/python3 nodesentinel.py

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

Step B: Load and Start
Execute the following commands:

sudo systemctl daemon-reload
sudo systemctl enable nodesentinel
sudo systemctl start nodesentinel
sudo systemctl status nodesentinel 

🌟 4. Contributions and Donations
If my work pleases you, any donation is greatly appreciated!

Lightning: davidebtc@walletofsatoshi.com

Bitcoin On-Chain: bc1qqksvzgksjgmffmggyg836h45le3d5aq5d5xqj0
