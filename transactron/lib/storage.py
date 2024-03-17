from amaranth import *
from amaranth.utils import *

from transactron.utils.transactron_helpers import from_method_layout, make_layout
from ..core import *
from ..utils import SrcLoc, get_src_loc, MultiPriorityEncoder
from typing import Optional
from transactron.utils import assign, AssignType, LayoutList
from .reqres import ArgumentsToResultsZipper

__all__ = ["MemoryBank", "ContentAddressableMemory"]


class MemoryBank(Elaboratable):
    """MemoryBank module.

    Provides a transactional interface to synchronous Amaranth Memory with one
    read and one write port. It supports optionally writing with given granularity.

    Attributes
    ----------
    read_req: Method
        The read request method. Accepts an `addr` from which data should be read.
        Only ready if there is there is a place to buffer response.
    read_resp: Method
        The read response method. Return `data_layout` View which was saved on `addr` given by last
        `read_req` method call. Only ready after `read_req` call.
    write: Method
        The write method. Accepts `addr` where data should be saved, `data` in form of `data_layout`
        and optionally `mask` if `granularity` is not None. `1` in mask means that appropriate part should be written.
    """

    def __init__(
        self,
        *,
        data_layout: LayoutList,
        elem_count: int,
        granularity: Optional[int] = None,
        safe_writes: bool = True,
        src_loc: int | SrcLoc = 0
    ):
        """
        Parameters
        ----------
        data_layout: method layout
            The format of structures stored in the Memory.
        elem_count: int
            Number of elements stored in Memory.
        granularity: Optional[int]
            Granularity of write, forwarded to Amaranth. If `None` the whole structure is always saved at once.
            If not, the width of `data_layout` is split into `granularity` parts, which can be saved independently.
        safe_writes: bool
            Set to `False` if an optimisation can be done to increase throughput of writes. This will cause that
            writes will be reordered with respect to reads eg. in sequence "read A, write A X", read can return
            "X" even when write was called later. By default `True`, which disable optimisation.
        src_loc: int | SrcLoc
            How many stack frames deep the source location is taken from.
            Alternatively, the source location to use instead of the default.
        """
        self.src_loc = get_src_loc(src_loc)
        self.data_layout = make_layout(*data_layout)
        self.elem_count = elem_count
        self.granularity = granularity
        self.width = from_method_layout(self.data_layout).size
        self.addr_width = bits_for(self.elem_count - 1)
        self.safe_writes = safe_writes

        self.read_req_layout: LayoutList = [("addr", self.addr_width)]
        write_layout = [("addr", self.addr_width), ("data", self.data_layout)]
        if self.granularity is not None:
            write_layout.append(("mask", self.width // self.granularity))
        self.write_layout = make_layout(*write_layout)

        self.read_req = Method(i=self.read_req_layout, src_loc=self.src_loc)
        self.read_resp = Method(o=self.data_layout, src_loc=self.src_loc)
        self.write = Method(i=self.write_layout, src_loc=self.src_loc)
        self._internal_read_resp_trans = None

    def elaborate(self, platform) -> TModule:
        m = TModule()

        mem = Memory(width=self.width, depth=self.elem_count)
        m.submodules.read_port = read_port = mem.read_port()
        m.submodules.write_port = write_port = mem.write_port()
        read_output_valid = Signal()
        prev_read_addr = Signal(self.addr_width)
        write_pending = Signal()
        write_req = Signal()
        write_args = Signal(self.write_layout)
        write_args_prev = Signal(self.write_layout)
        m.d.comb += read_port.addr.eq(prev_read_addr)

        zipper = ArgumentsToResultsZipper([("valid", 1)], self.data_layout)
        m.submodules.zipper = zipper

        self._internal_read_resp_trans = Transaction(src_loc=self.src_loc)
        with self._internal_read_resp_trans.body(m, request=read_output_valid):
            m.d.sync += read_output_valid.eq(0)
            zipper.write_results(m, read_port.data)

        write_trans = Transaction(src_loc=self.src_loc)
        with write_trans.body(m, request=write_req | (~read_output_valid & write_pending)):
            if self.safe_writes:
                with m.If(write_pending):
                    m.d.comb += assign(write_args, write_args_prev, fields=AssignType.ALL)
            m.d.sync += write_pending.eq(0)
            m.d.comb += write_port.addr.eq(write_args.addr)
            m.d.comb += write_port.data.eq(write_args.data)
            if self.granularity is None:
                m.d.comb += write_port.en.eq(1)
            else:
                m.d.comb += write_port.en.eq(write_args.mask)

        @def_method(m, self.read_resp)
        def _():
            output = zipper.read(m)
            return output.results

        @def_method(m, self.read_req, ~write_pending)
        def _(addr):
            m.d.sync += read_output_valid.eq(1)
            m.d.comb += read_port.addr.eq(addr)
            m.d.sync += prev_read_addr.eq(addr)
            zipper.write_args(m, valid=1)

        @def_method(m, self.write, ~write_pending)
        def _(arg):
            if self.safe_writes:
                with m.If((arg.addr == read_port.addr) & (read_output_valid | self.read_req.run)):
                    m.d.sync += write_pending.eq(1)
                    m.d.sync += assign(write_args_prev, arg, fields=AssignType.ALL)
                with m.Else():
                    m.d.comb += write_req.eq(1)
            else:
                m.d.comb += write_req.eq(1)
            m.d.comb += assign(write_args, arg, fields=AssignType.ALL)

        return m


class ContentAddressableMemory(Elaboratable):
    """Content addresable memory

    This module implements a content-addressable memory (in short CAM) with Transactron interface. 
    CAM is a type of memory where instead of predefined indexes there are used values feed in runtime
    as keys (smimlar as in python dictionary). To insert new entry a pair `(key, value)` has to be
    provided. Such pair takes an free slot which depends on internal implementation. To read value
    a `key` has to be provided. It is compared with every valid key stored in CAM. If there is a hit,
    a value is read. There can be many instances of the same key in CAM. In such case it is undefined
    which value will be read.


    .. warning::
       Current implementation has critical path O(entries_number). If needed we can
       optimise it in future to have O(log(entries_number)).


    Attributes
    ----------
    pop : Method
        Looks for the data in memory and, if found, returns it and removes it.
    push : Method
        Inserts new data.
    """

    def __init__(self, address_layout: MethodLayout, data_layout: MethodLayout, entries_number: int):
        """
        Parameters
        ----------
        address_layout : LayoutLike
            The layout of the address records.
        data_layout : LayoutLike
            The layout of the data.
        entries_number : int
            The number of slots to create in memory.
        """
        self.address_layout = from_method_layout(address_layout)
        self.data_layout = from_method_layout(data_layout)
        self.entries_number = entries_number

        self.pop = Method(i=[("addr", self.address_layout)], o=[("data", self.data_layout), ("not_found", 1)])
        self.push = Method(i=[("addr", self.address_layout), ("data", self.data_layout)])

    def elaborate(self, platform) -> TModule:
        m = TModule()

        address_array = Array([Signal(self.address_layout) for _ in range(self.entries_number)])
        data_array = Array([Signal(self.data_layout) for _ in range(self.entries_number)])
        valids = Signal(self.entries_number, name="valids")

        m.submodules.encoder_addr = encoder_addr = MultiPriorityEncoder(self.entries_number, 1)
        m.submodules.encoder_valids = encoder_valids = MultiPriorityEncoder(self.entries_number, 1)
        m.d.comb += encoder_valids.input.eq(~valids)

        @def_method(m, self.push, ready=~valids.all())
        def _(addr, data):
            id = Signal(range(self.entries_number), name="id_push")
            m.d.comb += id.eq(encoder_valids.outputs[0])
            m.d.sync += address_array[id].eq(addr)
            m.d.sync += data_array[id].eq(data)
            m.d.sync += valids.bit_select(id, 1).eq(1)

        if_addr = Signal(self.entries_number, name="if_addr")
        data_to_send = Record(self.data_layout)

        @def_method(m, self.pop)
        def _(addr):
            m.d.top_comb += if_addr.eq(Cat([addr == stored_addr for stored_addr in address_array]) & valids)
            id = encoder_addr.outputs[0]
            with m.If(if_addr.any()):
                m.d.comb += data_to_send.eq(data_array[id])
                m.d.sync += valids.bit_select(id, 1).eq(0)

            return {"data": data_to_send, "not_found": ~if_addr.any()}

        m.d.comb += encoder_addr.input.eq(if_addr)

        return m
