import os
import sys
import time
import asyncio
import uuid
import aiohttp

# Ensure local directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from solver import ReCaptchaSolver
from simple_logger import Logger

logger = Logger()

# Configuration from environment variables
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:11208").rstrip('/')
WORKER_ID = os.environ.get("WORKER_ID", f"gha-worker-{uuid.uuid4().hex[:8]}")
MAX_RUN_TIME = int(os.environ.get("MAX_RUN_TIME", "3300"))  # Default 55 minutes execution

# Persistent warm browser instance
_browser_instance = None
_current_browser_config = {"headless": None, "proxy": None}

async def get_warm_browser(headless: bool, proxy: dict) -> any:
    """
    Retrieve or launch the shared Playwright browser instance.
    If the requested proxy/headless config changes, it automatically destroys 
    the existing browser and launches a new one with the target settings.
    """
    global _browser_instance, _current_browser_config
    
    # Check if configurations changed
    config_changed = (_current_browser_config["headless"] != headless or 
                      _current_browser_config["proxy"] != proxy)
                      
    if _browser_instance is not None and (config_changed or not _browser_instance.is_connected()):
        logger.info(f"[{WORKER_ID}] Re-initializing warm browser instance due to config change/disconnect.")
        try:
            await _browser_instance.close()
        except Exception:
            pass
        _browser_instance = None
        
    if _browser_instance is None:
        logger.info(f"[{WORKER_ID}] Launching new warm browser. Headless: {headless}, Proxy: {proxy}")
        from cloakbrowser import launch_async
        
        pw_proxy = None
        if proxy:
            if isinstance(proxy, str):
                pw_proxy = proxy
            elif isinstance(proxy, dict) and proxy.get("server"):
                pw_proxy = {"server": proxy["server"]}
                if proxy.get("username"):
                    pw_proxy["username"] = proxy["username"]
                if proxy.get("password"):
                    pw_proxy["password"] = proxy["password"]
                    
        _browser_instance = await launch_async(
            headless=headless,
            proxy=pw_proxy,
            geoip=True if pw_proxy else False,
            humanize=True
        )
        _current_browser_config = {"headless": headless, "proxy": proxy}
        
    return _browser_instance

async def process_job_async(job: dict, ws_client=None):
    """Solve the captcha job and submit the result back to the gateway (supporting WS or HTTP)."""
    job_id = job["job_id"]
    url = job["url"]
    site_key = job.get("sitekey")
    proxy = job.get("proxy")
    headless = job.get("headless", True)
    
    logger.info(f"[{WORKER_ID}] Processing job {job_id} for URL: {url}")
    start_solve = time.time()
    
    try:
        # Get/maintain the warm browser instance (instant tab creation)
        browser = await get_warm_browser(headless=headless, proxy=proxy)
        
        # Solve the recaptcha (passing the warm browser object)
        token = await ReCaptchaSolver.solve_async(
            url=url,
            site_key=site_key,
            proxy=proxy,
            headless=headless,
            debug=True,
            browser=browser
        )
        
        logger.success(f"[{WORKER_ID}] Solved job {job_id} in {time.time() - start_solve:.2f}s!")
        
        # Submit the result
        if ws_client and not ws_client.closed:
            await ws_client.send_json({
                "action": "submit_result",
                "job_id": job_id,
                "status": "completed",
                "token": token
            })
            logger.info(f"[{WORKER_ID}] Result submitted via WebSocket for job {job_id}")
        else:
            submit_url = f"{GATEWAY_URL}/submitResult/{job_id}"
            async with aiohttp.ClientSession() as session:
                async with session.post(submit_url, json={"status": "completed", "token": token}, timeout=10) as res:
                    if res.status == 200:
                        logger.info(f"[{WORKER_ID}] Result submitted via HTTP for job {job_id}")
                    else:
                        logger.error(f"[{WORKER_ID}] HTTP result submit failed: HTTP {res.status}")
                        
    except Exception as e:
        logger.failure(f"[{WORKER_ID}] Failed to solve job {job_id}: {e}")
        
        # Submit failure status
        if ws_client and not ws_client.closed:
            await ws_client.send_json({
                "action": "submit_result",
                "job_id": job_id,
                "status": "failed",
                "error": str(e)
            })
        else:
            submit_url = f"{GATEWAY_URL}/submitResult/{job_id}"
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(submit_url, json={"status": "failed", "error": str(e)}, timeout=10)
            except Exception as ex:
                logger.error(f"[{WORKER_ID}] Critical error submitting failure status: {ex}")

async def run_websocket_loop(ws_url: str) -> bool:
    """Connect to the gateway using WebSockets for ultra-low latency push delivery."""
    logger.info(f"[{WORKER_ID}] Connecting to WebSocket Gateway: {ws_url}")
    
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, timeout=10) as ws:
            # Active WebSocket connection registered!
            logger.message("WORKER", f"Connected via WebSocket. Idle and listening for job dispatches...")
            
            # Start background heartbeat ping task
            async def heartbeat():
                while not ws.closed:
                    try:
                        await ws.send_json({"action": "ping"})
                    except Exception:
                        break
                    await asyncio.sleep(25)
                    
            hb_task = asyncio.create_task(heartbeat())
            
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = msg.json()
                        action = data.get("action")
                        
                        if action == "solve":
                            # Process job in the active loop (non-blocking scheduler)
                            asyncio.create_task(process_job_async(data, ws))
                            
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                hb_task.cancel()
                
    return True

async def run_http_polling_fallback():
    """Fallback HTTP Polling in case WebSocket is blocked/unreachable."""
    logger.warning(f"[{WORKER_ID}] Falling back to HTTP polling loop...")
    poll_interval = 2 # Poll faster on fallback
    start_time = time.time()
    
    async with aiohttp.ClientSession() as session:
        while time.time() - start_time < MAX_RUN_TIME:
            try:
                poll_url = f"{GATEWAY_URL}/requestJob?worker_id={WORKER_ID}"
                async with session.get(poll_url, timeout=10) as response:
                    if response.status == 204:
                        await asyncio.sleep(poll_interval)
                        continue
                        
                    if response.status != 200:
                        await asyncio.sleep(poll_interval)
                        continue
                        
                    job_data = await response.json()
                    if job_data.get("success") and "job_id" in job_data:
                        await process_job_async(job_data)
                        
            except aiohttp.ClientConnectionError:
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"[{WORKER_ID}] Falling back polling error: {e}")
                await asyncio.sleep(poll_interval)

async def main():
    logger.message("WORKER", f"Worker {WORKER_ID} started. Gateway: {GATEWAY_URL}")
    start_time = time.time()
    
    ws_url = GATEWAY_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/worker?worker_id=" + WORKER_ID
    
    while time.time() - start_time < MAX_RUN_TIME:
        try:
            # Try WebSockets first
            await run_websocket_loop(ws_url)
        except Exception as e:
            logger.warning(f"[{WORKER_ID}] WebSocket error/disconnected: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
            
            # If WebSocket connection fails repeatedly, run HTTP fallback for a short duration
            # before trying to connect to WS again.
            try:
                await asyncio.wait_for(run_http_polling_fallback(), timeout=60)
            except asyncio.TimeoutError:
                pass # Return to WebSocket loop attempt
                
    # Close warm browser on final worker shutdown
    global _browser_instance
    if _browser_instance:
        logger.info(f"[{WORKER_ID}] Worker shutdown. Closing warm browser instance.")
        try:
            await _browser_instance.close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker stopped manually.")
