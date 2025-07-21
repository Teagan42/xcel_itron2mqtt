import os
import logging
import paho.mqtt.client as mqtt
from time import sleep
from pathlib import Path
from xcelMeter import xcelMeter
import asyncio
from asyncio import TaskGroup
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
import httpx

from common import TerminateTaskGroup

INTEGRATION_NAME = "Xcel Itron 5 (2)"

LOGLEVEL = os.environ.get('LOGLEVEL', 'INFO').upper()
logging.basicConfig(format='%(levelname)s: %(message)s', level=LOGLEVEL)


class CCM8Transport(httpx.AsyncHTTPTransport):
    """
    Async HTTPX transport that mimics the old CCM8Adapter:
    • Forces TLS 1.2
    • Turns off hostname checking (because the meters' cert CNs are a joke)
    • Keeps normal cert-chain validation
    • Re-enables ECDHE ciphers
    """

    def __init__(self, *args, **kwargs):
        # httpx lets `verify=` accept an ssl.SSLContext.
        kwargs["verify"] = self._create_ssl_context()
        super().__init__(*args, **kwargs)

    @staticmethod
    def _create_ssl_context() -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        ctx.check_hostname = False                     # CN vs IP? Yeah, we know.
        ctx.verify_mode = ssl.CERT_REQUIRED            # Still verify the chain.
        ctx.set_ciphers(CIPHERS)                       # Bring back ECDHE.
        return ctx

# mDNS listener to find the IP Address of the meter on the network
class XcelListener(ServiceListener):
    def __init__(self):
        self.info = None

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.info = zc.get_service_info(type_, name)
        print(f"Service {name} added, service info: {self.info}")

def look_for_creds() -> tuple:
    """
    Defaults to extracting the cert and key path from environment variables,
    but if those don't exist it tries to find the hidden credentials files 
    in the default folder of /certs.

    Returns: tuple of paths for cert and key files
    """
    # Find if the cred paths are on PATH
    cert = os.getenv('CERT_PATH')
    key = os.getenv('KEY_PATH')
    cert_path = Path('certs/.cert.pem')
    key_path = Path('certs/.key.pem')
    if cert and key:
        return cert, key
    # If not, look in the local directory
    elif cert_path.is_file() and key_path.is_file():
        return (cert_path, key_path)
    else:
        raise FileNotFoundError('Could not find cert and key credentials')

# def mDNS_search_for_meter() -> list[tuple[str, int]]:
#     """
#     Creates a new zeroconf instance to probe the network for the meter
#     to extract its ip address and port. Closes the instance down when complete.

#     Returns: string, ip address of the meter
#     """
#     zeroconf = Zeroconf()
#     listener = XcelListener()
#     # Meter will respond on _smartenergy._tcp.local. port 5353
#     browser = ServiceBrowser(zeroconf, "_smartenergy._tcp.local.", listener)
#     # Have to wait to hear back from the asynchrounous listener/browser task
#     sleep(10)
#     try:
#         addresses = listener.info.addresses
#     except:
#         raise TimeoutError('Waiting too long to get response from meter')
#     print(listener.info)
#     # Auto parses the network byte format into a legible address
#     meter_info: [
#         ()
#         for address
#         in listener.info.parsed_addresses()
#     ]
#     ip_address = listener.info.parsed_addresses()[0]
#     port = listener.info.port
#     # Close out our mDNS discovery device
#     zeroconf.close()
  
#     return ip_address, port


def get_mqtt_port() -> int:
    """
    Identifies the port to use for the MQTT server. Very basic,
    just offers a detault of 1883 if no other port is set

    Returns: int
    """
    env_port = os.getenv('MQTT_PORT')
    # If environment variable for MQTT port is set, use that
    # if not, use the default
    mqtt_port = int(env_port) if env_port else 1883

    return mqtt_port


def get_mqtt_host() -> str:
    """
    Identifies the host to use for the MQTT server.

    Returns: str
    """
    env_host = os.getenv.get('MQTT_SERVER', None)
    if not env_host:
        raise ValueError('Must specify the MQTT_SERVER env.')
    return env_host


def setup_mqtt(mqtt_server_address, mqtt_port) -> mqtt.Client:
    """
    Creates a new mqtt client to be used for the the xcelQuery
    objects.

    Returns: mqtt.Client object
    """
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logging.info("Connected to MQTT Broker!")
        else:
            logging.error("Failed to connect, return code %d\n", rc)

    # Check if a username/PW is setup for the MQTT connection
    mqtt_username = os.getenv('MQTT_USER')
    mqtt_password = os.getenv('MQTT_PASSWORD')
    # If no env variable was set, skip setting creds?
    client = mqtt.Client()
    if mqtt_username and mqtt_password:
        client.username_pw_set(mqtt_username, mqtt_password)
    client.on_connect = on_connect
    client.connect(mqtt_server_address, mqtt_port)
    client.loop_start()

    return client


def setup_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=CCM8Transport)


async def main() -> None:
    setup_mqtt(get_mqtt_host(), get_mqtt_port())
    creds = look_for_creds()
    http_client = setup_http_client()
    if not os.environ.get('METERS', None):
        raise ValueError("Expected METERS env to be pipe separate list of `ip:port` specifications for each meter to poll.")
    try:
         async with TaskGroup() as group:
            for meter in os.getenv('METERS').split('|'):
                ip_address = meter.split(':')[0]
                port_num = meter.split(':')[1]
                meter = xcelMeter(
                    INTEGRATION_NAME,
                    ip_address,
                    port_num,
                    creds,
                    http_client,
                    os.environ.get('MQTT_TOPIC_PREFIX', None)
                )
                meter.setup()

                if meter.initalized:
                    group.create_task(meter.run())
    except* TerminateTaskGroup:
        pass
    


if __name__ == '__main__':
    asyncio.run(main())
