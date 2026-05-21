from typing import Annotated

from fastapi import Depends, Request

from app.factory import PMEngine


def get_engine(request: Request) -> PMEngine:
    return request.app.state.engine
