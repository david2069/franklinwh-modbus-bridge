"""FastAPI application with staged lifespan startup."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from franklinwh_bridge import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="franklinwh-modbus-bridge",
    version=__version__,
    lifespan=lifespan,
)
