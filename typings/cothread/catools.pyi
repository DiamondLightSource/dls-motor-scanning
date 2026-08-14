"""Minimal stubs for the parts of cothread.catools this project uses.

cothread ships no type information and no stub package exists on PyPI, so
pyright infers Unknown for every call. These stubs state the dynamic typing
deliberately: caget's return type genuinely depends on the PV's data type and
on the format/count arguments.
"""

from typing import Any

FORMAT_RAW: int
FORMAT_TIME: int
FORMAT_CTRL: int

def caget(pvs: Any, **kargs: Any) -> Any: ...
def caput(pvs: Any, values: Any, **kargs: Any) -> Any: ...
