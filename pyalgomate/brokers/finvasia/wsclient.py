"""
.. moduleauthor:: Nagaraju Gunda
"""

import os
import sys
import tempfile

import clickhouse_connect
import clickhouse_connect.driver
import clickhouse_connect.driver.client

sys.path.append(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        os.pardir,
    )
)

import datetime
import json
import logging
import pickle
import threading
import time

import yaml
import zmq
from NorenRestApiPy.NorenApi import NorenApi

import pyalgomate.brokers.finvasia as finvasia

logger = logging.getLogger(__name__)
clickhouse_client = None


def createClickhouseTable():
    query = """
    CREATE TABLE IF NOT EXISTS finvasia_market_data (
        instrument String,
        timestamp DateTime64(3),
        ltp Float64,
        volume Float64
    ) ENGINE = MergeTree()
    ORDER BY timestamp
    """
    clickhouse_client.command(query)


def storeDataInClickhouse(data):
    try:
        if clickhouse_client is None:
            return
        query = "INSERT INTO finvasia_market_data (instrument, timestamp, ltp, volume) VALUES"
        values = (
            data["e"] + "|" + data["ts"],
            data["ft"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            float(data["lp"]),
            float(data["volume"]),
        )
        clickhouse_client.command(f"{query} {values}")
    except Exception as e:
        logger.error(f"Error storing data in ClickHouse: {e}")


class WebSocketClient:

    def __init__(self, api, tokenMappings, ipc_path=None):
        assert len(tokenMappings), "Missing subscriptions"
        self.__quotes = dict()
        self.__lastQuoteDateTime = None
        self.__lastReceivedDateTime = None
        self.__api: NorenApi = api
        self.__tokenMappings = tokenMappings
        self.__pendingSubscriptions = list()
        self.__connected = False
        self.__connectionOpened = threading.Event()

        # Set up ZeroMQ publisher using IPC
        self.__context = zmq.Context()
        self.__socket = self.__context.socket(zmq.PUB)

        if ipc_path is None:
            # Create a platform-independent IPC path
            ipc_dir = tempfile.gettempdir()
            ipc_file = "pyalgomate_ipc"
            self.__ipc_path = os.path.join(ipc_dir, ipc_file)
        else:
            self.__ipc_path = ipc_path

        # On Windows, we need to use tcp instead of ipc
        if os.name == "nt":
            self.__socket.bind("tcp://127.0.0.1:*")
            self.__ipc_path = self.__socket.getsockopt(zmq.LAST_ENDPOINT).decode()
        else:
            self.__socket.bind(f"ipc://{self.__ipc_path}")

        self.periodicThread = threading.Thread(target=self.periodicPrint)
        self.periodicThread.daemon = True
        self.periodicThread.start()

    def startClient(self):
        self.__api.start_websocket(
            order_update_callback=self.onOrderUpdate,
            subscribe_callback=self.onQuoteUpdate,
            socket_open_callback=self.onOpened,
            socket_close_callback=self.onClosed,
            socket_error_callback=self.onError,
        )

    def stopClient(self):
        try:
            if self.__connected:
                self.__api.close_websocket()
                self.__socket.close()
                self.__context.term()
        except Exception as e:
            logger.error("Failed to close connection: %s" % e)

    def waitInitialized(self, timeout=10):
        logger.info(
            f"Waiting for WebSocketClient waitInitialized with timeout of {timeout}"
        )
        opened = self.__connectionOpened.wait(timeout)

        if opened:
            logger.info("Connection opened. Waiting for subscriptions to complete")
        else:
            logger.error(f"Connection not opened in {timeout} secs. Stopping the feed")
            return False

        for _ in range(timeout):
            if {
                pendingSubscription
                for pendingSubscription in self.__pendingSubscriptions
            }.issubset(self.__quotes.keys()):
                self.__pendingSubscriptions.clear()
                return True
            time.sleep(1)

        return False

    def onOpened(self):
        logger.info("Websocket connected")
        self.__connected = True
        self.__pendingSubscriptions = list(self.__tokenMappings.keys())
        for channel in self.__pendingSubscriptions:
            logger.info("Subscribing to channel %s." % channel)
            self.__api.subscribe(channel)
        self.__api.subscribe_orders()
        self.__connectionOpened.set()

    def onClosed(self):
        if self.__connected:
            self.__connected = False

        logger.info("Websocket disconnected")

    def onError(self, exception):
        logger.error("Error: %s." % exception)

    def onUnknownEvent(self, event):
        logger.warning("Unknown event: %s." % event)

    def onQuoteUpdate(self, message):
        try:
            key = message["e"] + "|" + message["tk"]
            self.__lastReceivedDateTime = datetime.datetime.now()
            message["ct"] = self.__lastReceivedDateTime

            previousVolume = (
                self.__quotes[key]["v"]
                if key in self.__quotes and "v" in self.__quotes[key]
                else 0
            )
            self.__lastQuoteDateTime = (
                datetime.datetime.fromtimestamp(int(message["ft"]))
                if "ft" in message
                else self.__lastReceivedDateTime.replace(microsecond=0)
            )
            message["ft"] = self.__lastQuoteDateTime
            if key in self.__quotes:
                symbolInfo = self.__quotes[key]
                symbolInfo.update(message)
                self.__quotes[key] = symbolInfo
            else:
                self.__quotes[key] = message

            if "v" in message:
                self.__quotes[key]["volume"] = float(message["v"]) - float(
                    previousVolume
                )
                self.__socket.send_multipart(
                    [b"FEED_UPDATE", pickle.dumps(self.__quotes[key])]
                )
                storeDataInClickhouse(self.__quotes[key])
            else:
                self.__quotes[key]["volume"] = 0
                self.__socket.send_multipart(
                    [b"FEED_UPDATE", pickle.dumps(self.__quotes[key])]
                )
        except Exception as e:
            logger.error(e)

    def onOrderUpdate(self, message):
        logger.info(f"Order update: {message}")
        self.__socket.send_multipart([b"ORDER_UPDATE", json.dumps(message).encode()])

    def periodicPrint(self):
        while True:
            logger.info(
                f"Last Quote: {self.__lastQuoteDateTime}\tLast Received: {self.__lastReceivedDateTime}"
            )
            time.sleep(60)

    def get_ipc_path(self):
        return self.__ipc_path


if __name__ == "__main__":
    import pyalgomate.brokers.finvasia as finvasia

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "[%(levelname)-5s]|[%(asctime)s]|[PID:%(process)d::TID:%(thread)d]|[%(name)s::%(module)s::%(funcName)s::%("
        "lineno)d]|=> %(message)s"
    )

    fileHandler = logging.FileHandler("WebsocketClient.log", "a", "utf-8")
    fileHandler.setLevel(logging.INFO)
    fileHandler.setFormatter(formatter)

    consoleHandler = logging.StreamHandler()
    consoleHandler.setLevel(logging.INFO)
    consoleHandler.setFormatter(formatter)

    logger.addHandler(fileHandler)
    logger.addHandler(consoleHandler)

    logging.getLogger("requests").setLevel(logging.WARNING)

    creds = None
    with open("cred.yml") as f:
        creds = yaml.load(f, Loader=yaml.FullLoader)

    with open("strategies.yaml", "r") as file:
        config = yaml.safe_load(file)

    broker = config["Broker"]
    api, tokenMappings = None, None

    if broker == "Finvasia":
        api, tokenMappings = finvasia.getApiAndTokenMappings(
            creds[broker], registerOptions=["Weekly"], underlyings=config["Underlyings"]
        )
    else:
        logger.error("Broker not supported")
        exit(1)

    wsClient = WebSocketClient(api, tokenMappings)
    logger.info(f"IPC path: {wsClient.get_ipc_path()}")

    if "Clickhouse" in creds:
        clickhouse = creds["Clickhouse"]

        clickhouse_client = clickhouse_connect.get_client(
            host=clickhouse["host"],
            port=clickhouse["port"],
            username=clickhouse["user"],
            password=clickhouse["password"],
        )
        logger.info(
            f"Connected to ClickHouse. Server version: {clickhouse_client.server_version}"
        )
        createClickhouseTable()

    wsClient.startClient()
    if not wsClient.waitInitialized():
        exit(1)
    else:
        logger.info("Initialization complete!")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        wsClient.stopClient()
