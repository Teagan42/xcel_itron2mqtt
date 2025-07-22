import yaml
import json
import httpx
import logging
import defusedxml.ElementTree as ET
from typing import Tuple
from tenacity import retry, stop_after_attempt, before_sleep_log, wait_exponential
import asyncio

# Local imports
from xcel_endpoint import XcelEndpoint
from mqtt import Mqtt

IEEE_PREFIX = '{urn:ieee:std:2030.5:ns}'
# Our target cipher is: ECDHE-ECDSA-AES128-CCM8
CIPHERS = ('ECDHE')

logger = logging.getLogger(__name__)

class XcelMeter():

    def __init__(
        self,
        name: str,
        ip_address: str,
        port: int,
        creds: Tuple[str, str],
        mqtt: Mqtt,
        http_client: httpx.AsyncClient,
        mqtt_prefix: str | None = "homeassitant/"
    ) -> None:
        self._name = name
        self._mfid = ""
        self._lfdi = ""
        self._swVer = ""
        self._mqtt_prefix = mqtt_prefix or ""
        self.POLLING_RATE = 5.0
        # Base URL used to query the meter
        self.url = f'https://{ip_address}:{port}'

        # Setup the MQTT server connection
        self.mqtt = mqtt
        # Create a new requests session based on the passed in ip address and port #
        self.http_client = http_client

        # Set to uninitialized
        self.initalized = False

    @property
    def mqtt_topic_prefix(self):
        prefix = self._mqtt_prefix
        if prefix[-1] == "/":
            prefix = prefix[:-1]
        return prefix

    @property
    def mqtt_topic(self):
        return f'{self.mqtt_topic_prefix}/device/energy/{self.name.replace(" ", "_").lower()}'

    @property
    def name(self):
        return f"{self._name} ({self._lfdi})"

    @retry(stop=stop_after_attempt(15),
           wait=wait_exponential(multiplier=1, min=1, max=15),
           before_sleep=before_sleep_log(logger, logging.WARNING),
           reraise=True)
    async def setup(self) -> None:
        # XML Entries we're looking for within the endpoint
        hw_info_names = ['lFDI', 'swVer', 'mfID']
        # Endpoint of the meter used for HW info
        hw_info_url = '/sdev/sdi'
        # Query the meter to get some more details about it
        details_dict = await self.get_hardware_details(hw_info_url, hw_info_names)
        self._mfid = details_dict['mfID']
        self._lfdi = details_dict['lFDI']
        self._swVer = details_dict['swVer']
        logger.info(json.dumps(details_dict, indent=2))
        # Device info used for home assistant MQTT discovery
        self.device_info = {
                            "device": {
                                "identifiers": [self._lfdi],
                                "name": self.name,
                                "model": self._mfid,
                                "sw_version": self._swVer
                                }
                            }
        # Send homeassistant a new device config for the meter
        await self.send_mqtt_config()

        # The swVer will dictate which version of endpoints we use
        endpoints_file_ver = 'default' if str(self._swVer) != '3.2.39' else '3_2_39'
        # List to store our endpoint objects in
        endpoints_list = self.load_endpoints(f'configs/endpoints_{endpoints_file_ver}.yaml')

        # create endpoints from list
        self.endpoints = await self.create_endpoints(endpoints_list, self.device_info)

        # ready to go
        self.initalized = True

    async def get_hardware_details(self, hw_info_url: str, hw_names: list) -> dict:
        """
        Queries the meter hardware endpoint at the ip address passed
        to the class.

        Returns: dict, {<element name>: <meter response>}
        """
        query_url = f'{self.url}{hw_info_url}'
        # query the hw specs endpoint
        resp = await self.http_client.get(query_url, timeout=4.0)
        root = ET.fromstring(resp.text)
        hw_info_dict = {}
        for name in hw_names:
            element = root.find(f'.//{IEEE_PREFIX}{name}')
            if element is not None:
                hw_info_dict[name] = element.text

        return hw_info_dict

    @staticmethod
    def load_endpoints(file_path: str) -> list[dict]:
        """
        Loads the yaml file passed containing meter endpoint information

        Returns: list
        """
        with open(file_path, mode='r', encoding='utf-8') as file:
            endpoints = yaml.safe_load(file)

        return endpoints

    async def create_endpoints(
        self, endpoints: list[dict], device_info: dict
    ) -> list[XcelEndpoint]:
        # Build query objects for each endpoint
        return await asyncio.gather(*[
            XcelEndpoint.create_endpoint(
                self.http_client, self.mqtt,
                f'{self.url}{v["url"]}', self._lfdi,
                endpoint_name, v["tags"],
                device_info, self.mqtt_topic_prefix
            )
            for point in endpoints
            for endpoint_name, v in point.items()
        ])

    # Send MQTT config setup to Home assistant
    async def send_configs(self):
        """
        Sends the MQTT config to the homeassistant topic for
        automatic discovery

        Returns: None
        """
        await asyncio.gather(*[
            obj.mqtt_send_config()
            for obj
            in self.endpoints
        ])

    async def send_mqtt_config(self) -> None:
        """
        Sends a discovery payload to homeassistant for the new meter device

        Returns: None
        """
        state_topic = f'homeassistant/device/energy/{self.name.replace(" ", "_").lower()}'
        config_dict = {
            "name": self.name,
            "device_class": "energy",
            "state_topic": state_topic,
            "unique_id": self._lfdi
        }
        config_dict.update(self.device_info)
        config_json = json.dumps(config_dict)
        logging.debug("Sending MQTT Discovery Payload")
        logging.debug(f"TOPIC: {state_topic}")
        logging.debug(f"Config: {config_json}")
        await self.mqtt.publish(state_topic, config_json.encode("utf-8"))

    async def run(self) -> None:
        """
        Main business loop. Just repeatedly queries the meter endpoints,
        parses the results, packages these up into MQTT payloads, and sends
        them off to the MQTT server

        Returns: None
        """
        try:
            while True:
                await asyncio.sleep(self.POLLING_RATE)
                await asyncio.gather(*[
                    obj.run()
                    for obj
                    in self.endpoints
                ])
        except asyncio.CancelledError:
            await self.mqtt.disconnect()
