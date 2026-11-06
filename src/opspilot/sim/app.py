"""Topology of the simulated "hotel-reservation" microservice application.

The layout mirrors the DeathStarBench Hotel Reservation app that AIOpsLab deploys:
a frontend fans out to business services, which talk to their own databases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NAMESPACE = "hotel-reservation"
IMAGE_TAG = "1.4.2"
PREVIOUS_TAG = "1.4.1"


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    port: int
    depends_on: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    kind: str = "app"  # "app" or "db"
    startup_logs: tuple[str, ...] = ()

    @property
    def image(self) -> str:
        if self.kind == "db":
            return "mongo:6.0.14"
        return f"opspilot/hotel-{self.name}:{IMAGE_TAG}"

    @property
    def valid_images(self) -> set[str]:
        if self.kind == "db":
            return {"mongo:6.0.14", "mongo:6.0.13"}
        return {
            f"opspilot/hotel-{self.name}:{IMAGE_TAG}",
            f"opspilot/hotel-{self.name}:{PREVIOUS_TAG}",
        }


def _app(name: str, port: int, deps: tuple[str, ...] = (), db: str | None = None) -> ServiceSpec:
    env = {"SERVICE_PORT": str(port), "LOG_LEVEL": "info"}
    required: tuple[str, ...] = ("SERVICE_PORT",)
    if db:
        env["DB_URL"] = f"mongodb://{db}:27017/{name}"
        required = ("SERVICE_PORT", "DB_URL")
    logs = (
        f'level=info msg="starting {name} service" port={port}',
        'level=info msg="loaded config" source=env',
    )
    if db:
        logs += (f'level=info msg="connected to database" url=mongodb://{db}:27017',)
    logs += (f'level=info msg="gRPC server listening" addr=:{port}',)
    return ServiceSpec(name=name, port=port, depends_on=deps + ((db,) if db else ()),
                       required_env=required, env=env, startup_logs=logs)


def _db(name: str) -> ServiceSpec:
    return ServiceSpec(
        name=name, port=27017, kind="db",
        startup_logs=(
            '{"t":{"$date":"2026-11-05T09:12:01.114Z"},"s":"I","c":"NETWORK","msg":"Waiting for connections","attr":{"port":27017}}',
        ),
    )


SERVICES: dict[str, ServiceSpec] = {
    s.name: s
    for s in [
        _app("frontend", 5000, deps=("search", "profile", "recommendation", "reservation", "user")),
        _app("search", 8082, deps=("geo", "rate")),
        _app("geo", 8083, db="mongodb-geo"),
        _app("rate", 8084, db="mongodb-rate"),
        _app("profile", 8081, db="mongodb-profile"),
        _app("recommendation", 8085, db="mongodb-recommendation"),
        _app("reservation", 8087, db="mongodb-reservation"),
        _app("user", 8086, db="mongodb-user"),
        _db("mongodb-geo"),
        _db("mongodb-rate"),
        _db("mongodb-profile"),
        _db("mongodb-recommendation"),
        _db("mongodb-reservation"),
        _db("mongodb-user"),
    ]
}

NODES = [
    {"name": "kind-control-plane", "role": "control-plane"},
    {"name": "kind-worker", "role": "<none>"},
    {"name": "kind-worker2", "role": "<none>"},
]

APP_DESCRIPTION = (
    "Hotel Reservation is a microservice application (based on DeathStarBench) that lets "
    "users search hotels, view profiles and recommendations, and make reservations. "
    "It consists of a frontend, business-logic services (search, geo, rate, profile, "
    "recommendation, reservation, user) communicating over gRPC, and one MongoDB "
    "database per stateful service."
)
