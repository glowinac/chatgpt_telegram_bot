import os
import logging
import asyncio
import traceback
import html
import json
import tempfile
import pydub
from pathlib import Path
from datetime import datetime
import openai

import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction

import config
import database
import openai_utils


# setup
db = database.Database()
logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}

HELP_MESSAGE = """Commands:
⚪ /new – Start new dialog
⚪ /mode – Select chat mode
⚪ /retry – Regenerate last bot answer
⚪ /cancel – Cancel reply
⚪ /settings – Show settings
⚪ /balance – Show balance
⚪ /help – Show help

🎨 Generate images from text prompts in <b>👩‍🎨 Artist</b> /mode
👥 Add bot to <b>group chat</b>: /help_group_chat
🎤 You can send <b>Voice Messages</b> instead of text
"""

HELP_GROUP_CHAT_MESSAGE = """You can add bot to any <b>group chat</b> to help and entertain its participants!

Instructions (see <b>video</b> below):
1. Add the bot to the group chat
2. Make it an <b>admin</b>, so that it can see messages (all other rights can be restricted)
3. You're awesome!

To get a reply from the bot in the chat – @ <b>tag</b> it or <b>reply</b> to its message.
For example: "{bot_username} write a poem about Telegram"
"""


def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    # back compatibility for n_used_tokens field
    n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, int):  # old format
        new_n_used_tokens = {
            "gpt-3.5-turbo": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    # voice message transcription
    if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    # image generation
    if db.get_user_attribute(user.id, "n_generated_images") is None:
        db.set_user_attribute(user.id, "n_generated_images", 0)

    # back compatibility for chat_modes
    if db.get_user_attribute(user.id, "chat_modes") is None:
        db.set_user_attribute(user.id, "chat_modes", config.get_default_chat_modes())

    if db.get_user_attribute(user.id, "current_chat_mode_index") is None:
        db.set_user_attribute(user.id, "current_chat_mode_index", 0)


async def is_bot_mentioned(update: Update, context: CallbackContext):
     try:
         message = update.message

         if message.chat.type == "private":
             return True

         if message.text is not None and ("@" + context.bot.username) in message.text:
             return True

         if message.reply_to_message is not None:
             if message.reply_to_message.from_user.id == context.bot.id:
                 return True
     except:
         return True
     else:
         return False


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    reply_text = "Hi! I'm <b>ChatGPT</b> bot implemented with OpenAI API 🤖\n\n"
    reply_text += HELP_MESSAGE

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)
    await show_chat_modes_handle(update, context)


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def help_group_chat_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update, context, update.message.from_user)
     user_id = update.message.from_user.id
     db.set_user_attribute(user_id, "last_interaction", datetime.now())

     text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)

     await update.message.reply_text(text, parse_mode=ParseMode.HTML)
     await update.message.reply_video(config.help_group_chat_video_path)


async def retry_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("No message to retry 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=False):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    # check if message is edited
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return
    
    # check if it's in a command conversation
    if is_in_command_conversation(update, context):
        if 'add_mode_state' in context.user_data:
            await add_chat_mode_callback_handle(update, context)
            return
        elif 'edit_mode_state' in context.user_data and 'mode_index_to_edit' in context.user_data:
            await edit_chat_mode_content_handle(update, context, update.message.from_user)
            return
        elif 'delete_mode_state' in context.user_data and 'mode_index_to_delete' in context.user_data:
            await delete_chat_mode_confirm_handle(update, context)
            return
    _message = message or update.message.text

    # remove bot mention (in group chats)
    if update.message.chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    chat_mode_index = db.get_user_attribute(user_id, "current_chat_mode_index")

    if chat_mode == "👩‍🎨 Artist":
        await generate_image_handle(update, context, message=message)
        return

    async def message_handle_fn():
        # new dialog timeout
        if use_new_dialog_timeout:
            if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
                await update.message.reply_text(f"Starting new dialog due to timeout (<b>{db.get_chat_modes(user_id)[chat_mode_index]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        # in case of CancelledError
        n_input_tokens, n_output_tokens = 0, 0
        current_model = db.get_user_attribute(user_id, "current_model")

        try:
            # send placeholder message to user
            placeholder_message = await update.message.reply_text("thinking ...")

            # send typing action
            await update.message.chat.send_action(action="typing")

            if _message is None or len(_message) == 0:
                 await update.message.reply_text("🥲 You sent <b>empty message</b>. Please, try again!", parse_mode=ParseMode.HTML)
                 return

            dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[db.get_chat_modes(user_id)[chat_mode_index]["parse_mode"]]
            prompt_start = db.get_chat_modes(user_id)[chat_mode_index]["prompt_start"]

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)
            if config.enable_message_streaming:
                gen = chatgpt_instance.send_message_stream(_message, dialog_messages=dialog_messages, chat_mode_prompt=prompt_start)
            else:
                answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    _message,
                    dialog_messages=dialog_messages,
                    chat_mode_prompt=prompt_start
                )

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                gen = fake_gen()

            prev_answer = ""
            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

                answer = answer[:4096]  # telegram message limit

                # update only when n_update_chunk_symbols new symbols are ready
                n_update_chunk_symbols = config.n_update_chunk_symbols
                if abs(len(answer) - len(prev_answer)) < n_update_chunk_symbols and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=parse_mode)
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue
                    else:
                        await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id)

                await asyncio.sleep(0.01)  # wait a bit to avoid flooding

                prev_answer = answer

            # update user data
            new_dialog_message = {"user": _message, "bot": answer, "date": datetime.now()}
            db.set_dialog_messages(
                user_id,
                db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                dialog_id=None
            )

            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            # note: intermediate token updates only work when enable_message_streaming=True (config.yml)
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise

        except Exception as e:
            error_text = f"Something went wrong during completion. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

        # send message if some messages were removed from the context
        if n_first_dialog_messages_removed > 0:
            text = f"\nSend /new to start a new dialog or go to /settings and switch to the <b>ChatGPT-16k</b> model."
            if n_first_dialog_messages_removed == 1:
                text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context." + text
            else:
                text = f"✍️ <i>Note:</i> Your current dialog is too long, so the <b>first {n_first_dialog_messages_removed} messages</b> were removed from the context." + text
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async with user_semaphores[user_id]:
        task = asyncio.create_task(message_handle_fn())
        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    if user_semaphores[user_id].locked():
        text = "⏳ Please <b>wait</b> for a reply to the previous message\n"
        text += "Or you can /cancel it"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False


async def voice_message_handle(update: Update, context: CallbackContext):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    placeholder_message = await update.message.reply_text("transcribing ...")

    voice = update.message.voice
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)
        voice_ogg_path = tmp_dir / "voice.ogg"

        # download
        voice_file = await context.bot.get_file(voice.file_id)
        await voice_file.download_to_drive(voice_ogg_path)

        # convert to mp3
        voice_mp3_path = tmp_dir / "voice.mp3"
        pydub.AudioSegment.from_file(voice_ogg_path).export(voice_mp3_path, format="mp3")

        # transcribe
        with open(voice_mp3_path, "rb") as f:
            transcribed_text = await openai_utils.transcribe_audio(f)

            if transcribed_text is None:
                 transcribed_text = ""

    text = f"🎤: <i>{transcribed_text}</i>"
    await context.bot.edit_message_text(text, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)

    # await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # update n_transcribed_seconds
    db.set_user_attribute(user_id, "n_transcribed_seconds", voice.duration + db.get_user_attribute(user_id, "n_transcribed_seconds"))

    await message_handle(update, context, message=transcribed_text)


async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    try:
        image_urls = await openai_utils.generate_images(message, n_images=config.return_n_generated_images)
    except openai.error.InvalidRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies."
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            raise

    # token usage
    db.set_user_attribute(user_id, "n_generated_images", config.return_n_generated_images + db.get_user_attribute(user_id, "n_generated_images"))

    for i, image_url in enumerate(image_urls):
        await update.message.chat.send_action(action="upload_photo")
        await update.message.reply_photo(image_url, parse_mode=ParseMode.HTML)


async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    db.start_new_dialog(user_id)
    await update.message.reply_text("Starting new dialog ✅")

    chat_mode_index = db.get_user_attribute(user_id, "current_chat_mode_index")
    await update.message.reply_text(f"{db.get_chat_modes(user_id)[chat_mode_index]['welcome_message']}", parse_mode=ParseMode.HTML)


async def cancel_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    elif is_in_command_conversation(update, context):
        context.user_data.clear()
        await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("<i>Nothing to cancel...</i>", parse_mode=ParseMode.HTML)


def get_chat_mode_menu(user_id: int, page_index: int, action="set_chat_mode"):
    n_chat_modes_per_page = config.n_chat_modes_per_page
    current_mode = db.get_user_attribute(user_id, "current_chat_mode")
    
    if action == "edit_chat_mode":
        text = f"Select the <b>chat mode</b> from below to <b>edit</b>"
    elif action == "delete_chat_mode":
        text = f"Select the <b>chat mode</b> from below to <b>delete</b>"
    else: 
        # defalut: set chat mode
        text = f"Current mode: <b>{current_mode}</b> \nSelect <b>chat mode</b> from below \nYou can also /add, /edit or /delete a chat mode"

    # buttons
    chat_modes = db.get_chat_modes(user_id)
    page_chat_modes = chat_modes[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]
    keyboard = []
    for chat_mode in page_chat_modes:
        name = chat_mode["name"]
        key = chat_modes.index(chat_mode)
        keyboard.append([InlineKeyboardButton(name, callback_data=f"{action}|{key}")])

    # pagination
    if len(chat_modes) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_modes))

        if is_first_page:
            keyboard.append([
                InlineKeyboardButton(">>", callback_data=f"show_chat_modes|{page_index + 1}|{action}")
            ])
        elif is_last_page:
            keyboard.append([
                InlineKeyboardButton("<<", callback_data=f"show_chat_modes|{page_index - 1}|{action}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("<<", callback_data=f"show_chat_modes|{page_index - 1}|{action}"),
                InlineKeyboardButton(">>", callback_data=f"show_chat_modes|{page_index + 1}|{action}")
            ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


# Respond to /mode command
async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(user_id, 0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


# Show different pages of chat modes
async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
     if await is_previous_message_not_answered_yet(update.callback_query, context): return

     user_id = update.callback_query.from_user.id
     db.set_user_attribute(user_id, "last_interaction", datetime.now())

     query = update.callback_query
     await query.answer()

     page_index = int(query.data.split("|")[1])
     action = query.data.split("|")[2]
     if page_index < 0:
         return

     text, reply_markup = get_chat_mode_menu(user_id, page_index, action)
     try:
         await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
     except telegram.error.BadRequest as e:
         if str(e).startswith("Message is not modified"):
             pass


# Set the selected chat mode
async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    chat_modes = db.get_chat_modes(user_id)

    query = update.callback_query
    await query.answer()

    chat_mode_index = int(query.data.split("|")[1])

    db.set_user_attribute(user_id, "current_chat_mode", chat_modes[chat_mode_index]['name'])
    db.set_user_attribute(user_id, "current_chat_mode_index", chat_mode_index)
    db.start_new_dialog(user_id)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        f"{chat_modes[chat_mode_index]['welcome_message']}",
        parse_mode=ParseMode.HTML
    )


def is_in_command_conversation(update: Update, context: CallbackContext):
    if 'add_mode_state' in context.user_data:
        return True
    elif 'delete_mode_state' in context.user_data:
        return True
    elif 'edit_mode_state' in context.user_data:
        return True
    else:
        return False


async def add_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text = "What is the <b>name</b> for the new mode?"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    context.user_data['add_mode_state'] = 'mode_name'


async def add_chat_mode_callback_handle(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if context.user_data['add_mode_state'] == 'mode_name':
        context.user_data['mode_name'] = update.message.text

        text = f"What is the <b>prompt</b> for {context.user_data['mode_name']}?"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

        context.user_data['add_mode_state'] = "mode_prompt"
        return
    
    elif context.user_data['add_mode_state'] == "mode_prompt":
        context.user_data['mode_prompt'] = update.message.text
        
        chat_modes = db.get_chat_modes(user_id)
        new_chat_mode = {
            "name": f"👩🏼‍🎓 {context.user_data['mode_name']}",
            "welcome_message": f"👩🏼‍🎓 Hi, I'm <b>{context.user_data['mode_name']}</b>. How can I help you?",
            "prompt_start": context.user_data['mode_prompt'],
            "parse_mode": "html",
        }

        chat_modes += [new_chat_mode]
        db.set_user_attribute(user_id, "chat_modes", chat_modes)

        text = f"👩🏼‍🎓 {context.user_data['mode_name']} has been added to the modes list"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

        db.set_user_attribute(user_id, "current_chat_mode", f"👩🏼‍🎓 {context.user_data['mode_name']}")

        new_chat_mode_index = len(chat_modes)-1
        db.set_user_attribute(user_id, "current_chat_mode_index", new_chat_mode_index) 

        db.start_new_dialog(user_id)

        text = f"{chat_modes[new_chat_mode_index]['welcome_message']}"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

        context.user_data.clear()
        return
    else: 
        return
    

async def edit_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    context.user_data['edit_mode_state'] = "mode_name"

    text, reply_markup = get_chat_mode_menu(user_id, 0, "edit_chat_mode")
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def edit_chat_mode_callback_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    chat_modes = db.get_chat_modes(user_id)

    query = update.callback_query
    await query.answer()

    context.user_data['edit_mode_state'] = "mode_name"

    mode_index_to_edit = int(query.data.split("|")[1])
    chat_mode_name = chat_modes[mode_index_to_edit]['name']
    context.user_data['mode_index_to_edit'] = mode_index_to_edit

    if chat_mode_name == "👩‍🎨 Artist":
        text = f"👩‍🎨 <b>Artist</b> can't be edited or deleted"
        context.user_data.clear()
    else:
        text = f"What is the new <b>name</b> for <b>{chat_mode_name}</b>?"

    keyboard = []
    keyboard.append([InlineKeyboardButton("Use Current Name", callback_data=f"use_current_name")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )


async def use_current_name_callback_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)

    user_id = update.callback_query.from_user.id
    chat_modes = db.get_chat_modes(user_id)
    mode_index_to_edit = context.user_data['mode_index_to_edit']
    mode_name = chat_modes[mode_index_to_edit]['name'] 
    # remove the leading emoji
    mode_name = mode_name[len("👩🏼‍🎓 "):]
    context.user_data['mode_name'] = mode_name

    await edit_chat_mode_content_handle(update, context, update.callback_query.from_user)


async def use_current_prompt_callback_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)

    user_id = update.callback_query.from_user.id
    chat_modes = db.get_chat_modes(user_id)
    mode_index_to_edit = context.user_data['mode_index_to_edit']
    context.user_data['mode_prompt'] = chat_modes[mode_index_to_edit]['prompt_start']

    await edit_chat_mode_content_handle(update, context, update.callback_query.from_user)


async def edit_chat_mode_content_handle(update: Update, context: CallbackContext, user: User):
    user_id = user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    chat_modes = db.get_chat_modes(user_id)
    mode_index_to_edit = context.user_data['mode_index_to_edit']

    if context.user_data['edit_mode_state'] == 'mode_name':
        if 'mode_name' not in context.user_data:
            context.user_data['mode_name'] = update.message.text
        
        keyboard = []
        keyboard.append([InlineKeyboardButton("Use Current Prompt", callback_data=f"use_current_prompt")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        chat_mode_prompt = chat_modes[mode_index_to_edit]['prompt_start']

        text = f"What is the <b>prompt</b> for 👩🏼‍🎓 {context.user_data['mode_name']}? \n\nCurrent prompt:\n<code>{chat_mode_prompt}</code>"
        try: 
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except AttributeError:
            query = update.callback_query
            bot = context.bot
            await bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.HTML)

        context.user_data['edit_mode_state'] = "mode_prompt"
        return
    
    elif context.user_data['edit_mode_state'] == "mode_prompt":
        if 'mode_prompt' not in context.user_data:
            context.user_data['mode_prompt'] = update.message.text
        
        edited_chat_mode = {
            "name": f"👩🏼‍🎓 {context.user_data['mode_name']}",
            "welcome_message": f"👩🏼‍🎓 Hi, I'm <b>{context.user_data['mode_name']}</b>. How can I help you?",
            "prompt_start": context.user_data['mode_prompt'],
            "parse_mode": "html",
        }

        chat_modes[mode_index_to_edit] = edited_chat_mode
        db.set_user_attribute(user_id, "chat_modes", chat_modes)

        text = f"👩🏼‍🎓 <b>{context.user_data['mode_name']}</b> has been updated"
        try: 
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        except AttributeError:
            query = update.callback_query
            bot = context.bot
            await bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.HTML)

        current_mode_index = db.get_user_attribute(user_id, "current_chat_mode_index")
        if current_mode_index is mode_index_to_edit:
            db.start_new_dialog(user_id)

            text = f"{chat_modes[current_mode_index]['welcome_message']}"
            try:
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            except AttributeError:
                query = update.callback_query
                bot = context.bot
                await bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.HTML)

        context.user_data.clear()
        return
    else: 
        return
    
    
async def delete_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    context.user_data['delete_mode_state'] = "delete"

    text, reply_markup = get_chat_mode_menu(user_id, 0, "delete_chat_mode")
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def delete_chat_mode_callback_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    chat_modes = db.get_chat_modes(user_id)

    query = update.callback_query
    await query.answer()

    context.user_data['delete_mode_state'] = "delete"

    chat_mode_index = int(query.data.split("|")[1])
    chat_mode_name = chat_modes[chat_mode_index]['name']
    context.user_data['mode_index_to_delete'] = chat_mode_index

    if chat_mode_name == "👩‍🎨 Artist":
        text = f"👩‍🎨 <b>Artist</b> can't be edited or deleted"
        context.user_data.clear()
    else:
        text = f"Send '<b>Yes</b>' to confirm that you want to delete <b>{chat_mode_name}</b>"

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        text,
        parse_mode=ParseMode.HTML
    )


async def delete_chat_mode_confirm_handle(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    chat_modes = db.get_chat_modes(user_id)
    chat_mode_delete_index = context.user_data['mode_index_to_delete']

    if update.message.text.lower() == "yes":
        del_name = chat_modes[chat_mode_delete_index]['name']
        del chat_modes[chat_mode_delete_index]
        db.set_user_attribute(user_id, "chat_modes", chat_modes)

        current_mode_index = db.get_user_attribute(user_id, "current_chat_mode_index")

        if current_mode_index is chat_mode_delete_index:
            db.set_user_attribute(user_id, "current_chat_mode", chat_modes[0]['name'])
            db.set_user_attribute(user_id, "current_chat_mode_index", 0)

            text = f"✅ <b>{del_name}</b> is deleted. Switched to <b>{chat_modes[0]['name']}</b>"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

            db.start_new_dialog(user_id)

            text = f"{chat_modes[0]['welcome_message']}"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else: 
            text = f"✅ <b>{del_name}</b> is deleted"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

        context.user_data.clear()
        return
    else:
        text = f"⛔️ <b>{chat_modes[chat_mode_delete_index]['name']}</b> is <b>not</b> deleted"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        context.user_data.clear()
        return
    

def get_settings_menu(user_id: int):
    current_model = db.get_user_attribute(user_id, "current_model")
    text = config.models["info"][current_model]["description"]

    text += "\n\n"
    score_dict = config.models["info"][current_model]["scores"]
    for score_key, score_value in score_dict.items():
        text += "🟢" * score_value + "⚪️" * (5 - score_value) + f" – {score_key}\n\n"

    text += "\nSelect <b>model</b>:"

    # buttons to choose models
    buttons = []
    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title

        buttons.append(
            InlineKeyboardButton(title, callback_data=f"set_settings|{model_key}")
        )
    reply_markup = InlineKeyboardMarkup([buttons])

    return text, reply_markup


async def settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def set_settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    _, model_key = query.data.split("|")
    db.set_user_attribute(user_id, "current_model", model_key)
    # db.start_new_dialog(user_id)

    text, reply_markup = get_settings_menu(user_id)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass


async def show_balance_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # count total usage statistics
    total_n_spent_dollars = 0
    total_n_used_tokens = 0

    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")

    details_text = "🏷️ Details:\n"
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}$</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    # image generation
    image_generation_n_spent_dollars = config.models["info"]["dalle-2"]["price_per_1_image"] * n_generated_images
    if n_generated_images != 0:
        details_text += f"- DALL·E 2 (image generation): <b>{image_generation_n_spent_dollars:.03f}$</b> / <b>{n_generated_images} generated images</b>\n"

    total_n_spent_dollars += image_generation_n_spent_dollars

    # voice recognition
    voice_recognition_n_spent_dollars = config.models["info"]["whisper"]["price_per_1_min"] * (n_transcribed_seconds / 60)
    if n_transcribed_seconds != 0:
        details_text += f"- Whisper (voice recognition): <b>{voice_recognition_n_spent_dollars:.03f}$</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    total_n_spent_dollars += voice_recognition_n_spent_dollars


    text = f"You spent <b>${total_n_spent_dollars:.03f}</b>\n"
    text += f"You used <b>{total_n_used_tokens}</b> tokens\n\n"
    text += details_text

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
    if update.edited_message.chat.type == "private":
        text = "🥲 Unfortunately, message <b>editing</b> is not supported"
        await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # split text into multiple messages due to 4096 character limit
        for message_chunk in split_text_into_chunks(message, 4096):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # answer has invalid characters, so we send it without parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("/new", "start new dialog"),
        BotCommand("/mode", "select a chat mode"),
        BotCommand("/add", "add a chat mode"),
        BotCommand("/edit", "edit a chat mode"),
        BotCommand("/delete", "delete a chat mode"),
        BotCommand("/retry", "regenerate response for the previous query"),
        BotCommand("/cancel", "cancel the current operation"),
        BotCommand("/balance", "show balance"),
        BotCommand("/settings", "show settings"),
        BotCommand("/help", "show help message"),
    ])

def run_bot() -> None:
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .build()
    )

    # add handlers
    user_filter = filters.ALL
    if len(config.allowed_telegram_usernames) > 0:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        user_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    application.add_handler(CommandHandler("add", add_chat_mode_handle, filters=user_filter))
    application.add_handler(CommandHandler("edit", edit_chat_mode_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(edit_chat_mode_callback_handle, pattern="^edit_chat_mode"))
    application.add_handler(CallbackQueryHandler(use_current_name_callback_handle, pattern="^use_current_name"))
    application.add_handler(CallbackQueryHandler(use_current_prompt_callback_handle, pattern="^use_current_prompt"))
    application.add_handler(CommandHandler("delete", delete_chat_mode_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(delete_chat_mode_callback_handle, pattern="^delete_chat_mode"))

    application.add_handler(CommandHandler("settings", settings_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="^set_settings"))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))

    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling()


if __name__ == "__main__":
    run_bot()