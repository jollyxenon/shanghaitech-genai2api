import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from auth.cas_login import LoginError
from auth.token_manager import TokenManager
from config import Config
from app import create_app

load_dotenv(Path(__file__).resolve().parent / ".env")

parser = argparse.ArgumentParser(description='GenAI Flask API Server')
parser.add_argument('--token', type=str, default=None,
                    help='JWT token (eyJ...) or student_id@password for auto-login (or set GENAI_TOKEN)')
parser.add_argument('--port', type=int, default=None,
                    help='Flask server port (or set PORT; default: 31100)')
parser.add_argument('--debug', action='store_true',
                    help='Enable debug logging')
parser.add_argument('--api-key', type=str, default=None,
                    help='API key for client authentication (or set API_KEY env var)')
parser.add_argument('--api-format', type=str, default=None,
                    choices=['openai', 'anthropic', 'both'],
                    help='API format: openai, anthropic, or both (default: API_FORMAT env var or both)')
args = parser.parse_args()
token_input = args.token or os.environ.get("GENAI_TOKEN")

if not token_input:
    parser.error("token is required via --token or GENAI_TOKEN")

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

try:
    token_manager = TokenManager(token_input)
    token_manager.initial_login()
except (LoginError, ValueError) as e:
    logger.error("Failed to initialize token: %s", e)
    sys.exit(1)

config = Config(
    token_manager=token_manager,
    port=args.port if args.port is not None else int(os.environ.get("PORT", "31100")),
    api_key=args.api_key or os.environ.get("API_KEY"),
    debug=args.debug,
    api_format=args.api_format or os.environ.get("API_FORMAT", "both"),
)

app = create_app(config)

if __name__ == '__main__':
    if config.api_format == "anthropic":
        api_format_name = "Anthropic (/v1/messages)"
    elif config.api_format == "openai":
        api_format_name = "OpenAI (/v1/chat/completions + /v1/responses)"
    else:
        api_format_name = "OpenAI (/v1/chat/completions + /v1/responses) + Anthropic (/v1/messages)"
    logger.info("Starting GenAI proxy on port %d", config.port)
    logger.info("API Format: %s, Auth: %s, Token mode: %s",
                api_format_name, "enabled" if config.api_key else "disabled",
                token_manager.mode)
    app.run(host='0.0.0.0', port=config.port, debug=False)
