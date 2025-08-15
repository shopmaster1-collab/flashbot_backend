# utils/cors.py
from flask_cors import CORS
from config import ALLOWED_ORIGINS

def setup_cors(app):
    CORS(app, resources={r"/chat": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)
