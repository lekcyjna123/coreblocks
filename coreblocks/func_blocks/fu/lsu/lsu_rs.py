from amaranth import *
from amaranth.lib.data import *
from typing import Optional, Iterable
from coreblocks.func_blocks.fu.common.fifo_rs import FifoRS
from coreblocks.params import *
from coreblocks.arch import OpType
from coreblocks.interface.layouts import RSLayouts
from transactron.utils.transactron_helpers import make_layout
from transactron.utils import ValueLike
from transactron import Method, TModule, def_method, Transaction
from transactron.lib.metrics import HwExpHistogram, TaggedLatencyMeasurer

__all__ = ["LsuRS"]

class LsuRS(Elaboratable):
    def __init__(
        self,
        gen_params: GenParams,
        rs_entries: int,
        rs_number: int,
        ready_for: Optional[Iterable[Iterable[OpType]]] = None,
        rob_get_indices : Optional[Method] = None
    ) -> None:
        self.gen_params = gen_params
        self.rs_entries = rs_entries
        self.rs_entries_bits = (rs_entries - 1).bit_length()
        self.layouts = gen_params.get(RSLayouts, rs_entries_bits=self.rs_entries_bits)
        self.internal_layout = make_layout(
            ("rs_data", self.layouts.rs.data_layout),
            ("full", 1),
            ("reserved", 1),
            ("depends", self.rs_entries)
        )

        self.insert = Method(i=self.layouts.rs.insert_in)
        self.select = Method(o=self.layouts.rs.select_out)
        self.update = Method(i=self.layouts.rs.update_in)
        self.take = Method(i=self.layouts.take_in, o=self.layouts.take_out)

        self.data = Array(Signal(self.internal_layout) for _ in range(self.rs_entries))
        self.data_ready = Signal(self.rs_entries)

        assert rob_get_indices is not None, "Temporary check before implementation of proper interface"
        self.rob_get_indices = rob_get_indices

    def calculate_address(self, i):
        # Address is valid if the entry is full and we already have the rs1 value
        valid = ~self.data[i].rs_data.rp_s1.bool() & self.data[i].full
        addr = self.data[i].rs_data.s1_val + self.data[i].rs_data.imm
        return addr, valid

    def is_second_older(self, rob_id_fst, rob_id_snd):
        return (rob_id_fst - self.rob_start_idx).as_unsigned() < (rob_id_snd - self.rob_start_idx).as_unsigned()

    def check_same_address_access(self, addr1, addr2):
        """
        Check if two memory accesses can conflict with each other. We assume that
        all memory operations are aligned, so it is safe to compare whole address
        except two least significant bits.

        If future a more finegrained comparison rules can be created to take into
        account different lenght of accesses.
        """
        return (addr1 >> 2) == (addr2 >> 2)

    def elaborate(self, platform):
        m = TModule()

        is_fence_waiting = Signal()
        is_fence_waiting_comb = Signal()
        self.rob_start_idx = Signal(self.gen_params.rob_entries_bits)
        
        reserved_signals = Signal(self.rs_entries)
        m.d.top_comb += reserved_signals.eq(Cat(i.reserved for i in self.data))
        slot_available = ~reserved_signals.all()
        select_possible = slot_available & ~ (is_fence_waiting | is_fence_waiting_comb)

        for i, record in enumerate(self.data):
            m.d.comb += self.data_ready[i].eq(
                ~record.rs_data.rp_s1.bool() & ~record.rs_data.rp_s2.bool() & record.rec_full.bool()
            )

        @def_method(m, self.select, ready = select_possible )
        def _():
            pass

        @def_method(m, self.insert)
        def _(rs_entry_id, rs_data):
            # Block selecting new RS enries after we received Fence.
            with m.If((rs_data.exec_fn.op_type == OpType.FENCE) | (rs_data.exec_fn.op_type == OpType.FENCEI)):
                m.d.comb += is_fence_waiting_comb.eq(1)
                m.d.sync += is_fence_waiting.eq(1)

            # Try to calculate memory address of this instruction
            this_instr_addr_v = ~rs_data.rp_s1.bool()
            this_instr_addr = rs_data.s1_val + rs_data.imm

            # Look for all older instructions which can potentialy conflict with us.
            # There is a conflict if both instructions access the same address or
            # if one of them doesn't know the address yet.
            depends = Signal(self.rs_entries)
            for i in range(self.rs_entries):
                addr, valid = self.calculate_address(i)
                m.d.top_comb += depends[i].eq(~valid | ~this_instr_addr_v | self.check_same_address_access(addr, this_instr_addr))

            m.d.sync += self.data[rs_entry_id].rs_data.eq(rs_data)
            m.d.sync += self.data[rs_entry_id].rec_full.eq(1)
            m.d.sync += self.data[rs_entry_id].rec_reserved.eq(1)
            m.d.sync += self.data[rs_entry_id].depends.eq(depends)


        with Transaction().body(m):
            m.d.top_comb += self.rob_start_idx.eq(self.rob_get_indices(m).start)

        return m
