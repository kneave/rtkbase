#!/usr/bin/env python3

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import time
import re
import sys
from datetime import datetime, timezone
from pyproj import Transformer
try:
    import serial
except ImportError as exc:
    raise SystemExit(
        "pyserial is required. Install it with: pip install pyserial"
    ) from exc

if not hasattr(serial, 'Serial'):
    raise SystemExit(
        "The imported 'serial' module is not pyserial. Remove the conflicting 'serial' package and install pyserial."
    )


# Terminal colour codes
class Colour:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    GREEN = '\033[92m'

# Accuracy thresholds (constants for easier adjustment)
ACCURACY_THRESHOLD_1M = 1.0
ACCURACY_THRESHOLD_10CM = 0.1

# Common baud rates to try for auto-detection
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


def read_raw_line(connection) -> bytes:
    return connection.readline()


def read_line(connection) -> str:
    return read_raw_line(connection).decode('ascii', errors='ignore').strip()


def format_serial_context(raw_response: bytes) -> str:
    stripped = raw_response.strip()
    if not stripped:
        return '<empty line>'

    decoded = stripped.decode('ascii', errors='ignore')
    printable = ''.join(char if 32 <= ord(char) <= 126 else '.' for char in decoded)
    hex_preview = stripped[:24].hex()

    if printable.startswith('$'):
        return printable

    if printable:
        return f"{printable} [hex:{hex_preview}]"

    return f"<binary {len(stripped)} bytes> [hex:{hex_preview}]"


def write_line(connection, message: str):
    connection.write((message + '\r\n').encode('ascii'))


def write_command(connection, nmea_command: str, verbose: bool = False):
    nmea_command_with_checksum = append_checksum_if_missing(nmea_command)
    write_line(connection, nmea_command_with_checksum)
    if verbose:
        print(f"Sent command: {Colour.OKBLUE}{nmea_command_with_checksum}{Colour.ENDC}")


def send_command_batch(connection, commands: list[str], verbose: bool = False, delay: float = 0.2):
    for command in commands:
        write_command(connection, command, verbose)
        time.sleep(delay)


def wait_for_command_response(connection, timeout: int, expected_tokens: list[str], verbose: bool = False) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = read_line(connection)
        if not response:
            continue
        if verbose:
            print(f"Received response: {Colour.OKBLUE}{response}{Colour.ENDC}")
        if any(token in response for token in expected_tokens):
            return response
    return None


def queue_quectel_survey_commands(connection, min_dur: int, acc_limit: float, verbose: bool = False):
    survey_commands = [
        "$PQTMCFGRCVRMODE,W,2",
        f"$PQTMCFGSVIN,W,1,{min_dur},{acc_limit},0.0,0.0,0.0",
        "$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1",
        "$PQTMSAVEPAR",
        "$PQTMSRR",
    ]

    send_command_batch(connection, survey_commands, verbose)


def wait_for_receiver_reset(connection, timeout: int, verbose: bool = False):
    reset_deadline = time.time() + timeout
    while time.time() < reset_deadline:
        response = read_line(connection)
        if not response:
            continue
        if verbose:
            print(f"Received response: {Colour.OKBLUE}{response}{Colour.ENDC}")
        return


def write_survey_result(output_file: str, result: dict):
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


def monitor_survey_status(connection: ConnectionConfig, timeout: int, min_dur: int, output_file: str, verbose: bool = False):
    with open_connection(connection, timeout) as gps_connection:
        print(Colour.HEADER + Colour.BOLD + "Starting survey-in..." + Colour.ENDC)

        start_time = time.time()
        prev_ecef = None
        prev_geo = None
        prev_latlon = None
        reset_cursor = False
        saw_in_progress = False
        warned_stale_completion = False

        for response in stream_gps_messages(gps_connection, verbose):
            status = parse_svin_status(response)
            if not status:
                continue

            valid_flag = status['valid_flag']
            mean_x = status['mean_x']
            mean_y = status['mean_y']
            mean_z = status['mean_z']
            mean_acc = status['mean_acc']
            obs_count = status['obs_count']
            elapsed_time = int(time.time() - start_time)
            target_duration = status['target_duration'] or min_dur
            remaining_time = max(0, int(target_duration - elapsed_time))

            if prev_ecef != (mean_x, mean_y, mean_z):
                lat, lon, alt = ecef_to_geodetic(mean_x, mean_y, mean_z)
                prev_latlon = f"Lat={lat:.7f}, Lon={lon:.7f}, Alt={alt:.3f}"

            current_ecef = f"X={mean_x:.4f}, Y={mean_y:.4f}, Z={mean_z:.4f}"

            if prev_ecef:
                colourised_ecef = colour_diff(current_ecef, f"X={prev_ecef[0]:.4f}, Y={prev_ecef[1]:.4f}, Z={prev_ecef[2]:.4f}")
                colourised_geo = colour_diff(prev_latlon, prev_geo)
            else:
                colourised_ecef = current_ecef
                colourised_geo = prev_latlon

            prev_ecef = (mean_x, mean_y, mean_z)
            prev_geo = prev_latlon

            coloured_accuracy = colourise_accuracy(mean_acc)

            if valid_flag == 1:
                saw_in_progress = True
                lines_to_display = [
                    f"{Colour.WARNING}Survey-in in progress{Colour.ENDC}: {Colour.BOLD}Elapsed{Colour.ENDC}: {elapsed_time} seconds, {Colour.BOLD}Remaining{Colour.ENDC}: {remaining_time} seconds, {Colour.BOLD}Target{Colour.ENDC}: {target_duration} seconds, {Colour.BOLD}Observations{Colour.ENDC}: {obs_count}, {Colour.BOLD}Accuracy{Colour.ENDC}: {coloured_accuracy}",
                    f"{Colour.HEADER}{Colour.BOLD}ECEF{Colour.ENDC}: {colourised_ecef}",
                    f"{Colour.HEADER}{Colour.BOLD}Geodetic{Colour.ENDC}: {colourised_geo}"
                ]
                redraw_terminal(lines_to_display, reset_cursor)
                reset_cursor = True
            elif valid_flag == 2:
                if not saw_in_progress:
                    if not warned_stale_completion:
                        print(
                            f"{Colour.WARNING}Receiver still reports a completed survey immediately after the start command. "
                            f"Waiting for a fresh in-progress status instead of treating this as a new completion.{Colour.ENDC}"
                        )
                        warned_stale_completion = True
                    continue

                result = {
                    'completed_at': datetime.now(timezone.utc).isoformat(),
                    'connection_type': connection.connection_type,
                    'endpoint': connection.endpoint,
                    'speed': connection.speed,
                    'valid_flag': valid_flag,
                    'observations': obs_count,
                    'cfg_duration': target_duration,
                    'mean_accuracy_metres': mean_acc,
                    'ecef': {
                        'x': mean_x,
                        'y': mean_y,
                        'z': mean_z,
                    },
                    'geodetic': {
                        'latitude': lat,
                        'longitude': lon,
                        'altitude': alt,
                    },
                }
                write_survey_result(output_file, result)

                print(Colour.OKGREEN + "Survey-in complete." + Colour.ENDC)
                print(f"Final {Colour.BOLD}Accuracy{Colour.ENDC}: {coloured_accuracy}")
                print(f"Final {Colour.BOLD}ECEF{Colour.ENDC}: {current_ecef}")
                print(f"Final {Colour.BOLD}Geodetic{Colour.ENDC}: {prev_latlon}")
                print(f"Saved survey result to {output_file}")
                break
            time.sleep(1)


def parse_svin_status(response: str):
    if not response.startswith('$PQTMSVINSTATUS,'):
        return None

    sentence = response.split('*', 1)[0]
    fields = sentence.split(',')
    if len(fields) < 11:
        return None

    try:
        valid_flag = int(fields[3])
        res1 = int(fields[5])
        obs_count = int(fields[6])
        target_duration = int(fields[7])
        mean_x = float(fields[8])
        mean_y = float(fields[9])
        mean_z = float(fields[10])
        mean_acc = float(fields[11])
    except (IndexError, ValueError):
        return None

    return {
        'valid_flag': valid_flag,
        'res1': res1,
        'obs_count': obs_count,
        'target_duration': target_duration,
        'mean_x': mean_x,
        'mean_y': mean_y,
        'mean_z': mean_z,
        'mean_acc': mean_acc,
    }


def build_connection_config(args: argparse.Namespace) -> ConnectionConfig:
    if args.tcp:
        return ConnectionConfig('tcp', args.tcp)
    if args.port:
        return ConnectionConfig('serial', args.port, args.speed)
    raise ValueError("You must provide either a serial port or --tcp HOST:PORT")

def calculate_nmea_checksum(nmea_sentence: str) -> str:
    checksum = 0
    for char in nmea_sentence[1:]:
        checksum ^= ord(char)
    return f"{nmea_sentence}*{checksum:02X}"

def append_checksum_if_missing(nmea_sentence: str) -> str:
    if '*' not in nmea_sentence:
        return calculate_nmea_checksum(nmea_sentence)
    return nmea_sentence

def read_gps_messages(connection: ConnectionConfig, timeout: int, verbose: bool = False):
    try:
        with open_connection(connection, timeout) as gps_connection:
            yield from stream_gps_messages(gps_connection, verbose)
    except (ConnectionError, ValueError) as exc:
        print(exc)


def stream_gps_messages(gps_connection, verbose: bool = False):
    previous_context = None
    print_next_svin_context = False

    while True:
        raw_response = read_raw_line(gps_connection)
        if not raw_response:
            continue

        response = raw_response.decode('ascii', errors='ignore').strip()
        current_context = format_serial_context(raw_response)

        if response and response.startswith('$'):
            if print_next_svin_context:
                print(
                    f"{Colour.HEADER}Serial context after $PQTMSVINSTATUS:{Colour.ENDC} "
                    f"{Colour.OKBLUE}{current_context}{Colour.ENDC}"
                )
                print_next_svin_context = False

            if verbose:
                print(f"{Colour.OKBLUE}Received message: {response}{Colour.ENDC}")

            if '$PQTMSVINSTATUS' in response:
                previous_text = previous_context or '<no previous serial line>'
                print(
                    f"{Colour.HEADER}Serial context before $PQTMSVINSTATUS:{Colour.ENDC} "
                    f"{Colour.OKBLUE}{previous_text}{Colour.ENDC}"
                )
                print(
                    f"{Colour.HEADER}Matched $PQTMSVINSTATUS:{Colour.ENDC} "
                    f"{Colour.OKBLUE}{response}{Colour.ENDC}"
                )
                print_next_svin_context = True

            previous_context = current_context
            yield response
            continue

        previous_context = current_context

def ecef_to_geodetic(x: float, y: float, z: float) -> tuple:
    transformer = Transformer.from_crs("EPSG:4978", "EPSG:4326", always_xy=True)
    lon, lat, alt = transformer.transform(x, y, z)
    return lat, lon, alt

def colour_diff(current: str, previous: str) -> str:
    """
    Compare digits in the current and previous strings. Mark changed digits in red.
    """
    result = []
    change_detected = False
    for c, p in zip(current, previous):
        if c.isdigit() and p.isdigit():
            if change_detected or c != p:
                result.append(f"{Colour.RED}{c}{Colour.ENDC}")
                change_detected = True
            else:
                result.append(f"{Colour.GREEN}{c}{Colour.ENDC}")
        else:
            result.append(c)
            change_detected = False
    if len(current) > len(previous):
        for c in current[len(previous):]:
            result.append(f"{Colour.RED}{c}{Colour.ENDC}" if c.isdigit() else c)
    return ''.join(result)

def redraw_terminal(lines: list, reset_cursor: bool):
    """
    Redraw lines in the terminal. If reset_cursor is True, reset cursor position for updates.
    If False, simply print lines without resetting the cursor.
    """
    if sys.stdout.isatty():
        if reset_cursor:
            sys.stdout.write('\033[F' * len(lines))  # Reset the cursor for updates
        for line in lines:
            sys.stdout.write('\033[K' + line + '\n')  # Print each line

def colourise_accuracy(accuracy: float) -> str:
    """Colour accuracy based on its value."""
    if accuracy > ACCURACY_THRESHOLD_1M:
        return f"{Colour.RED}{accuracy:.2f}{Colour.ENDC} metres"
    elif accuracy >= ACCURACY_THRESHOLD_10CM:
        return f"{Colour.YELLOW}{accuracy:.2f}{Colour.ENDC} metres"
    else:
        return f"{Colour.GREEN}{accuracy:.2f}{Colour.ENDC} metres"

def start_survey_in(connection: ConnectionConfig, timeout: int, min_dur: int, acc_limit: float, output_file: str, verbose: bool):
    try:
        with open_connection(connection, timeout) as gps_connection:
            queue_quectel_survey_commands(gps_connection, min_dur, acc_limit, verbose)
            wait_for_receiver_reset(gps_connection, timeout, verbose)

        if connection.connection_type == 'tcp':
            time.sleep(1)

        monitor_survey_status(connection, timeout, min_dur, output_file, verbose)
    except (ConnectionError, ValueError) as exc:
        print(exc)

def send_nmea_command(connection: ConnectionConfig, nmea_command: str, timeout: int, verbose: bool = False) -> str:
    try:
        with open_connection(connection, timeout) as gps_connection:
            nmea_command_with_checksum = append_checksum_if_missing(nmea_command)
            write_line(gps_connection, nmea_command_with_checksum)
            if verbose:
                print(f"Sent command: {Colour.OKBLUE}{nmea_command_with_checksum}{Colour.ENDC}")

            command_name = nmea_command_with_checksum.split(',', 1)[0].lstrip('$')
            response = wait_for_command_response(
                gps_connection,
                timeout,
                [command_name, 'OK', 'ERROR'],
                verbose,
            )
            if response:
                return response
    except (ConnectionError, ValueError) as exc:
        print(exc)

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

def disable_survey_in(connection: ConnectionConfig, timeout: int, verbose: bool = False):
    command = "$PQTMCFGSVIN,W,0,0,0.0,0.0,0.0,0.0"
    send_nmea_command(connection, command, timeout, verbose)
    print(Colour.OKGREEN + "Survey-in disabled." + Colour.ENDC)

def set_fixed_mode(connection: ConnectionConfig, ecef_x: float, ecef_y: float, ecef_z: float, timeout: int, verbose: bool = False):
    fixed_command = f"$PQTMCFGSVIN,W,2,0,0.0,{ecef_x},{ecef_y},{ecef_z}"
    try:
        with open_connection(connection, timeout) as gps_connection:
            write_command(gps_connection, "$PQTMCFGRCVRMODE,W,2", verbose)
            time.sleep(0.2)

            write_command(gps_connection, fixed_command, verbose)
            response = wait_for_command_response(
                gps_connection,
                timeout,
                ['PQTMCFGSVIN', 'OK', 'ERROR'],
                verbose,
            )

            write_command(gps_connection, "$PQTMSAVEPAR", verbose)
            time.sleep(0.2)
            write_command(gps_connection, "$PQTMSRR", verbose)

        if connection.connection_type == 'tcp':
            time.sleep(1)

        if response and 'ERROR' not in response:
            print(Colour.OKGREEN + "Fixed mode set successfully." + Colour.ENDC)
            return

        print(
            Colour.WARNING
            + "Fixed mode commands were sent, but no explicit success acknowledgement was received."
            + Colour.ENDC
        )
    except (ConnectionError, ValueError) as exc:
        print(Colour.FAIL + f"Failed to set fixed mode: {exc}" + Colour.ENDC)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Survey-in and Fixed mode tool for Quectel LC29H-BS GPS module.")
    parser.add_argument('port', nargs='?', type=str, help='Serial port to use (e.g., /dev/ttyUSB0 or COM3)')
    parser.add_argument('--tcp', type=str, help='TCP endpoint to use instead of a serial port (format: HOST:PORT)')
    parser.add_argument('--timeout', type=int, default=3, help='Timeout in seconds for GPS response (default: 3 seconds)')
    parser.add_argument('--speed', type=int, help='Baud rate for serial connections. If not provided, the script will attempt to detect the speed.')
    parser.add_argument('--mode', type=str, choices=['survey', 'fixed', 'disable'], required=True, help="Select mode: 'survey', 'fixed', or 'disable'")
    parser.add_argument('--ecef', nargs=3, type=float, help="ECEF coordinates (X Y Z) for fixed mode")
    parser.add_argument('--min-dur', type=int, default=86400, help="Minimum duration for survey-in mode (default: 86400 seconds / 1 day)")
    parser.add_argument('--acc-limit', type=float, default=15.0, help="Accuracy limit for survey-in mode in metres (default: 15 metres)")
    parser.add_argument('--output-file', type=str, default='survey_result.json', help='Write completed survey results to this JSON file (default: survey_result.json)')
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

    if args.mode == 'disable':
        disable_survey_in(connection, args.timeout, args.verbose)
    elif args.mode == 'survey':
        start_survey_in(connection, args.timeout, args.min_dur, args.acc_limit, args.output_file, args.verbose)
    elif args.mode == 'fixed':
        if not args.ecef:
            print(Colour.FAIL + "Error: You must provide ECEF coordinates for fixed mode." + Colour.ENDC)
            exit(1)
        ecef_x, ecef_y, ecef_z = args.ecef
        set_fixed_mode(connection, ecef_x, ecef_y, ecef_z, args.timeout, args.verbose)

