#!/usr/bin/env python3
"""Inspect and exercise the local DCS JSON socket bridge (state API)."""

import argparse
import json
import socket
import sys
import uuid


def exchange(host: str, port: int, operation: str, *, request_id: str | None = None, **fields: object) -> dict:
    request = {"v": 1, "id": request_id or uuid.uuid4().hex, "op": operation, **fields}
    payload = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(payload)
        with connection.makefile("rb") as stream:
            line = stream.readline(2 * 1024 * 1024 + 1)
    if not line.endswith(b"\n") or len(line) > 2 * 1024 * 1024:
        raise RuntimeError("No complete response from DCS hook")
    reply = json.loads(line)
    if not isinstance(reply, dict) or reply.get("v") != 1 or reply.get("id") != request["id"]:
        raise RuntimeError("Unexpected response ID or protocol version")
    return reply


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10309)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("status")
    test = commands.add_parser("move-test", help="Move Blue ground group 150 m northeast")
    test.add_argument("--group", default="ATC Blue Ground")
    move = commands.add_parser("move", help="Move a ground group to DCS x/z coordinates")
    move.add_argument("group")
    move.add_argument("x", type=float)
    move.add_argument("z", type=float)
    geo = commands.add_parser("move-geo", help="Move a ground group to map latitude/longitude")
    geo.add_argument("group")
    geo.add_argument("lat", type=float)
    geo.add_argument("lon", type=float)
    hold = commands.add_parser("hold")
    hold.add_argument("group")
    args = parser.parse_args()

    fields = {}
    operation = args.operation
    if operation == "move-test":
        snapshot = exchange(args.host, args.port, "status")
        if not snapshot.get("ok"):
            raise SystemExit(json.dumps(snapshot, indent=2))
        matching = [g for g in snapshot["groups"] if g["name"] == args.group and g["units"]]
        if len(matching) != 1:
            raise SystemExit(f"Expected one active group named {args.group!r}")
        point = matching[0]["units"][0]
        fields = {"group_name": args.group, "route_data": {"points": [
            {"x": point["x"], "y": point["z"], "action": "Off Road", "speed": 5, "speed_locked": True},
            {"x": point["x"] + 150, "y": point["z"] + 150,
             "action": "Off Road", "speed": 5, "speed_locked": True},
        ]}}
        operation = "set_route"
    elif operation == "move":
        snapshot = exchange(args.host, args.port, "status")
        matching = [g for g in snapshot.get("groups", []) if g["name"] == args.group and g["units"]]
        if len(matching) != 1:
            raise SystemExit(f"Expected one active group named {args.group!r}")
        point = matching[0]["units"][0]
        fields = {"group_name": args.group, "route_data": {"points": [
            {"x": point["x"], "y": point["z"], "action": "Off Road", "speed": 5, "speed_locked": True},
            {"x": args.x, "y": args.z, "action": "Off Road", "speed": 5, "speed_locked": True},
        ]}}
        operation = "set_route"
    elif operation == "move-geo":
        snapshot = exchange(args.host, args.port, "status")
        matching = [g for g in snapshot.get("groups", []) if g["name"] == args.group and g["units"]]
        if len(matching) != 1:
            raise SystemExit(f"Expected one active group named {args.group!r}")
        point = matching[0]["units"][0]
        fields = {"group_name": args.group, "route_data": {"points": [
            {"action": "Off Road", "speed": 5, "speed_locked": True,
             "__geo": {"lat": point["lat"], "lon": point["lon"]}},
            {"action": "Off Road", "speed": 5, "speed_locked": True,
             "__geo": {"lat": args.lat, "lon": args.lon}},
        ]}}
        operation = "set_route"
    elif operation == "hold":
        fields = {"group_name": args.group, "task_data": {"id": "Hold", "params": {}}}
        operation = "set_task"

    reply = exchange(args.host, args.port, operation, **fields)
    print(json.dumps(reply, indent=2, ensure_ascii=False))
    if not reply.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"State bridge error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
