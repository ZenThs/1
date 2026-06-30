import os
import sys
import time
import requests
import asyncio
import uuid

# Ensure local directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from solver import ReCaptchaSolver
from simple_logger import Logger

logger = Logger()

# Configuration from environment variables
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:11208").rstrip('/')
WORKER_ID = os.environ.get("WORKER_ID", f"gha-worker-{uuid.uuid4().hex[:8]}")
MAX_RUN_TIME = int(os.environ.get("MAX_RUN_TIME", "3300"))  # Default 55 minutes execution
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))    # Poll every 5 seconds

async def heartbeat_loop():
    """Background task to continuously send heartbeat pings to the API Gateway."""
    ping_url = f"{GATEWAY_URL}/worker/ping"
    logger.info(f"[{WORKER_ID}] Heartbeat transmitter started. Registering with gateway...")
    
    while True:
        try:
            # We use a short timeout to prevent blocking
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, 
                lambda: requests.post(ping_url, json={"worker_id": WORKER_ID}, timeout=5)
            )
            if res.status_code != 200:
                logger.warning(f"[{WORKER_ID}] Heartbeat transmitter returned HTTP {res.status_code}")
        except Exception as e:
            logger.warning(f"[{WORKER_ID}] Heartbeat transmitter failed to connect: {e}")
            
        await asyncio.sleep(25)  # Ping every 25 seconds (less than the 60s gateway timeout)

async def process_job(job: dict):
    """Solve the captcha job and submit the result back to the gateway."""
    job_id = job["job_id"]
    url = job["url"]
    site_key = job.get("sitekey")
    proxy = job.get("proxy")
    headless = job.get("headless", True)
    
    logger.info(f"[{WORKER_ID}] Starting job {job_id} for URL: {url}")
    
    try:
        # Solve the recaptcha using our robust cloakbrowser solver
        token = await ReCaptchaSolver.solve_async(
            url=url,
            site_key=site_key,
            proxy=proxy,
            headless=headless,
            debug=True
        )
        
        logger.success(f"[{WORKER_ID}] Solved job {job_id} successfully!")
        
        # Submit the result back
        submit_url = f"{GATEWAY_URL}/submitResult/{job_id}"
        
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None,
            lambda: requests.post(submit_url, json={
                "status": "completed",
                "token": token
            }, timeout=10)
        )
        
        if res.status_code == 200:
            logger.info(f"[{WORKER_ID}] Result submitted successfully for job {job_id}")
        else:
            logger.error(f"[{WORKER_ID}] Failed to submit result for job {job_id}: HTTP {res.status_code}")
            
    except Exception as e:
        logger.failure(f"[{WORKER_ID}] Failed to solve job {job_id}: {e}")
        
        # Submit the failure back
        submit_url = f"{GATEWAY_URL}/submitResult/{job_id}"
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None,
                lambda: requests.post(submit_url, json={
                    "status": "failed",
                    "error": str(e)
                }, timeout=10)
            )
            if res.status_code == 200:
                logger.info(f"[{WORKER_ID}] Failure status submitted successfully for job {job_id}")
        except Exception as ex:
            logger.error(f"[{WORKER_ID}] Critical error submitting failure status for job {job_id}: {ex}")

async def main():
    logger.message("WORKER", f"Worker {WORKER_ID} started. Connecting to gateway: {GATEWAY_URL}")
    start_time = time.time()
    
    # Start the heartbeat task in the background
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    
    try:
        while time.time() - start_time < MAX_RUN_TIME:
            try:
                # Poll gateway for next job
                poll_url = f"{GATEWAY_URL}/requestJob?worker_id={WORKER_ID}"
                
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: requests.get(poll_url, timeout=10)
                )
                
                if response.status_code == 204:
                    # No jobs available, sleep and try again
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                    
                if response.status_code != 200:
                    logger.error(f"[{WORKER_ID}] Error polling gateway: HTTP {response.status_code}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                    
                job_data = response.json()
                if job_data.get("success") and "job_id" in job_data:
                    # Process the job
                    await process_job(job_data)
                    
            except requests.exceptions.ConnectionError:
                logger.error(f"[{WORKER_ID}] Cannot connect to gateway at {GATEWAY_URL}. Retrying in {POLL_INTERVAL}s...")
                await asyncio.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error(f"[{WORKER_ID}] Unexpected error in polling loop: {e}")
                await asyncio.sleep(POLL_INTERVAL)
                
    finally:
        # Ensure background task is cleaned up
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
            
    logger.message("WORKER", f"Worker {WORKER_ID} finished maximum execution time ({MAX_RUN_TIME}s). Exiting.")

if __name__ == "__main__":
    asyncio.run(main())
