from flask import Flask

from .config import Config
from .dashboard import bp as dashboard_bp
from .ingest import bp as ingest_bp
from .opensearch_client import make_client


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    if not app.config["SHARED_SECRET"]:
        raise RuntimeError("SHARED_SECRET environment variable must be set")

    app.extensions["opensearch"] = make_client(app.config["OPENSEARCH_URL"])

    app.register_blueprint(ingest_bp)
    app.register_blueprint(dashboard_bp)

    return app
