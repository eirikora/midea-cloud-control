#!/usr/bin/env python3
"""Query (and optionally control) an appliance without Home Assistant.

    python tools/probe.py --user EMAIL --password PW list
    python tools/probe.py --user EMAIL --password PW state --id 1539...
    python tools/probe.py --user EMAIL --password PW set --id 1539... --temp 22
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import pathlib
import sys
import types

# Load the integration's modules without importing its Home Assistant entry point.
_COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "midea_cloud_control"
)
_pkg = types.ModuleType("_mcc")
_pkg.__path__ = [str(_COMPONENT)]
sys.modules["_mcc"] = _pkg
for _name in ("protocol", "client"):
    _spec = importlib.util.spec_from_file_location(f"_mcc.{_name}", _COMPONENT / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"_mcc.{_name}"] = _mod
    _spec.loader.exec_module(_mod)

from _mcc.client import SUPPORTED, MideaCloudClient  # noqa: E402

FIELDS = ("power_on", "operational_mode", "target_temperature", "indoor_temperature",
          "outdoor_temperature", "fan_speed", "swing_mode", "eco", "turbo", "sleep")


def _dump(state) -> None:
    for field in FIELDS:
        print(f"  {field:22s} = {getattr(state, field, None)}")


async def run(args: argparse.Namespace) -> int:
    async with MideaCloudClient(args.user, args.password, args.cloud) as client:
        if args.command == "list":
            appliances = await client.list_appliances()
            if not appliances:
                print("no appliances on this account")
                return 1
            for aid, info in appliances.items():
                print(f"  {aid}  {info.get('name')}  type={info.get('type')} "
                      f"online={info.get('online')}")
            return 0

        if args.command == "state":
            _dump(await client.query(args.id))
            return 0

        changes = {}
        if args.temp is not None:
            changes["target_temperature"] = args.temp
        if args.power is not None:
            changes["power_on"] = args.power == "on"
        if not changes:
            print("nothing to set")
            return 1
        await client.set_state(args.id, **changes)
        await asyncio.sleep(3)
        print("state after set:")
        _dump(await client.query(args.id))
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("list", "state", "set"))
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--cloud", default="NetHome Plus", choices=SUPPORTED)
    p.add_argument("--id", type=int, help="appliance id")
    p.add_argument("--temp", type=float, help="target temperature")
    p.add_argument("--power", choices=("on", "off"))
    p.add_argument("-d", "--debug", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    if args.command in ("state", "set") and args.id is None:
        p.error("--id is required for that command")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
