#!/usr/bin/env python3

import argparse
from contextlib import contextmanager
from pathlib import Path
import socket
import time

import serial


class Colour:
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'


BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 921600]


class ConnectionConfig:
    def __init__(self, connection_type: str, endpoint: str, speed: int | None = None):
        self.connection_type = connection_type
        self.endpoint = endpoint
        self.speed = speed


@contextmanager
def open_connection(connection: ConnectionConfig, timeout: int):
    if connection.connection_type == 'serial':
        try:
            with serial.Serial(connection.endpoint, baudrate=connection.speed, timeout=timeout) as serial_connection:
                yield serial_connection
        except serial.SerialException as exc:
            raise ConnectionError(f"Error opening serial port: {exc}") from exc
        return

    try:
        host, port = parse_tcp_endpoint(connection.endpoint)
        tcp_socket = socket.create_connection((host, port), timeout=timeout)
        tcp_socket.settimeout(timeout)
        with tcp_socket, tcp_socket.makefile('rwb') as tcp_connection:
            yield tcp_connection
    except OSError as exc:
        raise ConnectionError(f"Error opening TCP socket: {exc}") from exc


def parse_tcp_endpoint(endpoint: str) -> tuple[str, int]:
    host, separator, port = endpoint.rpartition(':')
    if not separator or not host or not port:
        raise ValueError("TCP endpoint must use the format HOST:PORT")
    return host, int(port)


def read_line(connection) -> str:
    return connection.readline().decode('ascii', errors='ignore').strip()


def write_line(connection, message: str):
    connection.write((message + '\r\n').encode('ascii'))


def calculate_nmea_checksum(nmea_sentence: str) -> str:
    checksum = 0
    for char in nmea_sentence[1:]:
        checksum ^= ord(char)
    return f"{nmea_sentence}*{checksum:02X}"


def append_checksum_if_missing(nmea_sentence: str) -> str:
    if '*' not in nmea_sentence:
        return calculate_nmea_checksum(nmea_sentence)
    return nmea_sentence


def write_command(connection, nmea_command: str, verbose: bool = False):
    nmea_command_with_checksum = append_checksum_if_missing(nmea_command)
    write_line(connection, nmea_command_with_checksum)
    if verbose:
        print(f"Sent command: {Colour.OKBLUE}{nmea_command_with_checksum}{Colour.ENDC}")


def load_command_file(file_path: str) -> list[str]:
    commands = []
    for raw_line in Path(file_path).read_text(encoding='ascii').splitlines():
        command = raw_line.strip()
        if not command or command.startswith('#'):
            continue
        commands.append(command)
    return commands


def send_command_batch(connection, commands: list[str], verbose: bool = False, delay: float = 0.5, read_responses: bool = False):
    for command in commands:
        write_command(connection, command, verbose)
        if read_responses:
            response = read_line(connection)
            if response and verbose:
                print(f"Received response: {Colour.OKBLUE}{response}{Colour.ENDC}")
        time.sleep(delay)


def send_config_file(connection: ConnectionConfig, file_path: str, timeout: int, verbose: bool = False, delay: float = 0.5, read_responses: bool = False):
    commands = load_command_file(file_path)
    if not commands:
        print(f"{Colour.WARNING}No commands found in {file_path}.{Colour.ENDC}")
        return

    commands.extend([
        "$PQTMSAVEPAR",
        "$PQTMSRR",
    ])

    try:
        with open_connection(connection, timeout) as gps_connection:
            send_command_batch(gps_connection, commands, verbose, delay, read_responses)
        print(f"{Colour.OKGREEN}Sent {len(commands)} commands from {file_path}.{Colour.ENDC}")
    except (ConnectionError, OSError, ValueError) as exc:
        print(exc)


def build_connection_config(args: argparse.Namespace) -> ConnectionConfig:
    if args.tcp:
        return ConnectionConfig('tcp', args.tcp)
    if args.port:
        return ConnectionConfig('serial', args.port, args.speed)
    raise ValueError("You must provide either a serial port or --tcp HOST:PORT")


def detect_speed(port: str, timeout: int, verbose: bool = False) -> int:
    command = "$PQTMVERNO"
    command_with_checksum = append_checksum_if_missing(command)
    for speed in BAUD_RATES:
        if verbose:
            print(f"Trying baud rate {speed}...")
        try:
            with serial.Serial(port, baudrate=speed, timeout=timeout) as ser:
                ser.write((command_with_checksum + '\r\n').encode('ascii'))
                if verbose:
                    print(f"Sent command: {Colour.OKBLUE}{command_with_checksum}{Colour.ENDC}")
                response = ser.readline().decode('ascii', errors='ignore').strip()
                if response.startswith("$PQTMVERNO"):
                    if verbose:
                        print(f"{Colour.OKGREEN}Received response at {speed} baud: {response}{Colour.ENDC}")
                    return speed
        except serial.SerialException:
            if verbose:
                print(f"{Colour.FAIL}Failed to open serial port at {speed} baud.{Colour.ENDC}")
    raise Exception("Failed to detect baud rate. No valid response for PQTMVERNO command.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send LC29H NMEA command files over TCP or serial.")
    parser.add_argument('port', nargs='?', type=str, help='Serial port to use (e.g., /dev/ttyUSB0 or COM3)')
    parser.add_argument('--tcp', type=str, help='TCP endpoint to use instead of a serial port (format: HOST:PORT)')
    parser.add_argument('--timeout', type=int, default=3, help='Timeout in seconds for receiver responses (default: 3 seconds)')
    parser.add_argument('--speed', type=int, help='Baud rate for serial connections. If not provided, the script will attempt to detect the speed.')
    parser.add_argument('--file', required=True, type=str, help='Path to a text file containing NMEA commands to send')
    parser.add_argument('--command-delay', type=float, default=0.5, help='Delay in seconds between commands (default: 0.5)')
    parser.add_argument('--read-responses', action='store_true', help='Read a line back after each command')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose output')

    args = parser.parse_args()
    try:
        connection = build_connection_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    if connection.connection_type == 'serial':
        if args.speed:
            connection.speed = args.speed
        else:
            connection.speed = detect_speed(connection.endpoint, args.timeout, args.verbose)
            print(f"Detected speed: {connection.speed} baud")
    elif args.speed:
        print(f"{Colour.WARNING}Ignoring --speed for TCP connections.{Colour.ENDC}")

    send_config_file(connection, args.file, args.timeout, args.verbose, args.command_delay, args.read_responses)