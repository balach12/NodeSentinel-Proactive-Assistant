# crypto_utils.py
import httpx
import logging
import time
import os
import re
import asyncio
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger(__name__)

# --- CONFIGURATION (Reads from OS Environment) ---
COINGECKO_RATE_LIMIT_DELAY = 10 
MEMPOOL_API_URL = "https://mempool.space/api/v1/fees/recommended"
PRICE_API_URL = "https://api.coingecko.com/api/v3/simple/price"

# --- GLOBAL STATE ---
FEE_HISTORY = []
PRICE_HISTORY = []
LAST_VOLATILITY_REFERENCE_USD = 0.0
LAST_MACRO_REPORT_TS = 0.0 


# --- UTILITY FUNCTIONS ---

def update_fee_history(new_fees: Dict[str, Any]):
    """Updates fee history (keeps last hour)."""
    global FEE_HISTORY 
    
    new_fees['time'] = time.time()
    FEE_HISTORY.append(new_fees)
    
    one_hour_ago = time.time() - 3600
    FEE_HISTORY = [d for d in FEE_HISTORY if d['time'] > one_hour_ago]

def update_price_history(new_prices: Dict[str, Any]):
    """Updates price history (keeps last 4 hours for 120 min analysis)."""
    global PRICE_HISTORY 
    
    new_prices['time'] = time.time()
    PRICE_HISTORY.append(new_prices)
    
    four_hours_ago = time.time() - (4 * 3600)
    PRICE_HISTORY = [d for d in PRICE_HISTORY if d['time'] > four_hours_ago]

# --- ASYNC DATA RETRIEVAL ---

async def get_fee_data() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Retrieves recommended transaction fees (sat/vB)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(MEMPOOL_API_URL)
            response.raise_for_status()
            fees = response.json()
            return fees, None
    except Exception as e:
        logger.error(f"Mempool retrieval error: {e}")
        return None, f"⚠️ Mempool API Error: {e}"

async def get_price_data(currency="eur") -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """Retrieves BTC price in EUR and USD."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            params = {"ids": "bitcoin", "vs_currencies": "eur,usd"} 
            
            response = await client.get(PRICE_API_URL, params=params)
            
            if response.status_code == 429:
                logger.warning(f"CoinGecko Rate Limit hit (429). Delaying by {COINGECKO_RATE_LIMIT_DELAY}s.")
                await asyncio.sleep(COINGECKO_RATE_LIMIT_DELAY) 
                response.raise_for_status() 
            
            response.raise_for_status()
            price_data = response.json().get('bitcoin', {})
            
            eur_price = price_data.get('eur')
            usd_price = price_data.get('usd')
            
            if eur_price and usd_price:
                 return {"eur": eur_price, "usd": usd_price}, None

            return None, "⚠️ Incomplete price data."

    except Exception as e:
        logger.error(f"Error retrieving BTC price: {e}")
        return None, f"⚠️ Price API Error: {e}"

async def get_onchain_info() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Retrieves mining difficulty and next adjustment estimate."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://mempool.space/api/v1/difficulty-adjustment", timeout=15)
            response.raise_for_status()
            data = response.json()
            
            return {
                "difficulty": data.get('difficulty'),
                "blocks_remaining": data.get('remainingBlocks'),
                "next_adjustment_ts": data.get('estimateRetargetDate'),
                "adjustment_progress": data.get('progressPercent')
            }, None
    except Exception as e:
        logger.error(f"Error retrieving On-Chain info: {e}")
        return None, f"⚠️ On-Chain API Error: {e}"


async def perform_contextual_analysis(query: str) -> Tuple[str, Optional[str]]:
    """
    Executes a market context analysis via Gemini LLM with web search (Grounding).
    """
    logger.info(f"Executing LLM contextual analysis for: {query}")
    
    # API Key is read from environment variable 'GEMINI_API_KEY'
    apiKey = os.getenv("GEMINI_API_KEY") 
    apiUrl = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key=" + apiKey;
    
    systemPrompt = "Act as a senior financial market analyst specializing in Bitcoin. Use the search tool to find the most relevant news of the last few hours that explains this price movement and summarize it in a maximum of two paragraphs. Focus exclusively on: FED/rates/inflation, Dollar Index (DXY) trend, ETF flows/institutional adoption, and Gold/BTC correlation. EXCLUDE direct geopolitical tensions."
    
    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}], 
        "systemInstruction": {"parts": [{"text": systemPrompt}]},
    };

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(apiUrl, json=payload)
            
            if response.status_code == 403:
                return "🔎 *Contextual Analysis Failed: Invalid API Key (403 Forbidden).* Check GEMINI_API_KEY in .env.", "403 Forbidden"
            
            response.raise_for_status()
            
            result = response.json()
            
            text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', 'Analysis not available.')
            
            sources = []
            grounding_metadata = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('groundingMetadata')
            if grounding_metadata and grounding_metadata.get('groundingAttributions'):
                 sources = [f"[{i+1}] {a['web']['title']}" for i, a in enumerate(grounding_metadata['groundingAttributions']) if a.get('web')]


            context = (
                f"🧠 *AI Contextual Analysis (Gemini)*:\n"
                f"{text}"
            )
            
            if sources:
                context += "\n\nSources Used:\n" + "\n".join(sources[:3])


            return context, None
        
    except Exception as e:
        logger.error(f"Error during LLM context retrieval: {e}")
        return "🔎 *Contextual Analysis Failed: API or Network Error.*", f"Error: {e}"

# --- PROACTIVE SURVEILLANCE TASK (The Brain) ---
async def surveillance_task(app):
    """
    Task that monitors BTC Fees/Price for sudden changes based on state and content changes.
    """
    logger.info("Proactive crypto surveillance task started.")
    
    # Global state variables needed for assignment
    global LAST_VOLATILITY_REFERENCE_USD 
    global LAST_MACRO_REPORT_TS 

    # Local timestamp variables
    last_fee_alert_ts = 0
    last_price_volatility_alert_ts = 0 
    current_fee_state = "INIT" # Initialization state (silent)
    

    # Import thresholds from nodesentinel.py
    from nodesentinel import (
        ALERT_COOLDOWN, send_alert, FEE_LOW_THRESHOLD, FEE_HIGH_THRESHOLD, FEE_MED_THRESHOLD, 
        PRICE_CHANGE_THRESHOLD_LOW, PRICE_CHANGE_THRESHOLD_HIGH
    ) 

    SURVEILLANCE_INTERVAL = 300 # 5 minutes
    
    PRICE_VOLATILITY_COOLDOWN = 4 * 3600 # 4 hours
    MACRO_REPORT_COOLDOWN = 24 * 3600 # 86400 seconds


    while True:
        now = time.time()
        
        # --- 1. Fee Surveillance (Stateful) ---
        fees, fee_error = await get_fee_data()
        
        if fees:
            update_fee_history(fees)
            current_fast_fee = fees.get('fastestFee', 0)
            
            new_fee_state = "NORMAL"
            
            # State Logic (LOW <= 5, MEDIUM 6-10, HIGH > 10)
            if current_fast_fee > FEE_MED_THRESHOLD: 
                new_fee_state = "HIGH"
            elif current_fast_fee <= FEE_LOW_THRESHOLD: 
                new_fee_state = "LOW"
            elif current_fast_fee > FEE_LOW_THRESHOLD and current_fast_fee <= FEE_MED_THRESHOLD:
                 new_fee_state = "MEDIUM"
            
            # Alert Logic: Only notify if state changes AND not in INIT state
            if current_fee_state != "INIT" and new_fee_state != current_fee_state and now - last_fee_alert_ts > ALERT_COOLDOWN:
                
                if new_fee_state == "HIGH":
                    await send_alert(app, f"🚨 **STATE CHANGE! HIGH FEES!** ({current_fast_fee} sat/vB). **Avoid non-urgent on-chain transactions**.")
                
                elif new_fee_state == "MEDIUM":
                    await send_alert(app, f"🔶 **STATE CHANGE! NORMAL FEES!** ({current_fast_fee} sat/vB). Fees are acceptable, but not cheap.")
                
                elif new_fee_state == "LOW":
                    await send_alert(app, f"⬇️ **STATE CHANGE! LOW FEES!** ({current_fast_fee} sat/vB). **Excellent time for on-chain consolidation** or opening channels.")
                
                elif new_fee_state == "NORMAL": 
                     if current_fee_state != "NORMAL": 
                        await send_alert(app, f"✅ **STATE CHANGE! FEES NORMALIZED!** ({current_fast_fee} sat/vB).")
                
                current_fee_state = new_fee_state
                last_fee_alert_ts = now
            
            # First Run Logic: Initialize state without sending an alert
            elif current_fee_state == "INIT":
                current_fee_state = new_fee_state
                LAST_MACRO_REPORT_TS = now # Set macro timer to NOW to block immediate report
                logger.info(f"Fee state and Macro timer initialized to {current_fee_state}. No alert sent on first run.")


        # --- 2. BTC Price Surveillance (Absolute USD Change) ---
        prices, price_error = await get_price_data(currency="EUR")

        if prices:
            update_price_history(prices)
            current_price_usd = prices.get('usd', 0) 
            
            price_history = PRICE_HISTORY
            one_hundred_twenty_minutes_ago = now - (120 * 60) 
            old_price_data = next((d for d in price_history if d['time'] < one_hundred_twenty_minutes_ago), None)
            
            if old_price_data and current_price_usd > 0 and old_price_data.get('usd', 0) > 0:
                old_price_usd = old_price_data['usd']
                
                change_abs_usd = current_price_usd - old_price_usd
                change_pct = (change_abs_usd / old_price_usd) * 100
                
                # --- VOLATILITY ANTI-SPAM LOGIC ---
                
                # 1. Reset check: If price returns near the reference point (within 50% of min threshold)
                if LAST_VOLATILITY_REFERENCE_USD != 0.0 and abs(current_price_usd - LAST_VOLATILITY_REFERENCE_USD) < (PRICE_CHANGE_THRESHOLD_LOW / 2):
                    LAST_VOLATILITY_REFERENCE_USD = 0.0
                    logger.info("Volatility reference reset. New alarm possible.")
                
                
                is_significant_change = abs(change_abs_usd) >= PRICE_CHANGE_THRESHOLD_LOW
                
                # 2. Alert conditions: (1) Significant, (2) Cooldown passed (4 hours), AND (3) State is reset (not in active alert).
                is_alert_needed = (
                    current_fee_state != "INIT" and 
                    is_significant_change and 
                    now - last_price_volatility_alert_ts > PRICE_VOLATILITY_COOLDOWN and 
                    LAST_VOLATILITY_REFERENCE_USD == 0.0
                )
                
                
                if is_alert_needed:
                    
                    if change_abs_usd > 0:
                        magnitude = "MAJOR" if change_abs_usd >= PRICE_CHANGE_THRESHOLD_HIGH else "SIGNIFICANT"
                        alert_text = (
                            f"🚀 **{magnitude} IMPETUS!** BTC rose by **${change_abs_usd:,.0f}** ({change_pct:.1f}%) "
                            f"to **${current_price_usd:,.0f}** in 2 hours. Assistant is seeking the news..."
                        )
                        analysis_query = f"BTC rose by ${change_abs_usd:,.0f} ({change_pct:.1f}%) in two hours. Analyze the causes."
                    else:
                        magnitude = "MAJOR" if abs(change_abs_usd) >= PRICE_CHANGE_THRESHOLD_HIGH else "SIGNIFICANT"
                        alert_text = (
                            f"🚨 **{magnitude} CRASH!** BTC fell by **${-change_abs_usd:,.0f}** ({-change_pct:.1f}%) "
                            f"to **${current_price_usd:,.0f}** in 2 hours. Assistant is seeking the cause..."
                        )
                        analysis_query = f"BTC fell by ${-change_abs_usd:,.0f} ({-change_pct:.1f}%) in two hours. Analyze the causes."

                    # 1. Initial Alert
                    await send_alert(app, alert_text)
                    
                    # 2. Contextual Analysis (LLM)
                    context, search_error = await perform_contextual_analysis(analysis_query)
                    
                    # 3. Follow-up Alert with Context
                    await send_alert(app, context)
                    
                    # REGISTER THE STARTING POINT AND DEDICATED COOLDOWN
                    LAST_VOLATILITY_REFERENCE_USD = current_price_usd
                    last_price_volatility_alert_ts = now 

        # --- 3. Macro News Surveillance (Periodic Analysis) ---
        
        # Report macro periodic (once every 24 hours)
        if current_fee_state != "INIT" and now - LAST_MACRO_REPORT_TS > MACRO_REPORT_COOLDOWN: 
            
            logger.info(f"Executing macro surveillance (periodic, 24h).")
            
            analysis_query = "Provide the periodic macro report as per system instructions."
            context, error = await perform_contextual_analysis(analysis_query)
            
            # NOVELTY FILTER
            if "Contextual Analysis Failed" not in context and "NESSUN AGGIORNAMENTO MACRO SIGNIFICATIVO" not in context:
                 await send_alert(app, f"📰 **PERIODIC MACRO REPORT:**\n{context}")
            else:
                 logger.info("Macro report skipped: Non-significant content or error.")

            # Reset the macro timer
            LAST_MACRO_REPORT_TS = now 
            
        await asyncio.sleep(SURVEILLANCE_INTERVAL)
