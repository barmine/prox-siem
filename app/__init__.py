from flask import Flask

from .config import Config
from .dashboard import bp as dashboard_bp
from .ingest import bp as ingest_bp
from .opensearch_client import make_client
from .pipeline_health import bp as pipeline_health_bp
from .ranking import bp as ranking_bp
from .rules_engine import RuleEngine


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    if not app.config["SHARED_SECRET"]:
        raise RuntimeError("SHARED_SECRET environment variable must be set")

    app.extensions["opensearch"] = make_client(app.config["OPENSEARCH_URL"])
    app.extensions["rules"] = RuleEngine(app.config["RULES_PATH"])

    app.register_blueprint(ingest_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(ranking_bp)
    app.register_blueprint(pipeline_health_bp)

    return app
