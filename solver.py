import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import io
import aiohttp
from typing import Optional, Dict, List
import speech_recognition as sr
from pydub import AudioSegment
from cloakbrowser import launch_async
from simple_logger import Logger

# Add bin directory to PATH so pydub can find ffmpeg on Windows
if sys.platform == "win32":
    bin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin')
    if os.path.exists(bin_dir) and bin_dir not in os.environ['PATH']:
        os.environ['PATH'] = bin_dir + os.path.pathsep + os.environ['PATH']

logger = Logger()

class AudioProcessor:
    """Handles downloading and processing of ReCaptcha audio challenges using speech recognition."""
    def __init__(self, debug: bool = False):
        self.recognizer = sr.Recognizer()
        self.debug = debug

    async def process_audio(self, audio_url: str) -> str:
        """Download and convert MP3 audio to text using Google Speech Recognition."""
        if self.debug:
            logger.debug(f"Downloading audio from: {audio_url}")
            
        async with aiohttp.ClientSession() as session:
            async with session.get(audio_url) as response:
                if response.status != 200:
                    raise Exception(f"Failed to download audio file: HTTP {response.status}")
                audio_content = await response.read()

        loop = asyncio.get_running_loop()
        wav_bytes = await loop.run_in_executor(None, self._convert_to_wav, audio_content)
        text = await loop.run_in_executor(None, self._recognize_speech, wav_bytes)
        return text

    def _convert_to_wav(self, audio_content: bytes) -> io.BytesIO:
        """Convert MP3 bytes to mono WAV format at 16kHz for SpeechRecognition."""
        audio_bytes = io.BytesIO(audio_content)
        audio = AudioSegment.from_mp3(audio_bytes)
        audio = audio.set_frame_rate(16000).set_channels(1)
        wav_bytes = io.BytesIO()
        audio.export(wav_bytes, format="wav", parameters=["-q:a", "0"])
        wav_bytes.seek(0)
        return wav_bytes

    def _recognize_speech(self, wav_bytes: io.BytesIO) -> str:
        """Run speech recognition on the WAV audio."""
        with sr.AudioFile(wav_bytes) as source:
            audio = self.recognizer.record(source)
            try:
                text = str(self.recognizer.recognize_google(audio))
                if self.debug:
                    logger.debug(f"Raw recognized text: '{text}'")
                cleaned_text = ''.join(c.lower() for c in text if c.isalnum() or c.isspace())
                if self.debug:
                    logger.debug(f"Cleaned recognized text: '{cleaned_text}'")
                if not cleaned_text.strip():
                    raise sr.UnknownValueError("Speech recognition returned empty response")
                return cleaned_text.strip()
            except sr.UnknownValueError:
                raise Exception("Google Speech Recognition could not understand the audio")
            except sr.RequestError as e:
                raise Exception(f"Could not request results from Google Speech Recognition service: {e}")

async def get_recaptcha_token(page) -> Optional[str]:
    """Helper to search for the reCAPTCHA response token on the page."""
    # Method 1: g-recaptcha-response textarea
    token = await page.evaluate("""() => {
        const el = document.getElementById('g-recaptcha-response');
        return el && el.value ? el.value : null;
    }""")
    if token:
        return token

    # Method 2: name="recaptcha-token"
    token = await page.evaluate("""() => {
        const el = document.querySelector('[name="recaptcha-token"]');
        return el && el.value ? el.value : null;
    }""")
    if token:
        return token

    # Method 3: Check inside all frames
    for frame in page.frames:
        try:
            token = await frame.evaluate("""() => {
                const el = document.getElementById('recaptcha-token');
                return el && el.value ? el.value : null;
            }""")
            if token:
                return token
        except Exception:
            continue
    return None

class ReCaptchaSolver:
    @classmethod
    async def solve_async(
        cls,
        url: str,
        site_key: Optional[str] = None,
        proxy: Optional[Dict] = None,
        headless: bool = True,
        timeout: int = 30,
        max_retries: int = 3,
        humanize: bool = True,
        check_score: bool = False,
        debug: bool = False
    ) -> str:
        """
        Asynchronously solve reCAPTCHA v2 using cloakbrowser.
        
        Args:
            url: The page URL containing the reCAPTCHA.
            site_key: Optional site key to inject reCAPTCHA if not present.
            proxy: Optional proxy dictionary (e.g. {"server": "http://ip:port", "username": "user", "password": "pass"}).
            headless: Whether to run the browser in headless mode.
            timeout: Max time to search for elements/frames.
            max_retries: Max attempts to solve the audio challenge.
            humanize: Enable cloakbrowser's human-like behavior simulator.
            check_score: If True, checks the solved token score via 2captcha API.
            debug: Enable debug logging.
            
        Returns:
            The reCAPTCHA response token.
        """
        start_time = time.time()
        logger.info(f"Solving reCAPTCHA. URL: {url}, SiteKey: {site_key or 'None'}, Headless: {headless}")
        
        # Prepare proxy for Playwright/cloakbrowser
        pw_proxy = None
        if proxy:
            if isinstance(proxy, str):
                pw_proxy = proxy
            elif isinstance(proxy, dict) and proxy.get("server"):
                pw_proxy = {
                    "server": proxy["server"]
                }
                if proxy.get("username"):
                    pw_proxy["username"] = proxy["username"]
                if proxy.get("password"):
                    pw_proxy["password"] = proxy["password"]

        # Launch cloakbrowser
        browser = await launch_async(
            headless=headless,
            proxy=pw_proxy,
            geoip=True if pw_proxy else False,
            humanize=humanize,
        )
        
        page = None
        try:
            page = await browser.new_page()
            
            # Load page
            await page.goto(url or "https://www.google.com/recaptcha/api2/demo")
            await page.wait_for_load_state("domcontentloaded")
            
            # Inject site_key widget if it is provided and not present
            if site_key:
                js_inject = f"""
                (() => {{
                    if (document.querySelector('.g-recaptcha') || document.querySelector('iframe[src*="recaptcha"]')) {{
                        return;
                    }}
                    let div = document.createElement('div');
                    div.className = 'g-recaptcha';
                    div.setAttribute('data-sitekey', '{site_key}');
                    document.body.appendChild(div);
                    let script = document.createElement('script');
                    script.src = 'https://www.google.com/recaptcha/api.js';
                    document.head.appendChild(script);
                }})();
                """
                await page.evaluate(js_inject)
                await asyncio.sleep(2)
                
            # Define frame locators using Playwright's dynamic resolution
            anchor_locator = page.frame_locator('iframe[title*="reCAPTCHA"], iframe[src*="anchor"]')
            bframe_locator = page.frame_locator('iframe[title*="recaptcha challenge"], iframe[src*="bframe"]')
            
            # Check if token is already present
            token = await get_recaptcha_token(page)
            if token:
                logger.success("reCAPTCHA solved/loaded instantly on page load!")
                return token

            # Check if checkbox is visible or if token is generated automatically
            checkbox = anchor_locator.locator("#recaptcha-anchor")
            has_checkbox = False
            start_wait = time.time()
            while time.time() - start_wait < 5:
                token = await get_recaptcha_token(page)
                if token:
                    logger.success("reCAPTCHA token generated automatically!")
                    return token
                if await checkbox.is_visible():
                    has_checkbox = True
                    break
                await asyncio.sleep(0.5)

            if has_checkbox:
                # Click checkbox (standard v2)
                await asyncio.sleep(1.5)
                await checkbox.click()
                
                # Wait to see if solved instantly or challenge appears
                solved = False
                bframe_appeared = False
                challenge_check_start = time.time()
                
                while time.time() - challenge_check_start < 10:
                    token = await get_recaptcha_token(page)
                    if token:
                        solved = True
                        break
                    
                    # Check if challenge bframe is visible
                    bframe_audio_button = bframe_locator.locator("#recaptcha-audio-button")
                    if await bframe_audio_button.is_visible():
                        bframe_appeared = True
                        break
                    await asyncio.sleep(0.5)
                    
                if solved:
                    token = await get_recaptcha_token(page)
                    logger.success("reCAPTCHA solved instantly via checkbox!")
                    return token
                    
                if not bframe_appeared:
                    # Check if it was solved after the loop
                    token = await get_recaptcha_token(page)
                    if token:
                        return token
                    raise Exception("reCAPTCHA checkbox clicked but challenge iframe did not appear and no token was found.")
            else:
                # Invisible reCAPTCHA flow: Wait for token to be populated automatically
                logger.info("No checkbox found. Waiting for invisible reCAPTCHA token...")
                start_wait = time.time()
                while time.time() - start_wait < 15:
                    token = await get_recaptcha_token(page)
                    if token:
                        logger.success("reCAPTCHA token retrieved from invisible widget!")
                        return token
                    await asyncio.sleep(0.5)
                raise Exception("Could not find reCAPTCHA checkbox and no token was populated automatically.")
                
            # Solve audio challenge
            audio_processor = AudioProcessor(debug=debug)
            
            for attempt in range(max_retries):
                if debug:
                    logger.debug(f"Attempting to solve audio challenge (attempt {attempt + 1}/{max_retries})")
                
                # Wait for audio challenge button and click it
                audio_button = bframe_locator.locator("#recaptcha-audio-button")
                await audio_button.wait_for(state="visible", timeout=10000)
                await audio_button.click()
                await asyncio.sleep(2.5)
                
                # Check for rate limit
                rate_limit_header = bframe_locator.locator(".rc-doscaptcha-header")
                if await rate_limit_header.is_visible():
                    text = await rate_limit_header.inner_text()
                    if "Try again later" in text or "automated" in text or "phần mềm tự động" in text:
                        raise Exception("Rate limit reached: Google has flagged this request. Try again later or use a different proxy.")
                
                # Wait for audio download link
                download_link = bframe_locator.locator(".rc-audiochallenge-tdownload-link")
                try:
                    await download_link.wait_for(state="visible", timeout=10000)
                    audio_url = await download_link.get_attribute("href")
                except Exception:
                    # Double check rate limit
                    if await rate_limit_header.is_visible():
                        raise Exception("Rate limit reached: Google has flagged this request.")
                    raise Exception("Audio challenge download link not found.")
                    
                if not audio_url:
                    raise Exception("Audio download URL is empty.")
                    
                # Download and run speech recognition
                try:
                    audio_text = await audio_processor.process_audio(audio_url)
                except Exception as e:
                    logger.warning(f"Audio processing failed: {e}. Reloading challenge...")
                    reload_button = bframe_locator.locator("#recaptcha-reload-button")
                    if await reload_button.is_visible():
                        await reload_button.click()
                        await asyncio.sleep(2)
                        continue
                    else:
                        raise e
                
                # Fill the input
                response_input = bframe_locator.locator("#audio-response")
                await response_input.wait_for(state="visible", timeout=5000)
                await response_input.fill(audio_text)
                
                # Click verify
                verify_button = bframe_locator.locator("#recaptcha-verify-button")
                await verify_button.click()
                await asyncio.sleep(3)
                
                # Check if token is ready
                token = await get_recaptcha_token(page)
                if token:
                    end_time = time.time()
                    score = None
                    if check_score:
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.post(
                                    'https://2captcha.com/api/v1/captcha-demo/recaptcha-enterprise/verify',
                                    json={
                                        'siteKey': site_key or "6LfB5B8UAAAAAJgXZxP_d-9KzXaqFzYGpXzJ2sFP",
                                        'token': token,
                                    }
                                ) as response:
                                    result = await response.json()
                                    score = result.get("riskAnalysis", {}).get("score")
                        except Exception as e:
                            logger.warning(f"Failed to check token score: {e}")
                    
                    status_msg = f"Successfully solved reCAPTCHA in {end_time - start_time:.2f}s."
                    if score is not None:
                        status_msg += f" Verified score: {score}"
                    logger.success(status_msg)
                    return token
                
                # Check for error message
                error_msg_loc = bframe_locator.locator(".rc-audiochallenge-error-message")
                if await error_msg_loc.is_visible():
                    error_text = await error_msg_loc.inner_text()
                    logger.warning(f"Google rejected answer (attempt {attempt + 1}): {error_text.strip()}")
                else:
                    logger.warning(f"Google did not provide a token after submission (attempt {attempt + 1}).")
                
                # Click reload to get a new challenge for the next retry
                reload_button = bframe_locator.locator("#recaptcha-reload-button")
                if await reload_button.is_visible():
                    await reload_button.click()
                    await asyncio.sleep(2)
            
            raise Exception(f"Failed to solve reCAPTCHA audio challenge after {max_retries} attempts.")
            
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass

    @classmethod
    def solve_sync(
        cls,
        url: str,
        site_key: Optional[str] = None,
        proxy: Optional[Dict] = None,
        headless: bool = True,
        timeout: int = 30,
        max_retries: int = 3,
        humanize: bool = True,
        check_score: bool = False,
        debug: bool = False
    ) -> str:
        """
        Synchronously solve reCAPTCHA v2 using cloakbrowser.
        """
        return asyncio.run(
            cls.solve_async(
                url=url,
                site_key=site_key,
                proxy=proxy,
                headless=headless,
                timeout=timeout,
                max_retries=max_retries,
                humanize=humanize,
                check_score=check_score,
                debug=debug
            )
        )

    @classmethod
    def solve_recaptcha(cls, *args, **kwargs) -> str:
        return cls.solve_sync(*args, **kwargs)
        
    @classmethod
    async def solve_recaptcha_async(cls, *args, **kwargs) -> str:
        return await cls.solve_async(*args, **kwargs)

class AsyncReCaptchaSolver:
    @classmethod
    async def solve_recaptcha(cls, *args, **kwargs) -> str:
        return await ReCaptchaSolver.solve_recaptcha_async(*args, **kwargs)
