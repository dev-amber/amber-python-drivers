import logging
import logging.config
import sys
import threading
import time

import os

import serial

from amberdriver.common import runtime, drivermsg_pb2
from amberdriver.common.message_handler import MessageHandler
from amberdriver.hokuyo import hokuyo_pb2
from amberdriver.hokuyo.hokuyo import Hokuyo
from amberdriver.null.null import NullController
from amberdriver.tools import serial_port, config


__author__ = 'paoolo'

pwd = os.path.dirname(os.path.abspath(__file__))
logging.config.fileConfig('%s/hokuyo.ini' % pwd)
config.add_config_ini('%s/hokuyo.ini' % pwd)

LOGGER_NAME = 'HokuyoController'
HIGH_SENSITIVE = bool(config.HOKUYO_HIGH_SENSITIVE_ENABLE)
SPEED_MOTOR = int(config.HOKUYO_SPEED_MOTOR)
SERIAL_PORT = config.HOKUYO_SERIAL_PORT
BAUD_RATE = config.HOKUYO_BAUD_RATE
TIMEOUT = 0.3


class HokuyoController(MessageHandler):
    def __init__(self, pipe_in, pipe_out, port, _disable_assert=False):
        super(HokuyoController, self).__init__(pipe_in, pipe_out)

        self.__hokuyo = Hokuyo(port, _disable_assert=_disable_assert)

        sys.stderr.write('RESET:\n%s\n' % self.__hokuyo.reset())
        sys.stderr.write('LASER_ON:\n%s\n' % self.__hokuyo.laser_on())
        sys.stderr.write('HIGH_SENSITIVE:\n%s\n' % self.__hokuyo.set_high_sensitive(HIGH_SENSITIVE))
        sys.stderr.write('SPEED_MOTOR:\n%s\n' % self.__hokuyo.set_motor_speed(SPEED_MOTOR))

        sys.stderr.write('SENSOR_SPECS:\n%s\n' % self.__hokuyo.get_sensor_specs())
        sys.stderr.write('SENSOR_STATE:\n%s\n' % self.__hokuyo.get_sensor_state())
        sys.stderr.write('VERSION_INFO:\n%s\n' % self.__hokuyo.get_version_info())

        self.__timestamp, self.__angles, self.__distances = None, [], []
        self.__scan_condition = threading.Condition()

        self.__logger = logging.getLogger(LOGGER_NAME)

        self.__subscribers = []
        self.__subscribers_condition = threading.Condition()

        self.__enable_scanning = False
        self.__scanning_thread = None
        self.__scanning_thread_condition = threading.Condition()

        runtime.add_shutdown_hook(self.terminate)

    @MessageHandler.handle_and_response
    def __handle_get_single_scan(self, received_header, received_message, response_header, response_message):
        self.__logger.debug('Get single scan')

        try:
            self.__subscribers_condition.acquire()
            if len(self.__subscribers) == 0:
                angles, distances, timestamp = self.__get_scan_now()
            else:
                angles, distances, timestamp = self.__get_scan()

        finally:
            self.__subscribers_condition.release()

        response_message = self.__fill_scan(response_message, angles, distances, timestamp)

        return response_header, response_message

    def __handle_enable_scanning(self, header, message):
        self.__enable_scanning = message.Extensions[hokuyo_pb2.enable_scanning]
        self.__logger.debug('Enable scanning, set to %s' % ('true' if self.__enable_scanning else 'false'))

        if self.__enable_scanning:
            self.__try_to_start_scanning_thread()

    def handle_data_message(self, header, message):
        if message.HasExtension(hokuyo_pb2.get_single_scan):
            self.__handle_get_single_scan(header, message)

        elif message.HasExtension(hokuyo_pb2.enable_scanning):
            self.__handle_enable_scanning(header, message)

        else:
            self.__logger.warning('No request or unknown request type in message')

    def handle_subscribe_message(self, header, message):
        self.__logger.debug('Subscribe action')

        try:
            self.__subscribers_condition.acquire()

            current_subscribers_count = len(self.__subscribers)
            self.__subscribers.extend(header.clientIDs)
            if current_subscribers_count == 0:
                self.__try_to_start_scanning_thread()

        finally:
            self.__subscribers_condition.release()

    def handle_unsubscribe_message(self, header, message):
        self.__logger.debug('Unsubscribe action')

        map(lambda client_id: self.__remove_subscriber(client_id), header.clientIDs)

    def handle_client_died_message(self, client_id):
        self.__logger.info('Client %d died' % client_id)

        self.__remove_subscriber(client_id)

    def __scanning_thread(self):
        try:
            while self.is_alive():
                subscribers = self.__get_subscribers()

                if len(subscribers) == 0 and self.__enable_scanning is False:
                    break

                angles, distances, timestamp = self.__get_scan_now()

                response_header = drivermsg_pb2.DriverHdr()
                response_message = drivermsg_pb2.DriverMsg()

                response_message.type = drivermsg_pb2.DriverMsg.DATA
                response_message.ackNum = 0

                response_header.clientIDs.extend(subscribers)
                response_message = self.__fill_scan(response_message, angles, distances, timestamp)

                self.get_pipes().write_header_and_message_to_pipe(response_header, response_message)

            self.__logger.warning('hokuyo: stop')

        finally:
            self.__remove_scanning_thread()

    def __parse_scan(self, scan):
        angles = sorted(scan.keys())
        distances = map(scan.get, self.__angles)
        return angles, distances

    def __get_scan_now(self):
        scan = self.__hokuyo.get_single_scan()
        timestamp = time.time()
        angles, distances = self.__parse_scan(scan)
        self.__set_scan(angles, distances, timestamp)
        return angles, distances, timestamp

    def __set_scan(self, angles, distances, timestamp):
        try:
            self.__scan_condition.acquire()
            self.__angles, self.__distances, self.__timestamp = angles, distances, timestamp

        finally:
            self.__scan_condition.release()

    def __get_scan(self):
        try:
            self.__scan_condition.acquire()
            return self.__angles, self.__distances, self.__timestamp

        finally:
            self.__scan_condition.release()

    @staticmethod
    def __fill_scan(response_message, angles, distances, timestamp):
        response_message.Extensions[hokuyo_pb2.scan].angles.extend(angles)
        response_message.Extensions[hokuyo_pb2.scan].distances.extend(distances)
        response_message.Extensions[hokuyo_pb2.timestamp] = timestamp
        return response_message

    def __return_current_count_and_add_subscribers(self, client_ids):
        try:
            self.__subscribers_condition.acquire()

            current_count = len(self.__subscribers)
            self.__subscribers.extend(client_ids)

            return current_count

        finally:
            self.__subscribers_condition.release()

    def __get_subscribers(self):
        try:
            self.__subscribers_condition.acquire()
            return list(self.__subscribers)

        finally:
            self.__subscribers_condition.release()

    def __get_subscribers_count(self):
        try:
            self.__subscribers_condition.acquire()
            return len(self.__subscribers)

        finally:
            self.__subscribers_condition.release()

    def __remove_subscriber(self, client_id):
        try:
            self.__subscribers_condition.acquire()
            self.__subscribers.remove(client_id)

        except ValueError:
            self.__logger.warning('Client %d does not registered as subscriber' % client_id)

        finally:
            self.__subscribers_condition.release()

    def __try_to_start_scanning_thread(self):
        try:
            self.__scanning_thread_condition.acquire()

            if self.__scanning_thread is None:
                self.__scanning_thread = threading.Thread(target=self.__scanning_thread, name="scanning-thread")
                self.__scanning_thread.start()

        finally:
            self.__scanning_thread_condition.release()

    def __remove_scanning_thread(self):
        try:
            self.__scanning_thread_condition.acquire()
            self.__scanning_thread = None

        finally:
            self.__scanning_thread_condition.release()

    @staticmethod
    def __parse_scan(scan):
        angles = sorted(scan.keys())
        distances = map(scan.get, angles)
        return angles, distances

    def terminate(self):
        self.__logger.warning('hokuyo: terminate')
        self.__hokuyo.close()


if __name__ == '__main__':
    try:
        _serial = serial.Serial(port=SERIAL_PORT, baudrate=BAUD_RATE, timeout=TIMEOUT)
        _serial_port = serial_port.SerialPort(_serial)

        _serial.write('QT\nRS\nQT\n')
        result = ''
        flushing = True
        while flushing:
            char = _serial.read()
            flushing = (char != '')
            result += char
        sys.stderr.write('\n===============\nFLUSH SERIAL PORT\n"%s"\n===============\n' % result)

        controller = HokuyoController(sys.stdin, sys.stdout, _serial_port)
        controller()

    except BaseException as e:
        sys.stderr.write('%s\nRun without Hokuyo.' % str(e))

        controller = NullController(sys.stdin, sys.stdout)
        controller()