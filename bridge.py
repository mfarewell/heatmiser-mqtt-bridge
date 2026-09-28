#!/usr/bin/env python3
"""
Heatmiser → MQTT Bridge — Home Assistant Auto-Discovery
Safe RS485 access with command queue + immediate state updates
"""

import json
import logging
import threading
import time
import queue
import itertools
import paho.mqtt.client as mqtt
from heatmiserv3 import heatmiser, connection

LOG = logging.getLogger("heatmiser_mqtt_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class HeatmiserMqttBridge:
    def __init__(self, mqtt_cfg, hm_cfg, zones, hotwater=None, discovery_prefix="homeassistant"):
        self.mqtt_cfg = mqtt_cfg
        self.hm_cfg = hm_cfg
        self.zones = {z['name']: z for z in zones}
        self.discovery_prefix = discovery_prefix
        self.hotwater = hotwater
        self.poll_interval = hm_cfg.get("poll_interval", 120)

        # Single UH1 connection + lock
        self.hm_lock = threading.Lock()
        self.hm_conn = connection.HeatmiserUH1(
            device=hm_cfg['device'],
            ipaddress=hm_cfg['ip'], 
            port=hm_cfg['port'],
            url=hm_cfg['url'])

        # Cache Thermostat objects
        self.thermostats = {}
        self.hotwaterThermostat = None
        for name, zone in self.zones.items():
            self.thermostats[name] = heatmiser.HeatmiserThermostat(zone['id'], zone['type'], self.hm_conn)
            if self.hotwater and zone['id'] == self.hotwater['zone_id']:
                LOG.info("Hot water control enabled on zone '%s'", name)
                self.hotwaterThermostat = self.thermostats[name]


        # Command queue with priority (0=high=command,1=low=poll)
        self.task_queue = queue.PriorityQueue()
        self._counter = itertools.count()
        self._poll_pending = False
        self._poll_pending_lock = threading.Lock()

        # Setpoint debouncing and tracking
        self._running = True
        self.debounce_delay = hm_cfg.get("debounce_delay", 1.0)
        self._target_timers = {}
        self._pending_targets = {}
        self._last_target_times = {}
        self._target_lock = threading.Lock()

        # Start worker thread
        threading.Thread(target=self.worker_thread, daemon=True).start()

        # MQTT setup
        self.mqtt = mqtt.Client()
        self.mqtt.username_pw_set(mqtt_cfg['username'], mqtt_cfg['password'])
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        self.mqtt.connect(mqtt_cfg['broker'], mqtt_cfg['port'])
        self.mqtt.loop_start()

        # Publish discovery to MQTT
        for name, zone in self.zones.items():
            self.publish_discovery(name, zone)

        # Start polling thread
        threading.Thread(target=self.state_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # MQTT handlers
    # ------------------------------------------------------------------
    def on_connect(self, client, userdata, flags, rc):
        LOG.info("MQTT connected (rc=%s). Subscribing...", rc)
        for name in self.zones:
            self.mqtt.subscribe(f"home/heatmiser/{name}/set/#")
        if self.hotwater:
            self.mqtt.subscribe("home/heatmiser/hotwater/set/#")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode().strip()
        LOG.info("MQTT RX: %s => %s", topic, payload)

        # --- Hot water ---
        if topic.startswith("home/heatmiser/hotwater/set/") and self.hotwater:
            thermo = self.hotwaterThermostat
            if topic.endswith("hw_state"):
                cmd = payload.upper()
                if cmd == "ON":
                    self.enqueue_task(0, thermo.set_hotwater_state, args=(thermo.HotWaterWriteState.ON,),
                                      desc="HotWater ON", callback=lambda _: self._publish_hotwater_state(cmd))
                elif cmd == "OFF":
                    self.enqueue_task(0, thermo.set_hotwater_state, args=(thermo.HotWaterWriteState.OFF,),
                                      desc="HotWater OFF", callback=lambda _: self._publish_hotwater_state(cmd))
                else:
                    LOG.warning("Invalid hotwater payload: %s", payload)                
            return

        # --- Thermostats ---
        for name, zone in self.zones.items():
            if topic.startswith(f"home/heatmiser/{name}/set/"):
                thermo = self.thermostats[name]

                if topic.endswith("target"):
                    try:
                        val = round(float(payload))
                        if not (5 <= val <= 35):
                            LOG.warning("Invalid target temp (out of range 5-35) for %s: %s", name, payload)
                            return

                        with self._target_lock:
                            # If already at this pending or confirmed value, ignore
                            current_target = thermo.get_target_temp() if thermo.dcb else None
                            if self._pending_targets.get(name) == val or (self._pending_targets.get(name) is None and current_target == val):
                                LOG.debug("Target temp for %s already %d°C, skipping", name, val)
                                return

                            # Cancel existing debounce timer for this zone if running
                            if name in self._target_timers:
                                self._target_timers[name].cancel()

                            self._pending_targets[name] = val
                            self._last_target_times[name] = time.time()
                            timer = threading.Timer(self.debounce_delay, self._apply_target_temp, args=(name, val))
                            self._target_timers[name] = timer
                            timer.start()
                            LOG.debug("Debouncing target temp for %s -> %d°C (delay %.1fs)", name, val, self.debounce_delay)

                    except ValueError:
                        LOG.warning("Invalid target temp for %s: %s", name, payload)

                elif topic.endswith("mode"):
                    frost = payload.upper() == "OFF"
                    expected_mode = "off" if frost else "heat"
                    self.enqueue_task(
                        0, thermo.set_frost_protect_mode, args=(frost,),
                        desc=f"{name} set mode {expected_mode}",
                        callback=lambda _: self._publish_single_state(name, {"mode": expected_mode})
                    )
                return

    def _apply_target_temp(self, name, val):
        """Called when the debounce timer fires for a zone."""
        thermo = self.thermostats[name]
        LOG.info("Debounce elapsed: enqueueing target temp %d°C for %s", val, name)
        self.enqueue_task(
            0, thermo.set_target_temp, args=(val,),
            desc=f"{name} set target {val}°C",
            callback=lambda res: self._on_target_temp_complete(name, val, res)
        )

    def _on_target_temp_complete(self, name, val, res):
        """Handle target temp write completion."""
        with self._target_lock:
            # If a newer target was set while this one was executing, do not overwrite/publish
            if self._pending_targets.get(name) != val:
                LOG.debug("Target temp for %s has newer pending value (%s != %s), skipping publish",
                          name, self._pending_targets.get(name), val)
                return
            self._pending_targets.pop(name, None)
            self._target_timers.pop(name, None)

        thermo = self.thermostats[name]
        if isinstance(thermo.dcb, dict) and 18 in thermo.dcb:
            thermo.dcb[18]["value"] = val

        # Confirm target temperature to MQTT
        self.mqtt.publish(f"home/heatmiser/{name}/state/target", val, retain=True)
        LOG.info("Confirmed target temp for %s: %d°C", name, val)

    # ------------------------------------------------------------------
    # Command queue
    # ------------------------------------------------------------------
    def enqueue_task(self, priority, func, args=(), desc="", is_poll=False, callback=None):
        """Put a task into the queue with priority (0=command,1=poll)."""
        count = next(self._counter)
        self.task_queue.put((priority, count, (func, args, desc, is_poll, callback)))

    def worker_thread(self):
        """Execute commands sequentially, handle callbacks."""
        while self._running:
            try:
                priority, _, (func, args, desc, is_poll, callback) = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                LOG.debug("Executing task: %s [%s]", desc, "poll" if is_poll else "command")
                result = self.with_lock(lambda: func(*args))
                if callback:
                    try:
                        callback(result)
                    except Exception as e:
                        LOG.warning("Callback for %s failed: %s", desc, e)
                if is_poll and isinstance(result, dict):
                    self._publish_poll_results(result)
            except Exception as e:
                LOG.error("Task '%s' failed: %s", desc, e)
            finally:
                if is_poll:
                    with self._poll_pending_lock:
                        self._poll_pending = False
                self.task_queue.task_done()
                time.sleep(0.25 if priority == 1 else 0.5)

    def with_lock(self, func, retries=2, delay=0.3):
        """Run UH1 command safely with retries."""
        for attempt in range(retries + 1):
            with self.hm_lock:
                try:
                    return func()
                except (OSError, TimeoutError) as e:
                    LOG.warning("UH1 command failed (%s), attempt %d/%d", e, attempt + 1, retries)
                    if attempt == retries:
                        self.reconnect()
                time.sleep(delay)
        raise Exception("Failed to communicate with UH1 after retries")

    def reconnect(self):
        with self.hm_lock:
            try:
                self.hm_conn.close()
            except Exception:
                pass
            time.sleep(1)
            self.hm_conn = connection.HeatmiserUH1(device=self.hm_cfg['device'],
                ipaddress=self.hm_cfg['ip'], 
                port=self.hm_cfg['port'],
                url=self.hm_cfg['url'])
            LOG.info("Reconnected to UH1")

    # ------------------------------------------------------------------
    # State publishing
    # ------------------------------------------------------------------
    def _publish_single_state(self, name, overrides):
        """Publish state update for specified keys for a single thermostat."""
        thermo = self.thermostats[name]
        # Keep internal DCB updated if present
        if isinstance(thermo.dcb, dict):
            if "target" in overrides and 18 in thermo.dcb:
                thermo.dcb[18]["value"] = overrides["target"]
            if "mode" in overrides and 23 in thermo.dcb:
                thermo.dcb[23]["value"] = 1 if overrides["mode"] == "off" else 0

        for key, value in overrides.items():
            self.mqtt.publish(f"home/heatmiser/{name}/state/{key}", value, retain=True)
            LOG.debug("Immediate MQTT update for %s: %s = %s", name, key, value)

    def _publish_hotwater_state(self, hw_state=None):
        """Publish hot water state."""
        if not self.hotwater:
            return
        if hw_state is None:
            time.sleep(0.5) # slight delay to ensure state is updated    
            self.hotwaterThermostat.read_dcb()  # refresh data from UH1        
            hw_state = self.hotwaterThermostat.get_hotwater_state().upper()
            LOG.debug("Hot Water state not provided real state obtained: %s", hw_state)
        try:
            self.mqtt.publish("home/heatmiser/hotwater/state/hw_state", hw_state, retain=True)
            LOG.debug("Hot water state published: %s", hw_state)
        except Exception as e:
            LOG.warning("Failed to publish hot water state: %s", e)

    def publish_states(self):
        """Poll all thermostats and hot water (low-priority)."""
        def poll_all():
            results = {}
            for name, thermo in self.thermostats.items():
                thermo.read_dcb()  # refresh data from UH1
                zone = self.zones[name]
                temp = thermo.get_air_temp() if zone.get("sensor_type") == "air" else thermo.get_floor_temp()
                target = thermo.get_target_temp()
                mode = "off" if thermo.get_run_mode() == "frost" else "heat"
                action = "heating" if thermo.get_current_state() else "idle"
                hw_state = None                
                results[name] = dict(
                    temperature=temp, target=target, mode=mode, action=action, hw_state=hw_state
                )
                time.sleep(0.05)
            if self.hotwater:
                hw_state = self.hotwaterThermostat.get_hotwater_state().upper()
                results["hotwater"] = {"hw_state": hw_state} 
            return results

        with self._poll_pending_lock:
            if self._poll_pending:
                return
            self._poll_pending = True
        self.enqueue_task(1, poll_all, desc="Poll all thermostats", is_poll=True)

    def _publish_poll_results(self, results):
        """Publish results of poll to MQTT (outside lock)."""
        now = time.time()
        for name, res in results.items():
            if name == "hotwater" and self.hotwater:
                if res.get("hw_state") is not None:
                    self.mqtt.publish("home/heatmiser/hotwater/state/hw_state", res['hw_state'], retain=True)
                    LOG.debug("Published polled hot water state")
                continue

            self.mqtt.publish(f"home/heatmiser/{name}/state/temperature", res['temperature'], retain=True)

            # Guard against overwriting recently commanded target
            with self._target_lock:
                has_pending = name in self._pending_targets
                is_recent = (now - self._last_target_times.get(name, 0)) < 15.0

            if not has_pending and not is_recent:
                self.mqtt.publish(f"home/heatmiser/{name}/state/target", res['target'], retain=True)
            else:
                LOG.debug("Skipping polled target publish for %s due to recent/pending command", name)

            self.mqtt.publish(f"home/heatmiser/{name}/state/mode", res['mode'], retain=True)
            self.mqtt.publish(f"home/heatmiser/{name}/state/action", res['action'], retain=True)            
            LOG.debug("Published polled state for %s", name)

    # ------------------------------------------------------------------
    # Home Assistant discovery
    # ------------------------------------------------------------------
    def publish_discovery(self, name, zone):
        tid = zone['id']
        climate_topic = f"{self.discovery_prefix}/climate/heatmiser_{name}/config"
        climate_payload = {
            "name": f"{name.capitalize()} Thermostat",
            "unique_id": f"heatmiser_{tid}_climate",
            "current_temperature_topic": f"home/heatmiser/{name}/state/temperature",
            "temperature_state_topic": f"home/heatmiser/{name}/state/target",
            "temperature_command_topic": f"home/heatmiser/{name}/set/target",
            "min_temp": 5,
            "max_temp": 30,
            "temp_step": 1.0,
            "optimistic": True,
            "modes": ["heat", "off"],
            "mode_state_topic": f"home/heatmiser/{name}/state/mode",
            "mode_command_topic": f"home/heatmiser/{name}/set/mode",
            "action_topic": f"home/heatmiser/{name}/state/action",
        }
        self.mqtt.publish(climate_topic, json.dumps(climate_payload), retain=True)
        LOG.info("Published discovery for zone %s", name)

        if zone.get("type", "").lower() == "prthw" and self.hotwater:
            switch_topic = f"{self.discovery_prefix}/switch/heatmiser_hotwater/config"
            switch_payload = {
                "name": self.hotwater.get("name", "Hot Water"),
                "unique_id": f"heatmiser_{tid}_hotwater",
                "command_topic": "home/heatmiser/hotwater/set/hw_state",
                "state_topic": "home/heatmiser/hotwater/state/hw_state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "optimistic": True,
            }
            self.mqtt.publish(switch_topic, json.dumps(switch_payload), retain=True)
            LOG.info("Published hot water discovery for %s", name)

    # ------------------------------------------------------------------
    # Background polling loop
    # ------------------------------------------------------------------
    def state_loop(self):
        while self._running:
            try:
                self.publish_states()
            except Exception as e:
                LOG.error("Polling loop failed: %s", e)

            # occasional queue health log
            if int(time.time()) % 600 < 2:
                LOG.debug("Queue size: %d", self.task_queue.qsize())

            for _ in range(self.poll_interval):
                if not self._running:
                    break
                time.sleep(1)

    def stop(self):
        """Cancel pending debounce timers and clean up resources."""
        self._running = False
        with self._target_lock:
            for timer in self._target_timers.values():
                timer.cancel()
            self._target_timers.clear()
            self._pending_targets.clear()
        LOG.info("HeatmiserMqttBridge stopped")
