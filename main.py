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
            if res and len(res) == 4:
                return res

        if GROQ_API_KEY:
            res = analyze_with_groq_vision(image_pil, caption_raw)
            if res and len(res) == 4:
                return res

        if attempt < max_retries:
            print(f"⏳ APIs saturades. Esperant {delay_seconds} segons...")
            time.sleep(delay_seconds)

    print(f"❌ La IA no ha respost després de {max_retries} intents.")
    send_telegram_alert("Totes les APIs de visió han fallat.", reel_url)
    return None, None, None, None


# ==========================================
# RENDERITZAT TWEET AMB PLUS JAKARTA SANS & NEGRETA
# ==========================================

def tokenize_markdown_text(text):
    paragraphs = text.split("\n")
    tokenized_paragraphs = []

    for para in paragraphs:
        if not para.strip():
            tokenized_paragraphs.append([])
            continue

        parts = re.split(r'(\*\*[^*]+\*\*)', para)
        tokens = []
        for part in parts:
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) >= 4:
                bold_words = part[2:-2].split()
                for w in bold_words:
                    tokens.append((w, True))
            else:
                regular_words = part.split()
                for w in regular_words:
                    tokens.append((w, False))
        tokenized_paragraphs.append(tokens)

    return tokenized_paragraphs


def wrap_tokenized_text(tokenized_paragraphs, regular_font, bold_font, max_width, draw):
    all_lines = []
    space_w_reg = draw.textbbox((0, 0), " ", font=regular_font)[2]

    for para_tokens in tokenized_paragraphs:
        if not para_tokens:
            all_lines.append([])
            continue

        current_line = []
        current_width = 0

        for word, is_bold in para_tokens:
            f = bold_font if is_bold else regular_font
            word_w = draw.textbbox((0, 0), word, font=f)[2]

            needed_width = word_w if not current_line else (current_width + space_w_reg + word_w)

            if needed_width <= max_width:
                current_line.append((word, is_bold, word_w))
                current_width = needed_width
            else:
                if current_line:
                    all_lines.append(current_line)
                    current_line = [(word, is_bold, word_w)]
                    current_width = word_w
                else:
                    all_lines.append([(word, is_bold, word_w)])
                    current_line = []
                    current_width = 0

        if current_line:
            all_lines.append(current_line)

    return all_lines


def create_tweet_header_image(tweet_text, width=1080):
    margin_x = 75
    max_text_width = width - (margin_x * 2)

    name_font = get_jakarta_font("semibold", size=48)
    handle_font = get_jakarta_font("regular", size=38)
    body_font_regular = get_jakarta_font("regular", size=44)
    body_font_bold = get_jakarta_font("bold", size=44)

    dummy_img = Image.new("RGBA", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)

    tokenized = tokenize_markdown_text(tweet_text)
    wrapped_lines = wrap_tokenized_text(tokenized, body_font_regular, body_font_bold, max_text_width, dummy_draw)

    line_height = 62
    paragraph_gap = 26
    
    body_height = 0
    for line in wrapped_lines:
        if not line:
            body_height += paragraph_gap
        else:
            body_height += line_height

    avatar_size = 110
    top_padding = 50
    bottom_padding = 40
    header_height = top_padding + avatar_size + 36 + body_height + bottom_padding

    img = Image.new("RGBA", (width, header_height), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)

    # 1. Avatar Circular de Ballity
    avatar_x = margin_x
    avatar_y = top_padding

    logo_file = LOGO_PATH if os.path.exists(LOGO_PATH) else ("logo.png" if os.path.exists("logo.png") else None)
    if logo_file:
        try:
            logo_img = Image.open(logo_file).convert("RGBA").resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            img.paste(logo_img, (avatar_x, avatar_y), mask)
        except Exception as e:
            print(f"⚠️ Error carregant el logo: {e}")
    else:
        draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size], fill=(22, 24, 28))
        draw.text((avatar_x + 32, avatar_y + 20), "B", font=name_font, fill=(50, 205, 50))

    # 2. Nom i Username de Ballity
    text_start_x = avatar_x + avatar_size + 24
    draw.text((text_start_x, avatar_y + 6), "Ballity", font=name_font, fill=(255, 255, 255))
    draw.text((text_start_x, avatar_y + 58), "@ballity.tv", font=handle_font, fill=(113, 118, 123))

    # 3. Cos del Tweet
    text_y = avatar_y + avatar_size + 36
    space_w = dummy_draw.textbbox((0, 0), " ", font=body_font_regular)[2]

    for line in wrapped_lines:
        if not line:
            text_y += paragraph_gap
            continue

        cursor_x = margin_x
        for word, is_bold, word_w in line:
            f = body_font_bold if is_bold else body_font_regular
            draw.text((cursor_x, text_y), word, font=f, fill=(255, 255, 255))
            cursor_x += word_w + space_w

        text_y += line_height

    return np.array(img)


# ==========================================
# DETECCIÓ I CROP INTEL·LIGENT DE CONTINGUT
# ==========================================

def detect_background_color(frame):
    h, w, _ = frame.shape
    top_sample = frame[0:15, w//4:3*w//4]
    bottom_sample = frame[h-15:h, w//4:3*w//4]
    samples = np.concatenate([top_sample, bottom_sample], axis=0)
    return np.median(samples, axis=(0, 1))


def get_longest_consecutive_run(bool_array):
    max_run = 0
    current_run = 0
    for val in bool_array:
        if val:
            current_run += 1
            if current_run > max_run:
                max_run = current_run
        else:
            current_run = 0
    return max_run


def find_video_box_in_frame(frame, color_diff_threshold=30, min_width_ratio=0.65, min_height_ratio=0.65):
    h, w, _ = frame.shape
    bg_color = detect_background_color(frame)

    diff = np.max(np.abs(frame.astype(np.float32) - bg_color), axis=2)
    foreground_mask = diff > color_diff_threshold

    min_continuous_px = int(w * min_width_ratio)
    valid_y = []

    for y in range(h):
        row = foreground_mask[y, :]
        if get_longest_consecutive_run(row) >= min_continuous_px:
            valid_y.append(y)

    if not valid_y:
        return None

    y1 = valid_y[0]
    y2 = valid_y[-1]

    if (y2 - y1) < 100:
        return None

    video_region_mask = foreground_mask[y1:y2, :]
    region_h = y2 - y1
    min_col_pixels = int(region_h * min_height_ratio)

    valid_x = []
    for x in range(w):
        col = video_region_mask[:, x]
        if np.sum(col) >= min_col_pixels:
            valid_x.append(x)

    if not valid_x:
        x1, x2 = 0, w
    else:
        x1 = valid_x[0]
        x2 = valid_x[-1]

    return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))


def crop_content_bounding_box(clip, num_samples=6):
    duration = clip.duration
    if not duration or duration <= 0:
        return None

    timestamps = np.linspace(0.5, max(duration - 0.5, 0.5), num=num_samples)
    boxes = []

    for t in timestamps:
        try:
            frame = clip.get_frame(t)
            box = find_video_box_in_frame(frame)
            if box:
                boxes.append(box)
        except Exception:
            continue

    if not boxes:
        return None

    boxes = np.array(boxes)
    median_box = np.median(boxes, axis=0).astype(int)
    x, y, w, h = median_box
    return (int(x), int(y), int(w), int(h))


# ==========================================
# MINIATURA EDITORIAL DE BALLITY
# ==========================================

def create_editorial_thumbnail(video_path, thumbnail_title, output_path=os.path.join(VIDEOS_DIR, "final_thumbnail.jpg")):
    """Genera la portada editorial amb el requadre del vídeo desenfocat i el logo centrat."""
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    clip = VideoFileClip(video_path)
    frame_w, frame_h = clip.w, clip.h
    bbox = crop_content_bounding_box(clip)

    t_sample = min(1.0, max(clip.duration - 0.1, 0.5)) if clip.duration else 0.5
    frame_np = clip.get_frame(t_sample)
    clip.close()

    frame_pil = Image.fromarray(frame_np)

    # 1. Aplicar el crop idèntic al del vídeo per eliminar capçaleres antigues
    min_area_ratio = 0.10
    if bbox and (bbox[2] * bbox[3]) >= min_area_ratio * frame_w * frame_h:
        x, y, w, h = bbox
        margin_x = int(w * 0.01)
        margin_y = int(h * 0.01)
        x1 = max(0, x - margin_x)
        y1 = max(0, y - margin_y)
        x2 = min(frame_w, x + w + margin_x)
        y2 = min(frame_h, y + h + margin_y)
        cropped_frame = frame_pil.crop((x1, y1, x2, y2))
    else:
        cropped_frame = frame_pil

    # 2. Llenç base: Fons negre sòlid 1080x1920
    canvas = Image.new("RGBA", (1080, 1920), (0, 0, 0, 255))

    # 3. Escalar el requadre del vídeo a 1080 d'amplada i aplicar el BLUR intens
    w_c, h_c = cropped_frame.size
    scale_fg = 1080 / w_c
    fg_w = 1080
    fg_h = int(h_c * scale_fg)

    fg_resized = cropped_frame.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
    fg_blurred = fg_resized.filter(ImageFilter.GaussianBlur(radius=32)).convert("RGBA")

    dark_tint = Image.new("RGBA", (fg_w, fg_h), (0, 0, 0, 130))
    fg_box = Image.alpha_composite(fg_blurred, dark_tint)

    fg_y = max(0, (1920 - fg_h) // 2)
    canvas.paste(fg_box, (0, fg_y), fg_box)

    draw = ImageDraw.Draw(canvas)

    # 4. Tipografia del titular
    title_font = get_jakarta_font("bold", size=76)
    words = thumbnail_title.split()
    lines = []
    current_line = []

    for word in words:
        test_line = " ".join(current_line + [word])
        w = draw.textbbox((0, 0), test_line, font=title_font)[2]
        if w <= 920:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
            else:
                lines.append(word)
                current_line = []
    if current_line:
        lines.append(" ".join(current_line))

    # 5. Centrat vertical exacte a y=960 (Safe Zone 1:1)
    line_h = 96
    total_title_h = len(lines) * line_h
    logo_size = 190
    gap = 46
    total_block_h = total_title_h + gap + logo_size
    start_y = 960 - (total_block_h // 2)

    # Dibuixar titular en blanc amb ombra
    text_y = start_y
    for line in lines:
        w = draw.textbbox((0, 0), line, font=title_font)[2]
        x = (1080 - w) // 2
        draw.text((x + 4, text_y + 4), line, font=title_font, fill=(0, 0, 0, 240))
        draw.text((x, text_y), line, font=title_font, fill=(255, 255, 255))
        text_y += line_h

    # 6. Dibuixar el Logotip gegant (190 px) a sota
    logo_x = (1080 - logo_size) // 2
    logo_y = start_y + total_title_h + gap

    logo_file = LOGO_PATH if os.path.exists(LOGO_PATH) else ("logo.png" if os.path.exists("logo.png") else None)
    if logo_file:
        try:
            logo_img = Image.open(logo_file).convert("RGBA").resize((logo_size, logo_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (logo_size, logo_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, logo_size, logo_size), fill=255)
            canvas.paste(logo_img, (logo_x, logo_y), mask)
        except Exception as e:
            print(f"⚠️ Error carregant el logo per a la miniatura: {e}")
    else:
        draw.ellipse([logo_x, logo_y, logo_x + logo_size, logo_y + logo_size], fill=(50, 205, 50))
        b_font = get_jakarta_font("bold", size=int(logo_size * 0.65))
        b_bbox = draw.textbbox((0, 0), "b", font=b_font)
        b_w = b_bbox[2] - b_bbox[0]
        b_h = b_bbox[3] - b_bbox[1]
        b_x = logo_x + (logo_size - b_w) // 2 - b_bbox[0]
        b_y = logo_y + (logo_size - b_h) // 2 - b_bbox[1]
        draw.text((b_x, b_y), "b", font=b_font, fill=(255, 255, 255))

    bg_rgb = canvas.convert("RGB")
    bg_rgb.save(output_path, "JPEG", quality=95)
    print(f"🖼️ Miniatura editorial generada amb èxit a: {output_path}")
    return np.array(bg_rgb), output_path


# ==========================================
# RENDERITZAT FINAL (AMB FRAME 0 PORTADA)
# ==========================================

def process_video_canvas(input_path, tweet_text, thumbnail_img_np, output_path):
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    clip = VideoFileClip(input_path)
    frame_w, frame_h = clip.w, clip.h
    bbox = crop_content_bounding_box(clip)

    min_area_ratio = 0.10
    if bbox and (bbox[2] * bbox[3]) >= min_area_ratio * frame_w * frame_h:
        x, y, w, h = bbox
        margin_x = int(w * 0.01)
        margin_y = int(h * 0.01)
        x1 = max(0, x - margin_x)
        y1 = max(0, y - margin_y)
        x2 = min(frame_w, x + w + margin_x)
        y2 = min(frame_h, y + h + margin_y)
        print(f"✂️ Crop aplicat: x={x1}, y={y1}, x2={x2}, y2={y2}")
        cropped_clip = clip.cropped(x1=x1, y1=y1, x2=x2, y2=y2)
    else:
        print("ℹ️ No s'ha detectat cap marc distintiu, s'utilitza el vídeo original.")
        cropped_clip = clip

    scaled_clip = cropped_clip.resized(width=1080)

    print("🎨 Renderitzant capçalera estil Tweet per a Ballity...")
    header_img_np = create_tweet_header_image(tweet_text, width=1080)
    header_h = header_img_np.shape[0]

    header_clip = ImageClip(header_img_np).with_duration(scaled_clip.duration)

    header_clip = header_clip.with_position(("center", 180))
    video_y_pos = 180 + header_h + 10
    video_positioned = scaled_clip.with_position(("center", video_y_pos))

    main_video_composite = CompositeVideoClip([video_positioned, header_clip], size=(1080, 1920))

    # Incrustar la miniatura com a primer fotograma (1 frame = 33.3 ms)
    cover_clip = ImageClip(thumbnail_img_np).with_duration(1.0 / 30.0)
    final_video = concatenate_videoclips([cover_clip, main_video_composite])

    final_video.write_videofile(
        output_path, 
        codec="libx264", 
        audio_codec="aac",
        fps=30,
        preset="fast"
    )

    clip.close()
    cropped_clip.close()
    main_video_composite.close()
    cover_clip.close()
    final_video.close()
    header_clip.close()


# ==========================================
# NOTIFICACIÓ TELEGRAM
# ==========================================

def send_telegram_notification(video_path, thumbnail_path, tweet_text, credits, generated_caption, video_id):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Notificació de Telegram omessa (tokens no configurats).")
        return

    safe_tweet = html.escape(tweet_text or "")
    safe_credits = html.escape(credits or "No especificada")
    safe_video_id = html.escape(video_id or "")

    if TEST_MODE:
        print("🧪 [Mode Proves] Enviant vídeo, miniatura i caption per a revisió...")
        
        video_caption = (
            f"⚽ <b>[BALLITY TEST MODE] NOU CLIP PROCESSAT</b>\n\n"
            f"📌 <b>Tweet Text</b>:\n<i>{safe_tweet}</i>\n\n"
            f"👤 <b>Font Original</b>: {safe_credits}\n"
            f"🆔 <b>ID</b>: <code>{safe_video_id}</code>"
        )
        url_video = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
        with open(video_path, "rb") as video_file:
            requests.post(url_video, data={"chat_id": TELEGRAM_CHAT_ID, "caption": video_caption, "parse_mode": "HTML"}, files={"video": video_file})

        if thumbnail_path and os.path.exists(thumbnail_path):
            url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(thumbnail_path, "rb") as photo_file:
                requests.post(url_photo, data={"chat_id": TELEGRAM_CHAT_ID, "caption": "🖼️ <b>[TEST MODE] Portada Ballity generada</b>", "parse_mode": "HTML"}, files={"photo": photo_file})

        url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        caption_text = f"📝 <b>[TEST MODE] CAPTION BALLITY</b>:\n\n<code>{html.escape(generated_caption)}</code>"
        requests.post(url_msg, data={"chat_id": TELEGRAM_CHAT_ID, "text": caption_text, "parse_mode": "HTML"})

    else:
        print("🚀 [Producció] Enviant només resum de confirmació...")
        url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        confirm_text = (
            f"✅ <b>CLIP DE BALLITY PUBLICAT (BUFFER)</b>\n\n"
            f"📌 <b>Tweet:</b> {safe_tweet}\n"
            f"👤 <b>Font:</b> {safe_credits}\n"
            f"🆔 <b>ID:</b> <code>{safe_video_id}</code>\n"
            f"🌐 <i>Estat a sources.csv: <b>done</b></i>"
        )
        requests.post(url_msg, data={"chat_id": TELEGRAM_CHAT_ID, "text": confirm_text, "parse_mode": "HTML"})


# ==========================================
# FLUX PRINCIPAL
# ==========================================

def extract_shortcode(reel_url):
    match = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", reel_url)
    return match.group(1) if match else None


def _cleanup_temp_input():
    for f in glob.glob("temp_input.*"):
        try:
            os.remove(f)
        except OSError:
            pass


def get_reel_by_url(reel_url):
    shortcode = extract_shortcode(reel_url)
    print(f"⬇️ Descarregant reel amb yt-dlp: {reel_url}")
    _cleanup_temp_input()

    ydl_opts = {
        "outtmpl": "temp_input.%(ext)s",
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
        ydl_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(reel_url, download=True)
    except yt_dlp.utils.DownloadError as e:
        print(f"❌ Error en descarregar el reel amb yt-dlp: {e}")
        return None, None, None, None, None, None
    except Exception as e:
        print(f"❌ Error inesperat amb yt-dlp: {e}")
        return None, None, None, None, None, None

    video_id = str(info.get("id") or shortcode or reel_url)
    downloaded_path = ydl.prepare_filename(info)
    if not os.path.exists(downloaded_path):
        candidates = glob.glob("temp_input.*")
        downloaded_path = candidates[0] if candidates else None

    if not downloaded_path or not os.path.exists(downloaded_path):
        print("⚠️ yt-dlp no ha generat cap fitxer de vídeo.")
        return None, None, None, None, None, None

    caption_raw = info.get("description") or ""

    frame_image = extract_frame_as_image(downloaded_path, timestamp=0.5)
    ai_result = analyze_content_with_retry(
        frame_image, 
        caption_raw=caption_raw, 
        reel_url=reel_url,
        max_retries=5, 
        delay_seconds=60
    )

    if not ai_result or len(ai_result) != 4:
        return None, None, None, None, None, None

    credits, tweet_text, generated_caption, thumbnail_title = ai_result

    if not tweet_text:
        return None, None, None, None, None, None

    return downloaded_path, video_id, credits, tweet_text, generated_caption, thumbnail_title


def main():
    if not os.path.exists("sources.csv"):
        print("❌ No s'ha trobat el fitxer sources.csv")
        return

    os.makedirs(VIDEOS_DIR, exist_ok=True)

    pending_urls = []
    with open("sources.csv", mode="r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if not row or len(row) < 1:
                continue
            url = row[0].strip()
            status = row[1].strip().lower() if len(row) > 1 else "pending"
            
            if (status == "pending" or TEST_MODE) and url.startswith("http"):
                pending_urls.append(url)

    if not pending_urls:
        print("ℹ️ No hi ha cap reel amb estat 'pending' a sources.csv.")
        return

    for reel_url in pending_urls:
        print(f"\n🚀 Processant reel pendent: {reel_url}")
        reel_data = get_reel_by_url(reel_url)

        if not reel_data or len(reel_data) != 6 or not reel_data[0]:
            print(f"⚠️ Reel omès o fallat: {reel_url}")
            update_csv_status(reel_url, "failed")
            print("⏭️ Saltant al següent reel pendent...")
            continue

        video_file, video_id, credits, tweet_text, generated_caption, thumbnail_title = reel_data

        if video_file and tweet_text:
            unique_video_rel = f"video_{video_id}.mp4"
            unique_video_path = os.path.join(VIDEOS_DIR, unique_video_rel)
            thumbnail_rel = "final_thumbnail.jpg"
            thumbnail_path = os.path.join(VIDEOS_DIR, thumbnail_rel)

            # 1. Generar la miniatura editorial dins de videos/
            thumbnail_np, thumbnail_file = create_editorial_thumbnail(video_file, thumbnail_title, thumbnail_path)

            # 2. Composició de vídeo final dins de videos/
            process_video_canvas(video_file, tweet_text, thumbnail_np, unique_video_path)

            # 3. Netejar fitxers antics de videos/ i fer PUSH a GitHub
            push_media_to_github(unique_video_rel, thumbnail_rel)

            # 4. Publicació a xarxes socials via Buffer
            if not TEST_MODE:
                publish_to_buffer(generated_caption, video_filename=unique_video_rel, thumbnail_offset_ms=0)
            else:
                print("🧪 [Mode Proves Actiu]: S'omet la crida a l'API de Buffer.")

            # 5. Notificació a Telegram segons el mode
            send_telegram_notification(
                unique_video_path, 
                thumbnail_file, 
                tweet_text, 
                credits, 
                generated_caption, 
                video_id
            )

            save_processed_id(video_id)
            update_csv_status(reel_url, "done")
            print("✅ Procés finalitzat amb èxit!")
            break
        else:
            print(f"⚠️ Reel omès o fallat: {reel_url}")
            update_csv_status(reel_url, "failed")
            print("⏭️ Saltant al següent reel pendent...")


if __name__ == "__main__":
    main()