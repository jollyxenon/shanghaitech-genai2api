from api.chat import chat_bp
from api.models import models_bp
from api.health import health_bp
from api.messages import messages_bp
from api.responses import responses_bp


def register_routes(app):
    """Register routes based on configured API format."""
    config = app.config.get("APP_CONFIG")
    api_format = getattr(config, "api_format", "both") if config else "both"

    if api_format in ("openai", "both"):
        app.register_blueprint(chat_bp)
        app.register_blueprint(responses_bp)

    if api_format in ("anthropic", "both"):
        app.register_blueprint(messages_bp)

    app.register_blueprint(models_bp)
    app.register_blueprint(health_bp)
