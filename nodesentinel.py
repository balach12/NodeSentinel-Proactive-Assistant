#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NodeSentinel - Bitcoin/LND Monitoring and Proactive Telegram Assistant
Requirements:
  pip install lndgrpc python-telegram-bot psutil nest_asyncio httpx python-dotenv
Start:
  python nodesentinel.py
"""

import asyncio
import time
import os
import psutil
import subprocess
import re
import logging
from dotenv import load_dotenv
from typing import Dict, Any, Optional, Tuple, List

# Load environment variables from the .env file
load_dotenv() 

# Import crypto module and functions
import crypto_utils
from crypto_utils import surveillance_task, get_fee_data, get_price_data, perform_contextual_analysis, update_fee_history, update_price_history, FEE_HISTORY, PRICE_HISTORY
from lndgrpc import LNDClient
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================ LOGGING CONFIG ================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Function to read variables and convert them
def get_env(key, type=str, default=None):
    value = os.getenv(key, default=default)
    if value is None:
        raise ValueError(f"Environment variable {key} not found. Check the .env file.")
    if type == int:
        return int(value)
    if type == float:
        return float(value)
    return value

# ================ CONFIG (Reads from .env) ================
TELEGRAM_TOKEN = get_env("TELEGRAM_TOKEN")
CHAT_ID = get_env("CHAT_ID", int)

# LND and Bitcoin RPC
CERT_PATH = get_env("CERT_PATH")
MACAROON_PATH = get_env("MACAROON_PATH")
LND_HOST = get_env("LND_HOST")
BITCOIN_CLI = get_env("BITCOIN_CLI")
RPC_USER = get_env("RPC_USER")
RPC_HOST = get_env("RPC_HOST")
RPC_PORT = get_env("RPC_PORT", int)
RPC_PASSWORD = get_env("RPC_PASSWORD")

# SSH Remote Host
SSH_USER = get_env("SSH_USER") 
SSH_HOST = get_env("SSH_HOST") 
REMOTE_MOUNTS_TO_CHECK = ["/", "/mnt/hdd"]

# Thresholds (Read as Float/Int)
DISK_THRESHOLD_PCT = get_env("DISK_THRESHOLD_PCT", float)
CPU_THRESHOLD_PCT = get_env("CPU_THRESHOLD_PCT", float)
RAM_THRESHOLD_PCT = get_env("RAM_THRESHOLD_PCT", float)
LOAD_PER_CORE_THRESHOLD = get_env("LOAD_PER_CORE_THRESHOLD", float)

# Frequency and Cooldowns
SYS_CHECK_INTERVAL = 60
LND_CHECK_INTERVAL = 10
ALERT_COOLDOWN = 300
DEFAULT_SSH_TIMEOUT = 20
PERSISTENCE_COUNT = 3 

# Fee Thresholds
FEE_LOW_THRESHOLD = get_env("FEE_LOW_THRESHOLD", float)
FEE_MED_THRESHOLD = get_env("FEE_MED_THRESHOLD", float)
FEE_HIGH_THRESHOLD = get_env("FEE_HIGH_THRESHOLD", float)

# Volatility Thresholds
PRICE_CHANGE_THRESHOLD_LOW = get_env("PRICE_CHANGE_THRESHOLD_LOW", float)
PRICE_CHANGE_THRESHOLD_HIGH = get_env("PRICE_CHANGE_THRESHOLD_HIGH", float)
# ========================================

# ---------- Helper: LND / Bitcoin ----------
def read_lnd_cert_and_macaroon():
    with open(CERT_PATH, "rb") as f:
        cert_bytes = f.read()
    with open(MACAROON_PATH, "rb") as f:
        macaroon_bytes = f.read()
    return cert_bytes, macaroon_bytes.hex()

def get_lnd_client():
    cert, macaroon = read_lnd_cert_and_macaroon()
    return LNDClient(LND_HOST, macaroon=macaroon, cert=cert)

def get_bitcoin_status():
    try:
        blocks = subprocess.check_output(
            [
                BITCOIN_CLI,
                "-rpcconnect=" + RPC_HOST,
                "-rpcport=" + str(RPC_PORT),
                "-rpcuser=" + RPC_USER,
                "-rpcpassword=" + RPC_PASSWORD,
                "getblockcount"
            ],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10
        ).strip()
        return f"⛓️ Bitcoin block height: **{blocks}**"
    except subprocess.CalledProcessError as e:
        return f"⚠️ Bitcoin RPC error: {e.output.strip()}"
    except subprocess.TimeoutExpired:
        return "⚠️ Bitcoin RPC timeout (node unresponsive)"
    except FileNotFoundError:
        return "⚠️ bitcoin-cli not found"
    except Exception as e:
        return f"⚠️ Bitcoin Core not reachable: {e}"

# ---------- Helper alias peers ----------
_node_alias_cache = {}

def get_alias_for_pubkey(client, pubkey):
    if not pubkey:
        return None
    if pubkey in _node_alias_cache:
        return _node_alias_cache[pubkey]

    try:
        node_info = None
        if hasattr(client, "get_node_info"):
            try:
                node_info = client.get_node_info(pubkey)
            except TypeError:
                node_info = client.get_node_info(pub_key=pubkey)
        elif hasattr(client, "GetNodeInfo"):
            try:
                node_info = client.GetNodeInfo(pubkey)
            except Exception:
                try:
                    node_info = client.GetNodeInfo(pub_key=pubkey)
                except Exception:
                    node_info = None

        alias_str = None
        if node_info is not None:
            node = getattr(node_info, "node", None)
            if node:
                alias_str = getattr(node, "alias", None)
            if not alias_str:
                alias_str = getattr(node_info, "alias", None)

        if alias_str:
            _node_alias_cache[pubkey] = alias_str
            return alias_str
    except Exception:
        pass

    _node_alias_cache[pubkey] = None
    return None

# ---------- Helper: Bytes and Humanization ----------
def human_bytes(n):
    for u in ('B','KB','MB','GB','TB'):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.0f}PB"

# --- ASYNCHRONOUS SSH FUNCTIONS ---

async def run_remote_command(command, timeout=DEFAULT_SSH_TIMEOUT):
    """
    Executes a command on the remote node via SSH in a separate thread.
    Returns (output, error).
    """
    effective_timeout = 10 if command.startswith("top") or command.startswith("df") else timeout
    
    ssh_command = f"ssh -o BatchMode=yes -o ConnectTimeout=5 {SSH_USER}@{SSH_HOST} '{command}'"
    loop = asyncio.get_running_loop()
    
    logger.debug(f"Executing SSH command: {ssh_command[:50]}...")
    
    try:
        output = await loop.run_in_executor(
            None,
            lambda: subprocess.check_output(
                ssh_command,
                shell=True,
                text=True,
                stderr=subprocess.PIPE,
                timeout=effective_timeout
            )
        )
        logger.debug("SSH command completed successfully.")
        return output.strip(), None
        
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else "Unknown error."
        logger.error(f"SSH Error (code {e.returncode}): {err_msg}")
        return None, f"⚠️ SSH Error (code {e.returncode}): {err_msg}"
    except subprocess.TimeoutExpired:
        return None, "⚠️ Remote SSH Timeout (node unresponsive)"
    except Exception as e:
        logger.error(f"Generic SSH error: {e}")
        return None, f"⚠️ Remote SSH check failed: {e}"


async def get_remote_system_status():
    """
    Retrieves CPU, RAM, Load, and Uptime from the remote node.
    Returns a dictionary with data and a formatted report text.
    """
    data = {"cpu_pct": 0.0, "ram_pct": 0.0, "load1": 0.0, "load5": 0.0, "load15": 0.0, "uptime": "n/a", "cores": 2}
    report = []
    
    # 1. Retrieve Hardware Data (top)
    command_top = "top -bn1"
    output_top, error_top = await run_remote_command(command_top, timeout=10) 
    
    if error_top:
        return {"error": error_top}, error_top

    # 2. Retrieve Load Average and Uptime
    command_uptime = "uptime"
    output_uptime, error_uptime = await run_remote_command(command_uptime, timeout=5)
    
    if error_uptime:
        logger.warning(f"Error retrieving remote UPTIME: {error_uptime}")
        
    # 3. Data Parsing
    try:
        cpu_line = None
        mem_line = None
        
        for line in output_top.splitlines():
            if 'Cpu(s)' in line or 'Cpu:' in line:
                cpu_line = line
            if 'Mem:' in line or 'MiB Mem' in line or 'kB Mem' in line:
                mem_line = line

        # Parsing CPU
        if cpu_line:
            m = re.search(r'(\d+\.?\d*)\s+id', cpu_line)
            if m:
                idle_pct = float(m.group(1))
                data['cpu_pct'] = 100.0 - idle_pct
            report.append(f"⚡ CPU: **{data['cpu_pct']:.1f}%**")

        # Parsing RAM
        if mem_line:
            mem_m = re.search(r'(\d+\.?\d*)\s+total,\s+\d+\.?\d*\s+free,\s+(\d+\.?\d*)\s+used', mem_line)
            if mem_m:
                total_value = float(mem_m.group(1))
                used_value = float(mem_m.group(2))
                
                unit_multiplier = 1024 if 'kB' in mem_line else (1024 * 1024)

                data['ram_total'] = total_value * unit_multiplier
                data['ram_used'] = used_value * unit_multiplier
                
                if data['ram_total'] > 0:
                    data['ram_pct'] = (data['ram_used'] / data['ram_total']) * 100
                    report.append(f"🧠 RAM: **{data['ram_pct']:.0f}%** ({human_bytes(data['ram_used'])} / {human_bytes(data['ram_total'])})")

        # Parsing Uptime/Load
        if output_uptime:
            m = re.search(r'load average: (\d+\.\d+), (\d+\.\d+), (\d+\.\d+)', output_uptime)
            if m:
                data['load1'], data['load5'], data['load15'] = float(m.group(1)), float(m.group(2)), float(m.group(3))
            
            upt_m = re.search(r'up\s+(.*?)(?:,\s+\d+\s+user|$)|\d{2}:\d{2}:\d{2}\s+up\s+(.*)', output_uptime)
            if upt_m:
                uptime_str = next((g for g in upt_m.groups() if g is not None), None)
                if uptime_str:
                    data['uptime'] = uptime_str.replace('days', 'd').replace('day', 'd')

            load1_per_core = data['load1'] / data['cores']
            
            report.append(f"🚦 Load (1/5/15): {data['load1']:.2f} {data['load5']:.2f} {data['load15']:.2f} | P/Core: **{load1_per_core:.2f}**")
            report.append(f"⬆️ Uptime: {data['uptime']}")
        else:
             report.append("🚦 Load (1/5/15): n/a | P/Core: n/a")
             report.append("⬆️ Uptime: n/a")

    except Exception as e:
        logger.error(f"Error parsing remote data: {e}")
        report.append(f"⚠️ Remote hardware parsing error: {e}")
        
    return data, "\n".join(report)


async def get_remote_disk_status(mounts_to_check):
    """
    Executes 'df' on the remote node via SSH.
    Returns (formatted report text, list of alerts).
    """
    remote_mounts_str = ' '.join(mounts_to_check)
    command = f"df -P -k {remote_mounts_str}"
    output, error = await run_remote_command(command) 

    if error:
        return error, []
    
    lines = output.splitlines()
    if len(lines) < 2:
        return "⚠️ Remote DF error: Incomplete output (mount not found or SSH error)", []
    
    report_lines = []
    alert_data = [] 
    
    for line in lines[1:]: 
        parts = line.split()
        if len(parts) >= 6:
            mount_point = parts[5]
            try:
                usage_pct = int(parts[4].replace('%', ''))
                used = int(parts[2]) * 1024 
                total = int(parts[1]) * 1024 
            except ValueError:
                continue 
            
            # Formatting with emoji and bold text
            emoji = "💾" if mount_point == "/" else "🚀" # SD vs NVMe
            report_lines.append(f" - {emoji} {mount_point}: **{usage_pct}%** ({human_bytes(used)} / {human_bytes(total)})")
            
            if usage_pct >= DISK_THRESHOLD_PCT:
                alert_data.append((mount_point, usage_pct, used))
            
    report_text = "\n".join(report_lines) if report_lines else " - No remote mounts found."
    return report_text, alert_data

# ---------- Local System (Disabled) ----------
def get_system_status_detailed():
    return "Local hardware monitoring disabled."

# ---------- Alerts sender ----------
async def send_alert(app, text):
    logger.info(f"Sending Alert to Telegram: {text[:50]}...")
    try:
        # Use Markdown for formatting (bold)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error sending alert: {e}")

# ---------- Remote Monitoring Task ----------
async def monitor_system_task(app):
    # Persistent state and cooldown variables
    last_remote_disk_alert = {} 
    last_remote_cpu_alert = 0
    last_remote_ram_alert = 0
    last_remote_load_alert = 0
    last_lnd_service_status = "UNKNOWN"
    last_btc_service_status = "UNKNOWN"
    last_service_alert_ts = 0
    
    is_cpu_alert_active = False
    is_ram_alert_active = False
    is_load_alert_active = False
    
    cpu_high_count = 0
    ram_high_count = 0
    load_high_count = 0
    PERSISTENCE_COUNT = 3 

    cooldown = ALERT_COOLDOWN
    
    # Verified service names on MyNode
    LND_SERVICE_NAME = "lnd"
    BTC_SERVICE_NAME = "bitcoin"
    
    logger.info("Remote monitoring task started.")
    
    while True:
        now = time.time()
        try:
            # --- SECTION 1: Hardware Monitoring ---
            remote_data, remote_report_text = await get_remote_system_status() 
            
            if "error" not in remote_data: 
                
                # --- FALSE POSITIVE ELIMINATION LOGIC (CPU, RAM, LOAD) ---
                
                # 1a. CPU Monitoring
                if remote_data['cpu_pct'] >= CPU_THRESHOLD_PCT:
                    cpu_high_count += 1
                else:
                    cpu_high_count = 0
                    if is_cpu_alert_active:
                        await send_alert(app, f"✅ **ALARM RECOVERY:** Node CPU returned to **{remote_data['cpu_pct']:.0f}%** (below threshold).")
                        is_cpu_alert_active = False

                if cpu_high_count >= PERSISTENCE_COUNT and not is_cpu_alert_active and now - last_remote_cpu_alert > cooldown:
                    await send_alert(app, f"🚨 CPU NODE: **{remote_data['cpu_pct']:.0f}%** (High Threshold for {PERSISTENCE_COUNT} min!)")
                    is_cpu_alert_active = True
                    last_remote_cpu_alert = now
                
                
                # 1b. RAM Monitoring
                if remote_data['ram_pct'] >= RAM_THRESHOLD_PCT:
                    ram_high_count += 1
                else:
                    ram_high_count = 0
                    if is_ram_alert_active:
                        await send_alert(app, f"✅ **ALARM RECOVERY:** Node RAM returned to **{remote_data['ram_pct']:.0f}%** (below threshold).")
                        is_ram_alert_active = False

                if ram_high_count >= PERSISTENCE_COUNT and not is_ram_alert_active and now - last_remote_ram_alert > cooldown:
                    await send_alert(app, f"🚨 RAM NODE: **{remote_data['ram_pct']:.0f}%** (High Threshold for {PERSISTENCE_COUNT} min!)")
                    is_ram_alert_active = True
                    last_remote_ram_alert = now


                # 1c. LOAD Monitoring
                load1_per_core = remote_data['load1'] / remote_data['cores']
                if load1_per_core >= LOAD_PER_CORE_THRESHOLD:
                    load_high_count += 1
                else:
                    load_high_count = 0
                    if is_load_alert_active:
                        await send_alert(app, f"✅ **ALARM RECOVERY:** Node Load returned to **{remote_data['load1']:.2f}** (below threshold).")
                        is_load_alert_active = False

                if load_high_count >= PERSISTENCE_COUNT and not is_load_alert_active and now - last_remote_load_alert > cooldown:
                    await send_alert(app, f"⚠️ Load NODE: **{remote_data['load1']:.2f}** (P/Core {load1_per_core:.2f} High for {PERSISTENCE_COUNT} min!)")
                    is_load_alert_active = True
                    last_remote_load_alert = now

                
                # --- Disk Monitoring ---
                _, remote_alerts = await get_remote_disk_status(REMOTE_MOUNTS_TO_CHECK) 

                for mount, pct, used in remote_alerts:
                    key = f"remote_{mount}"
                    ts = last_remote_disk_alert.get(key, 0)
                    
                    if now - ts > cooldown:
                        await send_alert(app, f"⚠️ Disk NODE **{mount}**: **{pct}%** ({human_bytes(used)} used)")
                        last_remote_disk_alert[key] = now
            
            # --- SECTION 2: Service Status Monitoring ---
            
            SERVICE_STATUS_CMD = f"systemctl is-active {LND_SERVICE_NAME} {BTC_SERVICE_NAME}"
            output, error = await run_remote_command(SERVICE_STATUS_CMD, timeout=5)
            
            if not error and output:
                statuses = output.split() 
                
                # 1. Check LND
                current_lnd_status = statuses[0].upper() if len(statuses) > 0 else "UNKNOWN"
                if current_lnd_status != last_lnd_service_status and now - last_service_alert_ts > cooldown:
                    
                    if current_lnd_status == "INACTIVE" or current_lnd_status == "FAILED":
                        await send_alert(app, f"🔥 **SERVICE DOWN!** LND ({LND_SERVICE_NAME}) is now **{current_lnd_status}**.\nRun /diagnose and /restartlnd to resolve.")
                        last_service_alert_ts = now
                    elif current_lnd_status == "ACTIVE" and last_lnd_service_status != "UNKNOWN":
                        await send_alert(app, f"✅ **SERVICE UP!** LND ({LND_SERVICE_NAME}) returned to **{current_lnd_status}**.")
                        last_service_alert_ts = now 
                        
                    last_lnd_service_status = current_lnd_status
                
                # 2. Check Bitcoin Core
                current_btc_status = statuses[1].upper() if len(statuses) > 1 else "UNKNOWN"
                if current_btc_status != last_btc_service_status and now - last_service_alert_ts > cooldown:
                    
                    if current_btc_status == "INACTIVE" or current_btc_status == "FAILED":
                        await send_alert(app, f"🔥 **SERVICE DOWN!** Bitcoin ({BTC_SERVICE_NAME}) is now **{current_btc_status}**.\nRun /diagnose and /restartbtc to resolve.")
                        last_service_alert_ts = now
                    elif current_btc_status == "ACTIVE" and last_btc_service_status != "UNKNOWN":
                        await send_alert(app, f"✅ **SERVICE UP!** Bitcoin ({BTC_SERVICE_NAME}) returned to **{current_btc_status}**.")
                        last_service_alert_ts = now 
                        
                    last_btc_service_status = current_btc_status
                
        except Exception as e:
            logger.error(f"[monitor_system_task] General error: {e}")

        await asyncio.sleep(SYS_CHECK_INTERVAL)

# ---------- LND Monitoring Task ----------
async def monitor_lnd_task(app):
    prev_peers = set()
    prev_channels = set()
    prev_settled_invoices = set()
    
    logger.info("LND monitoring task started.")

    while True:
        try:
            try:
                client = get_lnd_client()
            except Exception as e:
                await send_alert(app, f"⚠️ LND client error: {e}")
                await asyncio.sleep(LND_CHECK_INTERVAL)
                continue
            
            # Peers check...
            try:
                peers = client.list_peers().peers
                cur_peers = set(getattr(p, "pub_key", str(p)) for p in peers)
                new = cur_peers - prev_peers
                removed = prev_peers - cur_peers
                for pk in new:
                    alias = get_alias_for_pubkey(client, pk)
                    logger.info(f"New Peer Connected: {alias or pk}")
                    await send_alert(app, f"🔗 New Peer Connected: **{alias or pk}**")
                for pk in removed:
                    alias = get_alias_for_pubkey(client, pk)
                    logger.info(f"Peer Disconnected: {alias or pk}")
                    await send_alert(app, f"❌ Peer Disconnected: **{alias or pk}**")
                prev_peers = cur_peers
            except Exception as e:
                await send_alert(app, f"⚠️ LND peers error: {e}")

            # Channels check...
            try:
                channels = client.list_channels().channels
                cur_ch = set(getattr(c, "channel_point", "") for c in channels)
                newch = cur_ch - prev_channels
                closed = prev_channels - cur_ch
                for cp in newch:
                    # CORREZIONE: Recupera l'alias del partner del canale
                    remote_pubkey = next((c.remote_pubkey for c in channels if c.channel_point == cp), None)
                    alias = get_alias_for_pubkey(client, remote_pubkey) if remote_pubkey else None
                    display_name = alias or remote_pubkey[:10] + '...'

                    logger.info(f"Channel Opened: {cp}")
                    await send_alert(app, f"🔔 Channel Opened with **{display_name}**")
                for cp in closed:
                    logger.info(f"Channel Closed: {cp}")
                    await send_alert(app, f"🔕 Channel Closed: **{cp}**")
                prev_channels = cur_ch
            except Exception as e:
                await send_alert(app, f"⚠️ LND channels error: {e}")

            # Invoices check...
            try:
                invoices = client.list_invoices().invoices
                cur_settled = set()
                # CORREZIONE: Ottenere il valore in sats e formattare l'alert
                for inv in invoices:
                    settled = getattr(inv, "settled", False)
                    if settled:
                        # Usiamo r_hash_str come chiave per lo stato
                        r_hash = getattr(inv, "r_hash_str", None) or str(getattr(inv, "add_index", ""))
                        value_sats = getattr(inv, "value", 0) 
                        cur_settled.add((r_hash, value_sats)) 
                
                # Calcola le nuove fatture saldate
                new_settled = [ (rh, val) for rh, val in cur_settled if rh not in set(rh_prev for rh_prev, val_prev in prev_settled_invoices) ]
                removed_settled = [ (rh, val) for rh, val in prev_settled_invoices if rh not in set(rh_curr for rh_curr, val_curr in cur_settled) ]

                for rh, value_sats in new_settled:
                    logger.info(f"Invoice Settled: {rh} for {value_sats} sats")
                    # INVIO ALERT CON VALORE CORRETTO E TRADOTTO
                    await send_alert(app, f"💰 Invoice Settled: **{value_sats:,} sats**")
                
                # Aggiorna lo stato dei pagamenti
                prev_settled_invoices = cur_settled
            except Exception as e:
                await send_alert(app, f"⚠️ LND invoices error: {e}")

        except Exception as e:
            logger.error(f"[monitor_lnd_task] General error: {e}")

        await asyncio.sleep(LND_CHECK_INTERVAL)

# ---------- Telegram Commands ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Command /start received from {update.effective_user.id}")
    text = (
        "👋 Hi! I'm NodeSentinel — now active.\n"
        "I monitor Bitcoin, LND, and your remote NODE hardware.\n\n"
        "I also provide proactive crypto market analysis."
    )
    await update.message.reply_text(text)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Command /status received from {update.effective_user.id}")
    btc_status = get_bitcoin_status()
    try:
        lnd_raw = get_lnd_status_text()
    except Exception as e:
        lnd_raw = f"LND error: {e}"
    
    # Remote Hardware Status
    remote_data, remote_system_report = await get_remote_system_status() 
    
    # Remote Disk Status
    remote_disk_report, _ = await get_remote_disk_status(REMOTE_MOUNTS_TO_CHECK) 
    
    # Simplified LND parsing
    lnd_lines = lnd_raw.splitlines()
    if len(lnd_lines) >= 4 and "error" not in lnd_raw.lower():
         lnd_report = (
            f"⚡ LND Status\n"
            f"Alias: **{lnd_lines[0].split(': ')[1]}**\n"
            f"Synced: {lnd_lines[1].split(': ')[1]}\n"
            f"Peers: **{lnd_lines[2].split(': ')[1]}**\n"
            f"Balance: **{lnd_lines[3].split(': ')[1]}**"
        )
    else:
        lnd_report = lnd_raw

    final_message = (
        f"{btc_status}\n\n"
        f"{lnd_report}\n\n"
        f"--- 💾 Hardware NODE (Remote) ---\n"
        f"{remote_system_report}\n"
        f"Disks:\n"
        f"{remote_disk_report}"
    )
    
    await update.message.reply_text(final_message, parse_mode='Markdown')


def get_lnd_status_text():
    client = get_lnd_client()
    info = client.get_info()
    balance = client.wallet_balance()
    peers = client.list_peers()
    
    # Returns raw lines for cleaner report parsing
    return (
        f"LND alias: {getattr(info,'alias', 'n/a')}\n"
        f"Synced: {getattr(info,'synced_to_chain', 'n/a')}\n"
        f"Peers: {len(getattr(peers,'peers', []))}\n"
        f"Balance: {getattr(balance,'total_balance', 0)} sats"
    )

async def diagnose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Command /diagnose received from {update.effective_user.id}")
    
    LND_STATUS_CMD = "systemctl status lnd | grep 'Active:'"
    BTC_STATUS_CMD = "systemctl status bitcoin | grep 'Active:'"
    
    lnd_output, lnd_error = await run_remote_command(LND_STATUS_CMD, timeout=10)
    btc_output, btc_error = await run_remote_command(BTC_STATUS_CMD, timeout=10)
    
    report = f"🩺 **Remote Node Services Diagnostics**\n"
    report += "---------------------------------------\n"
    
    if lnd_error:
        report += f"⚡ LND Status: {lnd_error}\n"
    else:
        status_line = lnd_output.replace('Active:', '').strip()
        report += f"⚡ LND Status: **{status_line}**\n"
        
    if btc_error:
        report += f"₿ Bitcoin Status: {btc_error}\n"
    else:
        status_line = btc_output.replace('Active:', '').strip()
        report += f"₿ Bitcoin Status: **{status_line}**\n"

    await update.message.reply_text(report, parse_mode='Markdown')


async def restartbtc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Command /restartbtc received from {update.effective_user.id}")
    
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text("🚨 **ACCESS DENIED:** This command can only be run by the bot owner.", parse_mode='Markdown')
        logger.warning(f"Unauthorized Bitcoin restart attempt by ID: {update.effective_user.id}")
        return

    await update.message.reply_text("⏳ Restarting Bitcoin Core service via SSH...", parse_mode='Markdown')
    RESTART_CMD = "sudo systemctl restart bitcoin"
    
    output, error = await run_remote_command(RESTART_CMD, timeout=30) 
    
    if error:
        final_msg = f"❌ **RESTART FAILED!** Check node logs. Error: {error}"
    else:
        final_msg = "✅ **BITCOIN CORE RESTARTED!** Check status with /diagnose in a few seconds."
        
    await update.message.reply_text(final_msg, parse_mode='Markdown')

async def restartlnd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Command /restartlnd received from {update.effective_user.id}")
    
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text("🚨 **ACCESS DENIED:** This command can only be run by the bot owner.", parse_mode='Markdown')
        logger.warning(f"Unauthorized LND restart attempt by ID: {update.effective_user.id}")
        return

    await update.message.reply_text("⏳ Restarting LND service via SSH...", parse_mode='Markdown')
    RESTART_CMD = "sudo systemctl restart lnd"
    
    output, error = await run_remote_command(RESTART_CMD, timeout=30) 
    
    if error:
        final_msg = f"❌ **RESTART FAILED!** Check status with /diagnose. Error: {error}"
    else:
        final_msg = "✅ **LND RESTARTED!** Check status with /diagnose in a few seconds."
        
    await update.message.reply_text(final_msg, parse_mode='Markdown')

async def mempool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /mempool ricevuto da {update.effective_user.id}")
    fees, error = await crypto_utils.get_fee_data()
    
    if error:
        await update.message.reply_text(error)
        return

    report = (
        f"⛽ Tariffe Mempool Attuali (sat/vB):\n"
        f"- Veloce (Next block): **{fees.get('fastestFee', 'n/d')}**\n"
        f"- Media (30 min): **{fees.get('halfHourFee', 'n/d')}**\n"
        f"- Bassa (1 hr): **{fees.get('hourFee', 'n/d')}**"
    )
    
    await update.message.reply_text(report, parse_mode='Markdown')


async def btcinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /btcinfo ricevuto da {update.effective_user.id}")
    
    info, error = await crypto_utils.get_onchain_info()
    
    if error:
        await update.message.reply_text(error)
        return

    raw_difficulty = info['difficulty'] if info['difficulty'] is not None else 0
    formatted_difficulty = raw_difficulty / 1_000_000_000_000
    
    ts = info['next_adjustment_ts']
    adj_date = time.strftime('%d-%m-%Y %H:%M UTC', time.localtime(ts))
    
    report = (
        f"📊 **Analisi On-Chain Bitcoin**\n"
        f"----------------------------------------\n"
        f"⚒️ Difficoltà Attuale: **{formatted_difficulty:.2f} T**\n"
        f"⚙️ Progresso Aggiustamento: **{info['adjustment_progress']:.2f}%**\n"
        f"🧱 Blocchi Rimanenti: **{info['blocks_remaining']}**\n"
        f"⏳ Stima Prossimo Aggiustamento: *{adj_date}*\n"
    )
    
    if info['adjustment_progress'] > 80:
        report += "\n🔔 *Nota: L'aggiustamento della difficoltà è imminente (oltre l'80%).*"

    await update.message.reply_text(report, parse_mode='Markdown')

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /price ricevuto da {update.effective_user.id}")
    
    currency = context.args[0] if context.args else "EUR" 
    prices, error = await crypto_utils.get_price_data(currency=currency)
    
    if error or not prices:
        await update.message.reply_text(f"⚠️ Errore nel recupero dei prezzi: {error or 'Dati mancanti'}")
        return

    report = (
        f"💸 Prezzi Attuali di Bitcoin:\n"
        f"- BTC/EUR: **€{prices.get('eur', 0):,.2f}**\n"
        f"- BTC/USD: **${prices.get('usd', 0):,.2f}**"
    )
    
    await update.message.reply_text(report, parse_mode='Markdown')


async def peers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /peers ricevuto da {update.effective_user.id}")
    
    def escape_html(text):
        if text is None:
            return ""
        return (text.replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;'))
        
    try:
        client = get_lnd_client()
        peers = client.list_peers().peers
        if not peers:
            await update.message.reply_text("Nessun peer connesso")
            return
            
        lines = []
        for p in peers:
            pk = getattr(p, "pub_key", None)
            addr = getattr(p, "address", "")
            alias = get_alias_for_pubkey(client, pk) 
            
            # Sanifica tutti gli elementi dinamici e usa l'HTML per il grassetto (<b>)
            display_alias = alias or pk[:10] + '...'
            safe_alias = escape_html(display_alias)
            safe_pk = escape_html(pk)
            safe_addr = escape_html(addr)
            
            lines.append(f"- <b>{safe_alias}</b> ({safe_pk}) {safe_addr}")
        
        msg = "🔗 Peers attivi:\n" + "\n".join(lines)
        
        # Send in HTML mode
        await update.message.reply_text(msg, parse_mode='HTML') 
        
    except Exception as e:
        logger.error(f"Errore comando /peers: {e}")
        await update.message.reply_text(f"⚠️ LND error: {e}")

async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /channels ricevuto da {update.effective_user.id}")
    try:
        client = get_lnd_client()
        channels = client.list_channels().channels
        if not channels:
            await update.message.reply_text("Nessun canale aperto")
            return
        msg = "🔔 Canali Aperti:\n" + "\n".join(f"- **{getattr(c,'remote_pubkey','?')[:10] + '...'}** ({getattr(c,'capacity','?')} sats)" for c in channels)
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Errore comando /channels: {e}")
        await update.message.reply_text(f"⚠️ LND error: {e}")

async def invoices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /invoices ricevuto da {update.effective_user.id}")
    try:
        client = get_lnd_client()
        invoices = client.list_invoices().invoices
        if not invoices:
            await update.message.reply_text("Nessuna invoice")
            return
        last = invoices[-10:]
        msg = "💰 Ultime Invoice (max 10):\n"
        for i in last:
            memo = getattr(i, "memo", "")
            value = getattr(i, "value", 0)
            settled = getattr(i, "settled", False)
            status_emoji = "✅" if settled else "⏳"
            msg += f"- *{memo}* : **{value} sats** - Status: {status_emoji}\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Errore comando /invoices: {e}")
        await update.message.reply_text(f"⚠️ LND error: {e}")

async def hardware_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /hardware ricevuto da {update.effective_user.id}")
    # Stato Hardware Remoto del NODO (CPU/RAM/Load)
    remote_data, remote_system_report = await get_remote_system_status() 
    
    # Stato Dischi Remoto del NODO
    remote_disk_report, _ = await get_remote_disk_status(REMOTE_MOUNTS_TO_CHECK) 
    
    final_message = (
        f"--- 💾 Hardware NODO Bitcoin (Remoto) ---\n"
        f"{remote_system_report}\n"
        f"Dischi:\n"
        f"{remote_disk_report}"
    )
    await update.message.reply_text(final_message, parse_mode='Markdown')


async def netscan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Comando /netscan ricevuto da {update.effective_user.id}")
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("⚠️ Uso: /netscan <subnet_base> (Es: /netscan 10.21.10)", parse_mode='Markdown')
        return
        
    subnet_base = context.args[0].strip()
    
    await update.message.reply_text(f"⏳ Avvio scansione ping leggera sulla subnet **{subnet_base}.x** (Potrebbe richiedere fino a 2 minuti)...", parse_mode='Markdown')

    # SSH Scan Command (ping sweep from the remote node)
    SCAN_CMD = (
        f"for i in {{1..254}}; do (ping -c 1 -W 1 {subnet_base}.$i | grep 'bytes from' | awk '{{print \"Host up: \" $4}}' &); done; wait"
    )
    
    output, error = await run_remote_command(SCAN_CMD, timeout=120) 

    if error:
        report = f"❌ **SCAN FALLITO!** Errore durante l'esecuzione SSH: {error}"
    elif not output:
        report = f"🔍 **SCAN COMPLETATO:** Nessun host attivo rilevato sulla subnet **{subnet_base}.x**."
    else:
        active_hosts = output.replace('Host up: ', '- ').replace(':', '').splitlines()
        report = f"✅ **SCAN COMPLETATO:** Rilevati **{len(active_hosts)}** host attivi su **{subnet_base}.x**:\n" + "\n".join(active_hosts)
        
    await update.message.reply_text(report, parse_mode='Markdown')


async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    logger.info("Bot Telegram avviato.") 

    await app.bot.set_my_commands([
        BotCommand("start", "Avvia il bot"),
        BotCommand("status", "Mostra lo stato del nodo"), 
        BotCommand("peers", "Lista dei peers"),
        BotCommand("channels", "Lista dei canali"),
        BotCommand("invoices", "Ultime invoice"),
        BotCommand("hardware", "Stato hardware del NODO"),
        
        # MONITORAGGIO E MERCATO
        BotCommand("mempool", "Tariffe transazione BTC (sat/vB)"),
        BotCommand("price", "Prezzo BTC in EUR/USD (es. /price USD)"),
        BotCommand("btcinfo", "Analisi Difficoltà e Aggiustamento Bitcoin"),
        
        # DIAGNOSI E AZIONI REMOTE
        BotCommand("diagnose", "Stato dei servizi LND e Bitcoin Core"), 
        BotCommand("restartlnd", "Riavvia il servizio LND (Solo Admin)"),
        BotCommand("restartbtc", "Riavvia Bitcoin Core (Solo Admin)"),
        BotCommand("netscan", "Scansione di rete locale")
    ])

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("peers", peers_cmd))
    app.add_handler(CommandHandler("channels", channels_cmd))
    app.add_handler(CommandHandler("invoices", invoices_cmd))
    app.add_handler(CommandHandler("hardware", hardware_cmd))
    app.add_handler(CommandHandler("netscan", netscan_cmd))
    
    # REGISTRAZIONE NUOVI HANDLER
    app.add_handler(CommandHandler("mempool", mempool_cmd)) 
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("btcinfo", btcinfo_cmd))
    app.add_handler(CommandHandler("diagnose", diagnose_cmd))
    app.add_handler(CommandHandler("restartlnd", restartlnd_cmd))
    app.add_handler(CommandHandler("restartbtc", restartbtc_cmd)) 

    loop = asyncio.get_running_loop()
    loop.create_task(monitor_system_task(app))
    loop.create_task(monitor_lnd_task(app))
    loop.create_task(crypto_utils.surveillance_task(app))

    print("=== NodeSentinel Live & Full Running ===")
    logger.info("Avvio del polling...")
    await app.run_polling()

# ---------- Run ----------
if __name__ == "__main__":
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except Exception:
        pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        pass
