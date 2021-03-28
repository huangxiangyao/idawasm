from .decode import decode_bytecode as decode_bytecode
from .opcodes import INSN_ENTER_BLOCK as INSN_ENTER_BLOCK, INSN_LEAVE_BLOCK as INSN_LEAVE_BLOCK
from .wasmtypes import IMMUTABLE as IMMUTABLE, MUTABLE as MUTABLE, VAL_TYPE_F32 as VAL_TYPE_F32, VAL_TYPE_F64 as VAL_TYPE_F64, VAL_TYPE_I32 as VAL_TYPE_I32, VAL_TYPE_I64 as VAL_TYPE_I64
from typing import Any, Optional

def format_instruction(insn: Any): ...
def format_mutability(mutability: Any): ...
def format_lang_type(lang_type: Any): ...
def format_function(func_body: Any, func_type: Optional[Any] = ..., indent: int = ..., format_locals: bool = ...) -> None: ...
