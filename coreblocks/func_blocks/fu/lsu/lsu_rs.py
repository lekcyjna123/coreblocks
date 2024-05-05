from amaranth import *
from typing import Optional, Iterable
from coreblocks.func_blocks.fu.common.fifo_rs import FifoRS
from coreblocks.params import *
from coreblocks.arch import OpType

__all__ = ["LsuRS"]


class LsuRS(FifoRS):
    def __init__(
        self,
        gen_params: GenParams,
        rs_entries: int,
        rs_number: int,
        ready_for: Optional[Iterable[Iterable[OpType]]] = None,
    ) -> None:
        super().__init__(gen_params, rs_entries, rs_number, ready_for)
