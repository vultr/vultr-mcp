"""The compiled interface is built once and shared across servers.

create_http_app builds 35 FastMCP servers, and each used to recompile the whole
interface layer for an identical result — ~21s of a ~26s boot spent doing the
same work 35 times. These pin the fix, and the properties that make sharing
safe.
"""

from __future__ import annotations

import time

import pytest

from vultr_mcp import server as S
from vultr_mcp.server import clear_interface_cache, load_interface, load_spec


@pytest.fixture(autouse=True)
def _clean_cache():
    """Each test starts cold, and leaves nothing behind for the next."""
    clear_interface_cache()
    yield
    clear_interface_cache()


@pytest.fixture(scope="module")
def spec():
    return load_spec()


def test_the_second_load_is_the_same_object(spec):
    """Not merely equal — the same instance, or servers are not sharing."""
    directory = S.interface_dir_from_env()
    assert load_interface(spec, directory) is load_interface(spec, directory)


def test_compile_runs_once_across_repeated_loads(spec, monkeypatch):
    """The point of the cache: the expensive call happens once."""
    calls = 0
    original = S.compile_interface

    def counting(directory, spec_arg):
        nonlocal calls
        calls += 1
        return original(directory, spec_arg)

    monkeypatch.setattr(S, "compile_interface", counting)

    directory = S.interface_dir_from_env()
    for _ in range(5):
        load_interface(spec, directory)

    assert calls == 1, f"compiled {calls} times; the cache is not holding"


def test_a_different_spec_recompiles(spec, monkeypatch):
    """Identity, not the path alone, or a scratch spec would get stale tools."""
    calls = 0
    original = S.compile_interface

    def counting(directory, spec_arg):
        nonlocal calls
        calls += 1
        return original(directory, spec_arg)

    monkeypatch.setattr(S, "compile_interface", counting)

    directory = S.interface_dir_from_env()
    load_interface(spec, directory)
    load_interface(dict(spec), directory)  # a distinct object with equal content

    assert calls == 2, "a different spec object must not reuse the cached compile"


def test_clearing_forces_a_recompile(spec, monkeypatch):
    """Tests that rewrite definitions on disk need this escape hatch."""
    calls = 0
    original = S.compile_interface

    def counting(directory, spec_arg):
        nonlocal calls
        calls += 1
        return original(directory, spec_arg)

    monkeypatch.setattr(S, "compile_interface", counting)

    directory = S.interface_dir_from_env()
    load_interface(spec, directory)
    clear_interface_cache()
    load_interface(spec, directory)

    assert calls == 2


def test_a_missing_directory_is_not_cached_as_a_hit(spec, tmp_path):
    """An absent layer returns the empty interface without poisoning the cache."""
    empty = load_interface(spec, tmp_path)
    assert empty.version == "none"
    assert not empty.tools

    real = load_interface(spec, S.interface_dir_from_env())
    assert real.tools, "the real directory must still compile"


def test_sharing_is_safe_because_the_compiled_types_are_frozen(spec):
    """Why one instance can back many servers: nothing can mutate it."""
    import dataclasses

    compiled = load_interface(spec, S.interface_dir_from_env())
    assert dataclasses.is_dataclass(compiled)

    tool = compiled.tools[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        tool.name = "mutated"  # type: ignore[misc]


async def test_two_servers_share_one_compile(spec, monkeypatch):
    """The behaviour that actually matters at boot."""
    calls = 0
    original = S.compile_interface

    def counting(directory, spec_arg):
        nonlocal calls
        calls += 1
        return original(directory, spec_arg)

    monkeypatch.setattr(S, "compile_interface", counting)

    S.create_server(spec)
    S.create_server(spec, only_categories={"instances"})

    assert calls == 1


def test_building_every_server_is_not_dominated_by_recompiling(spec):
    """A regression guard on the thing that caused a 150s startup probe to fail.

    Deliberately generous: this asserts the cache is working at all, not a
    particular machine's speed. Uncached, ten servers cost ~8s of compilation
    alone; cached, the compile happens once.
    """
    started = time.monotonic()
    for _ in range(10):
        S.create_server(spec, only_categories={"instances"})
    elapsed = time.monotonic() - started

    assert elapsed < 6.0, (
        f"ten servers took {elapsed:.1f}s; the interface is likely recompiling "
        "for each one"
    )
