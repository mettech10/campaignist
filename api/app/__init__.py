import logging

from flask import Flask, jsonify
from flask_cors import CORS

from .config import Config


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    app = Flask(__name__)
    app.config.from_object(Config)

    CORS(
        app,
        resources={r"/api/*": {"origins": Config.CORS_ORIGINS}},
        supports_credentials=True,
    )

    missing = Config.validate()
    if missing:
        app.logger.warning("[config] not set: %s", ", ".join(missing))

    from .routes import assets, billing, business, campaigns, health, results

    app.register_blueprint(health.bp)
    app.register_blueprint(business.bp)
    app.register_blueprint(campaigns.bp)
    app.register_blueprint(assets.bp)
    app.register_blueprint(results.bp)
    app.register_blueprint(billing.bp)

    @app.errorhandler(404)
    def not_found(_):
        return jsonify(error="not_found"), 404

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("unhandled error: %s", e)
        return jsonify(error="server_error"), 500

    return app
