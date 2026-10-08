# Stand-in UTAPI server for local testing only -- NOT part of the real deployment.
# Simulates vacuum faults, a chamber pressure spike, and a low/filling LN2 dewar
# so you can see tem_utapi_monitor.py react without touching the real instrument.
import os
import sys

_GENERATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated")
if os.path.isdir(_GENERATED_DIR):
    sys.path.insert(0, _GENERATED_DIR)

import time
import grpc
from concurrent import futures

from vacuum import vacuum_pb2, vacuum_pb2_grpc
from vacuum import vacuum_chambers_pb2, vacuum_chambers_pb2_grpc
from sample.v1 import temperature_pb2, temperature_pb2_grpc


class FakeVacuumService(vacuum_pb2_grpc.VacuumServiceServicer):
    def GetVacuumState(self, request, context):
        return vacuum_pb2.VacuumStateResponse(state=vacuum_pb2.VacuumStateResponse.VACUUM_STATE_READY)

    def WatchVacuumState(self, request, context):
        yield vacuum_pb2.VacuumStateResponse(state=vacuum_pb2.VacuumStateResponse.VACUUM_STATE_READY)
        time.sleep(1)
        yield vacuum_pb2.VacuumStateResponse(state=vacuum_pb2.VacuumStateResponse.VACUUM_STATE_NOT_READY)
        time.sleep(1)
        yield vacuum_pb2.VacuumStateResponse(state=vacuum_pb2.VacuumStateResponse.VACUUM_STATE_READY)
        while True:
            time.sleep(5)
            yield vacuum_pb2.VacuumStateResponse(state=vacuum_pb2.VacuumStateResponse.VACUUM_STATE_READY)

    def GetIsPrevacuumPumpRunning(self, request, context):
        return vacuum_pb2.IsPrevacuumPumpRunningResponse(is_prevacuum_pump_running=False)


class FakeVacuumChambersService(vacuum_chambers_pb2_grpc.VacuumChambersServiceServicer):
    def GetVacuumChambers(self, request, context):
        return vacuum_chambers_pb2.GetVacuumChambersResponse(chambers=["column", "gun"])

    def WatchVacuumChambersPressure(self, request, context):
        yield vacuum_chambers_pb2.WatchVacuumChambersPressureResponse(chamber_pressures=[
            vacuum_chambers_pb2.VacuumChamberPressure(
                chamber="column",
                pressure_collection=vacuum_chambers_pb2.ChamberPressureCollection(pressure=2.1e-7),
            )
        ])
        time.sleep(1)
        yield vacuum_chambers_pb2.WatchVacuumChambersPressureResponse(chamber_pressures=[
            vacuum_chambers_pb2.VacuumChamberPressure(
                chamber="column",
                pressure_collection=vacuum_chambers_pb2.ChamberPressureCollection(pressure=5.4e-4),
            )
        ])
        while True:
            time.sleep(5)


class FakeTemperatureService(temperature_pb2_grpc.TemperatureServiceServicer):
    def GetDewarRefrigerantLevel(self, request, context):
        return temperature_pb2.GetDewarRefrigerantLevelResponse(level=0.15)

    def GetTimeUntilDewarRefill(self, request, context):
        return temperature_pb2.GetTimeUntilDewarRefillResponse(remaining_time=1800)

    def IsFillingDewar(self, request, context):
        return temperature_pb2.IsFillingDewarResponse(is_filling=True)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    vacuum_pb2_grpc.add_VacuumServiceServicer_to_server(FakeVacuumService(), server)
    vacuum_chambers_pb2_grpc.add_VacuumChambersServiceServicer_to_server(FakeVacuumChambersService(), server)
    temperature_pb2_grpc.add_TemperatureServiceServicer_to_server(FakeTemperatureService(), server)
    server.add_insecure_port("localhost:46699")
    server.start()
    print("Fake UTAPI server running on localhost:46699 -- Ctrl+C to stop")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
