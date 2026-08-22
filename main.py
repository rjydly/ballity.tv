import os
import re
import csv
import json
import glob
import html
import time
import base64
import subprocess
from io import BytesIO
import requests
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import yt_dlp
from moviepy import VideoFileClip, CompositeVideoClip, ImageClip, concatenate_videoclips

# ==========================================
# CONFIGURACIÓ PRINCIPAL
# ==========================================

# True per a fer proves (envia tot a Telegram sense publicar a Buffer).
# Canvia a False per a posar-ho en producció.
TEST_MODE = True

# Secrets i credencials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INSTAGRAM_COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BUFFER_ACCESS_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_CHANNEL_IDS = os.getenv("BUFFER_CHANNEL_IDS")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")

# Rutes de recursos i carpetes
ASSETS_DIR = "assets"
FONTS_DIR = os.path.join(ASSETS_DIR, "fonts")
LOGO_PATH = os.path.join(ASSETS_DIR, "logo.png")
VIDEOS_DIR = "videos"

# Text de drets obligatori al final del caption
DISCLAIMER_TEXT = "All rights belong to the respective owner. DM for credit or removal."


# ==========================================
# GESTIÓ D'HISTORIAL I CSV
# ==========================================

def load_processed_ids():
    if os.path.exists("processed_videos.json"):
        try:
            with open("processed_videos.json", "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_processed_id(video_id):
    if TEST_MODE:
        print("ℹ️ TEST_MODE actiu: No es desa l'ID a processed_videos.json")
        return
    history = load_processed_ids()
    if video_id not in history:
        history.append(video_id)
        with open("processed_videos.json", "w") as f:
            json.dump(history, f, indent=4)


def update_csv_status(target_url, new_status="done"):
    """Actualitza la columna status a sources.csv."""
    if not os.path.exists("sources.csv") or TEST_MODE:
        return

    rows = []
    with open("sources.csv", mode="r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if row[0].strip() == target_url.strip():
                rows.append([row[0].strip(), new_status])
            else:
                rows.append(row)

    with open("sources.csv", mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    print(f"📝 sources.csv actualitzat: {target_url} -> {new_status}")


# ==========================================
# GESTIÓ DE MEDIA I COMMIT A GITHUB
# ==========================================

def cleanup_videos_dir(keep_filenames=None):
    """Elimina fitxers antics de la carpeta videos/."""
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    keep_filenames = keep_filenames or []
    for f in os.listdir(VIDEOS_DIR):
        if f not in keep_filenames:
            full_path = os.path.join(VIDEOS_DIR, f)
            try:
                if os.path.isfile(full_path):
                    os.remove(full_path)
            except OSError:
                pass


def push_media_to_github(video_rel_filename, thumbnail_rel_filename="final_thumbnail.jpg"):
    """Neteja videos antics, afegeix el nou i fa push a GitHub abans de cridar Buffer."""
    if TEST_MODE:
        return True

    print("📤 Netejant fitxers antics i fent push a GitHub...")
    try:
        cleanup_videos_dir(keep_filenames=[video_rel_filename, thumbnail_rel_filename])

        # git add -A garanteix que els fitxers esborrats es registrin a Git
        subprocess.run(["git", "add", "-A", VIDEOS_DIR, "processed_videos.json", "sources.csv"], check=False)
        subprocess.run(["git", "commit", "-m", f"Publish {video_rel_filename} [skip ci]"], check=False)
        subprocess.run(["git", "push"], check=True)
        print("✅ Carpeta videos/ sincronitzada a GitHub amb èxit!")
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ Error fent push a GitHub: {e}")
        return False


# ==========================================
# PUBLICACIÓ VIA BUFFER GRAPHQL API
# ==========================================

def get_channel_service(channel_id, headers):
    """Obté el servei del canal (instagram, facebook, tiktok) de manera unitària."""
    query = """
    query GetChannel($input: ChannelInput!) {
      channel(input: $input) {
        id
        service
      }
    }
    """
    try:
        res = requests.post(
            "https://api.buffer.com",
            headers=headers,
            json={"query": query, "variables": {"input": {"id": channel_id}}},
            timeout=15
        )
        data = res.json()
        ch = (data.get("data") or {}).get("channel")
        if ch and "service" in ch:
            return str(ch["service"]).lower()
    except Exception as e:
        print(f"ℹ️ Consulta canal {channel_id}: {e}")
    return ""


def publish_to_buffer(caption_text, video_filename, thumbnail_offset_ms=0):
    """Publica el vídeo des de la carpeta videos/ a tots els canals connectats a Buffer."""
    if not BUFFER_ACCESS_TOKEN or not BUFFER_CHANNEL_IDS or not GITHUB_REPOSITORY:
        print("⚠️ Dades de Buffer o GITHUB_REPOSITORY no configurades. S'omet la publicació.")
        return False

    public_video_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{VIDEOS_DIR}/{video_filename}"
    print(f"🌐 URL pública del vídeo per a Buffer: {public_video_url}")

    channel_list = [c.strip() for c in BUFFER_CHANNEL_IDS.split(",") if c.strip()]
    if not channel_list:
        print("⚠️ No hi ha cap channel_id vàlid a BUFFER_CHANNEL_IDS.")
        return False

    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            status
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """

    all_success = True
    for channel_id in channel_list:
        service = get_channel_service(channel_id, headers)

        post_input = {
            "channelId": channel_id,
            "text": caption_text,
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": [
                {
                    "video": {
                        "url": public_video_url,
                        "metadata": {
                            "thumbnailOffset": thumbnail_offset_ms
                        }
                    }
                }
            ]
        }

        if "instagram" in service:
            post_input["metadata"] = {
                "instagram": {
                    "type": "reel",
                    "shouldShareToFeed": True
                }
            }
        elif "facebook" in service:
            post_input["metadata"] = {
                "facebook": {
                    "type": "reel"
                }
            }

        def send_request(inp):
            return requests.post(
                "https://api.buffer.com",
                headers=headers,
                json={"query": mutation, "variables": {"input": inp}},
                timeout=30
            )

        try:
            print(f"🚀 Publicant al canal de Buffer ({service.upper() or 'CANAL'} - {channel_id})...")
            response = send_request(post_input)
            res_data = response.json()

            result = (res_data.get("data") or {}).get("createPost", {})
            error_msg = result.get("message") or ""

            if "Instagram posts require a type" in error_msg:
                print("🔄 Reintentant com a Instagram Reel...")
                post_input["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
                response = send_request(post_input)
                res_data = response.json()
                result = (res_data.get("data") or {}).get("createPost", {})
                error_msg = result.get("message") or ""
            elif "Facebook posts require a type" in error_msg:
                print("🔄 Reintentant com a Facebook Reel...")
                post_input["metadata"] = {"facebook": {"type": "reel"}}
                response = send_request(post_input)
                res_data = response.json()
                result = (res_data.get("data") or {}).get("createPost", {})
                error_msg = result.get("message") or ""

            if "errors" in res_data:
                print(f"❌ Error GraphQL al canal {channel_id}: {json.dumps(res_data['errors'], indent=2)}")
                all_success = False
            elif error_msg:
                print(f"⚠️ Resposta de Buffer al canal {channel_id}: {error_msg}")
                all_success = False
            elif "post" in result:
                print(f"🎉 Publicat amb èxit al canal {service.upper() or channel_id}! Post ID: {result['post']['id']}")

        except Exception as e:
            print(f"❌ Error inesperat connectant amb Buffer ({channel_id}): {e}")
            all_success = False

    return all_success


# ==========================================
# GESTIÓ DE TIPOGRAFIES (PLUS JAKARTA SANS)
# ==========================================

def ensure_fonts():
    """Assegura que les fonts Plus Jakarta Sans estiguin disponibles a assets/fonts/."""
    os.makedirs(FONTS_DIR, exist_ok=True)
    
    font_urls = {
        "PlusJakartaSans-Regular.ttf": "https://raw.githubusercontent.com/tokotype/PlusJakartaSans/master/fonts/ttf/PlusJakartaSans-Regular.ttf",
        "PlusJakartaSans-Bold.ttf": "https://raw.githubusercontent.com/tokotype/PlusJakartaSans/master/fonts/ttf/PlusJakartaSans-Bold.ttf",
        "PlusJakartaSans-SemiBold.ttf": "https://raw.githubusercontent.com/tokotype/PlusJakartaSans/master/fonts/ttf/PlusJakartaSans-SemiBold.ttf"
    }

    for font_file, url in font_urls.items():
        dest = os.path.join(FONTS_DIR, font_file)
        if not os.path.exists(dest):
            try:
                print(f"📥 Descarregant font {font_file}...")
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    with open(dest, "wb") as f:
                        f.write(r.content)
            except Exception as e:
                print(f"⚠️ Error descarregant {font_file}: {e}")


def get_jakarta_font(style="regular", size=42):
    ensure_fonts()
    font_map = {
        "bold": os.path.join(FONTS_DIR, "PlusJakartaSans-Bold.ttf"),
        "semibold": os.path.join(FONTS_DIR, "PlusJakartaSans-SemiBold.ttf"),
        "regular": os.path.join(FONTS_DIR, "PlusJakartaSans-Regular.ttf")
    }

    path = font_map.get(style, font_map["regular"])
    if os.path.exists(path):
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass

    for fallback in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(fallback):
            return ImageFont.truetype(fallback, size=size)

    return ImageFont.load_default()


# ==========================================
# UTILITATS D'IMATGE I VISIÓ AI
# ==========================================

def clean_tweet_text(text):
    """Elimina emojis i caràcters no renderitzables."""
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U00010000-\U0010ffff"
        "\u200d\u200c\u200e\u200f"
        "\u2300-\u23ff"
        "\u2600-\u27bf"
        "\u2190-\u21ff"
        "\u2200-\u22ff"
        "\u2b50\u2b06\u2b07"
        "]+",
        flags=re.UNICODE
    )
    cleaned = emoji_pattern.sub("", text)
    cleaned = cleaned.replace("≡", "").replace("■", "").replace("□", "")
    return cleaned.strip()


def extract_frame_as_image(video_path, timestamp=0.5):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
    success, frame = cap.read()
    cap.release()
    
    if success and frame is not None:
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(frame_rgb)
    return None


def image_to_base64_jpeg(image_pil):
    buffered = BytesIO()
    image_pil.save(buffered, format="JPEG", quality=85)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')


# PROMPT ADAPTAT A BALLITY (ESPORTS & FUTBOL - TEXT ULTRA CURT I DINÀMIC)
AI_PROMPT_INSTRUCTIONS = f"""
Examine this sports/football video frame and the original description carefully.

CRITICAL CONSISTENCY & TONE RULE:
Both 'tweet_text' and 'generated_caption' MUST be 100% focused on the EXACT sports/football event shown in the video.
- Keep the tone energetic, hype, and engaging for football/sports fans on TikTok and Instagram.
- NEVER invent unrelated scientific or general facts. Stay 100% on the sports topic.

RULES FOR CREDITS:
1. Identify the TRUE ORIGINAL source/creator of the video (e.g. "@player", "@club", "@creator").
2. NEVER credit reposter aggregator pages (ignore @433, @espn, @pubity, @wealth, etc.).
3. If no clear third-party source is mentioned, set "credits" to "".

RULES FOR TWEET TEXT ('tweet_text'):
- Write a SHORT, PUNCHY viral sports tweet in ENGLISH (maximum 1 or 2 SHORT lines, 15 to 25 words total).
- It must grab attention in under 2 seconds.
- STRICTLY NO EMOJIS OR UNICODE SYMBOLS in 'tweet_text'.
- EMPHASIZE 1-3 key punchline words using markdown asterisks **like this**.

RULES FOR THUMBNAIL TITLE ('thumbnail_title'):
- Ultra-short, high-impact headline of 2 TO 4 WORDS in UPPERCASE ENGLISH.
- Examples: "WHAT A GOAL", "PRIME NEYMAR", "INSTANT REGRET", "COLD MOMENT", "BALL OF THE YEAR".

RULES FOR THE CAPTION ('generated_caption'):
Structure in this exact order:
1. Short engaging context sentence about this play/moment.
2. Debate Call to Action (CTA) to drive comments (e.g. 'Is this the best goal this season? Let us know below! 👇' or 'Who did it better? Tell us! 👇').
3. 8-12 viral football/sports hashtags (#football #soccer #futbol #ballity #premierleague #championsleague #skills #goals...).
4. Credit line (ONLY if true original source identified):
   Credit: @original_author
5. AT THE VERY BOTTOM (last line):
   {DISCLAIMER_TEXT}

Return strictly a JSON object with this format:
{{
  "credits": "@original_creator_or_empty",
  "tweet_text": "This player just pulled off the **most insane skill** this season.",
  "thumbnail_title": "WHAT A GOAL",
  "generated_caption": "Unbelievable moment from yesterday's match.\\n\\nIs this the goal of the season? Let us know below! 👇\\n\\n#football #soccer #futbol #ballity #premierleague #goals\\n\\nCredit: @original_author\\n\\n{DISCLAIMER_TEXT}"
}}
"""


def format_final_caption(generated_caption):
    caption = (generated_caption or "").strip()
    if DISCLAIMER_TEXT not in caption:
        caption = f"{caption}\n\n{DISCLAIMER_TEXT}" if caption else DISCLAIMER_TEXT
    return caption


def parse_json_safely(raw_text):
    """Extreu i parseja JSON de manera robusta."""
    try:
        return json.loads(raw_text)
    except Exception:
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            return json.loads(match.group())
    return None


def analyze_with_gemini_vision(image_pil, caption_raw=""):
    """Anàlisi principal amb Google Gemini."""
    from google import genai
    from google.genai import types
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = AI_PROMPT_INSTRUCTIONS
    
    contents = [prompt]
    if image_pil is not None:
        contents.append(image_pil)
    if caption_raw:
        contents.append(f"\nOriginal post description: {caption_raw}")

    candidate_models = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
    for model_name in candidate_models:
        try:
            print(f"🧠 [Gemini] Provant model {model_name}...")
            res = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            data = parse_json_safely(res.text)
            if data and data.get("tweet_text"):
                return (
                    data.get("credits", ""),
                    clean_tweet_text(data.get("tweet_text", "")),
                    format_final_caption(data.get("generated_caption", "")),
                    data.get("thumbnail_title", "HIGHLIGHT MOMENT").upper()
                )
        except Exception as e:
            print(f"ℹ️ Gemini error ({model_name}): {e}")
            continue
    return None


def analyze_with_groq_vision(image_pil, caption_raw=""):
    """Fallback amb Groq Vision si Gemini falla o està saturat."""
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)

    prompt = AI_PROMPT_INSTRUCTIONS
    if caption_raw:
        prompt += f"\nOriginal post description: {caption_raw}"

    content = [{"type": "text", "text": prompt}]
    if image_pil is not None:
        b64 = image_to_base64_jpeg(image_pil)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        })

    candidate_models = [
        "qwen/qwen3.6-27b",
        "meta-llama/llama-4-scout-17b-16e-instruct"
    ]

    for model_name in candidate_models:
        try:
            print(f"🧠 [Groq Fallback] Provant model {model_name}...")
            completion = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": content}],
                temperature=0.6
            )
            raw_text = completion.choices[0].message.content
            data = parse_json_safely(raw_text)
            if data and data.get("tweet_text"):
                return (
                    data.get("credits", ""),
                    clean_tweet_text(data.get("tweet_text", "")),
                    format_final_caption(data.get("generated_caption", "")),
                    data.get("thumbnail_title", "HIGHLIGHT MOMENT").upper()
                )
        except Exception as e:
            print(f"ℹ️ Groq error ({model_name}): {e}")
            continue
    return None


def send_telegram_alert(error_detail, reel_url=""):
    """Envia una alerta immediata si les IAs no responen després dels 5 intents."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    alert_text = (
        f"🚨 <b>ALERTA CRÍTICA BALLITY PIPELINE</b> 🚨\n\n"
        f"❌ <b>Error:</b> Cap servei d'Intel·ligència Artificial (Gemini / Groq) ha respost després de <b>5 intents</b>.\n\n"
        f"🔗 <b>Reel afectat:</b> {html.escape(reel_url)}\n\n"
        f"⚠️ <i>El processament s'ha aturat per seguretat.</i>\n\n"
        f"📋 <b>Detalls de l'error:</b>\n<code>{html.escape(str(error_detail)[:350])}</code>"
    )
    data_msg = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": alert_text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url_msg, data=data_msg, timeout=10)
        print("🚨 Alerta d'error enviada a Telegram!")
    except Exception as e:
        print(f"⚠️ Error enviant l'alerta a Telegram: {e}")


def analyze_content_with_retry(image_pil, caption_raw="", reel_url="", max_retries=5, delay_seconds=60):
    for attempt in range(1, max_retries + 1):
        print(f"\n🤖 [Intent {attempt}/{max_retries}] Analitzant contingut esportiu amb IA...")

        if GEMINI_API_KEY:
            res = analyze_with_gemini_vision(image_pil, caption_raw)
            