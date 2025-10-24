import json
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from bridge import HeatmiserMqttBridge  # adjust import if needed

# Load configuration
with open("options.json") as f:
    config = json.load(f)

def setup_logging():
    """Set up rotating log file and consistent formatting."""
    handler = RotatingFileHandler(
        "logs/heatmiserMqtt.log",
        maxBytes=1_000_000,
        backupCount=5
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(config.get("log_level", "INFO"))
    root_logger.addHandler(handler)


def main():
    setup_logging()
    logging.info("🚀 Starting Heatmiser → MQTT bridge")    

    mqtt_cfg = config.get("mqtt", {})
    hm_cfg = config.get("heatmiser", {})
    zones = config.get("zones", [])
    hotwater = config.get("hotwater", {})

    # Create bridge
    bridge = HeatmiserMqttBridge(
        mqtt_cfg=mqtt_cfg,
        hm_cfg=hm_cfg,
        zones=zones,
        hotwater=hotwater,
        discovery_prefix="homeassistant"
    )

    # Flag for shutdown control
    running = True

    def handle_exit(signum, frame):
        nonlocal running
        logging.info(f"🛑 Received signal {signum}, shutting down gracefully...")
        running = False
        try:
            bridge.mqtt.loop_stop()  # stop MQTT loop
            bridge.mqtt.disconnect()
            logging.info("✅ MQTT disconnected cleanly")
        except Exception as e:
            logging.warning(f"Error while disconnecting MQTT: {e}")
        try:
            bridge.hm_conn.close()
            logging.info("✅ Closed Heatmiser UH1 connection")
        except Exception as e:
            logging.warning(f"Error closing UH1 connection: {e}")

    # Handle Ctrl+C and Docker stop signals
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    logging.info("✅ Bridge started successfully. Running until stopped...")

    # Keep alive loop
    try:
        while running:
            time.sleep(1)
    except Exception as e:
        logging.error(f"Unhandled exception in main loop: {e}")
    finally:
        handle_exit(None, None)
        logging.info("👋 Bridge shutdown complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
