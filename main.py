import logging
import os
from dotenv import load_dotenv
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import re
from datetime import datetime, timedelta
import tempfile
from PIL import Image
import imageio.v2 as imageio
try:
    from nsfw_detector import predict as nsfw_predict
except Exception:
    nsfw_predict = None

# Load environment variables
from pathlib import Path
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Simple in-memory cache for username -> user_id mapping
USERNAME_CACHE = {}
NSFW_POLICY = "mute"
OFFENSES = {}
NSFW_MODEL = None
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.5"))
NSFW_KEYWORDS = [
    "porn", "xxx", "nsfw", "18+", "sex", "sexual", "nude", "nudity",
    "hardcore", "softcore", "blowjob", "handjob", "anal", "cum", "orgasm",
    "boobs", "tits", "penis", "vagina", "fetish", "incest"
]
NSFW_PATTERN = re.compile(r"(" + "|".join([re.escape(k) for k in NSFW_KEYWORDS]) + r")", re.IGNORECASE)
URL_PATTERN = re.compile(r"(https?://\S+|www\.\S+|\b[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\S*)", re.IGNORECASE)

def extract_text(update: Update) -> str:
    parts = []
    if update.message:
        if update.message.text:
            parts.append(update.message.text)
        if update.message.caption:
            parts.append(update.message.caption)
    return " ".join(parts).strip()

def is_nsfw_text(text: str) -> bool:
    if not text:
        return False
    return bool(NSFW_PATTERN.search(text))

def ensure_nsfw_model():
    global NSFW_MODEL
    if NSFW_MODEL is not None:
        return True
    if nsfw_predict is None:
        return False
    model_path = os.getenv("NSFW_MODEL_PATH")
    if not model_path:
        return False
    try:
        NSFW_MODEL = nsfw_predict.load_model(model_path)
        return True
    except Exception:
        return False

def classify_image(path: str) -> float:
    if NSFW_MODEL is None:
        return 0.0
    try:
        result = nsfw_predict.classify(NSFW_MODEL, path)
        probs = result.get(path, {})
        return max(probs.get("porn", 0.0), probs.get("hentai", 0.0), probs.get("sexy", 0.0))
    except Exception:
        return 0.0

def convert_webp_to_jpg(path: str) -> str:
    try:
        img = Image.open(path).convert("RGB")
        out = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        img.save(out.name, format="JPEG")
        return out.name
    except Exception:
        return ""

def extract_video_frames(path: str, num_frames: int = 5) -> list[str]:
    frames = []
    try:
        reader = imageio.get_reader(path)
        count = reader.get_meta_data().get("nframes", 0)
        if count <= 0:
            count = 30
        step = max(1, count // num_frames)
        idxs = list(range(0, count, step))[:num_frames]
        for i in idxs:
            try:
                frame = reader.get_data(i)
                img = Image.fromarray(frame).convert("RGB")
                out = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                img.save(out.name, format="JPEG")
                frames.append(out.name)
            except Exception:
                continue
        reader.close()
    except Exception:
        pass
    return frames

def extract_gif_frames(path: str, num_frames: int = 5) -> list[str]:
    frames = []
    try:
        reader = imageio.get_reader(path)
        count = reader.get_length()
        step = max(1, count // num_frames)
        idxs = list(range(0, count, step))[:num_frames]
        for i in idxs:
            try:
                frame = reader.get_data(i)
                img = Image.fromarray(frame).convert("RGB")
                out = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                img.save(out.name, format="JPEG")
                frames.append(out.name)
            except Exception:
                continue
        reader.close()
    except Exception:
        pass
    return frames
async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Middleware to track users and update the username cache."""
    if update.effective_user and update.effective_user.username:
        username = update.effective_user.username.lower()
        USERNAME_CACHE[username] = update.effective_user.id

async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Resolves the target user from a reply or a username mention.
    Returns: (User object or None, error_message or None)
    """
    # 1. Check if it's a reply
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user, None
    
    # 2. Check if there are arguments (e.g., /ban @username)
    if context.args:
        username_arg = context.args[0]
        if username_arg.startswith("@"):
            username_arg = username_arg[1:]
        
        username_lower = username_arg.lower()
        
        # Check cache
        user_id = USERNAME_CACHE.get(username_lower)
        if user_id:
            try:
                # Fetch full user info from chat member
                chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
                return chat_member.user, None
            except Exception as e:
                return None, f"Could not find user with ID {user_id} in this chat."
        else:
            return None, f"I haven't seen the user @{username_arg} yet. I can only manage users I've seen speak."
            
    return None, "Please reply to a user's message or tag them (e.g., /command @username)."

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the command /start is issued."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Hi! I'm a Panda that manages groups and chats with you.\n"
             "Add me to a group and make me admin for management features.\n"
             "Use /help to see available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a list of available commands."""
    help_text = (
        "Available commands:\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n"
        "/ban - Ban a user (Reply to their message, Admin only)\n"
        "/unban - Unban a user (Reply or @username, Admin only)\n"
        "/mute - Mute a user (Reply to their message, Admin only)\n"
        "/unmute - Unmute a user (Reply to their message, Admin only)\n"
        "/free - Free a user from restrictions (Reply or @username, Admin only)\n"
        "/nsfwaction <warn|mute> - Set NSFW moderation policy (Admin only)\n"
        "/info - Get information about a user (Reply or current user)\n"
        "/promote - Promote a user to Admin (Reply, Admin only)\n"
        "/demote - Demote an Admin to member (Reply, Admin only)\n"
        "/pin - Pin a message (Reply, Admin only)"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_text)

async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays information about a user."""
    # If replying or args provided, use resolve_target_user
    if update.message.reply_to_message or context.args:
        target_user, error = await resolve_target_user(update, context)
        if error:
            await update.message.reply_text(error)
            return
    else:
        # Default to self
        target_user = update.effective_user
    
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target_user.id)
        status = member.status
    except Exception:
        status = "Unknown"

    info_text = (
        f"<b>User Information:</b>\n"
        f"ID: <code>{target_user.id}</code>\n"
        f"Name: {target_user.full_name}\n"
        f"Username: @{target_user.username if target_user.username else 'None'}\n"
        f"Status: {status}\n"
        f"Is Bot: {'Yes' if target_user.is_bot else 'No'}"
    )
    await update.message.reply_text(info_text, parse_mode='HTML')

async def promote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Promotes a user to admin with standard permissions."""
    user_to_promote, error = await resolve_target_user(update, context)
    if error:
        await update.message.reply_text(error)
        return

    chat_id = update.effective_chat.id
    
    # Check if the user issuing the command is an admin (or creator)
    # We rely on Telegram API to throw an error if the bot doesn't have rights,
    # but we should also check if the commander is allowed.
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return

    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_to_promote.id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_manage_video_chats=True,
            can_restrict_members=True,
            can_promote_members=False,
            can_change_info=True,
            can_invite_users=True,
            can_pin_messages=True
        )
        await update.message.reply_text(f"Successfully promoted {user_to_promote.full_name} to Admin!")
    except Exception as e:
        await update.message.reply_text(f"Failed to promote user: {e}")

async def demote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demotes an admin to a regular member."""
    user_to_demote, error = await resolve_target_user(update, context)
    if error:
        await update.message.reply_text(error)
        return

    chat_id = update.effective_chat.id
    
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return

    try:
        # Promoting with all False permissions effectively demotes them
        await context.bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_to_demote.id,
            is_anonymous=False,
            can_manage_chat=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False
        )
        await update.message.reply_text(f"Successfully demoted {user_to_demote.full_name}.")
    except Exception as e:
        await update.message.reply_text(f"Failed to demote user: {e}")

async def echo_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Echoes the user message (Chatbot functionality)."""
    # Simple chatbot logic: echo back with a prefix
    # You can replace this with more complex logic or an LLM API call
    if update.message.text:
        user_text = update.message.text
        if is_nsfw_text(user_text):
            return
        response_text = f"You said: {user_text}\n(I am a simple bot for now!)"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=response_text)

async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if update.message.text and update.message.text.startswith("/"):
        return
    user = update.effective_user
    if not user or user.is_bot:
        return
    chat_id = update.effective_chat.id
    text = extract_text(update)
    if text and URL_PATTERN.search(text):
        try:
            await context.bot.send_message(
                chat_id,
                f"{user.mention_html()} links are not allowed in this group.",
                parse_mode='HTML'
            )
        except Exception:
            pass
    if not is_nsfw_text(text):
        pass
    else:
        try:
            await context.bot.delete_message(chat_id, update.message.message_id)
        except Exception:
            pass
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in admins]
        if user.id in admin_ids:
            await context.bot.send_message(chat_id, f"{user.mention_html()} NSFW content is not allowed.", parse_mode='HTML')
            return
        until_date = datetime.utcnow() + timedelta(hours=1)
        permissions = ChatPermissions(can_send_messages=False)
        try:
            await context.bot.restrict_chat_member(chat_id, user.id, permissions=permissions, until_date=until_date)
            await context.bot.send_message(chat_id, f"{user.mention_html()} muted for NSFW content.", parse_mode='HTML')
        except Exception as e:
            await context.bot.send_message(chat_id, f"Failed to mute {user.full_name}: {e}")
    has_image = False
    image_file_id = None
    document_mime = update.message.document.mime_type if update.message.document else None
    if update.message.photo:
        has_image = True
        image_file_id = update.message.photo[-1].file_id
    elif update.message.document and document_mime and (
        document_mime.startswith("image/") or document_mime.startswith("video/") or document_mime in ("image/gif", "image/webp")
    ):
        has_image = True
        image_file_id = update.message.document.file_id
    elif update.message.sticker:
        st = update.message.sticker
        if st.is_animated:
            has_image = False
        elif st.is_video:
            has_image = True
            image_file_id = st.file_id
        else:
            has_image = True
            image_file_id = st.file_id
    elif update.message.animation:
        has_image = True
        image_file_id = update.message.animation.file_id
    elif update.message.video:
        has_image = True
        image_file_id = update.message.video.file_id
    if has_image and ensure_nsfw_model():
        try:
            file = await context.bot.get_file(image_file_id)
            tmp_path = tempfile.NamedTemporaryFile(delete=False).name
            await file.download_to_drive(tmp_path)
            score = 0.0
            mime = update.message.document.mime_type if update.message.document else None
            if update.message.video or update.message.animation or (update.message.sticker and update.message.sticker.is_video):
                frames = extract_video_frames(tmp_path, num_frames=5)
                for f in frames:
                    score = max(score, classify_image(f))
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            elif mime and mime == "image/gif":
                frames = extract_gif_frames(tmp_path, num_frames=5)
                for f in frames:
                    score = max(score, classify_image(f))
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            elif mime and mime == "image/webp":
                jpg = convert_webp_to_jpg(tmp_path)
                if jpg:
                    score = classify_image(jpg)
                    try:
                        os.remove(jpg)
                    except Exception:
                        pass
            elif update.message.sticker and not update.message.sticker.is_animated:
                jpg = convert_webp_to_jpg(tmp_path)
                if jpg:
                    score = classify_image(jpg)
                    try:
                        os.remove(jpg)
                    except Exception:
                        pass
            else:
                score = classify_image(tmp_path)
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            if score >= NSFW_THRESHOLD:
                try:
                    await context.bot.delete_message(chat_id, update.message.message_id)
                except Exception:
                    pass
                admins = await context.bot.get_chat_administrators(chat_id)
                admin_ids = [admin.user.id for admin in admins]
                if user.id in admin_ids:
                    await context.bot.send_message(chat_id, f"{user.mention_html()} NSFW content is not allowed.", parse_mode='HTML')
                    return
                until_date = datetime.utcnow() + timedelta(hours=1)
                permissions = ChatPermissions(can_send_messages=False)
                try:
                    await context.bot.restrict_chat_member(chat_id, user.id, permissions=permissions, until_date=until_date)
                    await context.bot.send_message(chat_id, f"{user.mention_html()} muted for NSFW content.", parse_mode='HTML')
                except Exception as e:
                    await context.bot.send_message(chat_id, f"Failed to mute {user.full_name}: {e}")
        except Exception:
            pass

async def nsfw_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    if user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /nsfwaction <warn|mute>")
        return
    mode = context.args[0].lower()
    global NSFW_POLICY
    if mode == "warn":
        NSFW_POLICY = "warn_then_mute"
        await update.message.reply_text("NSFW action set to: warn then mute on repeat.")
    elif mode == "mute":
        NSFW_POLICY = "mute"
        await update.message.reply_text("NSFW action set to: mute on first offense.")
    else:
        await update.message.reply_text("Invalid mode. Use 'warn' or 'mute'.")

async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcomes new members to the group."""
    for member in update.message.new_chat_members:
        # Don't welcome the bot itself
        if member.id == context.bot.id:
            continue
        
        welcome_text = f"Welcome to the group, {member.mention_html()}!"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=welcome_text,
            parse_mode='HTML'
        )

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bans a user from the group."""
    user_to_ban, error = await resolve_target_user(update, context)
    if error:
        await update.message.reply_text(error)
        return

    chat_id = update.effective_chat.id
    user_id = user_to_ban.id

    # Check if the user issuing the command is an admin
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return

    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await update.message.reply_text(f"Banned {user_to_ban.full_name}.")
    except Exception as e:
        await update.message.reply_text(f"Failed to ban user: {e}")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unbans a user from the group."""
    user_to_unban, error = await resolve_target_user(update, context)
    if error:
        await update.message.reply_text(error)
        return

    chat_id = update.effective_chat.id
    user_id = user_to_unban.id

    # Check admin
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return

    try:
        await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        await update.message.reply_text(f"Unbanned {user_to_unban.full_name}. They can now rejoin.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unban user: {e}")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mutes a user in the group."""
    user_to_mute, error = await resolve_target_user(update, context)
    if error:
        await update.message.reply_text(error)
        return

    chat_id = update.effective_chat.id
    user_id = user_to_mute.id

    # Check admin
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return

    permissions = ChatPermissions(can_send_messages=False)
    
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=permissions)
        await update.message.reply_text(f"Muted {user_to_mute.full_name}.")
    except Exception as e:
        await update.message.reply_text(f"Failed to mute user: {e}")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unmutes a user in the group."""
    user_to_unmute, error = await resolve_target_user(update, context)
    if error:
        await update.message.reply_text(error)
        return

    chat_id = update.effective_chat.id
    user_id = user_to_unmute.id

    # Check admin
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return

    # Granting default permissions (sending messages allowed)
    # Using granular permissions as can_send_media_messages is deprecated
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True
    )
    
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=permissions)
        await update.message.reply_text(f"Unmuted {user_to_unmute.full_name}.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unmute user: {e}")

async def free_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Frees a user from restrictions (enables stickers, media, etc.)."""
    user_to_free, error = await resolve_target_user(update, context)
    if error:
        await update.message.reply_text(error)
        return

    chat_id = update.effective_chat.id
    user_id = user_to_free.id

    # Check admin
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return

    # Granting all permissions
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True
    )
    
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=permissions)
        await update.message.reply_text(f"Freed {user_to_free.full_name} from all restrictions!")
    except Exception as e:
        await update.message.reply_text(f"Failed to free user: {e}")

async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Reply to the message you want to pin.")
        return
    chat_id = update.effective_chat.id
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("You need to be an admin to use this command.")
        return
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=update.message.reply_to_message.message_id)
        await update.message.reply_text("Pinned the message.")
    except Exception as e:
        await update.message.reply_text(f"Failed to pin message: {e}")

if __name__ == '__main__':
    # Get token from environment variable
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not found in environment variables.")
        print("Please create a .env file with your token.")
        exit(1)

    application = ApplicationBuilder().token(token).connect_timeout(30).read_timeout(30).build()
    
    # Handlers
    start_handler = CommandHandler('start', start)
    help_handler = CommandHandler('help', help_command)
    ban_handler = CommandHandler('ban', ban_user)
    unban_handler = CommandHandler('unban', unban_user)
    mute_handler = CommandHandler('mute', mute_user)
    unmute_handler = CommandHandler('unmute', unmute_user)
    free_handler = CommandHandler('free', free_user)
    nsfwaction_handler = CommandHandler('nsfwaction', nsfw_action)
    info_handler = CommandHandler('info', user_info)
    promote_handler = CommandHandler('promote', promote_user)
    demote_handler = CommandHandler('demote', demote_user)
    pin_handler = CommandHandler('pin', pin_message)
    
    # Message handlers
    # Track users middleware (group=-1 ensures it runs before others)
    track_user_handler = MessageHandler(filters.ALL, track_user)
    application.add_handler(track_user_handler, group=-1)

    # Welcome new members
    welcome_handler = MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members)
    # Chatbot echo (filters.TEXT & (~filters.COMMAND) ensures we don't catch commands as text)
    chat_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), echo_chat)
    moderation_handler = MessageHandler(filters.ALL, moderate_message)

    # Add handlers
    application.add_handler(start_handler)
    application.add_handler(help_handler)
    application.add_handler(ban_handler)
    application.add_handler(unban_handler)
    application.add_handler(mute_handler)
    application.add_handler(unmute_handler)
    application.add_handler(free_handler)
    application.add_handler(nsfwaction_handler)
    application.add_handler(info_handler)
    application.add_handler(promote_handler)
    application.add_handler(demote_handler)
    application.add_handler(pin_handler)
    application.add_handler(welcome_handler)
    application.add_handler(moderation_handler)
    application.add_handler(chat_handler)

    print("Bot is running...")
    base_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL")
    port = int(os.getenv("PORT", "8080"))
    path = os.getenv("WEBHOOK_PATH", f"/webhook/{token}")
    if base_url:
        webhook_url = f"{base_url}{path}"
        print(f"Starting webhook server on 0.0.0.0:{port} -> {webhook_url}")
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=path.lstrip("/"),
            webhook_url=webhook_url,
        )
    else:
        print("Starting polling...")
        application.run_polling()
