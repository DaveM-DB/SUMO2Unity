#!/usr/bin/env python3
"""macOS/Linux replacement for the bundled Windows Sumo2UnityTool.exe."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


VERSION = "Sumo2Unity v2.0.0"
DEFAULT_EGO_ID = "f_0.0"
DEFAULT_PUB_ENDPOINT = "tcp://*:5556"
DEFAULT_ROUTER_ENDPOINT = "tcp://*:5557"


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path.cwd()
    return Path(__file__).resolve().parents[1]


def infer_sumo_home() -> Path | None:
    env_home = os.environ.get("SUMO_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    sumo_bin = shutil.which("sumo") or shutil.which("sumo-gui")
    if sumo_bin:
        return Path(sumo_bin).resolve().parents[1]

    home_candidate = Path.home() / "Sumo" / "sumo"
    if (home_candidate / "tools" / "traci").is_dir():
        return home_candidate

    return None


def configure_sumo_imports() -> Path:
    sumo_home = infer_sumo_home()
    if not sumo_home:
        raise RuntimeError(
            "SUMO_HOME is not set and SUMO was not found on PATH. "
            "Install SUMO and export SUMO_HOME, or add SUMO's bin directory to PATH."
        )

    tools_dir = sumo_home / "tools"
    if not tools_dir.is_dir():
        raise RuntimeError(f"SUMO tools directory not found: {tools_dir}")

    tools_text = str(tools_dir)
    if tools_text not in sys.path:
        sys.path.insert(0, tools_text)

    os.environ.setdefault("SUMO_HOME", str(sumo_home))
    return sumo_home


def require_zmq():
    try:
        import zmq  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyzmq is not installed. Run `make macos-setup` before launching the tool."
        ) from exc
    return zmq


def resolve_sumo_binary(use_gui: bool, explicit_binary: str | None) -> str:
    if explicit_binary:
        resolved = shutil.which(explicit_binary) if os.sep not in explicit_binary else explicit_binary
        if resolved and Path(resolved).exists():
            return str(resolved)
        raise RuntimeError(f"SUMO binary not found: {explicit_binary}")

    name = "sumo-gui" if use_gui else "sumo"
    resolved = shutil.which(name)
    if resolved:
        return resolved

    sumo_home = infer_sumo_home()
    if sumo_home:
        candidate = sumo_home / "bin" / name
        if candidate.exists():
            return str(candidate)

    raise RuntimeError(f"Could not find `{name}`. Check SUMO_HOME or PATH.")


def split_sumo_file_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def resolve_existing_sumo_path(base_dir: Path, value: str) -> Path | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve() if candidate.exists() else None


def sumocfg_input_overrides(sumocfg: Path) -> list[str]:
    try:
        root = ET.parse(sumocfg).getroot()
    except ET.ParseError:
        logging.warning("Could not parse SUMO config XML; using it unchanged: %s", sumocfg)
        return []

    base_dir = sumocfg.parent
    input_node = root.find("input")
    if input_node is None:
        return []

    overrides: list[str] = []
    fallback_names = {
        "net-file": ["Sumo2Unity.net.xml", "net.net.xml"],
        "route-files": ["routes.rou.xml", "Sumo2Unity.rou.xml"],
    }

    for option_name, fallbacks in fallback_names.items():
        node = input_node.find(option_name)
        if node is None:
            continue

        value = node.attrib.get("value", "")
        if not value:
            continue

        resolved_values: list[str] = []
        missing_values: list[str] = []
        for item in split_sumo_file_list(value):
            existing = resolve_existing_sumo_path(base_dir, item)
            if existing:
                resolved_values.append(str(existing))
            else:
                missing_values.append(item)

        if not missing_values:
            continue

        for fallback in fallbacks:
            existing = resolve_existing_sumo_path(base_dir, fallback)
            if existing:
                logging.warning(
                    "SUMO config references missing %s value %s; using %s",
                    option_name,
                    ", ".join(missing_values),
                    existing,
                )
                resolved_values.append(str(existing))
                overrides.extend([f"--{option_name}", ",".join(resolved_values)])
                break

    return overrides


def parse_vehicle_payload(payload: dict[str, Any], ego_id: str) -> dict[str, Any] | None:
    vehicles = payload.get("vehicles")
    if not isinstance(vehicles, list):
        return None

    for vehicle in vehicles:
        if isinstance(vehicle, dict) and vehicle.get("vehicle_id") == ego_id:
            position = vehicle.get("position")
            if isinstance(position, list) and len(position) >= 2:
                return vehicle
    return None


def unity_vehicle_to_sumo_xy(vehicle: dict[str, Any]) -> tuple[float, float] | None:
    try:
        position = vehicle["position"]
        return float(position[0]), float(position[1])
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def distance_2d(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def receive_unity_loop(router: Any, out_queue: "queue.Queue[dict[str, Any]]", stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            parts = router.recv_multipart(flags=1)
        except Exception:
            time.sleep(0.002)
            continue

        if not parts:
            continue

        raw = parts[-1]
        try:
            decoded = raw.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logging.debug("Ignored malformed Unity payload")
            continue

        if isinstance(payload, dict):
            out_queue.put(payload)


def publish_json(pub: Any, payload: dict[str, Any]) -> None:
    pub.send_string(json.dumps(payload, separators=(",", ":")))


def try_sync_ego_to_sumo(traci: Any, ego_id: str, ego: dict[str, Any]) -> None:
    xy = unity_vehicle_to_sumo_xy(ego)
    if xy is None:
        return

    angle = float(ego.get("angle", 0.0))

    if ego_id in traci.vehicle.getIDList():
        try:
            traci.vehicle.moveToXY(ego_id, "", 0, xy[0], xy[1], angle=angle, keepRoute=2)
        except Exception:
            logging.debug("Could not move SUMO ego vehicle", exc_info=True)
        return

    try:
        road = traci.simulation.convertRoad(xy[0], xy[1], isGeo=False)
        edge_id = road[0]
        lane_position = float(road[1]) if len(road) > 1 else 0.0
        lane_index = int(road[2]) if len(road) > 2 else 0
        if not edge_id or edge_id.startswith(":"):
            return

        route_id = f"{ego_id}_route"
        if route_id not in traci.route.getIDList():
            traci.route.add(route_id, [edge_id])

        type_id = "EgoCar" if "EgoCar" in traci.vehicletype.getIDList() else ""
        traci.vehicle.add(
            ego_id,
            route_id,
            typeID=type_id,
            depart="now",
            departLane=str(lane_index),
            departPos=str(lane_position),
            departSpeed="0",
        )
        traci.vehicle.moveToXY(ego_id, edge_id, lane_index, xy[0], xy[1], angle=angle, keepRoute=2)
    except Exception:
        logging.debug("Could not create SUMO ego vehicle", exc_info=True)


def vehicle_position_3d(traci: Any, vehicle_id: str) -> tuple[float, float, float]:
    try:
        x, y, z = traci.vehicle.getPosition3D(vehicle_id)
        return float(x), float(y), float(z)
    except Exception:
        x, y = traci.vehicle.getPosition(vehicle_id)
        return float(x), float(y), 0.0


def collect_vehicle_data(
    traci: Any,
    ego_id: str,
    ego_xy: tuple[float, float] | None,
    subscribe_radius: float,
    last_positions: dict[str, tuple[float, float, float, float]],
    sim_time: float,
) -> list[dict[str, Any]]:
    vehicles: list[dict[str, Any]] = []

    for vehicle_id in traci.vehicle.getIDList():
        if vehicle_id == ego_id:
            continue

        x, y, z = vehicle_position_3d(traci, vehicle_id)
        if ego_xy is not None and distance_2d((x, y), ego_xy) > subscribe_radius:
            continue

        previous = last_positions.get(vehicle_id)
        vertical_speed = 0.0
        if previous:
            dt = max(sim_time - previous[3], 1e-9)
            vertical_speed = (z - previous[2]) / dt
        last_positions[vehicle_id] = (x, y, z, sim_time)

        try:
            lateral_speed = float(traci.vehicle.getLateralSpeed(vehicle_id))
        except Exception:
            lateral_speed = 0.0

        vehicles.append(
            {
                "vehicle_id": vehicle_id,
                "position": [round(x, 2), round(y, 2), round(z, 2)],
                "angle": round(float(traci.vehicle.getAngle(vehicle_id)), 2),
                "type": traci.vehicle.getTypeID(vehicle_id),
                "long_speed": round(float(traci.vehicle.getSpeed(vehicle_id)), 2),
                "vert_speed": round(vertical_speed, 2),
                "lat_speed": round(lateral_speed, 2),
            }
        )

    return vehicles


def collect_traffic_lights(traci: Any) -> list[dict[str, str]]:
    lights = []
    for junction_id in traci.trafficlight.getIDList():
        lights.append(
            {
                "junction_id": junction_id,
                "state": traci.trafficlight.getRedYellowGreenState(junction_id),
            }
        )
    return lights


def sleep_until(target: float) -> None:
    while True:
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.002))


def run_simulation(args: argparse.Namespace) -> int:
    configure_sumo_imports()
    zmq = require_zmq()

    try:
        import traci  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Could not import SUMO TraCI from SUMO_HOME/tools.") from exc

    sumocfg = Path(args.sumocfg).expanduser().resolve()
    if not sumocfg.exists():
        raise RuntimeError(f"SUMO config not found: {sumocfg}")

    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    logging.info("Using SUMO config: %s", sumocfg)
    logging.info("Writing results to: %s", results_dir)

    sumo_binary = resolve_sumo_binary(args.gui, args.sumo_binary)
    sumo_cmd = [
        sumo_binary,
        "-c",
        str(sumocfg),
        "--step-length",
        str(args.step_length),
        "--lateral-resolution",
        str(args.lateral_resolution),
        "--delay",
        "0",
    ]
    sumo_cmd.extend(sumocfg_input_overrides(sumocfg))

    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    router = ctx.socket(zmq.ROUTER)
    stop_event = threading.Event()
    unity_queue: queue.Queue[dict[str, Any]] = queue.Queue()

    rtf_file = None
    if args.rtf:
        rtf_file = (results_dir / "rtf_report.txt").open("w", encoding="utf-8")
        rtf_file.write("Time(s);RTF\n")

    previous_cwd = Path.cwd()
    os.chdir(sumocfg.parent)

    try:
        pub.bind(args.pub_endpoint)
        router.bind(args.router_endpoint)
        receiver = threading.Thread(
            target=receive_unity_loop,
            args=(router, unity_queue, stop_event),
            daemon=True,
        )
        receiver.start()

        logging.info("Starting SUMO: %s", " ".join(sumo_cmd))
        traci.start(sumo_cmd)

        if args.gui and not args.free_cam:
            try:
                traci.gui.trackVehicle("View #0", args.ego_id)
                traci.gui.setZoom("View #0", args.zoom)
            except Exception:
                logging.debug("SUMO GUI camera setup skipped", exc_info=True)

        last_positions: dict[str, tuple[float, float, float, float]] = {}
        latest_ego: dict[str, Any] | None = None
        start_recording_sent = False
        last_tl_send = -1.0
        last_rtf_wall = time.perf_counter()
        last_rtf_sim = 0.0
        next_wall_step = time.perf_counter()

        # Give subscribers a short window to connect after bind.
        time.sleep(0.25)

        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            sim_time = float(traci.simulation.getTime())

            while True:
                try:
                    payload = unity_queue.get_nowait()
                except queue.Empty:
                    break

                ego = parse_vehicle_payload(payload, args.ego_id)
                if ego:
                    latest_ego = ego

            if latest_ego:
                try_sync_ego_to_sumo(traci, args.ego_id, latest_ego)

            if sim_time >= args.integration_start_time:
                ego_xy = unity_vehicle_to_sumo_xy(latest_ego) if latest_ego else None
                vehicles = collect_vehicle_data(
                    traci,
                    args.ego_id,
                    ego_xy,
                    args.subscribe_radius,
                    last_positions,
                    sim_time,
                )
                publish_json(pub, {"type": "vehicles", "vehicles": vehicles})

                if sim_time - last_tl_send >= args.traffic_light_interval:
                    publish_json(pub, {"type": "trafficlights", "lights": collect_traffic_lights(traci)})
                    last_tl_send = sim_time

            if not start_recording_sent and sim_time >= args.experiment_start_time:
                publish_json(pub, {"type": "command", "command": "START_RECORDING"})
                start_recording_sent = True
                logging.info("Sent START_RECORDING at SUMO time %.2f", sim_time)

            if args.rtf and rtf_file and sim_time >= args.experiment_start_time:
                now = time.perf_counter()
                wall_delta = max(now - last_rtf_wall, 1e-9)
                sim_delta = max(sim_time - last_rtf_sim, 0.0)
                rtf_file.write(f"{sim_time:.2f};{sim_delta / wall_delta:.2f}\n")
                last_rtf_wall = now
                last_rtf_sim = sim_time

            if sim_time >= args.experiment_end_time:
                publish_json(pub, {"type": "command", "command": "STOP_RECORDING"})
                logging.info("Sent STOP_RECORDING at SUMO time %.2f", sim_time)
                break

            next_wall_step += float(args.step_length)
            sleep_until(next_wall_step)

    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    finally:
        stop_event.set()
        if rtf_file:
            rtf_file.close()

        try:
            if "traci" in locals() and traci.isLoaded():
                traci.close()
        except Exception:
            pass

        router.close(linger=0)
        pub.close(linger=0)
        ctx.term()
        os.chdir(previous_cwd)

    return 0


def build_parser() -> argparse.ArgumentParser:
    root = project_root()
    parser = argparse.ArgumentParser(description="Run the SUMO side of SUMO2Unity on macOS/Linux.")
    parser.add_argument("--sumocfg", default=str(root / "Scenario" / "Sumo2Unity.sumocfg"))
    parser.add_argument("--results-dir", default=str(root / "Results"))
    parser.add_argument("--integration-start-time", type=float, default=540.0)
    parser.add_argument("--experiment-start-time", type=float, default=600.0)
    parser.add_argument("--experiment-end-time", type=float, default=720.0)
    parser.add_argument("--step-length", type=float, default=0.1)
    parser.add_argument("--lateral-resolution", type=float, default=0.3)
    parser.add_argument("--zoom", type=float, default=150.0)
    parser.add_argument("--subscribe-radius", type=float, default=250.0)
    parser.add_argument("--traffic-light-interval", type=float, default=1.0)
    parser.add_argument("--ego-id", default=DEFAULT_EGO_ID)
    parser.add_argument("--pub-endpoint", default=DEFAULT_PUB_ENDPOINT)
    parser.add_argument("--router-endpoint", default=DEFAULT_ROUTER_ENDPOINT)
    parser.add_argument("--sumo-binary")
    parser.add_argument("--gui", dest="gui", action="store_true", default=True)
    parser.add_argument("--no-gui", dest="gui", action="store_false")
    parser.add_argument("--rtf", action="store_true")
    parser.add_argument("--free-cam", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    try:
        return run_simulation(args)
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
