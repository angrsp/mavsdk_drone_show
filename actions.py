#!/usr/bin/env python3
"""
===============================================================
Drone Action Executor with MAVSDK - Multi-Parameter Setting
---------------------------------------------------------------
Usage Examples:
---------------
1) Take off with altitude 15 and set multiple PX4 parameters:
   python3 actions.py --action takeoff --altitude 15 \
       --param MAV_SYS_ID 4 \
       --param MPC_XY_CRUISE 8

2) Land without setting any parameters:
   python3 actions.py --action land

3) Update code from a specific branch (e.g., "new_feature_branch"):
   python3 actions.py --action update_code --branch new_feature_branch

4) Set only parameters (no flight action). If you want to just set parameters,
   you can still pick an action (like "hold") or any other valid action to
   ensure the script runs, but supply all your desired parameters:
   python3 actions.py --action hold \
       --param MAV_SYS_ID 6 \
       --param MPC_XY_VEL_MAX 10 \
       --param MIS_TAKEOFF_ALT 5

5) Initialize the system ID automatically from the detected hardware ID
   and reboot the flight controller:
   python3 actions.py --action init_sysid

6) Apply common parameters from 'common_params.csv' in the project root folder:
   python3 actions.py --action apply_common_params
   (Optionally add --reboot_after to reboot the flight controller right after)

Description:
------------
This script executes various drone actions using MAVSDK:
 - takeoff, land, hold, test, reboot, kill_terminate, update_code,
   return_rtl, init_sysid, apply_common_params, etc.
 - Safely manages MAVSDK server launch/teardown.
 - Provides logging, exit codes, LED status feedback, and robust error handling.
 - Supports setting multiple PX4 parameters in a single run via repeated --param.
 - Supports automatically setting MAV_SYS_ID based on a local .hwID file with 'init_sysid'.
 - Now supports applying a shared set of parameters stored in a 'common_params.csv' file
   via the 'apply_common_params' action.

---------------------------------------------------------------
"""

import argparse
import asyncio
import csv
import glob
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time

import psutil
from mavsdk import System, telemetry, action
from mavsdk.action import ActionError
from src.led_controller import LEDController
from src.params import Params

# Return codes: 0 = success, 1 = failure
RETURN_CODE = 0

GRPC_PORT = Params.DEFAULT_GRPC_PORT
UDP_PORT = Params.mavsdk_port
HW_ID = None

# Configure logging
logs_directory = os.path.join(Params.LOG_DIRECTORY_PATH, "action_logs")
os.makedirs(logs_directory, exist_ok=True)

logger = logging.getLogger("action_logger")
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s: %(message)s', '%Y-%m-%d %H:%M:%S')

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

log_file = os.path.join(logs_directory, "actions.log")
file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

# -----------------------
# Helper / Setup Functions
# -----------------------

def fail():
    """
    Sets the return code to 1 indicating failure.
    """
    global RETURN_CODE
    RETURN_CODE = 1

def check_mavsdk_server_running(port):
    """
    Checks if a mavsdk_server process is already running on the specified port.
    Returns (bool, pid).
    """
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            for conn in proc.net_connections(kind='inet'):
                if conn.laddr.port == port:
                    return True, proc.info['pid']
        except Exception:
            pass
    return False, None

def wait_for_port(port, host='localhost', timeout=10.0):
    """
    Waits until a port on the specified host is open, or until timeout is reached.
    Returns True if open, False otherwise.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.2)
    return False

async def log_mavsdk_output(mavsdk_server):
    """
    Asynchronously reads MAVSDK server's stdout/stderr for logging.
    """
    loop = asyncio.get_event_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, mavsdk_server.stdout.readline)
            if not line:
                break
            logger.debug(f"MAVSDK Server: {line.decode().strip()}")
    except Exception:
        logger.exception("Error reading MAVSDK server stdout")

    try:
        while True:
            line = await loop.run_in_executor(None, mavsdk_server.stderr.readline)
            if not line:
                break
            logger.error(f"MAVSDK Server Error: {line.decode().strip()}")
    except Exception:
        logger.exception("Error reading MAVSDK server stderr")

def read_hw_id():
    """
    Attempts to read the first *.hwID file in the current directory
    and parse it as an integer hardware ID.
    """
    hwid_files = glob.glob('*.hwID')
    if hwid_files:
        filename = hwid_files[0]
        hw_id_str = os.path.splitext(filename)[0]
        logger.info(f"Hardware ID file detected: {filename}")
        try:
            hw_id = int(hw_id_str)
            logger.info(f"Hardware ID {hw_id} detected.")
            return hw_id
        except ValueError:
            logger.error(f"Invalid hardware ID format in {filename}. Expected an integer.")
            return None
    else:
        logger.warning("No .hwID file found.")
        return None

def read_config(filename=Params.config_csv_name):
    """
    Reads the drone configuration from a CSV file matching the HW_ID.
    Returns a dictionary with drone_config or None if not found/failed.
    """
    global HW_ID
    logger.info("Reading drone configuration...")
    try:
        with open(filename, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                hw_id = int(row.get('hw_id', -1))
                if hw_id == HW_ID:
                    drone_config = {
                        'hw_id': hw_id,
                        'pos_id': int(row.get('pos_id', -1)),
                        'x': float(row.get('x', 0.0)),
                        'y': float(row.get('y', 0.0)),
                        'ip': row.get('ip', ''),
                        'udp_port': int(row.get('mavlink_port', UDP_PORT)),
                        'grpc_port': int(row.get('debug_port', GRPC_PORT)),
                        'gcs_ip': row.get('gcs_ip', ''),
                    }
                    logger.info(f"Drone configuration: {drone_config}")
                    return drone_config
        logger.warning(f"No matching HW_ID {HW_ID} found in config file.")
    except FileNotFoundError:
        logger.error(f"Config file '{filename}' not found.")
    except Exception:
        logger.exception("Error reading config file")
    return None

def stop_mavsdk_server(mavsdk_server):
    """
    Gracefully stops the MAVSDK server if it's still running.
    """
    if mavsdk_server and mavsdk_server.poll() is None:
        logger.info("Stopping MAVSDK server...")
        mavsdk_server.terminate()
        try:
            mavsdk_server.wait(timeout=5)
            logger.info("MAVSDK server terminated gracefully.")
        except subprocess.TimeoutExpired:
            logger.warning("MAVSDK server did not terminate. Killing it.")
            mavsdk_server.kill()
            mavsdk_server.wait()
            logger.info("MAVSDK server killed.")
    else:
        logger.debug("MAVSDK server already stopped or never started.")

def find_mavsdk_server():
    """
    Finds the path to the mavsdk_server binary.
    Priority:
    1. MAVSDK_SERVER_PATH environment variable.
    2. Current script directory (relative to __file__).
    3. Default fallback directory: project root.
    """
    # 1. Check environment variable
    mavsdk_server_path = os.environ.get("MAVSDK_SERVER_PATH")
    if mavsdk_server_path and os.path.isfile(mavsdk_server_path):
        return mavsdk_server_path

    # 2. Check script directory (relative to __file__)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    mavsdk_server_path = os.path.join(script_dir, "mavsdk_server")
    if os.path.isfile(mavsdk_server_path):
        return mavsdk_server_path

    # 3. Check fallback directory (project root)
    fallback_path = os.path.join(script_dir, "..", "mavsdk_server")
    if os.path.isfile(fallback_path):
        return fallback_path

    return None

def start_mavsdk_server(grpc_port, udp_port):
    """
    Starts or restarts the MAVSDK server, ensuring any previously running server
    on the same gRPC port is stopped first. Returns the subprocess.Popen instance.
    """
    is_running, pid = check_mavsdk_server_running(grpc_port)
    if is_running:
        logger.info(f"MAVSDK server already running on port {grpc_port}, terminating it.")
        try:
            psutil.Process(pid).terminate()
            psutil.Process(pid).wait(timeout=5)
            logger.info(f"Terminated existing MAVSDK server (PID: {pid}).")
        except psutil.NoSuchProcess:
            logger.warning(f"No process found with PID {pid}.")
        except psutil.TimeoutExpired:
            logger.warning(f"Process {pid} did not terminate, killing it.")
            psutil.Process(pid).kill()
            psutil.Process(pid).wait()
            logger.info(f"Killed MAVSDK server (PID: {pid}).")

    mavsdk_server_path = find_mavsdk_server()
    if not mavsdk_server_path:
        logger.error("mavsdk_server executable not found.")
        fail()
        sys.exit(1)

    logger.info(f"Starting MAVSDK server: {mavsdk_server_path} on gRPC:{grpc_port}, UDP:{udp_port}")
    try:
        mavsdk_server = subprocess.Popen(
            [mavsdk_server_path, "-p", str(grpc_port), f"udp://:{udp_port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        asyncio.create_task(log_mavsdk_output(mavsdk_server))

        if not wait_for_port(grpc_port, timeout=10):
            logger.error("MAVSDK server did not start listening in time.")
            mavsdk_server.terminate()
            fail()
            sys.exit(1)

        logger.info("MAVSDK server ready.")
        return mavsdk_server
    except Exception:
        logger.exception("Failed to start MAVSDK server")
        fail()
        sys.exit(1)


# -----------------------
# Core Action Execution
# -----------------------

async def perform_action(action, altitude=None, parameters=None, branch=None, reboot_after=False):
    """
    Main entry to perform the requested action with optional altitude/parameters/branch, plus
    an optional reboot_after boolean for certain actions like apply_common_params.
    """
    logger.info(f"Requested action: {action}, altitude: {altitude}, parameters: {parameters}, "
                f"branch: {branch}, reboot_after: {reboot_after}")
    global HW_ID

    # Special case: code update
    if action == "update_code":
        await update_code(branch)
        return

    # For init_sysid, we do need a valid HW_ID. That is checked later in init_sysid logic.
    # For apply_common_params or normal flight actions, we also read HW_ID for consistency.
    HW_ID = read_hw_id()

    if action not in ["init_sysid", "update_code"]:
        # For normal flight actions (and apply_common_params), we also read config
        if HW_ID is None:
            logger.error("No valid HW_ID found, cannot proceed.")
            fail()
            return

        drone_config = read_config()
        if not drone_config:
            logger.error("Drone config not found, cannot proceed.")
            fail()
            return

    # Start MAVSDK if not just "update_code" (that doesn't need flight connect).
    grpc_port = GRPC_PORT
    udp_port = UDP_PORT
    logger.info(f"MAVSDK: gRPC Port: {grpc_port}, UDP Port: {udp_port}")

    mavsdk_server = start_mavsdk_server(grpc_port, udp_port)
    if not mavsdk_server:
        logger.error("Failed to start MAVSDK server.")
        fail()
        return

    drone = System(mavsdk_server_address="localhost", port=grpc_port)
    logger.info("Connecting to drone...")
    try:
        await drone.connect(system_address=f"udp://:{udp_port}")
    except Exception:
        logger.exception("Failed to connect to MAVSDK server")
        fail()
        stop_mavsdk_server(mavsdk_server)
        return

    # Wait for connection
    if not await wait_for_drone_connection(drone):
        logger.error("Drone not connected in time.")
        fail()
        stop_mavsdk_server(mavsdk_server)
        return

    # Set parameters if provided via CLI
    if parameters:
        await set_parameters(drone, parameters)

    # Execute the requested action safely
    try:
        if action == "takeoff":
            if not await safe_action(takeoff, drone, altitude):
                fail()
        elif action == "land":
            if not await safe_action(land, drone):
                fail()
        elif action == "return_rtl":
            if not await safe_action(return_rtl, drone):
                fail()
        elif action == "hold":
            if not await safe_action(hold, drone):
                fail()
        elif action == "kill_terminate":
            if not await safe_action(kill_terminate, drone):
                fail()
        elif action == "test":
            if not await safe_action(test, drone):
                fail()
        elif action == "reboot_fc":
            if not await safe_action(reboot, drone, fc_flag=True, sys_flag=False):
                fail()
        elif action == "reboot_sys":
            if not await safe_action(reboot, drone, fc_flag=False, sys_flag=True):
                fail()
        elif action == "init_sysid":
            # automatically set MAV_SYS_ID from HW_ID, then reboot FC
            if not await safe_action(init_sysid, drone):
                fail()
        elif action == "apply_common_params":
            if not await safe_action(apply_common_params, drone, reboot_after):
                fail()
        else:
            logger.error(f"Invalid action specified: {action}")
            fail()
    except Exception:
        logger.exception(f"Error performing action '{action}'")
        fail()
    finally:
        stop_mavsdk_server(mavsdk_server)
        logger.info("Action completed.")

async def wait_for_drone_connection(drone, timeout=10):
    """
    Waits up to 'timeout' seconds for drone connection.
    Returns True if connected, else False.
    """
    logger.info("Waiting for drone connection state...")
    start = time.time()
    async for state in drone.core.connection_state():
        if state.is_connected:
            logger.info("Drone connected successfully.")
            return True
        if time.time() - start > timeout:
            return False
        await asyncio.sleep(0.5)

async def safe_action(func, *args, **kwargs):
    """
    Wraps an action function with exception handling.
    Logs start/end, returns True if success, False if failure.
    """
    action_name = func.__name__
    logger.info(f"Starting action: {action_name}")
    try:
        await func(*args, **kwargs)
        logger.info(f"Action {action_name} completed successfully.")
        return True
    except ActionError as ae:
        logger.error(f"Action {action_name} failed with ActionError: {ae}")
        return False
    except Exception:
        logger.exception(f"Action {action_name} failed with an unexpected error.")
        return False

# Mapping of parameter names to their expected types
PARAM_TYPES = {
    "COM_RCL_EXCEPT": "int",
    "GF_ACTION": "int",
    "GF_MAX_HOR_DIST": "float",
    "GF_MAX_VER_DIST": "float",
}

def parse_param_value(raw_value, param_name):
    """
    Parses the raw parameter value string into the correct type based on the
    expected type for the parameter as defined in PARAM_TYPES.
    """
    expected_type = PARAM_TYPES.get(param_name)
    try:
        if expected_type == "int":
            return int(raw_value), "int"
        elif expected_type == "float":
            return float(raw_value), "float"
        else:
            # Fallback: if no mapping exists, try guessing based on a decimal point.
            if '.' in raw_value:
                return float(raw_value), "float"
            else:
                return int(raw_value), "int"
    except ValueError as e:
        logger.error(f"Failed to parse value '{raw_value}' for parameter '{param_name}' with expected type '{expected_type}'")
        raise e

async def set_parameters(drone, parameters):
    """
    Sets multiple parameters on the drone using MAVSDK's param interface.
    The `parameters` dict should be {param_name: param_value_str}.
    """
    for param_name, raw_value in parameters.items():
        try:
            param_value, param_type = parse_param_value(raw_value, param_name)
            logger.info(f"Setting param '{param_name}' to {param_value} (type: {param_type})")
            if param_type == "int":
                await drone.param.set_param_int(param_name, param_value)
            elif param_type == "float":
                await drone.param.set_param_float(param_name, param_value)
            else:
                raise ValueError(f"Unsupported parameter type for {param_name}")
            logger.info(f"Param '{param_name}' set successfully.")
        except Exception as e:
            logger.exception(f"Failed to set param '{param_name}': {e}")
            fail()  # Assuming fail() handles the error as per your project's conventions

async def apply_common_params(drone, reboot_after=False):
    """
    Reads a 'common_params.csv' file from the project root, applies each parameter to
    the drone, and optionally reboots the flight controller.
    
    The expected CSV format is:
      param_name,param_value
    Example:
      COM_RCL_EXCEPT,7
      GF_ACTION,3
      GF_MAX_HOR_DIST,3000
      GF_MAX_VER_DIST,120
    """
    led_controller = LEDController.get_instance()
    common_file = 'common_params.csv'

    # Indicate start with a distinct LED color (e.g., magenta)
    led_controller.set_color(255, 0, 255)
    await asyncio.sleep(0.5)

    if not os.path.isfile(common_file):
        logger.error(f"Common parameter file '{common_file}' not found.")
        fail()
        return

    logger.info(f"Loading common parameters from {common_file} ...")
    try:
        common_params = {}
        with open(common_file, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                param_name = row['param_name'].strip()
                param_value = row['param_value'].strip()
                common_params[param_name] = param_value

        logger.info(f"Found {len(common_params)} common parameters. Applying now...")
        await set_parameters(drone, common_params)

        # Blink green a few times for success feedback
        for _ in range(3):
            led_controller.set_color(0, 255, 0)
            await asyncio.sleep(0.2)
            led_controller.turn_off()
            await asyncio.sleep(0.2)

        if reboot_after:
            logger.info("Rebooting flight controller as requested...")
            await drone.action.reboot()
            led_controller.set_color(255, 255, 0)
            await asyncio.sleep(1.0)

        logger.info("apply_common_params action completed successfully.")
    except Exception:
        logger.exception("Error applying common parameters")
        fail()
    finally:
        led_controller.turn_off()

# -----------------------
# Action Implementations
# -----------------------

async def ensure_ready_for_flight(drone):
    """
    Before takeoff, ensure the drone is healthy, global position is good,
    and home position is set.
    """
    logger.info("Checking preflight conditions...")
    start = time.time()
    gps_ok = False
    home_ok = False
    async for health in drone.telemetry.health():
        if health.is_global_position_ok:
            gps_ok = True
        if health.is_home_position_ok:
            home_ok = True
        if gps_ok and home_ok:
            logger.info("Preflight checks passed: GPS and Home position are good.")
            return True
        if time.time() - start > 15:
            logger.error("Preflight checks timed out. GPS or Home not ready.")
            return False
        await asyncio.sleep(1)

async def takeoff(drone, altitude):
    """
    Arms and takes off to the specified altitude (in meters).
    """
    led_controller = LEDController.get_instance()
    # Check preflight conditions
    if not await ensure_ready_for_flight(drone):
        raise Exception("Preflight conditions not met (GPS/Home)")

    # Try arming
    try:
        led_controller.set_color(255, 255, 0)  # Yellow: starting
        await asyncio.sleep(0.5)
        await drone.action.set_takeoff_altitude(float(altitude))
        await drone.action.arm()
        led_controller.set_color(255, 255, 255)  # White: armed
        await asyncio.sleep(0.5)
        await drone.action.takeoff()
    except ActionError as e:
        logger.error(f"Failed to take off: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during takeoff")
        raise

    # Indicate success with green blinks
    for _ in range(3):
        led_controller.set_color(0, 255, 0)
        await asyncio.sleep(0.2)
        led_controller.turn_off()
        await asyncio.sleep(0.2)
    led_controller.turn_off()
    logger.info("Takeoff successful.")

async def land(drone):
    """
    Commands the drone to land safely.
    """
    led_controller = LEDController.get_instance()
    led_controller.set_color(255, 255, 0)  # Yellow
    await asyncio.sleep(0.5)

    try:
        await drone.action.hold()
        await asyncio.sleep(1)

        # Indicate landing in progress (blue pulses)
        for _ in range(3):
            led_controller.set_color(0, 0, 255)
            await asyncio.sleep(0.5)
            led_controller.turn_off()
            await asyncio.sleep(0.5)

        await drone.action.land()

        for _ in range(3):
            led_controller.set_color(0, 255, 0)
            await asyncio.sleep(0.2)
            led_controller.turn_off()
            await asyncio.sleep(0.2)
        led_controller.turn_off()
        logger.info("Landing successful.")
    except ActionError as e:
        logger.error(f"Landing failed: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during landing")
        raise

async def return_rtl(drone):
    """
    Commands the drone to return to launch (home) position.
    """
    led_controller = LEDController.get_instance()
    led_controller.set_color(255, 0, 255)  # Purple start
    await asyncio.sleep(0.5)

    try:
        await drone.action.hold()
        await asyncio.sleep(1)

        for _ in range(3):
            led_controller.set_color(0, 0, 255)
            await asyncio.sleep(0.5)
            led_controller.turn_off()
            await asyncio.sleep(0.5)

        await drone.action.return_to_launch()

        for _ in range(3):
            led_controller.set_color(0, 255, 0)
            await asyncio.sleep(0.2)
            led_controller.turn_off()
            await asyncio.sleep(0.2)
        led_controller.turn_off()
        logger.info("RTL successful.")
    except ActionError as e:
        logger.error(f"RTL failed: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during RTL")
        raise

async def kill_terminate(drone):
    """
    Immediately terminates the drone (emergency kill).
    """
    led_controller = LEDController.get_instance()
    led_controller.set_color(255, 0, 0)
    await asyncio.sleep(0.2)
    led_controller.set_color(0, 0, 0)
    led_controller.set_color(255, 0, 0)
    await asyncio.sleep(0.2)

    try:
        await drone.action.terminate()
        await asyncio.sleep(1)
        for _ in range(3):
            led_controller.set_color(0, 255, 0)
            await asyncio.sleep(0.2)
            led_controller.turn_off()
            await asyncio.sleep(0.2)
        led_controller.turn_off()
        await asyncio.sleep(0.2)
        led_controller.set_color(255, 0, 0)
        logger.info("Kill and Terminate successful.")
    except ActionError as e:
        logger.error(f"Kill terminate failed: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during kill terminate")
        raise

async def hold(drone):
    """
    Commands the drone to hold (loiter) at current position.
    """
    led_controller = LEDController.get_instance()
    led_controller.set_color(0, 0, 255)
    await asyncio.sleep(0.5)
    try:
        await drone.action.hold()
        led_controller.set_color(0, 0, 255)
        await asyncio.sleep(1)
        led_controller.turn_off()
        logger.info("Hold successful.")
    except ActionError as e:
        logger.error(f"Hold failed: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during hold")
        raise

async def test(drone):
    """
    A simple test action to verify connectivity and LED control.
    """
    led_controller = LEDController.get_instance()
    try:
        led_controller.set_color(255, 0, 0)
        await asyncio.sleep(1)
        await drone.action.arm()
        led_controller.set_color(255, 255, white)
        await asyncio.sleep(1)
        led_controller.set_color(0, 0, 255)
        await asyncio.sleep(1)
        led_controller.set_color(0, 255, 0)
        await asyncio.sleep(1)
        await drone.action.disarm()
        led_controller.turn_off()
        logger.info("Test action successful.")
    except ActionError as e:
        logger.error(f"Test action failed: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during test")
        raise

async def reboot(drone, fc_flag, sys_flag, force_reboot=True):
    """
    Reboots flight controller or entire system (Linux-based), or both.
    """
    led_controller = LEDController.get_instance()
    led_controller.set_color(255, 255, 0)
    await asyncio.sleep(0.5)

    try:
        if fc_flag:
            await drone.action.reboot()
            for _ in range(3):
                led_controller.set_color(0, 255, 0)
                await asyncio.sleep(0.2)
                led_controller.turn_off()
                await asyncio.sleep(0.2)
            logger.info("FC reboot successful.")

        if sys_flag:
            logger.info("Initiating system reboot...")
            led_controller.turn_off()
            await reboot_system()

        led_controller.turn_off()
    except ActionError as e:
        logger.error(f"Reboot failed: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during reboot")
        raise

async def reboot_system():
    """
    Reboots the entire system via D-Bus (for Linux-based OS).
    """
    process = await asyncio.create_subprocess_exec(
        'dbus-send', '--system', '--print-reply', '--dest=org.freedesktop.login1',
        '/org/freedesktop/login1', 'org.freedesktop.login1.Manager.Reboot', 'boolean:true',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"System reboot via D-Bus failed: {stderr.decode().strip()}")
    else:
        logger.info("System reboot command executed successfully.")

async def update_code(branch=None):
    """
    Pulls latest code from a git repository (via tools/update_repo_ssh.sh).
    Optionally checks out a specific branch.
    """
    global RETURN_CODE
    led_controller = LEDController.get_instance()
    led_controller.set_color(255, 255, 0)
    await asyncio.sleep(0.5)

    try:
        script_path = os.path.join('tools', 'update_repo_ssh.sh')
        command = [script_path]
        if branch:
            command.append(branch)
        logger.info(f"Executing update script: {' '.join(command)}")

        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"Update script failed: {stderr.decode().strip()}")
            fail()
            for _ in range(3):
                led_controller.set_color(255, 0, 0)
                await asyncio.sleep(0.2)
                led_controller.turn_off()
                await asyncio.sleep(0.2)
        else:
            logger.info(f"Update script successful: {stdout.decode().strip()}")
            for _ in range(3):
                led_controller.set_color(0, 255, 0)
                await asyncio.sleep(0.2)
    except Exception:
        logger.exception("Update code action failed")
        fail()
        for _ in range(3):
            led_controller.set_color(255, 0, 0)
            await asyncio.sleep(0.2)
            led_controller.turn_off()
            await asyncio.sleep(0.2)
    finally:
        led_controller.turn_off()

# -----------------------
# Action: init_sysid
# -----------------------

async def init_sysid(drone):
    """
    Automatically set MAV_SYS_ID based on the hardware ID file and
    then reboot the flight controller.
    """
    led_controller = LEDController.get_instance()

    # We rely on the global HW_ID already read in perform_action().
    global HW_ID

    if HW_ID is None:
        raise Exception("HW_ID not found or invalid. Cannot init system ID.")

    logger.info(f"Initializing system ID: MAV_SYS_ID = {HW_ID}")

    try:
        # Indicate start with yellow LED
        led_controller.set_color(255, 255, 0)
        await asyncio.sleep(0.5)

        # Set MAV_SYS_ID param
        await drone.param.set_param_int("MAV_SYS_ID", HW_ID)
        logger.info("MAV_SYS_ID parameter set successfully.")

        # Reboot FC to make the new system ID take effect
        led_controller.set_color(0, 255, 255)  # Cyan to indicate reboot in progress
        await asyncio.sleep(0.5)

        logger.info("Rebooting flight controller for system ID change...")
        await drone.action.reboot()

        # Blink green a few times for success
        for _ in range(3):
            led_controller.set_color(0, 255, 0)
            await asyncio.sleep(0.2)
            led_controller.turn_off()
            await asyncio.sleep(0.2)

        logger.info("init_sysid action completed successfully.")
    except ActionError as e:
        logger.error(f"init_sysid failed with ActionError: {e}")
        raise
    except Exception:
        logger.exception("Unexpected error during init_sysid")
        raise
    finally:
        led_controller.turn_off()

# -----------------------
# Main Entry Point
# -----------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Perform actions with drones.")
    parser.add_argument('--action',
                        help='Actions: takeoff, land, hold, test, reboot_fc, reboot_sys, update_code, '
                             'return_rtl, kill_terminate, init_sysid, apply_common_params')
    parser.add_argument('--altitude', type=float, default=10.0, help='Altitude (meters) for takeoff')
    parser.add_argument('--param', action='append', nargs=2, metavar=('param_name', 'param_value'),
                        help='Set one or more PX4 parameters, e.g.: --param MPC_XY_CRUISE 5.0 --param MAV_SYS_ID 4')
    parser.add_argument('--branch', type=str, help='Branch name for code update')
    parser.add_argument('--reboot_after', action='store_true',
                        help='If set, certain actions (e.g. apply_common_params) will reboot FC at the end')

    args = parser.parse_args()

    # Convert all param pairs into a dictionary { 'param_name': 'param_value_str', ... }
    parameters = {p[0]: p[1] for p in args.param} if args.param else None

    try:
        asyncio.run(
            perform_action(
                action=args.action,
                altitude=args.altitude,
                parameters=parameters,
                branch=args.branch,
                reboot_after=args.reboot_after
            )
        )
    except Exception:
        logger.exception("An unexpected error occurred in the main block.")
        fail()
    finally:
        logger.info("Operation completed.")
        sys.exit(RETURN_CODE)
