#!/usr/bin/env python3

# Compiles the UTAPI .proto files this monitor needs into Python gRPC
# stubs. Run this once (and again if Thermo updates the API folder).
#
# Usage:
#     python compile_protos.py --api-dir "/path/to/Thermo UTAPI/API"
#
# --api-dir should point at the "API" folder Thermo ships alongside
# utapi_server.exe on the instrument control PC (the one with
# vacuum/, sample/, utapi_types/, etc. subfolders) NOT a copy bundled
# into this project, since it's Thermo's confidential/proprietary
# material and shouldn't be duplicated into version control.
#
# Only the files this monitor actually uses are compiled:
#     vacuum/vacuum.proto
#     vacuum/vacuum_chambers.proto
#     vacuum/column_valves.proto
#     sample/v1/temperature.proto
#     utapi_types/v1/utapi_response.proto
#     utapi_types/v1/version_extensions.proto
#     utapi_types/v1/device_id.proto
#     utapi_types/v1/vector.proto
#
# Output goes to ./generated (gitignored) with the same package layout
# Thermo's own example scripts use, e.g. `from vacuum import vacuum_pb2`.

import argparse
import os
import shutil
import sys

try:
    import grpc_tools.protoc
    import grpc_tools
except ImportError:
    sys.exit("Missing dependency grpcio-tools. Run: pip install -r requirements_utapi.txt")

NEEDED_PROTOS = [
    "vacuum/vacuum.proto",
    "vacuum/vacuum_chambers.proto",
    "vacuum/column_valves.proto",
    "sample/v1/temperature.proto",
    "utapi_types/v1/utapi_response.proto",
    "utapi_types/v1/version_extensions.proto",
    "utapi_types/v1/device_id.proto",
    "utapi_types/v1/vector.proto",
]

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-dir", required=True,
        help="Path to Thermo's UTAPI 'API' folder (contains vacuum/, sample/, utapi_types/, ...).",
    )
    args = parser.parse_args()

    api_dir = os.path.abspath(args.api_dir)
    if not os.path.isdir(api_dir):
        sys.exit(f"--api-dir does not exist or is not a directory: {api_dir}")

    missing = [p for p in NEEDED_PROTOS if not os.path.isfile(os.path.join(api_dir, p))]
    if missing:
        sys.exit(
            "Some expected .proto files were not found under --api-dir:\n  "
            + "\n  ".join(missing)
            + "\n\nIf Thermo's folder structure differs on your installation (e.g. a 'v1' "
            "subfolder under vacuum/, or different file names), update NEEDED_PROTOS in "
            "this script to match what you actually see under the API folder."
        )

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR)

    protobuf_include = os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")

    for rel_path in NEEDED_PROTOS:
        full_path = os.path.join(api_dir, rel_path)
        command = [
            "grpc_tools.protoc",
            f"--proto_path={protobuf_include}",
            f"--proto_path={api_dir}",
            f"--python_out={OUTPUT_DIR}",
            f"--grpc_python_out={OUTPUT_DIR}",
            full_path,
        ]
        print(f"Compiling {rel_path} ...")
        if grpc_tools.protoc.main(command) != 0:
            sys.exit(f"protoc failed on {rel_path}")

    # grpc_tools doesn't create __init__.py files; add them so the generated
    # tree importable as packages (e.g. `from vacuum import vacuum_pb2`).
    for root, _dirs, _files in os.walk(OUTPUT_DIR):
        init_path = os.path.join(root, "__init__.py")
        if not os.path.exists(init_path):
            open(init_path, "w").close()

    print(f"\nDone. Generated stubs are in: {OUTPUT_DIR}")
    print("tem_utapi_monitor.py adds this folder to sys.path automatically.")


if __name__ == "__main__":
    main()
