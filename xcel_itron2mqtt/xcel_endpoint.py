import asyncio
import json
import httpx
import logging
import xml.etree.ElementTree as ET
from copy import deepcopy
from tenacity import retry, stop_after_attempt, before_sleep_log, wait_exponential
from mqtt import Mqtt

logger = logging.getLogger(__name__)

# Prefix that appears on all of the XML elements
IEEE_PREFIX = '{urn:ieee:std:2030.5:ns}'

class XcelEndpoint():
    """
    Class wrapper for all readings associated with the Xcel meter.
    Expects a request session that should be shared amongst the 
    instances.
    """
    @staticmethod
    async def create_endpoint(
        http_client: httpx.AsyncClient,
        mqtt: Mqtt,
        url: str,
        ldfi: str,
        name: str,
        tags: dict,
        device_info: dict,
        mqtt_prefix: str,
        meter_name: str
    ) -> "XcelEndpoint":
        endpoint = XcelEndpoint(
            http_client, mqtt, url, ldfi, name, tags, device_info, mqtt_prefix, meter_name
        )
        await endpoint.mqtt_send_config()
        return endpoint

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        mqtt: Mqtt, 
        url: str,
        ldfi: str,
        name: str,
        tags: dict,
        device_info: dict,
        mqtt_prefix: str,
        meter_name: str
    ):
        self.session = http_client
        self.mqtt = mqtt
        self.url = url
        self.ldfi = ldfi
        self.name = name
        self.tags = tags
        self.device_info = device_info
        self.meter_name = meter_name.replace(" ", "_").lower()
        self._mqtt_topic_prefix = mqtt_prefix

        self._current_response = None
        self._mqtt_topic = None
        # Record all of the sensor state topics in an easy to lookup dict
        self._sensor_state_topics = {}

    @property
    def mqtt_topic_prefix(self):
        prefix = self._mqtt_topic_prefix
        if prefix.endswith("/"):
            prefix = prefix[:-1]
        return prefix

    @retry(stop=stop_after_attempt(15),
           wait=wait_exponential(multiplier=1, min=1, max=15),
           before_sleep=before_sleep_log(logger, logging.WARNING),
           reraise=True)
    async def query_endpoint(self) -> str:
        """
        Sends a request to the given endpoint associated with the 
        object instance

        Returns: str in XML format of the meter's response
        """
        resp = await self.session.get(self.url, timeout=15.0)
        return resp.text

    @staticmethod
    def parse_response(response: str, tags: dict) -> dict:
        """
        Drill down the XML response from the meter and extract the
        readings according to the endpoints.yaml structure.

        Returns: dict in the nesting structure of found below each tag
        in the endpoints.yaml
        """
        readings_dict = {}
        logger.info(response)
        root = ET.fromstring(response)
        # Kinda gross
        for k, v in tags.items():
            if isinstance(v, list):
                for val_items in v:
                    if not isinstance(val_items, dict):
                        continue
                    for k2 in val_items.keys():
                        search_val = f'{IEEE_PREFIX}{k2}'
                        element = root.find(f".//{search_val}")
                        if element is not None:
                            value = element.text
                            readings_dict[f'{k}{k2}'] = value
            else:
                search_val = f'{IEEE_PREFIX}{k}'
                element = root.find(f".//{search_val}")
                if element is not None:
                    value = element.text
                    readings_dict[k] = value
        logger.info(json.dumps(readings_dict, indent=2))
        return readings_dict

    async def get_reading(self) -> dict:
        """
        Query the endpoint associated with the object instance and
        return the parsed XML response in the form of a dictionary
        
        Returns: Dict in the form of {reading: value}
        """
        response = await self.query_endpoint()
        self.current_response = self.parse_response(response, self.tags)

        return self.current_response

    def create_config(self, sensor_name: str,  details: dict) -> tuple[str, dict]:
        """
        Helper to generate the JSON sonfig payload for setting
        up the new Homeassistant entities

        Returns: Tuple consisting of a string representing the mqtt
        topic, and a dict to be used as the payload.
        """
        logger.info(json.dumps(details))
        payload = deepcopy(details)
        mqtt_friendly_name = f"{self.name.replace(" ", "_")}"
        entity_type = payload.pop('entity_type')
        payload["state_topic"] = f'{self.mqtt_topic_prefix}/{entity_type}/{self.meter_name}/{mqtt_friendly_name}/{sensor_name}/state'
        payload['name'] = f'{self.name} {sensor_name} ({self.ldfi})'
        # Mouthful
        # Unique ID becomes the device name + class name + sensor name, all lower case, all underscores instead of spaces
        payload['unique_id'] = f"{self.device_info['device']['name']}_{self.ldfi}_{self.name}_{sensor_name}".lower().replace(' ', '_')
        payload.update(self.device_info)
        # MQTT Topics don't like spaces
        mqtt_topic = f'{self.mqtt_topic_prefix}/{entity_type}/{self.meter_name}/{mqtt_friendly_name}/{sensor_name}/config'
        # Capture the state topic the sensor is associated with for later use
        self._sensor_state_topics[sensor_name] = payload['state_topic']
        logger.info(json.dumps(payload, indent=2))

        return mqtt_topic, payload

    async def mqtt_send_config(self) -> None:
        """
        Homeassistant requires a config payload to be sent to more
        easily setup the sensor/device once it appears over mqtt
        https://www.home-assistant.io/integrations/mqtt/
        """
        logger.info(self.tags)
        _tags = deepcopy(self.tags)
        async def process_list_item(k, v_item):
            name, details = v_item.popitem()
            sensor_name = f'{k}{name}'
            mqtt_topic, payload = self.create_config(sensor_name, details)
            # Send MQTT payload
            await self.mqtt_publish(mqtt_topic, payload, retain=True)
        async def process_list(k, v):
            await asyncio.gather(*[
                process_list_item(k, v_item)
                for v_item
                in v
            ])
        async def process_obj(k, v):
            mqtt_topic, payload = self.create_config(k, v)
            await self.mqtt_publish(
                mqtt_topic, payload, retain=True
            )

        await asyncio.gather(*[
            process_list(k, v) if isinstance(v, list) else process_obj(k, v)
            for k, v
            in _tags.items()
        ])

    async def process_send_mqtt(self, reading: dict) -> None:
        """
        Run through the readings from the meter and translate
        and prepare these readings to send over mqtt

        Returns: None
        """
        mqtt_topic_message = {}
        # Cycle through all the readings for the given sensor
        for k, v in reading.items():
            # Figure out which topic this reading needs to be sent to
            topic = self._sensor_state_topics[k]
            if topic not in mqtt_topic_message.keys():
                mqtt_topic_message[topic] = {}
            # Create dict of {topic: payload}
            mqtt_topic_message[topic] = v

        logger.info(
            f"Sending MQTT payloads: {json.dumps(mqtt_topic_message, indent=2)}"
        )

        # Cycle through and send the payload to the associated keys
        await asyncio.gather(*[
            self.mqtt_publish(topic, payload)
            for topic, payload
            in mqtt_topic_message.items()
        ])

    async def mqtt_publish(self, topic: str, message: str | dict, retain=False) -> None:
        """
        Publish the given message to the topic associated with the class
       
        Returns: integer
        """
        payload = json.dumps(message) if isinstance(message, dict) else message
        await self.mqtt.publish(topic, payload.encode("utf-8"), retain=retain)

    async def run(self) -> None:
        """
        Main business loop for the endpoint class.
        Read from the meter, process and send over MQTT

        Returns: None
        """
        reading = await self.get_reading()
        await self.process_send_mqtt(reading)
