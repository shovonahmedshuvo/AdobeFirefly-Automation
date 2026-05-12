"""
engines/adobe_firefly.py
~~~~~~~~~~~~~~~~~~~~~~~~
Adobe Firefly background removal engine using API-level automation.
Bypasses brittle UI selectors by mimicking network requests.
"""

import io
import os
import time
import logging
import json
import subprocess
import threading
from pathlib import Path
from typing import Optional, Dict
import requests
from playwright.sync_api import sync_playwright

logger = logging.getLogger("sa-sidecar.adobe-firefly")

# ── Constants ──────────────────────────────────────────────────────────────────

SESSION_FILE = Path(__file__).parent.parent / "adobe_session" / "state.json"
FIREFLY_URL = "https://firefly.adobe.com/production/generate?activeTool=tool-remove-background"

# ── Engine ─────────────────────────────────────────────────────────────────────

class AdobeFireflyEngine:
    def __init__(self):
        self._lock = threading.Lock()
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._bearer_token = None

    def session_exists(self) -> bool:
        return SESSION_FILE.exists() and SESSION_FILE.stat().st_size > 50

    def setup_session(self) -> str:
        """Open a visible browser for login using the persistent Chrome profile."""
        logger.info("[Adobe Firefly] Starting interactive session setup...")
        
        # Free up the profile
        self._terminate_chrome_processes()
        
        user_data_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
        profile_name = "Default"

        with sync_playwright() as play:
            try:
                browser_context = play.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    channel="chrome",
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--disable-infobars", f"--profile-directory={profile_name}"],
                    ignore_default_args=["--enable-automation", "--no-sandbox"]
                )
                page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
                page.goto(FIREFLY_URL, wait_until="domcontentloaded", timeout=30_000)
                
                logger.info("Browser opened. Please log in if needed. Close the window when finished.")
                
                # Keep open until user closes it
                page.wait_for_event("close", timeout=0)
                return "Session setup completed and saved to Chrome profile."
            except Exception as e:
                logger.error(f"Setup session failed: {e}")
                return f"Setup failed: {str(e)}"

    def process(self, img_data: bytes, filename: str = "image.jpg") -> Optional[bytes]:
        """
        New API-based processing logic.
        1. Get Bearer token via headless browser.
        2. Upload image to Adobe storage.
        3. Execute background removal workflow.
        4. Poll history until complete.
        5. Download result.
        """
        with self._lock:
            try:
                return self._run_api_workflow(img_data, filename)
            except Exception as e:
                logger.error(f"[Firefly] API Workflow failed: {e}")
                raise

    def _run_api_workflow(self, img_data: bytes, filename: str) -> bytes:
        logger.info(f"[Firefly] Starting automation for {filename}")

        # Surgical termination of existing Chrome instances to free up the profile lock
        self._terminate_chrome_processes()

        # Attempt to find the user's local Chrome data directory
        user_data_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
        profile_name = "Default"
        
        play = sync_playwright().start()
        try:
            # We use launch_persistent_context to use the actual logged-in profile
            try:
                browser_context = play.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir, 
                    channel="chrome", 
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--disable-infobars", f"--profile-directory={profile_name}"],
                    ignore_default_args=["--enable-automation", "--no-sandbox"] 
                )
            except Exception as e:
                logger.error(f"[Firefly] Failed to launch with persistent context: {e}")
                # If we fail here, it's likely because Chrome is still locked despite taskkill
                logger.info("[Firefly] Attempting one last kill and retry...")
                self._terminate_chrome_processes()
                browser_context = play.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir, 
                    channel="chrome", 
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--disable-infobars", f"--profile-directory={profile_name}"],
                    ignore_default_args=["--enable-automation", "--no-sandbox"] 
                )

            page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
            
            # 1. Navigate to the tool
            page.goto(FIREFLY_URL, wait_until="load", timeout=60_000)
            
            # Check for login wall
            if "adobeid" in page.url or "auth.services.adobe.com" in page.url or page.locator("text=Sign in").first.is_visible():
                logger.warning("[Firefly] Login required. Attempting to wait for user or failing...")
                # We can't automatically log in, so we notify and fail
                raise RuntimeError("Adobe session expired or not found. Please run 'Setup Session' to log in.")

            logger.info(f"[Firefly] Arrived at: {page.url}")

            # 2. Modal Buster
            self._dismiss_modals(page)

            # 3. Upload Image
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(img_data)
                tmp_path = tmp.name
            
            try:
                # Find the file input (often hidden)
                file_input = page.locator("input[type='file']").first
                file_input.set_input_files(tmp_path)
                logger.info("[Firefly] Image uploaded.")
            finally:
                if os.path.exists(tmp_path): os.remove(tmp_path)

            # 4. Click 'Continue'
            # The button becomes enabled after upload. It's a primary Spectrum button.
            continue_btn = page.locator('sp-button[variant="primary"]').filter(has_text="Continue").first
            try:
                continue_btn.wait_for(state="visible", timeout=15_000)
                continue_btn.click()
                logger.info("[Firefly] Clicked 'Continue'.")
            except Exception as e:
                logger.warning(f"[Firefly] 'Continue' button not found or already processed: {e}")

            # 5. Click 'Process'
            # After Continue, the same button (or a similar one) usually says 'Process' or 'Process [X] credits'
            process_btn = page.locator('sp-button[variant="primary"]').filter(has_text="Process").first
            try:
                process_btn.wait_for(state="visible", timeout=10_000)
                process_btn.click()
                logger.info("[Firefly] Clicked 'Process'.")
            except Exception as e:
                logger.warning(f"[Firefly] 'Process' button not found (might have processed automatically): {e}")

            # 6. Wait for Job to appear in History/Job Log
            # Instead of polling API, we can check the UI or wait for the completion toast/redirect
            logger.info("[Firefly] Waiting for background removal to finish...")
            
            # Navigate to Job Log to verify and wait
            page.goto("https://firefly.adobe.com/production/inspire?tab=history", wait_until="load")
            
            # Wait for the most recent job to be 'Completed'
            # Selector for the status in the table/grid
            completed_status = page.locator("text=Completed").first
            try:
                completed_status.wait_for(state="visible", timeout=120_000) # 2 mins max
                logger.info("[Firefly] Job completed!")
            except:
                raise RuntimeError("Timed out waiting for job completion in Job Log.")

            # 7. Download result
            # Click the download button of the first row
            download_btn = page.locator('button[aria-label="Download"]').first
            with page.expect_download() as download_info:
                download_btn.click()
            download = download_info.value
            
            # Read the downloaded file
            with tempfile.NamedTemporaryFile(delete=False) as dl_tmp:
                download.save_as(dl_tmp.name)
                with open(dl_tmp.name, "rb") as f:
                    data = f.read()
                os.remove(dl_tmp.name)
            
            return self._extract_image(data)

        finally:
            if 'browser_context' in locals():
                browser_context.close()
            if 'play' in locals():
                play.stop()

    def _dismiss_modals(self, page):
        try:
            modal_selectors = [
                "button:has-text('Got it')", 
                "button:has-text('Accept all')", 
                "button:has-text('Dismiss')",
                "sp-button:has-text('Got it')",
                ".spectrum-Modal-closeButton"
            ]
            for sel in modal_selectors:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    time.sleep(1)
        except: pass

    def _terminate_chrome_processes(self):
        """Surgically terminate Chrome processes to unlock the profile."""
        logger.info("[Firefly] Terminating existing Chrome processes...")
        try:
            if os.name == 'nt': # Windows
                # Try soft kill first
                subprocess.run(["taskkill", "/IM", "chrome.exe", "/T"], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
                # Then force kill
                subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else: # Unix/Mac
                subprocess.run(["pkill", "-f", "chrome"], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Verify if still running and retry if needed
            for _ in range(5):
                check = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"], capture_output=True, text=True)
                if "chrome.exe" not in check.stdout:
                    logger.info("[Firefly] Chrome terminated successfully.")
                    break
                time.sleep(1)
            else:
                logger.warning("[Firefly] Chrome processes still detected after termination attempt.")
        except Exception as e:
            logger.warning(f"[Firefly] Failed to terminate Chrome: {e}")

    def _get_download_url(self, batch_id: str, headers: Dict) -> Optional[str]:
        """Fetch the final download URL for a completed batch."""
        url = f"https://ffe-services.adobe.io/v1/batch-requests/{batch_id}"
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            # Navigate to the download URL in the response
            try:
                # The structure is usually batches -> results -> artifacts -> url
                artifacts = data.get("artifacts", [])
                if artifacts:
                    return artifacts[0].get("url")
            except: pass
        return None

    def _extract_image(self, data: bytes) -> bytes:
        """Handle ZIP archives or raw bytes."""
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                img_name = next((n for n in z.namelist() if n.lower().endswith(('.png', '.jpg'))), None)
                if img_name:
                    return z.read(img_name)
        except:
            return data # Not a zip, return raw
        return data
