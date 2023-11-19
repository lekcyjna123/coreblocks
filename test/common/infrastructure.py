import os
import random
import unittest
import functools
from contextlib import contextmanager, nullcontext
from typing import TypeVar, Generic, Type, TypeGuard, Any, Union, Callable, cast, TypeAlias
from abc import ABC
from amaranth import *
from amaranth.sim import *
from .testbenchio import TestbenchIO
from .functions import TestGen
from ..gtkw_extension import write_vcd_ext
from transactron import Method
from transactron.lib import AdapterTrans
from transactron.core import TransactionModule
from transactron.utils import ModuleConnector, HasElaborate, auto_debug_signals, HasDebugSignals

T = TypeVar("T")
_T_nested_collection: TypeAlias = T | list["_T_nested_collection[T]"] | dict[str, "_T_nested_collection[T]"]


def guard_nested_collection(cont: Any, t: Type[T]) -> TypeGuard[_T_nested_collection[T]]:
    if isinstance(cont, (list, dict)):
        if isinstance(cont, dict):
            cont = cont.values()
        return all([guard_nested_collection(elem, t) for elem in cont])
    elif isinstance(cont, t):
        return True
    else:
        return False


_T_HasElaborate = TypeVar("_T_HasElaborate", bound=HasElaborate)


class SimpleTestCircuit(Elaboratable, Generic[_T_HasElaborate]):
    def __init__(self, dut: _T_HasElaborate):
        self._dut = dut
        self._io: dict[str, _T_nested_collection[TestbenchIO]] = {}

    def __getattr__(self, name: str) -> Any:
        return self._io[name]

    def elaborate(self, platform):
        def transform_methods_to_testbenchios(
            container: _T_nested_collection[Method],
        ) -> tuple[_T_nested_collection["TestbenchIO"], Union[ModuleConnector, "TestbenchIO"]]:
            if isinstance(container, list):
                tb_list = []
                mc_list = []
                for elem in container:
                    tb, mc = transform_methods_to_testbenchios(elem)
                    tb_list.append(tb)
                    mc_list.append(mc)
                return tb_list, ModuleConnector(*mc_list)
            elif isinstance(container, dict):
                tb_dict = {}
                mc_dict = {}
                for name, elem in container.items():
                    tb, mc = transform_methods_to_testbenchios(elem)
                    tb_dict[name] = tb
                    mc_dict[name] = mc
                return tb_dict, ModuleConnector(*mc_dict)
            else:
                tb = TestbenchIO(AdapterTrans(container))
                return tb, tb

        m = Module()

        m.submodules.dut = self._dut

        for name, attr in vars(self._dut).items():
            if guard_nested_collection(attr, Method) and attr:
                tb_cont, mc = transform_methods_to_testbenchios(attr)
                self._io[name] = tb_cont
                m.submodules[name] = mc

        return m

    def debug_signals(self):
        sigs = {"_dut": auto_debug_signals(self._dut)}
        for name, io in self._io.items():
            sigs[name] = auto_debug_signals(io)
        return sigs


class TestModule(Elaboratable):
    def __init__(self, tested_module: HasElaborate, add_transaction_module):
        self.tested_module = TransactionModule(tested_module) if add_transaction_module else tested_module
        self.add_transaction_module = add_transaction_module

    def elaborate(self, platform) -> HasElaborate:
        m = Module()

        # so that Amaranth allows us to use add_clock
        _dummy = Signal()
        m.d.sync += _dummy.eq(1)

        m.submodules.tested_module = self.tested_module

        return m


class CoreblocksCommand(ABC):
    pass


class Now(CoreblocksCommand):
    pass


class Fork(CoreblocksCommand):
    def __init__(self, f: Callable[[], TestGen[None]]):
        self.f = f


class SyncProcessWrapper:
    def __init__(self, sim: "PysimSimulator", f: Callable[[], TestGen[None]]):
        self.org_process = f
        self.sim = sim
        self.current_cycle = 0

    def _wrapping_function(self):
        response = None
        org_coroutine = self.org_process()
        try:
            while True:
                # call orginal test process and catch data yielded by it in `command` variable
                command = org_coroutine.send(response)
                # If process wait for new cycle
                if command is None:
                    self.current_cycle += 1
                    # forward to amaranth
                    yield
                # Do early forward to amaranth
                elif not isinstance(command, CoreblocksCommand):
                    # Pass everything else to amaranth simulator without modifications
                    response = yield command
                elif isinstance(command, Now):
                    response = self.current_cycle
                elif isinstance(command, Fork):
                    f = command.f
                    self.sim.one_shot_callbacks.append(lambda: self.sim.add_sync_process(f))
                    response = None
                else:
                    raise RuntimeError(f"Unrecognized command: {command}")
        except StopIteration:
            pass


class PysimSimulator(Simulator):
    def __init__(self, module: HasElaborate, max_cycles: float = 10e4, add_transaction_module=True, traces_file=None):
        test_module = TestModule(module, add_transaction_module)
        tested_module = test_module.tested_module
        super().__init__(test_module)

        clk_period = 1e-6
        self.add_clock(clk_period)

        if isinstance(tested_module, HasDebugSignals):
            extra_signals = tested_module.debug_signals
        else:
            extra_signals = functools.partial(auto_debug_signals, tested_module)

        if traces_file:
            traces_dir = "test/__traces__"
            os.makedirs(traces_dir, exist_ok=True)
            # Signal handling is hacky and accesses Simulator internals.
            # TODO: try to merge with Amaranth.
            if isinstance(extra_signals, Callable):
                extra_signals = extra_signals()
            clocks = [d.clk for d in cast(Any, self)._fragment.domains.values()]

            self.ctx = write_vcd_ext(
                cast(Any, self)._engine,
                f"{traces_dir}/{traces_file}.vcd",
                f"{traces_dir}/{traces_file}.gtkw",
                traces=[clocks, extra_signals],
            )
        else:
            self.ctx = nullcontext()

        self.deadline = clk_period * max_cycles
        self.one_shot_callbacks = []

    def add_sync_process(self, f: Callable[[], TestGen]):
        f_wrapped = SyncProcessWrapper(self, f)
        super().add_sync_process(f_wrapped._wrapping_function)

    def run_until_with_callbacks(self, deadline, *, run_passive=False):
        """Run the simulation until it advances to `deadline` executing callbacks after each iteration.

        This function is based on `run_until` from amaranth Simulator class. After each `advance` step
        it calls all registred one shot callbacks. After execution of all one shot callbacks there are
        removed from the list before starting the next iteration.
        """
        # Convert deadline in seconds into internal amaranth 1 ps units
        deadline = deadline * 1e12
        assert cast(Any, self)._engine.now <= deadline
        while (self.advance() or run_passive) and cast(Any, self)._engine.now < deadline:
            for callback in self.one_shot_callbacks:
                callback()
            self.one_shot_callbacks.clear()

    def run(self) -> bool:
        with self.ctx:
            self.run_until_with_callbacks(self.deadline)

        return not self.advance()


class TestCaseWithSimulator(unittest.TestCase):
    @contextmanager
    def run_simulation(self, module: HasElaborate, max_cycles: float = 10e4, add_transaction_module=True):
        traces_file = None
        if "__COREBLOCKS_DUMP_TRACES" in os.environ:
            traces_file = unittest.TestCase.id(self)

        sim = PysimSimulator(
            module, max_cycles=max_cycles, add_transaction_module=add_transaction_module, traces_file=traces_file
        )
        yield sim
        res = sim.run()

        self.assertTrue(res, "Simulation time limit exceeded")

    def tick(self, cycle_cnt=1):
        """
        Yields for the given number of cycles.
        """

        for _ in range(cycle_cnt):
            yield

    def random_wait(self, max_cycle_cnt):
        """
        Wait for a random amount of cycles in range [1, max_cycle_cnt)
        """
        yield from self.tick(random.randrange(max_cycle_cnt))
