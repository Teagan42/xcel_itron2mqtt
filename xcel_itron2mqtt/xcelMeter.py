import os
import ssl
import yaml
import json
import httpx
import logging
import xml.etree.ElementTree as ET
from time import sleep
from typing import Tuple, Callable
from requests.packages.urllib3.util.ssl_ import create_urllib3_context
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, before_sleep_log, wait_exponential
import asyncio

# Local imports
from xcelEndpoint import xcelEndpoint
from types import PublishFn

IEEE_PREFIX = '{urn:ieee:std:2030.5:ns}'
# Our target cipher is: ECDHE-ECDSA-AES128-CCM8
CIPHERS = ('ECDHE')

logger = logging.getLogger(__name__)


# Create an adapter for our request to enable the non-standard cipher
# From https://lukasa.co.uk/2017/02/Configuring_TLS_With_Requests/
class CCM8Adapter(HTTPAdapter):
    """
    A TransportAdapter that re-enables ECDHE support in Requests.
    Not really sure how much redundancy is actually required here
    """
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self.create_ssl_context()
        return super(CCM8Adapter, self).init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs['ssl_context'] = self.create_ssl_context()
        return super(CCM8Adapter, self).proxy_manager_for(*args, **kwargs)

    def create_ssl_context(self):
        ssl_version=ssl.PROTOCOL_TLSv1_2
        context = create_urllib3_context(ssl_version=ssl_version)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.set_ciphers(CIPHERS)
        return context

class xcelMeter():

    def __init__(
        self,
        name: str,
        ip_address: str,
        port: int,
        creds: Tuple[str, str],
        mqtt_publish: PublishFn,
        http_client: httpx.AsyncClient,
        mqtt_prefix: str | None = "homeassitant/"
    ) -> None:
        self._name = name
        self._mfid = ""
        self._lfdi = ""
        self._swVer = ""
        self._mqtt_prefix = mqtt_prefix
        self.POLLING_RATE = 5.0
        # Base URL used to query the meter
        self.url = f'https://{ip_address}:{port}'

        # Setup the MQTT server connection
        self.publish = mqtt_publish
        # Create a new requests session based on the passed in ip address and port #
        self.http_client = http_client

        # Set to uninitialized
        self.initalized = False

    @property
    def mqtt_topic_prefix(self):
        prefix = self._mqtt_prefix
        if prefix.endswith("/"):
            prefix = prefix[:-1]
        return prefix
    
    @property
    def mqtt_topic(self):
        return f'{self.mqtt_topic_prefix}/device/energy/{self.name.replace(" ", "_").lower()}_${self._lfdi}'

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
        self.endpoints_list = self.load_endpoints(f'configs/endpoints_{endpoints_file_ver}.yaml')

        # create endpoints from list
        self.endpoints = self.create_endpoints(self.endpoints_list, self.device_info)

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
        async with self.http_client.get(query_url, verify=False, timeout=4.0) as resp:
        # Parse the response xml looking for the passed in element names
            root = ET.fromstring(resp.text)
        hw_info_dict = {}
        for name in hw_names:
            hw_info_dict[name] = root.find(f'.//{IEEE_PREFIX}{name}').text

        return hw_info_dict

    @staticmethod
    def load_endpoints(file_path: str) -> list:
        """
        Loads the yaml file passed containing meter endpoint information

        Returns: list
        """
        with open(file_path, mode='r', encoding='utf-8') as file:
            endpoints = yaml.safe_load(file)

        return endpoints

    def create_endpoints(self, endpoints: dict, device_info: dict) -> None:
        # Build query objects for each endpoint
        query_obj = []
        for point in endpoints:
            for endpoint_name, v in point.items():
                request_url = f'{self.url}{v["url"]}'
                query_obj.append(
                    xcelEndpoint(
                        self.http_client,
                        self.publish,
                        request_url,
                        self._lfdi,
                        endpoint_name,
                        v['tags']
                        device_info,
                        self.mqtt_topic_prefix
                    )
                )
        return query_obj
    

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
            in self.query_obj
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
        logging.debug(f"Sending MQTT Discovery Payload")
        logging.debug(f"TOPIC: {state_topic}")
        logging.debug(f"Config: {config_json}")
        await self.publish(state_topic, str(config_json))

    async def run(self) -> None:
        """
        Main business loop. Just repeatedly queries the meter endpoints,
        parses the results, packages these up into MQTT payloads, and sends
        them off to the MQTT server

        Returns: None
        """
        while True:
            asyncio.sleep(self.POLLING_RATE)
            await asyncio.gather(*[
                obj.run()
                for obj
                in self.endpoints
            ])
