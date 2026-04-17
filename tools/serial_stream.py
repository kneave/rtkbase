
import argparse
import sys
import time

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Open a serial port and write the raw stream to stdout.'
    )
    parser.add_argument('port', help='Serial device path, for example /dev/ttyAMA0')
    parser.add_argument(
        '--speed',
        type=int,
        default=115200,
        help='Serial baud rate. Default: 115200',
    )
    parser.add_argument(
        '--timeout',
        type=float,
        default=1.0,
        help='Read timeout in seconds. Default: 1.0',
    )
    parser.add_argument(
        '--sleep',
        type=float,
        default=0.05,
        help='Idle sleep in seconds when no data is available. Default: 0.05',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        with serial.Serial(args.port, baudrate=args.speed, timeout=args.timeout) as connection:
            print(
                f'Opened {args.port} at {args.speed} baud. Press Ctrl+C to stop.',
                file=sys.stderr,
            )
            while True:
                chunk = connection.read(connection.in_waiting or 1)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    continue
                time.sleep(args.sleep)
    except serial.SerialException as exc:
        print(f'Error opening or reading serial port: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())