"""Slack Bolt app initialization (Socket Mode)."""
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from config import Config
from slack.handlers import register_handlers

logger = logging.getLogger(__name__)

app = App(token=Config.SLACK_BOT_TOKEN, signing_secret=Config.SLACK_SIGNING_SECRET)
register_handlers(app)

# Debug: log every incoming event type
@app.event({"type": "message"})
def debug_message(body):
    logger.info("DEBUG event=message body=%s", body.get("event", {}))


def get_socket_handler() -> SocketModeHandler:
    return SocketModeHandler(app, Config.SLACK_APP_TOKEN)
