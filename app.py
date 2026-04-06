from flask import Flask, render_template, request, session, jsonify, redirect, url_for, send_file, make_response, Response
from flask_session import Session
from scrapers.drivearabia import scrape_drivearabia
from scrapers.yallamotor import scrape_yallamotor
from scrapers.opensooq import scrape_opensooq
from scrapers.insurance_lookup import lookup_insurance_claim
from scrapers.report_emailer import send_report_email
from scrapers.google_image import google_chasis_image_search
# Optional: Direct import if you want to use DuckDuckGo explicitly
# from scrapers.duckduckgo_search import duckduckgo_image_search
from werkzeug.security import check_password_hash
import concurrent.futures
import pandas as pd
import requests 
import math
import time
from datetime import datetime, timedelta
import threading
import pyodbc
from functools import wraps
import pdfkit
import re
import requests
import base64
import io
import os
import uuid
import json
from urllib.parse import quote
from PIL import Image

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import logging, traceback
import atexit
import signal
import subprocess


# --- CLEANUP: Kill orphaned chromedriver on app shutdown ---
def _cleanup_chrome():
    """Kill any chromedriver processes spawned by this app."""
    import os
    for proc_name in ("chromedriver.exe", "chromedriver"):
        try:
            if os.name == 'nt':
                subprocess.run(["taskkill", "/F", "/IM", proc_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(["pkill", "-f", proc_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    print("🧹 Cleaned up chromedriver processes (cross-platform).")

atexit.register(_cleanup_chrome)

# Also handle Ctrl+C gracefully
def _signal_handler(sig, frame):
    print("\n⚠️ Interrupt received — cleaning up Chrome processes...")
    _cleanup_chrome()
    raise SystemExit(0)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# --- LOGGING SETUP ---
os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    filename='logs/scraper.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s'
)

def run_with_retries(fn, kwargs, name, retries=2, backoff=2):
    for attempt in range(retries + 1):
        try:
            result = fn(**kwargs)
            return True, result
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f"[{name}] Attempt {attempt+1} failed: {e}\n{tb}")
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
            else:
                return False, f"{type(e).__name__}: {e}"

# New Gradio Client API - LAZY INITIALIZATION
try:
    from gradio_client import Client, handle_file
    HF_API_AVAILABLE = True
    HF_CLIENT = None  # Initialize on first use
except Exception as e:
    print(f"⚠️ Failed to import Gradio client: {e}")
    print("📝 Damage detection will use manual review mode - images will be shown for manual inspection")
    HF_API_AVAILABLE = False
    HF_CLIENT = None

def get_hf_client():
    """Lazy initialize Gradio client on first use"""
    global HF_CLIENT
    if not HF_API_AVAILABLE:
        return None
    if HF_CLIENT is None:
        print("🔄 Initializing Gradio client for damage detection...")
        HF_CLIENT = Client("intelliarts/Car_parts_damage_detection")
        print("✅ Gradio client initialized successfully - Damage detection API ready")
    return HF_CLIENT

def _normalize_image_for_api(image_path):
    """
    Normalize an image to a clean JPEG that the upstream HF Space can process.
    Resizes large images (>1920px) and converts any format to RGB JPEG.
    Returns: path to the normalized temp JPEG file.
    """
    import tempfile
    try:
        img = Image.open(image_path)
        img = img.convert("RGB")  # Strip alpha, handle palette modes
        # Resize if too large (HF free Spaces have memory limits)
        max_dim = 1920
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            print(f"📐 Resized image to {img.size}")
        norm_path = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg").name
        img.save(norm_path, "JPEG", quality=90)
        print(f"✅ Normalized image saved: {norm_path} ({os.path.getsize(norm_path)} bytes)")
        return norm_path
    except Exception as e:
        print(f"⚠️ Image normalization failed: {e}, using original")
        return image_path


def detect_damage_with_gradio(image_path_or_base64):
    """
    Detect damage using the new Gradio client API
    Returns: tuple (has_damage: bool, damage_info: dict)
    """
    client = get_hf_client()
    if not client:
        print("⚠️ Gradio client not available - using manual review mode")
        return False, {
            "error": "API unavailable - manual review required",
            "manual_review": True,
            "success": False
        }
    
    cleanup_files = []
    try:
        print(f"🔍 Processing image with Gradio API...")
        
        # If it's a base64 string, save it as a temp file first
        if isinstance(image_path_or_base64, str) and image_path_or_base64.startswith("data:image"):
            import tempfile
            import base64
            
            print("📝 Converting base64 to temp file...")
            # Extract base64 data
            header, data = image_path_or_base64.split(",", 1)
            image_data = base64.b64decode(data)
            
            # Create temp file with proper extension
            file_ext = ".png"
            if "jpeg" in header or "jpg" in header:
                file_ext = ".jpg"
            elif "webp" in header:
                file_ext = ".webp"
            
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                temp_file.write(image_data)
                temp_path = temp_file.name
            cleanup_files.append(temp_path)
            
            print(f"📁 Temp file created: {temp_path}")
            image_input = temp_path
        else:
            # It's already a file path
            print(f"📁 Using file path: {image_path_or_base64}")
            image_input = image_path_or_base64
        
        # Normalize image to a clean JPEG to avoid upstream crashes
        norm_path = _normalize_image_for_api(image_input)
        if norm_path != image_input:
            cleanup_files.append(norm_path)
        image_input = norm_path
        
        # Retry loop — HF Spaces can cold-start or transiently fail
        MAX_RETRIES = 3
        result = None
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"🚀 Calling Gradio API (attempt {attempt}/{MAX_RETRIES})...")
                result = client.predict(
                    image=handle_file(image_input),
                    api_name="/inference"
                )
                print(f"📊 API Response received on attempt {attempt}")
                break  # Success
            except Exception as api_err:
                last_error = api_err
                print(f"⚠️ Attempt {attempt} failed: {api_err}")
                if attempt < MAX_RETRIES:
                    wait_secs = 3 * attempt
                    print(f"⏳ Waiting {wait_secs}s before retry...")
                    time.sleep(wait_secs)
                    # Re-initialize client in case the Space restarted
                    global HF_CLIENT
                    HF_CLIENT = None
                    client = get_hf_client()
        
        if result is None:
            raise last_error or Exception("All API attempts failed")
        
        print(f"📊 Response length: {len(result) if result else 'None'}")
        
        # Parse result - it returns 4 elements according to the API info:
        # [0] image_of_damages, [1] image_of_scratches, [2] image_of_car_parts, [3] information_about_type_of_damages_on_each_part
        if result and len(result) >= 4:
            damage_image = result[0]  # Image of damages (dict with path/url)
            scratch_image = result[1]  # Image of scratches (dict with path/url)
            parts_image = result[2]   # Image of car parts (dict with path/url)
            damage_text = result[3]   # Text description
            
            print(f"🔍 Damage text received: '{damage_text}'")
            print(f"🖼️ Damage image type: {type(damage_image)}")
            print(f"🖼️ Scratch image type: {type(scratch_image)}")
            print(f"🖼️ Parts image type: {type(parts_image)}")
            
            # Debug what we're actually getting from the API
            print(f"🔍 Raw damage image: {str(damage_image)[:200]}...")
            
            # Handle the different image formats from API
            web_accessible_image = None
            if isinstance(damage_image, str):
                if damage_image.startswith('https://'):
                    print(f"✅ Received Hugging Face URL: {damage_image[:100]}...")
                    web_accessible_image = damage_image
                elif damage_image.startswith('data:image'):
                    print(f"✅ Received base64 damage image")
                    web_accessible_image = damage_image
                elif damage_image.startswith('file:///'):
                    # Extract the actual file path from file:/// URL and copy to static
                    temp_file_path = damage_image.replace('file:///', '').replace('/', os.sep)
                    print(f"🔄 Converting file:/// URL to web accessible: {temp_file_path}")

                    try:
                        unique_id = str(uuid.uuid4())[:8]
                        web_filename = f"damage_detection_{unique_id}.webp"
                        web_path = os.path.join("static", "converted_images", web_filename)
                        os.makedirs("static/converted_images", exist_ok=True)
                        import shutil
                        shutil.copy2(temp_file_path, web_path)
                        web_accessible_image = f"/static/converted_images/{web_filename}"
                        print(f"✅ Damage image copied to: {web_accessible_image}")
                    except Exception as copy_error:
                        print(f"❌ Error copying damage image: {copy_error}")
                        web_accessible_image = None
                elif damage_image.startswith('C:\\') or damage_image.startswith('/tmp/'):
                    temp_file_path = damage_image
                    print(f"🔄 Converting local file path to web accessible: {temp_file_path}")

                    try:
                        unique_id = str(uuid.uuid4())[:8]
                        web_filename = f"damage_detection_{unique_id}.webp"
                        web_path = os.path.join("static", "converted_images", web_filename)
                        os.makedirs("static/converted_images", exist_ok=True)
                        import shutil
                        shutil.copy2(temp_file_path, web_path)
                        web_accessible_image = f"/static/converted_images/{web_filename}"
                        print(f"✅ Damage image copied to: {web_accessible_image}")
                    except Exception as copy_error:
                        print(f"❌ Error copying local file: {copy_error}")
                        web_accessible_image = None
                else:
                    print(f"⚠️ Unknown damage image format: {damage_image[:100]}...")
                    web_accessible_image = damage_image  # Try to use it anyway
            
            # Check if damage was detected based on the text output
            has_damage = False
            manual_review = False
            if damage_text and isinstance(damage_text, str):
                damage_text_clean = damage_text.strip()
                # Handle both string and list-like string formats
                if damage_text_clean and damage_text_clean not in ["[]", "no damage detected", "no damage", "clean", "no issues"]:
                    has_damage = True
                elif damage_text_clean in ["[]", ""] and web_accessible_image:
                    # API returned an annotated image but empty damage text — inconclusive
                    manual_review = True
            elif web_accessible_image:
                # No text at all but we got an annotated image back — inconclusive
                manual_review = True
            
            print(f"✅ Damage detection result: has_damage={has_damage}, manual_review={manual_review}")
            
            return has_damage, {
                "damage_image": web_accessible_image,  # Now web accessible
                "damage_description": damage_text,
                "success": True,
                "manual_review": manual_review
            }
        else:
            print(f"❌ Invalid response format. Result: {result}")
            return False, {"error": "Invalid response format", "response": result}
            
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Gradio API Error: {error_msg}")
        
        # Clean up ugly technical error messages for user display
        if "upstream" in error_msg.lower() or "gradio" in error_msg.lower():
            clean_error = "API processing failed - manual review required"
        elif "exception" in error_msg.lower():
            clean_error = "API processing failed - manual review required"
        else:
            clean_error = error_msg
            
        return False, {"error": clean_error, "success": False}
    finally:
        # Clean up all temp files
        for f in cleanup_files:
            try:
                os.unlink(f)
                print(f"🗑️ Cleaned up temp file: {f}")
            except Exception:
                pass

def get_public_domain():
    """Return ngrok URL if available, else localhost."""
    try:
        tunnels = requests.get("http://localhost:4040/api/tunnels").json()["tunnels"]
        for tunnel in tunnels:
            public_url = tunnel["public_url"]
            if public_url.startswith("https://"):
                return public_url
        for tunnel in tunnels:
            public_url = tunnel["public_url"]
            if public_url.startswith("http://"):
                return public_url
    except Exception:
        pass
    # Fallback to localhost if ngrok not running
    return "http://localhost:5000"

app = Flask(__name__)
app.secret_key = "replace_this_with_a_random_secret"
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_FILE_DIR"] = "./flask_session_data"
Session(app)

def parse_price(value):
    if not value:
        return 0
    s = str(value).strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(match.group(1)) if match else 0
app.jinja_env.filters['parse_price'] = parse_price

# Canonicalize a scraped car row to consistent keys for both UI and DB
def normalize_car_row(row):
    try:
        car_name  = row.get('CarName') or row.get('Car Name') or row.get('name') or row.get('Title') or ''
        price     = row.get('Price') or row.get('price')
        body_type = row.get('BodyType') or row.get('Body Type') or row.get('body_type') or ''
        kms_raw   = row.get('Kilometers') or row.get('Kilometer') or row.get('kms') or row.get('mileage')
        year_raw  = row.get('Year') or row.get('year')
        fuel_eff  = row.get('FuelEfficiency') or row.get('Fuel Efficiency') or row.get('fuel_efficiency') or ''
        link      = row.get('CarLink') or row.get('link') or row.get('Link') or ''

        # Parse numeric-like values to consistent types/strings without throwing
        price_clean = parse_price(price) if price is not None else None
        import re as _re
        km_match = _re.search(r"(\d[\d,\. ]*)", str(kms_raw or ''))
        kms_clean = km_match.group(1).replace(',', '').replace(' ', '') if km_match else None
        y_str = str(year_raw or '').strip()
        year_clean = int(y_str) if y_str.isdigit() else None

        normalized = {
            'Source': row.get('Source'),
            'CarName': car_name,
            'Price': price_clean,
            'BodyType': body_type,
            'Kilometers': kms_clean,
            'Year': year_clean,
            'FuelEfficiency': fuel_eff,
            'CarLink': link,
        }
        # keep original keys alongside for compatibility
        normalized.update(row)
        return normalized
    except Exception:
        return row

def process_damage_detection(request, crit):
    """Process damage detection images - can run in parallel with scrapers"""
    damage_images = []
    damage_result = "No"
    damage_future = None
    damage_executor = None
    damage_stored = False
    
    if not crit.get("damage_detection"):
        print("⏭️ DAMAGE DETECTION: Module not enabled, skipping")
        return damage_images, damage_result
    
    print("🔍 DAMAGE DETECTION: Module enabled, checking for files...")
    
    if "damageFile" not in request.files:
        print("⚠️ DAMAGE DETECTION: Module enabled but no files uploaded")
        return damage_images, damage_result
    
    files = request.files.getlist("damageFile")
    print(f"📁 DAMAGE DETECTION: Found {len(files)} uploaded files")

    for file in files:
        filename = file.filename.lower()

        # Convert .webp → .jpg (same helper you already have)
        if filename.endswith(".webp"):
            jpg_path = convert_webp_to_jpg(file)
            if jpg_path:
                with open(jpg_path, "rb") as f:
                    file_content = f.read()
            else:
                continue
        else:
            file_content = file.read()

        encoded_string = "data:image/png;base64," + base64.b64encode(file_content).decode()

        try:
            # Use new Gradio client API - single attempt per image
            print(f"🔍 Processing image {len(damage_images) + 1} with Gradio API...")
            has_damage, damage_info = detect_damage_with_gradio(encoded_string)
            
            if damage_info.get("success"):
                # Store the damage detection results for this individual image
                damage_result_item = {
                    "input": encoded_string,
                    "has_damage": has_damage,
                    "damage_image": damage_info.get("damage_image"),
                    "scratch_image": damage_info.get("scratch_image"),
                    "parts_image": damage_info.get("parts_image"),
                    "damage_description": damage_info.get("damage_description", ""),
                    "manual_review": damage_info.get("manual_review", False),
                    "original_filename": filename
                }
                damage_images.append(damage_result_item)
                
                print(f"✅ Image {len(damage_images)} processed - Damage found: {has_damage}")
                if has_damage:
                    print(f"📝 Damage description: {damage_info.get('damage_description', 'N/A')}")
            else:
                print(f"⚠️ API failed for {filename} - requiring manual review")
                damage_images.append({
                    "input": encoded_string,
                    "has_damage": False,
                    "error": "API processing failed - manual review required",
                    "original_filename": filename
                })

        except Exception as e:
            print(f"⚠️ Error during damage detection for {filename}: {str(e)}")
            damage_images.append({
                "input": encoded_string,
                "has_damage": False,
                "error": "Processing failed - manual review required",
                "original_filename": filename
            })

    # Only save if actual new files uploaded (prevents accidental overwrite)
    if files:
        session["original_damage_files"] = [img["input"] for img in damage_images]

    # Decide Yes/No based on damage detection results
    for pair in damage_images:
        if pair.get("has_damage", False):
            damage_result = "Yes"
            break

    print(f"💾 DAMAGE DETECTION: Processed {len(damage_images)} images, Damage detected: {damage_result}")
    return damage_images, damage_result

def convert_webp_to_jpg(file_storage, save_folder="static/converted_images"):
    if not os.path.exists(save_folder):
        os.makedirs(save_folder)
    unique_name = f"{uuid.uuid4().hex}.jpg"
    save_path = os.path.join(save_folder, unique_name)
    try:
        with Image.open(file_storage.stream) as im:
            rgb_image = im.convert("RGB")
            rgb_image.save(save_path, "JPEG", quality=90)
            return save_path
    except Exception as e:
        print("Image conversion error:", e)
        return None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        if 'last_active' in session and time.time() - session['last_active'] > 3000:
            session.clear()
            return redirect(url_for('admin_login'))
        session['last_active'] = time.time()
        return f(*args, **kwargs)
    return decorated_function

def load_user_permissions_into_session(user):
    session['can_create_user']   = bool(user.get('can_create_user', 0))
    session['can_manage_access'] = bool(user.get('can_manage_access', 0))
    session['can_use_main_app']  = bool(user.get('can_use_main_app', 0))
    session['can_file_mgmt']     = bool(user.get('can_file_mgmt', 0))

def adjust_drivearabia_price(price):
    try:
        if price is None or price == "":
            return None
        price = int(price)
    except:
        return 5000
    if price < 5000:
        return 5000
    elif price > 100000:
        return 100000
    else:
        return int(math.ceil(price / 5000.0) * 5000)

@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template("index.html")

@app.route("/search", methods=["POST", "GET"])
@login_required
def search():
    import traceback
    page_num = int(request.args.get("page", 1))
    if request.method == "POST":
        sources = request.form.getlist("source")
        user_email = request.form.get('user_email')
        user_mobile = request.form.get('user_mobile')
        
        # Parse single year field into year_min and year_max for scraping
        year_value = request.form.get("year")
        year_min = year_max = None
        if year_value:
            try:
                year_int = int(year_value)
                year_min = year_int
                year_max = year_int
            except ValueError:
                pass
        
        criteria = {
            "make": request.form.get("make"),
            "model_value": request.form.get("model_value"),
            "body_type": request.form.get("body_type"),
            "trim": request.form.get("trim"),
            "price_min": request.form.get("price_min"),
            "price_max": request.form.get("price_max"),
            "year_min": year_min,
            "year_max": year_max,
            "sources": sources,
            "page_num": page_num,
            "case_id": request.form.get("case_id") or f"CASE_{uuid.uuid4().hex}",
            "chasis_no": request.form.get("chasis_no"),
            "odometer_reading": request.form.get("odometer_reading"),
            "user_email": user_email,
            "user_mobile": user_mobile,
            # New checkbox fields for individual module execution
            "google_image_check": request.form.get("google_image_check") == "1",
            "insurance_lookup": request.form.get("insurance_lookup") == "1",
            "damage_detection": request.form.get("damage_detection") == "1"
        }
        
        # Store in session for backward compatibility
        session['user_email'] = user_email
        session['user_mobile'] = user_mobile
        session["criteria"] = criteria
        session["case_id"] = criteria["case_id"]
        
        # Create database session
        session_id = create_user_session(criteria, session.get('admin_id'))
        if not session_id:
            return "Error creating database session", 500
        
        session['db_session_id'] = session_id
        
        # Log the search action
        log_action(session_id, 'Search', session.get('username', 'Unknown'), 
                  f"Search initiated for {criteria.get('make')} {criteria.get('model_value')}")
        
    else:
        if "criteria" not in session:
            return render_template("index.html")
        criteria = session["criteria"]
        session_id = session.get('db_session_id')

    crit = session["criteria"]
    sources = crit.get("sources", ["drivearabia", "yallamotor", "opensooq"])
    results = []
    
    # Initialize variables
    insurance_result = None
    google_image_result = None
    damage_images = []
    damage_result = "No"
    damage_future = None

    # ---- NEW SERIAL + PARALLEL SCRAPER TASKS LOGIC ----
    # Ensure progress tracker is initialized for this case before any updates
    try:
        case_id_for_progress = session.get('case_id') or crit.get('case_id')
        if case_id_for_progress:
            # Start case only if not already started
            if not progress_tracker.get_progress(case_id_for_progress):
                progress_tracker.start_case(case_id_for_progress, crit)
            # Mark preparing as started to reflect overlay immediately
            progress_tracker.update_module_status(case_id_for_progress, 'preparing', 'started', 'Initializing search...')
    except Exception:
        pass
    serial_scrapers = []
    parallel_scrapers = []

    if "opensooq" in sources:
        # Run OpenSooq in parallel with DriveArabia
        parallel_scrapers.append((
            'opensooq',
            scrape_opensooq,
            dict(
                make=crit.get("make"),
                model_value=crit.get("model_value"),
                body_type=crit.get("body_type"),
                price_min=crit.get("price_min"),
                price_max=crit.get("price_max"),
                year_min=crit.get("year_min"),
                year_max=crit.get("year_max"),
                page_num=page_num,
                headless=True,  # Back to headless mode
            ),
        ))

    if "yallamotor" in sources:
        serial_scrapers.append((
            'yallamotor',
            scrape_yallamotor,
            dict(
                make=crit.get("make"),
                model_value=crit.get("model_value"),
                body_type=crit.get("body_type"),
                price_min=crit.get("price_min"),
                price_max=crit.get("price_max"),
                year_min=crit.get("year_min"),
                year_max=crit.get("year_max"),
                page_num=page_num,
                headless=True,  # Back to headless mode
            ),
        ))

    if "drivearabia" in sources:
        price_min = adjust_drivearabia_price(crit.get("price_min")) if crit.get("price_min") else None
        price_max = adjust_drivearabia_price(crit.get("price_max")) if crit.get("price_max") else None
        parallel_scrapers.append((
            'drivearabia',
            scrape_drivearabia,
            dict(
                make=crit.get("make"),
                model_value=crit.get("model_value"),
                body_type=crit.get("body_type"),
                price_min=price_min,
                price_max=price_max,
                year_min=crit.get("year_min"),
                year_max=crit.get("year_max"),
                page_num=page_num,
                headless=True,  # Back to headless mode
            ),
        ))

    MAX_WORKERS = 2
    MAX_RETRIES = 1  # Reduced from 2 to avoid slow retries (2 attempts total)
    results = []
    source_errors = {}

    # 🚀 START DAMAGE DETECTION (parallel via thread)
    if crit.get("damage_detection") and "damageFile" in request.files:
        print("🚀 Starting damage detection in parallel with scrapers...")
        send_process_start("Damage Detection", "Processing uploaded images...")
        if 'case_id' in session:
            try:
                progress_tracker.update_module_status(session['case_id'], 'damage_detection', 'started', 'Processing uploaded images...')
            except Exception:
                pass
        files = request.files.getlist("damageFile")
        from concurrent.futures import ThreadPoolExecutor
        damage_executor = ThreadPoolExecutor(max_workers=1)
        def _damage_worker(file_list):
            damage_images_local = []
            damage_result_local = "No"
            for file in file_list:
                filename = (file.filename or '').lower()
                try:
                    if filename.endswith(".webp"):
                        jpg_path = convert_webp_to_jpg(file)
                        if jpg_path:
                            with open(jpg_path, "rb") as f:
                                file_content = f.read()
                        else:
                            continue
                    else:
                        file_content = file.read()
                    encoded_string = "data:image/png;base64," + base64.b64encode(file_content).decode()
                    try:
                        # Use new Gradio client API - single attempt per image
                        print(f"🔍 Processing image {len(damage_images_local) + 1} with Gradio API (parallel)...")
                        has_damage, damage_info = detect_damage_with_gradio(encoded_string)
                        
                        if damage_info.get("success"):
                            damage_result_item = {
                                "input": encoded_string,
                                "has_damage": has_damage,
                                "damage_image": damage_info.get("damage_image"),
                                "scratch_image": damage_info.get("scratch_image"),
                                "parts_image": damage_info.get("parts_image"),
                                "damage_description": damage_info.get("damage_description", ""),
                                "manual_review": damage_info.get("manual_review", False),
                                "original_filename": filename
                            }
                            damage_images_local.append(damage_result_item)
                            print(f"✅ Parallel image {len(damage_images_local)} processed - Damage found: {has_damage}")
                        else:
                            print(f"⚠️ API failed for {filename} - requiring manual review")
                            damage_images_local.append({
                                "input": encoded_string,
                                "has_damage": False,
                                "error": "API processing failed - manual review required",
                                "original_filename": filename
                            })
                            
                    except Exception as e:
                        print(f"⚠️ Error during parallel damage detection for {filename}: {str(e)}")
                        damage_images_local.append({
                            "input": encoded_string,
                            "has_damage": False,
                            "error": "Processing failed - manual review required",
                            "original_filename": filename
                        })
                except Exception as e:
                    print("⚠️ Error reading file:", str(e))
                    continue
            for pair in damage_images_local:
                if pair.get("has_damage", False):
                    damage_result_local = "Yes"
                    break
            return damage_images_local, damage_result_local
        damage_future = damage_executor.submit(_damage_worker, files)
    else:
        damage_images = []
        damage_result = "No"

    # 1. Run parallel scrapers first (DriveArabia, etc)
    if parallel_scrapers:
        print(f"🚀 Starting {len(parallel_scrapers)} parallel scrapers: {[name for name, _, _ in parallel_scrapers]}")
        for name, fn, kwargs in parallel_scrapers:
            send_process_start(f"{name.capitalize()} Scraping", f"Starting {name} scraper...")
            # Update progress tracking
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], name, 'started', f'Starting {name} scraper...')
        
        with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_name = {}
            for idx, (name, fn, kwargs) in enumerate(parallel_scrapers):
                # Use unique driver path for each scraper to avoid conflicts
                kwargs['driver_path'] = f'chromedriver_{name}.exe'
                # Copy main chromedriver.exe to unique name if it doesn't exist
                import shutil
                if not os.path.exists(kwargs['driver_path']):
                    shutil.copy2('chromedriver.exe', kwargs['driver_path'])
                future = executor.submit(run_with_retries, fn, kwargs, name, MAX_RETRIES)
                future_to_name[future] = name
            for future in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    success, data = future.result()
                    print(f"📊 [{name}] Scraper result - Success: {success}, Data length: {len(data) if data else 0}")
                    if success:
                        # Feed RAW rows to UI for backward-compatible templates
                        results.extend(data or [])
                        print(f"✅ [{name}] Added {len(data)} results to total results (now {len(results)})")
                        send_process_complete(f"{name.capitalize()} Scraping", f"Found {len(data)} results")
                        # Store in database with rollback
                        try:
                            source_key_map = {
                                'drivearabia': 'DriveArabia',
                                'yallamotor': 'YallaMotor',
                                'opensooq': 'OpenSooq'
                            }
                            canonical = source_key_map.get(name.lower(), name.capitalize())
                            if session_id:
                                ok = store_car_results_with_rollback(
                                    session_id,
                                    [normalize_car_row(r) for r in (data or [])],
                                    canonical
                                )
                                print(f"🗂️ Stored {len(data)} rows for {canonical} -> {ok}")
                        except Exception as _e:
                            logging.error(f"Store results failed for {name}: {_e}")
                        # Update progress tracking
                        if 'case_id' in session:
                            progress_tracker.update_module_status(session['case_id'], name, 'completed', f'Found {len(data)} results')
                    else:
                        source_errors[name] = data
                        print(f"❌ [{name}] Scraper failed: {data}")
                        send_process_error(f"{name.capitalize()} Scraping", f"Failed: {data}")
                        # Update progress tracking (mark as completed even if failed)
                        if 'case_id' in session:
                            progress_tracker.update_module_status(session['case_id'], name, 'completed', f'Failed: {data}')
                    # Mark preparing as completed once we start receiving results from any parallel module
                    try:
                        if 'case_id' in session:
                            progress_tracker.update_module_status(session['case_id'], 'preparing', 'completed', 'Preparation completed')
                    except Exception:
                        pass
                except Exception as e:
                    tb = traceback.format_exc()
                    logging.error(f"[{name}] Uncaught error: {e}\n{tb}")
                    source_errors[name] = f"Uncaught: {e}"
                    print(f"💥 [{name}] Uncaught exception: {e}")
                    send_process_error(f"{name.capitalize()} Scraping", f"Error: {str(e)}")
                    # Update progress tracking (mark as completed even if failed)
                    if 'case_id' in session:
                        progress_tracker.update_module_status(session['case_id'], name, 'completed', f'Error: {str(e)}')

    # 2. Run serial scrapers (OpenSooq, then YallaMotor) one after the other
    for name, fn, kwargs in serial_scrapers:
        print(f"🚀 Starting serial scraper: {name}")
        send_process_start(f"{name.capitalize()} Scraping", f"Starting {name} scraper...")
        # Update progress tracking
        if 'case_id' in session:
            progress_tracker.update_module_status(session['case_id'], name, 'started', f'Starting {name} scraper...')
        
        try:
            success, data = run_with_retries(fn, kwargs, name, MAX_RETRIES)
            print(f"📊 [{name}] Scraper result - Success: {success}, Data length: {len(data) if data else 0}")
            if success:
                # Feed RAW rows to UI; normalize only for DB write
                results.extend(data or [])
                print(f"✅ [{name}] Added {len(data)} results to total results (now {len(results)})")
                send_process_complete(f"{name.capitalize()} Scraping", f"Found {len(data)} results")
                # Store in database with rollback
                try:
                    source_key_map = {
                        'drivearabia': 'DriveArabia',
                        'yallamotor': 'YallaMotor',
                        'opensooq': 'OpenSooq'
                    }
                    canonical = source_key_map.get(name.lower(), name.capitalize())
                    ok = store_car_results_with_rollback(
                        session_id,
                        [normalize_car_row(r) for r in (data or [])],
                        canonical
                    )
                    print(f"🗂️ Stored {len(data)} rows for {canonical} -> {ok}")
                except Exception as _e:
                    logging.error(f"Store results failed for {name}: {_e}")
                # Update progress tracking
                if 'case_id' in session:
                    progress_tracker.update_module_status(session['case_id'], name, 'completed', f'Found {len(data)} results')
            else:
                source_errors[name] = data
                print(f"❌ [{name}] Scraper failed: {data}")
                send_process_error(f"{name.capitalize()} Scraping", f"Failed: {data}")
                # Update progress tracking (mark as completed even if failed)
                if 'case_id' in session:
                    progress_tracker.update_module_status(session['case_id'], name, 'completed', f'Failed: {data}')
        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f"[{name}] Uncaught error: {e}\n{tb}")
            source_errors[name] = f"Uncaught: {e}"
            print(f"💥 [{name}] Uncaught exception: {e}")
            send_process_error(f"{name.capitalize()} Scraping", f"Error: {str(e)}")
            # Update progress tracking (mark as completed even if failed)
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], name, 'completed', f'Error: {str(e)}')

    # ✅ Wait for damage detection if it was started in parallel
    if damage_future is not None:
        try:
            damage_images, damage_result = damage_future.result()
            session["damage_result"] = damage_result
            session["damage_images"] = damage_images
            session["original_damage_files"] = [img.get("input") for img in damage_images]
            send_process_complete("Damage Detection", f"Damage detected: {damage_result}")
            print(f"✅ DAMAGE DETECTION: Completed - Damage: {damage_result}, Images: {len(damage_images)}")
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], 'damage_detection', 'completed', f'Damage detected: {damage_result}')
            if damage_images and session_id:
                store_damage_detection(session_id, damage_result, damage_images)
                damage_stored = True
        except Exception as e:
            print("⚠️ DAMAGE DETECTION: Error in background processing:", str(e))
            send_process_error("Damage Detection", str(e))
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], 'damage_detection', 'completed', f'Error: {str(e)}')
        finally:
            try:
                if damage_executor:
                    damage_executor.shutdown(wait=False)
            except Exception:
                pass

    # 🎯 DAMAGE DETECTION RESULTS (already processed above)
    # damage_images and damage_result are already set above
    
    # Store damage detection results in session (already done above)
    # session["damage_result"] = damage_result
    # session["damage_images"] = damage_images
    print(f"💾 DAMAGE DETECTION: Final result stored in session - Damage: {damage_result}, Images: {len(damage_images)}")
    
    # Clean up temporary session data (no longer needed)
    # if "temp_damage_files" in session:
    #     del session["temp_damage_files"]
    # if "temp_damage_filenames" in session:
    #     del session["temp_damage_filenames"]

    session["last_results"] = results
    session["source_errors"] = source_errors
    
    print(f"🎯 FINAL RESULTS SUMMARY:")
    print(f"   Total results: {len(results)}")
    print(f"   Source errors: {len(source_errors)}")
    print(f"   Results breakdown by source:")
    for source in ["DriveArabia", "YallaMotor", "OpenSooq"]:
        source_count = len([r for r in results if r.get("Source") == source])
        print(f"     {source}: {source_count} results")

    # Excel export
    print(f"📋 MODULE STATUS:")
    print(f"   Car Scraping: {'Enabled' if sources else 'No sources selected'}")
    print(f"   Insurance Lookup: {'Enabled' if crit.get('insurance_lookup') else 'Disabled'}")
    print(f"   Google Image Check: {'Enabled' if crit.get('google_image_check') else 'Disabled'}")
    print(f"   Damage Detection: {'Enabled' if crit.get('damage_detection') else 'Disabled'}")
    
    if results:
        send_process_start("Excel Generation", "Creating Excel export...")
        # Per-case excel path to avoid collisions across users
        try:
            case_id_for_excel = session.get('case_id') or crit.get('case_id')
            excel_dir = os.path.join("static", "results", str(case_id_for_excel or "unknown"))
            os.makedirs(excel_dir, exist_ok=True)
            excel_fs_path = os.path.join(excel_dir, "results.xlsx")
        except Exception:
            excel_fs_path = os.path.join("static", "results.xlsx")
        with pd.ExcelWriter(excel_fs_path, engine="openpyxl") as writer:
            if any(car.get("Source") == "DriveArabia" for car in results):
                df_drivearabia = pd.DataFrame([car for car in results if car.get("Source") == "DriveArabia"])
                if not df_drivearabia.empty:
                    df_drivearabia.to_excel(writer, sheet_name="DriveArabia", index=False)

            if any(car.get("Source") == "YallaMotor" for car in results):
                df_yallamotor = pd.DataFrame([car for car in results if car.get("Source") == "YallaMotor"])
                if not df_yallamotor.empty:
                    df_yallamotor.to_excel(writer, sheet_name="YallaMotor", index=False)

            if any(car.get("Source") == "OpenSooq" for car in results):
                df_opensooq = pd.DataFrame([car for car in results if car.get("Source") == "OpenSooq"])
                if not df_opensooq.empty:
                    df_opensooq.to_excel(writer, sheet_name="OpenSooq", index=False)
        send_process_complete("Excel Generation", "Excel file created successfully")
        try:
            # Expose web path for template JS download
            case_id_for_excel = session.get('case_id') or crit.get('case_id')
            session["excel_web_path"] = f"/static/results/{case_id_for_excel}/results.xlsx"
        except Exception:
            session["excel_web_path"] = "/static/results.xlsx"
    else:
        print("⚠️ No data scraped. Skipping Excel write.")
        send_process_error("Excel Generation", "No data to export")

    # ✅ Run Google Image and Insurance in parallel AFTER Excel export
    insurance_result = None
    google_image_result = None
    from concurrent.futures import ProcessPoolExecutor
    post_tasks = []
    post_exec = ProcessPoolExecutor(max_workers=2)

    # Schedule Insurance
    if crit.get("insurance_lookup"):
        if crit.get("chasis_no"):
            print("🔍 INSURANCE LOOKUP: Starting post-scrape in parallel...")
            send_process_start("Insurance Lookup", "Checking insurance database...")
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], 'insurance_lookup', 'started', 'Checking insurance database...')
            post_tasks.append(('insurance', post_exec.submit(run_with_retries, lookup_insurance_claim, {"chasis_no": crit["chasis_no"], "headless": True}, "insurance_claim", MAX_RETRIES)))
        else:
            print("⚠️ INSURANCE LOOKUP: Enabled but no chassis number provided")
            send_process_error("Insurance Lookup", "No chassis number provided")
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], 'insurance_lookup', 'completed', 'No chassis number provided')
    else:
        print("⏭️ INSURANCE LOOKUP: Not enabled")
        if 'case_id' in session:
            progress_tracker.update_module_status(session['case_id'], 'insurance_lookup', 'completed', 'Module not enabled')

    # Schedule Google Image (stagger start after insurance to reduce contention)
    if crit.get("google_image_check"):
        if crit.get("chasis_no"):
            print("🔍 GOOGLE IMAGE CHECK: Starting post-scrape in parallel...")
            send_process_start("Google Image Search", "Searching Google for chassis images...")
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], 'google_image', 'started', 'Searching Google for chassis images...')
            post_tasks.append(('google', post_exec.submit(run_with_retries, google_chasis_image_search, {"chasis_no": crit["chasis_no"], "headless": True}, "google_image", MAX_RETRIES)))
        else:
            print("⚠️ GOOGLE IMAGE CHECK: Enabled but no chassis number provided")
            send_process_error("Google Image Search", "No chassis number provided")
            if 'case_id' in session:
                progress_tracker.update_module_status(session['case_id'], 'google_image', 'completed', 'No chassis number provided')
    else:
        print("⏭️ GOOGLE IMAGE CHECK: Not enabled")
        if 'case_id' in session:
            progress_tracker.update_module_status(session['case_id'], 'google_image', 'completed', 'Module not enabled')

    # Collect post results
    for key, fut in post_tasks:
        try:
            success, data = fut.result()
            if key == 'insurance':
                insurance_result = data if success else {"error": data}
                if success:
                    send_process_complete("Insurance Lookup", "Insurance check completed")
                    if 'case_id' in session:
                        progress_tracker.update_module_status(session['case_id'], 'insurance_lookup', 'completed', 'Insurance check completed')
                    if session_id:
                        store_insurance_check(session_id, insurance_result)
                else:
                    send_process_error("Insurance Lookup", f"Failed: {data}")
                    if 'case_id' in session:
                        progress_tracker.update_module_status(session['case_id'], 'insurance_lookup', 'completed', f'Failed: {data}')
            else:
                google_image_result = data if success else {"error": data}
                num_imgs = 0
                try:
                    num_imgs = len(google_image_result.get('downloaded_images', [])) if isinstance(google_image_result, dict) else 0
                except Exception:
                    pass
                if success:
                    send_process_complete("Google Image Search", f"Found {num_imgs} images")
                    if 'case_id' in session:
                        progress_tracker.update_module_status(session['case_id'], 'google_image', 'completed', f'Found {num_imgs} images')
                    if google_image_result and session_id:
                        store_google_images(session_id, google_image_result)
                else:
                    send_process_error("Google Image Search", f"Failed: {data}")
                    if 'case_id' in session:
                        progress_tracker.update_module_status(session['case_id'], 'google_image', 'completed', f'Failed: {data}')
        except Exception as e:
            if key == 'insurance':
                send_process_error("Insurance Lookup", str(e))
            else:
                send_process_error("Google Image Search", str(e))

    try:
        post_exec.shutdown(wait=False)
    except Exception:
        pass

    # Normalize session objects to safe defaults to avoid None in downstream consumers
    session["insurance_result"] = insurance_result or {}
    session["google_image_result"] = google_image_result or {}

    raw_images = (google_image_result or {}).get("downloaded_images", [])
    session["downloaded_images"] = [os.path.relpath(p, "static") for p in raw_images]

    # ✅✅✅ DAMAGE DETECTION BLOCK END ✅✅✅

    clean_prices = [
        parse_price(car.get("Price", ""))
        for car in results
        if parse_price(car.get("Price", "")) > 0
    ]

    # Store collective price summary in database
    if session_id:
        store_collective_price_summary(session_id, clean_prices)

    damage_result_for_template = session.get("damage_decision") or session.get("damage_result", "No")
    print(f"🔧 SEARCH: damage_decision={session.get('damage_decision')}")
    print(f"🔧 SEARCH: damage_result={session.get('damage_result')}")
    print(f"🔧 SEARCH: Final damage_result_for_template={damage_result_for_template}")

    # Store damage detection in database
    if damage_images and session_id and not damage_stored:
        store_damage_detection(session_id, damage_result, damage_images)

    print(f"🎭 TEMPLATE RENDERING:")
    print(f"   Results count: {len(results)}")
    print(f"   Case ID: {session.get('case_id')}")
    print(f"   Damage result: {damage_result_for_template}")
    print(f"   Damage images: {len(session.get('damage_images', []))}")
    print(f"   Insurance result: {insurance_result is not None}")
    print(f"   Google image result: {google_image_result is not None}")
    print(f"   Source errors: {len(source_errors)}")
    
    # Debug damage images being passed to template
    template_damage_images = session.get("damage_images", [])
    for i, img in enumerate(template_damage_images):
        print(f"🔍 Template damage_image[{i}]: {str(img.get('damage_image', 'None'))[:100]}...")
        print(f"🔍 Template has_damage[{i}]: {img.get('has_damage', False)}")
        print(f"🔍 Template description[{i}]: {img.get('damage_description', 'None')}")

    # (No background await needed; already completed)

    # Send search completion notification
    send_search_complete()

    # Mark case as completed in progress tracking
    if 'case_id' in session:
        progress_tracker.complete_case(session['case_id'])

    return render_template(
        "results.html",
        results=results,
        case_id=session.get("case_id"),
        damage_result=damage_result_for_template,
        clean_prices=clean_prices,
        damage_images=session.get("damage_images", []),
        insurance_result=insurance_result,
        downloaded_images=session["downloaded_images"],
        google_image_result=google_image_result,
        source_errors=source_errors,
        criteria=criteria  # Pass criteria so template knows which modules were selected
    )

@app.route('/dashboard')
@login_required
def dashboard():
    perms = {
        'create_user':   session.get('can_create_user', False),
        'manage_access': session.get('can_manage_access', False),
        'main_app':      session.get('can_use_main_app', False),
        'file_mgmt':     session.get('can_file_mgmt', False),
    }
    return render_template('admin_dashboard.html', perms=perms)
    
def get_db():
    # Use hardcoded defaults, allow .env override
    server = os.getenv('DB_SERVER', '208.91.198.196')
    database = os.getenv('DB_NAME', 'ICP')
    username = os.getenv('DB_USER', 'ICP')
    password = os.getenv('DB_PASSWORD', 'Teams@@2578')
    driver = os.getenv('DB_DRIVER', '{ODBC Driver 18 for SQL Server}')
    # Add Encrypt/Trust flags for Driver 18
    conn = pyodbc.connect(
        fr'DRIVER={driver};SERVER={server};DATABASE={database};UID={username};PWD={password};Encrypt=yes;TrustServerCertificate=yes;'
    )
    return conn

# --- DATABASE INTEGRATION FUNCTIONS ---
def create_user_session(criteria, admin_id):
    """Create a new user session in database"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO icp.v_UserSession 
                (CaseID, Email, Mobile, Make, Model, BodyType, Trim, PriceMin, PriceMax, 
                 YearMin, YearMax, ChassisNumber, OdometerReading, AdminID, GoogleImageCheck, InsuranceLookup)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                criteria.get('case_id'), criteria.get('user_email'), criteria.get('user_mobile'),
                criteria.get('make'), criteria.get('model_value'), criteria.get('body_type'),
                criteria.get('trim'), criteria.get('price_min'), criteria.get('price_max'), 
                criteria.get('year_min'), criteria.get('year_max'), criteria.get('chasis_no'),
                criteria.get('odometer_reading'), admin_id,
                criteria.get('google_image_check', False), criteria.get('insurance_lookup', False)
            ))
            conn.commit()
            
            # Get the created SessionID
            cursor.execute("SELECT SessionID FROM icp.v_UserSession WHERE CaseID = ?", (criteria.get('case_id'),))
            session_id = cursor.fetchone()[0]
            return session_id
    except Exception as e:
        logging.error(f"Error creating user session: {e}")
        return None

@app.route('/superadmin/create-user', methods=['GET', 'POST'])
@login_required
def superadmin_create_user():
    # enforce permission as well
    if not session.get('can_create_user'):
        return "Forbidden: you don't have Create User permission.", 403

    message = error = None

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        active   = 1 if (request.form.get('active') == '1') else 0

        # dashboard flags
        f_create = 1 if request.form.get('can_create_user')   == 'on' else 0
        f_access = 1 if request.form.get('can_manage_access') == 'on' else 0
        f_main   = 1 if request.form.get('can_use_main_app')  == 'on' else 0
        f_files  = 1 if request.form.get('can_file_mgmt')     == 'on' else 0

        # validate
        if not username or not password:
            error = "Username and Password are required."

        if not error:
            from datetime import datetime, timedelta
            expiry_dt = datetime.now() + timedelta(days=10)   # ← auto-set expiry (no form field)

            db = get_db()
            cur = db.cursor()
            try:
                # unique username check
                cur.execute("SELECT 1 FROM vidit_users WHERE username=?", (username,))
                if cur.fetchone():
                    error = "Username already exists."
                else:
                    from werkzeug.security import generate_password_hash
                    pwd_hash = generate_password_hash(password)

                    cur.execute("""
                        INSERT INTO vidit_users
                        (username, password_hash, active, expiry,
                         failed_attempts, lock_until,
                         can_create_user, can_manage_access, can_use_main_app, can_file_mgmt)
                        VALUES (?, ?, ?, ?, 0, NULL, ?, ?, ?, ?)
                    """, (username, pwd_hash, active, expiry_dt, f_create, f_access, f_main, f_files))

                    db.commit()
                    message = (
                        f"User '{username}' created. "
                        f"Expiry set to {expiry_dt:%Y-%m-%d %H:%M:%S} (in 10 days)."
                    )
            except Exception as e:
                error = f"DB error: {e}"
            finally:
                cur.close(); db.close()

    return render_template('superadmin_create_user.html', message=message, error=error)

@app.route('/superadmin/access', methods=['GET', 'POST'])
@login_required
def superadmin_access():
    # Only allow people who have the "manage access" permission
    if not session.get('can_manage_access'):
        return "Forbidden: you don't have User Access Management permission.", 403

    message = error = None
    db = get_db()
    cur = db.cursor()

    if request.method == 'POST':
        try:
            # We get a list of user IDs from hidden inputs named user_id
            ids = request.form.getlist('user_id')
            for sid in ids:
                uid = int(sid)

                # Active is a select with explicit "1"/"0"
                active = 1 if request.form.get(f'active_{uid}') == '1' else 0

                # Checkboxes: present -> 1, absent -> 0
                can_create_user   = 1 if request.form.get(f'create_{uid}') == 'on' else 0
                can_manage_access = 1 if request.form.get(f'manage_{uid}') == 'on' else 0
                can_use_main_app  = 1 if request.form.get(f'main_{uid}')   == 'on' else 0
                can_file_mgmt     = 1 if request.form.get(f'file_{uid}')   == 'on' else 0

                cur.execute("""
                    UPDATE vidit_users
                    SET active=?, 
                        can_create_user=?, 
                        can_manage_access=?, 
                        can_use_main_app=?, 
                        can_file_mgmt=?
                    WHERE id=?
                """, (active, can_create_user, can_manage_access, can_use_main_app, can_file_mgmt, uid))

            db.commit()
            message = "Changes saved."
        except Exception as e:
            db.rollback()
            error = f"DB error: {e}"

    # Always fetch fresh rows for display
    cur.execute("""
        SELECT id, username, active, expiry,
               can_create_user, can_manage_access, can_use_main_app, can_file_mgmt
        FROM vidit_users
        ORDER BY username
    """)
    cols = [d[0] for d in cur.description]
    users = [dict(zip(cols, r)) for r in cur.fetchall()]

    cur.close(); db.close()
    return render_template('superadmin_access.html', users=users, message=message, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('admin_login'))

def store_car_results_with_rollback(session_id, results, source):
    """Store car scraping results with rollback - replaces old data for the source"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get CaseID from session
            cursor.execute("SELECT CaseID FROM icp.v_UserSession WHERE SessionID = ?", (session_id,))
            case_id_row = cursor.fetchone()
            if not case_id_row:
                logging.error(f"SessionID {session_id} not found in v_UserSession")
                return False
            case_id = case_id_row[0]
            
            # Start transaction
            cursor.execute("BEGIN TRANSACTION")
            
            try:
                # DELETE old results for this source and session
                cursor.execute("""
                    DELETE FROM icp.v_CarQuoteResult 
                    WHERE SessionID = ? AND Source = ?
                """, (session_id, source))
                
                # INSERT new results (normalize keys coming from different scrapers)
                # INSERT new results
                for car in results:
                    car_name  = car.get('CarName') or car.get('Car Name') or ''
                    price_txt = car.get('Price') or car.get('price')
                    body_type = car.get('BodyType') or car.get('Body Type') or ''
                    kms_txt   = car.get('Kilometers') or car.get('Kilometer') or car.get('kms') or car.get('mileage') or None
                    year_txt  = car.get('Year') or car.get('year') or None
                    link_val  = car.get('CarLink') or car.get('link') or car.get('Link') or ''

                    # cast to match v_CarQuoteResult schema
                    price_val = parse_price(price_txt) if price_txt is not None else None  # FLOAT
                    year_val  = int(str(year_txt).strip()) if str(year_txt).strip().isdigit() else None  # INT
                    fuel_eff  = (car.get('FuelEfficiency') or car.get('Fuel Efficiency') or '')[:32]
                    link_val  = link_val[:512]

                    additional_data = {}
                    for k, v in car.items():
                        if k not in ('Source','CarName','Car Name','Price','price','BodyType','Body Type',
                                    'Kilometers','Kilometer','kms','mileage','Year','year',
                                    'FuelEfficiency','Fuel Efficiency','CarLink','link','Link'):
                            additional_data[k] = v

                    cursor.execute("""
                        INSERT INTO icp.v_CarQuoteResult 
                        (SessionID, CaseID, Source, CarName, Price, BodyType, Kilometers, Year, FuelEfficiency, CarLink, AdditionalData)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        session_id, case_id, source,
                        car_name, float(price_val) if price_val is not None else None,
                        body_type, kms_txt, year_val, fuel_eff, link_val,
                        json.dumps(additional_data) if additional_data else None
                    ))
                
                # Commit transaction
                conn.commit()
                try:
                    cursor.execute("SELECT @@SERVERNAME, DB_NAME(), COUNT(*) FROM icp.v_CarQuoteResult WHERE SessionID = ?", (session_id,))
                    srv, dbn, cnt = cursor.fetchone()
                    logging.info(f"DB VERIFY v_CarQuoteResult: server={srv}, db={dbn}, session={session_id}, rows={cnt}")
                    print(f"DB VERIFY v_CarQuoteResult: server={srv}, db={dbn}, session={session_id}, rows={cnt}")
                except Exception as _verr:
                    logging.error(f"Post-insert verify failed: {_verr}")
                return True
                
            except Exception as e:
                # Rollback on error
                conn.rollback()
                logging.error(f"Error in transaction, rolled back: {e}")
                return False
                
    except Exception as e:
        logging.error(f"Error storing car results: {e}")
        return False

def store_collective_price_summary(session_id, clean_prices):
    """Store collective price summary with rollback"""
    if not clean_prices:
        # No valid prices — preserve existing summary instead of wiping it
        print(f"⚠️ store_collective_price_summary: No valid prices, preserving existing summary")
        return True
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get CaseID from session
            cursor.execute("SELECT CaseID FROM icp.v_UserSession WHERE SessionID = ?", (session_id,))
            case_id_row = cursor.fetchone()
            if not case_id_row:
                logging.error(f"SessionID {session_id} not found in v_UserSession")
                return False
            case_id = case_id_row[0]
            
            # Start transaction
            cursor.execute("BEGIN TRANSACTION")
            
            try:
                # DELETE old summary for this session
                cursor.execute("""
                    DELETE FROM icp.v_CollectivePriceSummary 
                    WHERE SessionID = ?
                """, (session_id,))
                
                if clean_prices:
                    total_cars = len(clean_prices)
                    average_price = sum(clean_prices) / total_cars
                    min_price = min(clean_prices)
                    max_price = max(clean_prices)
                    price_range = f"{min_price:,.0f} - {max_price:,.0f}"
                    
                    cursor.execute("""
                        INSERT INTO icp.v_CollectivePriceSummary 
                        (SessionID, CaseID, TotalCars, AveragePrice, MinPrice, MaxPrice, PriceRange)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (session_id, case_id, total_cars, average_price, min_price, max_price, price_range))
                
                # Commit transaction
                conn.commit()
                return True
                
            except Exception as e:
                # Rollback on error
                conn.rollback()
                logging.error(f"Error in price summary transaction, rolled back: {e}")
                return False
                
    except Exception as e:
        logging.error(f"Error storing price summary: {e}")
        return False

def store_damage_detection(session_id, damage_result, damage_images):
    """Store damage detection results in database"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get CaseID from session
            cursor.execute("SELECT CaseID FROM icp.v_UserSession WHERE SessionID = ?", (session_id,))
            case_id_row = cursor.fetchone()
            if not case_id_row:
                logging.error(f"SessionID {session_id} not found in v_UserSession")
                return False
            case_id = case_id_row[0]
            
            # Create damage detection record
            cursor.execute("""
                INSERT INTO icp.v_DamageDetection (SessionID, CaseID, DetectionDecision)
                VALUES (?, ?, ?)
            """, (session_id, case_id, str(damage_result)[:255])) # Avoid truncation
            
            # Get the created DetectionID using SELECT for UNIQUEIDENTIFIER
            cursor.execute("""
                SELECT DetectionID FROM icp.v_DamageDetection 
                WHERE SessionID = ? AND DetectionDecision = ? 
                ORDER BY CreatedOn DESC
            """, (session_id, damage_result))
            detection_id = cursor.fetchone()[0]
            
            # Store images
            for i, pair in enumerate(damage_images):
                # Store original image
                cursor.execute("""
                    INSERT INTO icp.v_DamageDetectionImage (DetectionID, CaseID, ImageType, ImagePath, OrderIndex)
                    VALUES (?, ?, ?, ?, ?)
                """, (detection_id, case_id, 'Original', pair.get('input'), i * 2))
                
                # Store processed image
                cursor.execute("""
                    INSERT INTO icp.v_DamageDetectionImage (DetectionID, CaseID, ImageType, ImagePath, OrderIndex)
                    VALUES (?, ?, ?, ?, ?)
                """, (detection_id, case_id, 'Processed', pair.get('output'), i * 2 + 1))
            
            conn.commit()
            return True
    except Exception as e:
        logging.error(f"Error storing damage detection: {e}")
        return False

def store_google_images(session_id, google_image_result):
    """Store Google image search results in database"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get CaseID from session
            cursor.execute("SELECT CaseID FROM icp.v_UserSession WHERE SessionID = ?", (session_id,))
            case_id_row = cursor.fetchone()
            if not case_id_row:
                logging.error(f"SessionID {session_id} not found in v_UserSession")
                return False
            case_id = case_id_row[0]
            
            # Create Google image check record
            cursor.execute("""
                INSERT INTO icp.v_GoogleImageCheck (SessionID, CaseID, SearchDomain)
                VALUES (?, ?, ?)
            """, (session_id, case_id, 'google.com'))
            
            # Get the created GoogleImageCheckID using SELECT for UNIQUEIDENTIFIER
            cursor.execute("""
                SELECT GoogleImageCheckID FROM icp.v_GoogleImageCheck 
                WHERE SessionID = ? AND SearchDomain = ? 
                ORDER BY CreatedOn DESC
            """, (session_id, 'google.com'))
            google_check_id = cursor.fetchone()[0]
            
            # Store individual images
            downloaded_images = google_image_result.get('downloaded_images', [])
            for i, image_path in enumerate(downloaded_images):
                cursor.execute("""
                    INSERT INTO icp.v_GoogleImage (GoogleImageCheckID, CaseID, ImagePath, OrderIndex)
                    VALUES (?, ?, ?, ?)
                """, (google_check_id, case_id, image_path, i))
            
            conn.commit()
            return True
    except Exception as e:
        logging.error(f"Error storing Google images: {e}")
        return False

def store_insurance_check(session_id, insurance_result):
    """Store insurance check results in database"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get CaseID from session
            cursor.execute("SELECT CaseID FROM icp.v_UserSession WHERE SessionID = ?", (session_id,))
            case_id_row = cursor.fetchone()
            if not case_id_row:
                logging.error(f"SessionID {session_id} not found in v_UserSession")
                return False
            case_id = case_id_row[0]
            
            cursor.execute("""
                INSERT INTO icp.v_InsuranceClaimCheck 
                (SessionID, CaseID, ClaimFound, ExcessPaid, ClaimDetailsURL, PopupScreenshot)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                session_id, case_id,
                insurance_result.get('claim_exists', False),
                insurance_result.get('excess_paid'),
                insurance_result.get('claim_details_url'),
                insurance_result.get('popup_screenshot')
            ))
            
            conn.commit()
            return True
    except Exception as e:
        logging.error(f"Error storing insurance check: {e}")
        return False

def store_rating_data(session_id, status, remarks, approved_value, username):
    """Store rating data in database - prevents duplicates per session"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get CaseID from session
            cursor.execute("SELECT CaseID FROM icp.v_UserSession WHERE SessionID = ?", (session_id,))
            case_id_row = cursor.fetchone()
            if not case_id_row:
                logging.error(f"SessionID {session_id} not found in v_UserSession")
                return False, "Session not found"
            case_id = case_id_row[0]
            
            # Check if rating already exists for this session
            cursor.execute("""
                SELECT RatingID FROM icp.v_RatingData 
                WHERE SessionID = ?
            """, (session_id,))
            
            existing_rating = cursor.fetchone()
            
            if existing_rating:
                # Rating already exists - return error
                return False, "Rating already exists for this session. Cannot submit multiple times."
            
            # No existing rating - insert new one
            cursor.execute("""
                INSERT INTO icp.v_RatingData 
                (SessionID, CaseID, RatingStatus, RatingRemarks, ApprovedValue, ActionBy, CreatedOn)
                VALUES (?, ?, ?, ?, ?, ?, GETDATE())
            """, (session_id, case_id, status, remarks, approved_value, username))
            conn.commit()
            return True, "Rating submitted successfully"
            
    except Exception as e:
        logging.error(f"Error storing rating data: {e}")
        return False, f"Database error: {str(e)}"

def log_action(session_id, action_type, action_by, description=""):
    """Log user actions for audit trail"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get CaseID from session
            cursor.execute("SELECT CaseID FROM icp.v_UserSession WHERE SessionID = ?", (session_id,))
            case_id_row = cursor.fetchone()
            if not case_id_row:
                logging.error(f"SessionID {session_id} not found in v_UserSession")
                return False
            case_id = case_id_row[0]
            
            cursor.execute("""
                INSERT INTO icp.v_ActionLog (SessionID, CaseID, ActionType, ActionBy, Description)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, case_id, action_type, action_by, str(description)[:255])) # Avoid truncation
            conn.commit()
            return True
    except Exception as e:
        logging.error(f"Error logging action: {e}")
        return False

def update_damage_decision_db(session_id, decision):
    """Update damage decision in database"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE icp.v_DamageDetection 
                SET ManualDecision = ?, DetectionDecision = ?
                WHERE SessionID = ?
            """, (decision, decision, session_id))
            conn.commit()
            return True
    except Exception as e:
        logging.error(f"Error updating damage decision: {e}")
        return False

def get_session_results(session_id):
    """Retrieve all results for a session from database"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get car results
            cursor.execute("""
                SELECT Source, CarName, Price, BodyType, Kilometers, Year, FuelEfficiency, CarLink, AdditionalData
                FROM icp.v_CarQuoteResult WHERE SessionID = ?
            """, (session_id,))
            car_results = []
            for row in cursor.fetchall():
                car = {
                    'Source': row[0],
                    'CarName': row[1],
                    'Price': row[2],
                    'BodyType': row[3],
                    'Kilometers': row[4],
                    'Year': row[5],
                    'FuelEfficiency': row[6],
                    'CarLink': row[7]
                }
                # Add additional data if exists
                if row[8]:
                    additional = json.loads(row[8])
                    car.update(additional)
                car_results.append(car)
            
            # Get damage detection
            cursor.execute("""
                SELECT dd.DetectionDecision, dd.ManualDecision
                FROM icp.v_DamageDetection dd
                WHERE dd.SessionID = ?
            """, (session_id,))
            damage_row = cursor.fetchone()
            damage_result = damage_row[1] if damage_row and damage_row[1] else (damage_row[0] if damage_row else 'No')
            
            # Get insurance result
            cursor.execute("""
                SELECT ClaimFound, ExcessPaid, ClaimDetailsURL, PopupScreenshot
                FROM icp.v_InsuranceClaimCheck WHERE SessionID = ?
            """, (session_id,))
            insurance_row = cursor.fetchone()
            insurance_result = {
                'claim_exists': insurance_row[0] if insurance_row else False,
                'excess_paid': insurance_row[1] if insurance_row else None,
                'claim_details_url': insurance_row[2] if insurance_row else None,
                'popup_screenshot': insurance_row[3] if insurance_row else None
            } if insurance_row else {}
            
            return {
                'car_results': car_results,
                'damage_result': damage_result,
                'insurance_result': insurance_result
            }
    except Exception as e:
        logging.error(f"Error retrieving session results: {e}")
        return None

# --- FILE MANAGEMENT HELPERS ---
def _get_session_id_by_case(case_id: str):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT SessionID FROM icp.v_UserSession WHERE CaseID = ?", (case_id,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logging.error(f"Lookup session by case failed: {e}")
        return None

def _load_case_into_session(case_id: str) -> bool:
    """Populate session with context required by PDF/email routes for a given CaseID."""
    session_id = _get_session_id_by_case(case_id)
    if not session_id:
        return False
    data = get_session_results(session_id) or {}
    # Minimal criteria object for templates
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT CaseID, Email, Mobile, Make, Model, BodyType
                FROM icp.v_UserSession WHERE SessionID = ?
                """,
                (session_id,)
            )
            usr = cur.fetchone()
            if usr:
                session['user_email'] = usr[1]
                session['user_mobile'] = usr[2]
                crit = {
                    'case_id': usr[0],
                    'make': usr[3],
                    'model_value': usr[4],
                    'body_type': usr[5],
                }
            else:
                crit = {'case_id': case_id}
    except Exception:
        crit = {'case_id': case_id}
    # Convert DB rows to the format templates expect
    results = data.get('car_results', [])
    session['criteria'] = crit
    session['case_id'] = case_id
    session['db_session_id'] = session_id
    session['last_results'] = results
    session['insurance_result'] = data.get('insurance_result', {})
    # build clean prices for summary
    clean_prices = []
    for car in results:
        try:
            p = parse_price(car.get('Price', ''))
            if p > 0:
                clean_prices.append(p)
        except Exception:
            continue
    store_collective_price_summary(session_id, clean_prices)
    return True

@app.route('/files')
@login_required
def files_portal():
    status_filter = request.args.get('status')  # 'Approved' | 'Rejected' | None
    q = (request.args.get('q') or '').strip()
    rows = []
    try:
        with get_db() as conn:
            cur = conn.cursor()
            sql = [
                """
                SELECT r.CaseID, r.RatingStatus, r.CreatedOn, r.ActionBy, r.ApprovedValue, r.RatingRemarks,
                       CASE WHEN EXISTS (SELECT 1 FROM icp.v_CarQuoteResult cq WHERE cq.CaseID = r.CaseID) THEN 1 ELSE 0 END AS HasQuotes
                FROM icp.v_RatingData r
                WHERE 1=1
                """
            ]
            params = []
            if status_filter in ('Approved', 'Rejected'):
                sql.append(" AND r.RatingStatus = ? ")
                params.append(status_filter)
            if q:
                sql.append(" AND r.CaseID LIKE ? ")
                params.append(f"%{q}%")
            sql.append(" ORDER BY r.CreatedOn DESC ")
            cur.execute("".join(sql), tuple(params))
            for row in cur.fetchall():
                rows.append({
                    'CaseID': row[0],
                    'Status': row[1],
                    'Date': row[2].strftime('%Y-%m-%d %H:%M:%S') if row[2] else '',
                    'ActionBy': row[3],
                    'ApprovedValue': row[4],
                    'Remarks': row[5],
                    'HasQuotes': bool(row[6]),
                })
    except Exception as e:
        logging.error(f"files_portal query error: {e}")
    return render_template('files.html', cases=rows, status_filter=status_filter, q=q)

@app.route('/files/generate_pdf/<case_id>')
@login_required
def files_generate_pdf(case_id):
    if not _load_case_into_session(case_id):
        return f"Case {case_id} not found", 404
    return generate_pdf()

@app.route('/files/send_email/<case_id>')
@login_required
def files_send_email(case_id):
    if not _load_case_into_session(case_id):
        return f"Case {case_id} not found", 404
    # ensure PDF exists before email
    try:
        generate_pdf()
    except Exception:
        pass
    return send_report_email_route()

def get_existing_rating(session_id):
    """Get existing rating data for a session"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT RatingStatus, RatingRemarks, ApprovedValue, ActionBy, CreatedOn
                FROM icp.v_RatingData 
                WHERE SessionID = ?
            """, (session_id,))
            
            rating_row = cursor.fetchone()
            if rating_row:
                return {
                    "status": rating_row[0],
                    "remarks": rating_row[1],
                    "approved_value": rating_row[2],
                    "action_by": rating_row[3],
                    "created_on": rating_row[4].strftime("%Y-%m-%d %H:%M:%S") if rating_row[4] else "Unknown"
                }
            return None
            
    except Exception as e:
        logging.error(f"Error getting existing rating: {e}")
        return None

@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    db = get_db()
    cursor = db.cursor()
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        cursor.execute("SELECT * FROM vidit_users WHERE username=?", (username,))
        row = cursor.fetchone()
        if row:
            colnames = [desc[0] for desc in cursor.description]
            user = dict(zip(colnames, row))
            now = datetime.now()
            if user['lock_until'] and user['lock_until'] > now:
                error = f"Account locked! Try after {(user['lock_until'] - now).seconds} seconds."
            elif user['active'] == 0:
                error = "Account is deactivated. Contact admin."
            elif user['expiry'] and user['expiry'] < now:
                error = "Account expired. Contact admin."
            elif check_password_hash(user['password_hash'], password):
                session['admin_id'] = user['id']
                session['username'] = username
                session['last_active'] = time.time()
                load_user_permissions_into_session(user)
                cursor.execute("UPDATE vidit_users SET failed_attempts=0, lock_until=NULL WHERE id=?", (user['id'],))
                db.commit()
                cursor.close()
                db.close()
                return redirect(url_for('dashboard'))
            else:
                failed = (user['failed_attempts'] or 0) + 1
                if failed >= 3:
                    lock_until = now + timedelta(minutes=3)
                    cursor.execute("UPDATE vidit_users SET failed_attempts=0, lock_until=? WHERE id=?", (lock_until, user['id']))
                    error = f"Account locked for 3 minutes."
                else:
                    cursor.execute("UPDATE vidit_users SET failed_attempts=? WHERE id=?", (failed, user['id']))
                    error = f"Invalid password! {3-failed} attempts left."
                db.commit()
        else:
            error = "No such user."
    cursor.close()
    db.close()
    return render_template('admin_login.html', error=error)

@app.route("/update_decision", methods=["POST", "GET"])
@login_required
def update_decision():
    # Support JSON body, form-encoded, or querystring for robustness
    decision = None
    if request.method == "GET":
        decision = request.args.get("decision")
    else:
        data = request.get_json(silent=True) or {}
        decision = data.get("decision") or request.form.get("decision")

    if decision not in ["Yes", "No"]:
        return jsonify({"error": "Invalid decision"}), 400

    session["damage_decision"] = decision
    session.modified = True
    
    # Update in database
    session_id = session.get('db_session_id')
    if session_id:
        update_damage_decision_db(session_id, decision)
        log_action(session_id, 'UpdateDecision', session.get('username', 'Unknown'), 
                  f"Damage decision updated to: {decision}")
    
    return jsonify({"success": True, "damage_decision": decision})

@app.route("/test_update", methods=["POST"])

def test_update():
    print("🔧 TEST_UPDATE: Route called")
    return jsonify({"success": True, "message": "Test route works"})

@app.route("/submit_rating", methods=["POST"])
@login_required
def submit_rating():
    data = request.get_json() or {}
    status = (data.get("status") or "").strip()
    remarks = data.get("remarks") or ""
    case_id = data.get("case_id") or ""
    approved_value_raw = data.get("approvedValue", None)  # may be number or null

    if not case_id:
        return jsonify({"error": True, "message": "Missing case_id"}), 400
    if status not in ("Approved", "Rejected"):
        return jsonify({"error": True, "message": "Invalid status"}), 400

    # Normalize approved_value: NULL for Rejected; numeric for Approved
    approved_value = None
    if status == "Approved":
        try:
            # Allow int/float/str numbers
            approved_value = float(approved_value_raw)
        except (TypeError, ValueError):
            return jsonify({"error": True, "message": "Approved value must be numeric"}), 400
    else:
        approved_value = None  # Rejected → force NULL

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session["rating_status"] = status
    session["rating_remarks"] = remarks
    session["rating_submitted_on"] = timestamp
    session["approved_value"] = approved_value
    session.modified = True

    # Log rating submission
    session_id = session.get('db_session_id')
    if session_id:
        # IMPORTANT: pass None for NULL numeric to avoid nvarchar→numeric conversion
        success, message = store_rating_data(
            session_id,
            status,
            remarks,
            approved_value,              # None inserts NULL in SQL Server
            session.get('username', 'Unknown')
        )
        if not success:
            return jsonify({"error": True, "message": message}), 400

        log_action(
            session_id,
            'SubmitRating',
            session.get('username', 'Unknown'),
            f"Rating submitted: {status} - Approved Value: {approved_value} - Remarks: {remarks}"
        )

    return jsonify({
        "error": False,
        "status": status,
        "remarks": remarks,
        "approvedValue": approved_value,  # will be null for Rejected
        "username": session.get('username', 'Unknown'),
        "timestamp": timestamp
    })


@app.route("/generate_pdf")
@login_required
def generate_pdf():
    try:
        crit               = session.get("criteria") or {}
        results            = session.get("last_results") or []
        google_image_result= session.get("google_image_result") or {}
        damage_result      = session.get("damage_decision") or session.get("damage_result", "No")
        damage_images      = session.get("damage_images") or []
        insurance_result   = session.get("insurance_result") or {}

        damage_decision    = session.get("damage_decision", "Not set")
        rating_status      = session.get("rating_status", "")
        rating_remarks     = session.get("rating_remarks", "")
        rating_submitted_on= session.get("rating_submitted_on", "")
        approved_value     = session.get("approved_value", "")

        if not crit or not results:
            return jsonify({"error": True, "message": "No results to generate PDF. Please run a search first."}), 400

        # --- Compress images for smaller PDFs ---
        def _compress_b64(img_b64: str, max_w=900, quality=70) -> str:
            try:
                if not (img_b64 and img_b64.startswith("data:image")):
                    return img_b64
                header, b64 = img_b64.split(",", 1)
                data = base64.b64decode(b64)
                im = Image.open(io.BytesIO(data)).convert("RGB")
                w, h = im.size
                if w > max_w:
                    im.thumbnail((max_w, int(max_w * h / max(w, 1))), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=70, optimize=True)
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            except Exception:
                return img_b64

        def _compress_file(path: str, max_w=900, quality=70) -> str:
            try:
                im = Image.open(path).convert("RGB")
                w, h = im.size
                if w > max_w:
                    im.thumbnail((max_w, int(max_w * h / max(w, 1))), Image.Resampling.LANCZOS)
                out_dir = os.path.join("static", "tmp_pdf")
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, f"{uuid.uuid4().hex}.jpg")
                im.save(out_path, "JPEG", quality=quality, optimize=True)
                # Return a web-friendly relative path with forward slashes
                rel = os.path.relpath(out_path, os.getcwd()).replace(os.sep, "/")
                return rel
            except Exception:
                return path

        # zipped images (optional)
        zipped_damage_images = []
        for pair in damage_images:
            inp = _compress_b64(pair.get("input", ""))
            out = _compress_b64(pair.get("output", ""))
            if inp.startswith("data:image") and out.startswith("data:image"):
                zipped_damage_images.append((inp, out))

        # ✅ Handle popup screenshot for BOTH success and "Claim Not Found"
        popup_base64 = None
        popup_path   = insurance_result.get("popup_screenshot")
        if popup_path:
            full = os.path.join(os.getcwd(), popup_path.replace("/", os.sep))
            if os.path.exists(full):
                with open(full, "rb") as f:
                    data = base64.b64encode(f.read()).decode("utf-8")
                    popup_base64 = f"data:image/png;base64,{data}"
        else:
            # If screenshot path is missing, still try from session (in case stored separately)
            popup_path = session.get("popup_screenshot")
            if popup_path:
                full = os.path.join(os.getcwd(), popup_path.replace("/", os.sep))
                if os.path.exists(full):
                    with open(full, "rb") as f:
                        data = base64.b64encode(f.read()).decode("utf-8")
                        popup_base64 = f"data:image/png;base64,{data}"

        # --- RENDER ONCE, SAVE TO SESSION ---
        # Pre-compress google images (downloaded files)
        raw_images = google_image_result.get("downloaded_images", []) if google_image_result else []
        pdf_images = [_compress_file(p) for p in raw_images][:12]
        rendered_html = render_template(
            "pdf_template.html",
            criteria=crit,
            results=results,
            google_image_result=google_image_result,
            damage_result=damage_result,
            damage_images=zipped_damage_images,
            insurance_result=insurance_result,
            popup_base64=popup_base64,  # ✅ Always passed, even if Claim Not Found
            damage_decision=damage_decision,
            rating_status=rating_status,
            rating_remarks=rating_remarks,
            rating_submitted_on=rating_submitted_on,
            approved_value=approved_value,
            downloaded_images=pdf_images,
        )

        session["last_report_html"] = rendered_html  # <--- SAVE FOR EMAIL

        timestamp = datetime.now().strftime("%d-%m-%Y__%H-%M-%S")
        filename  = f"report_{timestamp}.pdf"
        # ensure static exists
        os.makedirs("static", exist_ok=True)
        filepath  = os.path.join("static", filename)

        # Prefer wkhtmltopdf with quality caps; fall back to Playwright
        try:
            wkhtml_path = os.getenv('WKHTMLTOPDF', r"C:\\Program Files\\wkhtmltopdf\\bin\\wkhtmltopdf.exe")
            config = pdfkit.configuration(wkhtmltopdf=wkhtml_path)
            options = {
                "encoding": "UTF-8",
                "enable-local-file-access": None,
                "image-quality": "60",
                "dpi": "96",
                "quiet": None,
            }
            pdfkit.from_string(rendered_html, filepath, configuration=config, options=options)
            if not os.path.exists(filepath) or os.path.getsize(filepath) < 2048:
                raise RuntimeError("wkhtmltopdf produced too-small PDF")
        except Exception:
            try:
                from playwright.sync_api import sync_playwright
                import subprocess, sys
                try:
                    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps", "--no-shell"], check=True)
                except Exception:
                    pass
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    page.set_content(rendered_html, wait_until="load")
                    pdf_bytes = page.pdf(format="A4", print_background=True, margin={"top":"10mm","right":"10mm","bottom":"10mm","left":"10mm"})
                    browser.close()
                with open(filepath, "wb") as f:
                    f.write(pdf_bytes)
            except Exception as _e:
                fallback_html = os.path.join("static", f"report_{timestamp}.html")
                try:
                    with open(fallback_html, "w", encoding="utf-8") as f:
                        f.write(rendered_html)
                except Exception:
                    pass
                return jsonify({"error": True, "message": f"PDF generation failed, saved HTML instead: {fallback_html}"}), 500

        # Verify generated file is a real PDF
        try:
            if not os.path.exists(filepath) or os.path.getsize(filepath) < 2048:
                raise RuntimeError("Generated PDF is missing or too small")
            with open(filepath, "rb") as _pf:
                _hdr = _pf.read(5)
            if _hdr != b"%PDF-":
                raise RuntimeError("Generated file is not a valid PDF")
        except Exception as verify_err:
            return jsonify({"error": True, "message": str(verify_err)}), 500

        session["last_pdf_path"] = filepath

        return send_file(filepath, as_attachment=True, download_name=filename, mimetype="application/pdf")

    except Exception as e:
        return jsonify({"error": True, "message": f"PDF generation failed: {str(e)}"}), 500

def filter_scraper_args(crit, allowed_keys):
    return {k: crit.get(k) for k in allowed_keys if crit.get(k) is not None}

@app.route('/send_report_email', methods=['POST'])
@login_required
def send_report_email_route():
    """
    Send a plain-text email only.
    - No HTML body rendering
    - No PDF attachment
    - Old logic kept commented for future use
    """
    try:
        # Keep original log message
        print("➡️ Received request to send report email.")

        user_email = session.get("user_email")
        crit       = session.get("criteria", {}) or {}
        case_id    = crit.get("case_id", "N/A")
        make       = crit.get("make", "N/A")
        model      = crit.get("model_value", "N/A")
        year_min   = crit.get("year_min", "")
        year_max   = crit.get("year_max", "")
        chassis    = crit.get("chasis_no", "")
        today      = datetime.now().strftime("%d-%m-%Y")

        # Resolve case decision from session or DB
        decision   = session.get("rating_status")
        session_id = session.get('db_session_id')
        if not decision and session_id:
            existing = get_existing_rating(session_id)
            if existing and isinstance(existing, dict):
                decision = existing.get("status")

        if not user_email:
            print("❌ No recipient email provided.")
            return "Recipient email is missing.", 400

        # Subject stays the same as before
        subject = f"Your Vehicle Report | {case_id} | {make} | {today}"

        # -------- PLAIN TEXT BODY (no HTML) --------
        body_text_lines = [
            "Hi,",
            "",
            f"Your vehicle case {case_id} has been processed.",
            "",
            f"CASE DECISION: {decision or '—'}",
            "",
            "Details:",
            f"- Make/Model: {make} {model}",
            f"- Year: {year_min if year_min else '—'}" + (f" - {year_max}" if (year_max and year_max != year_min) else ""),
            f"- Chassis: {chassis or '—'}",
            "",
            "If you need more details please contact your agent.",
            "",
            "Regards,",
            "Support Team",
        ]
        body_text = "\n".join(body_text_lines)

        # =========================
        # 🔒 OLD LOGIC (commented)
        # =========================
        # We previously embedded the full HTML report and attached a PDF:
        #
        # pdf_path = session.get("last_pdf_path")
        # rendered_html = session.get("last_report_html", "")
        # if not rendered_html:
        #     return "No report available to email. Please generate PDF first.", 400
        # intro = """
        #     <p style="font-size:16px;font-family:sans-serif">
        #         Hi,<br>Your vehicle report has been generated. Please see the details below, and the attached PDF for more details.
        #     </p>
        #     <hr>
        # """
        # body_html = intro + rendered_html
        # if not pdf_path or not os.path.exists(pdf_path):
        #     return f"PDF file missing at {pdf_path}", 400
        #
        # print("📧 Calling send_report_email()...")
        # send_report_email(user_email, subject, body_html, pdf_path)

        # ✅ NEW: plain text only (no attachment)
        print("📧 Calling send_report_email()...")
        try:
            # If your helper supports (to, subject, body)
            send_report_email(user_email, subject, body_text)
        except TypeError:
            # If your helper requires an attachment param, pass None
            send_report_email(user_email, subject, body_text, None)

        # Keep original success log + response
        print("✅ Email sent successfully.")

        # Log the action to DB exactly like before
        session_id = session.get('db_session_id')
        if session_id:
            log_action(
                session_id,
                'SendEmail',
                session.get('username', 'Unknown'),
                f"Plain text email sent to {user_email} (no attachment)"
            )

        # Keep the same response so your frontend notifications remain unchanged
        return "Report sent successfully!", 200

    except Exception as e:
        import traceback
        print("❌ Exception occurred while sending email:")
        print(traceback.format_exc())
        return f"Failed to send email: {str(e)}", 500


@app.route('/refresh_source', methods=['POST'])
@login_required
def refresh_source():
    import pandas as pd

    data = request.get_json()
    source = data.get('source')
    crit = session.get("criteria", {})
    # Get session_id from session storage
    session_id = session.get('db_session_id')
    
    if not source or not crit:
        return jsonify({"error": "Missing source or criteria"}), 400

    # Map sources to scraper functions and template files
    SCRAPER_FUNCS = {
        "opensooq": (scrape_opensooq, "partials/opensooq_card.html"),
        "yallamotor": (scrape_yallamotor, "partials/yallamotor_card.html"),
        "drivearabia": (scrape_drivearabia, "partials/drivearabia_card.html"),
        "google_image": (google_chasis_image_search, "partials/google_image_card.html"),
        "insurance_lookup": (lookup_insurance_claim, "partials/insurance_card.html"),
        "damage_detection": (None, "partials/damage_card.html"),
    }

    fn, template_file = SCRAPER_FUNCS.get(source, (None, None))
    if not template_file:
        return jsonify({"error": "Unknown source"}), 400

    def filter_scraper_args(crit, allowed_keys):
        return {k: crit.get(k) for k in allowed_keys if crit.get(k) is not None}

    def merge_source_results(source_name, new_results):
        all_results = session.get("last_results", [])
        # Standardize to expected source names
        source_key = {
            "drivearabia": "DriveArabia",
            "yallamotor": "YallaMotor",
            "opensooq": "OpenSooq"
        }.get(source_name, source_name[0].upper() + source_name[1:])
        
        # Check if new_results contains valid car data (not just error entries)
        valid_new = [r for r in (new_results or []) 
                     if (r.get("CarName") or r.get("Car Name") or r.get("name") or r.get("Title"))
                     and not r.get("error")]
        
        if not valid_new and new_results:
            # Scraper failed / returned only error entries — keep existing data
            print(f"⚠️ [{source_key}] Re-run returned no valid results, keeping existing data")
            log_action(session_id, 'RefreshSource', session.get('username', 'Unknown'), 
                      f"Refreshed {source_name} — scraper failed, existing data preserved")
            return all_results
        
        # Remove old
        updated_results = [r for r in all_results if r.get("Source") != source_key]
        # Set Source field for new entries
        for r in new_results:
            r["Source"] = source_key
        updated_results.extend(new_results)
        session["last_results"] = updated_results
        session.modified = True
        
        # Update database with rollback
        if session_id:
            store_car_results_with_rollback(session_id, new_results, source_key)
            
            # Update price summary - use global parse_price function
            clean_prices = []
            for car in updated_results:
                price = parse_price(car.get("Price",""))
                if price > 0:
                    clean_prices.append(price)
            
            store_collective_price_summary(session_id, clean_prices)
            
            # Log refresh action
            log_action(session_id, 'RefreshSource', session.get('username', 'Unknown'), 
                      f"Refreshed {source_name} results")
        
        return updated_results

    def write_excel(results):
        if results:
            try:
                case_id_for_excel = session.get('case_id') or session.get('CASE_ID')
                excel_dir = os.path.join("static", "results", str(case_id_for_excel or "unknown"))
                os.makedirs(excel_dir, exist_ok=True)
                excel_fs_path = os.path.join(excel_dir, "results.xlsx")
            except Exception:
                excel_fs_path = os.path.join("static", "results.xlsx")
            with pd.ExcelWriter(excel_fs_path, engine="openpyxl") as writer:
                for name in ["DriveArabia", "YallaMotor", "OpenSooq"]:
                    df = pd.DataFrame([car for car in results if car.get("Source") == name])
                    if not df.empty:
                        df.to_excel(writer, sheet_name=name, index=False)
            try:
                session["excel_web_path"] = f"/static/results/{case_id_for_excel}/results.xlsx"
            except Exception:
                session["excel_web_path"] = "/static/results.xlsx"
        else:
            print("⚠️ No data scraped. Skipping Excel write.")

    # -- MAIN LOGIC --
    context = {}
    summary_html = None

    if source in ["opensooq", "yallamotor", "drivearabia"]:
        # Handle year parsing for individual source refresh
        # Check if we have year_min/year_max (from search) or year (from form)
        year_min = crit.get("year_min")
        year_max = crit.get("year_max")
        
        # If we don't have year_min/year_max, try to parse from single year field
        if year_min is None and year_max is None:
            year_value = crit.get("year")
            if year_value:
                try:
                    year_int = int(year_value)
                    year_min = year_int
                    year_max = year_int
                except ValueError:
                    pass
        
        # Create criteria with parsed year values
        scraper_criteria = {
            "make": crit.get("make"),
            "model_value": crit.get("model_value"),
            "body_type": crit.get("body_type"),
            "price_min": crit.get("price_min"),
            "price_max": crit.get("price_max"),
            "year_min": year_min,
            "year_max": year_max,
            "page_num": crit.get("page_num", 1)
        }
        
        allowed_keys = ["make", "model_value", "body_type", "price_min", "price_max", "year_min", "year_max", "page_num"]
        result = fn(**filter_scraper_args(scraper_criteria, allowed_keys))
        merged = merge_source_results(source, result)
        write_excel(merged)
        context = {"source_results": result}
        # ---- Generate up-to-date summary block as well! ----
        clean_prices = []
        for car in merged:
            price = parse_price(car.get("Price",""))
            if price > 0:
                clean_prices.append(price)
        summary_html = render_template("partials/collective_summary.html", clean_prices=clean_prices)

    elif source == "google_image":
        if crit.get("google_image_check") and crit.get("chasis_no"):
            result = fn(chasis_no=crit.get("chasis_no"))
            session["google_image_result"] = result
            session.modified = True
            # Store in database
            if session_id and result:
                store_google_images(session_id, result)
            # Log refresh action
            log_action(session_id, 'RefreshSource', session.get('username', 'Unknown'), 
                      f"Refreshed Google Image results")
        else:
            result = {"error": "Google Image check not enabled or chassis number missing"}
        context = {"google_image_result": result}

    elif source == "insurance_lookup":
        if crit.get("insurance_lookup") and crit.get("chasis_no"):
            result = fn(chasis_no=crit.get("chasis_no"))
            session["insurance_result"] = result
            session.modified = True
            # Store in database
            if session_id and result:
                store_insurance_check(session_id, result)
            # Log refresh action
            log_action(session_id, 'RefreshSource', session.get('username', 'Unknown'), 
                      f"Refreshed Insurance Lookup results")
        else:
            result = {"error": "Insurance Lookup not enabled or chassis number missing"}
        context = {"insurance_result": result}

    elif source == "damage_detection":
        # Classic behavior: re-run on the session-stored originals (if any)
        original_files = session.get("original_damage_files", [])
        new_damage_images = []
        damage_detected = False

        if not original_files:
            result = {"error": "No damage detection files found"}
            context = {"damage_result": result}
        else:
            print(f"🔄 Re-processing {len(original_files)} images with Gradio API...")
            
            for idx, base64img in enumerate(original_files):
                print(f"🔍 Re-processing image {idx + 1}/{len(original_files)}...")
                
                try:
                    has_damage, damage_info = detect_damage_with_gradio(base64img)
                    
                    if damage_info.get("success"):
                        if has_damage:
                            damage_detected = True
                        new_damage_images.append({
                            "input": base64img,
                            "has_damage": has_damage,
                            "damage_image": damage_info.get("damage_image"),
                            "scratch_image": damage_info.get("scratch_image"),
                            "parts_image": damage_info.get("parts_image"),
                            "damage_description": damage_info.get("damage_description", ""),
                            "manual_review": damage_info.get("manual_review", False)
                        })
                        print(f"✅ Refresh image {idx + 1} processed - Damage found: {has_damage}")
                    else:
                        print(f"⚠️ Refresh API failed for image {idx + 1} - requiring manual review")
                        new_damage_images.append({
                            "input": base64img,
                            "has_damage": False,
                            "error": "API processing failed - manual review required"
                        })
                        
                except Exception as api_error:
                    print(f"❌ Refresh API error for image {idx + 1}: {str(api_error)}")
                    new_damage_images.append({
                        "input": base64img,
                        "has_damage": False,
                        "error": "API processing failed - manual review required"
                    })

            # Manual override takes precedence
            manual_decision = session.get("damage_decision")
            if manual_decision in ["Yes", "No"]:
                final_damage_result = manual_decision
            else:
                final_damage_result = "Yes" if damage_detected else "No"

            session["damage_images"] = new_damage_images
            session["damage_result"] = final_damage_result
            session.modified = True

            # Store in database if we have results
            if session_id and new_damage_images:
                store_damage_detection(session_id, final_damage_result, new_damage_images)

            # Log refresh action
            log_action(session_id, 'RefreshSource', session.get('username', 'Unknown'), 
                      f"Refreshed Damage Detection results")

            context = {
                "damage_result": final_damage_result,
                "damage_images": new_damage_images
            }

    else:
        return jsonify({"error": "Not implemented"}), 400

    html = render_template(template_file, **context)
    resp = {"html": html}
    if summary_html:
        resp["summary_html"] = summary_html
    return jsonify(resp)

@app.context_processor
def inject_parse_price():
    return dict(parse_price=parse_price)

@app.route("/debug_session")
@login_required
def debug_session():
    return jsonify({
        "damage_result": session.get("damage_result"),
        "damage_decision": session.get("damage_decision"),
        "damage_images": len(session.get("damage_images", [])),
        "original_damage_files": len(session.get("original_damage_files", [])),
        "session_keys": list(session.keys())
    })

@app.route('/check_rating_exists/<case_id>')
@login_required
def check_rating_exists(case_id):
    """Check if rating already exists for a case"""
    try:
        # Get session_id from case_id
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT SessionID FROM icp.v_UserSession 
                WHERE CaseID = ?
            """, (case_id,))
            
            session_row = cursor.fetchone()
            if not session_row:
                return jsonify({"exists": False})
            
            session_id = session_row[0]
            
            # Check if rating exists for this session
            cursor.execute("""
                SELECT RatingStatus, CreatedOn FROM icp.v_RatingData 
                WHERE SessionID = ?
            """, (session_id,))
            
            rating_row = cursor.fetchone()
            if rating_row:
                return jsonify({
                    "exists": True,
                    "rating": {
                        "status": rating_row[0],
                        "timestamp": rating_row[1].strftime("%Y-%m-%d %H:%M:%S") if rating_row[1] else "Unknown"
                    }
                })
            else:
                return jsonify({"exists": False})
                
    except Exception as e:
        logging.error(f"Error checking rating existence: {e}")
        return jsonify({"exists": False, "error": str(e)})

# 🚀 COMPREHENSIVE PROGRESS TRACKING SYSTEM
class ProgressTracker:
    def __init__(self):
        self.active_cases = {}  # case_id -> progress info
        self._lock = threading.Lock()
    
    def start_case(self, case_id, criteria):
        """Initialize progress tracking for a new case"""
        with self._lock:
            self.active_cases[case_id] = {
            'start_time': datetime.now(),
            'criteria': criteria,
            'modules': {
                'preparing': {'status': 'pending', 'started_at': None, 'completed_at': None},
                'drivearabia': {'status': 'pending', 'started_at': None, 'completed_at': None},
                'yallamotor': {'status': 'pending', 'started_at': None, 'completed_at': None},
                'opensooq': {'status': 'pending', 'started_at': None, 'completed_at': None},
                'google_image': {'status': 'pending', 'started_at': None, 'completed_at': None},
                'insurance_lookup': {'status': 'pending', 'started_at': None, 'completed_at': None},
                'damage_detection': {'status': 'pending', 'started_at': None, 'completed_at': None},
                'results': {'status': 'pending', 'started_at': None, 'completed_at': None}
            },
            'current_module': 'preparing',
            'overall_progress': 0
        }
        
        # Immediately mark disabled modules as completed so percent matches visible steps
        try:
            sources = criteria.get('sources', []) or []
            if 'drivearabia' not in sources:
                self.update_module_status(case_id, 'drivearabia', 'completed', 'Module not selected')
            if 'yallamotor' not in sources:
                self.update_module_status(case_id, 'yallamotor', 'completed', 'Module not selected')
            if 'opensooq' not in sources:
                self.update_module_status(case_id, 'opensooq', 'completed', 'Module not selected')
            if not criteria.get('google_image_check'):
                self.update_module_status(case_id, 'google_image', 'completed', 'Module not selected')
            if not criteria.get('insurance_lookup'):
                self.update_module_status(case_id, 'insurance_lookup', 'completed', 'Module not selected')
            if not criteria.get('damage_detection'):
                self.update_module_status(case_id, 'damage_detection', 'completed', 'Module not selected')
        except Exception:
            pass
        
        # Mark preparing as started
        self.update_module_status(case_id, 'preparing', 'started')
        return self.active_cases[case_id]
    
    def update_module_status(self, case_id, module_name, status, message=""):
        """Update the status of a specific module"""
        updated = False
        with self._lock:
            if case_id not in self.active_cases:
                return
            case = self.active_cases[case_id]
            if module_name in case['modules']:
                module = case['modules'][module_name]
                module['status'] = status
                module['message'] = message
                
                if status == 'started':
                    module['started_at'] = datetime.now()
                elif status == 'completed':
                    module['completed_at'] = datetime.now()
                
                # Update current module
                if status == 'started':
                    case['current_module'] = module_name
                updated = True
        if updated:
            # Calculate overall progress (acquires lock internally)
            self._calculate_progress(case_id)
    
    def _calculate_progress(self, case_id):
        """Calculate overall progress percentage"""
        with self._lock:
            if case_id not in self.active_cases:
                return
            case = self.active_cases[case_id]
            total_modules = len(case['modules'])
            completed_modules = sum(1 for m in case['modules'].values() if m['status'] == 'completed')
            case['overall_progress'] = round((completed_modules / total_modules) * 100)
    
    def get_progress(self, case_id):
        """Get current progress for a case"""
        with self._lock:
            if case_id not in self.active_cases:
                return None
            return dict(self.active_cases[case_id])
    
    def complete_case(self, case_id):
        """Mark a case as fully completed"""
        # Determine which modules need completion under lock
        modules_to_complete = []
        with self._lock:
            case = self.active_cases.get(case_id)
            if not case:
                return
            for module_name, module in case['modules'].items():
                if module['status'] != 'completed':
                    modules_to_complete.append(module_name)
        # Complete modules outside the lock
        for module_name in modules_to_complete:
            self.update_module_status(case_id, module_name, 'completed', 'Completed')
        # Mark results as completed
        self.update_module_status(case_id, 'results', 'completed', 'Results ready')
        # Calculate final progress
        self._calculate_progress(case_id)
    
    def cleanup_case(self, case_id):
        """Remove case from tracking (after results are displayed)"""
        with self._lock:
            if case_id in self.active_cases:
                del self.active_cases[case_id]

# Global progress tracker instance
progress_tracker = ProgressTracker()

# 🚀 PROGRESS TRACKING ENDPOINT (IMPROVED)
@app.route('/progress/<case_id>')
def get_progress(case_id):
    """Get progress for a specific case from database"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Get progress status for this case
            cursor.execute("""
                SELECT 
                    CASE WHEN EXISTS (SELECT 1 FROM icp.v_CarQuoteResult WHERE CaseID = ?) THEN 1 ELSE 0 END AS CarQuotesDone,
                    CASE WHEN EXISTS (SELECT 1 FROM icp.v_DamageDetection WHERE CaseID = ?) THEN 1 ELSE 0 END AS DamageDetectionDone,
                    CASE WHEN EXISTS (SELECT 1 FROM icp.v_GoogleImageCheck WHERE CaseID = ?) THEN 1 ELSE 0 END AS GoogleImageCheckDone,
                    CASE WHEN EXISTS (SELECT 1 FROM icp.v_InsuranceClaimCheck WHERE CaseID = ?) THEN 1 ELSE 0 END AS InsuranceCheckDone,
                    CASE WHEN EXISTS (SELECT 1 FROM icp.v_CollectivePriceSummary WHERE CaseID = ?) THEN 1 ELSE 0 END AS PriceSummaryDone
                FROM icp.v_UserSession 
                WHERE CaseID = ?
            """, (case_id, case_id, case_id, case_id, case_id, case_id))
            
            result = cursor.fetchone()
            if result:
                progress = {
                    "case_id": case_id,
                    "car_quotes_done": bool(result[0]),
                    "damage_detection_done": bool(result[1]),
                    "google_image_check_done": bool(result[2]),
                    "insurance_check_done": bool(result[3]),
                    "price_summary_done": bool(result[4]),
                    "timestamp": datetime.now().isoformat()
                }
                
                # Calculate overall progress percentage
                total_modules = 5
                completed_modules = sum([
                    progress["car_quotes_done"],
                    progress["damage_detection_done"], 
                    progress["google_image_check_done"],
                    progress["insurance_check_done"],
                    progress["price_summary_done"]
                ])
                progress["overall_percent"] = round((completed_modules / total_modules) * 100)
                
                return jsonify(progress)
            else:
                return jsonify({"error": "Case not found"}), 404
                
    except Exception as e:
        logging.error(f"Error getting progress for case {case_id}: {e}")
        return jsonify({"error": str(e)}), 500

# 🚀 REAL-TIME PROGRESS STREAMING ENDPOINT (IMPROVED)
@app.route('/progress_stream/<case_id>')
def progress_stream(case_id):
    """Stream real-time progress updates for a case"""
    def generate():
        try:
            # Get case details
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT SessionID, Make, Model, BodyType, GoogleImageCheck, InsuranceLookup
                    FROM v_UserSession WHERE CaseID = ?
                """, (case_id,))
                case_row = cursor.fetchone()
                
                if not case_row:
                    yield f"data: {json.dumps({'error': 'Case not found'})}\n\n"
                    return
                
                session_id, make, model, body_type, google_check, insurance_check = case_row
                
                # Initialize progress tracking for this case only if not already active
                criteria = {
                    'google_image_check': google_check,
                    'insurance_lookup': insurance_check,
                    'make': make,
                    'model': model,
                    'body_type': body_type
                }
                if not progress_tracker.get_progress(case_id):
                    progress_tracker.start_case(case_id, criteria)
                    # Send initial progress only when starting fresh
                    yield f"data: {json.dumps({'module': 'preparing', 'status': 'started', 'message': 'Initializing search...'})}\n\n"
                    last_progress = 0
                else:
                    # Always send a snapshot of current progress to avoid missing early updates
                    snap = progress_tracker.get_progress(case_id)
                    if snap:
                        payload = {
                            "type": "progress_update",
                            "overall_progress": snap.get('overall_progress', 0),
                            "current_module": snap.get('current_module'),
                            "modules": snap.get('modules', {})
                        }
                        yield "data: " + json.dumps(payload) + "\n\n"
                        last_progress = snap.get('overall_progress', 0)
                
                # Monitor progress and send updates
                # last_progress may be set above when sending initial snapshot
                try:
                    last_progress
                except NameError:
                    last_progress = 0
                while True:
                    progress = progress_tracker.get_progress(case_id)
                    if not progress:
                        break
                    
                    # Send progress updates
                    if progress['overall_progress'] != last_progress:
                        payload = {
                            "type": "progress_update",
                            "overall_progress": progress['overall_progress'],
                            "current_module": progress['current_module'],
                            "modules": progress['modules']
                        }
                        yield "data: " + json.dumps(payload) + "\n\n"
                        last_progress = progress['overall_progress']
                    else:
                        # Heartbeat every ~15 seconds to keep intermediaries from timing out
                        if not hasattr(generate, "_hb"):
                            generate._hb = time.time()
                        now = time.time()
                        if now - generate._hb >= 15:
                            yield 'data: {"type":"heartbeat"}\n\n'
                            generate._hb = now
                    
                    # Check if case is completed
                    if progress['overall_progress'] >= 100:
                        yield f"data: {json.dumps({'type': 'completed', 'message': 'All modules completed'})}\n\n"
                        break
                    
                    time.sleep(1)  # Check every second
                    
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    resp = Response(generate(), mimetype='text/event-stream')
    try:
        resp.headers['Cache-Control'] = 'no-cache'
        resp.headers['X-Accel-Buffering'] = 'no'
        resp.headers['Connection'] = 'keep-alive'
    except Exception:
        pass
    return resp

# Global variable to store progress callbacks
progress_callbacks = []

def send_progress_update(update_type, process_name, message=""):
    """Send progress update to all connected clients"""
    data = {
        "type": update_type,
        "process": process_name,
        "message": message,
        "timestamp": datetime.now().isoformat()
    }
    
    # Store in session for potential retrieval
    if 'progress_updates' not in session:
        session['progress_updates'] = []
    session['progress_updates'].append(data)
    
    # Log progress update
    logging.info(f"PROGRESS: {update_type} - {process_name} - {message}")
    
    # In a real implementation, you'd send this via SSE
    # For now, we'll store it and the frontend can poll if needed
    print(f"📊 PROGRESS: {update_type} - {process_name} - {message}")

def send_process_start(process_name, message=""):
    """Send process start notification"""
    send_progress_update("process_start", process_name, message)

def send_process_complete(process_name, message=""):
    """Send process complete notification"""
    send_progress_update("process_complete", process_name, message)

def send_process_error(process_name, message=""):
    """Send process error notification"""
    send_progress_update("process_error", process_name, message)

def send_search_complete():
    """Send search completion notification"""
    send_progress_update("search_complete", "Search", "All processes completed successfully")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
