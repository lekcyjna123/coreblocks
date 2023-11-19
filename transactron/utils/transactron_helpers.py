from contextlib import contextmanager
from typing import Literal, Optional, TypeAlias, cast, overload
from collections.abc import Callable, Iterable, Mapping
from ._typing import ValueLike, LayoutList, SignalBundle, HasElaborate, ModuleLike, ROGraph, GraphCC
import sys
from inspect import Parameter, signature
from typing import Any, Concatenate, Optional, TypeAlias, TypeGuard, TypeVar
from amaranth import *


__all__ = [
    "silence_mustuse",
    "_graph_ccs",
    "get_caller_class_name",
    "def_helper",
    "method_def_helper",
]

T = TypeVar("T")
U = TypeVar("U")

def _graph_ccs(gr: ROGraph[T]) -> list[GraphCC[T]]:
    """_graph_ccs

    Find connected components in a graph.

    Parameters
    ----------
    gr : Mapping[T, Iterable[T]]
        Graph in which we should find connected components. Encoded using
        adjacency lists.

    Returns
    -------
    ccs : List[Set[T]]
        Connected components of the graph `gr`.
    """
    ccs = []
    cc = set()
    visited = set()

    for v in gr.keys():
        q = [v]
        while q:
            w = q.pop()
            if w in visited:
                continue
            visited.add(w)
            cc.add(w)
            q.extend(gr[w])
        if cc:
            ccs.append(cc)
            cc = set()

    return ccs


def has_first_param(func: Callable[..., T], name: str, tp: type[U]) -> TypeGuard[Callable[Concatenate[U, ...], T]]:
    parameters = signature(func).parameters
    return (
        len(parameters) >= 1
        and next(iter(parameters)) == name
        and parameters[name].kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.POSITIONAL_ONLY}
        and parameters[name].annotation in {Parameter.empty, tp}
    )


def def_helper(description, func: Callable[..., T], tp: type[U], arg: U, /, **kwargs) -> T:
    parameters = signature(func).parameters
    kw_parameters = set(
        n for n, p in parameters.items() if p.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
    )
    if len(parameters) == 1 and has_first_param(func, "arg", tp):
        return func(arg)
    elif kw_parameters <= kwargs.keys():
        return func(**kwargs)
    else:
        raise TypeError(f"Invalid {description}: {func}")


def mock_def_helper(tb, func: Callable[..., T], arg: Mapping[str, Any]) -> T:
    return def_helper(f"mock definition for {tb}", func, Mapping[str, Any], arg, **arg)


def method_def_helper(method, func: Callable[..., T], arg: Record) -> T:
    return def_helper(f"method definition for {method}", func, Record, arg, **arg.fields)


def get_caller_class_name(default: Optional[str] = None) -> tuple[Optional[Elaboratable], str]:
    caller_frame = sys._getframe(2)
    if "self" in caller_frame.f_locals:
        owner = caller_frame.f_locals["self"]
        return owner, owner.__class__.__name__
    elif default is not None:
        return None, default
    else:
        raise RuntimeError("Not called from a method")

@contextmanager
def silence_mustuse(elaboratable: Elaboratable):
    try:
        yield
    except Exception:
        elaboratable._MustUse__silence = True  # type: ignore
        raise
