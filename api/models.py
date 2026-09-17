from flask import Blueprint, jsonify, g

from config import model_registry

models_bp = Blueprint('models', __name__)


@models_bp.route('/v1/models', methods=['GET'])
def list_models():
    token = g.get("token", "")
    models_map = model_registry.get_models(token)

    models = []
    for model_id, info in models_map.items():
        models.append({
            "id": model_id,
            "object": "model",
            "owned_by": info.root_ai_type,
            "permission": [],
            "capabilities": {
                "image_input": info.supports_images,
                "document_input": info.is_chat,
                "web_search": info.is_chat,
                "thinking_toggle": info.supports_thinking,
            },
        })
    return jsonify({"object": "list", "data": models})
