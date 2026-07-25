"""Declarations for portable HELIX actors and callbacks.

The decorators retain metadata for the host reference executor.  ``helixc``
also reads their syntax directly from the Python AST, so compiling a module
does not execute application source.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CallbackMetadata:
    kind: str
    argument: object = None
    period: object = None
    phase: object = None


@dataclass(frozen=True)
class ModuleMetadata:
    name: str
    api: str
    profile: str


def _callback(kind, *, argument=None, period=None, phase=None):
    def decorate(function):
        function.__helix_callback__ = CallbackMetadata(
            kind=kind, argument=argument, period=period, phase=phase
        )
        return function

    return decorate


def module(*, name, api, profile="application"):
    def decorate(cls):
        cls.__helix_module__ = ModuleMetadata(
            name=str(name), api=str(api), profile=str(profile)
        )
        return cls

    return decorate


def on_start(function):
    return _callback("start")(function)


def on_message(message_type):
    return _callback("message", argument=message_type)


def on_timer(*, period, phase=None):
    return _callback("timer", period=period, phase=phase)


def on_observation(config_field):
    return _callback("observation", argument=config_field)


def on_parameters(function):
    return _callback("parameters")(function)


def on_cancel(function):
    return _callback("cancel")(function)


def on_shutdown(function):
    return _callback("shutdown")(function)


def machine_program(*, resources=(), timeout=None):
    def decorate(function):
        function.__helix_machine_program__ = {
            "resources": tuple(resources),
            "timeout": timeout,
        }
        return function

    return decorate


__all__ = [
    "CallbackMetadata",
    "ModuleMetadata",
    "machine_program",
    "module",
    "on_cancel",
    "on_message",
    "on_observation",
    "on_parameters",
    "on_shutdown",
    "on_start",
    "on_timer",
]
