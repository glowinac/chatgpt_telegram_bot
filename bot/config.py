import os
import yaml
import dotenv
from pathlib import Path

config_dir = Path(__file__).parent.parent.resolve() / "config"
chat_modes = {}

# load yaml config
with open(config_dir / "config.yml", 'r') as f:
    config_yaml = yaml.safe_load(f)

# load .env config
# config_env = dotenv.dotenv_values(config_dir / "config.env")
dotenv.load_dotenv(config_dir / "config.env")

# config parameters
telegram_token = os.getenv("TELEGRAM_TOKEN")
openai_api_key = os.getenv("OPENAI_API_KEY")
use_chatgpt_api = config_yaml.get("use_chatgpt_api", True)
allowed_telegram_usernames = config_yaml["allowed_telegram_usernames"]
new_dialog_timeout = config_yaml["new_dialog_timeout"]
enable_message_streaming = config_yaml.get("enable_message_streaming", True)
return_n_generated_images = config_yaml.get("return_n_generated_images", 1)
n_chat_modes_per_page = config_yaml.get("n_chat_modes_per_page", 5)
n_update_chunk_symbols = config_yaml.get("n_update_chunk_symbols", 50)
mongodb_uri = os.getenv("MONGO_CONNECT_STRING")
#mongodb_uri = f"mongodb://mongo:{config_env['MONGODB_PORT']}"

# chat_modes
with open(config_dir / "chat_modes.yml", 'r') as f:
    chat_modes = yaml.safe_load(f)

def get_default_chat_modes():
    default_chat_modes = []

    for chat_mode in chat_modes.keys():   
        new_chat_mode = {
            "name": chat_modes[chat_mode]["name"],
            "welcome_message": chat_modes[chat_mode]["welcome_message"],
            "prompt_start": chat_modes[chat_mode]["prompt_start"],
            "parse_mode": chat_modes[chat_mode]["parse_mode"]
        }
        default_chat_modes += [new_chat_mode]

    return default_chat_modes

# models
with open(config_dir / "models.yml", 'r') as f:
    models = yaml.safe_load(f)

# files
help_group_chat_video_path = Path(__file__).parent.parent.resolve() / "static" / "help_group_chat.mp4"
